"""pipelines — Datenquellen und Pipelines mit Status (2026-10-06, App-Prüfung Runde 2).

Quelle ist die API der früheren Fläche Pipelines (fyos-web, am 2026-10-08 abgebaut): der Aufgabendienst
`GET /api/life/pipelines` (Adresse FYOS_AUFGABEN_URL; mit Pane-Kopfzeile, wie
`lage.dienst("aufgaben", …)`). Ohne Adresse ist die Fähigkeit nicht eingerichtet. NUR lesend; `POST …/run` wird nie
aufgerufen.

Werkzeug (lesend, ohne eigene Oberfläche):
    pipelines.zustand  {nur_probleme?: bool, voll?: bool}
        -> {quelle_ok, quellen:[{id, name, art, takt, status, letzter_lauf, vor, punkte}], zaehlung, satz, stand}

„Veraltet“ wird je Quelle nach ihrer Art bewertet, nicht starr nach 24 Stunden wie in der Oberfläche:
    cron     läuft von selbst; veraltet, wenn der letzte Lauf länger als das Doppelte des Takts
             (plus 10 Minuten) her ist. Den Takt liest `takt_aus_cron` aus dem Cron-Ausdruck.
    push     Geräte melden sich, wenn sie wach und erreichbar sind (iPhone, Mac). Veraltet erst nach dem
             Dreifachen des Takts, mindestens aber nach 3 Tagen; ohne Takt nach 14 Tagen.
    manuell  läuft nur auf Knopfdruck; nie veraltet.
Status: aktiv, veraltet, fehler (letzter Lauf fehlgeschlagen), aus (abgeschaltet), manuell, nie gelaufen.

Fehler sind nie eine Ausnahme nach außen, sondern `{quelle_ok: false, grund, satz}`.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable

from mcp_hub.plugin import HubPlugin, ToolDescriptor
from . import _umgebung, lage
from ._umgebung import NichtEingerichtet

logger = logging.getLogger(__name__)

FRIST_S = 8.0
_TAGS = ["pipelines", "read"]
PUSH_MINDEST_S = 3 * 86400
PUSH_OHNE_TAKT_S = 14 * 86400


def _jetzt_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _zeit(roh: Any) -> dt.datetime | None:
    if not roh:
        return None
    try:
        d = dt.datetime.fromisoformat(str(roh).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _vor(d: dt.datetime | None, jetzt: dt.datetime) -> str:
    if d is None:
        return "nie"
    s = (jetzt - d).total_seconds()
    if s < 90:
        return "gerade eben"
    if s < 3600:
        return f"vor {round(s / 60)} Minuten"
    if s < 86400:
        return f"vor {round(s / 3600)} Stunden"
    return f"vor {round(s / 86400)} Tagen"


def _schritt(feld: str) -> int | None:
    """`*/N` -> N, sonst None."""
    if feld.startswith("*/") and feld[2:].isdigit() and int(feld[2:]) > 0:
        return int(feld[2:])
    return None


def takt_aus_cron(cron: str | None) -> int | None:
    """Geschätzter Abstand zwischen zwei Läufen in Sekunden, None bei unlesbarem oder fehlendem Ausdruck."""
    teile = (cron or "").split()
    if len(teile) != 5:
        return None
    minute, stunde, tag, _monat, wochentag = teile
    if (n := _schritt(minute)) is not None and stunde == "*":
        return n * 60
    if minute == "*":
        return 60
    if stunde == "*":
        return 3600
    if (n := _schritt(stunde)) is not None:
        return n * 3600
    if wochentag != "*":
        return 7 * 86400
    if tag != "*":
        return 30 * 86400
    return 86400


def takt_text(cron: str | None, art: str) -> str:
    if art == "manuell":
        return "auf Knopfdruck"
    s = takt_aus_cron(cron)
    if s is None:
        return "bei Ereignis" if art == "push" else "unbekannt"
    if s < 3600:
        wert = f"alle {s // 60} Minuten"
    elif s < 86400:
        wert = "stündlich" if s == 3600 else f"alle {s // 3600} Stunden"
    elif s == 86400:
        wert = "täglich"
    elif s == 7 * 86400:
        wert = "wöchentlich"
    else:
        wert = "monatlich"
    return wert + (" (erwartet)" if art == "push" else "")


def toleranz_s(art: str, cron: str | None) -> float | None:
    """Wie alt der letzte Lauf höchstens sein darf, bevor die Quelle als veraltet gilt. None = nie."""
    takt = takt_aus_cron(cron)
    if art == "manuell":
        return None
    if art == "push":
        return max(PUSH_MINDEST_S, 3 * takt) if takt else PUSH_OHNE_TAKT_S
    return 2 * (takt or 86400) + 600


def quelle_bewerten(p: dict[str, Any], jetzt: dt.datetime) -> dict[str, Any]:
    art = str(p.get("schedule_kind") or "cron")
    art = "manuell" if art == "manual" else art  # die API sagt „manual“
    letzter = _zeit(p.get("last_run_at"))
    if not p.get("enabled"):
        status = "aus"
    elif art == "manuell":
        status = "manuell"
    elif p.get("last_run_status") == "error":
        status = "fehler"
    elif letzter is None:
        status = "nie gelaufen"
    else:
        grenze = toleranz_s(art, p.get("schedule_cron"))
        status = "veraltet" if grenze is not None and (jetzt - letzter).total_seconds() > grenze else "aktiv"
    return {
        "id": p.get("pipeline_id"),
        "name": p.get("label") or p.get("pipeline_id"),
        "art": art,
        "takt": takt_text(p.get("schedule_cron"), art),
        "status": status,
        "letzter_lauf": letzter.isoformat(timespec="seconds") if letzter else None,
        "vor": _vor(letzter, jetzt),
        "punkte": p.get("total_points") or 0,
    }


def satz_gesamt(quellen: list[dict[str, Any]]) -> str:
    if not quellen:
        return "Keine Pipelines eingerichtet."
    zaehl: dict[str, list[str]] = {}
    for q in quellen:
        zaehl.setdefault(q["status"], []).append(q["name"])
    gelaufen = [q for q in quellen if q["status"] not in ("aus", "manuell")]
    ok = len(zaehl.get("aktiv", []))
    s = f"{ok} von {len(gelaufen)} laufenden Quellen aktuell"
    for status, titel in (("fehler", "mit Fehler"), ("veraltet", "veraltet"), ("nie gelaufen", "nie gelaufen")):
        namen = zaehl.get(status)
        if namen:
            s += f", {len(namen)} {titel} ({', '.join(namen[:4])}{', …' if len(namen) > 4 else ''})"
    if zaehl.get("aus"):
        s += f"; {len(zaehl['aus'])} abgeschaltet"
    return s + "."


class PipelinesPlugin(HubPlugin):
    name = "pipelines"
    description = (
        "Datenquellen und Pipelines (Wetter, Kalender, Apple, Banking …) mit Status und letztem Lauf; "
        "veraltet wird je Quelle nach ihrem Takt bewertet. Nur lesend."
    )
    version = "0.1.0"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        self._cfg = dict(config or {})
        #: Tests: (pfad) -> Antwort des Aufgabendienstes
        self._api: Callable[[str], Any] | None = None

    def register(self) -> None:
        pass

    def health(self) -> dict[str, Any]:
        if self._api is None and not _umgebung.lesen("FYOS_AUFGABEN_URL"):
            return {"ok": True, "status": _umgebung.NICHT_EINGERICHTET, "einrichten": "Setze FYOS_AUFGABEN_URL (Adresse des Aufgabendienstes)."}
        return {"ok": True, "status": "ok"}

    def _hole(self, pfad: str) -> Any:
        if self._api is not None:
            return self._api(pfad)
        req = urllib.request.Request(f"{lage.dienst_url('aufgaben')}{pfad}", headers=lage.pane_auth())
        try:
            with urllib.request.urlopen(req, timeout=FRIST_S) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Aufgabendienst antwortet {e.code}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Aufgabendienst nicht erreichbar: {e.reason}") from e

    def zustand(self, nur_probleme: bool = False, voll: bool = False) -> dict[str, Any]:
        try:
            roh = (self._hole("/api/life/pipelines") or {}).get("items") or []
        except NichtEingerichtet as e:  # keine Adresse: kein Ausfall, ein eigener Zustand mit Weg zum Einrichten
            return e.lesend()
        except Exception as e:  # noqa: BLE001 — Ausfall ist ein Zustand, kein Fehler
            logger.warning("pipelines: %s", e)
            return {
                "quelle_ok": False,
                "grund": str(e)[:160],
                "satz": "Die Pipelines sind gerade nicht abrufbar.",
                "stand": _jetzt_iso(),
            }
        jetzt = dt.datetime.now(dt.timezone.utc)
        quellen = []
        for p in roh:
            if not isinstance(p, dict):
                continue
            q = quelle_bewerten(p, jetzt)
            if voll:
                q["beschreibung"] = p.get("description")
                q["cron"] = p.get("schedule_cron")
                q["letzter_lauf_status"] = p.get("last_run_status")
                q["letzter_lauf_neu"] = p.get("last_run_inserted")
            quellen.append(q)
        zaehlung: dict[str, int] = {}
        for q in quellen:
            zaehlung[q["status"]] = zaehlung.get(q["status"], 0) + 1
        satz = satz_gesamt(quellen)
        if nur_probleme:
            quellen = [q for q in quellen if q["status"] in ("fehler", "veraltet", "nie gelaufen")]
        return {
            "quelle_ok": True,
            "quellen": quellen,
            "zaehlung": zaehlung,
            "satz": satz,
            "stand": _jetzt_iso(),
        }

    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="zustand",
                title="Pipelines",
                description=(
                    "Datenquellen und Pipelines des Aufgabendienstes (Wetter, Kalender, Apple, Banking, Tankstellen …): "
                    "Art (cron, push, manuell), Takt, Status (aktiv, veraltet, fehler, aus, manuell, nie "
                    "gelaufen) und letzter Lauf, dazu ein kurzer deutscher Satz. Veraltet wird je Quelle nach "
                    "ihrem Takt bewertet: Cron-Quellen nach ihrem Intervall, Push-Quellen erst nach Tagen. "
                    "Nur lesend; startet keinen Lauf."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "nur_probleme": {
                            "type": "boolean",
                            "default": False,
                            "description": "Nur Quellen mit Fehler, veraltet oder nie gelaufen auflisten.",
                        },
                        "voll": {
                            "type": "boolean",
                            "default": False,
                            "description": "Vollform mit Beschreibung, Cron-Ausdruck und Ergebnis des letzten Laufs.",
                        },
                    },
                    "additionalProperties": False,
                },
                tags=_TAGS,
            )
        ]

    def call(self, method: str, args: dict[str, Any]) -> Any:
        if method == "zustand":
            return self.zustand(bool(args.get("nur_probleme")), bool(args.get("voll")))
        raise ValueError(f"pipelines hat kein Werkzeug {method!r}")
