# codex-rozi-sessions

Codex runs its threads in a shared background server, and a Codex client keeps the threads it
switched away from loaded there, often each in its own Git worktree. rozi sees that client as a
single pane. This extension lists every loaded thread as its own row in that pane, so rozi's
Activity sidebar and Agents view show each one's state under the repository and branch it works
in. Selecting a row switches the client to that thread.

```text
Activity
 rozi                                  master
 ⠋  Codex #1   Investigate pane close animation
 rozi                    codex/fix-login
 !  Codex #2   Fix login redirect
 tui-lipan                             main
 ✓  Codex #3   Document focused_node_id
```

## Requirements

- rozi 0.0.28 or newer
- Codex 0.160 or newer, with its background server (`codex app-server daemon`), which Codex starts
  on its own unless you run it with `--no-daemon`
- Python 3 available as `python3`
- Linux or macOS. See [Limits](#limits).

## Install

From rozi's **Extensions…** palette entry, open the **Discover** tab and install
`codex-rozi-sessions`. Or use the CLI:

```bash
rozi extensions install https://github.com/tui-lipan/codex-rozi-sessions.git
rozi run-action reload-extensions
```

Later releases can be applied with `rozi extensions update codex-rozi-sessions`.

Start `codex` in a rozi pane. Rows appear once more than one thread is listed for it.

## What is listed

The supervised `codex-rozi-sessions.watch` service asks Codex's background server which threads
it has loaded, every two seconds, and reads rozi's pane list. A pane is a Codex client when rozi
detects Codex in it and can read its foreground process.

The thread a client shows is the one Codex names in the pane's title (`<thread name> | <project>`
by default), when exactly one listed thread has that name. Failing that, it is the thread this
extension last switched the client to. That thread is the first row and the active one. Every
other loaded thread is listed after it, oldest first, each with the directory it works in, so rozi
groups it by that directory's project and branch. A thread another Codex pane shows is listed in
that pane instead.

Sub-agents are left out; they belong to their parent's turn. So are side conversations and the
blank thread a client opens before your first prompt. A client with only one thread listed publishes
no rows, and is left to rozi's ordinary agent detection.

| Codex reports | Row status |
| --- | --- |
| `active`, waiting on an approval | `blocked`, reason "Approval required" |
| `active`, waiting on your answer | `blocked`, reason "Question needs an answer" |
| `active` | `working` |
| `idle`, after a turn this extension saw finish | `done` |
| `idle` | `idle` |
| `systemError` | `idle`, reason "Thread failed" |

Codex unloads an idle thread from its background server soon after its turn ends. A thread this
extension saw finish stays listed as `done` after that, until a Codex pane has shown it and you
have moved on to another thread. Other idle threads leave the list once Codex unloads them;
`/resume` still opens them.

### With rozi's Codex plugin

rozi's Codex hook plugin reports the state of the thread the client shows. Each row carries its
thread ID as `native_session`, and rozi lets the hook report drive the row with the same ID, so
that row shows the hooks' live state while every other row stays listed.

## Switch threads

Selecting a row focuses the pane, and then this extension switches the client to that thread. It
types `/resume <thread-id>` into Codex's prompt, reads it back from the screen, and only then
presses Enter. The thread you leave stays listed while Codex keeps it loaded. A thread Codex has
already unloaded is loaded again.

The extension does not switch, and says why in a rozi notification, when:

- Codex's prompt holds unsent text. Codex's dim placeholder does not count.
- Codex is showing an approval, a question, a list, or any screen without its prompt.
- A turn is still running in the thread on screen. Codex refuses `/resume` until it ends.
- The selected thread is no longer listed.

If the command does not read back exactly, for example because someone typed at the same moment,
it is left in the prompt unsent. A switch counts once the pane's title names the selected thread
and no other listed thread has that name. If Codex refuses, the notification quotes Codex's answer.
If there is no confirmation within ten seconds, a notification says so, and the active row keeps
following what Codex itself shows.

To list threads without ever typing into Codex, set `switching = false`.

## Settings

Override the defaults in rozi's `config.toml`:

```toml
[extensions.codex-rozi-sessions]
codex = "codex"     # the Codex executable
poll_seconds = 2    # 0.5 to 60
scope = "all"       # or "cwd"
switching = true    # false: never type into Codex
```

With `scope = "all"`, a pane lists every loaded thread, including threads opened from the Codex
app or IDE extension, which share the same background server. With `scope = "cwd"`, it lists the
thread it shows and threads working in or below the pane's directory.

## Limits

- **Windows.** rozi reports no foreground process there, so no pane is recognised as a Codex
  client.
- **Remote panes.** rozi omits the foreground process for a remote attachment, whose client talks
  to another machine's background server, so those panes are left alone.
- **Clients run with `--no-daemon`** keep their threads to themselves; their panes list nothing.
- **The active row** comes from the thread name in the pane's title. A thread without a name yet,
  two threads with the same name, or a `/title` setup that hides the thread name makes it
  ambiguous; the last switch made here is used instead.
- The extension reads the background server through Codex's app-server protocol, which Codex
  marks experimental. A Codex release that changes it shows up as no rows, not as wrong ones.
- Switching reads Codex's screen. A future Codex release that redraws its prompt differently makes
  the extension refuse to switch rather than guess.
- The service runs only while a rozi client with the extension is attached. With a rozi newer
  than 0.0.29, it also exits on its own when that client is killed. Its errors go to a stream rozi
  discards, so a missing `codex` shows up as no rows rather than as a message.

## Development

```bash
python3 -m unittest discover tests -p 'test_*.py'
rozi extensions check .
rozi extensions install --link .
```

## License

MPL-2.0. Contributions require a DCO sign-off.
