"""lage — Lagebild von FYOS als Hub-Werkzeuge (2026-09-30).

Ersetzt die früheren Routen `/api/fyos/{ampel,metriken,nutzung,…}` (sie am 2026-09-29
abgebaut). Leitbild „eine Faehigkeit, alle Flaechen": dieselben Werkzeuge nutzen fyos-web
(Tag `flaeche` → /hub-api/app-call), Agenten (MCP) und WebMCP.

Werkzeuge (lesend, bis auf fenster_setzen):
    lage.ampel     Zustand gruen/blau/dev/debug/gelb/rot + Begruendung + offene Fenster
    lage.metrik    Kurve einer Prometheus-Kennzahl (eine query_range-Abfrage)
    lage.nutzung   Verweildauer je Flaeche aus dem Redis-Strom fyos:pane:dwell
    lage.fristen   Fruehwarn-Uhren: Zertifikat, Plattenfuellstand (Trend), Backups
    lage.anwesenheit  wer in den letzten Stunden committet hat (Menschen und Agenten)
    lage.chronik   Zeitstrahl: Commits (mit CI), Daemon-Laeufe, Insights, Vorfaelle
    lage.suche     Suche ueber Aufgaben, Tickets und Insights; Tickets kommen ueber die Vorgangs-Quelle des Hubs
                   (Ticketdienst oder Workboard des Agenten), der Link zum Vorgang aus FYOS_VORGANG_LINK
    lage.fenster_setzen  Dev-/Test-/Wartungsfenster oeffnen oder schliessen — ruft
                   scripts/ops/fenster.py aus dem Repo-Klon (eine Umsetzung, ein Verhalten)

Aenderungen gegenueber der frueheren Route (bewusst):
- Die Ampel meldet nie „gruen", wenn eine Quelle nicht lesbar war — dann „blau" mit Grund.
  Die frühere Route zeigte bei unlesbarem Redis gruen.
- Der Handschalter `fyos:ampel:modus` entfaellt: niemand hat ihn je geschrieben.
- Metriken: eine query_range statt 24 Einzelabfragen (früher: 24 x offset).
- Fristen: die Backup-Uhr kommt aus dem echten lastSuccessfulTime der Backup-CronJobs.
  Die frühere Route meldete sie fest „ok", ohne je zu messen.
- Anwesenheit: die zwei früheren Heartbeats lasen Schluessel mit Tippfehlern, die es nie gab
  (`leter_lauf`, `letzer_lauf`) — entfallen.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

from mcp_hub import vorgaenge
from mcp_hub.plugin import HubPlugin, ToolDescriptor

from . import _umgebung
from ._umgebung import NichtEingerichtet

# ── Einstellungen ─────────────────────────────────────────────────────────────────────────────────────────────────────
# Es gibt keine Vorgabe auf einen Dienst: ohne Einstellung ist die Quelle „nicht eingerichtet“ (siehe `_umgebung`).
# Die Funktionen lesen die Umgebung bei jedem Aufruf. Die Großbuchstaben-Namen bleiben für Pakete, die sie importieren
# (`lage.REPO`, `lage.GITEA_API`, `lage.PANE_AUTH`, …); sie halten den Stand beim Start des Hubs.

#: Allgemeine Vorgabe: so heißt die Etikett-Konvention der Backup-CronJobs von FYOS; ein Betreiber mit anderem Etikett
#: setzt FRIST_BACKUP_LABEL.
BACKUP_LABEL_VORGABE = "fyos/component=backup"
#: Kopfzeile, mit der interne Dienste den Hub erkennen (Wert aus FYOS_PANE_AUTH, ohne Wert keine Kopfzeile).
PANE_KOPF = "X-FYOS-Pane-Auth"

#: Dienste, die `dienst()` anspricht: Name → Einstellung mit der Adresse. Keine Vorgabe.
DIENST_ADRESSE = {
    # der Agent selbst (Schnittstelle /nexus/*). Nicht FYOS_AGENT_URL: unter dem Namen steht die Adresse des
    # OpenClaw-Gateways, mit der der Kern die Kopplung einschaltet; das ist ein anderer Dienst.
    "agent": "FYOS_AGENTDIENST_URL",
    "aufgaben": "FYOS_AUFGABEN_URL",  # Aufgaben (Schnittstelle /api/life/*)
    "einsichten": "FYOS_EINSICHTEN_URL",  # Einsichten (Schnittstelle /api/insights/*)
}


def redis_url() -> str:
    return _umgebung.lesen("REDIS_URL")


def prometheus_url() -> str:
    return _umgebung.lesen("PROMETHEUS_URL").rstrip("/")


def gitea_api() -> str:
    """Basisadresse der Git-API (mit /api/v1); ohne Einstellung `NichtEingerichtet`."""
    return _umgebung.verlangen(
        "GITEA_API_BASE", "Die Git-API (Gitea)",
        "Setze GITEA_API_BASE (z. B. https://git.example.org/api/v1), GITEA_REPO (Besitzer/Name) und GITEA_TOKEN.",
    ).rstrip("/")


def repo() -> str:
    """Besitzer/Name des Hauptrepositorys; ohne Einstellung `NichtEingerichtet`."""
    return _umgebung.verlangen("GITEA_REPO", "Das Hauptrepository", "Setze GITEA_REPO (Besitzer/Name, z. B. beispiel/projekt).")


def pane_auth() -> dict[str, str]:
    kopf = {"Accept": "application/json"}
    wert = _umgebung.lesen("FYOS_PANE_AUTH")
    if wert:
        kopf[PANE_KOPF] = wert
    return kopf


def dienst_url(name: str) -> str:
    """Adresse des Dienstes `name` (agent, aufgaben, einsichten); ohne Einstellung `NichtEingerichtet`."""
    if name not in DIENST_ADRESSE:
        raise KeyError(name)
    env = DIENST_ADRESSE[name]
    return _umgebung.verlangen(env, f"Der Dienst „{name}“", f"Setze {env} (Adresse des Dienstes, z. B. http://dienst.example.org).").rstrip("/")


REDIS_URL = redis_url()
PROMETHEUS = prometheus_url()
GITEA_API = _umgebung.lesen("GITEA_API_BASE").rstrip("/")
REPO = _umgebung.lesen("GITEA_REPO")
CADDY = _umgebung.lesen("FRIST_CADDY")
ZERT_HOSTS = _umgebung.liste("FRIST_ZERT_HOSTS")
BACKUP_LABEL = _umgebung.lesen("FRIST_BACKUP_LABEL") or BACKUP_LABEL_VORGABE
PANE_AUTH = pane_auth()
DIENSTE = {n: _umgebung.lesen(env) for n, env in DIENST_ADRESSE.items()}


def vorgang_ziel(vid: Any) -> str:
    """Link zu einem Vorgang aus FYOS_VORGANG_LINK (``{id}`` wird ersetzt), etwa ``/vorgaenge?ref={id}``; ohne
    Vorlage leer."""
    vorlage = os.getenv("FYOS_VORGANG_LINK", "").strip()
    return vorlage.replace("{id}", urllib.parse.quote(str(vid or ""), safe="")) if vorlage else ""


def dienst(name: str, pfad: str, timeout: float = 6) -> Any:
    """GET an einen Dienst des Agenten (mit Pane-Kopfzeile). Wirft bei Fehlern, ohne Adresse `NichtEingerichtet`.

    Der Agent selbst (/nexus/*) verlangt seinen API-Token als Bearer (FYOS_AGENTDIENST_TOKEN);
    mit der Pane-Kopfzeile allein kommt 401 (gesehen 2026-09-30 in lage.chronik). FYOS_AGENT_TOKEN ist das Token des
    OpenClaw-Gateways und geht nie hierher.
    """
    url = dienst_url(name)
    kopf = pane_auth()
    token = _umgebung.lesen("FYOS_AGENTDIENST_TOKEN")
    if name == "agent" and token:
        kopf["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{url}{pfad}", headers=kopf)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _iso(ts: Any) -> float | None:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None

METRIKEN: dict[str, str] = {
    "req_gesamt": "sum(rate(http_requests_total[2m]))",
    "fehlerquote": '100 * sum(rate(http_requests_total{status=~"5.."}[5m])) / clamp_min(sum(rate(http_requests_total[5m])), 0.001)',
    "gpu_vram": 'DCGM_FI_DEV_FB_USED{gpu="0"}',
    "gpu_util": 'DCGM_FI_DEV_GPU_UTIL{gpu="0"}',
    "pod_neustarts": 'sum(kube_pod_container_status_restarts_total{namespace="fyos"})',
    "redis_ops": "rate(redis_commands_processed_total[2m])",
}

# Art eines Wartungsfensters → Ampelzustand; der ernstere gewinnt.
_ART_ZUSTAND = {"dev": "dev", "test": "dev", "debug": "debug", "wartung": "gelb"}
_RANG = {"gruen": 0, "blau": 1, "dev": 2, "debug": 3, "gelb": 4, "rot": 5}

_READ_WEB = ["lage", "read", "flaeche"]
_WRITE_WEB = ["lage", "write", "flaeche"]
#: Knoten, für die Fenster gelten (FYOS_FENSTER_KNOTEN, kommagetrennt, der erste ist der Standardknoten). Ohne die
#: Einstellung gibt es keine: Fenster setzen ist dann „nicht eingerichtet“.


def fenster_knoten() -> list[str]:
    """Die Knoten mit Fenster, bei jedem Aufruf aus der Einstellung gelesen (leer = nicht eingerichtet)."""
    return _umgebung.liste("FYOS_FENSTER_KNOTEN")


def _knoten_schema() -> dict[str, Any]:
    """Schema des Arguments `knoten`: die eingerichteten Knoten als Auswahl, der erste als Vorgabe."""
    knoten = fenster_knoten()
    return {"type": "string", "enum": knoten, "default": knoten[0]} if knoten else {"type": "string"}


#: Stand beim Start des Hubs, für Importeure (andere Pakete bilden daraus ihre Knotenliste).
FENSTER_KNOTEN = fenster_knoten()
FENSTER_ARTEN = ["dev", "test", "debug", "wartung"]
FENSTER_MAX_MINUTEN = 10080  # wie fenster.py MAX_MINUTEN: 7 Tage


class _KeinRedis:
    """Platzhalter, solange REDIS_URL fehlt: jede Anfrage meldet „nicht eingerichtet“ (so wie ein nicht erreichbarer
    Redis eine Ausnahme wirft, nur mit dem Weg zum Einrichten). `_redis()` selbst wirft nie."""

    def __getattr__(self, name: str):
        def anfrage(*_a: Any, **_k: Any) -> Any:
            raise NichtEingerichtet("Redis", "Setze REDIS_URL (z. B. redis://redis.example.org:6379).")

        return anfrage


def _redis():
    url = redis_url()
    if not url:
        return _KeinRedis()
    import redis  # im Hub-Image vorhanden

    return redis.Redis.from_url(url, socket_timeout=3, decode_responses=True)


# ── Fenster-Sperre (gemeinsam fuer alle Steuer-Werkzeuge: Steuerpult, Modelle) ──────────────
#: In diesen Fenstern wird auf dem Knoten gebaut oder gemessen: Steuern ist gesperrt. Ein
#: Wartungsfenster sperrt nicht — darin wird ja gerade gesteuert (es meldet nur die Waechter leiser).
SPERR_ARTEN = ("dev", "test", "debug")
# Ampel: Fehler-Bus nur über dieses Fenster zählen; Warnungen erst ab dieser Zahl erwähnen (ohne Farbwechsel).
FEHLER_FENSTER_S = 3600
WARNUNGEN_SCHWELLE = 10
_WOCHENTAG = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def fenster_offen(knoten: str) -> dict[str, Any] | None:
    """Das offene Fenster fuer `knoten` (Redis fyos:wartung:<knoten>, geschrieben von
    scripts/ops/fenster.py ueber wartung.py) oder None.

    Wirft, wenn Redis nicht lesbar ist — wer damit sperrt, muss das als „gesperrt" lesen
    (siehe `sperrgrund`)."""
    r = _redis()
    roh = r.get(f"fyos:wartung:{knoten}")
    if not roh:
        return None
    try:
        e = json.loads(roh)
    except ValueError:
        e = {}
    grund = str(e.get("grund") or "")
    art = str(e.get("art") or "")
    if grund.startswith("[") and "]" in grund:
        art = grund[1 : grund.index("]")]
        grund = grund[grund.index("]") + 1 :].strip()
    rest = r.ttl(f"fyos:wartung:{knoten}")
    bis = None
    if isinstance(rest, int) and rest > 0:
        bis = dt.datetime.fromtimestamp(time.time() + rest, dt.timezone.utc).isoformat(timespec="minutes")
    return {
        "knoten": knoten,
        "art": art or "wartung",
        "grund": grund[:200] or "(ohne Grund)",
        "von": e.get("von"),
        "seit": e.get("angekuendigt"),
        "rest_min": int(rest // 60) if isinstance(rest, int) and rest > 0 else None,
        "bis": bis,
    }


def _bis_text(bis_iso: str | None) -> str:
    if not bis_iso:
        return "auf Weiteres"
    from zoneinfo import ZoneInfo

    t = dt.datetime.fromisoformat(bis_iso).astimezone(ZoneInfo("Europe/Berlin"))
    return f"{_WOCHENTAG[t.weekday()]} {t:%d.%m. %H:%M}"


def sperrgrund(knoten: str | None) -> str | None:
    """Ein Satz, warum Steuern auf `knoten` gerade gesperrt ist — oder None, wenn es frei ist.

    Fail-closed: Ist der Fensterzustand nicht lesbar, gilt der Knoten als gesperrt. Ein
    Steuer-Werkzeug ruft das vor jeder schreibenden Aktion fuer den Zielknoten auf (bei einem
    Pod: dessen nodeName)."""
    if not knoten:
        return None
    try:
        f = fenster_offen(knoten)
    except Exception as e:  # noqa: BLE001 — Sperre im Zweifel
        return f"Fensterzustand von {knoten} nicht lesbar ({type(e).__name__}), darum gesperrt"
    if f is None or f["art"] not in SPERR_ARTEN:
        return None
    return f"{f['art'].capitalize()}-Fenster auf {knoten} bis {_bis_text(f['bis'])}: {f['grund'][:120]}"


def gitea(pfad: str, timeout: float = 6) -> Any:
    """GET gegen die Gitea-API (mit GITEA_TOKEN). Wirft bei Fehlern, ohne GITEA_API_BASE `NichtEingerichtet`."""
    token = os.getenv("GITEA_TOKEN", "")
    req = urllib.request.Request(
        f"{gitea_api()}{pfad}", headers={"Authorization": f"token {token}"} if token else {}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def prometheus(pfad: str, params: dict[str, Any], timeout: float = 8) -> Any:
    basis = prometheus_url()
    if not basis:
        raise NichtEingerichtet("Prometheus", "Setze PROMETHEUS_URL (z. B. http://prometheus.example.org:9090).")
    u = f"{basis}{pfad}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(u, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    if d.get("status") != "success":
        raise RuntimeError(f"Prometheus: {d.get('error') or d.get('status')}")
    return d["data"]


def _alle_fehlen(was: str, fehlt: dict[str, NichtEingerichtet]) -> dict[str, Any]:
    """Antwort eines lesenden Werkzeugs, dessen Quellen alle nicht eingerichtet sind: ein Zustand, der sie nennt."""
    antwort = _umgebung.zustand(was, " ".join(f"{n}: {e.einrichten}" for n, e in fehlt.items()))
    antwort["nicht_eingerichtet"] = [{"quelle": n, "einrichten": e.einrichten} for n, e in fehlt.items()]
    return antwort


def _uhr_fehlt(art: str, name: str, was: str, einrichten: str) -> dict[str, Any]:
    """Eine Uhr der Fruehwarnung, für die nichts eingerichtet ist (kein Ausfall: ein eigener Zustand)."""
    return {
        "art": art, "name": name, "zustand": _umgebung.NICHT_EINGERICHTET,
        "text": f"{was} ist nicht eingerichtet", "einrichten": einrichten,
    }


class _Zwischenspeicher:
    """Kleiner TTL-Cache je Schluessel — die frühere Route cachte genauso (Ampel 30 s, Nutzung 5 min)."""

    # Hoechstzahl der Eintraege (2026-10-05). Vorher wuchs der Speicher ohne Raeumung: Schluessel mit Freitext
    # (`suche:{q}`, `prom:{q}`, `ereignisse:{ns}:{feld}`) liessen ihn mit jeder neuen Eingabe wachsen, auch
    # lange nach Ablauf der Frist. Beim Einlegen fliegen zuerst die abgelaufenen raus, danach die aeltesten.
    MAX_EINTRAEGE = 256

    def __init__(self, max_eintraege: int | None = None) -> None:
        self._d: dict[str, tuple[float, Any]] = {}
        self._ttl: dict[str, float] = {}  # Frist je Schluessel — fuer die Raeumung abgelaufener Eintraege
        self._max = max_eintraege or self.MAX_EINTRAEGE
        self._lock = threading.Lock()

    def holen(self, key: str, ttl: float, laden: Callable[[], Any]) -> Any:
        jetzt = time.monotonic()
        with self._lock:
            e = self._d.get(key)
            if e and jetzt - e[0] < ttl:
                return e[1]
        wert = laden()
        with self._lock:
            self._d.pop(key, None)  # neu einreihen: dict-Reihenfolge = Alter
            self._d[key] = (jetzt, wert)
            self._ttl[key] = ttl
            if len(self._d) > self._max:
                self._raeumen(jetzt)
        return wert

    def _raeumen(self, jetzt: float) -> None:
        """Abgelaufene Eintraege entfernen, danach die aeltesten, bis `_max` gilt (Sperre gehalten)."""
        for k in [k for k, (t, _w) in self._d.items() if jetzt - t >= self._ttl.get(k, 0.0)]:
            self._d.pop(k, None)
            self._ttl.pop(k, None)
        while len(self._d) > self._max:
            aeltester = next(iter(self._d))
            self._d.pop(aeltester, None)
            self._ttl.pop(aeltester, None)

    def vergessen(self, key: str) -> None:
        with self._lock:
            self._d.pop(key, None)
            self._ttl.pop(key, None)


class LagePlugin(HubPlugin):
    name = "lage"
    description = "Lagebild: Ampel, Kennzahlen-Kurven, Nutzung der Flaechen, Fristen, Chronik, Fenster."
    version = "0.1.0"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        self._cache = _Zwischenspeicher()

    def register(self) -> None:
        return None

    @staticmethod
    def _quellen_fehlen() -> list[dict[str, str]]:
        """Die Quellen des Lagebilds, für die nichts eingerichtet ist: [{quelle, einrichten}]."""
        fehlt = []
        if not redis_url():
            fehlt.append({"quelle": "Redis", "einrichten": "Setze REDIS_URL (z. B. redis://redis.example.org:6379)."})
        if not _umgebung.lesen("GITEA_API_BASE") or not _umgebung.lesen("GITEA_REPO"):
            fehlt.append({"quelle": "Git-API", "einrichten": "Setze GITEA_API_BASE, GITEA_REPO und GITEA_TOKEN."})
        if not prometheus_url():
            fehlt.append({"quelle": "Prometheus", "einrichten": "Setze PROMETHEUS_URL (z. B. http://prometheus.example.org:9090)."})
        return fehlt

    def health(self) -> dict[str, Any]:
        fehlt = self._quellen_fehlen()
        if len(fehlt) == 3:  # nichts eingerichtet: weder gesund noch krank
            return {
                "ok": True, "status": _umgebung.NICHT_EINGERICHTET,
                "einrichten": "; ".join(f"{f['quelle']}: {f['einrichten']}" for f in fehlt),
            }
        if not redis_url():
            return {"ok": True, "status": "ok", "nicht_eingerichtet": [f["quelle"] for f in fehlt]}
        try:
            _redis().ping()
            return {"ok": True, "status": "ok", "nicht_eingerichtet": [f["quelle"] for f in fehlt]} if fehlt else {"ok": True, "status": "ok"}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "status": "unhealthy", "reason": f"Redis: {e}"}

    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="ampel",
                title="System-Ampel",
                description=(
                    "Gesamtzustand von FYOS: gruen (Produktion), blau (online, mit Problemen), "
                    "dev/debug (Fenster offen), gelb (Wartung), rot (CI auf HEAD rot). Mit "
                    "Begruendungen (`warum`) und offenen Wartungsfenstern. Ist eine Quelle nicht eingerichtet, "
                    "steht sie unter `nicht_eingerichtet` (mit Weg zum Einrichten) und die Ampel ist nie gruen."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="metrik",
                title="Kennzahl-Kurve",
                # 2026-10-04: abgelöst durch verlauf (keine feste Liste, alle Rechner/GPUs/Dienste, eigene
                # Kurven per verlauf.merken). Bleibt, solange es genutzt wird; das Bestandsbuch meldet „ruht".
                description=(
                    "Veraltet — nimm verlauf.zustand (alle Kurven) oder verlauf.kurve. "
                    f"Kurve einer festen Kennzahl aus Prometheus (24 Punkte). Schluessel: {', '.join(METRIKEN)}."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string", "enum": list(METRIKEN)},
                        "stunden": {"type": "integer", "minimum": 1, "maximum": 168, "default": 24},
                    },
                    "required": ["key"],
                    "additionalProperties": False,
                },
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="fristen",
                title="Fruehwarnung",
                description=(
                    "Fruehwarn-Uhren: Zertifikats-Ablauf, Plattenfuellstand mit 30-Tage-Trend, "
                    "letzter erfolgreicher Lauf jedes Backup-CronJobs. Zustand ok/knapp/kritisch/unbekannt; "
                    "ein Teil ohne Einstellung (FRIST_ZERT_HOSTS, PROMETHEUS_URL) steht als „nicht eingerichtet“ da."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="anwesenheit",
                title="Wer ist dran",
                description="Wer in den letzten Stunden ins Repo committet hat (Menschen und Agenten), neueste zuerst.",
                input_schema={
                    "type": "object",
                    "properties": {"stunden": {"type": "integer", "minimum": 1, "maximum": 24, "default": 4}},
                    "additionalProperties": False,
                },
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="chronik",
                title="Chronik",
                description="Zeitstrahl der letzten Stunden: Commits (mit CI-Stand), Laeufe des Agenten, Einsichten, Vorfaelle.",
                input_schema={
                    "type": "object",
                    "properties": {"stunden": {"type": "integer", "minimum": 1, "maximum": 72, "default": 24}},
                    "additionalProperties": False,
                },
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="suche",
                title="Suche",
                description="Suche ueber Aufgaben, Tickets und Einsichten; je Quelle bis zu 5 Treffer mit Ziel-Link.",
                input_schema={
                    "type": "object",
                    "properties": {"q": {"type": "string", "minLength": 2}},
                    "required": ["q"],
                    "additionalProperties": False,
                },
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="nutzung",
                title="Nutzung der Flaechen",
                description="Verweildauer je fyos-web-Flaeche (Stunden, Anteil, Besuche) und haeufigste Wechsel.",
                input_schema={
                    "type": "object",
                    "properties": {"tage": {"type": "integer", "minimum": 1, "maximum": 90, "default": 30}},
                    "additionalProperties": False,
                },
                tags=_READ_WEB,
            ),
            ToolDescriptor(
                name="fenster_setzen",
                title="Fenster oeffnen/schliessen",
                description=(
                    "Dev-/Test-/Debug-/Wartungsfenster fuer einen Knoten oeffnen (grund Pflicht, "
                    "hoechstens 7 Tage) oder mit aus=true schliessen. Zieht die ganze Kette von "
                    "scripts/ops/fenster.py: ConfigMap fyos/fenster, Waechter leiser (wartung.py), "
                    "CI zieht auf einen anderen Knoten um. Antwortet mit der echten Skriptausgabe. Ohne "
                    "FYOS_REPO_PATH (Klon mit dem Skript) nicht eingerichtet: ok=false und der Weg dahin."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "knoten": _knoten_schema(),
                        "aus": {"type": "boolean", "default": False, "description": "Fenster schliessen"},
                        "art": {"type": "string", "enum": FENSTER_ARTEN, "default": "dev"},
                        "grund": {"type": "string", "maxLength": 400},
                        "minuten": {"type": "integer", "minimum": 1, "maximum": FENSTER_MAX_MINUTEN, "default": 720},
                        "bis": {"type": "string", "description": "Ende als ISO-Zeitpunkt statt minuten, z. B. 2026-10-01T06:00"},
                        "dienst": {"type": "string", "maxLength": 300, "description": "betroffene Dienste"},
                        "von": {"type": "string", "maxLength": 40, "default": "fyos-web"},
                    },
                    "additionalProperties": False,
                },
                tags=_WRITE_WEB,
                # fenster.py zieht die CI um (subprocess-Limit 240 s); der Hub-Rahmen liegt knapp darueber.
                zeitlimit_s=270,
            ),
        ]

    def call(self, method: str, args: dict[str, Any]) -> Any:
        try:
            return self._aufruf(method, args)
        except NichtEingerichtet as e:  # Quelle ohne Einstellung: eigener Zustand mit Weg zum Einrichten, kein Ausfall
            return e.schreibend() if method == "fenster_setzen" else e.lesend()

    def _aufruf(self, method: str, args: dict[str, Any]) -> Any:
        if method == "ampel":
            return self._cache.holen("ampel", 30, self._ampel)
        if method == "metrik":
            key = args.get("key")
            if key not in METRIKEN:
                raise ValueError(f"unbekannter Schluessel {key!r}; erlaubt: {', '.join(METRIKEN)}")
            stunden = max(1, min(int(args.get("stunden") or 24), 168))
            return self._cache.holen(f"metrik:{key}:{stunden}", 60, lambda: self._metrik(key, stunden))
        if method == "fristen":
            return self._cache.holen("fristen", 600, self._fristen)
        if method == "anwesenheit":
            stunden = max(1, min(int(args.get("stunden") or 4), 24))
            return self._cache.holen(f"anwesenheit:{stunden}", 120, lambda: self._anwesenheit(stunden))
        if method == "chronik":
            stunden = max(1, min(int(args.get("stunden") or 24), 72))
            return self._cache.holen(f"chronik:{stunden}", 120, lambda: self._chronik(stunden))
        if method == "suche":
            q = str(args.get("q") or "").strip()
            if len(q) < 2:
                return {"treffer": [], "hinweis": "mindestens 2 Zeichen", "fehler": []}
            return self._cache.holen(f"suche:{q.lower()}", 60, lambda: self._suche(q))
        if method == "nutzung":
            tage = max(1, min(int(args.get("tage") or 30), 90))
            return self._cache.holen(f"nutzung:{tage}", 300, lambda: self._nutzung(tage))
        if method == "fenster_setzen":
            ergebnis = fenster_setzen(args)
            self._cache.vergessen("ampel")  # sonst zeigt die Ampel bis zu 30 s den alten Stand
            return ergebnis
        raise ValueError(f"lage hat kein Werkzeug {method!r}")

    # ── Ampel ────────────────────────────────────────────────────────────────
    def _ampel(self) -> dict[str, Any]:
        zustand = "gruen"
        warum: list[str] = []
        fenster: list[dict[str, Any]] = []
        unvollstaendig = False
        fehlt: dict[str, NichtEingerichtet] = {}

        def hoeher(z: str) -> None:
            nonlocal zustand
            if _RANG.get(z, 0) > _RANG.get(zustand, 0):
                zustand = z

        # (1) Wartungsfenster: fyos:wartung:<knoten> (geschrieben von scripts/ops/fenster.py)
        try:
            r = _redis()
            for k in r.scan_iter(match="fyos:wartung:*", count=100):
                if ":spur:" in k:
                    continue
                roh = r.get(k)
                if not roh:
                    continue
                try:
                    e = json.loads(roh)
                except ValueError:
                    e = {"knoten": k.rsplit(":", 1)[-1], "grund": "(unlesbar)"}
                rest = r.ttl(k)
                e["_rest_min"] = int(rest // 60) if isinstance(rest, int) and rest > 0 else None
                g = str(e.get("grund", ""))
                if g.startswith("[") and "]" in g:
                    e["art"] = g[1 : g.index("]")]
                    e["grund"] = g[g.index("]") + 1 :].strip()
                hoeher(_ART_ZUSTAND.get(str(e.get("art", "")), "gelb"))
                dauer = f", noch {e['_rest_min']} min" if e["_rest_min"] is not None else ""
                warum.append(f"Fenster {e.get('knoten', '?')} ({e.get('art') or 'wartung'}): {str(e.get('grund', ''))[:110]}{dauer}")
                fenster.append(e)
        except NichtEingerichtet as e:
            unvollstaendig = True
            fehlt[e.was] = e
            warum.append(f"Wartungszustand: {e.was} ist nicht eingerichtet")
        except Exception as e:  # noqa: BLE001
            unvollstaendig = True
            warum.append(f"Wartungszustand nicht lesbar ({type(e).__name__})")

        # (2) CI auf HEAD
        try:
            head = gitea(f"/repos/{repo()}/commits?limit=1")[0]["sha"]
            st = gitea(f"/repos/{repo()}/commits/{head}/status").get("state") or ""
            if st in ("failure", "error"):
                hoeher("rot")
                warum.append(f"CI rot auf {head[:9]}")
            elif st == "pending":
                warum.append(f"CI laeuft ({head[:9]})")
        except NichtEingerichtet as e:
            unvollstaendig = True
            fehlt[e.was] = e
            warum.append(f"CI-Status: {e.was} ist nicht eingerichtet")
        except Exception as e:  # noqa: BLE001
            unvollstaendig = True
            warum.append(f"CI-Status unklar ({type(e).__name__})")

        # (3) Fehler-Bus: Fehler der letzten Stunde → blau; Warnungen nur nennen.
        # App-Prüfung 06.10.: vorher „die letzten 30 Einträge“ ohne Zeitfenster und mit Warnungen — die Ampel
        # stand dadurch dauerhaft auf „30 Fehler-Ereignisse“, während Live 0 % Fehlerquote zeigte.
        try:
            fehler = warnungen = 0
            seit_id = f"{int((time.time() - FEHLER_FENSTER_S) * 1000)}-0"
            for _id, f in _redis().xrevrange("fyos:events:errors", max="+", min=seit_id, count=500):
                sev = f.get("severity") or ""
                if not sev and f.get("data"):
                    try:
                        sev = str(json.loads(f["data"]).get("severity") or "")
                    except ValueError:
                        pass
                if sev in ("error", "critical"):
                    fehler += 1
                elif sev in ("warn", "warning"):
                    warnungen += 1
            if fehler:
                hoeher("blau")
                warum.append(f"{fehler} Fehler in der letzten Stunde" + (f", dazu {warnungen} Warnungen" if warnungen else ""))
            elif warnungen >= WARNUNGEN_SCHWELLE:
                warum.append(f"{warnungen} Warnungen in der letzten Stunde")
        except NichtEingerichtet as e:
            unvollstaendig = True
            fehlt[e.was] = e
            warum.append(f"Fehler-Bus: {e.was} ist nicht eingerichtet")
        except Exception as e:  # noqa: BLE001
            unvollstaendig = True
            warum.append(f"Fehler-Bus nicht lesbar ({type(e).__name__})")

        # Eine Luecke in den Quellen ist keine gute Nachricht — auch eine nicht eingerichtete Quelle ist eine Luecke.
        if unvollstaendig:
            hoeher("blau")
        antwort: dict[str, Any] = {
            "zustand": zustand, "warum": warum, "fenster": fenster, "vollstaendig": not unvollstaendig, "seit": time.time(),
        }
        if fehlt:
            antwort["nicht_eingerichtet"] = [{"quelle": e.was, "einrichten": e.einrichten} for e in fehlt.values()]
            if len(fehlt) == 2:  # weder Redis noch Git-API: die Ampel hat nichts, woraus sie etwas sagen könnte
                antwort["eingerichtet"] = False
                antwort["satz"] = "Die Ampel hat keine Quelle: Redis und die Git-API sind nicht eingerichtet."
                antwort["einrichten"] = " ".join(e.einrichten for e in fehlt.values())
        return antwort

    # ── Metrik ───────────────────────────────────────────────────────────────
    def _metrik(self, key: str, stunden: int) -> dict[str, Any]:
        ende = int(time.time())
        start = ende - stunden * 3600
        schritt = max(60, stunden * 3600 // 24)
        d = prometheus("/api/v1/query_range", {"query": METRIKEN[key], "start": start, "end": ende, "step": schritt})
        reihe = (d.get("result") or [{}])[0].get("values") or []
        werte_je_t = {int(float(t)): float(w) for t, w in reihe}
        punkte = []
        for t in range(start + schritt, ende + 1, schritt):
            naechster = min(werte_je_t, key=lambda x: abs(x - t)) if werte_je_t else None
            wert = werte_je_t[naechster] if naechster is not None and abs(naechster - t) <= schritt else None
            punkte.append({"t": t, "w": wert})
        werte = [p["w"] for p in punkte if p["w"] is not None]
        return {
            "key": key,
            "stunden": stunden,
            "punkte": punkte,
            "aktuell": werte[-1] if werte else None,
            "min": min(werte) if werte else None,
            "max": max(werte) if werte else None,
        }

    # ── Nutzung ──────────────────────────────────────────────────────────────
    def _nutzung(self, tage: int) -> dict[str, Any]:
        summen: dict[str, float] = {}
        besuche: dict[str, int] = {}
        spruenge: dict[str, int] = {}
        letzte: str | None = None
        gesehen: set[tuple[str, str]] = set()
        grenze = time.strftime("%Y-%m-%dT%H:%M", time.gmtime(time.time() - tage * 86400))
        r = _redis()
        cursor = "-"
        while True:
            eintraege = r.xrange("fyos:pane:dwell", min=cursor, count=1000)
            if not eintraege:
                break
            cursor = "(" + str(eintraege[-1][0])
            for _id, f in eintraege:
                pane = f.get("pane") or "?"
                start = str(f.get("start") or "")
                if start and start < grenze:
                    continue
                if (pane, start) in gesehen:  # mehrere Tabs melden denselben Aufenthalt
                    continue
                gesehen.add((pane, start))
                try:
                    dauer = float(f.get("dauer_ms") or 0) / 1000.0
                except ValueError:
                    dauer = 0.0
                summen[pane] = summen.get(pane, 0.0) + dauer
                besuche[pane] = besuche.get(pane, 0) + 1
                if letzte and letzte != pane:
                    k = f"{letzte} -> {pane}"
                    spruenge[k] = spruenge.get(k, 0) + 1
                letzte = pane
            if len(eintraege) < 1000:
                break
        gesamt = sum(summen.values())
        return {
            "tage": tage,
            "gesamt_stunden": round(gesamt / 3600, 2),
            "flaechen": [
                {"pane": p, "stunden": round(s / 3600, 2), "anteil": round(s / gesamt * 100, 1) if gesamt else 0, "besuche": besuche.get(p, 0)}
                for p, s in sorted(summen.items(), key=lambda kv: -kv[1])
            ][:20],
            "spruenge": sorted(spruenge.items(), key=lambda kv: -kv[1])[:10],
            "fehler": [],
        }

    # ── Fristen ──────────────────────────────────────────────────────────────
    @staticmethod
    def _stufe(rest_tage: float, knapp: float, kritisch: float) -> str:
        return "ok" if rest_tage > knapp else "knapp" if rest_tage > kritisch else "kritisch"

    def _zertifikat(self, host: str) -> dict[str, Any]:
        import socket
        import ssl
        import subprocess
        import tempfile
        from email.utils import parsedate_to_datetime

        try:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            # Mit FRIST_CADDY direkt an den Proxy davor (mit SNI, wenn der Weg ueber die Adresse des Hosts im Egress
            # des Hubs gesperrt ist), sonst an den Host selbst.
            with socket.create_connection((_umgebung.lesen("FRIST_CADDY") or host, 443), timeout=6) as roh:
                with ctx.wrap_socket(roh, server_hostname=host) as s:
                    pem = ssl.DER_cert_to_PEM_cert(s.getpeercert(binary_form=True))
            with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
                f.write(pem)
                f.flush()
                r = subprocess.run(["openssl", "x509", "-in", f.name, "-noout", "-enddate"],
                                   capture_output=True, text=True, timeout=8, check=True)
            ablauf = parsedate_to_datetime(r.stdout.split("=", 1)[1].strip())
            rest = (ablauf.timestamp() - time.time()) / 86400
            return {"art": "zertifikat", "name": host, "zustand": self._stufe(rest, 14, 3), "text": f"{rest:.1f} Tage", "in_tagen": round(rest, 1)}
        except Exception as e:  # noqa: BLE001
            return {"art": "zertifikat", "name": host, "zustand": "unbekannt", "text": f"nicht lesbar ({type(e).__name__})"}

    def _platten(self) -> list[dict[str, Any]]:
        sel = "fstype!~'tmpfs|overlay',mountpoint='/'"

        def belegt(versatz: str = "") -> str:
            # PromQL: `offset` gehoert an jeden Selektor, nicht hinter den ganzen Ausdruck
            # (so stand es zuerst hier — Prometheus antwortete mit 400).
            o = f" offset {versatz}" if versatz else ""
            return f"100 * (1 - node_filesystem_avail_bytes{{{sel}}}{o} / node_filesystem_size_bytes{{{sel}}}{o})"

        try:
            jetzt = prometheus("/api/v1/query", {"query": belegt()})["result"]
            vorher = prometheus("/api/v1/query", {"query": belegt("30d")})["result"]
        except NichtEingerichtet as e:
            return [_uhr_fehlt("platte", "Platten", e.was, e.einrichten)]
        except Exception as e:  # noqa: BLE001
            return [{"art": "platte", "name": "Platten", "zustand": "unbekannt", "text": f"Prometheus nicht lesbar ({type(e).__name__})"}]
        alt = {x["metric"].get("instance"): float(x["value"][1]) for x in vorher}
        uhren = []
        for x in jetzt[:8]:
            inst = x["metric"].get("instance") or "?"
            voll = float(x["value"][1])
            a = alt.get(inst)
            if a is not None and voll > a and voll < 100:
                bis_voll = (100 - voll) / ((voll - a) / 30.0)
                text = f"{voll:.0f}% belegt, voll in ~{int(bis_voll)} d (Trend 30 d)"
                in_tagen = round(bis_voll, 1)
                zustand = "kritisch" if bis_voll < 7 or voll > 95 else "knapp" if bis_voll < 30 or voll > 85 else "ok"
            else:
                text = f"{voll:.0f}% belegt"
                in_tagen = None
                zustand = "kritisch" if voll > 95 else "knapp" if voll > 85 else "ok"
            uhr = {"art": "platte", "name": f"{inst.split(':')[0]} /", "zustand": zustand, "text": text}
            if in_tagen is not None:
                uhr["in_tagen"] = in_tagen  # Zahl fuer leitstand.sammel (naechste Frist)
            uhren.append(uhr)
        return uhren

    def _backups(self) -> list[dict[str, Any]]:
        try:
            from kubernetes import client, config

            config.load_incluster_config()
            jobs = client.BatchV1Api().list_namespaced_cron_job(
                _umgebung.lesen("BETRIEB_NAMESPACE") or _umgebung.NAMESPACE_VORGABE,
                label_selector=_umgebung.lesen("FRIST_BACKUP_LABEL") or BACKUP_LABEL_VORGABE,
            ).items
        except Exception as e:  # noqa: BLE001
            return [{"art": "backup", "name": "Backups", "zustand": "unbekannt", "text": f"CronJobs nicht lesbar ({type(e).__name__})"}]
        uhren = []
        for cj in jobs:
            if cj.spec.suspend:
                continue
            letzte = cj.status.last_successful_time if cj.status else None
            if letzte is None:
                uhren.append({"art": "backup", "name": cj.metadata.name, "zustand": "kritisch", "text": "noch nie erfolgreich"})
                continue
            alter_h = (time.time() - letzte.timestamp()) / 3600
            zustand = "ok" if alter_h < 36 else "knapp" if alter_h < 72 else "kritisch"
            uhren.append({"art": "backup", "name": cj.metadata.name, "zustand": zustand, "text": f"zuletzt erfolgreich vor {alter_h:.0f} h"})
        return sorted(uhren, key=lambda u: ["kritisch", "knapp", "unbekannt", "ok"].index(u["zustand"]))

    def _fristen(self) -> dict[str, Any]:
        hosts = _umgebung.liste("FRIST_ZERT_HOSTS")
        zertifikate = (
            [self._zertifikat(h) for h in hosts]
            if hosts
            else [_uhr_fehlt("zertifikat", "Zertifikate", "Die Zertifikats-Uhr", "Setze FRIST_ZERT_HOSTS (kommagetrennte Hostnamen, z. B. www.example.org).")]
        )
        uhren = zertifikate + self._platten() + self._backups()
        antwort: dict[str, Any] = {"uhren": uhren}
        fehlt = [u for u in uhren if u["zustand"] == _umgebung.NICHT_EINGERICHTET]
        if fehlt:
            antwort["nicht_eingerichtet"] = [{"uhr": u["name"], "einrichten": u["einrichten"]} for u in fehlt]
        if len([u for u in fehlt if u["art"] in ("zertifikat", "platte")]) == 2:  # beide per Einstellung gesteuerten Uhren
            antwort.update(_umgebung.zustand("Die Fruehwarnung", " ".join(u["einrichten"] for u in fehlt)))
        return antwort

    # ── Anwesenheit ──────────────────────────────────────────────────────────
    def _anwesenheit(self, stunden: int) -> dict[str, Any]:
        from datetime import datetime

        def seit(ts: float) -> str:
            s = time.time() - ts
            return "jetzt" if s < 90 else f"vor {int(s // 60)} min" if s < 5400 else f"vor {int(s // 3600)} h"

        fehler: list[str] = []
        autoren: dict[str, float] = {}
        try:
            for c in gitea(f"/repos/{repo()}/commits?limit=50"):
                datum = ((c.get("commit") or {}).get("author") or {}).get("date") or ""
                try:
                    ts = datetime.fromisoformat(datum.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
                if time.time() - ts > stunden * 3600:
                    continue
                name = (c.get("author") or {}).get("login") or ((c.get("commit") or {}).get("author") or {}).get("name") or "?"
                autoren[name] = max(autoren.get(name, 0), ts)
        except NichtEingerichtet as e:
            return e.lesend()
        except Exception as e:  # noqa: BLE001
            fehler.append(f"Gitea: {type(e).__name__}: {e}")
        return {
            "hände": [{"name": n, "spur": "committet", "zuletzt": seit(ts)} for n, ts in sorted(autoren.items(), key=lambda kv: -kv[1])],
            "fehler": fehler,
        }

    # ── Chronik ──────────────────────────────────────────────────────────────
    def _chronik(self, stunden: int) -> dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor

        seit = time.time() - stunden * 3600
        fehler: list[str] = []

        def commits() -> list[dict[str, Any]]:
            out = []
            liste = [c for c in gitea(f"/repos/{repo()}/commits?limit=50") if (_iso(((c.get("commit") or {}).get("author") or {}).get("date")) or 0) >= seit]
            for i, c in enumerate(liste):
                ci = None
                if i < 20:  # CI-Stand nur fuer die juengsten 20 (früher: bis zu 50 Einzelabrufe)
                    try:
                        ci = gitea(f"/repos/{repo()}/commits/{c['sha']}/status", timeout=5).get("state")
                    except Exception:  # noqa: BLE001 — CI unbekannt ist ein Detail, kein Ausfall
                        ci = None
                autor = (c.get("author") or {}).get("login") or ((c.get("commit") or {}).get("author") or {}).get("name") or "?"
                out.append({
                    "art": "commit",
                    "ts": _iso(((c.get("commit") or {}).get("author") or {}).get("date")),
                    "wer": str(autor)[:20],
                    "text": f"{(c.get('sha') or '')[:9]} {((c.get('commit') or {}).get('message') or '').splitlines()[0][:80] if (c.get('commit') or {}).get('message') else ''}",
                    "detail": f"CI: {ci}" if ci else None,
                })
            return out

        def daemons() -> list[dict[str, Any]]:
            out = []
            for a in dienst("agent", "/nexus/agents").get("agents", []):
                ts = _iso(a.get("last_run")) if a.get("last_run") not in (None, "never") else None
                if not ts or ts < seit:
                    continue
                out.append({"art": "daemon", "ts": ts, "wer": str(a.get("name", "?"))[:28], "text": f"Lauf {a.get('status') or '?'}",
                            "detail": f"{a.get('duration_s'):.0f}s" if a.get("duration_s") else None})
            return out

        def insights() -> list[dict[str, Any]]:
            d = dienst("einsichten", "/api/insights/recent")
            out = []
            for i in d if isinstance(d, list) else d.get("items") or []:
                ts = _iso(i.get("generated_at"))
                if ts and ts >= seit:
                    out.append({"art": "insight", "ts": ts, "wer": "agent", "text": str(i.get("title", ""))[:90], "detail": i.get("category")})
            return out

        def vorfaelle() -> list[dict[str, Any]]:
            out = []
            for _id, f in _redis().xrevrange("fyos:events:errors", count=100):
                daten: dict[str, Any] = {}
                if f.get("data"):
                    try:
                        daten = json.loads(f["data"])
                    except ValueError:
                        pass
                try:
                    ts = float(f.get("ts") or daten.get("ts") or 0)
                except (TypeError, ValueError):
                    ts = 0
                sev = str(f.get("severity") or daten.get("severity") or "").lower()
                if ts >= seit and sev in ("error", "critical"):
                    out.append({"art": "vorfall", "ts": ts, "wer": str(f.get("source") or daten.get("source") or "?")[:24],
                                "text": str(daten.get("message") or f.get("message") or "")[:90], "detail": sev})
            return out

        quellen = {"Commits": commits, "Daemons": daemons, "Insights": insights, "Vorfaelle": vorfaelle}
        ereignisse: list[dict[str, Any]] = []
        fehlt: dict[str, NichtEingerichtet] = {}
        with ThreadPoolExecutor(max_workers=4) as pool:
            laeufe = {name: pool.submit(fn) for name, fn in quellen.items()}
            for name, lauf in laeufe.items():
                try:
                    ereignisse.extend(lauf.result())
                except NichtEingerichtet as e:  # keine Einstellung: kein Ausfall, sondern ein eigener Zustand
                    fehlt[name] = e
                except Exception as e:  # noqa: BLE001 — eine Quelle weg heisst nicht „nichts passiert"
                    fehler.append(f"{name}: {type(e).__name__}: {e}"[:160])
        if len(fehlt) == len(quellen):
            return _alle_fehlen("Die Chronik", fehlt)
        ereignisse = sorted([e for e in ereignisse if e.get("ts")], key=lambda e: -e["ts"])[:200]
        antwort = {"stunden": stunden, "ereignisse": ereignisse, "anzahl": len(ereignisse), "fehler": fehler}
        if fehlt:
            antwort["nicht_eingerichtet"] = [{"quelle": n, "einrichten": e.einrichten} for n, e in fehlt.items()]
        return antwort

    # ── Suche ────────────────────────────────────────────────────────────────
    def _suche(self, q: str) -> dict[str, Any]:
        from concurrent.futures import ThreadPoolExecutor

        ql = q.lower()

        def aufgaben() -> list[dict[str, Any]]:
            d = dienst("aufgaben", "/api/life/tasks?limit=200")
            items = d if isinstance(d, list) else d.get("items") or d.get("tasks") or []
            return [{"typ": "task", "titel": str(t.get("title") or "?")[:80], "ziel": f"/life/tasks?ref={t.get('id', '')}", "sub": str(t.get("status") or "")[:20]}
                    for t in items if ql in str(t.get("title", "")).lower() or ql in str(t.get("notes") or "").lower()][:5]

        def tickets() -> list[dict[str, Any]]:
            # Ticketdienst oder Workboard des Agenten; ohne Quelle keine Treffer (kein Fehler)
            items = vorgaenge.quelle().liste(grenze=vorgaenge.GRENZE_MAX)
            return [{"typ": "ticket", "titel": f"{t.get('id', '?')} {str(t.get('title') or '')[:60]}", "ziel": vorgang_ziel(t.get("id")), "sub": str(t.get("status") or "")[:20]}
                    for t in items if ql in str(t.get("title", "")).lower() or ql in str(t.get("id", "")).lower()][:5]

        def einsichten() -> list[dict[str, Any]]:
            d = dienst("einsichten", "/api/insights/recent")
            items = d if isinstance(d, list) else d.get("items") or []
            return [{"typ": "insight", "titel": str(i.get("title") or "")[:90], "ziel": "/admin/observability", "sub": str(i.get("category") or "")[:20]}
                    for i in items if ql in str(i.get("title", "")).lower()][:5]

        treffer: list[dict[str, Any]] = []
        fehler: list[str] = []
        fehlt: dict[str, NichtEingerichtet] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            laeufe = {"Aufgaben": pool.submit(aufgaben), "Tickets": pool.submit(tickets), "Insights": pool.submit(einsichten)}
            for name, lauf in laeufe.items():
                try:
                    treffer.extend(lauf.result())
                except NichtEingerichtet as e:
                    fehlt[name] = e
                except Exception as e:  # noqa: BLE001 — eine Quelle weg heisst nicht „keine Treffer"
                    fehler.append(f"{name}: {type(e).__name__}")
        # Tickets haben eine eigene Quelle des Hubs und melden „nicht eingerichtet" nie als Ausnahme: mit Aufgaben und
        # Einsichten ohne Adresse und ohne Treffer ist die Suche ohne Quelle.
        if {"Aufgaben", "Insights"} <= set(fehlt) and not treffer and not fehler:
            return _alle_fehlen("Die Suche", fehlt)
        antwort = {"treffer": treffer, "anzahl": len(treffer), "fehler": fehler}
        if fehlt:
            antwort["nicht_eingerichtet"] = [{"quelle": n, "einrichten": e.einrichten} for n, e in fehlt.items()]
        return antwort


# ── Fenster setzen ───────────────────────────────────────────────────────────
def fenster_argv(args: dict[str, Any], skript: str) -> list[str]:
    """Die Argumente gegen feste Listen pruefen und in den fenster.py-Aufruf uebersetzen.

    Hier wird NICHT nachgebaut, was fenster.py kann (Messen, ConfigMap, Doku, wartung.py,
    Bauten umziehen) — wie frueher ruft der Hub das Skript aus seinem Repo-Klon.
    """
    erlaubt = fenster_knoten()
    if not erlaubt:
        raise NichtEingerichtet(
            "Die Knoten für Fenster",
            "Setze FYOS_FENSTER_KNOTEN (kommagetrennt, der erste ist der Standardknoten; z. B. knoten-a,knoten-b).",
        )
    knoten = str(args.get("knoten") or erlaubt[0]).strip()
    if knoten not in erlaubt:
        raise ValueError(f"knoten muss eines sein von {erlaubt}")
    if args.get("aus"):
        return ["python3", skript, "aus", knoten]
    art = str(args.get("art") or "dev").strip()
    if art not in FENSTER_ARTEN:
        raise ValueError(f"art muss eines sein von {FENSTER_ARTEN}")
    grund = str(args.get("grund") or "").strip()
    if not grund:
        raise ValueError("grund fehlt — ein Fenster ohne Grund ist nicht nachvollziehbar")
    if args.get("bis"):
        # Das Ende rechnet fenster.py selbst um (Zeitzone, Vergangenheit, 7-Tage-Grenze);
        # hier nur sicherstellen, dass es ein Zeitpunkt ist und keine Option.
        bis = str(args["bis"]).strip()
        try:
            dt.datetime.fromisoformat(bis)
        except ValueError as e:
            raise ValueError(f"bis {bis!r} ist kein ISO-Zeitpunkt") from e
        dauer = ["--bis", bis]
    else:
        try:
            minuten = int(args.get("minuten") or 720)
        except (TypeError, ValueError) as e:
            raise ValueError("minuten muss eine Zahl sein") from e
        if not 1 <= minuten <= FENSTER_MAX_MINUTEN:
            raise ValueError(f"minuten muss zwischen 1 und {FENSTER_MAX_MINUTEN} liegen (7 Tage)")
        dauer = ["--minuten", str(minuten)]
    argv = ["python3", skript, "an", knoten, *dauer, "--grund", grund[:400],
            "--art", art, "--von", str(args.get("von") or "fyos-web")[:40]]
    if args.get("dienst"):
        argv += ["--dienst", str(args["dienst"])[:300]]
    return argv


def fenster_setzen(args: dict[str, Any]) -> dict[str, Any]:
    klon = _umgebung.lesen("FYOS_REPO_PATH")
    skript = os.path.join(klon or ".", "scripts", "ops", "fenster.py")
    argv = fenster_argv(args, skript)  # prüft die Eingaben zuerst: ein falsches Argument bleibt ein ValueError
    if not klon:
        raise NichtEingerichtet(
            "Der Repo-Klon mit dem Fenster-Skript",
            "Setze FYOS_REPO_PATH (Pfad eines Klons mit scripts/ops/fenster.py).",
        )
    if not os.path.exists(skript):
        raise RuntimeError(f"fenster.py nicht da ({skript}) — Repo-Klon fehlt")
    r = subprocess.run(argv, capture_output=True, text=True, timeout=240, cwd=klon)
    ausgabe = ((r.stdout or "") + (r.stderr or "")).strip()
    if r.returncode != 0:
        # Kein stilles ok: sonst zeigt die Ampel einen Zustand, den niemand hergestellt hat.
        raise RuntimeError(f"fenster.py rc={r.returncode}: {ausgabe[-600:]}")
    return {"aktion": "aus" if args.get("aus") else "an", "knoten": argv[3], "ausgabe": ausgabe[-2000:]}
