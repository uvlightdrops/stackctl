from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .spec import Project


@dataclass(frozen=True)
class RepoStatus:
    name: str
    path: Path
    branch: str
    head: str
    upstream: str
    upstream_head: str
    ahead: int
    behind: int
    dirty: bool
    exists: bool = True
    is_git: bool = True
    error: str = ""


def repo_status(project: Project) -> int:
    rows = collect_repos(project)
    _print(rows)
    return 0 if all(not row.error for row in rows) else 1


def repo_pull(project: Project) -> int:
    rows = collect_repos(project)
    changed = False
    code = 0
    for row in rows:
        if row.error:
            code = 1
            continue
        if row.dirty:
            print(f"{row.name}: übersprungen (dirty)")
            code = 1
            continue
        if row.upstream == "-":
            print(f"{row.name}: übersprungen (kein upstream für Branch {row.branch})")
            code = 1
            continue
        if row.behind == 0:
            print(f"{row.name}: aktuell ({row.branch} {row.head})")
            continue
        _git(row.path, "pull", "--ff-only")
        changed = True
        print(f"{row.name}: aktualisiert ({row.branch}, war {row.behind} hinterher)")
    if changed:
        print()
    _print(collect_repos(project))
    return code


def collect_repos(project: Project) -> list[RepoStatus]:
    rows = [_collect(project.root.name, project.root)]
    rows.extend(_collect(sibling.name, project.sibling_path(sibling)) for sibling in project.siblings)
    return rows


def _collect(name: str, path: Path) -> RepoStatus:
    if not path.exists():
        return RepoStatus(name=name, path=path, branch="-", head="-", upstream="-", upstream_head="-",
                          ahead=0, behind=0, dirty=False, exists=False, error="fehlt")
    if not (path / ".git").exists():
        return RepoStatus(name=name, path=path, branch="-", head="-", upstream="-", upstream_head="-",
                          ahead=0, behind=0, dirty=False, is_git=False, error="kein git repo")

    _git(path, "fetch", "--quiet", "--all", "--prune")
    branch = _git(path, "rev-parse", "--abbrev-ref", "HEAD")
    head = _git(path, "rev-parse", "--short", "HEAD")
    upstream = _git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}", check=False)
    upstream_head = "-"
    ahead = behind = 0
    if upstream:
        upstream_head = _git(path, "rev-parse", "--short", "@{upstream}")
        counts = _git(path, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        ahead, behind = [int(part) for part in counts.split()]
    dirty = bool(_git(path, "status", "--short"))
    return RepoStatus(name=name, path=path, branch=branch, head=head, upstream=upstream or "-",
                      upstream_head=upstream_head, ahead=ahead, behind=behind, dirty=dirty)


def _git(path: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(["git", *args], cwd=path, capture_output=True, text=True)
    if check and result.returncode:
        stderr = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"{path}: git {' '.join(args)} fehlgeschlagen: {stderr}")
    if result.returncode:
        return ""
    return result.stdout.strip()


def _print(rows: list[RepoStatus]) -> None:
    headers = ("NAME", "BRANCH", "LOCAL", "UPSTREAM", "REMOTE", "A/B", "DIRTY", "STATUS", "PATH")
    data = [headers]
    for row in rows:
        if row.error:
            data.append((row.name, "-", "-", "-", "-", "-", "-", row.error, str(row.path)))
            continue
        dirty = "yes" if row.dirty else "no"
        sync = f"{row.ahead}/{row.behind}" if row.upstream != "-" else "-"
        if row.dirty:
            state = "dirty"
        elif row.upstream == "-":
            state = "no-upstream"
        elif row.behind > 0:
            state = "behind"
        elif row.ahead > 0:
            state = "ahead"
        else:
            state = "ok"
        data.append((row.name, row.branch, row.head, row.upstream, row.upstream_head, sync, dirty, state, str(row.path)))
    widths = [max(len(str(line[i])) for line in data) for i in range(len(headers))]
    for line in data:
        print("  ".join(str(value).ljust(widths[i]) for i, value in enumerate(line)))
