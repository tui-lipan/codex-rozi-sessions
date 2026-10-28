# Scene for the extension gallery screenshot; see the rozi-extension skill's screenshots guide.
# One Codex client with threads loaded in two repositories and a worktree. `codex` is a stand-in:
# `codex app-server daemon version` points at a small fake daemon that serves staged threads over
# Codex's WebSocket JSON-RPC, and run plainly it draws a Codex screen. No real Codex is involved.
repo() {
    git init -q -b "$2" "$1"
    git -C "$1" -c user.name=demo -c user.email=demo@example.invalid commit -q --allow-empty -m init
}
repo "$HOME/src/rozi" master
git -C "$HOME/src/rozi" worktree add -q -b codex/fix-login "$HOME/src/rozi-fix-login"
repo "$HOME/src/tui-lipan" main

cat > "$LAB/daemon.py" <<'PY'
import json, os, socket, struct, sys, threading, time

home, path = sys.argv[1], sys.argv[2]
start = time.monotonic()

def threads():
    # The docs thread finishes its turn a few seconds in, so its row reads done.
    docs_active = time.monotonic() - start < 6
    return {
        "t1": {"id": "t1", "name": "Investigate pane close animation", "cwd": f"{home}/src/rozi",
               "status": {"type": "active", "activeFlags": []}, "createdAt": 1},
        "t2": {"id": "t2", "name": "Fix login redirect", "cwd": f"{home}/src/rozi-fix-login",
               "status": {"type": "active", "activeFlags": ["waitingOnApproval"]}, "createdAt": 2},
        "t3": {"id": "t3", "name": "Document focused_node_id", "cwd": f"{home}/src/tui-lipan",
               "status": {"type": "active" if docs_active else "idle", "activeFlags": []},
               "createdAt": 3},
    }

def serve(connection):
    buffer = b""
    def read(count):
        nonlocal buffer
        while len(buffer) < count:
            chunk = connection.recv(65536)
            if not chunk:
                raise OSError
            buffer += chunk
        data, buffer = buffer[:count], buffer[count:]
        return data
    def send(value):
        payload = json.dumps(value).encode()
        header = bytes([0x81])
        header += bytes([len(payload)]) if len(payload) < 126 else bytes([126]) + struct.pack(">H", len(payload))
        connection.sendall(header + payload)
    while b"\r\n\r\n" not in buffer:
        buffer += connection.recv(4096)
    buffer = buffer.partition(b"\r\n\r\n")[2]
    connection.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
    try:
        while True:
            first, second = read(2)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", read(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", read(8))[0]
            mask = read(4)
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(read(length)))
            if first & 0x0F != 0x1:
                continue
            message = json.loads(payload)
            if "id" not in message:
                continue
            method, params = message["method"], message.get("params") or {}
            if method == "initialize":
                result = {"userAgent": "demo"}
            elif method == "thread/loaded/list":
                result = {"data": list(threads()), "nextCursor": None}
            elif method == "thread/read":
                result = {"thread": threads()[params["threadId"]]}
            else:
                result = {}
            send({"id": message["id"], "result": result})
    except OSError:
        connection.close()

server = socket.socket(socket.AF_UNIX)
server.bind(path)
server.listen(8)
while True:
    connection, _ = server.accept()
    threading.Thread(target=serve, args=(connection,), daemon=True).start()
PY
python3 "$LAB/daemon.py" "$HOME" "$LAB/codex.sock" &
daemon_pid=$!
teardown() { kill "$daemon_pid" 2>/dev/null; }

bin codex "LAB='$LAB'"'
if [ "$1 $2 $3" = "app-server daemon version" ]; then
    printf "{\"status\": \"running\", \"socketPath\": \"%s\"}\n" "$LAB/codex.sock"
    exit
fi
width=$(stty size | cut -d" " -f2)
rule=$(printf "─%.0s" $(seq 1 $((width - 2))))
printf "\033]0;⠋ Investigate pane close animation | rozi\007\033[2J\033[H"
cat <<EOF
╭$(printf "─%.0s" $(seq 1 44))╮
│ >_ OpenAI Codex                            │
│                                            │
│ model:     gpt-5-codex   /model to change  │
│ directory: ~/src/rozi                      │
╰$(printf "─%.0s" $(seq 1 44))╯

› why does the pane close animation stutter?

• Explored
  └ Read anim.rs, pane_close.rs
    Search frame_budget in src/view

• The close animation schedules its frames from the
  UI tick, so a busy tick drops one. I am moving it
  onto the animation clock.

• Working (38s • esc to interrupt)

› Ask Codex to do anything

  ⏎ send   ⇧⏎ newline   ⌃T transcript   ⌃C quit
EOF
while read -r _; do :; done'

# Lay out first: the stand-in draws its screen at the pane's width when it starts.
keys "cd ~/src/rozi" Enter
rozi split --cwd "$HOME/src/rozi-fix-login" --argv bash >/dev/null
# The sidebar: `Ctrl+A`, then `b`.
keys C-a b
settle 1000
rozi focus 1 >/dev/null
settle 300
keys "clear; codex" Enter
# Long enough for the docs thread to finish and two polls to see it.
settle 10000
