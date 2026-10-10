"""Task-scoped transport and supervision of unmodified native Terminal workers.

One host process owns each environment. Cancellation stops its process group
before a separate cleanup worker verifies owned Docker resources are absent.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import math
import multiprocessing
import os
from pathlib import Path
import re
import select
import shutil
import signal
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from integrations.testprism.staging import validate_task_links


def _milliseconds(value, maximum=3600000):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= maximum:
        raise ValueError("Invalid native timeout")
    return value / 1000


def _lifecycle_deadline(seconds):
    # Public task lifetime is not a single RPC. Keep its units and validation
    # separate so tasks longer than the RPC limit can still start the bridge.
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
        raise ValueError("Invalid native lifecycle timeout")
    try:
        seconds = float(seconds)
        deadline = time.monotonic() + seconds
    except OverflowError:
        raise ValueError("Invalid native lifecycle timeout") from None
    if not math.isfinite(seconds) or seconds <= 0 or not math.isfinite(deadline):
        raise ValueError("Invalid native lifecycle timeout")
    return deadline


class NativeBridge:
    def __init__(self, task_path, workdir, original, logs, *, server_image=None, terminal_type=None,
                 timeout_sec=900, cleanup_timeout_sec=90):
        if terminal_type is None:
            from testprism.terminal.terminal import Terminal
            terminal_type = Terminal
        if terminal_type.__module__ == "__main__" or "." in terminal_type.__qualname__:
            raise ValueError("Terminal worker type must be importable from a module")
        self.terminal_type = terminal_type
        self.task_path, self.workdir = Path(task_path).resolve(), workdir
        self.original, self.logs, self.server_image = original, Path(logs), server_image
        self.deadline = _lifecycle_deadline(timeout_sec)
        self.cleanup_timeout = _milliseconds(cleanup_timeout_sec * 1000)
        self.client_image = original.attrs.get("Image") or original.attrs["Config"]["Image"]
        if self.server_image is None:
            project = original.attrs.get("Config", {}).get("Labels", {}).get("com.docker.compose.project")
            if project:
                servers = original.client.containers.list(filters={"label": ["com.docker.compose.project=" + project, "com.docker.compose.service=server"]})
                if servers:
                    self.server_image = servers[0].attrs.get("Image") or servers[0].attrs["Config"]["Image"]
        if not self.task_path.is_dir() or not (self.task_path / "docker-compose.yaml").is_file():
            raise ValueError("Native candidates require an owned task directory with docker-compose.yaml")
        validate_task_links(self.task_path)
        self.owner = "ctb-testhelix-" + uuid.uuid4().hex[:16]
        self.environments, self.operations, self.projects = {}, {}, []
        self.lock, self.closed = threading.RLock(), False
        self.logs.mkdir(parents=True, exist_ok=True)
        self.socket_dir = Path(tempfile.mkdtemp(prefix="testhelix-native-"))
        self.socket = self.socket_dir / "control.sock"
        bridge = self

        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                done, cancelled = threading.Event(), threading.Event()
                request = {}
                def watch():
                    while not done.wait(.025):
                        try:
                            if select.select([self.connection], [], [], 0)[0] and not self.connection.recv(1, socket.MSG_PEEK):
                                cancelled.set()
                                return
                        except OSError:
                            cancelled.set()
                            return
                try:
                    self.connection.settimeout(5)
                    line = self.rfile.readline(100001)
                    self.connection.settimeout(None)
                    if len(line) > 100000 or not line.endswith(b"\n"):
                        raise ValueError("Native request exceeds its framing limit")
                    request = json.loads(line)
                    if set(request) != {"method", "params"} or not isinstance(request["params"], dict):
                        raise ValueError("Invalid native request")
                    threading.Thread(target=watch, daemon=True).start()
                    value = {"ok": True, "result": bridge.dispatch(request["method"], request["params"], cancelled)}
                except Exception as error:
                    value = {"ok": False, "error": f"{type(error).__name__}: {error}"}
                finally:
                    done.set()
                try:
                    self.wfile.write(json.dumps(value, ensure_ascii=False).encode() + b"\n")
                except (BrokenPipeError, ConnectionResetError):
                    if value.get("ok") and request.get("method") == "fork":
                        bridge.close_environment(value["result"]["id"])

        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads = True
        self.server = Server(str(self.socket), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .1}, daemon=True)
        try:
            self._save()
            self.thread.start()
        except BaseException:
            self.server.server_close()
            shutil.rmtree(self.socket_dir, ignore_errors=True)
            raise

    def _save(self):
        with self.lock:
            target = self.logs / "native-resources.json"
            temporary = target.with_suffix(".tmp")
            temporary.write_text(json.dumps({"schemaVersion": 1, "owner": self.owner, "projects": self.projects,
                "closed": self.closed, "pending": list(self.environments), "operations": self.operations}, indent=2))
            temporary.replace(target)

    def dispatch(self, method, params, cancelled=None):
        fields = set(params)
        if method == "fork" and fields == {"label", "operationId", "timeoutMs"}:
            return self.fork(params["label"], params["timeoutMs"], params["operationId"], cancelled)
        if method == "cancel" and fields == {"operationId"}:
            if not isinstance(params["operationId"], str) or not re.fullmatch(r"[a-f0-9]{32}", params["operationId"]):
                raise ValueError("Invalid native operation ID")
            with self.lock:
                name = self.operations.get(params["operationId"])
            if name:
                self.close_environment(name)
            return {"cancelled": True}
        if method == "close" and fields == {"id"}:
            self.close_environment(params["id"])
            return {"closed": True}
        if method == "close_all" and not params:
            self.close_environments()
            return {"closed": True}
        if method == "exec" and fields == {"id", "script", "timeoutMs"}:
            return self.execute(**params, cancelled=cancelled)
        if method == "terminal" and fields == {"id", "action", "name", "text", "timeoutMs"}:
            return self.interact(**params, cancelled=cancelled)
        raise ValueError("Unsupported native operation or fields")

    def _spawn(self, environment, cleanup_only=False):
        parent, child = multiprocessing.Pipe()
        spec = {"module": self.terminal_type.__module__, "class": self.terminal_type.__name__,
                "kwargs": {key: str(value) if isinstance(value, Path) else value for key, value in environment["kwargs"].items()},
                "workdir": self.workdir, "cleanupOnly": cleanup_only, "parentPid": os.getpid()}
        env = os.environ.copy()
        env["PYTHONPATH"] = os.pathsep.join(str(Path(p or os.getcwd()).resolve()) for p in sys.path)
        log = (environment["directory"] / "worker.log").open("ab")
        try:
            process = subprocess.Popen([sys.executable, "-B", "-m", "integrations.testprism.native_worker", str(child.fileno())],
                pass_fds=(child.fileno(),), start_new_session=True, stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env)
        except BaseException:
            parent.close()
            raise
        finally:
            child.close()
            log.close()
        worker = {"process": process, "pipe": parent}
        try:
            parent.send(spec)
        except BaseException:
            self._stop_worker(worker)
            raise
        return worker

    @staticmethod
    def _stop_worker(worker):
        if not worker:
            return
        process = worker["process"]
        # Keep the leader unreaped until the group is killed, preventing PID reuse.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=5)
        worker["pipe"].close()

    @staticmethod
    def _receive(worker, deadline, cancelled=None, external_cancel=None):
        while True:
            if (cancelled and cancelled.is_set()) or (external_cancel and external_cancel.is_set()):
                raise RuntimeError("Native operation cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("Native operation deadline exceeded")
            if worker["pipe"].poll(min(.025, max(.001, deadline - time.monotonic()))):
                try:
                    response = worker["pipe"].recv()
                except EOFError as error:
                    raise RuntimeError("Native worker exited before returning a result") from error
                if not response.get("ok"):
                    if response.get("invalid"):
                        raise ValueError(response.get("error", "Invalid native operation"))
                    raise RuntimeError(response.get("error", "Native worker failed"))
                return response["result"]

    def fork(self, label, timeoutMs=300000, operationId=None, cancelled=None):
        seconds = _milliseconds(timeoutMs)
        operationId = operationId or uuid.uuid4().hex
        if not isinstance(label, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", label):
            raise ValueError("Invalid environment label")
        if not isinstance(operationId, str) or not re.fullmatch(r"[a-f0-9]{32}", operationId):
            raise ValueError("Invalid native operation ID")
        deadline = min(self.deadline, time.monotonic() + seconds)
        with self.lock:
            if self.closed or deadline <= time.monotonic() or (cancelled and cancelled.is_set()):
                raise RuntimeError("Native bridge is closing or deadline exhausted")
            if operationId in self.operations:
                raise ValueError("Native operation ID has already been used")
            name = self.owner + "-" + uuid.uuid4().hex[:10]
            directory = self.logs / name
            task = directory / "task"
            shutil.copytree(self.task_path, task, symlinks=True, ignore=shutil.ignore_patterns(".testprism-*", "__pycache__"))
            kwargs = dict(client_container_name=name, client_image_name=self.client_image, server_image_name=self.server_image,
                docker_image_name_prefix=name, docker_compose_path=task / "docker-compose.yaml", no_rebuild=True, cleanup=False,
                disable_recording=True, sessions_logs_path=directory / "sessions", agent_logs_path=directory / "logs", commands_path=directory / "commands.log")
            environment = {"kwargs": kwargs, "directory": directory, "lock": threading.RLock(), "cancelled": threading.Event(),
                           "closed": False, "worker": None}
            self.environments[name] = environment
            self.projects.append(name)
            self.operations[operationId] = name
            self._save()
        try:
            with environment["lock"]:
                if self.closed or environment["cancelled"].is_set():
                    raise RuntimeError("Native bridge closed before environment startup")
                worker = environment["worker"] = self._spawn(environment)
                self._receive(worker, deadline, environment["cancelled"], cancelled)
                worker["pipe"].send(("start", {}))
                value = self._receive(worker, deadline, environment["cancelled"], cancelled)
                if self.closed or environment["cancelled"].is_set() or (cancelled and cancelled.is_set()):
                    raise RuntimeError("Native bridge closed during environment startup")
                return {"id": name, **value}
        except BaseException:
            self.close_environment(name)
            raise

    def _environment(self, id):
        with self.lock:
            if id not in self.environments or self.environments[id]["cancelled"].is_set():
                raise ValueError("Unknown or closed task environment")
            return self.environments[id]

    def _call(self, id, method, params, timeout_ms, cancelled):
        environment = self._environment(id)
        deadline = min(self.deadline, time.monotonic() + _milliseconds(timeout_ms))
        try:
            with environment["lock"]:
                if environment["cancelled"].is_set() or deadline <= time.monotonic():
                    raise RuntimeError("Native operation cancelled or deadline exhausted")
                worker = environment["worker"]
                worker["pipe"].send((method, params))
                return self._receive(worker, deadline, environment["cancelled"], cancelled)
        except ValueError:
            raise
        except BaseException:
            self.close_environment(id)
            raise

    def execute(self, id, script, timeoutMs, cancelled=None):
        _milliseconds(timeoutMs, 300000)
        if not isinstance(script, str) or not 0 < len(script) <= 64000:
            raise ValueError("Invalid command")
        result = self._call(id, "exec", {"script": script, "timeoutMs": timeoutMs}, timeoutMs + 10000, cancelled)
        if result["timedOut"]:
            self.close_environment(id)
        return result

    def interact(self, id, action, name, text, timeoutMs=30000, cancelled=None):
        _milliseconds(timeoutMs, 300000)
        if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", name):
            raise ValueError("Invalid terminal name")
        if not isinstance(text, str) or len(text) > 16000 or action not in ("start", "send", "read", "interrupt"):
            raise ValueError("Invalid terminal input")
        return self._call(id, "terminal", {"action": action, "name": name, "text": text}, timeoutMs, cancelled)

    def close_environment(self, id, deadline=None):
        with self.lock:
            environment = self.environments.get(id)
            if environment:
                environment["cancelled"].set()
        if environment is None:
            return
        deadline = deadline or time.monotonic() + self.cleanup_timeout
        if not environment["lock"].acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("Native cleanup deadline exceeded waiting for environment")
        try:
            if environment["closed"]:
                return
            self._stop_worker(environment["worker"])
            environment["worker"] = None
            if time.monotonic() >= deadline:
                raise TimeoutError("Native cleanup deadline exceeded; retry from ownership journal")
            worker = self._spawn(environment, cleanup_only=True)
            try:
                self._receive(worker, deadline)
                self._receive(worker, deadline)
            finally:
                self._stop_worker(worker)
            environment["closed"] = True
            with self.lock:
                self.environments.pop(id, None)
                self._save()
        finally:
            environment["lock"].release()

    def close_environments(self):
        with self.lock:
            self.closed = True
            names = list(self.environments)
            for environment in self.environments.values():
                environment["cancelled"].set()
            self._save()
        errors = []
        deadline = time.monotonic() + self.cleanup_timeout
        with ThreadPoolExecutor(max_workers=max(1, min(4, len(names)))) as pool:
            futures = [pool.submit(self.close_environment, name, deadline) for name in names]
            for future in futures:
                try:
                    future.result()
                except Exception as error:
                    errors.append(str(error))
        if errors:
            raise RuntimeError("Native cleanup pending; recover from native-resources.json: " + "; ".join(errors))

    def close(self):
        try:
            self.close_environments()
        finally:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=2)
            shutil.rmtree(self.socket_dir, ignore_errors=True)
