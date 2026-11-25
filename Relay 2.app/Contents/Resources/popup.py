import fcntl
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
import dashboard as d
from projects import discover, recent_sessions
import handoff

folder=None
choices=[]
sessions=[]
def selected_project(targets):
    if folder in targets:
        return folder
    return next(iter(targets), None)

def scan():
    global choices, sessions
    while True:
        try:
            found = sorted(recent_sessions(Path.home(), time.time()), reverse=True)
            choices, sessions = discover(sessions=found), handoff.reachable(found)
        except (OSError,subprocess.SubprocessError):
            pass
        time.sleep(5)

class PopupHandler(d.Handler):
    def do_GET(self):
        if self.path!="/"+d.TOKEN+"/state":
            return self.send("{}",status=404)
        targets=list(choices)
        d.project=Path(selected) if (selected := selected_project(targets)) else None
        sources = [dict(id=s.id, provider=s.provider, stamp=s.stamp)
                   for s in sessions if s.project == selected]
        self.send(json.dumps(dict(d.state(), choices=targets, automatic=folder not in targets,
                                  sources=sources, handoff=handoff.status)))
    def do_POST(self):
        global folder
        if self.path == "/" + d.TOKEN + "/handoff":
            if self.headers.get("Origin") != d.origin or self.headers.get("Content-Type") != "application/json":
                return self.send("{}", status=403)
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 < size <= 32000:
                    raise ValueError("Invalid request size.")
                data = json.loads(self.rfile.read(size))
                path = selected_project(choices)
                source = next((s for s in sessions if s.project == path and s.id == data.get("id")
                               and s.provider == data.get("provider")), None)
                if path != data.get("path") or not source:
                    raise ValueError("The session changed. Reopen Relay to see the current project.")
                handoff.start(source)
                return self.send("{}")
            except (ValueError, TypeError, AttributeError) as exc:
                return self.send(json.dumps({"message": str(exc)}), status=409)
        if self.path=="/"+d.TOKEN+"/select" and self.headers.get("Origin")==d.origin:
            n=int(self.headers.get("Content-Length",0))
            if 0<n<32000:
                selection=json.loads(self.rfile.read(n)).get("path")
                if selection == "" or selection in choices:
                    folder=selection or None
                    return self.send("{}")
            return self.send("{}",status=400)
        if self.path=="/"+d.TOKEN+"/launch" and self.headers.get("Origin")==d.origin:
            n=int(self.headers.get("Content-Length",0))
            if not 0<n<32000:
                return self.send("{}",status=400)
            data=json.loads(self.rfile.read(n))
            selected=selected_project(choices)
            if not selected or selected!=data.get("path") or data.get("action") not in ("claude","codex"):
                return self.send('{"message":"Project changed. Reopen Relay and try again."}',status=409)
            d.project=Path(selected)
            import io
            payload=json.dumps({"action":data["action"]}).encode()
            self.rfile=io.BytesIO(payload)
            self.headers.replace_header("Content-Length",str(len(payload)))
            self.path="/"+d.TOKEN+"/action"
        super().do_POST()

if __name__=="__main__":
    lock=open(d.relay.STATE/"popup.lock","a")
    try:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        print(json.dumps({"error": "A Relay popup is already running."}), flush=True)
        raise SystemExit
    server=d.HTTPServer(("127.0.0.1",0),PopupHandler)
    d.origin="http://127.0.0.1:"+str(server.server_port)
    for fn in (d.poll,scan,server.serve_forever):
        threading.Thread(target=fn,daemon=True).start()
    print(json.dumps({"origin": d.origin, "token": d.TOKEN}), flush=True)
    # The native launcher owns this pipe for the lifetime of the menu-bar app.
    sys.stdin.read()
