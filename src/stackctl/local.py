"""Local target: processes (detached, PID/log files) and docker compose services."""

from __future__ import annotations

import os
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

from .spec import Project, Service, Stack

START_TIMEOUT = 60


class LocalRunner:
    def __init__(self, project: Project, stack: Stack) -> None:
        self.project = project
        self.stack = stack
        self.root = project.root
        self.run_dir = self.root / "var" / "run"
        self.log_dir = self.root / "var" / "log"

    # --- probing ---------------------------------------------------------------------

    def pid_file(self, service: Service) -> Path:
        return self.run_dir / f"{service.name}.pid"

    def log_file(self, service: Service) -> Path:
        return self.log_dir / f"{service.name}.log"

    @staticmethod
    def port_open(service: Service, timeout: float = 0.5) -> bool:
        try:
            with socket.create_connection((service.probe_host, service.port), timeout=timeout):
                return True
        except OSError:
            return False

    @staticmethod
    def http_ok(url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                return response.status < 500
        except Exception:
            return False

    def up(self, service: Service) -> bool:
        if not self.port_open(service):
            return False
        return self.http_ok(service.url) if service.health_path else True

    def read_pid(self, service: Service) -> int | None:
        try:
            pid = int(self.pid_file(service).read_text().strip())
        except (OSError, ValueError):
            return None
        if _pid_alive(pid):
            return pid
        self.pid_file(service).unlink(missing_ok=True)
        return None

    # --- actions ---------------------------------------------------------------------

    def status_line(self, service: Service) -> str:
        up = self.up(service)
        detail = []
        own = self.read_pid(service)
        pid = own or (_listener_pid(service.port) if up else None)
        if pid:
            detail.append(f"PID {pid}")
            if own is None and service.managed:
                detail.append("extern gestartet")
        if up and service.health_path:
            detail.append(service.url)
        state = "läuft " if up else "gestoppt"
        return f"  {service.name:<9} {state}  :{service.port:<5} {'  '.join(detail)}"

    def status(self, _names: list[str]) -> int:
        print(f"{self.stack.name} (lokal)")
        for service in self.stack.services.values():
            print(self.status_line(service))
        return 0

    def start(self, names: list[str]) -> int:
        ok = True
        for service in self.stack.select(names):
            ok = self.start_service(service) and ok
        return 0 if ok else 1

    def stop(self, names: list[str]) -> int:
        ok = True
        for service in reversed(self.stack.select(names, [n for n, s in self.stack.services.items() if s.managed])):
            ok = self.stop_service(service) and ok
        return 0 if ok else 1

    def restart(self, names: list[str]) -> int:
        services = self.stack.select(names, [n for n, s in self.stack.services.items() if s.managed])
        if not all([self.stop_service(service) for service in reversed(services)]):
            return 1
        return 0 if all([self.start_service(service) for service in services]) else 1

    def logs(self, names: list[str], follow: bool) -> int:
        services = self.stack.select(names, [n for n, s in self.stack.services.items() if s.managed])
        files = [str(self.log_file(s)) for s in services if self.log_file(s).exists()]
        if not files:
            print("Noch keine Logs (nur für hier gestartete Dienste).")
            return 1
        return subprocess.call(["tail", "-n", "40", *(["-F"] if follow else []), *files])

    # --- start -----------------------------------------------------------------------

    def start_service(self, service: Service) -> bool:
        if self.port_open(service):
            print(f"  {service.name}: läuft bereits (:{service.port})")
        elif service.kind == "external":
            print(f"  {service.name}: nicht erreichbar ({service.probe_host}:{service.port}) – eigener Dienst, bitte dort starten")
            return False
        elif service.kind == "compose":
            if not self._start_compose(service):
                return False
        elif not self._start_process(service):
            return False
        return self._post_start(service)

    def _env(self, service: Service) -> dict[str, str]:
        return {**os.environ, "PYTHONUNBUFFERED": "1", **service.env}

    def _start_compose(self, service: Service) -> bool:
        compose_dir = self.root / service.compose.get("dir", ".")
        missing = [f for f in service.compose.get("requires", []) if not (self.root / f).exists()]
        if missing:
            print(f"  {service.name}: nicht erreichbar und {', '.join(missing)} fehlt – bitte anlegen oder manuell starten")
            return False
        if service.probe_host not in {"127.0.0.1", "localhost", "::1"}:
            print(f"  {service.name}: {service.probe_host} ist kein lokaler Host – bitte dort starten")
            return False
        if shutil.which("docker") is None:
            print(f"  {service.name}: nicht erreichbar und kein docker vorhanden")
            return False
        compose_service = service.compose.get("service")
        print(f"  {service.name}: starte Container ({compose_dir.relative_to(self.root)}, Port {service.port}) …")
        command = ["docker", "compose", "up", "-d", *([compose_service] if compose_service else [])]
        result = subprocess.run(command, cwd=compose_dir, env=self._env(service))
        return result.returncode == 0 and self._wait(service)

    def _start_process(self, service: Service) -> bool:
        if not service.command:
            print(f"  {service.name}: kein command definiert")
            return False
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_file(service)
        env = self._env(service)
        with log_path.open("a") as log:
            log.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} start {service.name} ===\n")
            log.flush()
            if service.prepare:
                print(f"  {service.name}: {' '.join(service.prepare[1:])} …")
                if subprocess.run(service.prepare, cwd=self.root, stdout=log, stderr=subprocess.STDOUT, env=env).returncode:
                    print(f"  {service.name}: Vorbereitung fehlgeschlagen, siehe {log_path}")
                    print(_tail(log_path))
                    return False
            process = subprocess.Popen(
                service.command,
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # detached: survives this script and the terminal
                env=env,
            )
        self.pid_file(service).write_text(str(process.pid))
        if self._wait(service, process):
            print(f"  {service.name}: gestartet (PID {process.pid}) {service.url if service.health_path else ''}")
            return True
        print(f"  {service.name}: Start fehlgeschlagen, siehe {log_path}")
        print(_tail(log_path))
        return False

    def _post_start(self, service: Service) -> bool:
        if not service.post_start:
            return True
        result = subprocess.run(service.post_start, cwd=self.root, env=self._env(service))
        if result.returncode:
            print(f"  {service.name}: post_start fehlgeschlagen ({' '.join(service.post_start)})")
        return result.returncode == 0

    def _wait(self, service: Service, process: subprocess.Popen | None = None) -> bool:
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self.up(service):
                return True
            if process is not None and process.poll() is not None:
                return False
            time.sleep(0.5)
        return False

    # --- stop ------------------------------------------------------------------------

    def stop_service(self, service: Service) -> bool:
        if not service.managed:
            print(f"  {service.name}: wird nicht von hier gestoppt ({service.kind})")
            return True
        pids = []
        own = self.read_pid(service)
        if own:
            pids.append(own)
        listener = _listener_pid(service.port)
        if listener and own and _session(listener) == own:
            listener = None  # child of our own detached session (e.g. a reload worker): killpg covers it
        if listener and listener not in pids:
            # Started by hand: only stop it if it really is this service from this project.
            if service.marker not in _cmdline(listener) or _cwd(listener) != self.root:
                print(f"  {service.name}: Port {service.port} gehört einem fremden Prozess (PID {listener}) – nicht gestoppt")
                return False
            pids.append(listener)
            parent = _parent_pid(listener)
            if parent and service.marker in _cmdline(parent):
                pids.append(parent)  # e.g. an autoreloader parent
        if not pids:
            print(f"  {service.name}: läuft nicht")
            return True
        _terminate(pids)
        self.pid_file(service).unlink(missing_ok=True)
        if self.port_open(service):
            print(f"  {service.name}: Port {service.port} ist noch belegt")
            return False
        print(f"  {service.name}: gestoppt")
        return True


# --- process helpers (Linux /proc + ss) ---------------------------------------------------


def _listener_pid(port: int) -> int | None:
    """PID listening on ``port`` (None if unknown or owned by another user)."""
    try:
        output = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if "pid=" not in output:
        return None
    return int(output.split("pid=", 1)[1].split(",", 1)[0])


def _cmdline(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode().strip()
    except OSError:
        return ""


def _cwd(pid: int) -> Path | None:
    try:
        return Path(f"/proc/{pid}/cwd").resolve()
    except OSError:
        return None


def _parent_pid(pid: int) -> int | None:
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[1])
    except (OSError, ValueError, IndexError):
        return None


def _session(pid: int) -> int | None:
    try:
        return os.getsid(pid)
    except OSError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate(pids: list[int]) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in pids:
            try:
                if sig == signal.SIGTERM and os.getpgid(pid) == pid:
                    os.killpg(pid, sig)  # our own detached session incl. children
                else:
                    os.kill(pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and any(_pid_alive(pid) for pid in pids):
            time.sleep(0.2)
        if not any(_pid_alive(pid) for pid in pids):
            return


def _tail(path: Path, lines: int = 15) -> str:
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except OSError:
        return ""
