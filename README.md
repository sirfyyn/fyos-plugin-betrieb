English: [README.en.md](README.en.md)

# fyos-plugin-betrieb

Die Betriebs-Plugins von FYOS als Paket für den Hub (`fyos-hub`). Dieses Repo ist die **Basis** der weiteren
Plugin-Repos: `fyos_plugin_betrieb.lage` trägt die gemeinsamen Hilfen, die die anderen Pakete importieren.

**Nichts ist vorbelegt.** Das Paket kennt keine Adresse einer bestimmten Installation. Ist eine Einstellung nicht
gesetzt, ist die Fähigkeit „nicht eingerichtet“: ein lesendes Werkzeug antwortet mit dem eigenen Zustand
(`zustand: "nicht eingerichtet"`, `eingerichtet: false`, `satz`, `einrichten` mit dem Weg), ein schreibendes mit
`ok: false` und demselben Hinweis, `health` meldet `status: "nicht eingerichtet"`. Allgemeine Vorgaben (Namespace
`fyos`, Port, Zweigvorsatz `agent`) bleiben und stehen in der Tabelle.

## Inhalt

Plugins (Entry-Point-Gruppe `mcp_hub.plugins`, Name = `name` der Klasse, damit die Werkzeug-IDs gleich bleiben):

| Plugin | Modul | Zweck |
|---|---|---|
| `lage` | `lage.py` | Lagebild: Ampel, Kennzahlen (Prometheus), Fristen, Chronik, Suche, Dev-/Wartungsfenster |
| `cluster` | `cluster.py` | Steuerpult für k3s (App `ui://fyos/steuerpult.html`): Knoten, Workloads, Flux, Logs, gezielte Eingriffe |
| `repos` | `repos.py` | App „Repos“ (`ui://fyos/repos.html`): je Repo Standardzweig, letzter Commit, CI-Ampel, offene PRs, Paket- und Bildversion; im Detail Zweige, Commits, Läufe, PRs, Pakete. Nur lesend |
| `k8s` | `k8s_core.py` | Kubernetes-Kern: Pods, Deployments, Ereignisse, Exec |
| `betrieb` | `betrieb.py` (+ `_ci_fehler.py`, `betroffen.py`, `daten/prozesskarte-lanes.beispiel.yaml`) | Prozesskarte, Betroffenheit, CI-Fehler |
| `arbeit` | `arbeit.py` | Arbeitsaufträge als Jobs, Gitea-gestützt |
| `git` | `git_unified.py` | Git über mehrere Remotes |
| `truenas`, `omada`, `kerberuz` | `*_proxy.py` | Proxys zu den Sub-MCP-Servern (Speicher, Netz, Daemon) |
| `pipelines`, `grow` | `pipelines.py`, `grow.py` | Pipelines- und Grow-Zustand (lesend) |
| `repomix`, `cli_anything` | `repomix.py`, `cli_anything.py` | Sub-MCP-Plugins über Programme auf dem Pfad |
| `fle` | `fle_apps.py` | Messdaten der FLE-Apps |
| `graph` | `graph.py`, `betriebsgraph.py`, `graph_einspeisen/` | Betriebsgraph in Postgres: was hängt an X, was fällt aus, wie hängen A und B zusammen, was hat sich geändert (`graph.*`). Der Einspeiser `fyos-graph-einspeisen` füllt ihn, siehe „Betriebsgraph einrichten“ |
| (Hilfe) | `json5.py` | kleiner JSON5-Leser, kein Plugin |

Dazu: die Apps `ui/steuerpult.html` und `ui/repos.html` (melden sich beim Import von `cluster.py` bzw. `repos.py` über
`_identitaet.registriere_app` an),
zwei Skills (`fyos-git-remotes`, `fyos-rollout-verifikation`, über `fyos.skills`), ein Katalogeintrag
(`fyos-katalog.yaml`, `fyos.katalog`), Bilder, die Vorlage `daten/rechner.beispiel.yaml` (Quelle `rechner` des
Betriebsgraphen) und die Beispiel-Manifeste für den Einspeiser unter `deploy/graph/`.

`repos` merkt sich Antworten: die Übersicht 60 s, ein Detail 30 s, Paket- und Bildversionen 5 min (die Paketsuche ist
der langsamste Gitea-Abruf). Hat der letzte Commit keinen CI-Status, kommt die Ampel aus den neuesten Läufen auf dem
Standardzweig.

### Gemeinsame Hilfen aus `lage`

Andere Plugin-Repos importieren sie unverändert: `from fyos_plugin_betrieb import lage` bzw.
`from fyos_plugin_betrieb.lage import _Zwischenspeicher, _redis, _iso, PANE_AUTH, dienst, DIENSTE, gitea, REPO,
GITEA_API, prometheus, fenster_offen, sperrgrund, …`. `tests/test_paket.py` sichert die Namen.

## Abhängigkeiten

`fyos-hub` (ab 0.1.1.dev0, für die Vorgangs-Quelle `mcp_hub.vorgaenge`), `kubernetes>=30`, `pyyaml`, `httpx`, `redis`.
Keine Abhängigkeit zu anderen Plugin-Paketen
(`lage.py` erwähnt `leitstand.sammel` nur in einem Kommentar, keine Rückkante).

Extra `graph`: `psycopg[binary]>=3.1`, der Postgres-Treiber für das Plugin `graph` und den Einspeiser
(`pip install "fyos-plugin-betrieb[graph]"`; psycopg2 geht als Ersatz, wenn psycopg 3 fehlt). Ohne den Treiber lädt das
Plugin trotzdem: ohne Adresse meldet es sich als „aus“, mit Adresse als ungesund und nennt den fehlenden Treiber. Die
Tests brauchen ihn nicht (außer den Datenbank-Tests, siehe „Tests und Rückwege“).

## Konfiguration (Umgebung des Hubs)

Alle Einstellungen werden bei jedem Aufruf gelesen. „leer“ heißt: nicht eingerichtet.

| Variable | Vorgabe | Wirkung |
|---|---|---|
| `REDIS_URL` (geheim) | leer | Zwischenspeicher, Fenster, Ströme; ohne sie ist alles, was Redis braucht, nicht eingerichtet |
| `PROMETHEUS_URL` | leer | Kennzahlen im Lagebild und im Steuerpult |
| `GITEA_API_BASE`, `GITEA_REPO` | leer | Adresse der Git-API (mit `/api/v1`) und Hauptrepository `besitzer/name`: Chronik, Pipelines, Arbeit, CI-Fehler |
| `GITEA_TOKEN` (geheim) | leer | Zugriff auf die Git-API |
| `GITEA_PAKET_TOKEN` (geheim) | leer, dann `GITEA_TOKEN` | Paketversionen in `repos` (braucht `read:package`) |
| `FYOS_REPOS_BESITZER` | Besitzer aus `GITEA_REPO` | wessen Repos `repos` zeigt |
| `FYOS_AGENTDIENST_URL` | leer | Adresse des Agenten-Dienstes mit der Schnittstelle `/nexus/*` (`lage.dienst("agent")`: Anwesenheit, Chronik). Nicht `FYOS_AGENT_URL`: das ist das OpenClaw-Gateway, mit dem der Kern die Kopplung einschaltet |
| `FYOS_AGENTDIENST_TOKEN` (geheim) | leer | Bearer für den Agenten-Dienst; das Gateway-Token `FYOS_AGENT_TOKEN` geht nie dorthin |
| `FYOS_AUFGABEN_URL` | leer | Adresse des Aufgaben-Dienstes (`lage.dienst("aufgaben")`, Plugin `pipelines`) |
| `FYOS_EINSICHTEN_URL` | leer | Adresse des Einsichten-Dienstes (`lage.dienst("einsichten")`, Suche in der Chronik) |
| `FYOS_PANE_AUTH` (geheim) | leer | Wert der Kopfzeile `X-FYOS-Pane-Auth` für die internen Dienste; leer = ohne Kopfzeile |
| `GROW_URL` | leer | Adresse des Dienstes für das Plugin `grow` |
| `WIKI_API_BASE` | leer | Adresse der Wiki-API für `betrieb.notfallwege` |
| `FYOS_VORGANG_LINK` | leer | Link zu einem Vorgang in `lage.suche`, `{id}` wird ersetzt (z. B. `/vorgaenge?ref={id}`). Die Vorgänge selbst kommen über die Vorgangs-Quelle des Hubs: Ticketdienst (`FYOS_VORGAENGE_URL` oder von `openclaw` angemeldet) oder Workboard des Agenten |
| `FRIST_CADDY` | leer | Adresse des Caddy-Dienstes, über den das Zertifikat geprüft wird; leer = die Hosts aus `FRIST_ZERT_HOSTS` direkt |
| `FRIST_ZERT_HOSTS` | leer | kommagetrennt: Hosts, deren Zertifikatsfrist geprüft wird; leer = nicht eingerichtet |
| `FRIST_BACKUP_LABEL` | `fyos/component=backup` | Label der Backup-CronJobs (allgemeine Konvention) |
| `FYOS_FENSTER_KNOTEN` | leer | kommagetrennt: Knoten, für die ein Dev-/Wartungsfenster gesetzt werden kann, der erste ist der Standardknoten; leer = `lage.fenster_setzen` ist nicht eingerichtet |
| `FYOS_REPO_PATH` | leer | Repo-Klon für Skripte (Fenster, Git, Aussetzungsgründe); leer = nicht eingerichtet |
| `BETRIEB_NAMESPACE`, `ARBEIT_NAMESPACE` | `fyos` | Namespace der Backups bzw. der Prüf-Jobs (allgemeine Vorgabe) |
| `FYOS_BETRIEB_AUTOMATISMEN_CONFIGMAP`, `FYOS_BETRIEB_WERKZEUGE_CONFIGMAP` | leer | ConfigMaps mit dem Automatismen- bzw. Werkzeugkatalog (`betrieb.prozesskarte`, `betrieb.werkzeuge`); leer = der Katalog aus `FYOS_REPO_PATH` |
| `FYOS_BETRIEB_PFADPRAEFIXE` | leer | kommagetrennt: absolute Pfade, unter denen der Katalog den Repo-Klon nennt (zusätzlich zu `FYOS_REPO_PATH`) |
| `FYOS_PROZESSKARTE_LANES` | die Vorlage `daten/prozesskarte-lanes.beispiel.yaml` | Datei mit den Ketten der Prozesskarte |
| `FYOS_ARBEIT_WORKFLOW`, `FYOS_ARBEIT_WORKFLOW_NAME` | leer | Dateiname und Anzeigename des Test-Workflows, dessen rote Tests `arbeit.kandidaten` liefert |
| `FYOS_ARBEIT_BILD` | leer | Bild des Prüf-Jobs (mit git und pytest); alternativ `image` in der Konfiguration des Plugins |
| `FYOS_ARBEIT_TOKEN_SECRET` | leer | Name des Secrets mit `GITEA_USER` und `GITEA_TOKEN` für den Klon im Prüf-Job |
| `FYOS_ARBEIT_PULL_SECRETS`, `FYOS_ARBEIT_MEIDE_KNOTEN` | leer | kommagetrennt: Pull-Secrets des Bildes, Knoten, die der Prüf-Job meidet |
| `FYOS_ARBEIT_ZWEIG_VORSATZ`, `FYOS_ARBEIT_TESTWURZEL`, `FYOS_ARBEIT_PYTHONPATH` | `agent`, `tests`, `.` | erlaubte Zweige `<vorsatz>/<name>`, Ordner der Tests, Suchpfad des Prüf-Jobs |
| `FYOS_CLUSTER_MESSKNOTEN` | leer | Knoten, auf dem Messungen laufen; das Steuerpult sperrt CronJobs, die gegen ihn arbeiten, während sein Fenster offen ist |
| `FYOS_CLUSTER_MESSSPUREN` | leer | kommagetrennt: Namen, Adressen oder Ports, an denen ein CronJob „gegen den Messknoten arbeitet“ |
| `FYOS_CLUSTER_NIE_ABSPERREN` | leer | kommagetrennt: Knoten, die das Steuerpult nie absperrt (Knoten der Steuerebene sind immer geschützt) |
| `FYOS_FLE_DATEN` | leer | Verzeichnis mit `messungen/*.json` für `fle` |
| `FYOS_OMADA_URL`, `FYOS_TRUENAS_URL`, `FYOS_KERBERUZ_URL` | leer | Adressen der Sub-MCP-Server (oder `server_url` in der Konfiguration des Plugins) |
| `GRAPH_DB_URL` (geheim) | leer | Adresse der Graph-Datenbank (`postgresql://…`, Konto `fyos_graph_lesen`) für das Plugin `graph`; leer = kein Betriebsgraph eingerichtet, das Plugin meldet sich als „aus“ (anderer Name über `config.url_env`, Frist je Abfrage über `config.anweisung_s`, Vorgabe 20 s) |
| `KUBECONFIG` | im Cluster: In-Cluster-Konto | Zugang zum Cluster |

`hub-config.beispiel.yaml` zeigt die `config:`-Einträge einer Installation (Platzhalter-Adressen, ohne Geheimnisse, nur Namen).

### Hinweise

Die Dienstnamen in `lage.dienst(...)` sind `agent`, `aufgaben` und `einsichten`. Es gibt keine Alias-Namen für
Einstellungen: nur die Namen aus der Tabelle oben werden gelesen. Die Prozesskarte liegt nicht im Paket: wer eine eigene
Datei nutzt, legt sie selbst ab und setzt `FYOS_PROZESSKARTE_LANES`.

## Betriebsgraph einrichten

Der Betriebsgraph beantwortet vier Fragen aus echten Abläufen heraus: Was hängt an X? Was fällt aus, wenn X ausfällt?
Wie hängen A und B zusammen? Was hat sich an X geändert? Er liegt in Postgres (zwei Tabellen und `WITH RECURSIVE`, keine
Graph-Datenbank), wird vom Einspeiser alle 30 Minuten aus den Betriebsquellen gespeist und von den Hub-Werkzeugen
`graph.abhaengig`, `graph.auswirkung`, `graph.pfad`, `graph.verlauf` und `graph.zustand` gelesen.

| Teil | Wo |
|---|---|
| Logik (Schema, Abgleich, Abfragen) | `betriebsgraph.py` |
| Hub-Plugin `graph` | `graph.py` (liest nur; schreibt je Aufruf einen Zählvermerk in `nutzung`) |
| Einspeiser | `graph_einspeisen/` mit den Quellen unter `graph_einspeisen/quellen/`; Konsolenskript `fyos-graph-einspeisen` |
| Datenbank einrichten | `fyos-graph-einspeisen einrichten` (`graph_einspeisen/db_einrichten.py`) |
| Beispiele für Kubernetes | `deploy/graph/` (CronJob, Leserechte, Netzregeln) |

### 1. Postgres und Rollen

Gebraucht wird ein Postgres, in dem du Rollen und eine Datenbank anlegen darfst (ein Superuser genügt). Der Aufruf legt
die Datenbank `fyos_graph` mit zwei Rollen an und ist wiederholbar: `fyos_graph` (Eigentümer, für den Einspeiser) und
`fyos_graph_lesen` (nur lesen, dazu Zählvermerke für das Plugin), sperrt die Datenbank für alle anderen und legt das
Schema an. Die Passwörter setzt jeder Lauf neu; Adresse und Passwörter kommen aus der Umgebung, nie aus Argumenten, und
werden nie ausgegeben.

```
export GRAPH_ADMIN_URL='postgresql://<verwalter>:<passwort>@<postgres-host>:5432/postgres'
export GRAPH_EINSPEISER_PASSWORT="$(openssl rand -hex 24)"
export GRAPH_LESER_PASSWORT="$(openssl rand -hex 24)"
fyos-graph-einspeisen einrichten              # anlegen oder angleichen
fyos-graph-einspeisen einrichten --pruefen    # nur prüfen: Rollen, Datenbank, Schemaversion
```

### 2. Secret mit den Adressen

Das Secret legt der Betreiber selbst an; es gehört in keine Datei des Repos. Ein Schlüssel je Konto, im selben Lauf wie
Schritt 1 (die Passwörter stehen noch in der Umgebung):

```
kubectl -n <NAMESPACE> create secret generic <GRAPH_DB_SECRET> \
  --from-literal=url_einspeiser="postgresql://fyos_graph:${GRAPH_EINSPEISER_PASSWORT}@<postgres-host>:5432/fyos_graph" \
  --from-literal=url_lesen="postgresql://fyos_graph_lesen:${GRAPH_LESER_PASSWORT}@<postgres-host>:5432/fyos_graph"
```

Wer Passwörter mit Sonderzeichen wählt, kodiert sie in der Adresse prozentweise (`@` wird `%40`); die hexadezimalen
Passwörter oben brauchen das nicht. Wer Geheimnisse versiegelt (Sealed Secrets, SOPS), erzeugt aus diesem Secret das
versiegelte Objekt.

### 3. Hub: Plugin einschalten

Das Plugin lädt von selbst, sobald das Paket mit dem Extra `graph` im Hub-Bild liegt. Es braucht nur die Adresse des
lesenden Kontos in der Umgebung des Hubs:

```yaml
- name: GRAPH_DB_URL
  valueFrom:
    secretKeyRef: {name: <GRAPH_DB_SECRET>, key: url_lesen}
```

Ohne `GRAPH_DB_URL` meldet sich das Plugin als „aus“ (weder gesund noch krank: es ist nicht eingerichtet); wer keinen
Betriebsgraphen betreibt, schaltet es in der Hub-Konfiguration mit `enabled: false` ab. Ist die Adresse gesetzt und die
Datenbank nicht erreichbar, ist es ungesund (zu sehen im Gesundheitsbericht des Hubs).

### 4. Einspeiser: CronJob

`deploy/graph/` enthält die Beispiel-Manifeste. Vor dem Anwenden werden die Platzhalter ersetzt:

| Platzhalter | Bedeutung |
|---|---|
| `<NAMESPACE>` | Namensraum des Einspeisers (dort liegt auch das Secret) |
| `<IMAGE>` | ein Bild mit installiertem `fyos-plugin-betrieb[graph]`, zum Beispiel das des Hubs; der Befehl `fyos-graph-einspeisen` muss darin liegen |
| `<GRAPH_DB_SECRET>` | Name des Secrets aus Schritt 2 |
| `<POSTGRES_APP>` | Wert des Labels `app` an den Pods des Postgres (nur `networkpolicy.yaml`) |
| `<HUB_APP>` | Wert des Labels `app` an den Pods des Hubs (nur `networkpolicy.yaml`) |

```
kubectl apply -k deploy/graph       # rbac.yaml, einspeisen-cronjob.yaml, networkpolicy.yaml
```

`networkpolicy.yaml` braucht nur, wo NetworkPolicies den Verkehr sperren; sonst aus `kustomization.yaml` herausnehmen. Die
Leserechte (`rbac.yaml`) sind nur `get` und `list`, **keine Secrets**; der Einspeiser schreibt nur in die Graph-Datenbank
(und, wenn eingeschaltet, Kommentare an Vorgänge, siehe `itsm`).

### Quellen

Jede Quelle ist ein Modul unter `graph_einspeisen/quellen/`. Eine Quelle, die nichts findet, was sie lesen könnte, ist
**aus**: kein Fehler, kein Eintrag in `laeufe`, ihr alter Stand bleibt. Ist etwas eingerichtet und nicht lesbar, scheitert
sie laut; die anderen laufen weiter.

| Quelle | Liest | Einrichten (Umgebung des Einspeisers) | Ohne Einrichtung |
|---|---|---|---|
| `k8s` | Knoten, Deployments, StatefulSets, DaemonSets, CronJobs, Services, PVC/PV, Bilder; Adressen in Umgebung und ConfigMaps (nur Namen, nie Werte; Secrets nie) | Zugang zur Kubernetes-API: im Pod der ServiceAccount, sonst `KUBECONFIG` | scheitert laut |
| `flux` | Kustomizations, HelmReleases, Bildautomatik (wer rollt was aus) | wie `k8s` | aus, wenn Flux nicht installiert ist |
| `caddy` | Routen aus dem Caddyfile zu den Diensten dahinter | `GRAPH_CADDYFILE` (Datei oder ConfigMap-Manifest) oder `GRAPH_CADDY_CONFIGMAP=<namespace>/<name>` (+ `GRAPH_CADDY_SCHLUESSEL`, Vorgabe `Caddyfile`); `GRAPH_CADDY_DIENST` (Graph-ID des Caddy-Dienstes), `GRAPH_CADDY_AUTH` (Graph-ID des Anmeldedienstes), `GRAPH_CADDY_AUTH_SNIPPETS` (Vorgabe `authentik`) für die `braucht`-Kanten | aus |
| `hub` | Plugins des MCP-Hubs (Entry-Points der Umgebung des Einspeisers, am besten dasselbe Bild wie der Hub) und Dienstadressen in Plugin-Code und Konfiguration | optional: `GRAPH_HUB_CONFIG` (Datei) oder `GRAPH_HUB_CONFIGMAP=<namespace>/<name>` (+ `GRAPH_HUB_CONFIG_SCHLUESSEL`, Vorgabe `config.yaml`); `GRAPH_HUB_DIENST` (Graph-ID des Hub-Dienstes) | läuft immer |
| `bestand` | Werkzeuge, Skripte, Geräte, Seiten, Modelle aus der Momentaufnahme des Bestandsbuchs in Redis | `GRAPH_BESTAND_REDIS_URL` (+ `GRAPH_BESTAND_SCHLUESSEL`, Vorgabe `fyos:bestand:stand`) | aus |
| `rechner` | Dienste, Speicher, Artefakte und Profile auf Rechnern ohne Kubernetes, kuratiert aus einer YAML-Datei | `GRAPH_RECHNER_DATEN` (Pfad; Vorlage `daten/rechner.beispiel.yaml`) | aus |
| `experimente` | Steckbriefe der Experiment-App (Dateien `<id>.json`), verknüpft mit Artefakten und Profilen der Rechner-Datei | `EXPERIMENTE_DIR` | aus |
| `itsm` | offene Vorgänge und die Dienste, die sie betreffen (Textfund) | Vorgangs-Quelle des Hubs (`mcp_hub.vorgaenge`): `FYOS_VORGAENGE_URL` (+ `FYOS_VORGAENGE_KOPF`, `Name: Wert`) oder im Hub-Prozess das Workboard des Agenten | aus |

Dazu: `GRAPH_QUELLEN` (kommagetrennt, Vorgabe alle) wählt die Quellen eines Laufs, `GRAPH_TEXT_SPERRE` (kommagetrennt)
nimmt Namen aus den Textfunden heraus, die in jedem Text vorkommen.

**Rechner-Datei.** Nicht alles läuft in Kubernetes: ein systemd-Dienst auf einer Werkbank, ein Modellserver, ein NAS.
Ein Cluster-Objekt verrät davon nichts, und der Einspeiser liest Rechner nicht per ssh. Die Quelle `rechner` übernimmt
darum das Belegte aus einer Datei (Abschnitte `dienste`, `speicher`, `artefakte`, `profile`, `querverbindungen`, dazu
`adressen` und `extern` für die Quelle `caddy`); jeder Eintrag nennt einen `beleg`. `daten/rechner.beispiel.yaml` ist die
Vorlage mit erfundenen Namen und Adressen aus dem Beispielbereich. Die Datei kommt zum Beispiel aus einer ConfigMap in den
Pod des CronJobs (siehe die auskommentierten `volumes` in `einspeisen-cronjob.yaml`).

**Vorgänge.** Aus dem CronJob erreichbar ist nur ein Vorgangs-Dienst (`FYOS_VORGAENGE_URL`); das Workboard eines Agenten
lebt im Prozess des Hubs. Mit `GRAPH_ITSM_KOMMENTARE=1` hängt der Nachlauf der Quelle an junge INCs (höchstens drei Tage
alt, höchstens fünf je Lauf) einmalig einen Kommentar mit der Marke `[graph]` an: betroffene Dienste, was bei deren Ausfall
mitfällt, wer sie nutzt. Das ist das Einzige, was der Einspeiser außerhalb der Graph-Datenbank schreibt, darum ist es
standardmäßig aus. Der Akteur heißt `api` oder `GRAPH_ITSM_AKTEUR`.

### Einspeiser von Hand

```
fyos-graph-einspeisen --trocken              # eingerichtete Quellen lesen und zählen, nichts schreiben, keine Datenbank
fyos-graph-einspeisen --quellen k8s,flux     # nur diese Quellen (sonst GRAPH_QUELLEN, sonst alle)
fyos-graph-einspeisen                        # einspeisen (GRAPH_DB_URL = Konto fyos_graph)
fyos-graph-einspeisen --leeren rechner       # alle gültigen Zeilen einer abgeschalteten Quelle schließen
python -m fyos_plugin_betrieb.graph_einspeisen --trocken      # dasselbe ohne installiertes Konsolenskript
```

Rückgabewert: 0, wenn mindestens eine Quelle ok ist; 1, wenn alle scheitern oder aus sind oder die Datenbank fehlt;
2 bei falschem Aufruf. Der Einspeiser gibt nie Zugangsdaten aus. Pro Quelle erscheint eine JSON-Zeile mit den Zahlen.

### Aufbau des Graphen

| Tabelle | Inhalt |
|---|---|
| `knoten` | `id` (z. B. `dienst:fyos/qdrant`), `typ`, `name`, `attribute` (jsonb), `quelle`, `gueltig_ab`, `gueltig_bis` |
| `kanten` | `von`, `nach`, `art`, `gewicht`, `vertrauen`, `attribute`, `quelle`, `beleg`, `gueltig_ab`, `gueltig_bis` |
| `laeufe` | ein Eintrag je Quelle und Lauf: Beginn, Ende, ok, Zahlen (Knoten, Kanten, neu, zu), Fehler |
| `nutzung` | ein Zählvermerk je Werkzeugaufruf, damit sichtbar wird, ob der Graph gebraucht wird |
| `markierungen`, `schema_stand` | kleine Schlüssel-Wert-Ablage, Schemaversion |

**Historie statt Überschreiben.** Eine Zeile gilt, solange `gueltig_bis` leer ist. Ändert sich ein Knoten oder eine Kante
oder fällt sie weg, wird die Zeile geschlossen und eine neue angelegt; daraus kommt `graph.verlauf`. Gleicher Stand
erzeugt keine Zeile. **Schwundschutz:** bleibt mehr als die Hälfte der Knoten einer Quelle aus (ab 20 Knoten), gilt das als
Lesefehler und nichts wird geschlossen.

**Kantenrichtung: `von` braucht `nach`.** „Was hängt an qdrant“ folgt den Kanten rückwärts, „was braucht X“ vorwärts.
Tragende Arten geben einen Ausfall weiter: `laeuft_auf` (ODER-Gruppe, `attribute.pin` = `hart` oder `frei`), `waehlt`,
`routet_zu`, `mountet`, `bindet`, `liegt_auf`, `gehoert_zu`, `braucht`, `laedt`, `liegt_in` und das weiche `nutzt`.
Zusatzwissen gibt nichts weiter: `nutzt_image`, `verwaltet_von`, `betrifft` (Textfunde mit `vertrauen` unter 1),
`beschreibt`, `misst`, `erwaehnt`. `nutzt` entsteht aus Umgebungsvariablen und Konfiguration und sagt, dass A mit B
spricht, nicht ob A ohne B stehen bleibt: Nutzer erscheinen als „beeinträchtigt“, nicht als „ausgefallen“, es sei denn,
die Kante trägt `attribute.hart` (kuratiert).

### Eigene Quelle

Ein Modul unter `graph_einspeisen/quellen/` mit `NAME`, `sammeln(ctx)` (liest, schreibt nichts, liefert Knoten und
Kanten) und optional `nachlauf(ctx, graph)`; `Ausgeschaltet(grund)` aus `graph_einspeisen.kontext`, wenn nichts
eingerichtet ist. Der Name kommt in `QUELLEN` in `einspeisen.py` (ein Test prüft, dass beides übereinstimmt).

### Tests und Rückwege

Die Tests des Graphen laufen ohne Datenbank. Die Datenbank-Tests in `tests/test_betriebsgraph.py` werden übersprungen, bis
`GRAPH_TEST_DB_URL` auf ein echtes Postgres zeigt (`pip install -e ".[test,graph]"`); sie legen ein eigenes Schema an und
löschen es danach:

```
GRAPH_TEST_DB_URL='postgresql://<benutzer>:<passwort>@<host>:5432/<testdb>' python -P -m pytest tests/test_betriebsgraph.py -q
```

Rückwege: den CronJob mit `suspend: true` anhalten (der Graph bleibt lesbar, wird nur nicht frisch), das Plugin mit
`enabled: false` abschalten, die Zeilen einer abgeschalteten Quelle mit `--leeren <quelle>` schließen. Die Datenbank bleibt
bestehen; sie zu löschen ist eine eigene Entscheidung.

## Betrieb

```
uv pip install /pfad/zu/fyos-os/hub /pfad/zu/fyos-os/installer     # zuerst der Hub (ab 0.1.1.dev0)
uv pip install -e ".[test]"
python -P -m pytest tests -q -p no:cacheprovider
```

Die Tests laufen ohne Netz und Cluster. Ein Test (`test_echte_karte_aus_repo_katalog`) ist übersprungen, weil er einen
Automatismen-Katalog (`docs/automatismen-katalog.yaml`) im Repo-Klon braucht. GitHub Actions
(`.github/workflows/tests.yml`) führt die Tests gegen den Hub aus dem Quellbaum von `fyos-os` aus, dazu
`tools/sauber.py` (keine privaten Adressen, Konten oder Geheimnisse im Baum und in der Historie) und gitleaks.

## Lizenz

Apache License, Version 2.0 (SPDX-Kennung `Apache-2.0`). Copyright 2026 sirfyyn and FYOS CONTRIBUTORS <!-- sauber: ok -->

Der vollständige Lizenztext steht in [`LICENSE`](LICENSE), der Urheberhinweis in [`NOTICE`](NOTICE).
