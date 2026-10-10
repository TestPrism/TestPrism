"""Process boundary around the unmodified native Terminal (one owned environment).

The bridge can stop a blocked startup without leaving a Python thread issuing
late Compose commands. Docker resources are verified separately after process exit.
"""
from __future__ import annotations

import base64
import importlib
import os
from pathlib import Path
import shlex
import signal
import threading
import time
import uuid

from integrations.testprism.cleanup import remove_and_verify


def _run(container, command):
    result = container.exec_run(command)
    if result.exit_code:
        raise RuntimeError("Native container command failed: " + result.output.decode(errors="replace")[-1000:])
    return result.output


class Environment:
    def __init__(self, terminal, workdir):
        self.terminal, self.workdir, self.sessions = terminal, workdir, {}

    def start(self):
        self.terminal.start()
        self.session = self.terminal.create_session("testhelix-exec", as_configured_user=True)
        _run(self.terminal.container, ["sh", "-c", "rm -rf -- " + shlex.quote(self.workdir.rstrip("/") + "/agent-tests")])
        return {"container": self.terminal.container.id, "root": self.workdir}

    def execute(self, script, timeoutMs):
        terminal, session = self.terminal, self.session
        prefix = "/tmp/testhelix-command-" + uuid.uuid4().hex
        body = "cd " + shlex.quote(self.workdir) + " || exit 1\nexport PYTHONPATH=" + shlex.quote(self.workdir) + "${PYTHONPATH:+:${PYTHONPATH}}\n" + script
        encoded = base64.b64encode(body.encode()).decode()
        _run(terminal.container, ["sh", "-c", "printf %s " + shlex.quote(encoded) + " | base64 -d > " + prefix + ".sh"])
        # Redirection creates an empty file before printf writes the exit code.
        # Publish it atomically so the polling reader never sees that window.
        command = f"bash {prefix}.sh > {prefix}.out 2> {prefix}.err; printf '%s' $? > {prefix}.status.tmp && mv {prefix}.status.tmp {prefix}.status"
        started = time.monotonic()
        session.send_keys([command, "Enter"], block=False)
        timed_out, code = False, None
        while True:
            result = terminal.container.exec_run(["sh", "-c", f"cat {prefix}.status 2>/dev/null"])
            if result.exit_code == 0:
                code = int(result.output.decode().strip())
                break
            if (time.monotonic() - started) * 1000 >= timeoutMs:
                timed_out = True
                break
            time.sleep(.05)
        outputs, truncated = [], False
        for suffix in ("out", "err"):
            value = terminal.container.exec_run(["sh", "-c", f"head -c 500001 {prefix}.{suffix} 2>/dev/null"])
            truncated |= len(value.output) > 500000
            outputs.append(value.output[:500000].decode(errors="replace"))
        if not timed_out:
            _run(terminal.container, ["sh", "-c", f"rm -f {prefix}.sh {prefix}.out {prefix}.err {prefix}.status {prefix}.status.tmp"])
        return {"code": code, "stdout": outputs[0], "stderr": outputs[1], "timedOut": timed_out,
                "truncated": truncated, "durationMs": round((time.monotonic() - started) * 1000)}

    def interact(self, action, name, text):
        sessions = self.sessions
        if action == "start":
            if name in sessions or len(sessions) >= 4:
                raise ValueError("Terminal exists or session limit reached")
            sessions[name] = self.terminal.create_session("interactive-" + name, as_configured_user=True)
            sessions[name].send_keys(["cd " + shlex.quote(self.workdir), "Enter"], block=True, max_timeout_sec=10)
        if name not in sessions:
            raise ValueError("Unknown interactive terminal")
        if action in ("start", "send") and text:
            sessions[name].send_keys([text, "Enter"], block=False)
        elif action == "interrupt":
            sessions[name].send_keys(["C-c"], block=False)
        elif action not in ("start", "send", "read"):
            raise ValueError("Unknown terminal action")
        output = sessions[name].capture_pane(capture_entire=True)
        return {"ok": True, "name": name, "output": output[-20000:], "truncated": len(output) > 20000}

    def close(self):
        try:
            self.terminal.stop()
        except Exception:
            # Removal verification, not the behavior of best-effort stop(),
            # decides whether this environment is safe to release.
            pass
        remove_and_verify(self.terminal._compose_manager)
        return {"closed": True}


def worker_main(connection, terminal_type, kwargs, workdir, cleanup_only=False):
    # Native EnvModel lets ambient T_BENCH_* override per-project identity.
    # Sanitize only this worker process, never mutate the shared harness env.
    for key in list(os.environ):
        if key.startswith("T_BENCH_"):
            del os.environ[key]
    connection.send({"ok": True, "result": {"ready": True}})
    try:
        terminal = terminal_type(**kwargs)
        if cleanup_only:
            remove_and_verify(terminal._compose_manager)
            connection.send({"ok": True, "result": {"closed": True}})
            return
        environment = Environment(terminal, workdir)
        while True:
            request = connection.recv()
            try:
                method, params = request
                if method == "start":
                    result = environment.start()
                elif method == "exec":
                    result = environment.execute(**params)
                elif method == "terminal":
                    result = environment.interact(**params)
                elif method == "close":
                    result = environment.close()
                else:
                    raise ValueError("Unknown worker operation")
                connection.send({"ok": True, "result": result})
                if method == "close":
                    return
            except Exception as error:
                connection.send({"ok": False, "invalid": isinstance(error, ValueError), "error": f"{type(error).__name__}: {error}"})
    except (EOFError, BrokenPipeError):
        pass
    except Exception as error:
        try:
            connection.send({"ok": False, "error": f"{type(error).__name__}: {error}"})
        except (EOFError, BrokenPipeError):
            pass
    finally:
        connection.close()


if __name__ == "__main__":
    import sys
    from multiprocessing.connection import Connection
    connection = Connection(int(sys.argv[1]))
    spec = connection.recv()
    def watch_parent():
        while True:
            if os.getppid() != spec["parentPid"]:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            time.sleep(.1)
    threading.Thread(target=watch_parent, daemon=True).start()
    terminal_type = getattr(importlib.import_module(spec["module"]), spec["class"])
    kwargs = {key: Path(value) if key.endswith("_path") else value for key, value in spec["kwargs"].items()}
    worker_main(connection, terminal_type, kwargs, spec["workdir"], spec.get("cleanupOnly", False))
