"""Plugin graph (Betriebsgraph): Auflösen, knappe Antworten, voll, Fehlerfälle, Zählvermerk — mit einem Fake-Graphen,
ohne Datenbank. Die Logik gegen echtes Postgres prüft test_betriebsgraph.py."""

from __future__ import annotations

import datetime as dt

import pytest

from fyos_plugin_betrieb.graph import LAUFEND, VOLL_MAX, GraphPlugin


class FakeConn:
    def __init__(self) -> None:
        self.rollbacks = 0
        self.zu = False

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.zu = True


class FakeGraph:
    """Antwortet mit festen Daten; merkt sich die Aufrufe."""

    def __init__(self) -> None:
        self.conn = FakeConn()
        self.aufrufe: list[tuple] = []
        self.vermerke: list[tuple] = []
        self.fehler_beim_naechsten: Exception | None = None
        self.tabelle = {
            "qdrant": {"wie": "name", "ids": ["dienst:fyos/qdrant", "service:fyos/qdrant", "image:qdrant/qdrant"]},
            "dienst:fyos/webapp": {"wie": "id", "ids": ["dienst:fyos/webapp"]},
            "node-b": {"wie": "name", "ids": ["knoten:node-b"]},
            "a": {"wie": "teil", "ids": [f"plugin:p{i}" for i in range(6)]},
        }

    def aufloesen(self, text, *, typ=None, grenze=25):
        self.aufrufe.append(("aufloesen", text))
        if self.fehler_beim_naechsten:
            e, self.fehler_beim_naechsten = self.fehler_beim_naechsten, None
            raise e
        t = self.tabelle.get(text)
        if not t:
            return {"ids": [], "wie": "keiner", "treffer": [], "weitere": 0}
        return {**t, "treffer": [{"id": i, "typ": i.split(":")[0], "name": i.split("/")[-1].split(":")[-1]} for i in t["ids"]], "weitere": 0}

    def abhaengig(self, start, *, tiefe=3, richtung="an", arten=None, hart_nutzt=False):
        self.aufrufe.append(("abhaengig", list(start), tiefe, richtung, arten))
        z = [{"id": f"cronjob:fyos/job{i}", "tiefe": 1, "via": start[0], "art": "nutzt", "typ": "cronjob", "name": f"job{i}"} for i in range(3)]
        z.append({"id": "service:fyos/qdrant", "tiefe": 1, "via": start[0], "art": "waehlt", "typ": "service", "name": "qdrant"})
        return z

    def auswirkung(self, start, *, tiefe=12):
        self.aufrufe.append(("auswirkung", list(start)))
        zeile = lambda i, **x: {"id": i, "typ": i.split(":")[0], "name": i.split("/")[-1], **x}  # noqa: E731
        return {
            "ausgefallen": [zeile("service:fyos/qdrant", tiefe=1, via=start[0], art="waehlt")],
            "umgeplant": [],
            "eingeschraenkt": [zeile("dienst:fyos/webapp", via=start[0], art="laeuft_auf", von_n=2, weg_n=1)],
            "beeintraechtigt": [zeile("cronjob:fyos/pg-dump", tiefe=1, via=start[0], art="nutzt")],
            "kandidaten": 3,
        }

    def pfad(self, von, nach, *, tiefe=6):
        self.aufrufe.append(("pfad", list(von), list(nach)))
        if nach == ["knoten:node-b"]:
            return {
                "gefunden": True,
                "laenge": 2,
                "nur_info": False,
                "kette": [
                    {"id": "plugin:modell", "art": None, "richtung": None},
                    {"id": "service:fyos/modell-tor", "art": "nutzt", "richtung": "vor"},
                    {"id": "knoten:node-b", "art": "laeuft_auf", "richtung": "vor"},
                ],
            }
        return {"gefunden": False, "laenge": None, "kette": [], "nur_info": False}

    def knoten_info(self, ids):
        return {i: {"id": i, "typ": i.split(":")[0], "name": i.split("/")[-1].split(":")[-1]} for i in ids}

    def verlauf(self, ids, seit, *, grenze=200):
        self.aufrufe.append(("verlauf", list(ids), seit, grenze))
        t = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.timezone.utc)
        return [{"ts": t, "was": "geaendert", "ding": "knoten", "id": ids[0], "text": "image: a → b"} for _ in range(12)]

    def zustand(self):
        return {
            "quellen": {
                "k8s": {"knoten": 10, "kanten": 20, "letzter_ok": True, "zuletzt_ok": dt.datetime(2026, 10, 9, 1, 0, tzinfo=dt.timezone.utc)},
                "itsm": {"knoten": 5, "kanten": 6, "letzter_ok": False, "fehler": "Zeitüberschreitung"},
            },
            "typen": {"dienst": 10},
            "kanten_ohne_knoten": 0,
            "nutzung_30t": [{"werkzeug": "graph.auswirkung", "aufrufer": "claude", "anzahl": 3}],
        }

    def nutzung_vermerken(self, werkzeug, aufrufer, ziel, treffer, dauer_ms, ok):
        self.vermerke.append((werkzeug, aufrufer, ziel, treffer, ok))


@pytest.fixture()
def fake() -> FakeGraph:
    return FakeGraph()


@pytest.fixture()
def plugin(fake: FakeGraph) -> GraphPlugin:
    return GraphPlugin(graph_factory=lambda: fake)


def test_werkzeuge_und_schemas(plugin: GraphPlugin) -> None:
    namen = {t.name for t in plugin.describe_tools()}
    assert namen == {"abhaengig", "auswirkung", "pfad", "verlauf", "zustand"}
    for t in plugin.describe_tools():
        assert t.input_schema["additionalProperties"] is False
        assert "read" in t.tags


def test_abhaengig_ist_knapp_und_nimmt_das_laufende(plugin: GraphPlugin, fake: FakeGraph) -> None:
    r = plugin.call("abhaengig", {"ziel": "qdrant"})
    # Image und Service gleichen Namens fallen weg, der Dienst bleibt
    assert r["ziel"] == ["dienst:fyos/qdrant"]
    assert r["anzahl"] == 4 and "eintraege" not in r
    assert r["nach_typ"]["cronjob"]["anzahl"] == 3 and "voll=true" in r["hinweis"]
    assert fake.aufrufe[1][:2] == ("abhaengig", ["dienst:fyos/qdrant"])


def test_abhaengig_voll_und_typfilter(plugin: GraphPlugin) -> None:
    r = plugin.call("abhaengig", {"ziel": "qdrant", "voll": True, "typ": "cronjob"})
    assert len(r["eintraege"]) == 3 and r["eintraege"][0]["via"] == "dienst:fyos/qdrant" and r["nur_typ"] == "cronjob"


def test_abhaengig_prueft_arten_und_richtung(plugin: GraphPlugin) -> None:
    with pytest.raises(ValueError, match="unbekannte Kantenart"):
        plugin.call("abhaengig", {"ziel": "qdrant", "arten": ["gibtsnicht"]})
    with pytest.raises(ValueError):
        plugin.call("abhaengig", {"ziel": ""})


def test_unbekanntes_ziel_und_mehrdeutiger_teilstring(plugin: GraphPlugin) -> None:
    r = plugin.call("abhaengig", {"ziel": "xyzzy"})
    assert r["gefunden"] is False and r["anzahl"] == 0
    r = plugin.call("auswirkung", {"ziel": "a"})
    assert r["mehrdeutig"] is True and len(r["kandidaten"]) == 6


def test_auswirkung_kurz_und_voll(plugin: GraphPlugin) -> None:
    r = plugin.call("auswirkung", {"ziel": "node-b"})
    assert r["zahlen"] == {"ausgefallen": 1, "umgeplant": 0, "eingeschraenkt": 1, "beeintraechtigt": 1}
    assert "1 fallen aus" in r["satz"] and "eingeschränkt" in r["satz"] and "beeinträchtigt" in r["satz"]
    assert r["ausgefallen"]["service"]["namen"] == ["qdrant"] and "umgeplant" not in r
    v = plugin.call("auswirkung", {"ziel": "node-b", "voll": True})
    assert v["ausgefallen"][0]["via"] == "knoten:node-b" and v["eingeschraenkt"][0]["weg_n"] == 1


def test_auswirkung_mehrere_ziele(plugin: GraphPlugin, fake: FakeGraph) -> None:
    plugin.call("auswirkung", {"ziel": "node-b, dienst:fyos/webapp"})
    assert fake.aufrufe[-1] == ("auswirkung", ["knoten:node-b", "dienst:fyos/webapp"])


def test_pfad(plugin: GraphPlugin) -> None:
    r = plugin.call("pfad", {"von": "dienst:fyos/webapp", "nach": "node-b"})
    assert r["gefunden"] and r["laenge"] == 2
    assert "modell —nutzt→ modell-tor —laeuft_auf→ node-b" in r["satz"] and "kette" not in r
    assert plugin.call("pfad", {"von": "dienst:fyos/webapp", "nach": "node-b", "voll": True})["kette"][1]["art"] == "nutzt"
    r = plugin.call("pfad", {"von": "dienst:fyos/webapp", "nach": "dienst:fyos/webapp"})
    assert r["gefunden"] is False


def test_verlauf_kurz_zeigt_acht_voll_alle(plugin: GraphPlugin, fake: FakeGraph) -> None:
    r = plugin.call("verlauf", {"ziel": "node-b", "seit": "2d"})
    assert r["anzahl"] == 12 and len(r["ereignisse"]) == 8 and "voll=true" in r["hinweis"]
    assert r["ereignisse"][0]["ts"] == "2026-10-08 14:00"  # Berlin
    assert fake.aufrufe[-1][2:] == ("2d", 60)
    v = plugin.call("verlauf", {"ziel": "node-b", "voll": True})
    assert len(v["ereignisse"]) == 12 and fake.aufrufe[-1][2:] == ("7d", VOLL_MAX)


def test_zustand(plugin: GraphPlugin) -> None:
    r = plugin.call("zustand", {})
    assert r["anzahl"] == 15 and "itsm" in r["satz"] and r["quellen"]["itsm"]["fehler"] == "Zeitüberschreitung"
    assert r["quellen"]["k8s"]["zuletzt_ok"] == "2026-10-09 03:00"


def test_jeder_aufruf_wird_gezaehlt_und_die_transaktion_beendet(plugin: GraphPlugin, fake: FakeGraph) -> None:
    plugin.call("auswirkung", {"ziel": "node-b"})
    assert fake.vermerke[-1][0] == "graph.auswirkung" and fake.vermerke[-1][2] == "node-b" and fake.vermerke[-1][4] is True
    assert fake.conn.rollbacks >= 1
    with pytest.raises(ValueError):
        plugin.call("abhaengig", {"ziel": "qdrant", "arten": ["x"]})
    assert fake.vermerke[-1][0] == "graph.abhaengig" and fake.vermerke[-1][4] is False


def test_tote_verbindung_wird_einmal_neu_aufgebaut(fake: FakeGraph) -> None:
    gebaut = []

    def fabrik() -> FakeGraph:
        gebaut.append(1)
        return fake

    p = GraphPlugin(graph_factory=fabrik)
    p.call("zustand", {})
    fake.fehler_beim_naechsten = RuntimeError("connection closed")
    r = p.call("abhaengig", {"ziel": "qdrant"})
    assert r["anzahl"] == 4 and len(gebaut) == 2 and fake.conn.zu


def test_zweiter_fehler_geht_nach_oben(fake: FakeGraph) -> None:
    class Kaputt(FakeGraph):
        def aufloesen(self, *a, **k):
            raise RuntimeError("down")

    p = GraphPlugin(graph_factory=Kaputt)
    with pytest.raises(RuntimeError, match="down"):
        p.call("abhaengig", {"ziel": "qdrant"})


def test_health(plugin: GraphPlugin, monkeypatch: pytest.MonkeyPatch) -> None:
    assert plugin.health()["ok"] is True
    monkeypatch.delenv("GRAPH_DB_URL", raising=False)
    leer = GraphPlugin()
    h = leer.health()
    # nicht eingerichtet = „aus“ (weder gesund noch krank), damit ein ungenutztes Plugin die Ampel nicht rot färbt
    assert h["ok"] is False and h["status"] == "aus" and "GRAPH_DB_URL" in h["reason"]
    eigener = GraphPlugin(config={"url_env": "MEIN_GRAPH"})
    assert "MEIN_GRAPH" in eigener.health()["reason"]


def test_unbekanntes_werkzeug(plugin: GraphPlugin) -> None:
    with pytest.raises(ValueError, match="kein Werkzeug"):
        plugin.call("loeschen", {})


def test_laufend_enthaelt_dienste() -> None:
    assert {"dienst", "cronjob", "plugin", "rechner", "knoten"} <= LAUFEND
