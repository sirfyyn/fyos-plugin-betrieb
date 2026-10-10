"""grow — Zustand der Grow-Zelte für den Agenten und alle Flächen (2026-10-06, App-Prüfung Runde 2).

Quelle ist dieselbe API, die die Fläche Grow von fyos-web liest: der Grow-Dienst
(`GET /api/grow/setups` für Controller, Klima und Steckdosen; `GET /api/grow/setup/<slug>/sensor-log`
für den letzten Messpunkt, der den Controller-Abgleich ersetzt, siehe unten). Die Adresse des Dienstes steht in
`GROW_URL`; ohne sie ist die Fähigkeit nicht eingerichtet (Zustand mit Weg zum Einrichten). Dieselbe Pane-Kopfzeile wie
`lage.dienst`. NUR lesend; der Sync-POST (`/setups/sync`) und das Anwenden von Phasen werden nie aufgerufen.

Werkzeug (lesend, ohne eigene Oberfläche):
    grow.zustand  {voll?: bool}
        -> {quelle_ok, zelte:[{name, slug, online, temperatur_c, luftfeuchte_prozent, vpd_kpa,
                              licht_an, letzter_abgleich, steckdosen:[{port, name, typ, zustand,
                              stufe_prozent}], satz}], satz, stand}

„Letzter Abgleich“: der Grow-Dienst schreibt alle 5 Minuten einen Messpunkt (`grow_sensor_log`); dessen
Zeitstempel ist der letzte Abgleich zwischen Controller und FYOS. `licht_an` kommt ebenfalls aus diesem
Messpunkt. Die Steckdosen-Zustände liefert die Cloud derzeit nicht (`state: null`); dann steht
`zustand: "unbekannt"` (nicht „aus“) und die Stufe als Zahl.

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
#: Der Messpunkt kommt alle 5 Minuten; ab 20 Minuten Stille gilt der Abgleich als überfällig.
ABGLEICH_ALT_MIN = 20
_TAGS = ["grow", "read"]

TYP_DE = {
    "led": "Licht",
    "inline_fan": "Abluft",
    "oscillating_fan": "Umluft",
    "humidifier": "Befeuchter",
    "dehumidifier": "Entfeuchter",
    "heater": "Heizung",
    "aircon": "Klimagerät",
    "co2": "CO₂",
    "unused": "unbenutzt",
}


def grow_url() -> str:
    """Adresse des Grow-Dienstes aus GROW_URL; ohne Einstellung `NichtEingerichtet`."""
    return _umgebung.verlangen(
        "GROW_URL", "Der Grow-Dienst", "Setze GROW_URL (Adresse des Grow-Dienstes, z. B. http://grow.example.org)."
    ).rstrip("/")


def _jetzt_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _zahl(v: Any, stellen: int = 1) -> float | None:
    if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v:
        return round(float(v), stellen)
    return None


def _de(v: float | None, stellen: int = 1) -> str:
    return f"{v:.{stellen}f}".replace(".", ",") if v is not None else "?"


def _zeit(roh: Any) -> dt.datetime | None:
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


def steckdose(p: dict[str, Any]) -> dict[str, Any]:
    """Ein Port. `state` kommt aus der Cloud (heute immer null); `level` ist die Stufe 0 bis 100."""
    typ = p.get("type") or "unused"
    stufe = _zahl(p.get("level"), 0)
    state = str(p.get("state") or "").lower()
    if state in ("on", "off"):
        zustand = "an" if state == "on" else "aus"
    elif stufe is not None and stufe > 0:
        zustand = "an"
    else:
        zustand = "unbekannt"
    return {
        "port": p.get("port"),
        "name": p.get("name") or TYP_DE.get(typ, typ),
        "typ": TYP_DE.get(typ, typ),
        "zustand": zustand,
        "stufe_prozent": stufe,
    }


def satz_zelt(z: dict[str, Any]) -> str:
    s = f"{z['name']}: "
    if not z.get("online"):
        s += "offline"
        if z.get("temperatur_c") is None:
            return s + "."
        s += ", letzte Werte "
    s += f"{_de(z.get('temperatur_c'))} °C, {_de(z.get('luftfeuchte_prozent'), 0)} %"
    if z.get("vpd_kpa") is not None:
        s += f", VPD {_de(z['vpd_kpa'], 2)} kPa"
    if z.get("licht_an") is not None:
        s += f", Licht {'an' if z['licht_an'] else 'aus'}"
    if z.get("abgleich_veraltet"):
        s += f" (Abgleich {z['letzter_abgleich_vor']})"
    return s + "."


def zelt_normalisieren(
    setup: dict[str, Any], punkt: dict[str, Any] | None, jetzt: dt.datetime, voll: bool
) -> dict[str, Any]:
    live = setup.get("live") or {}
    ports = [steckdose(p) for p in (live.get("ports") or []) if isinstance(p, dict)]
    unbenutzt = [p for p in ports if p["typ"] == "unbenutzt"]
    letzter = _zeit((punkt or {}).get("ts"))
    veraltet = letzter is None or (jetzt - letzter).total_seconds() > ABGLEICH_ALT_MIN * 60
    licht = (punkt or {}).get("light_on")
    z: dict[str, Any] = {
        "name": setup.get("name") or setup.get("slug"),
        "slug": setup.get("slug"),
        "online": bool(setup.get("online")),
        "temperatur_c": _zahl(live.get("temp_c")),
        "luftfeuchte_prozent": _zahl(live.get("rh_pct"), 0),
        "vpd_kpa": _zahl(live.get("vpd_kpa"), 2),
        "licht_an": licht if isinstance(licht, bool) else None,
        "letzter_abgleich": letzter.isoformat(timespec="seconds") if letzter else None,
        "letzter_abgleich_vor": _vor(letzter, jetzt),
        "abgleich_veraltet": veraltet,
        "steckdosen": ports if voll else [p for p in ports if p["typ"] != "unbenutzt"],
    }
    if not voll and unbenutzt:
        z["unbenutzte_ports"] = len(unbenutzt)
    if voll:
        z["controller_id"] = setup.get("controller_id")
        z["kamera"] = setup.get("tent_camera_entity")
        z["sorte"] = setup.get("strain") or None
    z["satz"] = satz_zelt(z)
    return z


def satz_gesamt(zelte: list[dict[str, Any]]) -> str:
    if not zelte:
        return "Keine Zelte eingerichtet."
    on = [z for z in zelte if z["online"]]
    kopf = f"{len(on)} von {len(zelte)} Zelten online."
    if len(on) < len(zelte):
        kopf += " Offline: " + ", ".join(z["name"] for z in zelte if not z["online"]) + "."
    return kopf + " " + " ".join(z["satz"] for z in zelte)


class GrowPlugin(HubPlugin):
    name = "grow"
    description = (
        "Grow-Zelte lesen: Temperatur, Luftfeuchte, VPD, Steckdosen und letzter Abgleich je Zelt "
        "aus dem Grow-Dienst, dazu ein kurzer deutscher Satz. Nur lesend."
    )
    version = "0.1.0"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        self._cfg = dict(config or {})
        #: Tests: (pfad) -> Antwort des Grow-Dienstes
        self._api: Callable[[str], Any] | None = None

    def register(self) -> None:
        pass

    def health(self) -> dict[str, Any]:
        if self._api is None and not _umgebung.lesen("GROW_URL"):
            return {"ok": True, "status": _umgebung.NICHT_EINGERICHTET, "einrichten": "Setze GROW_URL (Adresse des Grow-Dienstes)."}
        return {"ok": True, "status": "ok"}

    def _hole(self, pfad: str) -> Any:
        if self._api is not None:
            return self._api(pfad)
        req = urllib.request.Request(f"{grow_url()}{pfad}", headers=lage.pane_auth())
        try:
            with urllib.request.urlopen(req, timeout=FRIST_S) as r:
                return json.loads(r.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Grow-Dienst antwortet {e.code}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"Grow-Dienst nicht erreichbar: {e.reason}") from e

    def zustand(self, voll: bool = False) -> dict[str, Any]:
        try:
            setups = (self._hole("/api/grow/setups") or {}).get("items") or []
        except NichtEingerichtet as e:  # keine Adresse: kein Ausfall, ein eigener Zustand mit Weg zum Einrichten
            return e.lesend()
        except Exception as e:  # noqa: BLE001 — Ausfall ist ein Zustand, kein Fehler
            logger.warning("grow: %s", e)
            return {
                "quelle_ok": False,
                "grund": str(e)[:160],
                "satz": "Die Grow-Zelte sind gerade nicht abrufbar.",
                "stand": _jetzt_iso(),
            }
        jetzt = dt.datetime.now(dt.timezone.utc)
        zelte = []
        for s in setups:
            if not isinstance(s, dict):
                continue
            punkt = None
            try:
                log = self._hole(f"/api/grow/setup/{s.get('slug')}/sensor-log?hours=1") or {}
                serie = log.get("series") or []
                punkt = serie[-1] if serie else None
            except Exception as e:  # noqa: BLE001 — ohne Messpunkt bleibt der Abgleich „nie“
                logger.info("grow: sensor-log %s: %s", s.get("slug"), e)
            zelte.append(zelt_normalisieren(s, punkt, jetzt, voll))
        return {"quelle_ok": True, "zelte": zelte, "satz": satz_gesamt(zelte), "stand": _jetzt_iso()}

    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="zustand",
                title="Grow-Zelte",
                description=(
                    "Zustand der Grow-Zelte: online, Temperatur, Luftfeuchte, "
                    "VPD, Licht, Steckdosen mit Zustand (an, aus oder unbekannt) und der letzte Abgleich "
                    "mit dem Controller, dazu ein kurzer deutscher Satz. Nur lesend; schaltet nichts und "
                    "löst keinen Sync aus. voll=true liefert zusätzlich unbenutzte Ports, Controller und "
                    "Kamera."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "voll": {
                            "type": "boolean",
                            "default": False,
                            "description": "Vollform mit unbenutzten Ports, Controller-ID und Kamera.",
                        }
                    },
                    "additionalProperties": False,
                },
                tags=_TAGS,
            )
        ]

    def call(self, method: str, args: dict[str, Any]) -> Any:
        if method == "zustand":
            return self.zustand(bool(args.get("voll")))
        raise ValueError(f"grow hat kein Werkzeug {method!r}")
