"""stackctl [--target local|minikube|k8s] ACTION [SERVICE ...]

Actions: status, start, stop, restart, logs [-f], install, delete (k8s only), spec.
The target defaults to ``stack.target`` of the project config (set per stage).
"""

from __future__ import annotations

import argparse
import sys

import yaml

from .install import install
from .local import LocalRunner
from .repo_status import repo_pull, repo_status
from .spec import TARGETS, SpecError, build_stack, find_project, load_raw_spec

ACTIONS = ("status", "start", "stop", "restart", "logs", "install", "delete", "spec", "repo-status", "pull")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stackctl", description="Stack lokal, auf minikube oder k8s betreiben")
    parser.add_argument("action", choices=ACTIONS, nargs="?", default="status")
    parser.add_argument("services", nargs="*", help="Dienste (Standard: alle bzw. alle verwalteten)")
    parser.add_argument("-t", "--target", choices=TARGETS, help="Standard: stack.target aus der Konfiguration")
    parser.add_argument("-f", "--follow", action="store_true", help="logs: fortlaufend anzeigen")
    args = parser.parse_args(argv)
    try:
        project = find_project()
        if args.action == "install":
            return install(project)  # before the project (and its config loader) is installed
        if args.action == "repo-status":
            return repo_status(project)
        if args.action == "pull":
            return repo_pull(project)
        stack = build_stack(load_raw_spec(project), project.root, args.target)
        if args.action == "spec":
            print(yaml.safe_dump({"target": stack.target, "services": {n: vars(s) for n, s in stack.services.items()},
                                  "k8s": stack.k8s}, sort_keys=False, allow_unicode=True))
            return 0
        if stack.target == "local":
            runner = LocalRunner(project, stack)
            if args.action == "delete":
                raise SpecError("delete gibt es nur für minikube/k8s")
        else:
            from .k8s import K8sRunner

            runner = K8sRunner(project, stack)
        if args.action == "logs":
            return runner.logs(args.services, args.follow)
        code = getattr(runner, args.action)(args.services)
        if args.action in {"start", "stop", "restart"} and stack.target == "local":
            print()
            runner.status([])
        return code
    except SpecError as exc:
        print(f"stackctl: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
