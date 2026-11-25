"""A Claude asyncRewake hook scoped to the session that registered it."""
import fcntl
import json
import socket
import sys
import uuid
from pathlib import Path

ROOT = Path.home() / ".local/state/ai-relay/listeners"


def address(session_id):
    return str(ROOT / str(uuid.UUID(session_id)))


def request_text(ticket):
    return (
        "The user clicked Relay's handoff button. Finish your current safe step, then pause. "
        "In your final reply return a standalone Markdown checkpoint for another coding agent. "
        "Include the user's objective and acceptance criteria, completed changes, branch and "
        "working files, checks actually run, blockers and the first unfinished step. "
        "Omit secrets and raw transcripts. Do not write a checkpoint file or start the next step. "
        f"Use exactly RELAY-BEGIN-{ticket} on the first line and RELAY-END-{ticket} on the last line."
    )


def listen():
    event = json.load(sys.stdin)
    path = Path(address(event["session_id"]))
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        if path.exists():
            path.unlink()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.bind(str(path))
            try:
                while True:
                    packet = json.loads(sock.recv(8192))
                    if Path(packet["project"]).resolve() == Path(event["cwd"]).resolve():
                        ticket = str(uuid.UUID(packet["ticket"]))
                        print(request_text(ticket), file=sys.stderr, flush=True)
                        return 2
            finally:
                path.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(listen())
