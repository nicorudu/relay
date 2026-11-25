"""Attach Relay's wake-up hook without changing the existing display or other hooks."""
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
settings = (config_dir / "settings.json").resolve()
script = Path(__file__).resolve().parents[1] / "Relay 2.app/Contents/Resources/checkpoint_bridge.py"
entry = {"hooks": [{"type": "command", "command": sys.executable,
                    "args": ["-B", str(script)], "asyncRewake": True, "timeout": 86400}]}
settings.parent.mkdir(parents=True, exist_ok=True)
original = settings.read_text() if settings.exists() else "{}"
config = json.loads(original)
changed = False
collector = shlex.join([sys.executable, "-B", str(script.with_name("relay.py")), "claude-status"]) + " >/dev/null"
if "statusLine" not in config:
    config["statusLine"] = {"type": "command", "command": collector}
    changed = True
elif config["statusLine"] != {"type": "command", "command": collector}:
    print("Keeping your existing Claude status line. For usage collection, see 'Existing status lines' in README.md.")
for event in ("SessionStart", "Stop"):
    hooks = config.setdefault("hooks", {}).setdefault(event, [])
    if entry not in hooks:
        hooks.append(entry)
        changed = True
if changed:
    if settings.exists():
        with tempfile.NamedTemporaryFile(dir=settings.parent, prefix="settings.pre-relay-handoff-", delete=False) as backup:
            backup.write(original.encode())
    with tempfile.NamedTemporaryFile(mode="w", dir=settings.parent, delete=False) as out:
        json.dump(config, out, indent=2, ensure_ascii=False)
        out.write("\n")
    if settings.exists() and settings.read_text() != original:
        os.unlink(out.name)
        raise SystemExit("Claude settings changed during installation. Retry.")
    os.replace(out.name, settings)
