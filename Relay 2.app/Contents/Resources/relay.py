#!/usr/bin/env python3
"""Relay: a local, interactive quota monitor for macOS/Linux."""
import json
import os
import select
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

STATE = Path.home() / ".local" / "state" / "ai-relay"
STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
CACHE = STATE / "claude.json"
NOTE = Path.cwd() / ".ai-relay-handoff.md"
PENDING = STATE / "pending-reset"

def atomic(path, value):
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as f:
        f.write(value)
    os.replace(f.name, path)

def rpc(method, params=None):
    def msg(**parts):
        return json.dumps(parts) + "\n"
    with subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0) as proc:
        try:
            def write(s):
                proc.stdin.write(s.encode())
            def receive(number):
                deadline = time.monotonic() + 8
                buf = b""
                while True:
                    if not select.select([proc.stdout], [], [], max(0, deadline-time.monotonic()))[0]:
                        raise RuntimeError("Codex timed out")
                    chunk = os.read(proc.stdout.fileno(), 65536)
                    if not chunk:
                        raise RuntimeError("Codex disconnected")
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        data = json.loads(line)
                        if data.get("id") == number:
                            if "error" in data:
                                raise RuntimeError(data["error"].get("message", "RPC failed"))
                            return data["result"]
            write(msg(id=1, method="initialize", params={"clientInfo":{"name":"ai_relay","version":"0.1"}}))
            receive(1)
            write(msg(method="initialized"))
            write(msg(id=2, method=method, **({"params":params} if params else {})))
            return receive(2)
        finally:
            proc.kill()

def window(label, percent, end):
    if percent is None or (end and end <= time.time()):
        return label + " awaiting update", False
    when = time.strftime("%a %H:%M", time.localtime(end)) if end else "unknown"
    return f"{label} {percent:g}% · resets {when}", percent >= 90

def codex_status():
    snapshot = rpc("account/rateLimits/read")
    bucket = (snapshot.get("rateLimitsByLimitId") or {}).get("codex") or snapshot.get("rateLimits") or {}
    rows = [window(str(w.get("windowDurationMins","?"))+"m", w.get("usedPercent"), w.get("resetsAt"))
            for w in (bucket.get("primary"), bucket.get("secondary")) if w]
    count = (snapshot.get("rateLimitResetCredits") or {}).get("availableCount")
    return rows, count

def claude_status():
    if not CACHE.exists():
        return [], "waiting for status line"
    data = json.loads(CACHE.read_text())
    rows = [window(label, w.get("used_percentage"), w.get("resets_at"))
            for key,label in [("five_hour","5h"),("seven_day","7d")]
            if (w := data["limits"].get(key))]
    return rows, time.strftime("last seen %a %H:%M",time.localtime(data["time"]))

def show(name, rows, detail):
    warn = any(high for _,high in rows)
    print(("▲ " if warn else "") + name + "  " + " | ".join(x for x,_ in rows) + "  " + detail)
    return warn

def main():
    if sys.argv[1:] == ["claude-status"]:
        data = json.load(sys.stdin)
        limits = data.get("rate_limits") or {}
        atomic(CACHE,json.dumps({"time":time.time(),"limits":{k:limits[k] for k in ("five_hour","seven_day") if k in limits}}))
        print("Relay", " ".join(f"{k} {v['used_percentage']}%" for k,v in limits.items()
                                 if isinstance(v,dict) and 'used_percentage' in v))
        return
    previous = False
    while True:
        count = None
        high = False
        try:
            rows,count = codex_status()
            high = show("Codex",rows,f"earned resets {count if count is not None else 'unknown'}")
        except (OSError,ValueError,RuntimeError) as e:
            print("Codex unavailable ·",e)
        rows,detail = claude_status()
        alert = show("Claude Code",rows,detail) or high
        if alert and not previous:
            print("\a90% reached · save a checkpoint or switch", flush=True)
        previous = alert
        print("p checkpoint request | c continue in Claude | x continue in Codex | q quit"
              + (" | r redeem one Codex reset" if high and count else ""),flush=True)
        if not select.select([sys.stdin],[],[],60)[0]:
            continue
        line = sys.stdin.readline()
        key = line.strip().lower()
        if not line or key == "q":
            return
        if key == "p":
            prompt = (Path(__file__).with_name("checkpoint.txt")).read_text().strip()
            print(prompt)
            if sys.platform == "darwin":
                subprocess.run(["pbcopy"],input=prompt,text=True,check=True)
                print("Copied. Paste into the active coding session.")
        if key in ("c","x"):
            if not NOTE.is_file():
                print("First use p and run the request in the source session.")
                continue
            target = "claude" if key == "c" else "codex"
            print("Checkpoint updated",time.ctime(NOTE.stat().st_mtime), "in",NOTE.parent)
            if input(f"Pause the source agent. Launch {target} here? [y/N] ").lower()=="y":
                try:
                    subprocess.run([target,"Read .ai-relay-handoff.md in this directory. Verify its branch and current state, then continue the next unfinished step."])
                except OSError as e:
                    print(e)
        if key == "r" and high and count and input("Spend one earned Codex reset? [y/N] ").lower()=="y":
            try:
                if not PENDING.exists():
                    atomic(PENDING,str(uuid.uuid4()))
                result = rpc("account/rateLimitResetCredit/consume",{"idempotencyKey":PENDING.read_text()})
                print("Provider outcome:",result.get("outcome"))
                if result.get("outcome") in {"reset","alreadyRedeemed","nothingToReset","noCredit"}:
                    PENDING.unlink()
            except (OSError,RuntimeError,ValueError) as e:
                print("Reset unconfirmed. Retry uses the same request ID.",e)

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
