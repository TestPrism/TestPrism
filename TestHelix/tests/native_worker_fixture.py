"""Importable process-backed Terminal fixture; shared files model Docker state."""
import json
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace


class Container:
    attrs = {"Image": "sha256:fixture", "Config": {"Image": "mutable-fixture-tag"}}
    id = "fixture"
    def exec_run(self, command):
        result = subprocess.run(command, capture_output=True)
        return SimpleNamespace(exit_code=result.returncode, output=result.stdout + result.stderr)


class Session:
    def __init__(self):
        self.commands = []
    def send_keys(self, keys, **kwargs):
        self.commands.append(keys)
        if keys[0].startswith(("sh /tmp/testhelix-command-", "bash /tmp/testhelix-command-")):
            subprocess.Popen(keys[0], shell=True)
    def capture_pane(self, **kwargs):
        return json.dumps(self.commands)


class Terminal:
    def __init__(self, **kwargs):
        control = json.loads((Path(kwargs["docker_compose_path"]).parent / "fixture.json").read_text())
        self.root = Path(control["root"])
        self.name = kwargs["client_container_name"]
        self.marker = self.root / (self.name + ".live")
        self.container = Container()
        self.sessions = {}
        containers = SimpleNamespace(list=lambda **_: [self.name] if self.marker.exists() else [])
        empty = SimpleNamespace(list=lambda **_: [])
        self._compose_manager = SimpleNamespace(_force_remove_compose_project=self.remove,
            _client_container_name=self.name, _client=SimpleNamespace(containers=containers, networks=empty, volumes=empty))
    def start(self):
        self.marker.touch()
        if (self.root / "block-start").exists():
            child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
            (self.root / "startup-child.pid").write_text(str(child.pid))
            while (self.root / "block-start").exists():
                time.sleep(.025)
        (self.root / (self.name + ".started")).touch()
    def create_session(self, name, **kwargs):
        self.sessions[name] = Session()
        return self.sessions[name]
    def stop(self):
        pass
    def remove(self):
        if (self.root / "cleanup-fails").exists():
            return  # Native best-effort removal swallows Docker failures.
        self.marker.unlink(missing_ok=True)
