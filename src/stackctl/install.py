"""Install sibling repos (editable, cloned if missing) and the project into the running venv."""

from __future__ import annotations

import os
import subprocess
import sys

from .spec import Project


def install(project: Project) -> int:
    if sys.prefix == sys.base_prefix:
        print("Bitte aus einer venv aufrufen.")
        return 1
    pip = [sys.executable, "-m", "pip", "install", "--quiet"]
    subprocess.run([*pip, "--upgrade", "pip"], check=False)
    for sibling in project.siblings:
        path = project.sibling_path(sibling)
        if not path.is_dir():
            if not sibling.url:
                print(f"  {sibling.name}: fehlt in {path} und keine url angegeben")
                return 1
            print(f"  {sibling.name}: klone {sibling.branch or 'Standard-Branch'} nach {path}", flush=True)
            branch = ["--branch", sibling.branch] if sibling.branch else []
            if subprocess.call(["git", "clone", "--quiet", *branch, sibling.url, str(path)]):
                return 1
        print(f"  {sibling.name}: pip install -e {path}", flush=True)
        if subprocess.call([*pip, "-e", str(path)]):
            return 1
    extras = (os.getenv("STACKCTL_EXTRAS") or project.tool.get("extras") or "").strip()
    target = f"{project.root}[{extras}]" if extras else str(project.root)
    print(f"  {project.root.name}: pip install -e {target}", flush=True)
    return subprocess.call([*pip, "-e", target])
