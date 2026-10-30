#!/usr/bin/env python3
"""Publish every Codex thread loaded behind a Codex client as a rozi Activity row, and switch the
client to a row's thread when the row is selected.

Codex runs its threads in a shared local app-server daemon. A Codex client shows one thread at a
time, and the threads it switched away from stay loaded in the daemon, often each in its own Git
worktree. rozi sees the client as one pane. This service asks the daemon which threads are loaded,
publishes one row per thread into the pane with the directory it works in, and answers a row
activation by switching the client to that thread through Codex's own `/resume` command.

Everything goes through public interfaces: `codex app-server daemon version`, the app-server
protocol's `thread/loaded/list` and `thread/read`, and rozi's `list-panes`, `capture-pane`,
`send-text`, `send-keys`, `notify`, and `publish`.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import signal
import stat
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from typing import Any, Callable


ROZI = os.environ.get("ROZI_BIN", "rozi")
EXTENSION_ID = "codex-rozi-sessions"
VERSION = "0.4.2"
# How often the pane titles are read between polls, so that a switch made in Codex shows up at once.
TITLE_INTERVAL = 0.5
# Foreground programs that are a Codex client.
CLIENT_PROGRAMS = {"codex", "codex-cli"}
COMMAND_TIMEOUT = 5.0
SOCKET_TIMEOUT = 5.0
# How long to wait for Codex to show the selected thread after `/resume`.
CONFIRM_ATTEMPTS = 20
CONFIRM_INTERVAL = 0.5
# How long to wait for the typed command to read back from Codex's prompt before giving up.
VERIFY_ATTEMPTS = 10
VERIFY_INTERVAL = 0.2
PROMPT = "›"
# A numbered choice under the selection cursor: an approval or question, not the prompt.
CHOICE = re.compile(rf"^{PROMPT}\s*\d+\.\s")
# A turn still running. Codex keeps the prompt open while it works, to queue the next message.
WORKING_FOOTER = "esc to interrupt"
# Codex's terminal title puts a spinner in front of the thread name while a turn runs.
SPINNER = re.compile(r"^[⠀-⣿\s]+")
TITLE_SEPARATOR = " | "
# Codex's `thread-id` title item, which it shortens with an ellipsis. Two UUID groups at least, so
# a project or thread name that happens to be hex is not taken for one.
THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}[0-9a-f-]*")
ELLIPSIS = ("...", "…")
# What a row says for the blank thread a client opens before its first prompt.
NEW_THREAD = "New thread"
# Codex refuses `/resume` while a turn runs, and offers no other way to switch until it ends.
BUSY = "Codex can't switch threads mid-turn. Select the row again once it finishes."
# Codex's answers to a `/resume` it did not carry out.
SWITCH_FAILURES = (
    "No saved chat found",
    "Failed to resume",
    "Failed to attach",
    "Failed to view thread",
    "is ambiguous",
    "is disabled while a task is in progress",
)


class SessionsError(RuntimeError):
    """A public interface failed or answered something unreadable."""


@dataclass(frozen=True)
class Settings:
    codex: str = "codex"
    poll_seconds: float = 2.0
    scope: str = "all"
    switching: bool = True

    @classmethod
    def from_environment(cls, environ: dict[str, str]) -> Settings:
        try:
            values = json.loads(environ.get("ROZI_EXTENSION_CONFIG") or "{}")
        except json.JSONDecodeError:
            values = {}
        if not isinstance(values, dict):
            values = {}
        codex = values.get("codex")
        scope = values.get("scope")
        switching = values.get("switching")
        try:
            poll = float(values.get("poll_seconds", cls.poll_seconds))
        except (TypeError, ValueError):
            poll = cls.poll_seconds
        return cls(
            codex=codex if isinstance(codex, str) and codex.strip() else cls.codex,
            poll_seconds=min(max(poll, 0.5), 60.0),
            scope=scope if scope in {"all", "cwd"} else cls.scope,
            switching=switching if isinstance(switching, bool) else cls.switching,
        )


@dataclass(frozen=True)
class Thread:
    thread_id: str
    name: str | None
    preview: str | None
    cwd: str | None
    status: str
    flags: frozenset[str]
    subagent: bool
    ephemeral: bool
    created_at: int

    @classmethod
    def from_wire(cls, value: object) -> Thread | None:
        if not isinstance(value, dict):
            return None
        thread_id = text(value.get("id"))
        if thread_id is None:
            return None
        status = value.get("status") if isinstance(value.get("status"), dict) else {}
        flags = status.get("activeFlags") if isinstance(status.get("activeFlags"), list) else []
        return cls(
            thread_id=thread_id,
            name=text(value.get("name")),
            preview=text(value.get("preview")),
            cwd=text(value.get("cwd")),
            status=text(status.get("type")) or "",
            flags=frozenset(flag for flag in flags if isinstance(flag, str)),
            subagent=bool(text(value.get("parentThreadId"))),
            ephemeral=value.get("ephemeral") is True,
            created_at=integer(value.get("createdAt")) or 0,
        )

    @property
    def short_id(self) -> str:
        return self.thread_id[:8]

    def is_listable(self) -> bool:
        """A conversation the user started: not a sub-agent, which belongs to its parent's turn,
        not an ephemeral side conversation, and not the blank thread a client opens before its
        first prompt."""
        return not self.subagent and not self.ephemeral and bool(self.name or self.preview)

    def is_blank(self) -> bool:
        """The thread a client opens before its first prompt: Codex's start screen."""
        return not self.subagent and not self.ephemeral and not (self.name or self.preview)

    def is_working(self) -> bool:
        return self.status == "active"

    def row_state(self, finished: bool) -> tuple[str, str | None]:
        """The rozi status this thread shows, and why when the word alone does not say.

        `finished` is whether this service saw the thread's last turn run to completion."""
        if self.status == "active":
            if "waitingOnApproval" in self.flags:
                return "blocked", "Approval required"
            if "waitingOnUserInput" in self.flags:
                return "blocked", "Question needs an answer"
            return "working", None
        if self.status == "systemError":
            return "error", "Thread failed"
        return ("done" if finished else "idle"), None

    def label(self) -> str:
        return self.name or self.short_id

    def row(self, active: bool, finished: bool) -> dict[str, object]:
        status, reason = self.row_state(finished)
        row: dict[str, object] = {
            "id": self.thread_id,
            "title": self.name or first_line(self.preview) or NEW_THREAD,
            "status": status,
            "active": active,
            # Ties the row to the thread Codex's own hooks report on, if they run.
            "native_session": self.thread_id,
        }
        if reason:
            row["reason"] = reason
        if self.cwd:
            row["cwd"] = self.cwd
        return row


# A pane's identity: its session instance and its id, which repeats across sessions.
PaneKey = tuple[str, int]


@dataclass(frozen=True)
class Pane:
    pane_id: int
    cwd: str | None
    program: str | None
    agent: str | None
    title: str | None
    # The thread the pane's Codex hooks last reported, from rozi releases that say.
    session: str | None = None
    # The session the pane belongs to, and whether it is the one on screen. Commands naming a pane
    # reach the session on screen, so `capture-pane` and `send-text` are for those panes only.
    instance: str | None = None
    on_screen: bool = True

    @classmethod
    def from_wire(cls, value: object) -> Pane | None:
        if not isinstance(value, dict):
            return None
        pane_id = integer(value.get("id"))
        if pane_id is None:
            return None
        return cls(
            pane_id=pane_id,
            cwd=text(value.get("cwd")),
            program=text(value.get("foreground_program")),
            agent=text(value.get("agent")),
            title=text(value.get("title")),
            session=text(value.get("agent_session")),
            instance=session_instance(value),
            on_screen=value.get("on_screen") is not False,
        )

    @property
    def key(self) -> PaneKey:
        return self.instance or "", self.pane_id

    def runs_codex(self) -> bool:
        """A pane running a Codex client. The service runs on each session's host and rozi lists
        it only the panes of that host's sessions, so every client it sees talks to the same
        background server it reads."""
        return self.agent == "codex" or (self.program or "").casefold() in CLIENT_PROGRAMS


@dataclass
class HostMemory:
    """What the service remembers about one Codex pane between polls."""

    # The thread this service last switched the client to, for when the title does not say.
    last_target: str | None = None
    # The thread the title's spinner last told apart from others sharing its name, for when the
    # spinner no longer does.
    last_shown: str | None = None
    published: list[dict[str, object]] | None = None


@dataclass(frozen=True)
class Composer:
    text: str
    """What the user has typed, with Codex's dim placeholder left out."""


@dataclass(frozen=True)
class Screen:
    composer: Composer | None
    working: bool

    @property
    def draft(self) -> bool:
        return self.composer is not None and bool(self.composer.text.strip())


def text(value: object) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def integer(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def first_line(value: str | None) -> str | None:
    return text(value.splitlines()[0]) if value else None


def response_data(output: str, command: str) -> Any:
    try:
        response = json.loads(output)
    except json.JSONDecodeError as error:
        raise SessionsError(f"rozi {command} returned invalid JSON") from error
    if not isinstance(response, dict) or response.get("ok") is not True:
        detail = response.get("error") if isinstance(response, dict) else None
        raise SessionsError(str(detail or f"rozi {command} failed"))
    return response.get("data")


def parse_panes(output: str) -> list[Pane]:
    data = response_data(output, "list-panes")
    if not isinstance(data, list):
        raise SessionsError("rozi list-panes returned a non-list payload")
    return [pane for item in data if (pane := Pane.from_wire(item)) is not None]


def title_names(title: str | None) -> list[str]:
    """The names a Codex terminal title may carry: `⠋ <thread name> | <project>` by default, and
    any subset of those items in that form once configured with `/title`."""
    if not title:
        return []
    return [name for part in title.split(TITLE_SEPARATOR) if (name := SPINNER.sub("", part).strip())]


def title_thread_ids(title: str | None) -> list[str]:
    """The thread ID prefixes a title carries once `/title` includes `thread-id`."""
    prefixes = []
    for name in title_names(title):
        for ellipsis in ELLIPSIS:
            name = name.removesuffix(ellipsis)
        if THREAD_ID.fullmatch(name):
            prefixes.append(name)
    return prefixes


def id_shown_thread(prefixes: list[str], threads: list[Thread]) -> str | None:
    """The one thread whose ID starts with a prefix from the title."""
    matches = [
        thread.thread_id
        for thread in threads
        if any(thread.thread_id.startswith(prefix) for prefix in prefixes)
    ]
    return matches[0] if len(matches) == 1 else None


def title_spins(title: str | None) -> bool:
    """Whether the title carries Codex's spinner, which it shows while the thread on screen runs a
    turn."""
    spinner = SPINNER.match(title or "")
    return spinner is not None and any("⠁" <= char <= "⣿" for char in spinner.group())


def titled_threads(title: str | None, threads: list[Thread]) -> list[str]:
    """The threads whose name the title carries."""
    names = set(title_names(title))
    return [thread.thread_id for thread in threads if thread.name and thread.name in names]


def shown_thread(title: str | None, threads: list[Thread]) -> str | None:
    """The thread whose name the title carries, when exactly one listed thread has that name."""
    matches = titled_threads(title, threads)
    return matches[0] if len(matches) == 1 else None


def is_under(path: str | None, root: str | None) -> bool:
    if not path or not root:
        return False
    path = os.path.normpath(path)
    root = os.path.normpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def session_instance(pane: dict[str, Any]) -> str | None:
    reference = pane.get("reference")
    return text(reference.get("session_instance")) if isinstance(reference, dict) else None


@dataclass(frozen=True)
class Host:
    """A pane running a Codex client, and the thread it shows, when that can be told."""

    pane: Pane
    active: str | None


def classify(
    panes: list[Pane], threads: list[Thread], memory: dict[int, HostMemory]
) -> list[Host]:
    """The Codex panes, each with the thread it shows when that can be told.

    A title carrying the thread ID says so exactly, even for a thread too new to list, which leaves
    no row on screen. Without one: a title naming exactly one thread, which follows a switch at
    once; else the thread Codex's hooks last reported, which also names a new thread before Codex
    has named it and tells threads sharing a name apart; else the last switch this service made;
    else the one thread sharing the titled name that runs exactly when the title spins, and once
    the spinner has told it apart, that thread for as long as no other signal says otherwise.

    When the title names several threads, only one of those can be on screen, so a hook report or
    switch from before a `/resume` to another of them is not taken for it."""
    hosts = []
    by_id = {thread.thread_id: thread for thread in threads}
    listable = [thread for thread in threads if thread.is_listable()]
    loaded = {thread.thread_id for thread in threads if thread.is_listable() or thread.is_blank()}
    for pane in panes:
        if not pane.runs_codex():
            continue
        prefixes = title_thread_ids(pane.title)
        if prefixes:
            hosts.append(Host(pane=pane, active=id_shown_thread(prefixes, threads)))
            continue
        titled = titled_threads(pane.title, listable)
        active = titled[0] if len(titled) == 1 else None
        candidates = set(titled) or loaded
        remembered = memory.setdefault(pane.key, HostMemory())
        for fallback in (pane.session, remembered.last_target):
            if active is None and fallback in candidates:
                active = fallback
        if active is None and len(titled) > 1:
            spins = title_spins(pane.title)
            running = [tid for tid in titled if by_id[tid].is_working() == spins]
            if len(running) == 1:
                remembered.last_shown = running[0]
            if remembered.last_shown in candidates:
                active = remembered.last_shown
        hosts.append(Host(pane=pane, active=active))
    return hosts


def assign_owners(
    hosts: list[Host], threads: list[Thread], owners: dict[str, PaneKey], scope: str
) -> dict[str, int]:
    """The one Codex pane each listable thread is published in.

    Every client lists the daemon's threads, so a thread published by each pane it could belong
    to would appear once per pane. A thread belongs to the pane that shows it, then to the pane it
    last belonged to, so it stays put while a title cannot be read for a moment and after the
    client moves on to another thread. A thread no pane has claimed yet goes to the pane working
    nearest above its directory, the lowest pane on a tie; with `scope = "all"`, to the lowest
    pane when none contains it."""
    by_pane = {host.pane.key: host for host in hosts}
    shown: dict[str, int] = {}
    for host in sorted(hosts, key=lambda host: host.pane.key):
        if host.active:
            shown.setdefault(host.active, host.pane.key)
    assigned = {}
    for thread in threads:
        if not thread.is_listable():
            continue
        owner = shown.get(thread.thread_id)
        if owner is None and owners.get(thread.thread_id) in by_pane:
            owner = owners[thread.thread_id]
        if owner is None:
            owner = nearest_pane(thread, hosts, scope)
        if owner is not None:
            assigned[thread.thread_id] = owner
    return assigned


def nearest_pane(thread: Thread, hosts: list[Host], scope: str) -> int | None:
    containing = [host for host in hosts if is_under(thread.cwd, host.pane.cwd)]
    if containing:
        nearest = min(
            containing,
            key=lambda host: (-len(os.path.normpath(host.pane.cwd or "")), host.pane.key),
        )
        return nearest.pane.key
    if scope == "all" and hosts:
        return min(host.pane.key for host in hosts)
    return None


def listed_threads(host: Host, threads: list[Thread], owners: dict[str, PaneKey]) -> list[Thread]:
    """The threads one Codex pane owns, oldest first.

    The order does not follow the thread on screen, so switching never moves a row: rozi names a
    pane's rows by position."""
    listed = [thread for thread in threads if owners.get(thread.thread_id) == host.pane.key]
    listed.sort(key=lambda thread: (thread.created_at, thread.thread_id))
    return listed


def host_rows(
    host: Host, threads: list[Thread], owners: dict[str, PaneKey], finished: set[str]
) -> list[dict[str, object]]:
    """The rows a Codex pane publishes.

    A client that owns only one thread gets a row once that thread is known to be on screen: Codex
    runs a background-server thread's hooks outside the pane, so the row is what tells rozi its
    state and which conversation a restore resumes. Not before, since a row off screen would hide
    the agent rozi detects on a blank new thread."""
    listed = listed_threads(host, threads, owners)
    if len(listed) < 2 and not (listed and listed[0].thread_id == host.active):
        return []
    # A client on its start screen still shows something: the blank thread is its active row
    # beside the threads it owns, and becomes that thread's own row after the first prompt.
    blank = next(
        (thread for thread in threads if thread.thread_id == host.active and thread.is_blank()),
        None,
    )
    if blank is not None:
        listed = sorted(listed + [blank], key=lambda thread: (thread.created_at, thread.thread_id))
    return [
        thread.row(thread.thread_id == host.active, thread.thread_id in finished)
        for thread in listed
    ]


def row_text(row: list[dict[str, Any]]) -> str:
    return "".join(span.get("text", "") for span in row)


def composer_rows(rows: list[list[dict[str, Any]]]) -> list[list[dict[str, Any]]] | None:
    """Codex's prompt: the last row that starts with `›` on a shaded background, and the rows
    below it that share that background. `None` when there is no prompt, as under a dialog, whose
    choices use the same cursor without the shading."""
    for index in range(len(rows) - 1, -1, -1):
        row = rows[index]
        spans = [span for span in row if span.get("text")]
        if not spans or not spans[0]["text"].startswith(PROMPT):
            continue
        background = spans[0].get("bg")
        if background is None or CHOICE.match(row_text(row).lstrip()):
            return None
        block = [row]
        for below in rows[index + 1 :]:
            if not below or any(span.get("bg") != background for span in below):
                break
            block.append(below)
        return block
    return None


def read_composer(block: list[list[dict[str, Any]]]) -> Composer:
    """What the user has typed: every span that is not Codex's dim placeholder, minus the `›`."""
    typed = []
    for index, row in enumerate(block):
        for span in row:
            content = span.get("text", "")
            if index == 0 and content.lstrip().startswith(PROMPT):
                content = content.lstrip()[len(PROMPT) :]
            if span.get("dim"):
                continue
            typed.append(content)
        typed.append("\n")
    return Composer(text="".join(typed).replace("\xa0", " ").strip())


def read_screen(frame_rows: list[list[dict[str, Any]]]) -> Screen:
    block = composer_rows(frame_rows)
    joined = "\n".join(row_text(row) for row in frame_rows)
    return Screen(
        composer=read_composer(block) if block is not None else None,
        working=WORKING_FOOTER in joined,
    )


def switch_failure(screen_text: str, command: str) -> str | None:
    """Codex's refusal of `command`, read only below the last place it echoed that command, so an
    older refusal still on screen is not mistaken for this one."""
    lines = screen_text.splitlines()
    echoed = [index for index, line in enumerate(lines) if command in line]
    start = echoed[-1] + 1 if echoed else 0
    for line in lines[start:]:
        if any(failure in line for failure in SWITCH_FAILURES):
            return line.strip(" ■•\xa0")
    return None


def resume_command(thread_id: str) -> str:
    return f"/resume {thread_id}"


@dataclass(frozen=True)
class Refusal:
    message: str


def switch_refusal(screen: Screen) -> Refusal | None:
    """Why typing into the client now could lose or misdirect something, if it could."""
    if screen.composer is None:
        return Refusal(
            "Codex is showing an approval, a question, or a list. Answer or close it, then select "
            "the row again."
        )
    if screen.draft:
        return Refusal(
            "Codex's prompt has unsent text. Send or clear it, then select the row again."
        )
    return None


class AppServer:
    """A connection to Codex's local app-server daemon, speaking its JSON-RPC protocol over the
    WebSocket the daemon serves on its control socket."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.sock: socket.socket | None = None
        self.buffer = b""
        self.next_id = 1

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.buffer = b""

    def socket_path(self) -> str | None:
        """The running daemon's socket, or `None` when no daemon runs. Never starts one."""
        try:
            result = subprocess.run(
                [self.settings.codex, "app-server", "daemon", "version"],
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SessionsError(f"{self.settings.codex}: {error}") from error
        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            data = None
        if not isinstance(data, dict):
            detail = text(result.stderr) or f"exit {result.returncode}"
            raise SessionsError(f"codex app-server daemon version failed: {detail}")
        if data.get("status") != "running":
            return None
        return text(data.get("socketPath"))

    def connect(self) -> bool:
        if self.sock is not None:
            return True
        path = self.socket_path()
        if path is None or not hasattr(socket, "AF_UNIX"):
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(SOCKET_TIMEOUT)
        try:
            sock.connect(path)
            key = base64.b64encode(os.urandom(16)).decode()
            sock.sendall(
                (
                    "GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\n"
                    f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                    "Sec-WebSocket-Version: 13\r\n\r\n"
                ).encode()
            )
            response = b""
            while b"\r\n\r\n" not in response:
                chunk = sock.recv(4096)
                if not chunk:
                    raise SessionsError("the Codex daemon closed the connection")
                response += chunk
        except (OSError, SessionsError) as error:
            sock.close()
            raise SessionsError(f"cannot connect to the Codex daemon: {error}") from error
        head, _, rest = response.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            sock.close()
            raise SessionsError("the Codex daemon refused the connection")
        self.sock, self.buffer = sock, rest
        try:
            self.request(
                "initialize",
                {
                    "clientInfo": {"name": EXTENSION_ID, "title": "rozi", "version": VERSION},
                    "capabilities": {"optOutNotificationMethods": ["account/updated"]},
                },
            )
            self.send({"method": "initialized"})
        except SessionsError:
            self.close()
            raise
        return True

    def send(self, message: dict[str, object]) -> None:
        assert self.sock is not None
        data = json.dumps(message, separators=(",", ":")).encode()
        self.send_frame(0x1, data)

    def send_frame(self, opcode: int, data: bytes) -> None:
        assert self.sock is not None
        mask = os.urandom(4)
        length = len(data)
        if length < 126:
            header = bytes([0x80 | opcode, 0x80 | length])
        elif length < 1 << 16:
            header = bytes([0x80 | opcode, 0x80 | 126]) + struct.pack(">H", length)
        else:
            header = bytes([0x80 | opcode, 0x80 | 127]) + struct.pack(">Q", length)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(data))
        try:
            self.sock.sendall(header + mask + masked)
        except OSError as error:
            self.close()
            raise SessionsError(f"lost the Codex daemon: {error}") from error

    def read_exact(self, count: int) -> bytes:
        assert self.sock is not None
        while len(self.buffer) < count:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise OSError("connection closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def read_message(self) -> bytes:
        """One complete text message, answering pings and joining fragments on the way."""
        message = b""
        while True:
            first, second = self.read_exact(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self.read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self.read_exact(8))[0]
            mask = self.read_exact(4) if second & 0x80 else b""
            payload = self.read_exact(length)
            if mask:
                payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
            opcode = first & 0x0F
            if opcode == 0x8:
                raise OSError("the daemon closed the connection")
            if opcode == 0x9:
                self.send_frame(0xA, payload)
                continue
            if opcode in {0x1, 0x2, 0x0}:
                message += payload
                if first & 0x80:
                    return message

    def request(self, method: str, params: dict[str, object]) -> Any:
        if self.sock is None:
            raise SessionsError("not connected to the Codex daemon")
        request_id = self.next_id
        self.next_id += 1
        self.send({"id": request_id, "method": method, "params": params})
        try:
            while True:
                reply = json.loads(self.read_message())
                # Notifications and the daemon's own requests are not this service's business.
                if isinstance(reply, dict) and reply.get("id") == request_id and "method" not in reply:
                    break
        except (OSError, ValueError) as error:
            self.close()
            raise SessionsError(f"lost the Codex daemon: {error}") from error
        if "error" in reply:
            error = reply["error"]
            detail = error.get("message") if isinstance(error, dict) else error
            raise SessionsError(f"Codex {method} failed: {detail}")
        return reply.get("result")

    def loaded_threads(self) -> list[Thread]:
        """Every thread the daemon has loaded, or none when no daemon runs."""
        if not self.connect():
            return []
        ids: list[str] = []
        cursor = None
        while True:
            result = self.request("thread/loaded/list", {"cursor": cursor})
            data = result.get("data") if isinstance(result, dict) else None
            ids += [item for item in data or [] if isinstance(item, str)]
            cursor = result.get("nextCursor") if isinstance(result, dict) else None
            if not cursor:
                break
        threads = []
        for thread_id in ids:
            try:
                result = self.request("thread/read", {"threadId": thread_id, "includeTurns": False})
            except SessionsError:
                if self.sock is None:
                    raise
                # Unloaded between the two calls.
                continue
            thread = Thread.from_wire(result.get("thread") if isinstance(result, dict) else None)
            if thread is not None and thread.status != "notLoaded":
                threads.append(thread)
        return threads


class Cli:
    """The external interfaces the service uses. Tests substitute their own."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.daemon = AppServer(settings)

    def run(self, args: list[str]) -> str:
        try:
            result = subprocess.run(
                args,
                text=True,
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=COMMAND_TIMEOUT,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise SessionsError(f"{args[0]}: {error}") from error
        if result.returncode != 0:
            detail = text(result.stderr) or text(result.stdout) or f"exit {result.returncode}"
            raise SessionsError(f"{' '.join(args[:3])} failed: {detail}")
        return result.stdout

    def threads(self) -> list[Thread]:
        return self.daemon.loaded_threads()

    def panes(self) -> list[Pane]:
        return parse_panes(
            self.run([ROZI, "list-panes", "--scope", "attached", "--format", "json"])
        )

    def pane(self, pane_id: int) -> Pane | None:
        return next(
            (pane for pane in self.panes() if pane.pane_id == pane_id and pane.on_screen), None
        )

    def screen_text(self, pane_id: int) -> str:
        data = response_data(
            self.run([ROZI, "capture-pane", "--target", str(pane_id), "--format", "json"]),
            "capture-pane",
        )
        return str(data.get("text", "")) if isinstance(data, dict) else ""

    def screen(self, pane_id: int) -> Screen:
        data = response_data(
            self.run(
                [
                    ROZI,
                    "capture-pane",
                    "--target",
                    str(pane_id),
                    "--render",
                    "spans",
                    "--format",
                    "json",
                ]
            ),
            "capture-pane",
        )
        rows = data.get("frame", {}).get("rows", []) if isinstance(data, dict) else []
        return read_screen(rows)

    def type_text(self, pane_id: int, value: str) -> None:
        self.run([ROZI, "send-text", "--target", str(pane_id), value])

    def press(self, pane_id: int, key: str) -> None:
        self.run([ROZI, "send-keys", "--target", str(pane_id), key])

    def notify(self, message: str, *, error: bool = False) -> None:
        args = [ROZI, "notify", "--title", "Codex"]
        if error:
            args += ["--level", "error"]
        try:
            self.run(args + ["--", message[:240]])
        except SessionsError:
            pass

    def close(self) -> None:
        self.daemon.close()


def prompt_holds(screen: Screen, command: str) -> bool:
    return screen.composer is not None and screen.composer.text == command


def switch(
    cli: Cli,
    host: Host,
    target: Thread,
    memory: HostMemory,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Put the Codex pane's client on `target`, or say why not. `/resume` also opens a thread
    Codex has unloaded since it was listed.

    Never types into a prompt that holds unsent text or shows a dialog, or while a turn runs,
    which Codex refuses `/resume` during. Reads the typed command back right before Enter, and
    reports success only once Codex names the target. Returns whether it did."""
    pane_id = host.pane.pane_id
    if target.thread_id == host.active:
        return True
    threads = {thread.thread_id: thread for thread in cli.threads()}
    threads.setdefault(target.thread_id, target)
    screen = cli.screen(pane_id)
    shown = threads.get(host.active) if host.active else None
    if screen.working or (shown is not None and shown.is_working()):
        cli.notify(BUSY, error=True)
        return False
    refusal = switch_refusal(screen)
    if refusal is not None:
        cli.notify(refusal.message, error=True)
        return False

    command = resume_command(target.thread_id)
    cli.type_text(pane_id, command)
    # Codex redraws its prompt, and opens its slash-command hints, a moment after the keys land.
    for attempt in range(VERIFY_ATTEMPTS):
        typed = cli.screen(pane_id)
        if prompt_holds(typed, command):
            break
        if attempt + 1 < VERIFY_ATTEMPTS:
            sleep(VERIFY_INTERVAL)
    if prompt_holds(typed, command) and typed.working:
        cli.notify(f"{BUSY} The command is left unsent in its prompt.", error=True)
        return False
    if not prompt_holds(typed, command):
        cli.notify(
            "Could not confirm the switch command in Codex's prompt, so it was not sent. "
            "Check the prompt before pressing Enter.",
            error=True,
        )
        return False
    cli.press(pane_id, "Enter")
    for _ in range(CONFIRM_ATTEMPTS):
        sleep(CONFIRM_INTERVAL)
        failure = switch_failure(cli.screen_text(pane_id), command)
        if failure is not None:
            cli.notify(f"Codex did not switch: {failure}", error=True)
            return False
        if switch_confirmed(target, cli.pane(pane_id), list(threads.values())):
            memory.last_target = target.thread_id
            return True
    # No answer either way. Codex may still switch, so nothing is recorded as on screen: the
    # active row keeps following what Codex itself shows.
    cli.notify(
        f"Could not confirm that Codex switched to “{target.label()}”. "
        "Check the pane before selecting another row.",
        error=True,
    )
    return False


def switch_confirmed(target: Thread, pane: Pane | None, threads: list[Thread]) -> bool:
    """Whether Codex now shows `target`, from a title that carries its ID, or else names it and no
    other thread.

    Without the ID, a thread without a name, or one sharing its name, cannot be confirmed."""
    if pane is None:
        return False
    prefixes = title_thread_ids(pane.title)
    if prefixes:
        return id_shown_thread(prefixes, threads) == target.thread_id
    if not target.name:
        return False
    names = set(title_names(pane.title))
    if target.name not in names:
        return False
    return not any(
        thread.thread_id != target.thread_id and thread.name == target.name for thread in threads
    )


def write_line(stream: Any, value: object) -> None:
    stream.write(json.dumps(value, separators=(",", ":")) + "\n")
    stream.flush()


@dataclass
class Publisher:
    token: int
    process: subprocess.Popen[str]


@dataclass
class Observed:
    """Turns this service saw finish, kept as `done` rows until the user has looked at them.

    Codex unloads an idle thread from its daemon soon after its turn ends, often before anyone has
    read the result. A finished thread therefore stays listed from its last snapshot, even once
    unloaded, until a Codex pane has shown it and moved on."""

    working: dict[str, Thread] = field(default_factory=dict)
    finished: dict[str, Thread] = field(default_factory=dict)
    viewed: set[str] = field(default_factory=set)

    def update(self, threads: list[Thread]) -> None:
        loaded = {thread.thread_id for thread in threads}
        for thread in threads:
            if thread.is_working():
                self.working[thread.thread_id] = thread
                self.finished.pop(thread.thread_id, None)
                self.viewed.discard(thread.thread_id)
            elif self.working.pop(thread.thread_id, None) is not None:
                if thread.status == "idle":
                    self.finished[thread.thread_id] = thread
            elif thread.thread_id in self.finished:
                self.finished[thread.thread_id] = thread
        # A turn that ended and was unloaded between two polls.
        for thread_id in set(self.working) - loaded:
            ended = self.working.pop(thread_id)
            self.finished[thread_id] = replace(ended, status="idle", flags=frozenset())

    def mark_viewed(self, shown: set[str]) -> None:
        """Forget each finished thread a pane showed and has since moved away from."""
        for thread_id in list(self.finished):
            if thread_id in shown:
                self.viewed.add(thread_id)
            elif thread_id in self.viewed:
                del self.finished[thread_id]
                self.viewed.discard(thread_id)

    def listed(self, threads: list[Thread]) -> list[Thread]:
        """The loaded threads, plus finished ones Codex has unloaded since."""
        loaded = {thread.thread_id for thread in threads}
        return threads + [
            thread for thread_id, thread in self.finished.items() if thread_id not in loaded
        ]


class SessionsService:
    def __init__(self, settings: Settings, cli: Cli | None = None) -> None:
        self.settings = settings
        self.cli = cli or Cli(settings)
        self.messages: queue.Queue[tuple[object, ...]] = queue.Queue()
        self.publishers: dict[int, Publisher] = {}
        self.memory: dict[int, HostMemory] = {}
        self.hosts: dict[int, Host] = {}
        # The pane each thread is published in, kept between polls; see `assign_owners`.
        self.owners: dict[str, PaneKey] = {}
        # Every thread the last poll could list, by id, for resolving a row activation.
        self.known: dict[str, Thread] = {}
        # The thread names each Codex pane's title carried when last read, to notice a switch.
        self.titles: dict[PaneKey, frozenset[str]] | None = None
        self.observed = Observed()
        self.next_token = 1
        self.reported_error: str | None = None

    def start_publisher(self, key: PaneKey) -> Publisher:
        token = self.next_token
        self.next_token += 1
        environment = os.environ.copy()
        # These name the pane a publish stream belongs to, and it stays with that session's pane
        # whichever session is on screen.
        instance, pane_id = key
        environment["ROZI_PANE"] = str(pane_id)
        if instance:
            environment["ROZI_SESSION_INSTANCE"] = instance
        else:
            environment.pop("ROZI_SESSION_INSTANCE", None)
        # `--kind agent`: each row is a Codex thread, so rozi's Agents tab and picker list it with the
        # other agents, not only among the Activity rows.
        process = subprocess.Popen(
            [ROZI, "publish", "--kind", "agent"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
            env=environment,
        )
        publisher = Publisher(token=token, process=process)
        self.publishers[key] = publisher

        def read() -> None:
            assert process.stdout is not None
            for line in process.stdout:
                row_id = activation(line)
                if row_id is not None:
                    self.messages.put(("activate", key, token, row_id))
            process.wait()
            self.messages.put(("closed", key, token))

        threading.Thread(target=read, name=f"publish-{pane_id}", daemon=True).start()
        return publisher

    def stop_publisher(self, key: PaneKey) -> None:
        publisher = self.publishers.pop(key, None)
        if publisher is None:
            return
        try:
            if publisher.process.stdin is not None:
                publisher.process.stdin.close()
        except OSError:
            pass
        try:
            publisher.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            publisher.process.kill()

    def publish(self, key: PaneKey, rows: list[dict[str, object]]) -> None:
        memory = self.memory.setdefault(key, HostMemory())
        publisher = self.publishers.get(key)
        if publisher is None:
            if not rows:
                return
            publisher = self.start_publisher(key)
            memory.published = None
        if memory.published == rows:
            return
        try:
            assert publisher.process.stdin is not None
            write_line(publisher.process.stdin, {"rows": rows})
            memory.published = rows
        except (BrokenPipeError, OSError):
            # The pane went away or the UI moved to another session. Try again next poll.
            self.stop_publisher(key)

    def poll(self) -> None:
        loaded = self.cli.threads()
        self.observed.update(loaded)
        hosts = classify(self.cli.panes(), self.observed.listed(loaded), self.memory)
        self.observed.mark_viewed({host.active for host in hosts if host.active})
        threads = self.observed.listed(loaded)
        self.known = {thread.thread_id: thread for thread in threads}
        self.hosts = {host.pane.key: host for host in hosts}
        # A pane is gone when no attached session lists it. A session off screen is still listed.
        for key in set(self.publishers) - set(self.hosts):
            self.stop_publisher(key)
        for key in set(self.memory) - set(self.hosts):
            del self.memory[key]
        finished = set(self.observed.finished)
        self.owners = assign_owners(hosts, threads, self.owners, self.settings.scope)
        for host in hosts:
            rows = host_rows(host, threads, self.owners, finished)
            self.publish(host.pane.key, rows)

    def activate(self, key: PaneKey, row_id: str) -> None:
        host = self.hosts.get(key)
        # Pane ids repeat across sessions, and switching types into the pane the id names now.
        if host is None or not host.pane.on_screen:
            return
        if not self.settings.switching:
            self.cli.notify("Switch to this thread in Codex with /resume.")
            return
        memory = self.memory.setdefault(key, HostMemory())
        target = self.known.get(row_id)
        if target is None:
            self.cli.notify("That Codex thread is no longer listed. Open it with /resume.", error=True)
            return
        try:
            switch(self.cli, host, target, memory)
        except SessionsError as error:
            self.cli.notify(f"Could not switch Codex's thread: {error}", error=True)
        # Show the result at once rather than at the next poll.
        memory.published = None
        self.messages.put(("poll",))

    def report(self, error: str | None) -> None:
        # One line per distinct failure: a missing `codex` would otherwise log every poll.
        if error is not None and error != self.reported_error:
            print(error, file=sys.stderr, flush=True)
        self.reported_error = error

    def handle(self, message: tuple[object, ...]) -> bool:
        """Act on one queued message; False once the service should stop."""
        kind = message[0]
        if kind == "stop":
            return False
        if kind == "closed":
            key, token = message[1], message[2]
            current = self.publishers.get(key)  # type: ignore[arg-type]
            if current is not None and current.token == token:
                self.publishers.pop(key)  # type: ignore[arg-type]
        elif kind == "activate":
            key, token, row_id = message[1], message[2], message[3]
            current = self.publishers.get(key)  # type: ignore[arg-type]
            if current is not None and current.token == token:
                self.activate(key, str(row_id))  # type: ignore[arg-type]
        elif kind == "poll":
            self.poll_once()
        return True

    def poll_once(self) -> None:
        try:
            self.poll()
            self.report(None)
        except SessionsError as error:
            self.report(str(error))

    def title_changed(self) -> bool:
        """Whether a Codex pane's title now names another thread than at the last look.

        Codex retitles the pane as soon as it switches threads, long before the next poll. The
        spinner in front of the name is not part of the comparison."""
        try:
            panes = self.cli.panes()
        except SessionsError:
            return False
        titles = {
            pane.key: frozenset(title_names(pane.title)) for pane in panes if pane.runs_codex()
        }
        changed = self.titles is not None and titles != self.titles
        self.titles = titles
        return changed

    def run(self) -> int:
        while True:
            self.poll_once()
            deadline = time.monotonic() + self.settings.poll_seconds
            while (remaining := deadline - time.monotonic()) > 0:
                try:
                    message = self.messages.get(timeout=min(remaining, TITLE_INTERVAL))
                except queue.Empty:
                    if self.title_changed():
                        break
                    continue
                if not self.handle(message):
                    return 0

    def close(self) -> None:
        for key in list(self.publishers):
            self.stop_publisher(key)
        self.cli.close()


def activation(line: str) -> str | None:
    """The row id in one line of a publish stream's output, if it is an activation."""
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict):
        return None
    return text(value.get("activate"))


def client_changed(line: str) -> bool:
    """Whether one rozi event is a Codex client starting or exiting in a pane.

    A pane is a client by its program or by rozi detecting Codex in it (see `Pane.runs_codex`), so
    both count: the program starting or exiting, and a Codex agent appearing or going. A Codex
    agent changing state does not change which panes are clients.
    """
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return False
    if not isinstance(event, dict) or not isinstance(data := event.get("data"), dict):
        return False
    if event.get("event") == "agent-state-changed":
        return text(data.get("agent")) == "codex" and not (
            text(data.get("state")) and text(data.get("previous_state"))
        )
    return any(
        (text(data.get(field)) or "").casefold() in CLIENT_PROGRAMS
        for field in ("program", "previous_program")
    )


def watch_clients(messages: "queue.Queue[tuple[object, ...]]") -> threading.Thread:
    """Queue a poll as soon as a Codex client starts or exits in a pane, so its rows come and go
    at once instead of at the next poll.

    A rozi without these events refuses the stream, and the poll alone remains. Codex's own thread
    list changes without any rozi event, so the poll stays either way.
    """

    def read() -> None:
        try:
            with subprocess.Popen(
                [ROZI, "subscribe", "foreground-changed", "agent-state-changed"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            ) as stream:
                assert stream.stdout is not None
                for line in stream.stdout:
                    if client_changed(line):
                        messages.put(("poll",))
        except OSError:
            pass

    thread = threading.Thread(target=read, name="clients", daemon=True)
    thread.start()
    return thread


def watch_host(messages: "queue.Queue[tuple[object, ...]]", fd: int = 0) -> bool:
    """Queue a stop once rozi is gone; False when this rozi gives no way to tell.

    rozi holds a service's stdin pipe open for as long as it runs and never writes to it, so end of
    file means rozi exited, even if it was killed before it could stop this service. Older rozi
    releases hand services `/dev/null`, which is at end of file from the start, so only a pipe is
    watched.
    """
    try:
        if not stat.S_ISFIFO(os.fstat(fd).st_mode):
            return False
    except OSError:
        return False

    def wait() -> None:
        try:
            while os.read(fd, 4096):
                pass
        except OSError:
            pass
        messages.put(("stop",))

    threading.Thread(target=wait, name="host", daemon=True).start()
    return True


def main() -> int:
    if os.environ.get("ROZI_EXTENSION") != EXTENSION_ID:
        print(f"{EXTENSION_ID} must be launched by Rozi", file=sys.stderr)
        return 2
    service = SessionsService(Settings.from_environment(dict(os.environ)))

    def stop(_signum: int, _frame: object) -> None:
        service.messages.put(("stop",))

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    watch_host(service.messages)
    watch_clients(service.messages)
    try:
        return service.run()
    finally:
        service.close()


if __name__ == "__main__":
    raise SystemExit(main())
