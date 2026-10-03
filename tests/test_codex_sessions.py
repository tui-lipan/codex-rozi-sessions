from __future__ import annotations

import importlib.util
import json
import os
import socket
import struct
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPT = Path(__file__).parents[1] / "bin" / "codex_sessions.py"


def load_script():
    spec = importlib.util.spec_from_file_location("codex_sessions_example", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"could not load {SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cs = load_script()

SHADE = "#2e2e2e"


def thread(
    tid: str,
    *,
    name: str | None = "work",
    status: str = "idle",
    flags: list[str] | None = None,
    cwd: str = "/home/x/rozi",
    created: int = 0,
    parent: str | None = None,
    preview: str = "do the work",
    ephemeral: bool = False,
) -> object:
    """One thread as the daemon's `thread/read` returns it."""
    status_value: dict[str, object] = {"type": status}
    if status == "active":
        status_value["activeFlags"] = flags or []
    return cs.Thread.from_wire(
        {
            "id": tid,
            "name": name,
            "preview": preview,
            "cwd": cwd,
            "status": status_value,
            "parentThreadId": parent,
            "ephemeral": ephemeral,
            "createdAt": created,
            "source": "vscode",
        }
    )


def pane(pane_id: int = 3, title: str | None = "⠋ work | rozi", pid: int | None = 500,
         program: str = "codex", cwd: str = "/home/x/rozi"):
    return cs.Pane(pane_id=pane_id, cwd=cwd, program=program, agent="codex",
                   foreground_pid=pid, title=title)


def composer_frame(typed: str = "", *, placeholder: bool = True, working: bool = False,
                   shade: str | None = SHADE) -> list[list[dict[str, object]]]:
    """The bottom of a Codex screen as `capture-pane --render spans` returns it."""
    rows: list[list[dict[str, object]]] = [[{"text": "• Ran cargo test"}], []]
    if working:
        rows.append([{"text": "• Working (12s • esc to interrupt)"}])
    background = {"bg": shade} if shade else {}
    prompt: list[dict[str, object]] = [{"text": "›", "bold": True, **background},
                                       {"text": " ", **background}]
    if typed:
        prompt.append({"text": typed, **background})
    elif placeholder:
        prompt.append({"text": "Ask Codex to do anything", "dim": True, **background})
    rows += [[{"text": " " * 40, **background}], prompt, [{"text": " " * 40, **background}]]
    rows += [[{"text": "  GPT-6 medium · ~/x/rozi · work"}], [{"text": "  ? for shortcuts"}]]
    return rows


def approval_frame() -> list[list[dict[str, object]]]:
    return [
        [{"text": "Would you like to run the following command?"}],
        [{"text": "› 1. Yes, proceed (y)"}],
        [{"text": "  2. No, and tell Codex what to do differently (esc)"}],
    ]


class ThreadTests(unittest.TestCase):
    def test_row_states_follow_the_daemon_status(self):
        cases = [
            (thread("a", status="active"), False, ("working", None)),
            (thread("a", status="active", flags=["waitingOnApproval"]), False,
             ("blocked", "Approval required")),
            (thread("a", status="active", flags=["waitingOnUserInput"]), False,
             ("blocked", "Question needs an answer")),
            (thread("a", status="systemError"), True, ("idle", "Thread failed")),
            (thread("a"), False, ("idle", None)),
            (thread("a"), True, ("done", None)),
        ]
        for value, finished, expected in cases:
            self.assertEqual(value.row_state(finished), expected)

    def test_sub_agents_side_conversations_and_blank_threads_are_not_listed(self):
        self.assertTrue(thread("a").is_listable())
        self.assertTrue(thread("a", name=None).is_listable())
        self.assertFalse(thread("a", parent="parent").is_listable())
        self.assertFalse(thread("a", ephemeral=True).is_listable())
        self.assertFalse(thread("a", name=None, preview="").is_listable())

    def test_row_carries_identity_directory_and_a_title(self):
        row = thread("abc", name=None, preview="first line\nsecond", cwd="/w/tree").row(True, False)
        self.assertEqual(row, {"id": "abc", "title": "first line", "status": "idle", "active": True,
                               "native_session": "abc", "cwd": "/w/tree"})

    def test_malformed_threads_are_dropped(self):
        self.assertIsNone(cs.Thread.from_wire({"name": "no id"}))
        self.assertIsNone(cs.Thread.from_wire("text"))
        bare = cs.Thread.from_wire({"id": "x", "status": "weird", "createdAt": True})
        self.assertEqual((bare.status, bare.created_at), ("", 0))


class HostTests(unittest.TestCase):
    def test_title_names_strip_the_spinner_and_project(self):
        self.assertEqual(cs.title_names("⠏ Investigate close | rozi"), ["Investigate close", "rozi"])
        self.assertEqual(cs.title_names("⠇ ⠇ | rozi"), ["rozi"])
        self.assertEqual(cs.title_names(None), [])

    def test_the_title_names_the_thread_on_screen_only_when_unambiguous(self):
        threads = [thread("a", name="work"), thread("b", name="other"), thread("c", name="twin"),
                   thread("d", name="twin")]
        self.assertEqual(cs.shown_thread("⠋ work | rozi", threads), "a")
        self.assertIsNone(cs.shown_thread("twin | rozi", threads))
        self.assertIsNone(cs.shown_thread("rozi", threads))

    def test_remote_and_other_panes_are_not_codex_clients(self):
        threads = [thread("a")]
        panes = [pane(1), pane(2, pid=None), cs.Pane(3, None, "bash", None, 9, None),
                 cs.Pane(4, None, "node", "codex", 9, None)]
        self.assertEqual([host.pane.pane_id for host in cs.classify(panes, threads, {})], [1, 4])

    def test_last_switch_stands_in_for_an_unreadable_title(self):
        threads = [thread("a", name="twin"), thread("b", name="twin")]
        memory = {3: cs.HostMemory(last_target="b")}
        self.assertEqual(cs.classify([pane(3, title="twin | rozi")], threads, memory)[0].active, "b")
        memory[3].last_target = "gone"
        self.assertIsNone(cs.classify([pane(3, title="rozi")], threads, memory)[0].active)

    def test_rows_list_the_shown_thread_first_then_the_others_oldest_first(self):
        threads = [thread("new", name="new", created=30), thread("old", name="old", created=10),
                   thread("work", name="work", created=20), thread("child", parent="work")]
        host = cs.Host(pane(), "work")
        rows = cs.host_rows(host, [host], threads, "all", {"old"})
        self.assertEqual([(row["id"], row["active"], row["status"]) for row in rows],
                         [("work", True, "idle"), ("old", False, "done"), ("new", False, "idle")])

    def test_a_thread_shown_in_another_pane_is_listed_only_there(self):
        threads = [thread("a", name="a"), thread("b", name="b"), thread("c", name="c")]
        first, second = cs.Host(pane(1), "a"), cs.Host(pane(2), "b")
        self.assertEqual([row["id"] for row in cs.host_rows(first, [first, second], threads, "all", set())],
                         ["a", "c"])
        self.assertEqual([row["id"] for row in cs.host_rows(second, [first, second], threads, "all", set())],
                         ["b", "c"])

    def test_a_lone_thread_publishes_no_rows(self):
        host = cs.Host(pane(), "a")
        self.assertEqual(cs.host_rows(host, [host], [thread("a"), thread("blank", name=None, preview="")],
                                      "all", set()), [])

    def test_cwd_scope_keeps_the_shown_thread_and_threads_below_the_pane(self):
        threads = [thread("a", cwd="/elsewhere"), thread("b", cwd="/home/x/rozi/sub"),
                   thread("c", cwd="/home/x/rozi-other")]
        host = cs.Host(pane(cwd="/home/x/rozi"), "a")
        self.assertEqual([row["id"] for row in cs.host_rows(host, [host], threads, "cwd", set())],
                         ["a", "b"])


class ObservedTests(unittest.TestCase):
    def test_a_turn_seen_finishing_reads_done_until_the_next_one_starts(self):
        observed = cs.Observed()
        observed.update([thread("a", status="idle")])
        self.assertEqual(set(observed.finished), set())
        observed.update([thread("a", status="active")])
        observed.update([thread("a", status="idle")])
        self.assertEqual(set(observed.finished), {"a"})
        observed.update([thread("a", status="active")])
        self.assertEqual(set(observed.finished), set())
        observed.update([thread("a", status="systemError")])
        self.assertEqual(set(observed.finished), set())

    def test_a_finished_thread_stays_listed_after_codex_unloads_it(self):
        observed = cs.Observed()
        observed.update([thread("a", status="active"), thread("b")])
        observed.update([thread("a", name="renamed"), thread("b")])
        observed.update([thread("b")])
        listed = observed.listed([thread("b")])
        self.assertEqual([(value.thread_id, value.name, value.status) for value in listed],
                         [("b", "work", "idle"), ("a", "renamed", "idle")])
        self.assertEqual(observed.working, {})

    def test_a_finished_thread_is_forgotten_once_shown_and_left(self):
        observed = cs.Observed()
        observed.update([thread("a", status="active")])
        observed.update([])
        observed.mark_viewed(set())
        self.assertIn("a", observed.finished)
        observed.mark_viewed({"a"})
        self.assertIn("a", observed.finished)
        observed.mark_viewed({"b"})
        self.assertEqual(observed.listed([]), [])
        self.assertEqual(observed.viewed, set())

    def test_an_idle_thread_that_never_ran_while_watched_is_not_kept(self):
        observed = cs.Observed()
        observed.update([thread("a")])
        observed.update([])
        self.assertEqual(observed.listed([]), [])


class ScreenTests(unittest.TestCase):
    def test_empty_prompt_ignores_the_placeholder(self):
        screen = cs.read_screen(composer_frame())
        self.assertEqual(screen.composer.text, "")
        self.assertFalse(screen.draft)
        self.assertFalse(screen.working)

    def test_typed_text_is_a_draft(self):
        screen = cs.read_screen(composer_frame("fix the build"))
        self.assertEqual(screen.composer.text, "fix the build")
        self.assertTrue(screen.draft)

    def test_running_turn_is_working(self):
        self.assertTrue(cs.read_screen(composer_frame(working=True)).working)

    def test_dialog_choices_are_not_the_prompt(self):
        self.assertIsNone(cs.read_screen(approval_frame()).composer)

    def test_an_unshaded_cursor_is_not_the_prompt(self):
        self.assertIsNone(cs.read_screen(composer_frame(shade=None)).composer)

    def test_multi_line_draft_reads_every_shaded_row(self):
        rows = composer_frame("first")
        rows.insert(-3, [{"text": "  second", "bg": SHADE}])
        self.assertEqual(cs.read_screen(rows).composer.text, "first\n  second")

    def test_switch_failure_reads_below_the_echoed_command(self):
        command = cs.resume_command("abc")
        old = "■ No saved chat found matching 'old'.\n› " + command + "\n"
        self.assertIsNone(cs.switch_failure(old, command))
        self.assertEqual(cs.switch_failure(old + "■ No saved chat found matching 'abc'.", command),
                         "No saved chat found matching 'abc'.")


class FakeCli:
    def __init__(self, threads, screens, panes=None):
        self.thread_list = threads
        self.screens = list(screens)
        self.pane_titles = list(panes or [])
        self.texts: list[str] = []
        self.typed: list[tuple[int, str]] = []
        self.pressed: list[tuple[int, str]] = []
        self.notes: list[tuple[str, bool]] = []

    def threads(self):
        return self.thread_list

    def screen(self, pane_id):
        return cs.read_screen(self.screens.pop(0) if len(self.screens) > 1 else self.screens[0])

    def screen_text(self, pane_id):
        return self.texts.pop(0) if self.texts else ""

    def pane(self, pane_id):
        title = self.pane_titles.pop(0) if len(self.pane_titles) > 1 else self.pane_titles[0]
        return pane(pane_id, title=title)

    def type_text(self, pane_id, value):
        self.typed.append((pane_id, value))

    def press(self, pane_id, key):
        self.pressed.append((pane_id, key))

    def notify(self, message, *, error=False):
        self.notes.append((message, error))


THREADS = [thread("a", name="work"), thread("b", name="other")]


def no_sleep(_seconds):
    pass


class SwitchTests(unittest.TestCase):
    def switch(self, cli, active="a", target="b", memory=None):
        memory = memory or cs.HostMemory()
        known = {value.thread_id: value for value in THREADS}
        target = known.get(target) or thread(target, name="unloaded")
        return cs.switch(cli, cs.Host(pane(), active), target, memory, sleep=no_sleep), memory

    def test_switch_types_resume_reads_it_back_then_confirms_by_title(self):
        command = cs.resume_command("b")
        cli = FakeCli(THREADS, [composer_frame(), composer_frame(command)],
                      ["work | rozi", "other | rozi"])
        switched, memory = self.switch(cli)
        self.assertTrue(switched)
        self.assertEqual(cli.typed, [(3, command)])
        self.assertEqual(cli.pressed, [(3, "Enter")])
        self.assertEqual(memory.last_target, "b")
        self.assertEqual(cli.notes, [])

    def test_selecting_the_shown_thread_does_nothing(self):
        cli = FakeCli(THREADS, [composer_frame()])
        self.assertTrue(self.switch(cli, target="a")[0])
        self.assertEqual(cli.typed, [])

    def test_refusals_never_type(self):
        cases = [
            (THREADS, composer_frame("draft"), "unsent text"),
            (THREADS, composer_frame(working=True), "still working"),
            ([thread("a", name="work", status="active"), THREADS[1]], composer_frame(), "still working"),
            (THREADS, approval_frame(), "approval"),
        ]
        for threads, frame, message in cases:
            cli = FakeCli(threads, [frame])
            self.assertFalse(self.switch(cli)[0])
            self.assertEqual(cli.typed, [])
            self.assertIn(message, cli.notes[0][0])
            self.assertTrue(cli.notes[0][1])

    def test_a_finished_thread_codex_unloaded_is_resumed(self):
        command = cs.resume_command("gone")
        cli = FakeCli(THREADS, [composer_frame(), composer_frame(command)], ["unloaded | rozi"])
        switched, memory = self.switch(cli, target="gone")
        self.assertTrue(switched)
        self.assertEqual(cli.typed, [(3, command)])
        self.assertEqual(memory.last_target, "gone")

    def test_command_that_does_not_read_back_is_not_sent(self):
        cli = FakeCli(THREADS, [composer_frame(), composer_frame("/resume bX")])
        self.assertFalse(self.switch(cli)[0])
        self.assertEqual(cli.pressed, [])
        self.assertIn("not sent", cli.notes[0][0])

    def test_a_turn_starting_before_enter_stops_the_switch(self):
        command = cs.resume_command("b")
        cli = FakeCli(THREADS, [composer_frame(), composer_frame(command, working=True)])
        self.assertFalse(self.switch(cli)[0])
        self.assertEqual(cli.pressed, [])

    def test_codex_refusal_is_quoted(self):
        command = cs.resume_command("b")
        cli = FakeCli(THREADS, [composer_frame(), composer_frame(command)], ["work | rozi"])
        cli.texts = [f"› {command}\n■ Failed to resume session from b: boom"]
        self.assertFalse(self.switch(cli)[0])
        self.assertIn("Failed to resume session from b: boom", cli.notes[0][0])

    def test_no_confirmation_leaves_the_active_row_to_codex(self):
        command = cs.resume_command("b")
        cli = FakeCli(THREADS, [composer_frame(), composer_frame(command)], ["work | rozi"])
        switched, memory = self.switch(cli)
        self.assertFalse(switched)
        self.assertIsNone(memory.last_target)
        self.assertIn("Could not confirm", cli.notes[0][0])

    def test_a_shared_or_missing_name_cannot_confirm_a_switch(self):
        twins = [thread("a", name="twin"), thread("b", name="twin")]
        self.assertFalse(cs.switch_confirmed(twins[1], pane(title="twin | rozi"), twins))
        unnamed = thread("b", name=None)
        self.assertFalse(cs.switch_confirmed(unnamed, pane(title="rozi"), [unnamed]))
        self.assertFalse(cs.switch_confirmed(THREADS[1], None, THREADS))


class ServiceTests(unittest.TestCase):
    def test_settings_are_clamped_and_validated(self):
        settings = cs.Settings.from_environment({"ROZI_EXTENSION_CONFIG": json.dumps(
            {"codex": " ", "poll_seconds": 0.01, "scope": "everywhere", "switching": "yes"})})
        self.assertEqual(settings, cs.Settings(poll_seconds=0.5))
        settings = cs.Settings.from_environment({"ROZI_EXTENSION_CONFIG": json.dumps(
            {"codex": "/opt/codex", "poll_seconds": 90, "scope": "cwd", "switching": False})})
        self.assertEqual(settings, cs.Settings("/opt/codex", 60.0, "cwd", False))
        self.assertEqual(cs.Settings.from_environment({"ROZI_EXTENSION_CONFIG": "[1"}), cs.Settings())

    def test_activation_lines(self):
        self.assertEqual(cs.activation('{"activate":"b"}\n'), "b")
        self.assertIsNone(cs.activation('{"ok":true}\n'))
        self.assertIsNone(cs.activation("garbage"))

    def test_poll_publishes_each_host_once_until_its_rows_change(self):
        class PollCli:
            def __init__(self):
                self.thread_list = [thread("a", name="work"), thread("b", name="other")]

            def threads(self):
                return self.thread_list

            def panes(self):
                return [pane(3, title="work | rozi")]

        service = cs.SessionsService(cs.Settings(), PollCli())
        published = []
        service.publish = lambda pane_id, rows: published.append((pane_id, rows))
        service.poll()
        self.assertEqual(published[0][0], 3)
        self.assertEqual([(row["id"], row["active"]) for row in published[0][1]],
                         [("a", True), ("b", False)])

    def test_activating_a_row_no_longer_listed_explains(self):
        cli = FakeCli(THREADS, [composer_frame()])
        service = cs.SessionsService(cs.Settings(), cli)
        service.hosts = {3: cs.Host(pane(), "a")}
        service.activate(3, "vanished")
        self.assertEqual(cli.typed, [])
        self.assertIn("no longer listed", cli.notes[0][0])

    def test_poll_keeps_a_finished_background_thread_until_it_is_viewed(self):
        class PollCli:
            def __init__(self):
                self.thread_list = [thread("a", name="work"), thread("b", name="other", status="active")]
                self.title = "work | rozi"

            def threads(self):
                return self.thread_list

            def panes(self):
                return [pane(3, title=self.title)]

        cli = PollCli()
        service = cs.SessionsService(cs.Settings(), cli)
        published = []
        service.publish = lambda pane_id, rows: published.append(rows)
        service.poll()
        cli.thread_list = [thread("a", name="work")]
        service.poll()
        self.assertEqual([(row["id"], row["status"]) for row in published[-1]],
                         [("a", "idle"), ("b", "done")])
        self.assertIn("b", service.known)
        cli.title = "other | rozi"
        service.poll()
        self.assertEqual([(row["id"], row["active"]) for row in published[-1]],
                         [("b", True), ("a", False)])
        cli.title = "work | rozi"
        service.poll()
        self.assertEqual(published[-1], [])

    def test_switching_disabled_only_explains(self):
        cli = FakeCli(THREADS, [composer_frame()])
        service = cs.SessionsService(cs.Settings(switching=False), cli)
        service.hosts = {3: cs.Host(pane(), "a")}
        service.activate(3, "b")
        self.assertEqual(cli.typed, [])
        self.assertIn("/resume", cli.notes[0][0])

    def test_main_refuses_to_run_outside_rozi(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(cs.main(), 2)


class FakeDaemon:
    """A Codex app-server stand-in on a real Unix socket, speaking WebSocket frames."""

    def __init__(self, handler):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "control.sock")
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(self.path)
        self.server.listen(1)
        self.handler = handler
        self.requests: list[dict[str, object]] = []
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def close(self):
        self.server.close()
        self.directory.cleanup()

    def serve(self):
        try:
            connection, _ = self.server.accept()
        except OSError:
            return
        with connection:
            request = b""
            while b"\r\n\r\n" not in request:
                request += connection.recv(4096)
            connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                               b"Connection: Upgrade\r\n\r\n")
            self.buffer = request.partition(b"\r\n\r\n")[2]
            self.connection = connection
            while True:
                try:
                    message = self.receive()
                except OSError:
                    return
                if message is None:
                    return
                self.requests.append(message)
                if "id" in message:
                    self.handler(self, message)

    def read(self, count):
        while len(self.buffer) < count:
            chunk = self.connection.recv(65536)
            if not chunk:
                raise OSError("closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def receive(self):
        first, second = self.read(2)
        length = second & 0x7F
        if length == 126:
            length = struct.unpack(">H", self.read(2))[0]
        elif length == 127:
            length = struct.unpack(">Q", self.read(8))[0]
        mask = self.read(4)
        payload = bytes(byte ^ mask[index % 4] for index, byte in enumerate(self.read(length)))
        if first & 0x0F == 0xA:
            return {"pong": payload.decode()}
        return json.loads(payload)

    def frame(self, opcode, payload, final=True):
        length = len(payload)
        header = bytes([(0x80 if final else 0) | opcode])
        if length < 126:
            header += bytes([length])
        elif length < 1 << 16:
            header += bytes([126]) + struct.pack(">H", length)
        else:
            header += bytes([127]) + struct.pack(">Q", length)
        self.connection.sendall(header + payload)

    def send(self, value):
        self.frame(0x1, json.dumps(value).encode())


def daemon_reply(threads):
    def handle(daemon, message):
        method, request_id = message["method"], message["id"]
        if method == "initialize":
            daemon.send({"method": "remoteControl/status/changed", "params": {}})
            daemon.frame(0x9, b"hi")
            daemon.send({"id": request_id, "result": {"userAgent": "fake"}})
        elif method == "thread/loaded/list":
            # A page boundary and a fragmented reply.
            if message["params"].get("cursor") is None:
                payload = json.dumps({"id": request_id, "result": {"data": ["a"], "nextCursor": "1"}}).encode()
                daemon.frame(0x1, payload[:10], final=False)
                daemon.frame(0x0, payload[10:])
            else:
                daemon.send({"id": request_id, "result": {"data": ["b", "gone"], "nextCursor": None}})
        elif method == "thread/read":
            thread_id = message["params"]["threadId"]
            daemon.send({"method": "thread/status/changed", "params": {"threadId": thread_id}})
            if thread_id == "gone":
                daemon.send({"id": request_id, "error": {"code": -32600, "message": "not loaded"}})
            else:
                daemon.send({"id": request_id, "result": {"thread": threads[thread_id]}})
    return handle


class AppServerTests(unittest.TestCase):
    def test_loaded_threads_over_the_daemon_protocol(self):
        wire = {
            "a": {"id": "a", "name": "work", "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}},
            "b": {"id": "b", "name": "other", "status": {"type": "notLoaded"}},
        }
        daemon = FakeDaemon(daemon_reply(wire))
        self.addCleanup(daemon.close)
        server = cs.AppServer(cs.Settings())
        self.addCleanup(server.close)
        with patch.object(server, "socket_path", return_value=daemon.path):
            threads = server.loaded_threads()
        self.assertEqual([(value.thread_id, value.row_state(False)) for value in threads],
                         [("a", ("blocked", "Approval required"))])
        initialize = daemon.requests[0]
        self.assertEqual(initialize["params"]["clientInfo"]["name"], "codex-rozi-sessions")
        self.assertIn({"method": "initialized"}, daemon.requests)
        self.assertIn({"pong": "hi"}, daemon.requests)
        reads = [request["params"] for request in daemon.requests if request.get("method") == "thread/read"]
        self.assertTrue(all(read["includeTurns"] is False for read in reads))

    def test_no_running_daemon_lists_nothing_and_starts_nothing(self):
        server = cs.AppServer(cs.Settings())
        completed = cs.subprocess.CompletedProcess([], 0, stdout='{"status":"stopped"}', stderr="")
        with patch.object(cs.subprocess, "run", return_value=completed) as run:
            self.assertEqual(server.loaded_threads(), [])
        self.assertEqual(run.call_args[0][0], ["codex", "app-server", "daemon", "version"])

    def test_unreadable_daemon_status_is_an_error(self):
        server = cs.AppServer(cs.Settings())
        completed = cs.subprocess.CompletedProcess([], 1, stdout="", stderr="unknown command")
        with patch.object(cs.subprocess, "run", return_value=completed):
            with self.assertRaises(cs.SessionsError):
                server.loaded_threads()

    def test_a_dropped_connection_reconnects_on_the_next_poll(self):
        daemon = FakeDaemon(lambda daemon, message: daemon.connection.close())
        self.addCleanup(daemon.close)
        server = cs.AppServer(cs.Settings())
        with patch.object(server, "socket_path", return_value=daemon.path):
            with self.assertRaises(cs.SessionsError):
                server.loaded_threads()
        self.assertIsNone(server.sock)


if __name__ == "__main__":
    unittest.main()
