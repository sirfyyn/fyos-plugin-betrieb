"""betrieb — Bau- und Betriebssicht als Hub-Werkzeuge (2026-09-30).

Ersetzt die frühere Routen `/api/fyos/{baukette,betrieb}` (Etappe 2b) und
`/api/fyos/{prozesskarte,betroffen,werkzeuge,wiki-wege}` (Etappe 4).

    betrieb.baukette      CI-Stand eines Commits (Vorgabe: HEAD von main) mit Einzeljobs
    betrieb.ci_laeufe     letzte Commits und Gitea-Actions-Laeufe mit Warteschlangen-Zahlen
    betrieb.ci_fehler     warum ist die CI zu einem Commit/Zweig rot? Logauszug je roter Job (_ci_fehler.py)
    betrieb.prozesskarte  alle Automatismen als Ketten (Katalog + kuratierte Lanes)
    betrieb.betroffen     was bricht, wenn X ausfaellt — Automatismus- oder Infra-Ebene
    betrieb.werkzeuge     Werkzeug-Register: Katalog + Lebenszeichen je Werkzeug
    betrieb.notfallwege   Notfall-Runbooks aus dem Wiki: Symptom -> Schritte -> Kommandos

Aenderungen gegenueber der frueheren Route:
- Gitea liefert den Gesamtstatus als `state`, Die frühere Route las `status` (immer leer).
- Der Cache beachtet `sha` (Die frühere Route lieferte fuer jeden sha den HEAD-Stand aus dem Cache).
- Prozesskarte: die kuratierten Lanes liegen als Daten in einer Datei (FYOS_PROZESSKARTE_LANES, ohne Einstellung die
  Vorlage `daten/prozesskarte-lanes.beispiel.yaml`) statt als Python-Konstanten; Katalog aus der ConfigMap, sonst aus
  dem Repo-Klon (`quelle`).
- Betroffen/infra: Cluster-Objekte ueber die Kubernetes-API (ServiceAccount mcp-hub) statt
  `kubectl` mit Node-Kubeconfig.
- Werkzeuge: die Hub-Karte kommt direkt aus der Registry (Die frühere Route rief hub__map ueber HTTP).
  Die STALE-Regel (>90 Tage ohne Commit) entfaellt: der Klon im Pod ist `--depth 1`, `git log`
  kennt dort nur einen Commit, und eine Gitea-Abfrage je Datei (~300) waere teurer als ihr
  Nutzen. Pfad-Ziele werden gegen den Repo-Klon geprueft — Die frühere Route konnte sie im Pod gar nicht
  pruefen und meldete sie pauschal „LEBT". Ist eine Quelle nicht lesbar, steht „?" mit Grund
  statt „TOT".
- Notfallwege: ueber die Wiki-API (WIKI_API_BASE) statt direkt aus der SQLite-Datei. Die frühere Route
  suchte die Wurzel mit `title LIKE 'Notfall%'`; die Seite heisst „Notfall — START HIER",
  der Treffer ist jetzt robust gegen ein vorangestelltes Emoji. Seiten im Block-Format
  (JSON, vom Wiki-Editor gespeichert) werden ebenfalls zerlegt, nicht nur Markdown.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import time
import urllib.request
from collections import Counter
from typing import Any

from mcp_hub.plugin import HubPlugin, ToolDescriptor
from . import _ci_fehler, _umgebung
from ._umgebung import NichtEingerichtet
from .betroffen import betroffen_bfs, infra_betroffen, knoten_treffer
from .lage import _Zwischenspeicher, gitea, gitea_api, repo

_READ_WEB = ["betrieb", "read", "flaeche"]

#: Namespace der Dienste: allgemeine Vorgabe des Produkts (keine Adresse); anderer Namespace über BETRIEB_NAMESPACE.
NS = _umgebung.lesen("BETRIEB_NAMESPACE") or _umgebung.NAMESPACE_VORGABE
#: Klon des Repositorys (FYOS_REPO_PATH) für Kataloge und Pfad-Ziele; ohne Einstellung None (nicht eingerichtet).
REPO_KLON: pathlib.Path | None = pathlib.Path(_umgebung.lesen("FYOS_REPO_PATH")) if _umgebung.lesen("FYOS_REPO_PATH") else None
#: Kuratierte Ketten der Prozesskarte: FYOS_PROZESSKARTE_LANES (Pfad einer YAML-Datei); ohne sie die Beispieldatei des Pakets.
LANES_BEISPIEL = pathlib.Path(__file__).resolve().parent / "daten" / "prozesskarte-lanes.beispiel.yaml"
LANES_DATEI = LANES_BEISPIEL  # früherer Name der mitgelieferten Datei


def wiki_api() -> str:
    """Basisadresse der Wiki-API aus WIKI_API_BASE; ohne Einstellung `NichtEingerichtet`."""
    return _umgebung.verlangen(
        "WIKI_API_BASE", "Das Wiki",
        "Setze WIKI_API_BASE (Basisadresse der Wiki-API, z. B. https://wiki.example.org/api/wiki).",
    ).rstrip("/")


def lanes_datei() -> pathlib.Path:
    eigene = _umgebung.lesen("FYOS_PROZESSKARTE_LANES")
    return pathlib.Path(eigene) if eigene else LANES_BEISPIEL

# Kategorien des Werkzeug-Katalogs mit eigener Lebenszeichen-Regel; alle uebrigen werden
# ueber ihr Pfad-Ziel (`wo`) gegen den Repo-Klon geprueft.
_KAT_DAEMON = "Domain-Daemon"
_KAT_HUB = "MCP-Hub Tool"
_WERKZEUG_STATUS = ["LEBT", "TOT", "SUPERSEDED", "?"]


def _k8s():
    """(CoreV1Api, AppsV1Api, BatchV1Api, ApiClient) mit dem ServiceAccount des Pods."""
    from kubernetes import client, config

    config.load_incluster_config()
    return client.CoreV1Api(), client.AppsV1Api(), client.BatchV1Api(), client.ApiClient()


def _configmap_yaml(name: str, schluessel: str = "katalog.yaml") -> str:
    core, _apps, _batch, _api = _k8s()
    cm = core.read_namespaced_config_map(name, NS)
    roh = (cm.data or {}).get(schluessel)
    if not roh:
        raise LookupError(f"ConfigMap {name} ohne {schluessel}")
    return roh


def _katalog(cm_name: str, repo_datei: str) -> tuple[dict[str, Any], str]:
    """Katalog-YAML: erst die ConfigMap (per Flux gesynct), sonst der Repo-Klon im Pod.

    Wirft mit beiden Gruenden, wenn keine Quelle lesbar ist — eine leere Karte waere
    geschoente Auskunft.
    """
    import yaml

    if not cm_name and REPO_KLON is None:
        raise NichtEingerichtet(
            "Der Katalog",
            "Setze den Namen einer ConfigMap mit dem Schlüssel katalog.yaml (FYOS_BETRIEB_AUTOMATISMEN_CONFIGMAP bzw. "
            "FYOS_BETRIEB_WERKZEUGE_CONFIGMAP) oder FYOS_REPO_PATH (Klon mit dem Katalog unter docs/).",
        )
    gruende: list[str] = []
    if cm_name:
        try:
            return yaml.safe_load(_configmap_yaml(cm_name)) or {}, f"ConfigMap {NS}/{cm_name}"
        except Exception as e:  # noqa: BLE001
            gruende.append(f"ConfigMap {cm_name}: {type(e).__name__}: {e}"[:160])
    if REPO_KLON is not None:
        pfad = REPO_KLON / repo_datei
        try:
            return yaml.safe_load(pfad.read_text(encoding="utf-8")) or {}, f"Repo-Klon {pfad}"
        except Exception as e:  # noqa: BLE001
            gruende.append(f"{pfad}: {type(e).__name__}")
    raise RuntimeError("; ".join(gruende))


def _lanes() -> dict[str, Any]:
    import yaml

    return yaml.safe_load(lanes_datei().read_text(encoding="utf-8"))


def _pk_norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().split("(")[0])


def prozesskarte_bauen(kat_roh: dict[str, Any], lanes_daten: dict[str, Any]) -> dict[str, Any]:
    """Karte aus Katalog + kuratierten Lanes (Logik der früheren Route)."""
    katalog = kat_roh.get("automatismen") or {}
    synth: dict[str, dict[str, str]] = lanes_daten.get("synth") or {}
    alias: dict[str, str] = lanes_daten.get("alias") or {}
    lanes: list[dict[str, Any]] = lanes_daten.get("lanes") or []
    zugeordnet: set[str] = set()

    def schluessel(nid: str) -> str | None:
        # Lanes nutzen Kurznamen („nginx"), der Katalog traegt Suffixe (.service/.timer)
        # oder kombinierte Namen.
        if nid in alias:
            return alias[nid] if alias[nid] in katalog else None
        kandidaten = [nid] + ([nid + ".timer", nid + ".service"] if "." not in nid else [])
        treffer = next((k for c in kandidaten for k in katalog if _pk_norm(k) == _pk_norm(c)), None)
        if not treffer:
            treffer = next(
                (k for k in katalog if _pk_norm(re.sub(r"\(.*\)", "", k)) == _pk_norm(nid)), None
            )
        return treffer

    nodes: dict[str, Any] = {}
    for lane in lanes:
        for spalte in lane.get("columns") or []:
            for nid in spalte:
                if nid in synth:
                    s = synth[nid]
                    nodes[nid] = {
                        "name": s.get("name"),
                        "kurz": s.get("kurz"),
                        "hinweis": s.get("hinweis"),
                        "synth": True,
                        "kategorie": lane.get("kategorie"),
                        "lane": lane["id"],
                        "schedule": {"kind": "sink"},
                    }
                    continue
                k = schluessel(nid)
                if k is None:
                    continue  # fehlt im Katalog -> faellt aus dem Layout (gemeinsam versionieren)
                zugeordnet.add(k)
                e = katalog[k]
                nodes[nid] = {
                    "name": nid,
                    "kurz": str(e.get("beschreibung", ""))[:110],
                    "synth": False,
                    "kategorie": e.get("kategorie"),
                    "lane": lane["id"],
                    "w": e,
                }
    return {
        "stand": kat_roh.get("aktualisiert"),
        "anzahl_katalog": len(katalog),
        "lanes": [
            {
                "id": lane["id"],
                "titel": lane.get("titel"),
                "sub": lane.get("sub"),
                "kategorie": lane.get("kategorie"),
                "columns": [list(c) for c in lane.get("columns") or []],
                "edges": [list(x) for x in lane.get("edges") or []],
            }
            for lane in lanes
            if any(n in nodes for spalte in lane.get("columns") or [] for n in spalte)
        ],
        "nodes": nodes,
        # Die frühere Route zog hier die Kartennamen ab — Eintraege mit Suffix oder Alias galten dadurch
        # als „nicht zugeordnet", obwohl sie auf der Karte standen.
        "nicht_zugeordnet": sorted(set(katalog) - zugeordnet),
    }


def _zeit(wert: Any) -> str | None:
    """Gitea-Zeitstempel; die Nullzeit (1970-01-01 bzw. 0001-01-01) heisst „noch nicht"."""
    if not wert or str(wert).startswith(("1970-01-01", "0001-01-01")):
        return None
    return str(wert)


def _pfadpraefixe() -> list[str]:
    """Absolute Pfade, die im Katalog für den Repo-Klon stehen können (jeweils mit „/“ am Ende): der Repo-Pfad des Hubs und
    die Einträge aus FYOS_BETRIEB_PFADPRAEFIXE (kommagetrennt, z. B. der Pfad auf dem Rechner des Betreibers)."""
    roh = [_umgebung.lesen("FYOS_REPO_PATH"), *_umgebung.liste("FYOS_BETRIEB_PFADPRAEFIXE")]
    return [p.rstrip("/") + "/" for p in roh if p and p.startswith("/")]


def _repo_ziel(wo: str) -> str | None:
    """Repo-relativer Pfad aus dem `wo`-Text des Katalogs, sonst None.

    Beispiele: „macbook · scripts/ops/x.py" -> scripts/ops/x.py;
    „/home/beispiel/projekt/.claude/skills/y" -> .claude/skills/y (wenn /home/beispiel/projekt der Repo-Pfad
    FYOS_REPO_PATH oder ein Eintrag aus FYOS_BETRIEB_PFADPRAEFIXE ist);
    „~/.claude/hooks/z.sh", „k8s ns fyos", „hermes mcp config" -> None (nicht im Repo).
    """
    for teil in re.split(r"[·,]", wo or ""):
        tok = (teil.strip().split() or [""])[0]
        if "/" not in tok or tok.startswith("~") or "*" in tok:
            continue
        for vorsatz in _pfadpraefixe():
            if tok.startswith(vorsatz):
                return tok[len(vorsatz) :].rstrip("/")
        if tok.startswith("/"):
            continue
        return tok.rstrip("/")
    return None


def werkzeug_status(
    name: str,
    e: dict[str, Any],
    daemons: set[str] | None,
    hub_tools: set[str] | None,
    klon: pathlib.Path | None,
) -> tuple[str, str]:
    """Lebenszeichen eines Katalog-Eintrags. None bei einer Quelle heisst: nicht lesbar."""
    k = str(e.get("kategorie") or "")
    if "archiviert" in k:
        return "SUPERSEDED", "archiviert"
    if k == _KAT_DAEMON:
        if daemons is None:
            return "?", "Service-Liste nicht lesbar"
        svc = name.split("|")[0].strip()
        return (
            ("LEBT", "Service im Cluster")
            if (name in daemons or svc in daemons)
            else ("TOT", "kein Service im Cluster")
        )
    if k == _KAT_HUB:
        if hub_tools is None:
            return "?", "Hub-Registry nicht lesbar"
        return ("LEBT", "im Hub geladen") if name in hub_tools else ("TOT", "nicht im Hub geladen")
    ziel = _repo_ziel(str(e.get("wo") or ""))
    if ziel is None:
        return "LEBT", "Katalog getragen (Ziel liegt ausserhalb des Repos)"
    if klon is None:
        return "?", "Repo-Klon im Pod fehlt"
    return (
        ("LEBT", "Ziel im Repo vorhanden")
        if (klon / ziel).exists()
        else ("TOT", f"Ziel fehlt im Repo: {ziel}")
    )


def _wiki_get(pfad: str, timeout: float = 8) -> Any:
    with urllib.request.urlopen(f"{wiki_api()}{pfad}", timeout=timeout) as r:
        return json.loads(r.read().decode())


def _bloecke(inhalt: str) -> list[dict[str, Any]] | None:
    """Block-Format des Wiki-Editors (JSON-Liste), sonst None (= Markdown)."""
    t = (inhalt or "").strip()
    if not t.startswith("["):
        return None
    try:
        b = json.loads(t)
    except ValueError:
        return None
    return [x for x in b if isinstance(x, dict)] if isinstance(b, list) else None


def wege_abschnitte(inhalt: str) -> list[dict[str, Any]]:
    """Schritte einer Runbook-Seite: je Ueberschrift (## oder tiefer) Titel, Kommandos, Text.

    Markdown (so legt scripts/ops/wiki_seed_runbooks.py die Seiten an) und das Block-Format
    des Wiki-Editors (Ueberschrift level>=2 beginnt einen Schritt, Code-Bloecke sind Kommandos).
    """
    bloecke = _bloecke(inhalt)
    if bloecke is not None:
        out: list[dict[str, Any]] = []
        for b in bloecke:
            typ, text = b.get("type"), str(b.get("text") or "")
            if typ == "heading" and int(b.get("level") or 1) >= 2:
                out.append({"titel": text.strip(), "befehle": [], "text": ""})
            elif not out:
                continue
            elif typ == "code":
                out[-1]["befehle"].append(text if text.endswith("\n") else text + "\n")
            elif text.strip():
                out[-1]["text"] = (out[-1]["text"] + "\n" + text).strip()[:400]
        return out
    out = []
    for teil in re.split(r"(?m)^##+ ", inhalt or "")[1:]:
        kopf, _, rest = teil.partition("\n")
        befehle = re.findall(r"```(?:\w+)?\n(.*?)```", rest, re.S)
        text = re.sub(r"```.*?```", "", rest, flags=re.S).strip()
        out.append({"titel": kopf.strip(), "befehle": befehle, "text": text[:400]})
    return out


def _symptom(inhalt: str, titel: str) -> str:
    bloecke = _bloecke(inhalt)
    klartext = (
        "\n".join(str(b.get("text") or "") for b in bloecke)
        if bloecke is not None
        else (inhalt or "")
    )
    m = re.search(r"\*\*Symptom:\*\*\s*(.+?)(?:\n|$)", klartext)
    return m.group(1).strip() if m else titel


def _ist_notfall_wurzel(seite: dict[str, Any]) -> bool:
    if seite.get("parentId") is not None:
        return False
    titel = re.sub(r"^[^A-Za-z0-9]+", "", str(seite.get("title") or ""))
    return titel.lower().startswith("notfall")


class BetriebPlugin(HubPlugin):
    name = "betrieb"
    description = "Bau- und Betriebssicht: CI-Stand je Commit, Actions-Laeufe, Warteschlange."
    version = "0.1.0"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        self._cache = _Zwischenspeicher()
        self._registry: Any = None

    def register(self) -> None:
        return None

    def registry_anbinden(self, registry: Any) -> None:
        """Vom Server nach dem Laden gerufen — Quelle der Hub-Karte fuer betrieb.werkzeuge."""
        self._registry = registry

    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="baukette",
                title="Baukette",
                description="CI-Stand eines Commits (Vorgabe: HEAD von main): Gesamtzustand und Einzeljobs.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "sha": {"type": "string", "description": "Commit (leer = HEAD)"}
                    },
                    "additionalProperties": False,
                },
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="ci_laeufe",
                title="CI-Laeufe",
                description="Letzte Commits und Gitea-Actions-Laeufe mit Zaehlern (wartend, laufend, gescheitert).",
                input_schema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 50}
                    },
                    "additionalProperties": False,
                },
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="ci_fehler",
                title="CI-Fehler",
                description=(
                    "Warum ist die CI zu einem Commit rot? Je Workflow-Lauf Name, Status und Einordnung "
                    "(grün, fehler, abgebrochen, läuft, wartet); je roter Job die Fehlerstelle aus dem Log "
                    "(pytest-Zusammenfassung, E-Zeilen, sonst Ausgabe des gescheiterten Schritts) statt des "
                    "Aufräum-Rauschens am Ende, dazu ein Satz oben und der Abgleich mit main (Test schon "
                    "dort rot = vorbestehend, sonst neu). Eingabe: sha (voll oder kurz) oder zweig "
                    "(Vorgabe main); sha gewinnt. `voll: true` zeigt auch grüne Läufe und mehr Tests."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "sha": {"type": "string", "description": "Commit, voll oder kurz (4-40 Hex-Zeichen)"},
                        "zweig": {
                            "type": "string",
                            "description": "Zweig, z. B. pruefen/betrieb/ci-vor-main oder main (Vorgabe main)",
                        },
                        "zeilen": {
                            "type": "integer",
                            "minimum": 5,
                            "maximum": 200,
                            "default": 40,
                            "description": "höchstens so viele Logzeilen je rotem Job",
                        },
                        "voll": {
                            "type": "boolean",
                            "default": False,
                            "description": "Auch grüne Läufe, bis zu 30 Tests je Job, längere Zeilen. Ohne: knapp",
                        },
                    },
                    "additionalProperties": False,
                },
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="prozesskarte",
                title="Prozesskarte",
                description=(
                    "Alle Automatismen als Ketten (Backup, Guardians, Wissen, Agenten, Inferenz, CI/CD, "
                    "Sicherheit, Leben, Ruhende): Katalog-Eintraege mit W-Fragen, Spalten, Kanten und "
                    "Katalog-Eintraege ohne Platz auf der Karte (`nicht_zugeordnet`)."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="betroffen",
                title="Was ist betroffen?",
                description=(
                    "Impact-Analyse vor einem Change. ebene=automatik: wer haengt an <von> (abhaengig) "
                    "und wer speist ein (speist), ueber die Prozesskarten-Kanten; Teilstring trifft "
                    "mehrere Knoten. ebene=infra: <von> als Node — hart gepinnte Workloads, weiche "
                    "Treffer mit Ausweich-Node, laufende Pods; <von> als Deployment — worauf es laeuft."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "von": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Automatismus, Node oder Deployment",
                        },
                        "ebene": {
                            "type": "string",
                            "enum": ["automatik", "infra"],
                            "default": "automatik",
                        },
                    },
                    "required": ["von"],
                    "additionalProperties": False,
                },
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="werkzeuge",
                title="Werkzeug-Register",
                description=(
                    "Werkzeug-Katalog (Bereich > Service, W-Fragen) mit Lebenszeichen je Werkzeug: "
                    "LEBT, TOT, SUPERSEDED oder ? (Quelle nicht lesbar), jeweils mit Grund. "
                    "Absichtlich abgeschaltete Hub-Plugins sind ausgeblendet. Filter: bereich, "
                    "status, q (Teilstring); hoechstens `limit` Eintraege (Vorgabe 50)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "bereich": {"type": "string", "description": "exakter Bereichsname"},
                        "status": {"type": "string", "enum": _WERKZEUG_STATUS},
                        "q": {
                            "type": "string",
                            "description": "Teilstring in Name, Beschreibung, Bereich oder Service",
                        },
                        "limit": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 2000,
                            "default": 50,
                            "description": "hoechstens so viele Eintraege (anzahl nennt alle Treffer)",
                        },
                    },
                    "additionalProperties": False,
                },
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="notfallwege",
                title="Notfallwege",
                description=(
                    'Notfall-Runbooks aus dem Wiki (Seiten unter „Notfall — START HIER"): je Weg '
                    "Symptom und Schritte mit kopierfertigen Kommandos."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                tags=_READ_WEB,
            ),
        ]

    def call(self, method: str, args: dict[str, Any]) -> Any:
        try:
            return self._aufruf(method, args)
        except NichtEingerichtet as e:  # Quelle ohne Einstellung: eigener Zustand mit Weg zum Einrichten, kein Ausfall
            return e.lesend()

    def _aufruf(self, method: str, args: dict[str, Any]) -> Any:
        if method == "baukette":
            sha = str(args.get("sha") or "")
            return self._cache.holen(f"baukette:{sha}", 60, lambda: self._baukette(sha))
        if method == "ci_laeufe":
            limit = max(1, min(int(args.get("limit") or 50), 50))
            return self._cache.holen(f"ci:{limit}", 15, lambda: self._ci_laeufe(limit))
        if method == "ci_fehler":
            sha, zweig, zeilen, voll = _ci_fehler.eingabe_pruefen(args)  # wirft bei falscher Eingabe
            gitea_api(), repo()  # ohne Git-API und Hauptrepository: nicht eingerichtet, kein Fehler aus dem Lauf
            return self._cache.holen(
                f"ci_fehler:{sha}:{zweig}:{zeilen}:{voll}",
                20,
                lambda: _ci_fehler.ci_fehler(args, gitea, _ci_fehler.gitea_text, self._cache),
            )
        if method == "prozesskarte":
            return self._prozesskarte()
        if method == "betroffen":
            von = str(args.get("von") or "").strip()
            if not von:
                raise ValueError("betroffen: `von` ist erforderlich")
            ebene = str(args.get("ebene") or "automatik")
            if ebene == "infra":
                return self._cache.holen(
                    f"betroffen:infra:{von}", 60, lambda: self._betroffen_infra(von)
                )
            if ebene != "automatik":
                raise ValueError(f"betroffen: unbekannte ebene {ebene!r} (automatik|infra)")
            return self._betroffen_automatik(von)
        if method == "werkzeuge":
            return self._werkzeuge_gefiltert(
                args.get("bereich"), args.get("status"), args.get("q"), args.get("limit")
            )
        if method == "notfallwege":
            return self._cache.holen("notfallwege", 600, self._notfallwege)
        raise ValueError(f"betrieb hat kein Werkzeug {method!r}")

    def _baukette(self, sha: str) -> dict[str, Any]:
        out: dict[str, Any] = {"commit": None, "ci": {"status": None, "jobs": []}, "images": []}
        try:
            out["commit"] = sha or gitea(f"/repos/{repo()}/commits?limit=1")[0]["sha"]
            st = gitea(f"/repos/{repo()}/commits/{out['commit']}/status")
            out["ci"]["status"] = st.get("state")
            out["ci"]["jobs"] = [
                {"name": (s.get("context") or "")[:60], "status": s.get("status") or s.get("state")}
                for s in st.get("statuses") or []
            ]
        except NichtEingerichtet:
            raise
        except Exception as e:  # noqa: BLE001 — Quellfehler ist Nutzdaten
            out["ci"]["fehler"] = f"{type(e).__name__}: {e}"[:160]
        return out

    def _ci_laeufe(self, limit: int) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "source": {"name": "Gitea Actions", "state": "live", "detail": None},
            "pushes": [],
            "runs": [],
            "queue": {"queued": 0, "running": 0, "failed": 0},
        }
        try:
            commits = gitea(f"/repos/{repo()}/commits?limit=20")
            out["pushes"] = [
                {
                    "sha": c.get("sha"),
                    "message": ((c.get("commit") or {}).get("message") or "").split("\n", 1)[0],
                    "author": (c.get("author") or {}).get("login")
                    or ((c.get("commit") or {}).get("author") or {}).get("name"),
                    "created_at": ((c.get("commit") or {}).get("author") or {}).get("date"),
                }
                for c in (commits if isinstance(commits, list) else [])
            ]
            payload = gitea(f"/repos/{repo()}/actions/runs?limit={limit}")
            for run in (payload.get("workflow_runs") or []) if isinstance(payload, dict) else []:
                status = str(run.get("status") or "unknown").lower()
                conclusion = str(run.get("conclusion") or "").lower()
                if status in {"queued", "pending", "waiting"}:
                    out["queue"]["queued"] += 1
                elif status in {"running", "in_progress"}:
                    out["queue"]["running"] += 1
                if conclusion in {"failure", "failed", "cancelled", "timed_out"}:
                    out["queue"]["failed"] += 1
                out["runs"].append(
                    {
                        "id": run.get("id"),
                        "name": run.get("name")
                        or run.get("workflow_name")
                        or run.get("display_title")
                        or run.get("path")
                        or "Workflow",
                        "event": run.get("event"),
                        "status": status,
                        "conclusion": run.get("conclusion"),
                        "sha": run.get("head_sha"),
                        # Audit 2026-10-02: Gitea 1.27 liefert `started_at`/`completed_at`, kein
                        # `created_at`/`updated_at` — beide Felder waren immer null. Die alten Namen
                        # bleiben (fyos-web BetriebView liest sie), gefuellt aus den echten Feldern.
                        # Noch nicht gestartete Laeufe tragen Giteas Nullzeit (1970-01-01) -> null.
                        "created_at": _zeit(run.get("created_at")) or _zeit(run.get("started_at")),
                        "updated_at": _zeit(run.get("updated_at"))
                        or _zeit(run.get("completed_at"))
                        or _zeit(run.get("started_at")),
                        "started_at": _zeit(run.get("started_at")),
                        "completed_at": _zeit(run.get("completed_at")),
                    }
                )
        except NichtEingerichtet:
            raise
        except Exception as e:  # noqa: BLE001
            out["source"] = {
                "name": "Gitea Actions",
                "state": "unavailable",
                "detail": f"{type(e).__name__}: {e}"[:180],
            }
        return out

    # ── Prozesskarte / betroffen ─────────────────────────────────────────────
    def _prozesskarte(self) -> dict[str, Any]:
        def laden() -> dict[str, Any]:
            kat, quelle = _katalog(
                _umgebung.lesen("FYOS_BETRIEB_AUTOMATISMEN_CONFIGMAP"), "docs/automatismen-katalog.yaml"
            )
            karte = prozesskarte_bauen(kat, _lanes())
            karte["quelle"] = quelle
            return karte

        return self._cache.holen("prozesskarte", 600, laden)

    def _betroffen_automatik(self, von: str) -> dict[str, Any]:
        karte = self._prozesskarte()
        knoten = knoten_treffer(karte, von)
        erg = betroffen_bfs(karte, knoten)
        erg.update(
            {
                "von": knoten,
                "ebene": "automatik",
                "stand": karte.get("stand"),
                "quelle": f"betrieb.prozesskarte (Kanten aus {lanes_datei().name})",
            }
        )
        return erg

    def _betroffen_infra(self, von: str) -> dict[str, Any]:
        core, apps, batch, api = _k8s()
        roh = api.sanitize_for_serialization  # camelCase wie `kubectl -o json`
        erg = infra_betroffen(
            roh(core.list_node()),
            roh(apps.list_namespaced_deployment(NS)),
            cronjobs_json=roh(batch.list_namespaced_cron_job(NS)),
            pods_json=roh(core.list_namespaced_pod(NS)),
            von=von,
        )
        erg["ebene"] = "infra"
        return erg

    # ── Werkzeug-Register ────────────────────────────────────────────────────
    def _werkzeuge_gefiltert(
        self, bereich: Any, status: Any, q: Any = None, limit: Any = None
    ) -> dict[str, Any]:
        """Audit 2026-10-02: ungefiltert kamen 146.000 Zeichen (479 Eintraege). Jetzt mit
        Teilstring-Filter `q` und Begrenzung; `anzahl` nennt alle Treffer, `verteilung` gilt
        fuer den ganzen Katalog (wie bisher)."""
        alle = self._cache.holen("werkzeuge", 600, self._werkzeuge)
        such = str(q or "").strip().lower()
        felder = ("name", "beschreibung", "bereich", "service")
        eintraege = [
            e
            for e in alle["eintraege"]
            if (not bereich or e.get("bereich") == bereich)
            and (not status or e.get("alive") == status)
            and (not such or any(such in str(e.get(f) or "").lower() for f in felder))
        ]
        grenze = max(1, min(int(limit or 50), 2000))
        return {
            **alle,
            "eintraege": eintraege[:grenze],
            "anzahl": len(eintraege),
            "geliefert": min(len(eintraege), grenze),
            "abgeschnitten": len(eintraege) > grenze,
            "filter": {
                "bereich": bereich or None,
                "status": status or None,
                "q": such or None,
                "limit": grenze,
            },
        }

    def _werkzeuge(self) -> dict[str, Any]:
        kat, quelle = _katalog(
            _umgebung.lesen("FYOS_BETRIEB_WERKZEUGE_CONFIGMAP"), "docs/werkzeug-katalog.yaml"
        )
        werkzeuge = kat.get("werkzeuge") or {}
        quellen: dict[str, str] = {"katalog": quelle}

        daemons: set[str] | None
        try:
            core, _apps, _batch, _api = _k8s()
            daemons = {s.metadata.name for s in core.list_namespaced_service(NS).items}
            quellen["services"] = f"{len(daemons)} Services in {NS}"
        except Exception as e:  # noqa: BLE001 — Luecke wird je Eintrag als „?" sichtbar
            daemons = None
            quellen["services"] = f"nicht lesbar: {type(e).__name__}"

        hub_tools: set[str] | None
        abgeschaltet: set[str] = set()
        try:
            if self._registry is None:
                raise RuntimeError("Registry nicht angebunden")
            hub_tools = {rt.fqtn for rt in self._registry.tools.values()}
            abgeschaltet = set(getattr(self._registry, "abgeschaltet", None) or ())
            quellen["hub"] = f"{len(hub_tools)} Werkzeuge geladen"
        except Exception as e:  # noqa: BLE001
            hub_tools = None
            quellen["hub"] = f"nicht lesbar: {type(e).__name__}"

        klon: pathlib.Path | None = REPO_KLON if REPO_KLON is not None and (REPO_KLON / ".git").exists() else None
        quellen["repo"] = (
            f"{REPO_KLON} (--depth 1, Stand Pod-Start)" if klon else f"{REPO_KLON or 'FYOS_REPO_PATH'} fehlt"
        )

        eintraege: list[dict[str, Any]] = []
        ausgeblendet = 0
        for name, roh in werkzeuge.items():
            e = roh if isinstance(roh, dict) else {}
            # Werkzeuge absichtlich abgeschalteter Hub-Plugins (echo, math, ...) sind nicht tot.
            if (
                str(e.get("kategorie") or "") == _KAT_HUB
                and str(name).split(".", 1)[0] in abgeschaltet
            ):
                ausgeblendet += 1
                continue
            alive, detail = werkzeug_status(str(name), e, daemons, hub_tools, klon)
            eintraege.append(
                {
                    "name": name,
                    **{
                        f: e.get(f)
                        for f in ("kategorie", "bereich", "service", "beschreibung", "wo", "quelle")
                    },
                    "alive": alive,
                    "alive_detail": detail,
                }
            )
        if ausgeblendet:
            quellen["ausgeblendet"] = (
                f"{ausgeblendet} Eintraege abgeschalteter Plugins ({', '.join(sorted(abgeschaltet))})"
            )
        return {
            "stand": kat.get("aktualisiert"),
            "live_stand": time.strftime("%Y-%m-%d %H:%M"),
            "anzahl": len(eintraege),
            "verteilung": dict(Counter(x["alive"] for x in eintraege)),
            "eintraege": eintraege,
            "bereiche": sorted({x["bereich"] for x in eintraege if x.get("bereich")}),
            "quellen": quellen,
        }

    # ── Notfallwege ──────────────────────────────────────────────────────────
    def _notfallwege(self) -> dict[str, Any]:
        seiten = (_wiki_get("/pages") or {}).get("items") or []
        wurzel = next(
            (s for s in sorted(seiten, key=lambda s: s.get("id") or 0) if _ist_notfall_wurzel(s)),
            None,
        )
        if wurzel is None:
            raise LookupError(f'Wiki: keine Wurzelseite „Notfall …" unter {len(seiten)} Seiten')
        kinder = sorted(
            (s for s in seiten if s.get("parentId") == wurzel["id"]),
            key=lambda s: (s.get("position") or 0, s.get("id") or 0),
        )
        wege = []
        for k in kinder:
            seite = _wiki_get(f"/pages/{int(k['id'])}")
            inhalt = str(seite.get("content") or "")
            wege.append(
                {
                    "id": seite.get("id"),
                    "titel": seite.get("title"),
                    "symptom": _symptom(inhalt, str(seite.get("title") or "")),
                    "aktualisiert": seite.get("updatedAt"),
                    "schritte": wege_abschnitte(inhalt),
                }
            )
        return {
            "stand": time.strftime("%Y-%m-%d %H:%M"),
            "anzahl": len(wege),
            "kommandos": sum(len(s["befehle"]) for w in wege for s in w["schritte"]),
            "quelle": f"{wiki_api()}/pages (Wurzel {wurzel['id']}: {wurzel.get('title')})",
            "wege": wege,
        }
