"""Checkpoint an identified local session and open its counterpart after saving."""
import fcntl
import hashlib
import io
import json
import os
import shlex
import shutil
import socket
import subprocess
import tarfile
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path

from checkpoint_bridge import address, request_text
import dashboard as d

lock = threading.Lock()
status = {"busy": False, "message": ""}
NAMES = {"claude": "Claude Code", "codex": "Codex"}


def reachable(sessions):
    """Offer chats with a live reader, including idle chats in open terminals."""
    codex_paths = [str(s.log.resolve()) for s in sessions if s.provider == "codex"]
    opened = set()
    for offset in range(0, len(codex_paths), 80):
        listing = subprocess.run(["/usr/sbin/lsof", "-F", "cn", "--",
                                  *codex_paths[offset:offset + 80]], capture_output=True, timeout=5)
        codex = False
        for line in os.fsdecode(listing.stdout).splitlines():
            if line.startswith("c"):
                codex = line[1:].lower() == "codex"
            elif codex and line.startswith("n"):
                opened.add(line[1:])
    result = []
    for session in sessions:
        if session.provider == "codex":
            if str(session.log.resolve()) in opened:
                result.append(session)
        else:
            endpoint = Path(address(session.id))
            if endpoint.is_socket():
                try:
                    with endpoint.with_suffix(".lock").open("r") as guard:
                        fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    result.append(session)
                except FileNotFoundError:
                    pass
    return result


def update(message, busy=True):
    global status
    status = {"busy": busy, "message": message}


def reply_text(row, provider):
    if provider == "codex":
        event = row.get("payload", {})
        if row.get("type") == "event_msg" and event.get("type") == "task_complete":
            return event.get("last_agent_message") or ""
    elif row.get("type") == "assistant" and not row.get("isSidechain"):
        message = row["message"]
        return "\n".join(block["text"] for block in message.get("content", [])
                         if block.get("type") == "text")
    return ""


def checkpoint(session):
    ticket = str(uuid.uuid4())
    with session.log.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        if session.provider == "codex":
            result = subprocess.run(["codex", "queue", "--thread", session.id, "--message",
                                     request_text(ticket)], cwd=session.project, capture_output=True,
                                    text=True, timeout=30)
            if result.returncode:
                raise ValueError("Codex could not queue the checkpoint in this chat.")
        else:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
                try:
                    sock.sendto(json.dumps({"ticket": ticket, "project": session.project}).encode(),
                                address(session.id))
                except OSError as exc:
                    raise ValueError("Open this Claude Code session with the Relay hook enabled.") from exc
        end = time.monotonic() + 600
        while time.monotonic() < end:
            offset = stream.tell()
            line = stream.readline()
            if not line.endswith(b"\n"):
                stream.seek(offset)
                time.sleep(0.5)
                continue
            try:
                answer = reply_text(json.loads(line), session.provider).strip()
            except (ValueError, TypeError, KeyError):
                continue
            lines = answer.splitlines()
            if (len(lines) >= 3 and lines[0].strip() == f"RELAY-BEGIN-{ticket}"
                    and lines[-1].strip() == f"RELAY-END-{ticket}"):
                body = "\n".join(lines[1:-1]).strip()
                if body:
                    return body
    raise ValueError("The source session did not return a complete checkpoint within ten minutes.")


def workspace_files(project):
    check = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=project,
                           capture_output=True)
    if check.stdout.strip() == b"true":
        raw = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others",
                                       "--exclude-standard", "--", "."], cwd=project)
        for name in sorted(set(raw.split(b"\0")) - {b""}):
            path = project / os.fsdecode(name)
            if path.is_dir() and not path.is_symlink():
                yield from workspace_files(path)
            elif path.exists() or path.is_symlink():
                yield path
    else:
        def fail(exc):
            raise exc
        for root, dirs, files in os.walk(project, onerror=fail):
            dirs[:] = [name for name in dirs if name != ".git"]
            for name in files + list(dirs):
                path = Path(root) / name
                if path.is_file() or path.is_symlink():
                    yield path


def save(session, body, target):
    project = Path(session.project)
    base = d.relay.STATE / "saves"
    if project in (base.resolve(), *base.resolve().parents):
        raise ValueError("Choose a project folder below the Relay save directory’s parent.")
    key = hashlib.sha256(os.fsencode(project)).hexdigest()[:16]
    versions = base / key
    versions.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    final = versions / name
    note = (f"# Relay checkpoint · {name}\n\nProject: {project}\n"
            f"Source: {NAMES[session.provider]} · {session.id}\nDestination: {NAMES[target]}\n\n{body}\n")
    shared = project / ".ai-relay-checkpoints"
    if shared.is_symlink():
        raise ValueError("The project checkpoint directory is a symbolic link.")
    shared.mkdir(mode=0o700, exist_ok=True)
    note_bytes = note.encode()
    def inventory():
        entries = {}
        for path in workspace_files(project):
            if path == project / ".ai-relay-handoff.md" or shared in path.parents:
                continue
            info = path.lstat()
            if stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                entries[path] = (info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns)
        return entries

    with tempfile.TemporaryDirectory(dir=versions) as tmp:
        stage = Path(tmp) / "save"
        stage.mkdir(mode=0o700)
        (stage / "checkpoint.md").write_text(note)
        entries = inventory()
        with tarfile.open(stage / "workspace.tar.gz", "w:gz", compresslevel=2) as archive:
            for path in entries:
                archive.add(path, arcname=str(path.relative_to(project)), recursive=False)
            info = tarfile.TarInfo(".ai-relay-handoff.md")
            info.size = len(note_bytes)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(note_bytes))
        if inventory() != entries:
            raise ValueError("Project files changed during the save. Click handoff again at the next safe step.")
        if (stage / "checkpoint.md").read_text() != note:
            raise ValueError("Could not verify your saved checkpoint.")
        stage.rename(final)
    d.relay.atomic(shared / (name + ".md"), note)
    d.relay.atomic(project / ".ai-relay-handoff.md", note)
    if (project / ".ai-relay-handoff.md").read_text() != note:
        raise ValueError("The shared checkpoint changed while saving.")
    return final


def launch(project, target, saved):
    exe = shutil.which(target)
    if not exe:
        raise ValueError(f"Install {NAMES[target]} to open this checkpoint.")
    prompt = (f"Continue the user's project handoff {saved.name}. Read .ai-relay-checkpoints/{saved.name}.md in "
              "this directory. Confirm its checkpoint ID and the actual working tree, then continue "
              "the first unfinished step within the user's scope. If already complete, report ready.")
    command = "cd " + shlex.quote(str(project)) + " && " + shlex.join([exe, prompt])
    d.script('on run argv\ntell application "Terminal"\nactivate\ndo script (item 1 of argv)\nend tell\nend run', command)


def run(session):
    target = "claude" if session.provider == "codex" else "codex"
    try:
        body = checkpoint(session)
        update("Saving checkpoint and project files…")
        saved = save(session, body, target)
        update(f"Opening {NAMES[target]}…")
        launch(session.project, target, saved)
        update(f"Saved {saved.name} · opened {NAMES[target]}", False)
    except Exception as exc:
        update(f"Handoff stopped · {exc}", False)
    finally:
        lock.release()


def start(session):
    if not lock.acquire(blocking=False):
        raise ValueError("A handoff is already running.")
    update(f"Waiting for {NAMES[session.provider]} to checkpoint…")
    threading.Thread(target=run, args=(session,), daemon=True).start()
