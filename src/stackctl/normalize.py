"""Normalize absolute file paths to relative paths in pyproject.toml files.

After installing editable packages, pyproject.toml files may contain absolute
file:// URLs with hardcoded paths like /home/flow/dev_flow/XXX. This module
normalizes them to relative paths (../XXX) so the installation works across
different machines and directory structures.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from .spec import Project, Sibling


def _normalize_pyproject(pyproject_path: Path, src_dir: Path) -> bool:
    """
    Normalize absolute file:// URLs in pyproject.toml to relative paths.
    
    Replaces patterns like:
        file:///home/flow/dev_flow/ki-core
    With:
        file://../ki-core
    
    Returns True if file was modified.
    """
    if not pyproject_path.exists():
        return False

    content = pyproject_path.read_text()
    original = content

    # Pattern to match file:// URLs with absolute paths
    # Matches: file:///home/flow/dev_flow/<package_name> followed by quote/slash/space
    pattern = r'file:///home/flow/dev_flow/([a-zA-Z0-9_-]+)(?=[/"\s])'
    
    def replace_with_relative(match):
        pkg_name = match.group(1)
        # Check if this package is a sibling (exists in src_dir)
        pkg_path = src_dir / pkg_name
        if pkg_path.exists() and pkg_path.is_dir():
            # Replace with relative path, preserve what comes after
            return f'file://../{pkg_name}'
        # Keep original if not a known sibling
        return match.group(0)
    
    content = re.sub(pattern, replace_with_relative, content)
    
    if content != original:
        pyproject_path.write_text(content)
        return True
    return False


def normalize(project: Project) -> int:
    """
    Normalize file:// URLs in all project and sibling pyproject.toml files.
    
    This should be called after `stackctl install` to convert hardcoded absolute
    paths to relative paths so the installation is portable across machines.
    """
    src_dir = project.src_dir
    modified = []
    
    # Normalize main project
    main_pyproject = project.root / "pyproject.toml"
    if _normalize_pyproject(main_pyproject, src_dir):
        modified.append(main_pyproject)
        print(f"  {project.root.name}: pyproject.toml normalisiert")
    
    # Normalize all siblings
    for sibling in project.siblings:
        sibling_path = project.sibling_path(sibling)
        sibling_pyproject = sibling_path / "pyproject.toml"
        if _normalize_pyproject(sibling_pyproject, src_dir):
            modified.append(sibling_pyproject)
            print(f"  {sibling.name}: pyproject.toml normalisiert")
    
    if not modified:
        print("  Keine absoluten Pfade gefunden - bereits normalisiert")
        return 0
    
    # Commit changes if in git repo
    try:
        # Check if we're in a git repo and there are changes
        for pyproject in modified:
            repo_root = pyproject.parent
            while repo_root != repo_root.parent:
                if (repo_root / ".git").exists():
                    # Found repo root - commit from there
                    rel_paths = [str(p.relative_to(repo_root)) for p in modified if (repo_root / p.relative_to(repo_root)).exists()]
                    if rel_paths:
                        os.chdir(repo_root)
                        subprocess.run(["git", "add"] + rel_paths, check=False)
                        subprocess.run(
                            ["git", "commit", "-m", "Normalize file:// URLs to relative paths in pyproject.toml"],
                            check=False,
                        )
                    break
                repo_root = repo_root.parent
    except Exception as e:
        print(f"  Warnung: Git-Commit fehlgeschlagen: {e}", flush=True)
    
    return 0
