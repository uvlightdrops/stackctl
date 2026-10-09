"""Stack specification: where it comes from and what it looks like.

A project opts in with a ``[tool.stackctl]`` table in its ``pyproject.toml``::

    [tool.stackctl]
    config_loader = "ki_core:load_config"   # callable returning the resolved config (optional)
    config_key = "stack"                     # section holding the stack spec
    spec_file = "deploy/stack.yaml"          # used when no loader is configured

    [[tool.stackctl.siblings]]               # editable sibling repos for `stackctl install`
    name = "ki-core"
    url = "https://github.com/org/ki-core.git"
    branch = "master"

The loader lets a project resolve the spec through its own config line
(stages, profiles, env overrides); stackctl itself knows no project names.

Strings in the spec may use placeholders, rendered per target:
``{target} {python} {root} {name} {host} {port} {probe_host}`` and
``{services[<name>].port}`` / ``{services[<name>].probe_host}`` / ``.host``
(``{{``/``}}`` for literal braces).  ``stack.targets.<target>`` overlays the
spec per target (local, minikube, k8s).
"""

from __future__ import annotations

import importlib
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

LOCAL = "local"
K8S_TARGETS = ("minikube", "k8s")
TARGETS = (LOCAL, *K8S_TARGETS)
KINDS = ("process", "compose", "external")


class SpecError(ValueError):
    pass


@dataclass(frozen=True)
class Sibling:
    name: str
    url: str = ""
    branch: str = ""


@dataclass
class Project:
    root: Path
    tool: dict[str, Any]

    @property
    def siblings(self) -> list[Sibling]:
        return [Sibling(**entry) for entry in self.tool.get("siblings", [])]

    @property
    def src_dir(self) -> Path:
        return Path(os.getenv("STACKCTL_SRC_DIR") or self.tool.get("src_dir") or self.root.parent).expanduser()

    def sibling_path(self, sibling: Sibling) -> Path:
        return self.src_dir / sibling.name


def find_project(start: Path | None = None) -> Project:
    """Nearest directory (upwards) whose pyproject.toml has a ``[tool.stackctl]`` table."""
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            tool = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("tool", {}).get("stackctl")
            if tool is not None:
                return Project(directory, tool)
    raise SpecError(f"Kein pyproject.toml mit [tool.stackctl] ab {here}")


def load_raw_spec(project: Project) -> dict[str, Any]:
    """The stack section, resolved through the project's loader or read from ``spec_file``."""
    loader = project.tool.get("config_loader")
    key = project.tool.get("config_key", "stack")
    previous = Path.cwd()
    os.chdir(project.root)  # loaders usually resolve their config relative to cwd
    sys.path.insert(0, str(project.root))
    try:
        if loader:
            module_name, _, attr = loader.partition(":")
            config = getattr(importlib.import_module(module_name), attr or "load_config")()
        else:
            path = project.root / project.tool.get("spec_file", "deploy/stack.yaml")
            config = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    finally:
        sys.path.pop(0)
        os.chdir(previous)
    spec = (config or {}).get(key) if isinstance(config, dict) else None
    if not isinstance(spec, dict) or not spec.get("services"):
        raise SpecError(f"Abschnitt '{key}.services' fehlt in der Projekt-Konfiguration")
    return spec


@dataclass
class Service:
    name: str
    kind: str = "process"
    host: str = "127.0.0.1"
    port: int = 0
    health_path: str = ""
    command: list[str] = field(default_factory=list)
    prepare: list[str] = field(default_factory=list)
    post_start: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    marker: str = ""
    compose: dict[str, Any] = field(default_factory=dict)
    k8s: dict[str, Any] | None = None

    @property
    def probe_host(self) -> str:
        return "127.0.0.1" if self.host in {"", "0.0.0.0", "::"} else self.host

    @property
    def url(self) -> str:
        return f"http://{self.probe_host}:{self.port}{self.health_path or '/'}"

    @property
    def managed(self) -> bool:
        """Started and stopped as a process by stackctl."""
        return self.kind == "process" and bool(self.command)


@dataclass
class Stack:
    name: str
    target: str
    services: dict[str, Service]
    k8s: dict[str, Any]
    raw: dict[str, Any]

    def service(self, name: str) -> Service:
        try:
            return self.services[name]
        except KeyError:
            raise SpecError(f"Unbekannter Dienst: {name} (verfügbar: {', '.join(self.services)})") from None

    def select(self, names: list[str], default: list[str] | None = None) -> list[Service]:
        """Services in spec order (start order)."""
        wanted = set(names or (default if default is not None else self.services))
        for name in wanted:
            self.service(name)
        return [service for name, service in self.services.items() if name in wanted]


class _Ref:
    """Placeholder view of a service for ``{services[api].port}``."""

    def __init__(self, host: str, port: int, probe_host: str) -> None:
        self.host, self.port, self.probe_host = host, port, probe_host


def render(value: Any, context: dict[str, Any]) -> Any:
    if isinstance(value, str):
        try:
            return value.format_map(context)
        except (KeyError, AttributeError, IndexError) as exc:
            raise SpecError(f"Platzhalter in '{value}' nicht auflösbar: {exc}") from None
    if isinstance(value, list):
        return [render(item, context) for item in value]
    if isinstance(value, dict):
        return {key: render(item, context) for key, item in value.items()}
    return value


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def build_stack(raw: dict[str, Any], root: Path, target: str | None = None) -> Stack:
    """Typed, placeholder-rendered stack for ``target`` (default: ``stack.target``, else local).

    ``stack.targets.<target>`` is deep-merged over the spec for that target.
    """
    target = target or raw.get("target") or LOCAL
    if target not in TARGETS:
        raise SpecError(f"Ziel '{target}' unbekannt (erlaubt: {', '.join(TARGETS)})")
    raw = deep_merge(raw, (raw.get("targets") or {}).get(target) or {})
    in_cluster = target in K8S_TARGETS
    services_raw: dict[str, dict[str, Any]] = raw["services"]

    refs: dict[str, _Ref] = {}
    for name, item in services_raw.items():
        port = int(item.get("port") or 0)
        if in_cluster:
            refs[name] = _Ref("0.0.0.0", port, name)  # cluster DNS name of the k8s Service
        else:
            host = str(item.get("host") or "127.0.0.1")
            refs[name] = _Ref(host, port, "127.0.0.1" if host in {"", "0.0.0.0", "::"} else host)

    base = {
        "target": target,
        "python": "python" if in_cluster else sys.executable,
        "root": "/app" if in_cluster else str(root),
        "services": refs,
    }
    services: dict[str, Service] = {}
    for name, item in services_raw.items():
        kind = item.get("kind", "process")
        if kind not in KINDS:
            raise SpecError(f"{name}: kind '{kind}' unbekannt (erlaubt: {', '.join(KINDS)})")
        ref = refs[name]
        context = {**base, "name": name, "host": ref.host, "port": ref.port, "probe_host": ref.probe_host}
        k8s = item.get("k8s")
        services[name] = Service(
            name=name,
            kind=kind,
            host=ref.host,
            port=ref.port,
            health_path=str(item.get("health_path", "")),
            command=render(list(item.get("command") or []), context),
            prepare=render(list(item.get("prepare") or []), context),
            post_start=render(list(item.get("post_start") or []), context),
            env={k: str(v) for k, v in render(item.get("env") or {}, context).items()},
            marker=str(item.get("marker", "")),
            compose=dict(item.get("compose") or {}),
            k8s=render(dict(k8s), context) if isinstance(k8s, dict) else None,
        )
    return Stack(
        name=str(raw.get("name") or root.name),
        target=target,
        services=services,
        k8s=render(dict(raw.get("k8s") or {}), {**base, "name": raw.get("name") or root.name}),
        raw=raw,
    )
