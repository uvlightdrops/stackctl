import shutil
import subprocess
import sys
from pathlib import Path
from textwrap import dedent

import pytest
import yaml

from stackctl.cli import main
from stackctl.k8s import CHART_DIR, K8sRunner
from stackctl.spec import Project, SpecError, build_stack, deep_merge

SPEC = {
    "name": "demo",
    "services": {
        "db": {"kind": "compose", "port": 5432, "k8s": {"image": "postgres:16", "secret_env": True,
                                                         "volumes": {"pgdata": {"mount": "/var/lib/pg", "size": "2Gi"}}}},
        "api": {
            "port": 8090,
            "host": "0.0.0.0",
            "health_path": "/docs",
            "command": ["{python}", "-m", "uvicorn", "--host", "{host}", "--port", "{port}"],
            "prepare": ["{python}", "-m", "migrate"],
            "env": {"DB_URL": "postgres://{services[db].probe_host}:{services[db].port}", "ROOT": "{root}"},
            "k8s": {"volumes": {"data": {"mount": "/data", "shared": True, "size": "5Gi"}}},
        },
        "web": {
            "port": 8000,
            "command": ["{python}", "manage.py", "runserver", "{host}:{port}"],
            "post_start": ["{python}", "seed.py"],
            "env": {"API": "http://{services[api].probe_host}:{services[api].port}"},
            "k8s": {"expose": True, "volumes": {"data": {"mount": "/data", "shared": True, "size": "5Gi"}}},
        },
    },
    "k8s": {"namespace": "demo-ns", "images": {"app": {"dockerfile": "Dockerfile"}},
            "secret_env": {"PG_USER": "demo"}, "secret_env_files": [".env"], "secret_files": ["creds.yaml"]},
    "targets": {"minikube": {"services": {"web": {"command": ["{python}", "serve", "--noreload"]}}}},
}


def test_local_placeholders(tmp_path):
    stack = build_stack(SPEC, tmp_path)
    api, web = stack.service("api"), stack.service("web")
    assert stack.target == "local"
    assert api.command == [sys.executable, "-m", "uvicorn", "--host", "0.0.0.0", "--port", "8090"]
    assert api.probe_host == "127.0.0.1"
    assert api.env == {"DB_URL": "postgres://127.0.0.1:5432", "ROOT": str(tmp_path)}
    assert web.env["API"] == "http://127.0.0.1:8090"
    assert [s.name for s in stack.select([])] == ["db", "api", "web"]
    assert [s.name for s in stack.select(["web", "db"])] == ["db", "web"]


def test_cluster_target_uses_service_dns_and_overlay(tmp_path):
    stack = build_stack(SPEC, tmp_path, "minikube")
    assert stack.service("api").env == {"DB_URL": "postgres://db:5432", "ROOT": "/app"}
    assert stack.service("web").env["API"] == "http://api:8090"
    assert stack.service("web").command == ["python", "serve", "--noreload"]
    assert stack.service("api").host == "0.0.0.0"


def test_errors(tmp_path):
    with pytest.raises(SpecError, match="unbekannt"):
        build_stack(SPEC, tmp_path, "nomad")
    with pytest.raises(SpecError, match="Unbekannter Dienst"):
        build_stack(SPEC, tmp_path).select(["nope"])
    bad = deep_merge(SPEC, {"services": {"web": {"env": {"X": "{missing}"}}}})
    with pytest.raises(SpecError, match="Platzhalter"):
        build_stack(bad, tmp_path)


def test_deep_merge_null_removes_block():
    merged = deep_merge({"a": {"k8s": {"x": 1}, "port": 1}}, {"a": {"k8s": None}})
    assert merged == {"a": {"k8s": None, "port": 1}}


def _values(tmp_path, target="minikube"):
    (tmp_path / ".env").write_text("PG_USER=override\nPG_PASSWORD='s3cret'\n# comment\n")
    (tmp_path / "creds.yaml").write_text("token: x\n")
    stack = build_stack(SPEC, tmp_path, target)
    return K8sRunner(Project(tmp_path, {}), stack).values({"app": "demo-app:abc"})


def test_values(tmp_path):
    values = _values(tmp_path)
    assert values["secretEnv"] == {"PG_USER": "override", "PG_PASSWORD": "s3cret"}
    assert values["secretFiles"] == {"creds.yaml": "token: x\n"}
    by_name = {s["name"]: s for s in values["services"]}
    assert by_name["db"]["image"] == "postgres:16" and by_name["db"]["command"] == []
    assert by_name["db"]["secretEnv"] is True and by_name["api"]["secretEnv"] is False
    assert by_name["api"]["image"] == "demo-app:abc"
    assert by_name["api"]["initCommand"] == ["python", "-m", "migrate"]
    assert by_name["web"]["volumes"][0]["claim"] == by_name["api"]["volumes"][0]["claim"] == "data"
    assert {c["name"]: c["size"] for c in values["volumeClaims"]} == {"db-pgdata": "2Gi", "data": "5Gi"}
    assert [j["name"] for j in values["jobs"]] == ["web-post-start"]
    assert values["nodePort"] is True and values["ingress"]["enabled"] is False


def test_conflicting_shared_volume_sizes(tmp_path):
    spec = deep_merge(SPEC, {"services": {"web": {"k8s": {"volumes": {"data": {"size": "9Gi"}}}}}})
    stack = build_stack(spec, tmp_path, "minikube")
    with pytest.raises(SpecError, match="widersprüchliche"):
        K8sRunner(Project(tmp_path, {}), stack).values({"app": "x"})


def test_k8s_target_requires_registry(tmp_path):
    with pytest.raises(SpecError, match="registry"):
        K8sRunner(Project(tmp_path, {}), build_stack(SPEC, tmp_path, "k8s"))


@pytest.mark.skipif(not shutil.which("helm"), reason="helm nicht installiert")
def test_chart_renders(tmp_path):
    values_file = tmp_path / "values.yaml"
    values_file.write_text(yaml.safe_dump(_values(tmp_path)))
    result = subprocess.run(["helm", "template", "demo", str(CHART_DIR), "-f", str(values_file)],
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    kinds = [(d["kind"], d["metadata"]["name"]) for d in yaml.safe_load_all(result.stdout) if d]
    assert ("Deployment", "web") in kinds and ("PersistentVolumeClaim", "data") in kinds
    assert ("Secret", "demo-env") in kinds and ("Secret", "demo-files") in kinds
    assert any(kind == "Job" for kind, _ in kinds)


def test_repo_status_lists_project_and_siblings(tmp_path, monkeypatch, capsys):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "pyproject.toml").write_text(dedent("""
        [tool.stackctl]
        spec_file = "stack.yaml"

        [[tool.stackctl.siblings]]
        name = "ki-core"
    """).strip() + "\n")
    (root / "stack.yaml").write_text("stack:\n  services:\n    api:\n      command: ['python', '-V']\n")

    sibling = tmp_path / "ki-core"
    sibling.mkdir()
    (root / ".git").mkdir()
    (sibling / ".git").mkdir()
    monkeypatch.chdir(root)

    calls = []

    def fake_run(command, cwd=None, capture_output=False, text=False):
        calls.append((tuple(command), cwd))
        if command[:2] != ["git", "fetch"] and cwd == root:
            outputs = {
                ("git", "rev-parse", "--abbrev-ref", "HEAD"): "devel\n",
                ("git", "rev-parse", "--short", "HEAD"): "abc1234\n",
                ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): "origin/devel\n",
                ("git", "rev-parse", "--short", "@{upstream}"): "fff9999\n",
                ("git", "rev-list", "--left-right", "--count", "HEAD...origin/devel"): "0 0\n",
                ("git", "status", "--short"): "",
            }
            return subprocess.CompletedProcess(command, 0, outputs[tuple(command)], "")
        if command[:2] != ["git", "fetch"] and cwd == sibling:
            outputs = {
                ("git", "rev-parse", "--abbrev-ref", "HEAD"): "main\n",
                ("git", "rev-parse", "--short", "HEAD"): "def5678\n",
                ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): "origin/main\n",
                ("git", "rev-parse", "--short", "@{upstream}"): "fff9999\n",
                ("git", "rev-list", "--left-right", "--count", "HEAD...origin/main"): "1 2\n",
                ("git", "status", "--short"): " M src/module.py\n",
            }
            return subprocess.CompletedProcess(command, 0, outputs[tuple(command)], "")
        if command[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected command: {command} cwd={cwd}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert main(["repo-status"]) == 0
    out = capsys.readouterr().out
    assert "+-" in out
    assert "| NAME" in out
    assert "AHEAD/BEHIND" in out
    assert "demo" in out
    assert "ki-core" in out
    assert "abc1234" in out
    assert "def5678" in out
    assert "origin/devel" in out
    assert "origin/main" in out
    assert "0/0" in out
    assert "1/2" in out
    assert "yes" in out
    assert "dirty" in out
    assert "ok" in out
    assert [cmd for cmd, cwd in calls if cmd[:2] == ("git", "fetch")] == [
        ("git", "fetch", "--quiet", "--all", "--prune"),
        ("git", "fetch", "--quiet", "--all", "--prune"),
    ]


def test_pull_updates_only_clean_repos_that_are_behind(tmp_path, monkeypatch, capsys):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "pyproject.toml").write_text(dedent("""
        [tool.stackctl]
        spec_file = "stack.yaml"

        [[tool.stackctl.siblings]]
        name = "ki-core"

        [[tool.stackctl.siblings]]
        name = "ki-web"
    """).strip() + "\n")
    (root / "stack.yaml").write_text("stack:\n  services:\n    api:\n      command: ['python', '-V']\n")
    sibling1 = tmp_path / "ki-core"
    sibling2 = tmp_path / "ki-web"
    sibling1.mkdir()
    sibling2.mkdir()
    for path in (root, sibling1, sibling2):
        (path / ".git").mkdir()
    monkeypatch.chdir(root)

    state = {
        root: {"branch": "devel", "head": "aaa1111", "upstream": "origin/devel", "remote": "bbb2222", "ahead": 0,
               "behind": 1, "dirty": False},
        sibling1: {"branch": "feature/x", "head": "ccc3333", "upstream": "origin/feature/x", "remote": "ccc3333",
                   "ahead": 0, "behind": 0, "dirty": False},
        sibling2: {"branch": "main", "head": "ddd4444", "upstream": "origin/main", "remote": "eee5555", "ahead": 0,
                   "behind": 2, "dirty": True},
    }
    pulls = []

    def fake_run(command, cwd=None, capture_output=False, text=False):
        repo = state[cwd]
        if command[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:3] == ["git", "pull", "--ff-only"]:
            pulls.append(cwd)
            repo["head"] = repo["remote"]
            repo["behind"] = 0
            return subprocess.CompletedProcess(command, 0, "updated\n", "")
        outputs = {
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): f"{repo['branch']}\n",
            ("git", "rev-parse", "--short", "HEAD"): f"{repo['head']}\n",
            ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): f"{repo['upstream']}\n",
            ("git", "rev-parse", "--short", "@{upstream}"): f"{repo['remote']}\n",
            ("git", "rev-list", "--left-right", "--count", f"HEAD...{repo['upstream']}"): f"{repo['ahead']} {repo['behind']}\n",
            ("git", "status", "--short"): " M changed.py\n" if repo["dirty"] else "",
        }
        return subprocess.CompletedProcess(command, 0, outputs[tuple(command)], "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert main(["pull"]) == 1
    out = capsys.readouterr().out
    assert pulls == [root]
    assert "demo: aktualisiert" in out
    assert "ki-core: aktuell" in out
    assert "ki-web: übersprungen (dirty)" in out
    assert "bbb2222" in out
    assert "ok" in out
    assert "dirty" in out


def test_repo_status_uses_optional_sibling_path(tmp_path, monkeypatch, capsys):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "pyproject.toml").write_text(dedent("""
        [tool.stackctl]
        spec_file = "stack.yaml"

        [[tool.stackctl.siblings]]
        name = "yaml-cfg-wizard"
        path = "yaml_cfg_wizard"
    """).strip() + "\n")
    (root / "stack.yaml").write_text("stack:\n  services:\n    api:\n      command: ['python', '-V']\n")
    sibling = tmp_path / "yaml_cfg_wizard"
    sibling.mkdir()
    (root / ".git").mkdir()
    (sibling / ".git").mkdir()
    monkeypatch.chdir(root)

    def fake_run(command, cwd=None, capture_output=False, text=False):
        if command[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        outputs = {
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): "main\n",
            ("git", "rev-parse", "--short", "HEAD"): "abc1234\n",
            ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): "origin/main\n",
            ("git", "rev-parse", "--short", "@{upstream}"): "def5678\n",
            ("git", "rev-list", "--left-right", "--count", "HEAD...origin/main"): "0 0\n",
            ("git", "status", "--short"): "",
        }
        return subprocess.CompletedProcess(command, 0, outputs[tuple(command)], "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert main(["repo-status"]) == 0
    out = capsys.readouterr().out
    assert "yaml-cfg-wizard" in out
    assert str(sibling) in out


def test_push_updates_only_clean_repos_that_are_ahead(tmp_path, monkeypatch, capsys):
    root = tmp_path / "demo"
    root.mkdir()
    (root / "pyproject.toml").write_text(dedent("""
        [tool.stackctl]
        spec_file = "stack.yaml"

        [[tool.stackctl.siblings]]
        name = "ki-core"

        [[tool.stackctl.siblings]]
        name = "ki-web"
    """).strip() + "\n")
    (root / "stack.yaml").write_text("stack:\n  services:\n    api:\n      command: ['python', '-V']\n")
    sibling1 = tmp_path / "ki-core"
    sibling2 = tmp_path / "ki-web"
    sibling1.mkdir()
    sibling2.mkdir()
    for path in (root, sibling1, sibling2):
        (path / ".git").mkdir()
    monkeypatch.chdir(root)

    state = {
        root: {"branch": "devel", "head": "aaa1111", "upstream": "origin/devel", "remote": "aaa1111", "ahead": 2,
               "behind": 0, "dirty": False},
        sibling1: {"branch": "feature/x", "head": "ccc3333", "upstream": "origin/feature/x", "remote": "ddd4444",
                   "ahead": 1, "behind": 1, "dirty": False},
        sibling2: {"branch": "main", "head": "eee5555", "upstream": "origin/main", "remote": "eee5555", "ahead": 0,
                   "behind": 0, "dirty": False},
    }
    pushes = []

    def fake_run(command, cwd=None, capture_output=False, text=False):
        repo = state[cwd]
        if command[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(command, 0, "", "")
        if command[:2] == ["git", "push"]:
            pushes.append(cwd)
            repo["ahead"] = 0
            return subprocess.CompletedProcess(command, 0, "pushed\n", "")
        outputs = {
            ("git", "rev-parse", "--abbrev-ref", "HEAD"): f"{repo['branch']}\n",
            ("git", "rev-parse", "--short", "HEAD"): f"{repo['head']}\n",
            ("git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}"): f"{repo['upstream']}\n",
            ("git", "rev-parse", "--short", "@{upstream}"): f"{repo['remote']}\n",
            ("git", "rev-list", "--left-right", "--count", f"HEAD...{repo['upstream']}"): f"{repo['ahead']} {repo['behind']}\n",
            ("git", "status", "--short"): "",
        }
        return subprocess.CompletedProcess(command, 0, outputs[tuple(command)], "")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert main(["push"]) == 1
    out = capsys.readouterr().out
    assert pushes == [root]
    assert "demo: gepusht" in out
    assert "ki-core: übersprungen (1 hinterher)" in out
    assert "ki-web: nichts zu pushen" in out
    assert "ok" in out
    assert "behind" in out
