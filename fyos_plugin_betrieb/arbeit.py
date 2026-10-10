"""arbeit — Prüfstand der Arbeitsschleife (2026-10-06).

Die Prüfung liegt außerhalb dessen, was der Agent ändern darf. Dieses Plugin ist diese Außenseite. Es nimmt
weder Befehle noch Flags entgegen, sondern nur einen Zweig und eine pytest-Knoten-ID in streng geprüfter Form;
der Prüf-Job klont den Zweig selbst und führt nur `python -m pytest <nodeid>` aus.

Pilot: nur rote Tests eines Workflows (FYOS_ARBEIT_WORKFLOW, Dateiname, und FYOS_ARBEIT_WORKFLOW_NAME, Anzeigename).

    arbeit.kandidaten  rote Tests aus dem letzten abgeschlossenen Lauf des Workflows auf main
    arbeit.pruefen     startet einen Prüf-Job (Kubernetes, Namespace ARBEIT_NAMESPACE) für Zweig + Knoten-ID
    arbeit.ergebnis    Stand und Ausgabe eines Prüf-Jobs
    arbeit.diff        was ein Zweig gegenüber main ändert (Gitea-Vergleich)

Einstellungen (alle ohne Vorgabe auf eine Installation, siehe README): FYOS_ARBEIT_WORKFLOW, FYOS_ARBEIT_WORKFLOW_NAME,
FYOS_ARBEIT_BILD (Bild des Prüf-Jobs, oder `image` in der Konfiguration des Plugins), FYOS_ARBEIT_TOKEN_SECRET (Secret mit
GITEA_USER und GITEA_TOKEN für den Klon), FYOS_ARBEIT_PULL_SECRETS (kommagetrennt), FYOS_ARBEIT_MEIDE_KNOTEN
(kommagetrennt: Knoten, auf denen der Job nicht bevorzugt läuft). Allgemeine Vorgaben: Zweigvorsatz `agent`,
Testwurzel `tests`, PYTHONPATH `.` (FYOS_ARBEIT_ZWEIG_VORSATZ, FYOS_ARBEIT_TESTWURZEL, FYOS_ARBEIT_PYTHONPATH).
Ohne die nötigen Einstellungen ist das Werkzeug nicht eingerichtet: lesend ein Zustand, `pruefen` mit ok=false.

Rechte des Hub-Kontos: nur create/get/list/watch auf jobs und get/list auf pods, pods/log im Namespace.
Kein delete; ttlSecondsAfterFinished räumt auf.
"""

from __future__ import annotations

import os
import re
import time
import urllib.parse
import urllib.request
import uuid
from typing import Any

from mcp_hub.plugin import HubPlugin, ToolDescriptor
from . import _umgebung
from ._umgebung import NichtEingerichtet
from .lage import _Zwischenspeicher, gitea, gitea_api, repo

#: Namespace der Prüf-Jobs: allgemeine Vorgabe des Produkts (keine Adresse); anderer über ARBEIT_NAMESPACE.
NS = _umgebung.lesen("ARBEIT_NAMESPACE") or _umgebung.NAMESPACE_VORGABE

MAX_KANDIDATEN = 10
ZEITLIMIT_VORGABE = 600
ZEITLIMIT_MAX = 1200
ZEITLIMIT_MIN = 30

#: Allgemeine Vorgaben (üblich, keine Installation): Zweige des Agenten heißen `agent/<name>`, die Tests liegen unter
#: `tests/`, der Prüf-Job braucht keinen zusätzlichen Suchpfad.
ZWEIG_VORSATZ_VORGABE = "agent"
TESTWURZEL_VORGABE = "tests"
PYTHONPATH_VORGABE = "."
_PFADTEIL = re.compile(r"[A-Za-z0-9._][A-Za-z0-9._/-]*")
JOB_RE = re.compile(r"arbeit-pruefen-[a-f0-9]{8}")

# ── Einstellungen (bei jedem Aufruf gelesen) ─────────────────────────────────
EINRICHTEN_PRUEFSTAND = (
    "Setze FYOS_ARBEIT_WORKFLOW (Dateiname des Test-Workflows, z. B. tests.yml) und FYOS_ARBEIT_WORKFLOW_NAME (sein "
    "Anzeigename), dazu GITEA_API_BASE, GITEA_REPO und GITEA_TOKEN."
)
EINRICHTEN_JOB = (
    "Setze FYOS_ARBEIT_BILD (Bild des Prüf-Jobs mit git und pytest) und FYOS_ARBEIT_TOKEN_SECRET (Name eines Secrets "
    "mit den Schlüsseln GITEA_USER und GITEA_TOKEN für den Klon); optional FYOS_ARBEIT_PULL_SECRETS und "
    "FYOS_ARBEIT_MEIDE_KNOTEN (kommagetrennt)."
)


def namespace() -> str:
    return _umgebung.lesen("ARBEIT_NAMESPACE") or _umgebung.NAMESPACE_VORGABE


def workflow() -> str:
    return _umgebung.lesen("FYOS_ARBEIT_WORKFLOW")


def workflow_name() -> str:
    return _umgebung.lesen("FYOS_ARBEIT_WORKFLOW_NAME")


def bild() -> str:
    return _umgebung.lesen("FYOS_ARBEIT_BILD")


def _pfadteil(name: str, vorgabe: str) -> str:
    """Zweigvorsatz bzw. Testwurzel aus der Umgebung; ein Wert mit unerlaubten Zeichen oder `..` gilt nicht."""
    wert = _umgebung.lesen(name).strip("/")
    return wert if wert and _PFADTEIL.fullmatch(wert) and ".." not in wert else vorgabe


def zweig_vorsatz() -> str:
    return _pfadteil("FYOS_ARBEIT_ZWEIG_VORSATZ", ZWEIG_VORSATZ_VORGABE)


def testwurzel() -> str:
    return _pfadteil("FYOS_ARBEIT_TESTWURZEL", TESTWURZEL_VORGABE)


def pythonpath() -> str:
    return _umgebung.lesen("FYOS_ARBEIT_PYTHONPATH") or PYTHONPATH_VORGABE


def zweig_re() -> re.Pattern[str]:
    return re.compile(re.escape(zweig_vorsatz()) + r"/[a-z0-9._-]{1,60}")


def nodeid_re() -> re.Pattern[str]:
    return re.compile(re.escape(testwurzel()) + r"/[A-Za-z0-9_/]+\.py(::[A-Za-z0-9_\[\]-]+)?")


def _failed_re() -> re.Pattern[str]:
    return re.compile(r"^(?:FAILED|ERROR)\s+(" + re.escape(testwurzel()) + r"/\S+?\.py(?:::\S+)?)(?:\s+-\s+(.*))?$")


def _pruefstand_pruefen() -> None:
    """Wirft `NichtEingerichtet`, wenn der Workflow nicht benannt ist (Voraussetzung der lesenden Werkzeuge)."""
    if not workflow():
        raise NichtEingerichtet("Der Prüfstand der Arbeitsschleife", EINRICHTEN_PRUEFSTAND)
    gitea_api(), repo()


def _job_pruefen(bild_: str | None = None) -> tuple[str, str]:
    """(Bild, Name des Token-Secrets) des Prüf-Jobs; ohne eines von beiden `NichtEingerichtet`."""
    b = bild_ or bild()
    secret = _umgebung.lesen("FYOS_ARBEIT_TOKEN_SECRET")
    if not b or not secret:
        raise NichtEingerichtet("Der Prüf-Job", EINRICHTEN_JOB)
    return b, secret


def _klon_adresse() -> str:
    """Adresse zum Klonen aus der Adresse der Git-API (`…/api/v1` abgeschnitten); Zugangsdaten kommen aus der Umgebung
    des Pods (${GITEA_USER}, ${GITEA_TOKEN}), nie aus dem Hub."""
    basis = gitea_api().split("/api/")[0]
    schema, _, rest = basis.partition("://")
    if not rest:
        schema, rest = "https", basis
    return f"{schema}://${{GITEA_USER}}:${{GITEA_TOKEN}}@{rest}/{repo()}.git"

_LESEN = ["arbeit", "read"]
_SCHREIBEN = ["arbeit", "write"]

# Zeilen des Gitea-Job-Logs tragen einen Zeitstempel („2026-10-06T10:00:00.1234567Z ").
_STEMPEL = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z\s?")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ABSCHNITT = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}$")


# ── Prüfen der Eingaben ──────────────────────────────────────────────────────
def zweig_pruefen(zweig: Any) -> str:
    z = zweig if isinstance(zweig, str) else ""
    if z != "main" and not (zweig_re().fullmatch(z) and ".." not in z and not z.endswith(".lock")):
        raise ValueError(f"zweig muss `main` oder `{zweig_vorsatz()}/<name>` sein (a-z 0-9 . _ -, höchstens 60)")
    return z


def nodeid_pruefen(nodeid: Any) -> str:
    n = nodeid if isinstance(nodeid, str) else ""
    if not nodeid_re().fullmatch(n):
        raise ValueError(f"nodeid muss `{testwurzel()}/<pfad>.py` oder `…::<test>` sein, ohne Flags")
    return n


def zeitlimit_pruefen(wert: Any) -> int:
    if wert is None:
        return ZEITLIMIT_VORGABE
    if isinstance(wert, bool) or not isinstance(wert, (int, float)):
        raise ValueError("zeitlimit_s muss eine Zahl sein")
    return max(ZEITLIMIT_MIN, min(int(wert), ZEITLIMIT_MAX))


def _label(wert: str) -> str:
    """Kubernetes-Label-Wert: `/` ist dort verboten (der volle Zweig steht in der Annotation)."""
    return wert.replace("/", "_")[:63].strip("_.-")


# ── Job-Manifest (rein funktional) ───────────────────────────────────────────
# Wiederholen: kube-router setzt die NetworkPolicy für einen frischen Pod erst nach einigen Sekunden; bis dahin
# scheitert die Verbindung sofort (gemessen 06.10.: „Failed to connect … after 4 ms“ beim ersten Prüflauf).
def _klon_skript() -> str:
    return (
        "set -e\n"
        "for versuch in 1 2 3 4 5 6; do\n"
        '  if git clone --depth 1 --quiet --branch "$ZWEIG" '
        f'"{_klon_adresse()}" /work/fyos; then break; fi\n'
        '  [ "$versuch" = 6 ] && exit 1\n'
        "  rm -rf /work/fyos; sleep 5\n"
        "done\n"
        'echo "[klon] $ZWEIG $(git -C /work/fyos rev-parse HEAD)"\n'
    )


def job_manifest(
    name: str, zweig: str, nodeid: str, zeitlimit_s: int, commit: str, bild: str | None = None
) -> dict[str, Any]:
    """Kubernetes-Job für einen Prüflauf. Eingaben müssen vorher geprüft sein.

    Zweig und Knoten-ID gelangen nur als Umgebungsvariable (Klon) bzw. als ein einzelnes
    argv-Element (pytest) in den Pod, nie in eine Shell-Zeichenkette. Ohne Bild und Token-Secret
    (FYOS_ARBEIT_BILD, FYOS_ARBEIT_TOKEN_SECRET) wirft das `NichtEingerichtet`.
    """
    bild, token_secret = _job_pruefen(bild)
    labels = {
        "app": "arbeit-pruefen",
        "fyos/zweig": _label(zweig),
        "fyos/commit": commit[:40],
    }
    gitea_env = [
        {
            "name": "GITEA_USER",
            "valueFrom": {"secretKeyRef": {"name": token_secret, "key": "GITEA_USER"}},
        },
        {
            "name": "GITEA_TOKEN",
            "valueFrom": {"secretKeyRef": {"name": token_secret, "key": "GITEA_TOKEN"}},
        },
    ]
    sicher = {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
    meide = _umgebung.liste("FYOS_ARBEIT_MEIDE_KNOTEN")
    pull = _umgebung.liste("FYOS_ARBEIT_PULL_SECRETS")
    # Weich: bevorzugt nicht auf den genannten Knoten (z. B. dem, der gerade misst); ohne Einstellung keine Vorliebe.
    affinitaet = {
        "nodeAffinity": {
            "preferredDuringSchedulingIgnoredDuringExecution": [
                {
                    "weight": 100,
                    "preference": {
                        "matchExpressions": [
                            {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": meide}
                        ]
                    },
                }
            ]
        }
    }
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace(),
            "labels": labels,
            "annotations": {"fyos/zweig-voll": zweig, "fyos/nodeid": nodeid},
        },
        "spec": {
            "ttlSecondsAfterFinished": 3600,
            "activeDeadlineSeconds": zeitlimit_s,
            "backoffLimit": 0,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    **({"imagePullSecrets": [{"name": s} for s in pull]} if pull else {}),
                    "securityContext": {"runAsUser": 10001, "runAsNonRoot": True, "fsGroup": 10001},
                    **({"affinity": affinitaet} if meide else {}),
                    "volumes": [{"name": "work", "emptyDir": {}}],
                    "initContainers": [
                        {
                            "name": "klon",
                            "image": bild,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["/bin/sh", "-c", _klon_skript()],
                            "env": [
                                *gitea_env,
                                {"name": "ZWEIG", "value": zweig},
                                {"name": "HOME", "value": "/tmp"},
                            ],
                            "volumeMounts": [{"name": "work", "mountPath": "/work"}],
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                            "securityContext": sicher,
                        }
                    ],
                    "containers": [
                        {
                            "name": "pruefen",
                            "image": bild,
                            "imagePullPolicy": "IfNotPresent",
                            "workingDir": "/work/fyos",
                            "command": [
                                "python",
                                "-m",
                                "pytest",
                                nodeid,
                                "-q",
                                "--tb=short",
                                "-p",
                                "no:cacheprovider",
                            ],
                            "env": [
                                {"name": "PYTHONPATH", "value": pythonpath()},
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                                {"name": "PYTHONUNBUFFERED", "value": "1"},
                                {"name": "HOME", "value": "/tmp"},
                            ],
                            "volumeMounts": [{"name": "work", "mountPath": "/work"}],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "1", "memory": "1Gi"},
                            },
                            "securityContext": sicher,
                        }
                    ],
                },
            },
        },
    }


# ── Log-Auswertung ───────────────────────────────────────────────────────────
def log_zeilen(text: str) -> list[str]:
    """Gitea-Job-Log → Zeilen ohne Zeitstempel und Farbcodes."""
    return [_ANSI.sub("", _STEMPEL.sub("", z)).rstrip() for z in (text or "").splitlines()]


def rote_tests(text: str) -> list[dict[str, str]]:
    """`FAILED <nodeid> - <Meldung>`-Zeilen eines pytest-Logs, ohne Doppelte, in Logreihenfolge.

    Die Fehlerzeile ist die Meldung der FAILED-Zeile; fehlt sie (pytest kürzt je nach Terminal), die
    erste `E `-Zeile aus dem Abschnitt des Tests (`____ test_name ____`).
    """
    zeilen = log_zeilen(text)
    erste_e: dict[str, str] = {}
    abschnitt = ""
    for z in zeilen:
        m = _ABSCHNITT.match(z)
        if m:
            abschnitt = m.group(1)
            continue
        if abschnitt and z.startswith("E ") and z[1:].strip():
            erste_e.setdefault(abschnitt, z[1:].strip())  # erste E-Zeile = die Aussage
    erg: dict[str, str] = {}
    for z in zeilen:
        m = _failed_re().match(z.strip())
        if not m or not nodeid_re().fullmatch(m.group(1)):
            continue
        nodeid, meldung = m.group(1), (m.group(2) or "").strip()
        if not meldung:
            name = nodeid.rsplit("::", 1)[-1]
            meldung = erste_e.get(name) or erste_e.get(name.split("[")[0], "")
        erg.setdefault(nodeid, meldung)
    return [{"nodeid": n, "fehler": f[:300]} for n, f in erg.items()]


# ── HTTP (Gitea) ─────────────────────────────────────────────────────────────
def _gitea_text(pfad: str, timeout: float = 15) -> str:
    token = os.getenv("GITEA_TOKEN", "")
    req = urllib.request.Request(
        f"{gitea_api()}{pfad}", headers={"Authorization": f"token {token}"} if token else {}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _ist_lauf_von_workflow(lauf: dict[str, Any]) -> bool:
    pfad = str(lauf.get("path") or "")
    return pfad.split("@", 1)[0].endswith(workflow()) or str(lauf.get("name") or "") == workflow_name()


def _zeit(wert: Any) -> str | None:
    if not wert or str(wert).startswith(("1970-01-01", "0001-01-01")):
        return None
    return str(wert)


def _k8s_batch_core():
    from kubernetes import client, config

    try:
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            config.load_incluster_config()
        else:
            config.load_kube_config(config_file=os.getenv("KUBECONFIG") or None)
    except config.ConfigException as e:  # kein Cluster-Zugang: nicht eingerichtet, kein Ausfall
        raise NichtEingerichtet(
            "Der Zugang zum Cluster",
            "Betreibe das Plugin im Cluster (ServiceAccount) oder setze KUBECONFIG auf eine lesbare Kubeconfig.",
        ) from e
    return client.BatchV1Api(), client.CoreV1Api()


def _zweig_pfad(zweig: str) -> str:
    return urllib.parse.quote(zweig, safe="/")


class ArbeitPlugin(HubPlugin):
    name = "arbeit"
    description = "Prüfstand der Arbeitsschleife: rote Tests, Prüf-Jobs, Ergebnis, Zweig-Diff."
    version = "0.1.0"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        self._cache = _Zwischenspeicher()
        self._bild = str((config or {}).get("image") or "")  # sonst FYOS_ARBEIT_BILD (bei jedem Aufruf gelesen)

    def health(self) -> dict[str, Any]:
        if not workflow():
            return {"ok": True, "status": _umgebung.NICHT_EINGERICHTET, "einrichten": EINRICHTEN_PRUEFSTAND}
        return {"ok": True, "status": "ok"}

    def register(self) -> None:
        return None

    def describe_tools(self) -> list[ToolDescriptor]:
        leer = {"type": "object", "properties": {}, "additionalProperties": False}
        return [
            ToolDescriptor(
                name="kandidaten",
                title="Kandidaten (rote Tests)",
                description=(
                    "Rote Tests aus dem letzten abgeschlossenen Lauf des Test-Workflows auf main, höchstens 10, "
                    "je mit id (`test:<nodeid>`), nodeid, Commit, Lauf-Link und letzter Fehlerzeile. "
                    "Ist der Lauf grün, ist die Liste leer. `quelle_ok: false` heißt: Gitea nicht lesbar."
                ),
                input_schema=leer,
                tags=_LESEN,
            ),
            ToolDescriptor(
                name="pruefen",
                title="Prüfen (Job starten)",
                description=(
                    "Startet einen Prüf-Job im Cluster: klont `zweig` (main oder agent/<name>) flach und führt "
                    "genau `pytest <nodeid>` aus. Antwort {ok, job, zweig, nodeid}; das Ergebnis holt "
                    "arbeit.ergebnis. Keine Flags, keine Shell; Zeitlimit Vorgabe 600 s, höchstens 1200 s. "
                    "Ohne Prüf-Job-Einstellungen (Bild, Token-Secret): ok=false mit dem Weg zum Einrichten."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "zweig": {"type": "string", "description": f"`main` oder `{zweig_vorsatz()}/<name>`"},
                        "nodeid": {
                            "type": "string",
                            "description": f"z. B. {testwurzel()}/test_x.py::test_y",
                        },
                        "zeitlimit_s": {
                            "type": "integer",
                            "minimum": ZEITLIMIT_MIN,
                            "maximum": ZEITLIMIT_MAX,
                            "default": ZEITLIMIT_VORGABE,
                        },
                    },
                    "required": ["zweig", "nodeid"],
                    "additionalProperties": False,
                },
                tags=_SCHREIBEN,
            ),
            ToolDescriptor(
                name="ergebnis",
                title="Ergebnis eines Prüf-Jobs",
                description=(
                    "Stand eines Prüf-Jobs: fertig, gruen (true/false, null solange er läuft oder der Klon "
                    "scheiterte), rc, commit, dauer_s und die letzten 60 Zeilen Ausgabe."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "job": {"type": "string", "description": "arbeit-pruefen-<8 hex>"}
                    },
                    "required": ["job"],
                    "additionalProperties": False,
                },
                tags=_LESEN,
            ),
            ToolDescriptor(
                name="diff",
                title="Zweig gegen main",
                description=(
                    "Was ein Zweig gegenüber main ändert: voraus (Commits), Dateien, Commits (sha, titel). "
                    "Gitea-Vergleich main...zweig."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"zweig": {"type": "string"}},
                    "required": ["zweig"],
                    "additionalProperties": False,
                },
                tags=_LESEN,
            ),
        ]

    def call(self, method: str, args: dict[str, Any]) -> Any:
        try:
            return self._aufruf(method, args)
        except NichtEingerichtet as e:  # Einstellung fehlt: eigener Zustand mit Weg zum Einrichten, kein Ausfall
            return e.schreibend() if method == "pruefen" else e.lesend()

    def _aufruf(self, method: str, args: dict[str, Any]) -> Any:
        if method == "kandidaten":
            return self._cache.holen("kandidaten", 60, self.kandidaten)
        if method == "pruefen":
            return self.pruefen(args.get("zweig"), args.get("nodeid"), args.get("zeitlimit_s"))
        if method == "ergebnis":
            return self.ergebnis(args.get("job"))
        if method == "diff":
            return self.diff(args.get("zweig"))
        raise ValueError(f"arbeit hat kein Werkzeug {method!r}")

    # ── kandidaten ───────────────────────────────────────────────────────────
    def kandidaten(self) -> dict[str, Any]:
        _pruefstand_pruefen()
        try:
            nutzlast = gitea(f"/repos/{repo()}/actions/runs?branch=main&limit=50", timeout=15)
            laeufe = [
                r
                for r in (nutzlast.get("workflow_runs") or [] if isinstance(nutzlast, dict) else [])
                if _ist_lauf_von_workflow(r)
                and str(r.get("status") or "").lower() in {"completed", "success", "failure"}
                and (not r.get("head_branch") or r.get("head_branch") == "main")
                # Abgebrochene/übersprungene Läufe sagen nichts über main (gemessen 06.10.: der jüngste
                # abgeschlossene Lauf war `cancelled`, davor drei `failure` — die Liste kam leer).
                and str(r.get("conclusion") or r.get("status") or "").lower() not in {"cancelled", "skipped"}
            ]
            if not laeufe:
                return {
                    "kandidaten": [],
                    "quelle_ok": True,
                    "grund": f"kein abgeschlossener Lauf von {workflow()} auf main in den letzten 50",
                }
            lauf = max(laeufe, key=lambda r: int(r.get("id") or 0))
            if str(lauf.get("conclusion") or "").lower() != "failure":
                return {"kandidaten": [], "quelle_ok": True}
            return {"kandidaten": self._aus_lauf(lauf)[:MAX_KANDIDATEN], "quelle_ok": True}
        except Exception as e:  # noqa: BLE001 — Quellfehler ist Nutzdaten, keine leere Liste ohne Grund
            return {"kandidaten": [], "quelle_ok": False, "grund": f"{type(e).__name__}: {e}"[:200]}

    def _aus_lauf(self, lauf: dict[str, Any]) -> list[dict[str, Any]]:
        lauf_id = int(lauf["id"])
        jobs = gitea(f"/repos/{repo()}/actions/runs/{lauf_id}/jobs", timeout=15)
        jobliste = jobs.get("jobs") or [] if isinstance(jobs, dict) else []
        rote = [
            j for j in jobliste if str(j.get("conclusion") or "").lower() == "failure"
        ] or jobliste
        gefunden: dict[str, str] = {}
        for j in rote:
            if j.get("id") is None:
                continue
            text = _gitea_text(f"/repos/{repo()}/actions/jobs/{int(j['id'])}/logs")
            for t in rote_tests(text):
                gefunden.setdefault(t["nodeid"], t["fehler"])
        seit = (
            _zeit(lauf.get("completed_at"))
            or _zeit(lauf.get("started_at"))
            or _zeit(lauf.get("created_at"))
        )
        url = lauf.get("html_url") or f"{gitea_api().split('/api/')[0]}/{repo()}/actions/runs/{lauf_id}"
        return [
            {
                "id": f"test:{nodeid}",
                "art": "roter_test",
                "workflow": workflow(),
                "nodeid": nodeid,
                "datei": nodeid.split("::", 1)[0],
                "seit": seit,
                "commit": lauf.get("head_sha"),
                "lauf_url": url,
                "fehler": fehler[:300],
            }
            for nodeid, fehler in gefunden.items()
        ]

    # ── pruefen ──────────────────────────────────────────────────────────────
    def pruefen(self, zweig: Any, nodeid: Any, zeitlimit_s: Any = None) -> dict[str, Any]:
        try:
            z = zweig_pruefen(zweig)
            n = nodeid_pruefen(nodeid)
            limit = zeitlimit_pruefen(zeitlimit_s)
        except ValueError as e:
            return {"ok": False, "fehler": str(e)}
        _job_pruefen(self._bild)  # ohne Bild und Token-Secret: nicht eingerichtet, bevor etwas angefasst wird
        try:
            commit = str(gitea(f"/repos/{repo()}/branches/{_zweig_pfad(z)}")["commit"]["id"])
        except NichtEingerichtet:
            raise
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "zweig": z,
                "nodeid": n,
                "fehler": f"Zweig nicht lesbar: {type(e).__name__}: {e}"[:200],
            }
        name = f"arbeit-pruefen-{uuid.uuid4().hex[:8]}"
        koerper = job_manifest(name, z, n, limit, commit, self._bild)
        try:
            batch, _core = _k8s_batch_core()
            batch.create_namespaced_job(namespace(), koerper)
        except NichtEingerichtet:
            raise
        except Exception as e:  # noqa: BLE001
            return {
                "ok": False,
                "zweig": z,
                "nodeid": n,
                "fehler": f"Job nicht angelegt: {type(e).__name__}: {e}"[:200],
            }
        return {
            "ok": True,
            "job": name,
            "zweig": z,
            "nodeid": n,
            "commit": commit,
            "zeitlimit_s": limit,
        }

    # ── ergebnis ─────────────────────────────────────────────────────────────
    def ergebnis(self, job: Any) -> dict[str, Any]:
        name = job if isinstance(job, str) else ""
        if not JOB_RE.fullmatch(name):
            return {
                "fertig": False,
                "gruen": None,
                "fehler": "job muss `arbeit-pruefen-<8 hex>` sein",
            }
        batch, core = _k8s_batch_core()
        j = batch.read_namespaced_job(name, namespace())
        pods = core.list_namespaced_pod(namespace(), label_selector=f"job-name={name}").items
        pod = max(pods, key=lambda p: str(p.metadata.creation_timestamp), default=None)
        pod_log = ""
        container = "pruefen"
        status_pod = getattr(pod, "status", None)
        haupt = (
            next((c for c in (status_pod.container_statuses or []) if c.name == "pruefen"), None)
            if status_pod
            else None
        )
        init = (
            next((c for c in (status_pod.init_container_statuses or []) if c.name == "klon"), None)
            if status_pod
            else None
        )
        if pod is not None:
            if haupt is None or (haupt.state.terminated is None and haupt.state.running is None):
                container = "klon" if init is not None and init.state.waiting is None else "pruefen"
            try:
                pod_log = core.read_namespaced_pod_log(
                    pod.metadata.name, namespace(), container=container, tail_lines=60
                )
            except Exception as e:  # noqa: BLE001 — Log fehlt z. B. solange der Pod noch startet
                pod_log = f"[log nicht lesbar: {type(e).__name__}]"
        return ergebnis_bauen(j, haupt, init, pod_log)

    # ── diff ─────────────────────────────────────────────────────────────────
    def diff(self, zweig: Any) -> dict[str, Any]:
        try:
            z = zweig_pruefen(zweig)
        except ValueError as e:
            return {"ok": False, "fehler": str(e)}
        try:
            d = gitea(f"/repos/{repo()}/compare/main...{_zweig_pfad(z)}", timeout=15)
        except NichtEingerichtet:
            raise
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "zweig": z, "fehler": f"{type(e).__name__}: {e}"[:200]}
        return diff_bauen(z, d)


def diff_bauen(zweig: str, d: dict[str, Any]) -> dict[str, Any]:
    commits = d.get("commits") or []
    return {
        "ok": True,
        "zweig": zweig,
        "voraus": int(
            d.get("total_commits") if d.get("total_commits") is not None else len(commits)
        ),
        "dateien": [str(f.get("filename")) for f in (d.get("files") or []) if f.get("filename")][
            :200
        ],
        "commits": [
            {
                "sha": c.get("sha"),
                "titel": str(((c.get("commit") or {}).get("message") or "")).split("\n", 1)[0][
                    :200
                ],
            }
            for c in commits[:50]
        ],
    }


def ergebnis_bauen(job: Any, haupt: Any, init: Any, log: str) -> dict[str, Any]:
    """Job-Status + Container-Zustände + Log → Antwort. Rein, gut mit Attrappen prüfbar."""
    st = job.status
    erfolg = (st.succeeded or 0) > 0
    bedingungen = {c.type: c for c in (st.conditions or []) if c.status == "True"}
    gescheitert = (st.failed or 0) > 0 or "Failed" in bedingungen
    fertig = erfolg or gescheitert
    rc = None
    if haupt is not None and haupt.state is not None and haupt.state.terminated is not None:
        rc = haupt.state.terminated.exit_code
    grund = None
    if gescheitert and rc is None:
        # Kein Testlauf zustande gekommen: Klon gescheitert, Zeitlimit, Pod verdrängt. Kein „rot".
        t = init.state.terminated if init is not None and init.state is not None else None
        if t is not None and t.exit_code not in (0, None):
            grund = "Klon fehlgeschlagen"
        else:
            b = bedingungen.get("Failed")
            grund = (getattr(b, "reason", None) or "Job gescheitert") if b else "Job gescheitert"
    gruen: bool | None
    if erfolg:
        gruen = True
    elif gescheitert and rc is not None:
        gruen = False
    else:
        gruen = None
    start = st.start_time
    ende = st.completion_time
    if ende is None and gescheitert:
        ende = next(
            (c.last_transition_time for c in bedingungen.values() if c.type == "Failed"), None
        )
    dauer = None
    if start is not None:
        dauer = round(((ende.timestamp() if ende else time.time()) - start.timestamp()), 1)
    labels = job.metadata.labels or {}
    erg: dict[str, Any] = {
        "fertig": fertig,
        "gruen": gruen,
        "rc": rc,
        "commit": labels.get("fyos/commit"),
        "dauer_s": dauer,
        "ausgabe": "\n".join(str(log or "").splitlines()[-60:]),
    }
    if grund:
        erg["grund"] = grund
    return erg
