"""grow, pipelines, mail — lesende Werkzeuge mit Attrappen (kein Netz), nach dem Vorbild test_wetter.py."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest

from fyos_plugin_betrieb import grow, pipelines
from fyos_plugin_betrieb.grow import GrowPlugin
from fyos_plugin_betrieb.pipelines import PipelinesPlugin

JETZT = dt.datetime.now(dt.timezone.utc)


def vor(minuten: float = 0, tage: float = 0) -> str:
    return (JETZT - dt.timedelta(minutes=minuten, days=tage)).isoformat()


def _sichtbar_fuer_modell(tools: list[Any]) -> None:
    for t in tools:
        assert t.description
        assert "read" in t.tags
        assert not (t.meta or {}).get("ui"), "lesende Modell-Werkzeuge ohne eigene Oberfläche wie wetter"
        assert t.input_schema["additionalProperties"] is False
        for name, feld in t.input_schema["properties"].items():
            assert feld.get("description"), f"{t.name}.{name} ohne Beschreibung"


# ── grow ────────────────────────────────────────────────────────────────────
SETUPS = {
    "items": [
        {
            "slug": "setup_a", "name": "Little Tent", "online": True, "controller_id": "123", "strain": "",
            "tent_camera_entity": "camera.little",
            "live": {
                "temp_c": 24.0, "rh_pct": 46.4, "vpd_kpa": 1.6,
                "ports": [
                    {"port": 1, "name": "Port 1", "type": "led", "state": None, "level": 0},
                    {"port": 2, "name": "Port 2", "type": "unused", "state": None, "level": 0},
                    {"port": 3, "name": "Port 3", "type": "humidifier", "state": "on", "level": 0},
                    {"port": 4, "name": "Port 4", "type": "inline_fan", "state": None, "level": 60},
                ],
            },
        },
        {"slug": "setup_b", "name": "Big Tent", "online": False, "live": {"ports": []}},
    ]
}


def grow_plugin(setups: Any = SETUPS, punkt_alter_min: float = 3, licht: Any = False) -> GrowPlugin:
    p = GrowPlugin()
    p.pfade = []  # type: ignore[attr-defined]

    def api(pfad: str) -> Any:
        p.pfade.append(pfad)  # type: ignore[attr-defined]
        if isinstance(setups, Exception):
            raise setups
        if pfad == "/api/grow/setups":
            return setups
        assert pfad.startswith("/api/grow/setup/") and "sensor-log" in pfad
        return {"series": [{"ts": vor(punkt_alter_min), "light_on": licht}]}

    p._api = api
    return p


def test_grow_zustand() -> None:
    p = grow_plugin()
    z = p.call("zustand", {})
    assert z["quelle_ok"]
    a = z["zelte"][0]
    assert a["temperatur_c"] == 24.0 and a["luftfeuchte_prozent"] == 46 and a["vpd_kpa"] == 1.6
    assert a["licht_an"] is False and a["abgleich_veraltet"] is False
    assert a["letzter_abgleich_vor"] == "vor 3 Minuten"
    zustaende = {s["port"]: s["zustand"] for s in a["steckdosen"]}
    assert zustaende == {1: "unbekannt", 3: "an", 4: "an"}, "unbenutzter Port 2 fehlt, state null ist nicht aus"
    assert a["unbenutzte_ports"] == 1
    assert a["satz"] == "Little Tent: 24,0 °C, 46 %, VPD 1,60 kPa, Licht aus."
    assert z["satz"].startswith("1 von 2 Zelten online. Offline: Big Tent.")
    assert z["zelte"][1]["satz"] == "Big Tent: offline."


def test_grow_nur_lesend_keine_schreibpfade() -> None:
    p = grow_plugin()
    p.call("zustand", {"voll": True})
    assert p.pfade  # type: ignore[attr-defined]
    assert all("sync" not in x and "apply" not in x for x in p.pfade)  # type: ignore[attr-defined]


def test_grow_voll_zeigt_unbenutzte_ports() -> None:
    a = grow_plugin().call("zustand", {"voll": True})["zelte"][0]
    assert len(a["steckdosen"]) == 4 and a["controller_id"] == "123" and a["kamera"] == "camera.little"


def test_grow_abgleich_veraltet_im_satz() -> None:
    a = grow_plugin(punkt_alter_min=90).call("zustand", {})["zelte"][0]
    assert a["abgleich_veraltet"] and "(Abgleich vor 2 Stunden)" in a["satz"]


def test_grow_ausfall_ist_keine_ausnahme() -> None:
    z = grow_plugin(RuntimeError("grow-dienst nicht erreichbar")).call("zustand", {})
    assert z["quelle_ok"] is False and "nicht abrufbar" in z["satz"] and "nicht erreichbar" in z["grund"]


def test_grow_unbekanntes_werkzeug() -> None:
    with pytest.raises(ValueError):
        GrowPlugin().call("sync", {})


def test_grow_beschreibung() -> None:
    _sichtbar_fuer_modell(GrowPlugin().describe_tools())


# ── pipelines ───────────────────────────────────────────────────────────────
def pipe(pid: str, art: str, cron: str | None, alter_min: float | None, status: str | None = "ok",
         aktiv: bool = True, punkte: int = 10) -> dict[str, Any]:
    return {
        "pipeline_id": pid, "label": pid.title(), "schedule_kind": art, "schedule_cron": cron,
        "enabled": aktiv, "last_run_at": vor(alter_min) if alter_min is not None else None,
        "last_run_status": status, "last_run_inserted": 1, "total_points": punkte, "description": "d",
    }


def pipelines_plugin(items: Any) -> PipelinesPlugin:
    p = PipelinesPlugin()
    p._api = lambda pfad: (_ for _ in ()).throw(items) if isinstance(items, Exception) else {"items": items}
    return p


@pytest.mark.parametrize(
    "cron,erwartet",
    [
        ("*/10 * * * *", 600), ("*/5 * * * *", 300), ("0 * * * *", 3600), ("0 */6 * * *", 6 * 3600),
        ("0 */12 * * *", 12 * 3600), ("0 6 * * *", 86400), ("0 5 * * 0", 7 * 86400), (None, None), ("quatsch", None),
    ],
)
def test_takt_aus_cron(cron: str | None, erwartet: int | None) -> None:
    assert pipelines.takt_aus_cron(cron) == erwartet


def test_cron_quelle_nach_eigenem_takt_bewertet() -> None:
    # alle 10 Minuten: nach 40 Minuten (> 2x10+10) veraltet, nach 15 aktiv
    z = pipelines_plugin([pipe("a", "cron", "*/10 * * * *", 40), pipe("b", "cron", "*/10 * * * *", 15)]).call("zustand", {})
    assert {q["id"]: q["status"] for q in z["quellen"]} == {"a": "veraltet", "b": "aktiv"}
    # täglich: nach 30 Stunden noch aktiv (starre 24 h würden „veraltet“ sagen)
    z = pipelines_plugin([pipe("c", "cron", "0 6 * * *", 30 * 60)]).call("zustand", {})
    assert z["quellen"][0]["status"] == "aktiv"


def test_push_quelle_nicht_starr_nach_24h() -> None:
    z = pipelines_plugin(
        [
            pipe("iphone", "push", "0 */6 * * *", None, aktiv=True) | {"last_run_at": vor(tage=2)},
            pipe("alt", "push", "0 * * * *", None) | {"last_run_at": vor(tage=40)},
            pipe("ereignis", "push", None, None) | {"last_run_at": vor(tage=10)},
        ]
    ).call("zustand", {})
    assert {q["id"]: q["status"] for q in z["quellen"]} == {"iphone": "aktiv", "alt": "veraltet", "ereignis": "aktiv"}
    assert z["quellen"][0]["takt"] == "alle 6 Stunden (erwartet)"
    assert z["quellen"][2]["takt"] == "bei Ereignis"


def test_status_fehler_aus_manuell_nie() -> None:
    z = pipelines_plugin(
        [
            pipe("f", "cron", "0 5 * * 0", 5, status="error"),
            pipe("aus", "cron", "0 2 * * *", 99999, aktiv=False, status="error"),
            pipe("man", "manual", None, None),
            pipe("nie", "cron", "0 * * * *", None, status=None),
        ]
    ).call("zustand", {})
    assert {q["id"]: q["status"] for q in z["quellen"]} == {
        "f": "fehler", "aus": "aus", "man": "manuell", "nie": "nie gelaufen"
    }
    assert z["satz"] == "0 von 2 laufenden Quellen aktuell, 1 mit Fehler (F), 1 nie gelaufen (Nie); 1 abgeschaltet."


def test_pipelines_nur_probleme_und_voll() -> None:
    items = [pipe("a", "cron", "*/10 * * * *", 5), pipe("b", "cron", "*/10 * * * *", 500)]
    z = pipelines_plugin(items).call("zustand", {"nur_probleme": True})
    assert [q["id"] for q in z["quellen"]] == ["b"] and z["zaehlung"] == {"aktiv": 1, "veraltet": 1}
    v = pipelines_plugin(items).call("zustand", {"voll": True})
    assert v["quellen"][0]["cron"] == "*/10 * * * *" and "beschreibung" in v["quellen"][0]


def test_pipelines_klein_und_ausfall() -> None:
    items = [pipe(f"q{i}", "cron", "*/5 * * * *", 1) for i in range(40)]
    assert len(json.dumps(pipelines_plugin(items).call("zustand", {}))) < 10_000
    z = pipelines_plugin(RuntimeError("aufgaben-dienst nicht erreichbar")).call("zustand", {})
    assert z["quelle_ok"] is False and "nicht abrufbar" in z["satz"]


def test_pipelines_beschreibung() -> None:
    _sichtbar_fuer_modell(PipelinesPlugin().describe_tools())
    with pytest.raises(ValueError):
        PipelinesPlugin().call("run", {})


