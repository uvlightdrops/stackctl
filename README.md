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
stackctl clone                     # fehlende Geschwister-Repos klonen, ohne Installation
stackctl checkout devel            # Hauptprojekt + siblings auf den Branch devel umstellen
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
# branch = "devel"                # optional: beim Klonen durch stackctl clone / install

[[tool.stackctl.siblings]]
name = "yaml-cfg-wizard"          # Anzeigename / Repo-Gruppe
path = "yaml_cfg_wizard"          # Ordnername unter src_dir, falls abweichend
```

`stackctl clone` klont alle noch fehlenden `siblings` nach `src_dir` (bzw. `STACKCTL_SRC_DIR`), mit `path` oder sonst `name` als Ordnername. Fehlende Elternverzeichnisse werden angelegt. Die `url` ist nur für fehlende Repos erforderlich; ohne `branch` wird der Standard-Branch des Remotes verwendet. Vorhandene Git-Repos bleiben unverändert: kein Fetch, Pull oder Branchwechsel. Existierende Pfade ohne Git-Repo, fehlende URLs und Git-Fehler werden gemeldet; die übrigen Repos werden trotzdem bearbeitet, der Exit-Code ist dann ungleich null. Der Befehl braucht weder eine aktive venv noch eine ladbare Stack-Konfiguration und installiert keine Pakete. Aufruf im bereits vorhandenen Hauptprojekt mit `[tool.stackctl]` in der `pyproject.toml`.

`stackctl repo-status` prüft das aktuelle Projekt und alle `siblings`. Dafür wird pro Repo `git fetch --all --prune` ausgeführt; die Ausgabe zeigt in einer Tabelle lokalen Branch, `HEAD`, Upstream, Remote-Commit, Ahead/Behind-Commits und ob das Worktree dirty ist. Ein positiver Behind-Wert zeigt, dass im Remote neue Commits vorhanden sind; ein positiver Ahead-Wert zeigt lokale Commits, die noch nicht gepusht wurden.

`stackctl checkout <branch>` wechselt im Hauptprojekt und allen `siblings` auf den angegebenen Branch, unabhängig von `siblings.branch`. Vorher werden die Remote-Refs per Fetch aktualisiert. Existiert der Branch nur auf einem Remote, legt `git switch --guess` einen lokalen Tracking-Branch an; bei mehreren passenden Remotes gelten die Git-Regeln (insbesondere `checkout.defaultRemote`). Repos, die bereits auf dem gewünschten Branch sind, bleiben unverändert. Andere Repos mit lokalen Änderungen (auch ungetrackten Dateien) werden übersprungen; es gibt kein automatisches Stash, Erzwingen oder Zurücksetzen. Fehlende Repos, fehlende/mehrdeutige Branches und Git-Fehler erscheinen in der Ergebnistabelle; die übrigen Repos werden weiterbearbeitet und der Exit-Code ist bei Fehlern oder übersprungenen Repos ungleich null. Der Befehl benötigt keine ladbare Stack-Konfiguration und führt keinen Pull aus.

`stackctl pull` verwendet **nicht** einen fest konfigurierten Branch, sondern den **aktuell ausgecheckten Branch jedes Repos**. Repos mit `dirty`-Worktree oder ohne konfigurierten Upstream werden übersprungen. Für Updates nutzt stackctl `git pull --ff-only`, damit keine impliziten Merge-Commits entstehen.

`stackctl push` arbeitet ebenso auf dem **aktuell ausgecheckten Branch**. Gepusht werden nur Repos, die **clean** sind, einen **Upstream** haben und **lokal voraus** sind. Repos, die noch `behind` sind oder gleichzeitig vor und hinter dem Remote liegen, werden übersprungen.

`pull` und `push` zeigen ihre Ergebnisse zuerst als dreispaltige Tabelle (`REPOSITORY | STATUS | DETAILS`): aktualisiert/gepusht, bereits aktuell oder übersprungen mit Begründung. Danach folgt die ausführliche Repo-Status-Tabelle.

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

### Service-Abhängigkeiten (depends_on)

Dienste können Abhängigkeiten untereinander deklarieren. `stackctl` beachtet diese beim Starten und Stoppen:

```yaml
services:
  db:
    kind: compose
    port: 5432
    compose: {dir: deploy/postgres, service: postgres}

  cache:
    kind: compose
    port: 6379
    depends_on: []
    compose: {dir: deploy/redis, service: redis}

  api:
    host: 0.0.0.0
    port: 8090
    health_path: /docs
    depends_on: [db, cache]  # db und cache müssen zuerst starten
    command: ["{python}", -m, uvicorn, app:app, --host, "{host}", --port, "{port}"]
    env:
      DB_HOST: "{services[db].probe_host}"
      CACHE_HOST: "{services[cache].probe_host}"

  worker:
    port: 0
    depends_on: [api, cache]  # Nach api und cache starten
    command: ["{python}", -m, celery, worker]
    env:
      CELERY_BROKER_URL: "redis://{services[cache].probe_host}:{services[cache].port}/0"
```

**Regeln:**
- `depends_on` ist eine optionale Liste von Dienst-Namen
- `stackctl start` sortiert Dienste nach ihren Abhängigkeiten topologisch und startet sie in dieser Reihenfolge
- `stackctl stop` verwendet die **umgekehrte** Reihenfolge (abhängige zuerst, dann ihre Abhängigkeiten)
- Zirkuläre Abhängigkeiten werden erkannt und führen zu einem Fehler
- Ungültige Referenzen werden abgelehnt

### Health Checks und Startup-Verhalten

Pro Dienst lassen sich Gesundheitschecks konfigurieren:

```yaml
services:
  api:
    host: 0.0.0.0
    port: 8090
    health_path: /docs
    health_timeout: 5              # Timeout insgesamt in Sekunden (Standard: 5)
    health_retries: 10             # Maximale Versuche (Standard: 3)
    health_interval: 0.5           # Wartezeit zwischen Versuchen in Sekunden (Standard: 0.5)
    command: ["{python}", -m, uvicorn, app:app, --host, "{host}", --port, "{port}"]
```

**Verhalten:**
- Wenn `health_path` gesetzt ist, wartet stackctl nach dem Start auf einen HTTP-GET zum Service (Status < 500)
- Wenn `health_path` leer ist, wartet stackctl nur auf offene Port (TCP-Verbindung)
- `health_retries` und `health_interval` steuern den Wiederholungsablauf
- Nach `health_timeout` Sekunden bricht stackctl ab, falls der Service nicht antwortet
- Abhängige Services starten erst, wenn ihre Abhängigkeiten healthy sind

### Unified Log Streaming

```bash
stackctl logs                      # Logs aller verwalteten Dienste (Standard: tail -n 40)
stackctl logs -f                   # Follow: live-Modus, fortlaufend anzeigen
stackctl logs api worker           # Nur bestimmte Dienste
stackctl logs -f api               # Nur api, Follow-Modus
```

**Features:**
- Zeigt Logs mit Zeitstempel und Service-Namen-Präfix
- Jeder Dienst bekommt eine stabile Farbe (cyan, gelb, grün, magenta, blau, rot)
- Live-Modus `-f` folgt neuen Log-Einträgen in Echtzeit
- Logs nur für lokal gestartete Prozesse (nicht für `external` oder entfernte Services)
- Tastenkombi `Ctrl+C` stoppt die Log-Anzeige

Beispiel-Output:
```
[db          ] 14:23:45  2024-10-28T12:51:29.954+01:00 database initialized
[cache       ] 14:23:46  Connected to Redis on localhost:6379
[api         ] 14:23:47  Uvicorn running on http://0.0.0.0:8090
[worker      ] 14:23:47  [celery@hostname ready.]
```

Siehe auch `examples/stack-with-dependencies.yaml` für ein vollständiges Beispiel mit allen Features.

## Tests

```bash
python -m pytest -q tests
```
