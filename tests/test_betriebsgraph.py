"""Tests für fyos_plugin_betrieb/betriebsgraph.py.

Reine Funktionen laufen immer. Die Datenbank-Tests brauchen ein echtes Postgres (WITH RECURSIVE, jsonb,
Teilindizes): sie laufen gegen `GRAPH_TEST_DB_URL` in einem eigenen, danach gelöschten Schema und werden sonst
übersprungen (Grund steht in der Ausgabe von `pytest -rs`). Von Hand, mit dem Treiber aus dem Extra `graph`:

    pip install -e ".[test,graph]"
    GRAPH_TEST_DB_URL=postgresql://benutzer:passwort@host:5432/testdb python -P -m pytest tests/test_betriebsgraph.py -q

Das Konto braucht das Recht, Schemas anzulegen und zu löschen; die Tests lassen nichts zurück.
"""

from __future__ import annotations

import datetime as dt
import os
import sys
import uuid

import pytest

from fyos_plugin_betrieb import betriebsgraph as bg
from fyos_plugin_betrieb.betriebsgraph import Kante, Knoten


# ── reine Funktionen ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "pod,name",
    [
        ("qdrant-64f566945f-bq4gw", "qdrant"),
        ("loki-stack-0", "loki-stack"),
        ("monitoring-prometheus-node-exporter-57w4h", "monitoring-prometheus-node-exporter"),
        ("caddy", "caddy"),
        ("webapp-life-67fc7b7fb7-czfh5", "webapp-life"),
    ],
)
def test_pod_zu_name(pod: str, name: str) -> None:
    assert bg.pod_zu_name(pod) == name


def test_id_teile() -> None:
    assert bg.typ_aus_id("dienst:fyos/qdrant") == "dienst"
    assert bg.name_aus_id("dienst:fyos/qdrant") == "qdrant"
    assert bg.name_aus_id("knoten:node-b") == "node-b"
    assert bg.typ_aus_id("ohne") == "?"


def test_zeitpunkt_relativ_und_iso() -> None:
    t = bg._zeitpunkt("7d")
    assert t is not None and dt.datetime.now(dt.timezone.utc) - t > dt.timedelta(days=6, hours=23)
    assert bg._zeitpunkt("2026-10-01").tzinfo is not None
    assert bg._zeitpunkt(None) is None
    with pytest.raises(ValueError):
        bg._zeitpunkt("gestern")


def test_attribut_unterschied_und_gruppiert() -> None:
    assert bg.attribut_unterschied({"a": 1, "b": 2}, {"a": 1, "b": 3, "c": 4}) == ["b: 2 → 3", "c: — → 4"]
    g = bg.gruppiert([{"typ": "dienst", "name": f"d{i}"} for i in range(12)] + [{"typ": "route", "name": "r"}], je_typ=3)
    assert g["dienst"] == {"anzahl": 12, "namen": ["d0", "d1", "d2"]}
    assert g["route"]["anzahl"] == 1


def test_schema_ist_idempotent_formuliert() -> None:
    # jede Anweisung, die etwas anlegt, trägt IF NOT EXISTS / ON CONFLICT
    for zeile in bg.SCHEMA_SQL.splitlines():
        z = zeile.strip().upper()
        if z.startswith("CREATE TABLE") or z.startswith("CREATE INDEX") or z.startswith("CREATE UNIQUE INDEX"):
            assert "IF NOT EXISTS" in z, zeile


def test_verbinden_ohne_adresse_und_ohne_treiber(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAPH_DB_URL", raising=False)
    with pytest.raises(bg.GraphFehler, match="GRAPH_DB_URL"):
        bg.verbinden()
    # Kein Postgres-Treiber installiert: eine klare Meldung mit dem Extra statt eines ImportError.
    # (Ein `None` in sys.modules lässt `import …` scheitern.)
    monkeypatch.setitem(sys.modules, "psycopg", None)
    monkeypatch.setitem(sys.modules, "psycopg2", None)
    with pytest.raises(bg.GraphFehler, match=r"fyos-plugin-betrieb\[graph\]") as e:
        bg.verbinden("postgresql://benutzer:geheim@host/db")
    assert "geheim" not in str(e.value)  # die Adresse trägt das Passwort und steht nie in einer Meldung


def test_verbinden_nimmt_psycopg3_vor_psycopg2(monkeypatch: pytest.MonkeyPatch) -> None:
    gesehen: dict[str, dict] = {}

    def treiber(name: str):
        class T:
            @staticmethod
            def connect(url, **kw):
                gesehen[name] = {"url": url, **kw}
                return name

        return T

    monkeypatch.setitem(sys.modules, "psycopg", treiber("psycopg"))
    monkeypatch.setitem(sys.modules, "psycopg2", treiber("psycopg2"))
    assert bg.verbinden("postgresql://a:b@h/db", schema="t_1", anweisung_s=7) == "psycopg"
    assert "statement_timeout=7000" in gesehen["psycopg"]["options"] and "search_path=t_1" in gesehen["psycopg"]["options"]
    assert gesehen["psycopg"]["connect_timeout"] == 5 and "psycopg2" not in gesehen
    # ohne psycopg 3 geht der Ältere
    monkeypatch.setitem(sys.modules, "psycopg", None)
    assert bg.verbinden("postgresql://a:b@h/db") == "psycopg2"
    # die Adresse darf auch aus der Umgebung kommen
    monkeypatch.setenv("GRAPH_DB_URL", "postgresql://u:p@umgebung/db")
    bg.verbinden()
    assert gesehen["psycopg2"]["url"] == "postgresql://u:p@umgebung/db"


# ── Datenbank ────────────────────────────────────────────────────────────────
URL = os.getenv("GRAPH_TEST_DB_URL")
braucht_db = pytest.mark.skipif(not URL, reason="GRAPH_TEST_DB_URL nicht gesetzt (echtes Postgres nötig)")


@pytest.fixture()
def graph():
    schema = "t_" + uuid.uuid4().hex[:10]
    admin = bg.verbinden(URL)
    admin.autocommit = True
    cur = admin.cursor()
    cur.execute(f"CREATE SCHEMA {schema}")
    conn = bg.verbinden(URL, schema=schema)
    g = bg.Graph(conn)
    g.schema_anlegen()
    yield g
    conn.close()
    cur.execute(f"DROP SCHEMA {schema} CASCADE")
    admin.close()


def _k(i: str, name: str | None = None, **attr) -> Knoten:
    typ = i.split(":", 1)[0]
    return Knoten(i, typ, name or i.rsplit("/", 1)[-1].split(":")[-1], attr)


def _beispiel(g: bg.Graph) -> None:
    """Zwei Knoten (node-a, node-b); qdrant auf node-a (hart); webapp läuft auf beiden (frei);
    route → service → qdrant; webapp nutzt qdrant; mcp-hub-Plugin nutzt webapp; Vorgang betrifft qdrant."""
    knoten = [
        _k("knoten:node-a"),
        _k("knoten:node-b"),
        _k("dienst:fyos/qdrant", replikas=1),
        _k("service:fyos/qdrant"),
        _k("route:qdrant.example"),
        _k("dienst:fyos/webapp"),
        _k("plugin:graph"),
        _k("vorgang:INC-1", "INC-1"),
        _k("pvc:fyos/qdrant-data"),
        _k("pv:qdrant-pv"),
    ]
    kanten = [
        Kante("dienst:fyos/qdrant", "knoten:node-a", "laeuft_auf", attribute={"pin": "hart"}),
        Kante("dienst:fyos/webapp", "knoten:node-a", "laeuft_auf", attribute={"pin": "frei"}),
        Kante("dienst:fyos/webapp", "knoten:node-b", "laeuft_auf", attribute={"pin": "frei"}),
        Kante("service:fyos/qdrant", "dienst:fyos/qdrant", "waehlt"),
        Kante("route:qdrant.example", "service:fyos/qdrant", "routet_zu"),
        Kante("dienst:fyos/webapp", "service:fyos/qdrant", "nutzt", beleg="QDRANT_URL"),
        Kante("plugin:graph", "dienst:fyos/webapp", "nutzt"),
        Kante("vorgang:INC-1", "dienst:fyos/qdrant", "betrifft", vertrauen=0.6),
        Kante("dienst:fyos/qdrant", "pvc:fyos/qdrant-data", "mountet"),
        Kante("pvc:fyos/qdrant-data", "pv:qdrant-pv", "bindet"),
        Kante("pv:qdrant-pv", "knoten:node-a", "liegt_auf"),
    ]
    g.einspeisen("test", knoten, kanten)


@braucht_db
def test_einspeisen_ist_idempotent_und_schreibt_historie(graph: bg.Graph) -> None:
    _beispiel(graph)
    s = graph.einspeisen("test", [], [], schutz=False)  # leerer Stand schliesst alles
    assert s["knoten_zu"] == 10 and s["kanten_zu"] == 11
    assert graph.aktuelle_knoten() == []
    _beispiel(graph)  # wieder da: neue Zeilen, alte bleiben als Historie
    n = graph._ausfuehren("SELECT count(*) FROM knoten")[0][0]
    assert n == 20
    # unverändert nochmal: nichts passiert
    zeilen_vorher = graph._ausfuehren("SELECT count(*) FROM kanten")[0][0]
    _beispiel(graph)
    assert graph._ausfuehren("SELECT count(*) FROM kanten")[0][0] == zeilen_vorher


@braucht_db
def test_aenderung_schliesst_alte_fassung(graph: bg.Graph) -> None:
    graph.einspeisen("test", [_k("dienst:fyos/a", replikas=1)], [Kante("dienst:fyos/a", "knoten:x", "laeuft_auf")])
    s = graph.einspeisen("test", [_k("dienst:fyos/a", replikas=2)], [Kante("dienst:fyos/a", "knoten:x", "laeuft_auf")])
    assert s["knoten_geaendert"] == 1 and s["kanten_geaendert"] == 0
    alt = graph._ausfuehren("SELECT attribute, gueltig_bis IS NULL FROM knoten WHERE id = 'dienst:fyos/a' ORDER BY zeile")
    assert [r[1] for r in alt] == [False, True]
    assert alt[0][0]["replikas"] == 1 and alt[1][0]["replikas"] == 2
    ev = graph.verlauf(["dienst:fyos/a"], "1d")
    assert any("replikas: 1 → 2" in e["text"] for e in ev)


@braucht_db
def test_fremde_knoten_werden_nicht_uebernommen_und_schwund_bricht_ab(graph: bg.Graph) -> None:
    graph.einspeisen("eins", [_k("dienst:fyos/a")], [])
    s = graph.einspeisen("zwei", [_k("dienst:fyos/a"), _k("dienst:fyos/b")], [])
    assert s["kollisionen"] == ["dienst:fyos/a"] and s["knoten"] == 1
    viele = [_k(f"dienst:fyos/s{i}") for i in range(30)]
    graph.einspeisen("gross", viele, [])
    with pytest.raises(bg.GraphFehler):
        graph.einspeisen("gross", viele[:5], [])
    assert len(graph._ausfuehren("SELECT 1 FROM knoten WHERE quelle = 'gross' AND gueltig_bis IS NULL")) == 30


@braucht_db
def test_aufloesen(graph: bg.Graph) -> None:
    _beispiel(graph)
    assert graph.aufloesen("dienst:fyos/qdrant")["wie"] == "id"
    r = graph.aufloesen("qdrant")
    assert r["wie"] == "name" and "dienst:fyos/qdrant" in r["ids"] and "service:fyos/qdrant" in r["ids"]
    assert graph.aufloesen("qdrant-64f566945f-bq4gw")["wie"] == "pod"
    assert graph.aufloesen("qdr")["wie"] == "teil"
    assert graph.aufloesen("qdrant", typ="dienst")["ids"] == ["dienst:fyos/qdrant"]
    assert graph.aufloesen("gibtsnicht")["wie"] == "keiner"


@braucht_db
def test_abhaengig_rekursiv_mit_tiefe(graph: bg.Graph) -> None:
    _beispiel(graph)
    flach = graph.abhaengig(["dienst:fyos/qdrant"], tiefe=1)
    assert {z["id"] for z in flach} == {"service:fyos/qdrant"}
    tief = {z["id"]: z for z in graph.abhaengig(["dienst:fyos/qdrant"], tiefe=4)}
    assert {"service:fyos/qdrant", "route:qdrant.example", "dienst:fyos/webapp"} <= set(tief)
    assert tief["route:qdrant.example"]["tiefe"] == 2 and tief["route:qdrant.example"]["via"] == "service:fyos/qdrant"
    # webapp nutzt den Service nur weich: sie steht in der Liste, ihre eigenen Nutzer (plugin:graph) aber nicht mehr
    assert "plugin:graph" not in tief
    # Info-Kanten (betrifft) zählen nicht zu den tragenden
    assert "vorgang:INC-1" not in tief
    mit_info = {z["id"] for z in graph.abhaengig(["dienst:fyos/qdrant"], tiefe=2, arten=bg.ALLE_ARTEN)}
    assert "vorgang:INC-1" in mit_info
    # Gegenrichtung
    braucht = {z["id"] for z in graph.abhaengig(["dienst:fyos/qdrant"], tiefe=4, richtung="braucht")}
    assert {"knoten:node-a", "pvc:fyos/qdrant-data", "pv:qdrant-pv"} <= braucht
    # weiches nutzt: sichtbar, aber nicht weiterverfolgt
    assert {z["id"] for z in graph.abhaengig(["plugin:graph"], tiefe=4, richtung="braucht")} == {"dienst:fyos/webapp"}


@braucht_db
def test_abhaengig_kommt_mit_zyklen_klar(graph: bg.Graph) -> None:
    graph.einspeisen(
        "z",
        [_k("dienst:fyos/a"), _k("dienst:fyos/b"), _k("dienst:fyos/c")],
        [Kante("dienst:fyos/a", "dienst:fyos/b", "braucht"), Kante("dienst:fyos/b", "dienst:fyos/c", "braucht"),
         Kante("dienst:fyos/c", "dienst:fyos/a", "braucht")],
    )
    ids = {z["id"] for z in graph.abhaengig(["dienst:fyos/a"], tiefe=12)}
    assert ids == {"dienst:fyos/b", "dienst:fyos/c"}


@braucht_db
def test_auswirkung_oder_gruppe(graph: bg.Graph) -> None:
    _beispiel(graph)
    a = graph.auswirkung(["knoten:node-a"])
    aus = {z["id"] for z in a["ausgefallen"]}
    # qdrant ist hart auf node-a, damit Service, Route und PV/PVC
    assert {"dienst:fyos/qdrant", "service:fyos/qdrant", "route:qdrant.example", "pv:qdrant-pv", "pvc:fyos/qdrant-data"} <= aus
    # webapp nutzt qdrant nur (weich) und läuft auf node-b weiter (eingeschränkt); plugin:graph nutzt webapp, das noch
    # läuft, und merkt nichts: weder ausgefallen noch beeinträchtigt (weich gilt nur den direkten Nutzern)
    assert "dienst:fyos/webapp" not in aus and "plugin:graph" not in aus
    assert [z["id"] for z in a["eingeschraenkt"]] == ["dienst:fyos/webapp"]
    weich = {z["id"] for z in a["beeintraechtigt"]}
    assert "plugin:graph" not in weich
    # Der Vorgang hängt nur über eine Info-Kante dran
    assert "vorgang:INC-1" not in aus and "vorgang:INC-1" not in weich
    # node-b allein: webapp läuft auf node-a weiter → eingeschränkt, nicht ausgefallen
    b = graph.auswirkung(["knoten:node-b"])
    assert b["ausgefallen"] == []
    assert [z["id"] for z in b["eingeschraenkt"]] == ["dienst:fyos/webapp"]


@braucht_db
def test_nutzt_mit_hart_gibt_den_ausfall_weiter(graph: bg.Graph) -> None:
    graph.einspeisen(
        "h",
        [_k("dienst:fyos/a"), _k("dienst:fyos/b"), _k("dienst:fyos/c")],
        [Kante("dienst:fyos/b", "dienst:fyos/a", "nutzt", attribute={"hart": True}),
         Kante("dienst:fyos/c", "dienst:fyos/a", "nutzt")],
    )
    r = graph.auswirkung(["dienst:fyos/a"])
    assert [z["id"] for z in r["ausgefallen"]] == ["dienst:fyos/b"]
    assert [z["id"] for z in r["beeintraechtigt"]] == ["dienst:fyos/c"]


@braucht_db
def test_auswirkung_frei_gepinnt_wird_umgeplant(graph: bg.Graph) -> None:
    graph.einspeisen(
        "u",
        [_k("knoten:a"), _k("dienst:fyos/frei")],
        [Kante("dienst:fyos/frei", "knoten:a", "laeuft_auf", attribute={"pin": "frei"})],
    )
    r = graph.auswirkung(["knoten:a"])
    assert r["ausgefallen"] == [] and [z["id"] for z in r["umgeplant"]] == ["dienst:fyos/frei"]


@braucht_db
def test_pfad(graph: bg.Graph) -> None:
    _beispiel(graph)
    p = graph.pfad(["route:qdrant.example"], ["knoten:node-b"])
    assert p["gefunden"] and p["kette"][0]["id"] == "route:qdrant.example" and p["kette"][-1]["id"] == "knoten:node-b"
    assert not p["nur_info"]
    q = graph.pfad(["vorgang:INC-1"], ["plugin:graph"])
    assert q["gefunden"] and q["nur_info"]
    assert graph.pfad(["route:qdrant.example"], ["niemand"])["gefunden"] is False


@braucht_db
def test_verlauf_zeigt_neu_und_weg(graph: bg.Graph) -> None:
    graph.einspeisen("v", [_k("dienst:fyos/a")], [Kante("dienst:fyos/a", "dienst:fyos/b", "nutzt")])
    graph.einspeisen("v", [_k("dienst:fyos/a")], [])
    ev = graph.verlauf(["dienst:fyos/a"], "1h")
    was = {(e["was"], e["ding"]) for e in ev}
    assert ("neu", "knoten") in was and ("neu", "kante") in was and ("weg", "kante") in was


@braucht_db
def test_zustand_und_nutzung(graph: bg.Graph) -> None:
    _beispiel(graph)
    lauf = graph.lauf_beginnen("test")
    graph.lauf_beenden(lauf, ok=True, stand={"knoten": 10, "kanten": 11})
    graph.nutzung_vermerken("graph.abhaengig", "test", "qdrant", 3, 12, True)
    z = graph.zustand()
    assert z["quellen"]["test"]["knoten"] == 10 and z["quellen"]["test"]["kanten"] == 11
    assert z["quellen"]["test"]["letzter_ok"] is True
    assert z["nutzung_30t"][0]["werkzeug"] == "graph.abhaengig"
    assert z["kanten_ohne_knoten"] == 0
    graph.markieren("x", {"a": 1})
    assert graph.markierung("x") == {"a": 1}
