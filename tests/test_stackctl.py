import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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
