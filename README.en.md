Deutsch: [README.md](README.md)

# fyos-plugin-betrieb

The operations plugins of **FYOS — For Your Own Sake**, packaged for the hub (`fyos-hub`). This repo is the **base** of
the further plugin repos: `fyos_plugin_betrieb.lage` carries the shared helpers the other packages import.

**Nothing is preset.** The package knows no address of any particular installation. If a setting is not set, the
capability is "not set up" (`nicht eingerichtet`): a read tool answers with its own state (`zustand: "nicht eingerichtet"`,
`eingerichtet: false`, `satz`, `einrichten` with the way to set it up), a write tool with `ok: false` and the same hint,
and `health` reports `status: "nicht eingerichtet"`. General defaults (namespace `fyos`, port, branch prefix `agent`) stay
and are listed in the table. The plugins' own status texts are German.

## Contents

Plugins (entry-point group `mcp_hub.plugins`, name = `name` of the class, so the tool IDs stay the same):

| Plugin | Module | Purpose |
|---|---|---|
| `lage` | `lage.py` | situation picture: traffic light, key figures (Prometheus), deadlines, chronicle, search, dev/maintenance windows |
| `cluster` | `cluster.py` | control desk for k3s (app `ui://fyos/steuerpult.html`): nodes, workloads, Flux, logs, targeted interventions |
| `repos` | `repos.py` | "Repos" app (`ui://fyos/repos.html`): per repo default branch, last commit, CI light, open PRs, package and image version; in detail branches, commits, runs, PRs, packages. Read-only |
| `k8s` | `k8s_core.py` | Kubernetes core: pods, deployments, events, exec |
| `betrieb` | `betrieb.py` (+ `_ci_fehler.py`, `betroffen.py`, `daten/prozesskarte-lanes.beispiel.yaml`) | process map, impact analysis, CI failures |
| `arbeit` | `arbeit.py` | work orders as jobs, backed by the git server |
| `git` | `git_unified.py` | git across several remotes |
| `truenas`, `omada`, `kerberuz` | `*_proxy.py` | proxies to the sub-MCP servers (storage, network, daemon) |
| `pipelines`, `grow` | `pipelines.py`, `grow.py` | pipelines and grow state (read-only) |
| `repomix`, `cli_anything` | `repomix.py`, `cli_anything.py` | sub-MCP plugins around programs on the path |
| `fle` | `fle_apps.py` | measurement data of the FLE apps |
| `graph` | `graph.py`, `betriebsgraph.py`, `graph_einspeisen/` | operations graph in Postgres: what hangs on X, what fails, how A and B relate, what changed (`graph.*`). The feeder `fyos-graph-einspeisen` fills it, see "Setting up the operations graph" |
| (helper) | `json5.py` | small JSON5 reader, not a plugin |

Also: the apps `ui/steuerpult.html` and `ui/repos.html` (registered with `_identitaet.registriere_app` when `cluster.py`
or `repos.py` is imported), two skills (`fyos-git-remotes`, `fyos-rollout-verifikation`, via `fyos.skills`), a catalogue
entry (`fyos-katalog.yaml`, `fyos.katalog`), images, the template `daten/rechner.beispiel.yaml` (source `rechner` of the
operations graph) and example manifests for the feeder under `deploy/graph/`.

`repos` caches answers: the overview 60 s, a detail 30 s, package and image versions 5 min (the package search is the
slowest git-server call). If the last commit has no CI status, the light comes from the newest runs on the default branch.

### Shared helpers from `lage`

Other plugin repos import them unchanged: `from fyos_plugin_betrieb import lage` or
`from fyos_plugin_betrieb.lage import _Zwischenspeicher, _redis, _iso, PANE_AUTH, dienst, DIENSTE, gitea, REPO, GITEA_API,
prometheus, fenster_offen, sperrgrund, …`. `tests/test_paket.py` pins the names.

## Dependencies

`fyos-hub` (from 0.1.1.dev0, for the ticket source `mcp_hub.vorgaenge`), `kubernetes>=30`, `pyyaml`, `httpx`, `redis`.
No dependency on other plugin packages (`lage.py` mentions `leitstand.sammel` only in a comment, no back edge).

Extra `graph`: `psycopg[binary]>=3.1`, the Postgres driver for the `graph` plugin and the feeder
(`pip install "fyos-plugin-betrieb[graph]"`; psycopg2 works as a substitute if psycopg 3 is missing). Without the driver
the plugin still loads: without an address it reports "off", with an address it reports unhealthy and names the missing
driver. The tests do not need it (except the database tests, see "Tests and ways back").

## Configuration (environment of the hub)

All settings are read on every call. "empty" means: not set up.

| Variable | Default | Effect |
|---|---|---|
| `REDIS_URL` (secret) | empty | cache, windows, streams; without it everything that needs Redis is not set up |
| `PROMETHEUS_URL` | empty | key figures in the situation picture and the control desk |
| `GITEA_API_BASE`, `GITEA_REPO` | empty | address of the git API (with `/api/v1`) and main repository `owner/name`: chronicle, pipelines, work, CI failures |
| `GITEA_TOKEN` (secret) | empty | access to the git API |
| `GITEA_PAKET_TOKEN` (secret) | empty, then `GITEA_TOKEN` | package versions in `repos` (needs `read:package`) |
| `FYOS_REPOS_BESITZER` | owner from `GITEA_REPO` | whose repos `repos` shows |
| `FYOS_AGENTDIENST_URL` | empty | address of the agent service with the `/nexus/*` interface (`lage.dienst("agent")`: presence, chronicle). Not `FYOS_AGENT_URL`: that is the OpenClaw gateway the core uses for the coupling |
| `FYOS_AGENTDIENST_TOKEN` (secret) | empty | bearer for the agent service; the gateway token `FYOS_AGENT_TOKEN` never goes there |
| `FYOS_AUFGABEN_URL` | empty | address of the tasks service (`lage.dienst("aufgaben")`, plugin `pipelines`) |
| `FYOS_EINSICHTEN_URL` | empty | address of the insights service (`lage.dienst("einsichten")`, chronicle search) |
| `FYOS_PANE_AUTH` (secret) | empty | value of the header `X-FYOS-Pane-Auth` for the internal services; empty = no header |
| `GROW_URL` | empty | address of the service for the `grow` plugin |
| `WIKI_API_BASE` | empty | address of the wiki API for `betrieb.notfallwege` |
| `FYOS_VORGANG_LINK` | empty | link to a ticket in `lage.suche`, `{id}` is replaced (e.g. `/vorgaenge?ref={id}`). The tickets themselves come through the hub's ticket source: ticket service (`FYOS_VORGAENGE_URL` or registered by `openclaw`) or the agent's workboard |
| `FRIST_CADDY` | empty | address of the Caddy service used to check the certificate; empty = the hosts in `FRIST_ZERT_HOSTS` directly |
| `FRIST_ZERT_HOSTS` | empty | comma-separated: hosts whose certificate deadline is checked; empty = not set up |
| `FRIST_BACKUP_LABEL` | `fyos/component=backup` | label of the backup CronJobs (general convention) |
| `FYOS_FENSTER_KNOTEN` | empty | comma-separated: nodes a dev/maintenance window can be set for, the first is the default node; empty = `lage.fenster_setzen` is not set up |
| `FYOS_REPO_PATH` | empty | repo clone for scripts (windows, git, suspension reasons); empty = not set up |
| `BETRIEB_NAMESPACE`, `ARBEIT_NAMESPACE` | `fyos` | namespace of the backups and of the check jobs (general default) |
| `FYOS_BETRIEB_AUTOMATISMEN_CONFIGMAP`, `FYOS_BETRIEB_WERKZEUGE_CONFIGMAP` | empty | ConfigMaps with the automation or tool catalogue (`betrieb.prozesskarte`, `betrieb.werkzeuge`); empty = the catalogue from `FYOS_REPO_PATH` |
| `FYOS_BETRIEB_PFADPRAEFIXE` | empty | comma-separated: absolute paths under which the catalogue names the repo clone (in addition to `FYOS_REPO_PATH`) |
| `FYOS_PROZESSKARTE_LANES` | the template `daten/prozesskarte-lanes.beispiel.yaml` | file with the chains of the process map |
| `FYOS_ARBEIT_WORKFLOW`, `FYOS_ARBEIT_WORKFLOW_NAME` | empty | file name and display name of the test workflow whose red tests `arbeit.kandidaten` returns |
| `FYOS_ARBEIT_BILD` | empty | image of the check job (with git and pytest); alternatively `image` in the plugin configuration |
| `FYOS_ARBEIT_TOKEN_SECRET` | empty | name of the secret with `GITEA_USER` and `GITEA_TOKEN` for the clone in the check job |
| `FYOS_ARBEIT_PULL_SECRETS`, `FYOS_ARBEIT_MEIDE_KNOTEN` | empty | comma-separated: pull secrets of the image, nodes the check job avoids |
| `FYOS_ARBEIT_ZWEIG_VORSATZ`, `FYOS_ARBEIT_TESTWURZEL`, `FYOS_ARBEIT_PYTHONPATH` | `agent`, `tests`, `.` | allowed branches `<prefix>/<name>`, tests folder, search path of the check job |
| `FYOS_CLUSTER_MESSKNOTEN` | empty | node measurements run on; the control desk blocks CronJobs that work against it while its window is open |
| `FYOS_CLUSTER_MESSSPUREN` | empty | comma-separated: names, addresses or ports by which a CronJob "works against the measuring node" |
| `FYOS_CLUSTER_NIE_ABSPERREN` | empty | comma-separated: nodes the control desk never cordons (control-plane nodes are always protected) |
| `FYOS_FLE_DATEN` | empty | directory with `messungen/*.json` for `fle` |
| `FYOS_OMADA_URL`, `FYOS_TRUENAS_URL`, `FYOS_KERBERUZ_URL` | empty | addresses of the sub-MCP servers (or `server_url` in the plugin configuration) |
| `GRAPH_DB_URL` (secret) | empty | address of the graph database (`postgresql://…`, account `fyos_graph_lesen`) for the `graph` plugin; empty = no operations graph set up, the plugin reports "off" (other name via `config.url_env`, deadline per query via `config.anweisung_s`, default 20 s) |
| `KUBECONFIG` | in the cluster: in-cluster account | access to the cluster |

`hub-config.beispiel.yaml` shows the `config:` entries of an installation (placeholder addresses, no secrets, only names).

### Notes

The service names in `lage.dienst(...)` are `agent`, `aufgaben` and `einsichten`. There are no alias names for settings:
only the names in the table above are read. The process map is not part of the package: if you use a file of your own,
put it in place yourself and set `FYOS_PROZESSKARTE_LANES`.

## Setting up the operations graph

The operations graph answers four questions from real processes: What hangs on X? What fails if X fails? How do A and B
relate? What changed on X? It lives in Postgres (two tables and `WITH RECURSIVE`, no graph database), is fed by the feeder
every 30 minutes from the operations sources and read by the hub tools `graph.abhaengig`, `graph.auswirkung`, `graph.pfad`,
`graph.verlauf` and `graph.zustand`.

| Part | Where |
|---|---|
| Logic (schema, reconciliation, queries) | `betriebsgraph.py` |
| Hub plugin `graph` | `graph.py` (read-only; writes one count note per call into `nutzung`) |
| Feeder | `graph_einspeisen/` with the sources under `graph_einspeisen/quellen/`; console script `fyos-graph-einspeisen` |
| Set up the database | `fyos-graph-einspeisen einrichten` (`graph_einspeisen/db_einrichten.py`) |
| Kubernetes examples | `deploy/graph/` (CronJob, read rights, network rules) |

### 1. Postgres and roles

You need a Postgres where you may create roles and a database (a superuser is enough). The call creates the database
`fyos_graph` with two roles and can be repeated: `fyos_graph` (owner, for the feeder) and `fyos_graph_lesen` (read-only,
plus count notes for the plugin), locks the database for everyone else and creates the schema. Every run sets the
passwords anew; address and passwords come from the environment, never from arguments, and are never printed.

```
export GRAPH_ADMIN_URL='postgresql://<admin>:<password>@<postgres-host>:5432/postgres'
export GRAPH_EINSPEISER_PASSWORT="$(openssl rand -hex 24)"
export GRAPH_LESER_PASSWORT="$(openssl rand -hex 24)"
fyos-graph-einspeisen einrichten              # create or reconcile
fyos-graph-einspeisen einrichten --pruefen    # check only: roles, database, schema version
```

### 2. Secret with the addresses

The operator creates the secret; it belongs in no file of the repo. One key per account, in the same run as step 1 (the
passwords are still in the environment):

```
kubectl -n <NAMESPACE> create secret generic <GRAPH_DB_SECRET> \
  --from-literal=url_einspeiser="postgresql://fyos_graph:${GRAPH_EINSPEISER_PASSWORT}@<postgres-host>:5432/fyos_graph" \
  --from-literal=url_lesen="postgresql://fyos_graph_lesen:${GRAPH_LESER_PASSWORT}@<postgres-host>:5432/fyos_graph"
```

If you choose passwords with special characters, percent-encode them in the address (`@` becomes `%40`); the hexadecimal
passwords above do not need that. If you seal secrets (Sealed Secrets, SOPS), create the sealed object from this secret.

### 3. Hub: switch the plugin on

The plugin loads by itself as soon as the package with the extra `graph` is in the hub image. It only needs the address of
the reading account in the hub's environment:

```yaml
- name: GRAPH_DB_URL
  valueFrom:
    secretKeyRef: {name: <GRAPH_DB_SECRET>, key: url_lesen}
```

Without `GRAPH_DB_URL` the plugin reports "off" (neither healthy nor sick: it is not set up); if you run no operations
graph, switch it off in the hub configuration with `enabled: false`. If the address is set and the database is
unreachable, it is unhealthy (visible in the hub's health report).

### 4. Feeder: CronJob

`deploy/graph/` holds the example manifests. Replace the placeholders before applying:

| Placeholder | Meaning |
|---|---|
| `<NAMESPACE>` | namespace of the feeder (the secret lives there too) |
| `<IMAGE>` | an image with `fyos-plugin-betrieb[graph]` installed, for example the hub's; the command `fyos-graph-einspeisen` must be in it |
| `<GRAPH_DB_SECRET>` | name of the secret from step 2 |
| `<POSTGRES_APP>` | value of the label `app` on the Postgres pods (`networkpolicy.yaml` only) |
| `<HUB_APP>` | value of the label `app` on the hub pods (`networkpolicy.yaml` only) |

```
kubectl apply -k deploy/graph       # rbac.yaml, einspeisen-cronjob.yaml, networkpolicy.yaml
```

`networkpolicy.yaml` is only needed where network policies block traffic; otherwise take it out of `kustomization.yaml`.
The read rights (`rbac.yaml`) are only `get` and `list`, **no secrets**; the feeder writes only into the graph database
(and, if switched on, comments on tickets, see `itsm`).

### Sources

Every source is a module under `graph_einspeisen/quellen/`. A source that finds nothing it could read is **off**: no
error, no entry in `laeufe`, its old state stays. If something is set up and unreadable, it fails loudly; the others
keep running.

| Source | Reads | Set up (environment of the feeder) | Without setup |
|---|---|---|---|
| `k8s` | nodes, deployments, StatefulSets, DaemonSets, CronJobs, services, PVC/PV, images; addresses in environment and ConfigMaps (names only, never values; never secrets) | access to the Kubernetes API: the ServiceAccount in a pod, otherwise `KUBECONFIG` | fails loudly |
| `flux` | Kustomizations, HelmReleases, image automation (who rolls out what) | like `k8s` | off if Flux is not installed |
| `caddy` | routes from the Caddyfile to the services behind them | `GRAPH_CADDYFILE` (file or ConfigMap manifest) or `GRAPH_CADDY_CONFIGMAP=<namespace>/<name>` (+ `GRAPH_CADDY_SCHLUESSEL`, default `Caddyfile`); `GRAPH_CADDY_DIENST` (graph ID of the Caddy service), `GRAPH_CADDY_AUTH` (graph ID of the sign-in service), `GRAPH_CADDY_AUTH_SNIPPETS` (default `authentik`) for the `braucht` edges | off |
| `hub` | plugins of the MCP hub (entry points of the feeder's environment, best the same image as the hub) and service addresses in plugin code and configuration | optional: `GRAPH_HUB_CONFIG` (file) or `GRAPH_HUB_CONFIGMAP=<namespace>/<name>` (+ `GRAPH_HUB_CONFIG_SCHLUESSEL`, default `config.yaml`); `GRAPH_HUB_DIENST` (graph ID of the hub service) | always runs |
| `bestand` | tools, scripts, devices, pages, models from the inventory book's snapshot in Redis | `GRAPH_BESTAND_REDIS_URL` (+ `GRAPH_BESTAND_SCHLUESSEL`, default `fyos:bestand:stand`) | off |
| `rechner` | services, storage, artefacts and profiles on machines without Kubernetes, curated from a YAML file | `GRAPH_RECHNER_DATEN` (path; template `daten/rechner.beispiel.yaml`) | off |
| `experimente` | sheets of the experiment app (files `<id>.json`), linked to artefacts and profiles of the machine file | `EXPERIMENTE_DIR` | off |
| `itsm` | open tickets and the services they affect (text match) | the hub's ticket source (`mcp_hub.vorgaenge`): `FYOS_VORGAENGE_URL` (+ `FYOS_VORGAENGE_KOPF`, `Name: value`) or, inside the hub process, the agent's workboard | off |

Also: `GRAPH_QUELLEN` (comma-separated, default all) picks the sources of a run, `GRAPH_TEXT_SPERRE` (comma-separated)
removes names from the text matches that occur in every text.

**Machine file.** Not everything runs in Kubernetes: a systemd service on a workbench, a model server, a NAS. A cluster
object reveals nothing of that, and the feeder does not read machines over ssh. The `rechner` source therefore takes what
is documented from a file (sections `dienste`, `speicher`, `artefakte`, `profile`, `querverbindungen`, plus `adressen`
and `extern` for the `caddy` source); every entry names a `beleg` (evidence). `daten/rechner.beispiel.yaml` is the
template with invented names and addresses from the documentation range. The file reaches the CronJob's pod for example
from a ConfigMap (see the commented-out `volumes` in `einspeisen-cronjob.yaml`).

**Tickets.** Only a ticket service (`FYOS_VORGAENGE_URL`) is reachable from the CronJob; an agent's workboard lives in the
hub's process. With `GRAPH_ITSM_KOMMENTARE=1` the source's follow-up attaches to young incidents (at most three days old,
at most five per run) a single comment with the mark `[graph]`: affected services, what falls with them if they fail, who
uses them. That is the only thing the feeder writes outside the graph database, which is why it is off by default. The
actor is called `api` or `GRAPH_ITSM_AKTEUR`.

### Running the feeder by hand

```
fyos-graph-einspeisen --trocken              # read and count the set-up sources, write nothing, no database
fyos-graph-einspeisen --quellen k8s,flux     # only these sources (else GRAPH_QUELLEN, else all)
fyos-graph-einspeisen                        # feed (GRAPH_DB_URL = account fyos_graph)
fyos-graph-einspeisen --leeren rechner       # close all valid rows of a switched-off source
python -m fyos_plugin_betrieb.graph_einspeisen --trocken      # the same without the installed console script
```

Return value: 0 if at least one source is ok; 1 if all fail or are off or the database is missing; 2 for a wrong call.
The feeder never prints credentials. One JSON line with the numbers appears per source.

### Structure of the graph

| Table | Content |
|---|---|
| `knoten` | `id` (e.g. `dienst:fyos/qdrant`), `typ`, `name`, `attribute` (jsonb), `quelle`, `gueltig_ab`, `gueltig_bis` |
| `kanten` | `von`, `nach`, `art`, `gewicht`, `vertrauen`, `attribute`, `quelle`, `beleg`, `gueltig_ab`, `gueltig_bis` |
| `laeufe` | one entry per source and run: start, end, ok, numbers (nodes, edges, new, closed), error |
| `nutzung` | one count note per tool call, so it shows whether the graph is used |
| `markierungen`, `schema_stand` | small key-value store, schema version |

**History instead of overwriting.** A row is valid while `gueltig_bis` is empty. If a node or an edge changes or
disappears, the row is closed and a new one created; `graph.verlauf` comes from that. An identical state creates no row.
**Shrink guard:** if more than half of a source's nodes are missing (from 20 nodes up), that counts as a read error and
nothing is closed.

**Edge direction: `von` needs `nach`.** "What hangs on qdrant" follows the edges backwards, "what does X need" forwards.
Carrying kinds pass a failure on: `laeuft_auf` (OR group, `attribute.pin` = `hart` or `frei`), `waehlt`, `routet_zu`,
`mountet`, `bindet`, `liegt_auf`, `gehoert_zu`, `braucht`, `laedt`, `liegt_in` and the soft `nutzt`. Additional knowledge
passes nothing on: `nutzt_image`, `verwaltet_von`, `betrifft` (text matches with `vertrauen` below 1), `beschreibt`,
`misst`, `erwaehnt`. `nutzt` arises from environment variables and configuration and says that A talks to B, not whether
A stays up without B: users appear as "impaired", not "failed", unless the edge carries `attribute.hart` (curated).

### Your own source

A module under `graph_einspeisen/quellen/` with `NAME`, `sammeln(ctx)` (reads, writes nothing, returns nodes and edges)
and optionally `nachlauf(ctx, graph)`; `Ausgeschaltet(grund)` from `graph_einspeisen.kontext` if nothing is set up. The
name goes into `QUELLEN` in `einspeisen.py` (a test checks that both match).

### Tests and ways back

The graph's tests run without a database. The database tests in `tests/test_betriebsgraph.py` are skipped until
`GRAPH_TEST_DB_URL` points at a real Postgres (`pip install -e ".[test,graph]"`); they create a schema of their own and
delete it afterwards:

```
GRAPH_TEST_DB_URL='postgresql://<user>:<password>@<host>:5432/<testdb>' python -P -m pytest tests/test_betriebsgraph.py -q
```

Ways back: stop the CronJob with `suspend: true` (the graph stays readable, it just does not get fresh), switch the
plugin off with `enabled: false`, close the rows of a switched-off source with `--leeren <source>`. The database stays;
deleting it is a decision of its own.

## Running and testing

```
uv pip install /path/to/fyos-os/hub /path/to/fyos-os/installer     # the hub first (from 0.1.1.dev0)
uv pip install -e ".[test]"
python -P -m pytest tests -q -p no:cacheprovider
```

The tests run without network and cluster. One test (`test_echte_karte_aus_repo_katalog`) is skipped because it needs an
automation catalogue (`docs/automatismen-katalog.yaml`) in the repo clone. GitHub Actions
(`.github/workflows/tests.yml`) runs the tests against the hub from the source tree of `fyos-os`, plus
`tools/sauber.py` (no private addresses, accounts or secrets in the tree or the history) and gitleaks.

## License

Apache License, Version 2.0 (SPDX identifier `Apache-2.0`). Copyright 2026 sirfyyn and FYOS CONTRIBUTORS <!-- sauber: ok -->

The full text is in [`LICENSE`](LICENSE), the copyright notice in [`NOTICE`](NOTICE).
