# stackctl

Startet, stoppt und deployt kleine Service-Stacks, ohne an ein Projekt gebunden zu sein:

- als lokale Prozesse und docker compose,
- auf minikube,
- auf Kubernetes per Helm.

Der Stack wird **einmal** in der Projekt-Konfiguration beschrieben. Zielspezifische Abweichungen stehen als Overlays daneben. stackctl bringt ein generisches Helm-Chart mit, ein eigenes Chart pro Projekt ist nicht nötig.

```bash
stackctl status                    # Ziel aus stack.target, sonst "local"
stackctl start [dienst …]
stackctl stop | restart | logs [-f] dienst
stackctl -t minikube start         # Images bauen und laden, dann helm upgrade --install --wait
stackctl -t minikube status        # pods/svc/pvc + URLs
stackctl -t k8s start              # Images in die Registry pushen, dann deployen
stackctl -t minikube delete        # Release und Namespace entfernen (PVCs gehen mit dem Namespace)
stackctl install                   # Geschwister-Repos klonen + editable installieren, dann das Projekt
stackctl spec                      # aufgelöste Spec anzeigen
stackctl repo-status               # Hauptprojekt + siblings: Branch, Commits, ahead/behind, dirty
stackctl pull                      # zieht veraltete Repos auf ihrem aktuellen Branch per ff-only nach
stackctl push                      # pusht saubere Repos, die lokal voraus sind
```

Installation als User-Tool:

```bash
./scripts/install-user.sh
```

Das Skript legt eine eigene venv unter `~/.local/share/stackctl/venv` an, installiert `stackctl` per `pip install -e` und verlinkt das Executable nach `~/.local/bin/stackctl`. Abweichende Pfade lassen sich über `STACKCTL_VENV_DIR` und `STACKCTL_BIN_DIR` setzen.

## Einbindung ins Projekt (`pyproject.toml`)

```toml
[tool.stackctl]
config_loader = "ki_core:load_config"   # callable -> dict; läuft im Projekt-Root (Stages/Profile greifen)
config_key = "stack"                     # Abschnitt mit der Spec
# spec_file = "deploy/stack.yaml"       # alternativ: einfache YAML-Datei
# src_dir = ".."                        # wo Geschwister-Repos liegen (env STACKCTL_SRC_DIR gewinnt)
# extras = "dev"                         # pip-Extras für install (env STACKCTL_EXTRAS)

[[tool.stackctl.siblings]]
name = "ki-core"
url = "https://github.com/org/ki-core.git"
# branch = "devel"                # optional: nur für stackctl install / git clone

[[tool.stackctl.siblings]]
name = "yaml-cfg-wizard"          # Anzeigename / Repo-Gruppe
path = "yaml_cfg_wizard"          # Ordnername unter src_dir, falls abweichend
```

`stackctl repo-status` prüft das aktuelle Projekt und alle `siblings`. Dafür wird pro Repo `git fetch --all --prune` ausgeführt; die Ausgabe zeigt in einer Tabelle lokalen Branch, `HEAD`, Upstream, Remote-Commit, Ahead/Behind-Commits und ob das Worktree dirty ist. Ein positiver Behind-Wert zeigt, dass im Remote neue Commits vorhanden sind; ein positiver Ahead-Wert zeigt lokale Commits, die noch nicht gepusht wurden.

`stackctl pull` verwendet **nicht** einen fest konfigurierten Branch, sondern den **aktuell ausgecheckten Branch jedes Repos**. Repos mit `dirty`-Worktree oder ohne konfigurierten Upstream werden übersprungen. Für Updates nutzt stackctl `git pull --ff-only`, damit keine impliziten Merge-Commits entstehen.

`stackctl push` arbeitet ebenso auf dem **aktuell ausgecheckten Branch**. Gepusht werden nur Repos, die **clean** sind, einen **Upstream** haben und **lokal voraus** sind. Repos, die noch `behind` sind oder gleichzeitig vor und hinter dem Remote liegen, werden übersprungen.

Am besten liefert der `config_loader` die Spec aus dem gemeinsamen Konfigurationsbaum, bei ki-core über `config/defaults/stack.yaml`. Dann gelten Stages und ENV-Overrides auch für den Stack, z.B. `KI_CFG_STACK__SERVICES__DJANGO__PORT=8011`.

## Spec

```yaml
stack:
  name: demo
  target: local                  # Standardziel, z.B. per Stage setzen
  services:                      # Reihenfolge = Startreihenfolge
    db:
      kind: compose              # process | compose | external
      port: 5432
      compose: {dir: deploy/postgres, service: postgres, requires: [deploy/postgres/.env]}
      k8s:
        image: pgvector/pgvector:pg16
        secret_env: true         # envFrom: Secret <release>-env
        volumes: {pgdata: {mount: /var/lib/postgresql/data, size: 10Gi}}
    api:
      host: 0.0.0.0
      port: 8090
      health_path: /docs
      command: ["{python}", -m, uvicorn, app:app, --host, "{host}", --port, "{port}"]
      prepare: ["{python}", manage.py, migrate]   # lokal vor dem Start, im Cluster als initContainer
      post_start: ["{python}", seed.py]           # lokal nach dem Start, im Cluster als Job
      env: {DB_HOST: "{services[db].probe_host}"}
      marker: app:app            # stop beendet nur Prozesse, deren Kommandozeile das enthält
      k8s:
        expose: true             # minikube: NodePort, k8s: Ziel des Ingress
        volumes: {data: {mount: /data, shared: true, size: 20Gi}}   # shared = eine PVC für mehrere Dienste
  k8s:
    namespace: demo
    images: {app: {dockerfile: deploy/docker/Dockerfile}}   # Dienste ohne image nutzen "@app"
    env: {DATA_ROOT: /data}
    secret_env: {PG_USER: demo}             # Standardwerte …
    secret_env_files: [deploy/postgres/.env] # … von Dotenv-Dateien überschrieben
    secret_files: [creds.yaml]              # gemountet unter /app/<datei>
    minikube: {profile: demo, cpus: 4, memory: 8192, driver: docker}
    # registry: registry.example.com/team   # Pflicht für -t k8s
    # context: prod-cluster
    # ingress: {host: demo.example.com, class_name: nginx}   # service/port: Standard = erster expose-Dienst
  targets:                       # Overlay pro Ziel (deep merge, null entfernt Blöcke)
    minikube:
      services:
        api: {command: ["{python}", -m, uvicorn, app:app, --host, "{host}", --port, "{port}"]}
        ollama: {k8s: null}      # nicht im Cluster deployen
```

**Platzhalter:** `{target} {python} {root} {name} {host} {port} {probe_host}` sowie `{services[x].port}` und `{services[x].probe_host}`. Im Cluster gilt:

- `python` ist `python`,
- `root` ist `/app`,
- `host` ist `0.0.0.0`,
- `probe_host` ist der Dienstname (Cluster-DNS).

Geschweifte Klammern als Text schreibt man `{{ }}`. Kubernetes-Referenzen wie `$(VAR)` bleiben unverändert.

**Images:** Der Build-Kontext enthält nur die Dateien, die git sieht. Geschwister-Repos werden als `--build-context <name>=…` übergeben, im Dockerfile also `COPY --from=<name> . /src/<name>`. Der Tag ist die Image-ID, damit lösen neue Builds automatisch einen Rollout aus.

## Tests

```bash
python -m pytest -q tests
```
