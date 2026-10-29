from __future__ import annotations

import importlib.util
import json
import os
import queue
import socket
import stat
import struct
import sys
import tempfile
import threading
import unittest
from dataclasses import replace
from unittest import mock
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


def pane(pane_id: int = 3, title: str | None = "⠋ work | rozi", program: str = "codex",
         cwd: str = "/home/x/rozi"):
    return cs.Pane(pane_id=pane_id, cwd=cwd, program=program, agent="codex", title=title)


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
            (thread("a", status="systemError"), True, ("error", "Thread failed")),
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

    def test_failed_threads_publish_error_even_after_a_previous_finished_turn(self):
        for finished in (False, True):
            with self.subTest(finished=finished):
                row = thread("failed", status="systemError").row(False, finished)
                self.assertEqual(row["status"], "error")
                self.assertEqual(row["reason"], "Thread failed")

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

    def test_other_panes_are_not_codex_clients(self):
        threads = [thread("a")]
        panes = [pane(1), cs.Pane(3, None, "bash", None, None), cs.Pane(4, None, "node", "codex", None)]
        self.assertEqual([host.pane.pane_id for host in cs.classify(panes, threads, {})], [1, 4])

    def test_last_switch_stands_in_for_an_unreadable_title(self):
        threads = [thread("a", name="twin"), thread("b", name="twin")]
        memory = {3: cs.HostMemory(last_target="b")}
        self.assertEqual(cs.classify([pane(3, title="twin | rozi")], threads, memory)[0].active, "b")
        memory[3].last_target = "gone"
        self.assertIsNone(cs.classify([pane(3, title="rozi")], threads, memory)[0].active)

    def test_rows_are_oldest_first_whichever_thread_is_shown(self):
        threads = [thread("new", name="new", created=30), thread("old", name="old", created=10),
                   thread("work", name="work", created=20), thread("child", parent="work")]
        for shown, expected in [("work", [("old", False, "done"), ("work", True, "idle"), ("new", False, "idle")]),
                                ("new", [("old", False, "done"), ("work", False, "idle"), ("new", True, "idle")])]:
            host = cs.Host(pane(), shown)
            rows = rows_by_pane([host], threads, finished={"old"})[3]
            self.assertEqual([(row["id"], row["active"], row["status"]) for row in rows], expected)

    def test_a_thread_shown_in_another_pane_is_listed_only_there(self):
        threads = [thread("a", name="a"), thread("b", name="b"), thread("c", name="c"),
                   thread("d", name="d")]
        first, second = cs.Host(pane(1), "a"), cs.Host(pane(2), "b")
        owners = {"d": 2}
        self.assertEqual(published_ids(rows_by_pane([first, second], threads, owners=owners)),
                         {1: ["a", "c"], 2: ["b", "d"]})

    def test_every_thread_is_published_by_one_pane_only(self):
        """Three clients, each on its own thread, and a finished one none of them shows: rozi
        numbers a thread that every pane publishes as three runs of one task."""
        threads = [thread("done", name="Update README", cwd="/home/x/other", created=1),
                   thread("a", name="a", created=2), thread("b", name="b", created=3),
                   thread("c", name="c", created=4)]
        hosts = [cs.Host(pane(2), "a"), cs.Host(pane(3), "b"), cs.Host(pane(4), "c")]
        published = published_ids(rows_by_pane(hosts, threads, finished={"done"}))
        self.assertEqual(published, {2: ["done", "a"], 3: ["b"], 4: ["c"]})

    def test_an_unshown_thread_goes_to_the_pane_nearest_above_it(self):
        threads = [thread("a", name="a"), thread("b", name="b"),
                   thread("deep", name="deep", cwd="/home/x/rozi/tree")]
        hosts = [cs.Host(pane(1, cwd="/home/x"), "a"), cs.Host(pane(2, cwd="/home/x/rozi"), "b")]
        self.assertEqual(cs.assign_owners(hosts, threads, {}, "all")["deep"], 2)
        far = thread("far", name="far", cwd="/srv")
        self.assertEqual(cs.assign_owners(hosts, threads + [far], {}, "all")["far"], 1)
        self.assertNotIn("far", cs.assign_owners(hosts, threads + [far], {}, "cwd"))

    def test_a_pane_keeps_its_threads_while_its_title_cannot_be_read(self):
        threads = [thread("a", name="a", created=1), thread("a2", name="a2", created=2),
                   thread("b", name="b", created=3), thread("b2", name="b2", created=4)]
        owners = cs.assign_owners([cs.Host(pane(1), "a"), cs.Host(pane(2), "b")], threads,
                                  {"a2": 1, "b2": 2}, "all")
        # A prompt was just sent: pane 1's title names no thread for a poll.
        blind = [cs.Host(pane(1), None), cs.Host(pane(2), "b")]
        owners = cs.assign_owners(blind, threads, owners, "all")
        self.assertEqual(owners, {"a": 1, "a2": 1, "b": 2, "b2": 2})

    def test_a_closed_pane_hands_its_threads_to_one_other(self):
        threads = [thread("a", name="a"), thread("a2", name="a2"), thread("b", name="b")]
        owners = {"a": 1, "a2": 1, "b": 2}
        hosts = [cs.Host(pane(2), "b"), cs.Host(pane(3), None)]
        self.assertEqual(cs.assign_owners(hosts, threads, owners, "all"),
                         {"a": 2, "a2": 2, "b": 2})

    def test_hooks_name_a_thread_codex_has_not_named_yet(self):
        threads = [thread("a", name="a"), thread("fresh", name=None, preview="fix the bug")]
        hooked = replace(pane(4, title="rozi"), session="fresh")
        self.assertEqual(cs.classify([hooked], threads, {})[0].active, "fresh")
        # The title, which follows a `/resume` at once, outranks a report from before it.
        titled = replace(hooked, title="a | rozi")
        self.assertEqual(cs.classify([titled], threads, {})[0].active, "a")

    def test_hooks_tell_threads_sharing_the_titled_name_apart(self):
        threads = [thread("a", name="twin"), thread("b", name="twin"), thread("c", name="other")]
        hooked = replace(pane(4, title="twin | rozi"), session="b")
        self.assertEqual(cs.classify([hooked], threads, {})[0].active, "b")
        # A report or switch naming a thread the title rules out is from before a `/resume`.
        stale = replace(hooked, session="c")
        memory = {4: cs.HostMemory(last_target="c")}
        self.assertIsNone(cs.classify([stale], threads, memory)[0].active)
        memory[4].last_target = "a"
        self.assertEqual(cs.classify([stale], threads, memory)[0].active, "a")

    def test_the_spinner_tells_threads_sharing_the_titled_name_apart(self):
        old, new = thread("old", name="twin"), thread("new", name="twin", status="active")
        memory: dict[int, object] = {}
        spinning = pane(4, title="⠏ twin | rozi")
        self.assertEqual(cs.classify([spinning], [old, new], memory)[0].active, "new")
        # The turn failed and the spinner stopped: neither twin runs, so the title cannot tell them
        # apart, and the thread it last told apart stays on screen.
        failed = replace(new, status="systemError")
        stopped = replace(spinning, title="twin | rozi")
        self.assertEqual(cs.classify([stopped], [old, failed], memory)[0].active, "new")
        # A twin running in the background while the one on screen sits idle.
        busy = replace(old, status="active")
        self.assertEqual(cs.classify([stopped], [busy, failed], {})[0].active, "new")
        # Both running, or neither, with nothing remembered, says nothing.
        self.assertIsNone(cs.classify([spinning], [busy, new], {})[0].active)
        self.assertIsNone(cs.classify([stopped], [old, failed], {})[0].active)
        # A hook report outranks the spinner.
        hooked = replace(spinning, session="old")
        self.assertEqual(cs.classify([hooked], [old, new], {})[0].active, "old")
        self.assertFalse(cs.title_spins("⠀twin | rozi"))
        self.assertFalse(cs.title_spins(None))

    def test_a_title_carrying_the_thread_id_says_exactly_which_thread_is_shown(self):
        a = "01a16fd1-20cc-7dc1-90ce-8885b809b462"
        b = "01a16fd3-38f3-7e21-9acd-8f6e77b0ba46"
        threads = [thread(a, name="twin"), thread(b, name="twin"),
                   thread("01a16fd4-0000-7000-8000-000000000000", name=None, preview="")]
        titled = pane(4, title="⠋ twin | rozi | 01a16fd3-38f3-7e21-9acd-8f6e7...")
        # Outranks a stale hook report and the last switch.
        stale = replace(titled, session=a)
        self.assertEqual(cs.classify([stale], threads, {4: cs.HostMemory(last_target=a)})[0].active, b)
        self.assertTrue(cs.switch_confirmed(threads[1], titled, threads))
        self.assertFalse(cs.switch_confirmed(threads[0], titled, threads))
        # A blank thread too new to list is still the one on screen, so no row is.
        blank = replace(titled, title="rozi | 01a16fd4-0000-7000-8000-000…", session=a)
        self.assertEqual(cs.classify([blank], threads, {})[0].active, threads[2].thread_id)
        self.assertEqual(cs.title_thread_ids("work | rozi"), [])
        # Hex names are not IDs.
        self.assertEqual(cs.title_thread_ids("cafebabe | deadbeef-1"), [])
        # An ID matching nothing loaded leaves no thread on screen, a short prefix still names its
        # one thread, and a fragment that is no ID falls back to the name and the hooks.
        unknown = replace(titled, title="twin | 01a16fd9-0000", session=a)
        self.assertIsNone(cs.classify([unknown], threads, {})[0].active)
        short = replace(titled, title="twin | 01a16fd1-20cc", session=a)
        self.assertEqual(cs.classify([short], threads, {})[0].active, a)
        shared = replace(titled, title="twin | 01a16fd", session=a)
        self.assertEqual(cs.classify([shared], threads, {})[0].active, a)

    def test_list_panes_carries_the_hooks_thread(self):
        value = {"id": 4, "foreground_pid": 9, "agent": "codex", "agent_session": "t-1"}
        self.assertEqual(cs.Pane.from_wire(value).session, "t-1")
        self.assertIsNone(cs.Pane.from_wire({"id": 4}).session)

    def test_a_client_on_its_start_screen_shows_it_as_the_active_row(self):
        threads = [thread("a", name="a", created=1), thread("b", name="b", created=2),
                   thread("blank", name=None, preview="", created=3)]
        host = cs.Host(pane(3), "blank")
        rows = rows_by_pane([host], threads)[3]
        self.assertEqual([(row["id"], row["title"], row["active"]) for row in rows],
                         [("a", "a", False), ("b", "b", False), ("blank", "New thread", True)])
        self.assertEqual(rows[-1]["native_session"], "blank")
        # Alone, it is left to rozi's own detection of the client.
        self.assertEqual(rows_by_pane([host], threads[2:]), {3: []})
        # Only the one on screen: another client's start screen is not listed here.
        self.assertEqual(published_ids(rows_by_pane([cs.Host(pane(3), "a")], threads)),
                         {3: ["a", "b"]})

    def test_a_hook_report_names_the_start_screen(self):
        threads = [thread("a", name="a"), thread("blank", name=None, preview="")]
        hooked = replace(pane(4, title="rozi"), session="blank")
        self.assertEqual(cs.classify([hooked], threads, {})[0].active, "blank")

    def test_a_lone_thread_is_published_only_while_it_is_on_screen(self):
        threads = [thread("a"), thread("blank", name=None, preview="")]
        rows = rows_by_pane([cs.Host(pane(), "a")], threads)[3]
        self.assertEqual([(row["id"], row["active"], row["native_session"]) for row in rows],
                         [("a", True, "a")])
        # A blank new thread on screen leaves the agent rozi detects there in charge.
        self.assertEqual(rows_by_pane([cs.Host(pane(), "blank")], threads), {3: []})
        self.assertEqual(rows_by_pane([cs.Host(pane(), None)], threads), {3: []})

    def test_cwd_scope_keeps_the_shown_thread_and_threads_below_the_pane(self):
        threads = [thread("a", cwd="/elsewhere"), thread("b", cwd="/home/x/rozi/sub"),
                   thread("c", cwd="/home/x/rozi-other")]
        host = cs.Host(pane(cwd="/home/x/rozi"), "a")
        self.assertEqual(published_ids(rows_by_pane([host], threads, scope="cwd")), {3: ["a", "b"]})
        self.assertEqual(published_ids(rows_by_pane([host], threads, scope="all")),
                         {3: ["a", "b", "c"]})


def rows_by_pane(hosts, threads, *, scope="all", finished=frozenset(), owners=None):
    owners = cs.assign_owners(hosts, threads, owners or {}, scope)
    return {host.pane.pane_id: cs.host_rows(host, threads, owners, set(finished)) for host in hosts}


def published_ids(rows):
    return {pane_id: [row["id"] for row in value] for pane_id, value in rows.items()}


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

    def test_a_running_turn_is_explained_not_typed_into(self):
        """Codex refuses `/resume` until the turn ends, and nothing else switches its thread."""
        for working_footer, daemon_active in [(True, False), (False, True), (True, True)]:
            with self.subTest(working_footer=working_footer, daemon_active=daemon_active):
                threads = [thread("a", name="work", status="active" if daemon_active else "idle"),
                           THREADS[1]]
                cli = FakeCli(threads, [composer_frame(working=working_footer)], ["other | rozi"])
                switched, memory = self.switch(cli)
                self.assertFalse(switched)
                self.assertEqual((cli.typed, cli.pressed), ([], []))
                self.assertIsNone(memory.last_target)
                self.assertEqual(cli.notes, [(cs.BUSY, True)])

    def test_selecting_the_shown_thread_does_nothing(self):
        cli = FakeCli(THREADS, [composer_frame()])
        self.assertTrue(self.switch(cli, target="a")[0])
        self.assertEqual(cli.typed, [])

    def test_refusals_never_type(self):
        cases = [
            (THREADS, composer_frame("draft"), "unsent text"),
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

    def test_a_turn_starting_before_enter_leaves_the_command_unsent(self):
        command = cs.resume_command("b")
        cli = FakeCli(THREADS, [composer_frame(), composer_frame(command, working=True)],
                      ["other | rozi"])
        self.assertFalse(self.switch(cli)[0])
        self.assertEqual(cli.pressed, [])
        self.assertIn("left unsent", cli.notes[0][0])

    def test_codexs_own_refusal_mid_turn_is_quoted(self):
        command = cs.resume_command("b")
        cli = FakeCli(THREADS, [composer_frame(), composer_frame(command)], ["work | rozi"])
        cli.texts = [f"› {command}\n■ '/resume' is disabled while a task is in progress."]
        self.assertFalse(self.switch(cli)[0])
        self.assertIn("disabled while a task is in progress", cli.notes[0][0])

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

    def test_the_stream_declares_its_rows_agents(self):
        service = cs.SessionsService(cs.Settings(), FakeCli(THREADS, []))
        with mock.patch.object(cs.subprocess, "Popen") as popen:
            popen.return_value.stdout = iter(())
            service.start_publisher(7)
        self.assertEqual(popen.call_args.args[0][1:], ["publish", "--kind", "agent"])

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

    def test_a_retitled_pane_is_noticed_but_a_spinner_is_not(self):
        class TitleCli:
            title = "⠋ work | rozi"

            def panes(self):
                return [pane(3, title=self.title), cs.Pane(4, None, "bash", None, 9, None)]

        cli = TitleCli()
        service = cs.SessionsService(cs.Settings(), cli)
        self.assertFalse(service.title_changed())
        cli.title = "⠙ work | rozi"
        self.assertFalse(service.title_changed())
        cli.title = "other | rozi"
        self.assertTrue(service.title_changed())
        self.assertFalse(service.title_changed())

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
                         [("a", False), ("b", True)])
        cli.title = "work | rozi"
        service.poll()
        self.assertEqual([(row["id"], row["active"]) for row in published[-1]], [("a", True)])

    def test_three_clients_publish_a_finished_background_thread_once(self):
        class PollCli:
            def __init__(self):
                self.thread_list = [thread("a", name="a", created=2), thread("b", name="b", created=3),
                                    thread("c", name="c", created=4),
                                    thread("bg", name="Update README", status="active", created=1)]
                self.titles = {2: "a | rozi", 3: "b | rozi", 4: "c | rozi"}

            def threads(self):
                return self.thread_list

            def panes(self):
                return [pane(pane_id, title=title) for pane_id, title in self.titles.items()]

        cli = PollCli()
        service = cs.SessionsService(cs.Settings(), cli)
        published = {}
        service.publish = lambda pane_id, rows: published.__setitem__(pane_id, rows)

        def listings(thread_id):
            return [pane_id for pane_id, rows in published.items()
                    if any(row["id"] == thread_id for row in rows)]

        service.poll()
        # The background turn ends and Codex unloads it; a prompt in pane 4 blanks its title.
        cli.thread_list = cli.thread_list[:3]
        cli.titles[4] = "rozi"
        service.poll()
        self.assertEqual(listings("bg"), [2])
        self.assertEqual(listings("c"), [])
        cli.titles[4] = "c | rozi"
        service.poll()
        self.assertEqual(listings("bg"), [2])

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


class ClientWatchTests(unittest.TestCase):
    """Polling again as soon as a Codex client starts or exits, from rozi's events."""

    @staticmethod
    def program(program: str, previous: str = "") -> str:
        data = {"pane": "3", "program": program, "previous_program": previous, "pid": ""}
        return json.dumps({"event": "foreground-changed", "data": data})

    @staticmethod
    def agent(agent: str, state: str, previous: str) -> str:
        data = {"pane": "3", "agent": agent, "state": state, "previous_state": previous}
        return json.dumps({"event": "agent-state-changed", "data": data})

    def test_only_a_codex_client_starting_or_exiting_counts(self) -> None:
        self.assertTrue(cs.client_changed(self.program("codex")))
        self.assertTrue(cs.client_changed(self.program("", previous="Codex-CLI")))
        self.assertFalse(cs.client_changed(self.program("cargo", previous="ls")))
        self.assertTrue(cs.client_changed(self.agent("codex", "idle", "")))
        self.assertTrue(cs.client_changed(self.agent("codex", "", "working")))
        self.assertFalse(cs.client_changed(self.agent("codex", "idle", "working")))
        self.assertFalse(cs.client_changed(self.agent("claude", "idle", "")))
        self.assertFalse(cs.client_changed("not json"))

    @unittest.skipIf(os.name == "nt", "the stand-in rozi is a POSIX shell script")
    def test_the_stream_queues_a_poll_and_a_refusal_leaves_the_poll_alone(self) -> None:
        def watch(script: str) -> tuple[list[tuple[object, ...]], str]:
            with tempfile.TemporaryDirectory() as directory:
                args = Path(directory) / "args"
                rozi = Path(directory) / "rozi"
                rozi.write_text(f'#!/bin/sh\necho "$@" > "{args}"\n{script}\n', encoding="utf-8")
                rozi.chmod(rozi.stat().st_mode | stat.S_IXUSR)
                messages: queue.Queue[tuple[object, ...]] = queue.Queue()
                with mock.patch.object(cs, "ROZI", str(rozi)):
                    cs.watch_clients(messages).join(timeout=5)
                return list(messages.queue), args.read_text().strip()

        streamed = "\n".join(
            f"echo '{line}'" for line in (self.program("cargo"), self.agent("codex", "idle", ""))
        )
        queued, args = watch(streamed)
        self.assertEqual(args, "subscribe foreground-changed agent-state-changed")
        self.assertEqual(queued, [("poll",)])
        refused, _ = watch("echo 'unknown event' >&2; exit 1")
        self.assertEqual(refused, [])


class WatchHostTest(unittest.TestCase):
    def test_end_of_file_on_a_stdin_pipe_stops_the_service(self) -> None:
        read_end, write_end = os.pipe()
        messages: queue.Queue[tuple[object, ...]] = queue.Queue()
        try:
            self.assertTrue(cs.watch_host(messages, read_end))
            self.assertTrue(messages.empty())
            os.close(write_end)
            self.assertEqual(messages.get(timeout=5), ("stop",))
        finally:
            os.close(read_end)

    def test_a_stdin_that_is_not_a_pipe_is_not_watched(self) -> None:
        # Older rozi releases hand services `/dev/null`, which is at end of file from the start.
        messages: queue.Queue[tuple[object, ...]] = queue.Queue()
        with open(os.devnull, "rb") as devnull:
            self.assertFalse(cs.watch_host(messages, devnull.fileno()))
        self.assertTrue(messages.empty())


if __name__ == "__main__":
    unittest.main()
