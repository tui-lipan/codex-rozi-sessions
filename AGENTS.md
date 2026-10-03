# codex-rozi-sessions agent guide

## Mission

Show every Codex thread loaded in Codex's background server as its own rozi Activity row in the
pane running the Codex client, grouped by the worktree it works in, and switch the client to a
thread when its row is selected. The repository is a rozi extension: a manifest and one supervised
Python service.

## Commands

- Unit tests: `python3 -m unittest discover tests -p 'test_*.py'`
- Manifest validation: `rozi extensions check .`
- Whitespace check: `git diff --check`

## Workflow rules

- Use only public interfaces: `codex app-server daemon version`, the app-server protocol's
  `initialize`, `thread/loaded/list`, and `thread/read` over the daemon's published socket, Codex's
  `/resume` command, and rozi's documented CLI (`list-panes`, `capture-pane`, `send-text`,
  `send-keys`, `notify`, `publish`) and extension environment. Never read Codex's or rozi's
  private state files, and never start the Codex daemon.
- Only read from the daemon. Never call a method that changes a thread, its settings, or the
  daemon.
- Never type into Codex's prompt unless the screen shows an empty prompt, no dialog, and no
  running turn, and the daemon reports the shown thread as not active. Read the typed command back
  before pressing Enter.
- Keep the service standard-library Python 3 with no third-party dependencies.
- A row status must be `working`, `idle`, `blocked`, or `done`; rozi reads any other status word as
  a live run.
- Keep `README.md` in sync with behavior, settings, and `min_rozi`.
- Bump `version` in `extension.toml`, and `VERSION` in `bin/codex_sessions.py`, with every change
  users install: patch for a fix, minor for a feature or setting. rozi offers an update whenever
  the remote moves, and labels one without a new version only by its commit hash.
- Update this guide when durable repository conventions change.

## Commits

- Use Conventional Commits and include a DCO `Signed-off-by` trailer.
- Do not commit local configuration, credentials, runtime data, or `__pycache__`.
