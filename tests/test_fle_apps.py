"""FLE (Plugin `fle`) — das Werkzeug `fle.messungen` und seine Anbindung im Hub.

Das Shadow-Duell (Werkzeuge shadow_duell, duell_laden, duell_stimme, Oberfläche) ist seit 2026-10-08 abgebaut;
seine Tests liegen in archive/2026-10-08-shadow-duell/.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mcp_hub import registry as hub_registry
from mcp_hub import server as hub_server
from fyos_plugin_betrieb.fle_apps import FleAppsPlugin


@pytest.fixture()
def daten(tmp_path: Path) -> Path:
    (tmp_path / "messungen").mkdir()
    (tmp_path / "messungen" / "modell-b1.json").write_text(
        json.dumps({"lauf": "modell-b1", "gruppe": "modell-m2", "kennzahlen": {"tok_s": 41.0, "kld_mean": 0.155}})
    )
    (tmp_path / "messungen" / "unsloth.json").write_text(
        json.dumps({"lauf": "unsloth", "gruppe": "referenz", "kennzahlen": {"tok_s": 38.5}})
    )
    return tmp_path


def test_messungen_filter_und_kennzahlen(daten: Path):
    p = FleAppsPlugin({"daten": str(daten)})
    alle = p.call("messungen", {})
    assert alle["anzahl"] == 2
    assert alle["kennzahlen"] == ["kld_mean", "tok_s"]
    assert [r["lauf"] for r in p.call("messungen", {"filter": "m2"})["laeufe"]] == ["modell-b1"]


def test_messungen_knapp_ist_der_standard_voll_liefert_alles(daten: Path):
    (daten / "messungen" / "alt.json").write_text(
        json.dumps({"lauf": "alt", "datum": "2026-01-02", "kennzahlen": {"tok_s": 99.0, "kld_mean": 0.9}})
    )
    p = FleAppsPlugin({"daten": str(daten)})
    k = p.call("messungen", {})
    assert k["knapp"] is True and k["kennzahl"] == "tok_s" and k["tiefer_ist_besser"] is False
    assert [r["lauf"] for r in k["laeufe"]] == ["alt", "modell-b1", "unsloth"]  # nach Tempo, bester zuerst
    assert set(k["laeufe"][0]) == {"lauf", "datum", "modell", "gruppe", "tok_s"}  # keine weiteren Kennzahlen je Lauf
    assert "Beste nach tok_s" in k["satz"] and "3 Läufe" in k["satz"] and k["alter_tage"] > 3 and "Tage alt" in k["satz"]
    kld = p.call("messungen", {"kennzahl": "kld_mean", "n": 1})
    assert kld["tiefer_ist_besser"] is True and [r["lauf"] for r in kld["laeufe"]] == ["modell-b1"]
    voll = p.call("messungen", {"voll": True})
    assert "knapp" not in voll and voll["anzahl"] == 3 and voll["laeufe"][0]["kennzahlen"]
    ausdruecklich = p.call("messungen", {"laeufe": ["unsloth"]})
    assert ausdruecklich["laeufe"][0]["kennzahlen"] == {"tok_s": 38.5}


def test_nur_noch_messungen_ohne_oberflaeche(daten: Path):
    p = FleAppsPlugin({"daten": str(daten)})
    werkzeuge = {t.name: t for t in p.describe_tools()}
    assert set(werkzeuge) == {"messungen"}
    assert werkzeuge["messungen"].meta is None  # die Oberflaeche steckt in Experimente (Sicht „Messungen“)
    assert p.describe_resources() == []
    with pytest.raises(ValueError):
        p.call("shadow_duell", {})


def test_hub_bietet_messungen_nicht_als_app_an(daten: Path, monkeypatch):
    reg = hub_registry.PluginRegistry()
    hub_registry._register_one(reg, "fle", FleAppsPlugin({"daten": str(daten)}))
    monkeypatch.setattr(hub_server, "_REGISTRY", reg)
    hub_server._apps_anbinden(reg)
    try:
        tools = {t.name for t in asyncio.run(hub_server.mcp.list_tools())}
        assert "fle__messungen" not in tools and "fle__shadow_duell" not in tools
        uris = {str(r.uri) for r in asyncio.run(hub_server.mcp.list_resources())}
        assert not any(u.startswith("ui://fyos/fle/") for u in uris)
        assert reg.plugins["fle"].call("messungen", {"filter": "unsloth"})["anzahl"] == 1
    finally:
        hub_server._apps_anbinden(hub_registry.PluginRegistry())


def test_app_call_route_laesst_nur_app_werkzeuge_durch(daten: Path, monkeypatch):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    reg = hub_registry.PluginRegistry()
    hub_registry._register_one(reg, "fle", FleAppsPlugin({"daten": str(daten)}))
    monkeypatch.setattr(hub_server, "_REGISTRY", reg)
    app = Starlette(routes=[
        Route("/hub/app-call", hub_server.rest_app_call, methods=["POST"]),
        Route("/hub/apps", hub_server.rest_apps, methods=["GET"]),
    ])
    c = TestClient(app)
    # fle.messungen hat keine Oberflaeche: der Browser ruft die Messungen ueber experiment.messungen_laden.
    assert c.post("/hub/app-call", json={"name": "fle__messungen", "arguments": {}}).status_code == 403
    # Ein Werkzeug ohne Oberflaeche (hier: unbekannt) kommt nicht durch.
    assert c.post("/hub/app-call", json={"tool": "k8s.exec_pod", "arguments": {}}).status_code == 403
    apps = c.get("/hub/apps").json()
    assert "fle__messungen" not in {a["name"] for a in apps["apps"]}


def test_buchhaltung_ist_keine_kennzahl(daten: Path):
    (daten / "messungen" / "mit-rows.json").write_text(
        json.dumps({"lauf": "x", "kennzahlen": {"rows": 24, "expected": 24, "median_decode_tps": 55.8}})
    )
    r = FleAppsPlugin({"daten": str(daten)}).call("messungen", {"laeufe": ["x"]})
    assert r["kennzahlen"] == ["median_decode_tps"]


def test_flaechen_tag_oeffnet_den_browserweg_ohne_oberflaeche(monkeypatch):
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from mcp_hub.plugin import HubPlugin, ToolDescriptor

    class _Lage(HubPlugin):
        name = "lage"

        def register(self) -> None:
            pass

        def describe_tools(self):
            return [
                ToolDescriptor(name="ampel", description="x", input_schema={}, tags=["read", "flaeche"]),
                ToolDescriptor(name="geheim", description="x", input_schema={}, tags=["exec"]),
            ]

        def call(self, method, args):
            return {"ok": True, "farbe": "gruen"}

    reg = hub_registry.PluginRegistry()
    hub_registry._register_one(reg, "lage", _Lage())
    monkeypatch.setattr(hub_server, "_REGISTRY", reg)
    c = TestClient(Starlette(routes=[
        Route("/hub/app-call", hub_server.rest_app_call, methods=["POST"]),
        Route("/hub/apps", hub_server.rest_apps, methods=["GET"]),
    ]))
    assert c.post("/hub/app-call", json={"tool": "lage.ampel"}).json()["result"]["farbe"] == "gruen"
    assert c.post("/hub/app-call", json={"tool": "lage.geheim"}).status_code == 403
    apps = {a["tool"]: a for a in c.get("/hub/apps").json()["apps"]}
    assert apps["lage.ampel"]["hatUi"] is False and "lage.geheim" not in apps
