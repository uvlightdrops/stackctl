"""Local target: processes (detached, PID/log files) and docker compose services."""

from __future__ import annotations

import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from threading import Thread

from .spec import Project, Service, Stack

START_TIMEOUT = 60

# ANSI color codes for log prefixes
COLORS = [
    "\033[36m",  # cyan
    "\033[33m",  # yellow
    "\033[32m",  # green
    "\033[35m",  # magenta
    "\033[34m",  # blue
    "\033[31m",  # red
]
RESET = "\033[0m"


class LogAggregator:
    """Aggregates and displays logs from multiple services with timestamps and colors."""
    
    def __init__(self, log_files: dict[str, Path]) -> None:
        """Initialize with service name -> log file path mapping."""
        self.log_files = log_files
        self.service_colors: dict[str, str] = {}
        self.file_positions: dict[str, int] = {}
        
        # Assign stable colors to services
        for i, service_name in enumerate(sorted(log_files.keys())):
            self.service_colors[service_name] = COLORS[i % len(COLORS)]
            # Start from end of existing log
            log_path = log_files[service_name]
            if log_path.exists():
                self.file_positions[service_name] = log_path.stat().st_size
            else:
                self.file_positions[service_name] = 0
    
    def tail_logs(self, follow: bool = False) -> None:
        """Tail logs from all services with real-time updates."""
        if not self.log_files:
            print("Keine Logs vorhanden")
            return
        
        try:
            # Initial read: show last 40 lines from each file
            for service_name, log_path in sorted(self.log_files.items()):
                if log_path.exists():
                    self._print_new_lines(service_name, log_path, initial=True)
            
            if not follow:
                return
            
            # Real-time monitoring
            print(f"\n{RESET}– Live-Modus (Ctrl+C zum Beenden) –\n")
            
            while True:
                any_new = False
                for service_name, log_path in sorted(self.log_files.items()):
                    if log_path.exists():
                        if self._print_new_lines(service_name, log_path):
                            any_new = True
                
                if not any_new:
                    time.sleep(0.5)
        except KeyboardInterrupt:
            print(f"\n{RESET}Logs beendet")
    
    def _print_new_lines(self, service_name: str, log_path: Path, initial: bool = False) -> bool:
        """Print new lines from log file. Returns True if new lines were printed."""
        try:
            current_size = log_path.stat().st_size
            prev_pos = self.file_positions.get(service_name, 0)
            
            if current_size < prev_pos:
                # Log was rotated/truncated
                prev_pos = 0
            
            if current_size <= prev_pos:
                return False
            
            with log_path.open("r", encoding="utf-8", errors="replace") as f:
                f.seek(prev_pos)
                content = f.read()
                self.file_positions[service_name] = f.tell()
            
            if not content:
                return False
            
            color = self.service_colors[service_name]
            prefix = f"{color}[{service_name:12s}]{RESET}"
            
            if initial:
                # Show last 40 lines for initial display
                lines = content.splitlines()
                lines = lines[-40:] if len(lines) > 40 else lines
                content = "\n".join(lines)
            
            # Add timestamp and service prefix to each line
            for line in content.splitlines():
                timestamp = time.strftime("%H:%M:%S")
                print(f"{prefix} {timestamp}  {line}")
            
            return True
        except (OSError, UnicodeDecodeError):
            return False


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
    
    def wait_for_ready(self, service: Service) -> bool:
        """Wait for service to be healthy with configurable retries and interval."""
        deadline = time.monotonic() + service.health_timeout
        attempt = 0
        
        while attempt < service.health_retries and time.monotonic() < deadline:
            if self.up(service):
                return True
            attempt += 1
            if attempt < service.health_retries:
                time.sleep(service.health_interval)
        
        return False

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
        services = self.stack.select(names, [n for n, s in self.stack.services.items() if s.managed])
        for service in reversed(services):
            ok = self.stop_service(service) and ok
        return 0 if ok else 1

    def restart(self, names: list[str]) -> int:
        services = self.stack.select(names, [n for n, s in self.stack.services.items() if s.managed])
        if not all([self.stop_service(service) for service in reversed(services)]):
            return 1
        return 0 if all([self.start_service(service) for service in services]) else 1

    def logs(self, names: list[str], follow: bool) -> int:
        services = self.stack.select(names, [n for n, s in self.stack.services.items() if s.managed])
        log_files = {s.name: self.log_file(s) for s in services}
        existing = {name: path for name, path in log_files.items() if path.exists()}
        
        if not existing:
            print("Noch keine Logs (nur für hier gestartete Dienste).")
            return 1
        
        aggregator = LogAggregator(existing)
        aggregator.tail_logs(follow=follow)
        return 0

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
            if self.wait_for_ready(service):
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
