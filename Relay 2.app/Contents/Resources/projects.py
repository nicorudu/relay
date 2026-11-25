"""Discover local coding sessions, reading paths and timestamps only."""
import json
import os
import subprocess
import time
import uuid
from typing import NamedTuple
from pathlib import Path

def run(args):
    return subprocess.run(args, capture_output=True, text=True, timeout=4).stdout

def active_dirs():
    result=set()
    pids=[]
    processes = run(["/bin/ps", "-axo", "pid=,args="])
    servers = {line.split()[0] for line in processes.splitlines()
               if "app-server" in line.split()[1:]}
    for line in run(["/bin/ps", "-axo", "uid=,pid=,comm="]).splitlines():
        parts = line.strip().split(None, 2)
        if (len(parts) == 3 and parts[0] == str(os.getuid())
                and Path(parts[2]).name in {"claude", "codex"} and parts[1] not in servers):
            pids.append(parts[1])
    if pids:
        for line in run(["/usr/sbin/lsof","-a","-p",",".join(pids),"-d","cwd","-Fn"]).splitlines():
            if line.startswith("n/"):
                result.add(line[1:])
    return result

class Session(NamedTuple):
    stamp: float
    project: str
    provider: str
    id: str
    log: Path


def recent_sessions(home, now):
    claude = Path(os.environ.get("CLAUDE_CONFIG_DIR", home / ".claude"))
    roots = [(claude / "projects", "*/*.jsonl", "claude")]
    codex = Path(os.environ.get("CODEX_HOME", home / ".codex")) / "sessions"
    # The directory dates the creation of the chat, even if it is active today.
    for base, pattern, provider in [*roots, (codex, "*/*/*/*.jsonl", "codex")]:
        for file in base.glob(pattern):
            try:
                stamp = file.stat().st_mtime
                if stamp < now - 86400:
                    continue
                with file.open("rb") as stream:
                    for _ in range(80):
                        line = stream.readline(4 * 1024 * 1024)
                        if not line or not line.endswith(b"\n"):
                            break
                        row = json.loads(line)
                        if provider == "codex":
                            if row.get("type") != "session_meta":
                                break
                            meta = row["payload"]
                            if isinstance(meta.get("source"), dict):
                                break
                            ident, cwd = meta["id"], meta["cwd"]
                        else:
                            if row.get("isSidechain") or not row.get("cwd"):
                                continue
                            ident, cwd = row["sessionId"], row["cwd"]
                        if Path(cwd).is_absolute():
                            yield Session(stamp, str(Path(cwd).resolve()), provider,
                                          str(uuid.UUID(ident)), file)
                        break
            except (OSError, KeyError, TypeError, ValueError):
                continue


def discover(home=None,now=None,live=None,sessions=None):
    home=home or Path.home()
    now=now or time.time()
    live=active_dirs() if live is None else live
    scores={}
    live = {str(Path(path).resolve()) for path in live}
    sessions = recent_sessions(home,now) if sessions is None else sessions
    recent = [(s.stamp,s.project) for s in sessions if s.stamp >= now-1800 or s.project in live]
    for stamp,path in recent+[(0,p) for p in live]:
        p=Path(path)
        if not p.is_absolute() or not p.is_dir():
            continue
        p=p.resolve()
        if p in (home,Path("/")) or any(part.endswith(".app") for part in p.parts) or ".codex" in p.parts or ".claude" in p.parts:
            continue
        scores[str(p)]=max(stamp,scores.get(str(p),0))
    return sorted(scores,key=lambda p:(p not in live,-scores[p],p.casefold()))
