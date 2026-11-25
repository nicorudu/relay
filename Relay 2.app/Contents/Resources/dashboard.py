import json
import os
import secrets
import shlex
import shutil
import subprocess
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import relay

ROOT = Path(__file__).resolve().parent
TOKEN = secrets.token_urlsafe(32)
project = None
snapshot = {}
refresh = threading.Event()

def poll():
    global snapshot
    last_rows, seen = [], None
    while True:
        try:
            last_rows, count = relay.codex_status()
            seen = time.time()
            online = True
        except (ValueError, RuntimeError, OSError):
            count, online = None, False
        snapshot = dict(rows=last_rows, credits=count, online=online, seen=seen)
        refresh.wait(60)
        refresh.clear()

def state():
    try:
        rows, detail = relay.claude_status()
    except (ValueError, OSError, KeyError, TypeError):
        rows, detail = [], "Reading unavailable"
    note = project / ".ai-relay-handoff.md" if project else None
    stamp = note.stat().st_mtime if note and note.is_file() else None
    return dict(codex=snapshot, claude=rows, detail=detail, project=str(project or ""),
                saved=stamp, request=(ROOT/"checkpoint.txt").read_text())

def script(source, *args):
    result = subprocess.run(["osascript","-"]+list(args),input=source,text=True,capture_output=True)
    if result.returncode:
        raise ValueError("Action canceled or macOS permission was declined.")
    return result.stdout.strip()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass
    def send(self, body, kind="application/json", status=200):
        self.send_response(status)
        self.send_header("Content-Type",kind)
        self.send_header("Cache-Control","no-store")
        self.send_header("X-Frame-Options","DENY")
        self.end_headers()
        self.wfile.write(body.encode())
    def do_GET(self):
        if self.path == "/"+TOKEN:
            self.send((ROOT/"index.html").read_text(),"text/html; charset=utf-8")
        elif self.path == "/"+TOKEN+"/state":
            self.send(json.dumps(state()))
        else:
            self.send("{}",status=404)
    def do_POST(self):
        global project
        if (self.path != "/"+TOKEN+"/action" or self.headers.get("Origin") != origin
                or self.headers.get("Content-Type") != "application/json"):
            return self.send("{}",status=403)
        try:
            size = int(self.headers.get("Content-Length",0))
            if not 0 < size <= 32000:
                raise ValueError("Request too large")
            data=json.loads(self.rfile.read(size))
            action=data["action"]
            message="Done"
            if action=="folder":
                project=Path(script('POSIX path of (choose folder with prompt "Choose the project both agents can open")'))
            elif action=="refresh":
                refresh.set()
                message="Refreshing Codex usage…"
            elif action=="reset":
                rows,count=relay.codex_status()
                if not count or not any(high for _,high in rows):
                    raise ValueError("Reset unavailable. Refresh the usage readings.")
                if not relay.PENDING.exists():
                    relay.atomic(relay.PENDING,str(uuid.uuid4()))
                result=relay.rpc("account/rateLimitResetCredit/consume",{"idempotencyKey":relay.PENDING.read_text()})
                outcome=result.get("outcome")
                if outcome in {"reset","alreadyRedeemed","nothingToReset","noCredit"}:
                    relay.PENDING.unlink()
                message="Reset response: "+str(outcome)
                refresh.set()
            elif action=="quit":
                threading.Thread(target=self.server.shutdown,daemon=True).start()
                message="Relay closed. You can close this tab."
            elif action in ("claude","codex") and project:
                note=project/".ai-relay-handoff.md"
                if not note.is_file():
                    raise ValueError("Save the checkpoint in this project first.")
                exe=shutil.which(action)
                if not exe:
                    raise ValueError("Relay could not find "+action+" on this Mac.")
                command="cd "+shlex.quote(str(project))+" && "+shlex.join([exe,"Read .ai-relay-handoff.md. Verify the current project state and continue the next unfinished step."])
                script('on run argv\ntell application "Terminal"\nactivate\ndo script (item 1 of argv)\nend tell\nend run',command)
                message="Opened "+action+" in your project."
            else:
                raise ValueError("Choose your project first.")
            self.send(json.dumps({"message":message}))
        except (ValueError,RuntimeError,OSError,KeyError) as e:
            self.send(json.dumps({"message":str(e)}),status=400)

if __name__=="__main__":
    import fcntl
    lock=open(relay.STATE/"dashboard.lock","a+")
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        lock.seek(0)
        url=lock.read().strip()
        if url.startswith("http://127.0.0.1:"):
            webbrowser.open(url)
        raise SystemExit
    server=HTTPServer(("127.0.0.1",0),Handler)
    origin="http://127.0.0.1:"+str(server.server_port)
    url=origin+"/"+TOKEN
    lock.seek(0)
    lock.truncate()
    lock.write(url)
    lock.flush()
    os.chmod(lock.name,0o600)
    threading.Thread(target=poll,daemon=True).start()
    webbrowser.open(url)
    server.serve_forever()
