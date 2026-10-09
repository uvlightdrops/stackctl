"""minikube / Kubernetes target: build images, render values, deploy the generic Helm chart.

Everything project-specific comes from the spec (``stack.k8s`` and the
per-service ``k8s`` blocks); the chart in ``stackctl/chart`` is generic.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .spec import Project, Service, SpecError, Stack

CHART_DIR = Path(__file__).resolve().parent / "chart"


def _run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess:
    return subprocess.run(command, check=False, **kwargs)


def git_files(directory: Path) -> list[str] | None:
    """Tracked + untracked-not-ignored files (None if not a git checkout)."""
    result = _run(
        ["git", "-C", str(directory), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        capture_output=True,
    )
    if result.returncode:
        return None
    return [name for name in result.stdout.decode().split("\0") if name]


def stage_context(source: Path, target: Path) -> Path:
    """Copy a build context without .venv, data, secrets, ... (only what git would see)."""
    files = git_files(source)
    if files is None:
        shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__"))
        return target
    for name in files:
        src = source / name
        if not src.exists() and not src.is_symlink():
            continue  # deleted but still tracked
        dst = target / name
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            shutil.copy2(src, dst)
    return target


def read_dotenv(path: Path) -> dict[str, str]:
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


class K8sRunner:
    def __init__(self, project: Project, stack: Stack) -> None:
        self.project = project
        self.stack = stack
        self.cfg = stack.k8s
        self.minikube = stack.target == "minikube"
        self.namespace = self.cfg.get("namespace") or stack.name
        self.release = self.cfg.get("release") or stack.name
        self.mk = {"profile": stack.name, "cpus": 2, "memory": 4096, **(self.cfg.get("minikube") or {})}
        self.context = self.mk["profile"] if self.minikube else self.cfg.get("context")
        self.registry = (self.cfg.get("registry") or "").rstrip("/")
        if not self.minikube and self.cfg.get("images") and not self.registry:
            raise SpecError("stack.k8s.registry fehlt: Images müssen für k8s in eine Registry gepusht werden")

    # --- cli wrappers ----------------------------------------------------------------

    def kubectl(self, *args: str) -> list[str]:
        return ["kubectl", *(["--context", self.context] if self.context else []), "-n", self.namespace, *args]

    def helm(self, *args: str) -> list[str]:
        return ["helm", *(["--kube-context", self.context] if self.context else []), "-n", self.namespace, *args]

    def deployed(self) -> list[Service]:
        return [s for s in self.stack.services.values() if s.k8s is not None]

    # --- cluster ---------------------------------------------------------------------

    def ensure_cluster(self) -> bool:
        if not self.minikube:
            if _run(self.kubectl("get", "ns", "default"), capture_output=True).returncode:
                print(f"  Cluster nicht erreichbar (Kontext {self.context or 'aktuell'})")
                return False
            return True
        profile = self.mk["profile"]
        if _run(["minikube", "status", "-p", profile], capture_output=True).returncode == 0:
            return True
        print(f"  minikube: starte Profil {profile} …", flush=True)
        command = ["minikube", "start", "-p", profile, f"--cpus={self.mk['cpus']}", f"--memory={self.mk['memory']}"]
        if self.mk.get("driver"):
            command.append(f"--driver={self.mk['driver']}")
        return _run(command).returncode == 0

    # --- images ----------------------------------------------------------------------

    def build_images(self) -> dict[str, str]:
        """Build every image in ``stack.k8s.images``; returns key -> pullable reference."""
        refs = {}
        for key, image in (self.cfg.get("images") or {}).items():
            refs[key] = self._build_image(key, image or {})
        return refs

    def _build_image(self, key: str, image: dict[str, Any]) -> str:
        name = f"{self.release}-{key}"
        with tempfile.TemporaryDirectory(prefix=f"stackctl-{name}-") as tmp:
            tmp_path = Path(tmp)
            print(f"  image {name}: Kontext vorbereiten …", flush=True)
            context = stage_context(self.project.root / image.get("context", "."), tmp_path / "context")
            command = ["docker", "build", "-q", "-t", f"{name}:build"]
            command += ["-f", str(context / image.get("dockerfile", "Dockerfile"))]
            for sibling in self._image_siblings(image):
                path = self.project.sibling_path(sibling)
                if not path.is_dir():
                    raise SpecError(f"Geschwister-Repo fehlt: {path} (zuerst stackctl install)")
                staged = stage_context(path, tmp_path / "siblings" / sibling.name)
                command += ["--build-context", f"{sibling.name}={staged}"]
            print(f"  image {name}: docker build …", flush=True)
            result = _run([*command, str(context)], capture_output=True, text=True)
            if result.returncode:
                raise SpecError(f"docker build {name} fehlgeschlagen:\n{result.stderr[-3000:]}")
        image_id = result.stdout.strip().split(":")[-1]
        ref = f"{self.registry + '/' if self.registry else ''}{name}:{image_id[:12]}"
        _run(["docker", "tag", f"{name}:build", ref])
        if self.minikube:
            print(f"  image {ref}: in minikube laden …", flush=True)
            if _run(["minikube", "-p", self.mk["profile"], "image", "load", ref]).returncode:
                raise SpecError(f"minikube image load {ref} fehlgeschlagen")
        else:
            print(f"  image {ref}: push …", flush=True)
            if _run(["docker", "push", ref]).returncode:
                raise SpecError(f"docker push {ref} fehlgeschlagen")
        return ref

    def preload_external_images(self) -> None:
        """minikube: copy third-party images already present in local docker (saves the download)."""
        if not self.minikube:
            return
        listed = _run(["minikube", "-p", self.mk["profile"], "image", "ls"], capture_output=True, text=True).stdout
        for service in self.deployed():
            image = str((service.k8s or {}).get("image") or "@app")
            if image.startswith("@") or image in listed or f"docker.io/{image}" in listed:
                continue
            if _run(["docker", "image", "inspect", image], capture_output=True).returncode == 0:
                print(f"  image {image}: aus lokalem docker in minikube laden …", flush=True)
                _run(["minikube", "-p", self.mk["profile"], "image", "load", image])

    def _image_siblings(self, image: dict[str, Any]):
        wanted = image.get("siblings")
        siblings = self.project.siblings
        return siblings if wanted is None else [s for s in siblings if s.name in wanted]

    # --- values ----------------------------------------------------------------------

    def _image(self, value: str, refs: dict[str, str]) -> str:
        if value.startswith("@"):
            try:
                return refs[value[1:]]
            except KeyError:
                raise SpecError(f"Image '{value}' nicht in stack.k8s.images definiert") from None
        return value

    def values(self, refs: dict[str, str]) -> dict[str, Any]:
        cfg = self.cfg
        # secret_env holds defaults, secret_env_files (e.g. a compose .env) override them
        secret_env: dict[str, str] = {k: str(v) for k, v in (cfg.get("secret_env") or {}).items()}
        for name in cfg.get("secret_env_files") or []:
            path = self.project.root / name
            if path.is_file():
                secret_env.update(read_dotenv(path))
        secret_files = {
            Path(name).name: (self.project.root / name).read_text(encoding="utf-8")
            for name in cfg.get("secret_files") or []
            if (self.project.root / name).is_file()
        }
        global_env = {k: str(v) for k, v in (cfg.get("env") or {}).items()}
        services, jobs = [], []
        claims: dict[str, str] = {}
        for service in self.deployed():
            k8s = service.k8s or {}
            image = self._image(str(k8s.get("image") or "@app"), refs)
            env = _env_list({**service.env, **global_env, **{k: str(v) for k, v in (k8s.get("env") or {}).items()}})
            command = k8s.get("command") or (service.command if service.kind == "process" else [])
            init = k8s.get("init_command") or (service.prepare if k8s.get("init", True) else [])
            services.append(
                {
                    "name": service.name,
                    "image": image,
                    "command": [str(c) for c in command],
                    "initCommand": [str(c) for c in init],
                    "port": service.port,
                    "env": env,
                    "secretEnv": bool(k8s.get("secret_env")) and bool(secret_env),
                    "secretFiles": bool(secret_files) and bool(k8s.get("secret_files", True)),
                    "probePath": service.health_path,
                    "expose": bool(k8s.get("expose")),
                    "volumes": [
                        {"name": vol, "mount": spec["mount"], "claim": self._claim(service, vol, spec, claims)}
                        for vol, spec in (k8s.get("volumes") or {}).items()
                    ],
                    "resources": k8s.get("resources") or {},
                }
            )
            if service.post_start and k8s.get("post_start_job", True):
                jobs.append(
                    {
                        "name": f"{service.name}-post-start",
                        "image": self._image(str(k8s.get("post_start_image") or "@app"), refs),
                        "command": [str(c) for c in service.post_start],
                        "env": env,
                        "secretEnv": services[-1]["secretEnv"],
                        "secretFiles": services[-1]["secretFiles"],
                    }
                )
        ingress = dict(cfg.get("ingress") or {})
        if ingress.get("host"):
            target = self.stack.service(ingress.setdefault("service", next(
                (s.name for s in self.deployed() if (s.k8s or {}).get("expose")), "")))
            ingress.setdefault("port", target.port)
        return {
            "stack": self.stack.name,
            "filesDir": cfg.get("files_dir", "/app"),
            "nodePort": self.minikube,
            "secretEnv": secret_env,
            "secretFiles": secret_files,
            "services": services,
            "volumeClaims": [{"name": name, "size": size} for name, size in claims.items()],
            "jobs": jobs,
            "ingress": {"enabled": bool(ingress.get("host")) and not self.minikube, **ingress},
        }

    @staticmethod
    def _claim(service: Service, volume: str, spec: dict[str, Any], claims: dict[str, str]) -> str:
        """PVC name: ``<service>-<volume>``, or ``<volume>`` if ``shared`` (mounted by several services)."""
        name = volume if spec.get("shared") else f"{service.name}-{volume}"
        size = str(spec.get("size", "1Gi"))
        if claims.setdefault(name, size) != size:
            raise SpecError(f"Volume {name}: widersprüchliche Größen ({claims[name]} / {size})")
        return name

    # --- actions ---------------------------------------------------------------------

    def start(self, _names: list[str]) -> int:
        if not self.ensure_cluster():
            return 1
        refs = self.build_images()
        self.preload_external_images()
        values = self.values(refs)
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", prefix="stackctl-values-") as handle:
            os.chmod(handle.name, 0o600)  # contains secrets
            yaml.safe_dump(values, handle, sort_keys=False)
            handle.flush()
            print(f"  helm upgrade --install {self.release} (Namespace {self.namespace}) …", flush=True)
            command = self.helm(
                "upgrade", "--install", self.release, str(CHART_DIR), "--create-namespace",
                "-f", handle.name, "--wait", "--timeout", str(self.cfg.get("timeout", "10m")),
            )
            code = _run(command).returncode
        self.status([])
        return code

    def stop(self, _names: list[str]) -> int:
        """Uninstall the release; persistent volumes are kept."""
        if _run(self.helm("status", self.release), capture_output=True).returncode:
            print(f"  Release {self.release} ist nicht installiert")
            return 0
        return _run(self.helm("uninstall", self.release, "--wait")).returncode

    def restart(self, names: list[str]) -> int:
        services = [s.name for s in self.stack.select(names, [s.name for s in self.deployed()])]
        return _run(self.kubectl("rollout", "restart", *[f"deployment/{name}" for name in services])).returncode

    def delete(self, _names: list[str]) -> int:
        """Uninstall and delete the namespace including all data volumes."""
        self.stop([])
        command = ["kubectl", *(["--context", self.context] if self.context else [])]
        return _run([*command, "delete", "namespace", self.namespace, "--ignore-not-found", "--wait=false"]).returncode

    def status(self, _names: list[str]) -> int:
        where = f"minikube -p {self.mk['profile']}" if self.minikube else (self.context or "aktueller Kontext")
        print(f"{self.stack.name} ({self.stack.target}: {where}, Namespace {self.namespace})")
        if self.minikube and _run(["minikube", "status", "-p", self.mk["profile"]], capture_output=True).returncode:
            print("  Cluster gestoppt")
            return 0
        _run(self.kubectl("get", "pods,svc,pvc", "-l", f"app.kubernetes.io/instance={self.release}"))
        self.urls()
        return 0

    def urls(self) -> None:
        for service in self.deployed():
            if not (service.k8s or {}).get("expose"):
                continue
            if self.minikube:
                result = _run(
                    ["minikube", "-p", self.mk["profile"], "service", service.name, "-n", self.namespace, "--url"],
                    capture_output=True, text=True, timeout=30,
                )
                url = result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else "-"
            else:
                host = (self.cfg.get("ingress") or {}).get("host")
                url = f"http://{host}/" if host else f"kubectl port-forward svc/{service.name} {service.port}"
            print(f"  {service.name}: {url}")

    def logs(self, names: list[str], follow: bool) -> int:
        services = self.stack.select(names, [s.name for s in self.deployed()])
        if len(services) != 1:
            raise SpecError("logs auf k8s: genau einen Dienst angeben")
        return _run(self.kubectl("logs", f"deployment/{services[0].name}", "--tail=100", *(["-f"] if follow else []))).returncode


def _env_list(env: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": key, "value": value} for key, value in env.items()]
