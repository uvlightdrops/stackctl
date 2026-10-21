"""Install sibling repos (editable, cloned if missing) and the project into the running venv."""

from __future__ import annotations

import os
import subprocess
import sys

from .normalize import normalize as normalize_paths
from .spec import Project, Sibling


def _clone_sibling(project: Project, sibling: Sibling) -> int:
    path = project.sibling_path(sibling)
    if path.exists() or path.is_symlink():
        if path.is_dir() and (path / ".git").exists():
            print(f"  {sibling.name}: bereits vorhanden in {path}")
            return 0
        print(f"  {sibling.name}: {path} existiert, ist aber kein git repo", file=sys.stderr)
        return 1
    if not sibling.url:
        print(f"  {sibling.name}: fehlt in {path} und keine url angegeben", file=sys.stderr)
        return 1
    print(f"  {sibling.name}: klone {sibling.branch or 'Standard-Branch'} nach {path}", flush=True)
    branch = ["--branch", sibling.branch] if sibling.branch else []
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        return subprocess.call(["git", "clone", "--quiet", *branch, "--", sibling.url, str(path)])
    except OSError as exc:
        print(f"  {sibling.name}: Klonen nach {path} fehlgeschlagen: {exc}", file=sys.stderr)
        return 1


def clone(project: Project) -> int:
    """Clone missing sibling repos without installing packages or changing existing repos."""
    code = 0
    for sibling in project.siblings:
        if _clone_sibling(project, sibling):
            code = 1
    return code


def install(project: Project) -> int:
    if sys.prefix == sys.base_prefix:
        print("Bitte aus einer venv aufrufen.")
        return 1
    pip = [sys.executable, "-m", "pip", "install", "--quiet"]
    subprocess.run([*pip, "--upgrade", "pip"], check=False)
    for sibling in project.siblings:
        path = project.sibling_path(sibling)
        if not path.is_dir():
            if _clone_sibling(project, sibling):
                return 1
        print(f"  {sibling.name}: pip install -e {path}", flush=True)
        if subprocess.call([*pip, "-e", str(path)]):
            return 1
    extras = (os.getenv("STACKCTL_EXTRAS") or project.tool.get("extras") or "").strip()
    target = f"{project.root}[{extras}]" if extras else str(project.root)
    print(f"  {project.root.name}: pip install -e {target}", flush=True)
    if subprocess.call([*pip, "-e", target]):
        return 1
    
    # Normalize file:// URLs to relative paths after successful installation
    print("Normalisiere Pfade in pyproject.toml...", flush=True)
    return normalize_paths(project)
