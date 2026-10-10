"""Der Einspeiser des Betriebsgraphen: Ablauf über mehrere Quellen, Rückgabewerte und Aufrufformen.

Ohne Datenbank: Verbindung und Graph sind Attrappen, die aufzeichnen, was der Einspeiser mit ihnen tut. Die echte
Datenbank prüft `test_betriebsgraph.py` (mit GRAPH_TEST_DB_URL)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from fyos_plugin_betrieb.betriebsgraph import GraphFehler, Kante, Knoten
from fyos_plugin_betrieb.graph_einspeisen import db_einrichten, einspeisen
from fyos_plugin_betrieb.graph_einspeisen.kontext import Ausgeschaltet, Kontext

WURZEL = Path(__file__).resolve().parent.parent
BEISPIEL = WURZEL / "fyos_plugin_betrieb" / "daten" / "rechner.beispiel.yaml"

A = Knoten("dienst:fyos/a", "dienst", "a")
P = Knoten("pvc:fyos/p", "pvc", "p")
M = Kante("dienst:fyos/a", "pvc:fyos/p", "mountet")


# ── Attrappen ────────────────────────────────────────────────────────────────


class Verbindung:
    """Merkt sich nur, ob zurückgerollt und geschlossen wurde."""

    def __init__(self) -> None:
        self.ereignisse: list[str] = []
        self.rollback_fehler: Exception | None = None

    def rollback(self) -> None:
        self.ereignisse.append("rollback")
        if self.rollback_fehler is not None:
            raise self.rollback_fehler

    def close(self) -> None:
        self.ereignisse.append("close")


class Graph:
    """Zeichnet auf, was der Einspeiser mit dem Graphen tut. `fehler_bei[quelle]` lässt `einspeisen` scheitern."""

    letzter: Graph
    fehler_bei: dict[str, Exception] = {}

    def __init__(self, conn: Verbindung) -> None:
        self.conn = conn
        self.aufrufe: list[tuple] = []
        self._lauf = 0
        Graph.letzter = self

    def schema_anlegen(self) -> None:
        self.aufrufe.append(("schema_anlegen",))

    def _ausfuehren(self, sql: str, params: Any = None) -> list[tuple]:
        self.aufrufe.append(("sql", " ".join(sql.split())))
        return [("dienst:fyos/schon-da",)]

    def lauf_beginnen(self, quelle: str) -> int:
        self._lauf += 1
        self.aufrufe.append(("lauf_beginnen", quelle))
        return self._lauf

    def lauf_beenden(self, lauf: int, *, ok: bool, stand: dict | None = None, fehler: str | None = None) -> None:
        self.aufrufe.append(("lauf_beenden", lauf, ok, stand, fehler))

    def einspeisen(self, quelle: str, knoten: Any, kanten: Any, *, schutz: bool = True) -> dict[str, Any]:
        knoten, kanten = list(knoten), list(kanten)
        self.aufrufe.append(("einspeisen", quelle, len(knoten), len(kanten), schutz))
        if quelle in Graph.fehler_bei:
            raise Graph.fehler_bei[quelle]
        return {"knoten": len(knoten), "kanten": len(kanten), "knoten_neu": len(knoten), "knoten_zu": 0, "kanten_neu": len(kanten), "kanten_zu": 0}


@pytest.fixture()
def datenbank(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    conn = Verbindung()
    verbunden: list[tuple] = []

    def verbinden(url: str | None = None, **kw: Any) -> Verbindung:
        verbunden.append((url, kw))
        return conn

    monkeypatch.setattr(einspeisen, "verbinden", verbinden)
    monkeypatch.setattr(einspeisen, "Graph", Graph)
    monkeypatch.setattr(Graph, "fehler_bei", {})
    return SimpleNamespace(conn=conn, verbunden=verbunden)


def quelle(name: str, *, knoten=(), kanten=(), fehler: Exception | None = None, schutz: bool | None = None, nachlauf=None) -> SimpleNamespace:
    """Eine Quelle als Modul-Attrappe; `sammeln.bekannt` hält, was der Kontext beim Aufruf schon kannte."""

    def sammeln(ctx: Kontext):
        sammeln.bekannt = set(ctx.bekannt)  # type: ignore[attr-defined]
        if fehler is not None:
            raise fehler
        return list(knoten), list(kanten)

    modul = SimpleNamespace(NAME=name, sammeln=sammeln)
    if schutz is not None:
        modul.SCHUTZ = schutz
    if nachlauf is not None:
        modul.nachlauf = nachlauf
    return modul


def installieren(monkeypatch: pytest.MonkeyPatch, **quellen: SimpleNamespace) -> None:
    monkeypatch.setattr(einspeisen, "quelle_laden", quellen.__getitem__)


# ── die Liste der Quellen ────────────────────────────────────────────────────


@pytest.mark.parametrize("name", einspeisen.QUELLEN)
def test_jede_quelle_ist_ladbar(name: str) -> None:
    modul = einspeisen.quelle_laden(name)
    assert modul.NAME == name and callable(modul.sammeln)


def test_quellen_liste_ist_vollstaendig_und_itsm_kommt_zuletzt() -> None:
    verzeichnis = Path(einspeisen.__file__).parent / "quellen"
    gefunden = {p.stem for p in verzeichnis.glob("*.py") if not p.name.startswith("_")}
    assert gefunden == set(einspeisen.QUELLEN)  # eine neue Quelle gehört auch in QUELLEN
    assert einspeisen.QUELLEN[-1] == "itsm"  # sein Nachlauf braucht den Stand der anderen


# ── lauf ohne Datenbank ──────────────────────────────────────────────────────


def test_trocken_zaehlt_und_laesst_die_datenbank_in_ruhe(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(einspeisen, "verbinden", lambda *a, **k: pytest.fail("--trocken öffnet keine Datenbank"))
    installieren(
        monkeypatch,
        a=quelle("a", knoten=[A, P], kanten=[M]),
        b=quelle("b", fehler=Ausgeschaltet("nichts eingerichtet")),
        c=quelle("c", fehler=RuntimeError("kaputt")),
    )
    erg = einspeisen.lauf(["a", "b", "c"], trocken=True, db_url=None, ctx=Kontext(umgebung={}, trocken=True))
    assert erg["a"]["ok"] is True and (erg["a"]["knoten"], erg["a"]["kanten"]) == (2, 1)
    assert erg["a"]["typen"] == {"dienst": 1, "pvc": 1} and erg["a"]["arten"] == {"mountet": 1}
    assert erg["b"]["ok"] is None and erg["b"]["aus"] == "nichts eingerichtet"
    assert erg["c"]["ok"] is False and erg["c"]["fehler"] == "RuntimeError: kaputt"
    zeilen = [json.loads(z) for z in capsys.readouterr().out.splitlines()]
    assert [next(iter(z)) for z in zeilen] == ["a", "b", "c"]  # je Quelle eine Zeile, in der Reihenfolge des Aufrufs
    assert all("sekunden" in z[next(iter(z))] for z in zeilen)


def test_spaetere_quellen_kennen_die_ids_der_frueheren(monkeypatch: pytest.MonkeyPatch) -> None:
    erste, zweite = quelle("erste", knoten=[A, P]), quelle("zweite")
    installieren(monkeypatch, erste=erste, zweite=zweite)
    einspeisen.lauf(["erste", "zweite"], trocken=True, db_url=None, ctx=Kontext(umgebung={}, trocken=True))
    assert erste.sammeln.bekannt == set() and zweite.sammeln.bekannt == {A.id, P.id}  # type: ignore[attr-defined]


def test_eine_unbekannte_quelle_ist_ein_fehler_der_quelle_nicht_des_laufs(monkeypatch: pytest.MonkeyPatch) -> None:
    installieren(monkeypatch, a=quelle("a", knoten=[A]))
    erg = einspeisen.lauf(["fehlt", "a"], trocken=True, db_url=None, ctx=Kontext(umgebung={}, trocken=True))
    assert erg["fehlt"]["ok"] is False and erg["a"]["ok"] is True


# ── lauf mit Datenbank (Attrappe) ────────────────────────────────────────────


def test_datenbanklauf_je_quelle_ein_eintrag_und_ein_fehler_nimmt_die_anderen_nicht_mit(
    monkeypatch: pytest.MonkeyPatch, datenbank: SimpleNamespace, capsys: pytest.CaptureFixture[str]
) -> None:
    nachlaeufe: list[Any] = []

    def nachlauf(ctx: Kontext, graph: Graph) -> dict[str, str]:
        nachlaeufe.append(graph)
        return {"kommentare": "aus"}

    Graph.fehler_bei["schwund"] = GraphFehler("Quelle schwindet: 40 -> 3")
    installieren(
        monkeypatch,
        gut=quelle("gut", knoten=[A, P], kanten=[M], schutz=False, nachlauf=nachlauf),
        aus=quelle("aus", fehler=Ausgeschaltet("nicht eingerichtet")),
        kaputt=quelle("kaputt", fehler=RuntimeError("Lesefehler")),
        schwund=quelle("schwund", knoten=[A]),
        danach=quelle("danach", knoten=[P]),
    )
    ctx = Kontext(umgebung={}, trocken=False)
    adresse = "postgresql://nutzer:geheimwort@host/db"
    erg = einspeisen.lauf(["gut", "aus", "kaputt", "schwund", "danach"], trocken=False, db_url=adresse, ctx=ctx)

    assert datenbank.verbunden == [(adresse, {"anweisung_s": 120})]
    stand_gut = {"knoten": 2, "kanten": 1, "knoten_neu": 2, "knoten_zu": 0, "kanten_neu": 1, "kanten_zu": 0}
    stand_danach = {"knoten": 1, "kanten": 0, "knoten_neu": 1, "knoten_zu": 0, "kanten_neu": 0, "kanten_zu": 0}
    assert Graph.letzter.aufrufe == [
        ("schema_anlegen",),
        ("sql", "SELECT id FROM knoten WHERE gueltig_bis IS NULL"),
        # gut: Lauf, Abgleich ohne Schwundschutz (SCHUTZ = False), Nachlauf, Lauf beendet
        ("lauf_beginnen", "gut"),
        ("einspeisen", "gut", 2, 1, False),
        ("lauf_beenden", 1, True, {**stand_gut, "nachlauf": {"kommentare": "aus"}}, None),
        # aus: nichts, kein Eintrag in `laeufe`
        # kaputt: scheitert beim Lesen, der Fehler bekommt trotzdem einen Eintrag
        ("lauf_beginnen", "kaputt"),
        ("lauf_beenden", 2, False, None, "RuntimeError: Lesefehler"),
        # schwund: scheitert beim Abgleich, derselbe Lauf wird mit dem Fehler beendet
        ("lauf_beginnen", "schwund"),
        ("einspeisen", "schwund", 1, 0, True),
        ("lauf_beenden", 3, False, None, "GraphFehler: Quelle schwindet: 40 -> 3"),
        # danach: läuft weiter, mit Schwundschutz (Vorgabe)
        ("lauf_beginnen", "danach"),
        ("einspeisen", "danach", 1, 0, True),
        ("lauf_beenden", 4, True, stand_danach, None),
    ]
    assert nachlaeufe == [Graph.letzter]
    assert datenbank.conn.ereignisse == ["rollback", "rollback", "close"]  # je Fehler zurückgerollt, am Ende geschlossen
    assert [erg[n]["ok"] for n in ("gut", "aus", "kaputt", "schwund", "danach")] == [True, None, False, False, True]
    assert erg["gut"]["nachlauf"] == {"kommentare": "aus"} and erg["aus"]["aus"] == "nicht eingerichtet"
    assert erg["schwund"]["fehler"] == "GraphFehler: Quelle schwindet: 40 -> 3"
    # Was die Datenbank schon kannte, und was die Quellen im Lauf lieferten, steht im Kontext
    assert ctx.bekannt == {"dienst:fyos/schon-da", A.id, P.id}
    gedruckt = capsys.readouterr()
    assert "geheimwort" not in gedruckt.out + gedruckt.err  # die Adresse mit dem Passwort steht nie in der Ausgabe


def test_ist_die_verbindung_weg_laeuft_der_lauf_trotzdem_weiter(monkeypatch: pytest.MonkeyPatch, datenbank: SimpleNamespace) -> None:
    datenbank.conn.rollback_fehler = OSError("Verbindung weg")
    installieren(monkeypatch, kaputt=quelle("kaputt", fehler=RuntimeError("x")), danach=quelle("danach", knoten=[A]))
    erg = einspeisen.lauf(["kaputt", "danach"], trocken=False, db_url="postgresql://h/db", ctx=Kontext(umgebung={}))
    assert erg["kaputt"]["ok"] is False and erg["danach"]["ok"] is True
    assert datenbank.conn.ereignisse[-1] == "close"


def test_lange_fehlertexte_werden_gekuerzt(monkeypatch: pytest.MonkeyPatch) -> None:
    installieren(monkeypatch, a=quelle("a", fehler=RuntimeError("x" * 2000)))
    erg = einspeisen.lauf(["a"], trocken=True, db_url=None, ctx=Kontext(umgebung={}, trocken=True))
    assert len(erg["a"]["fehler"]) == 500


# ── leeren ───────────────────────────────────────────────────────────────────


def test_leeren_schliesst_alle_zeilen_der_quelle(datenbank: SimpleNamespace, capsys: pytest.CaptureFixture[str]) -> None:
    erg = einspeisen.leeren(["abgeschaltet", "alt"], db_url="postgresql://h/db")
    leer = {"knoten": 0, "kanten": 0, "knoten_neu": 0, "knoten_zu": 0, "kanten_neu": 0, "kanten_zu": 0}
    assert Graph.letzter.aufrufe == [
        ("lauf_beginnen", "abgeschaltet"), ("einspeisen", "abgeschaltet", 0, 0, False), ("lauf_beenden", 1, True, leer, None),
        ("lauf_beginnen", "alt"), ("einspeisen", "alt", 0, 0, False), ("lauf_beenden", 2, True, leer, None),
    ]
    assert datenbank.conn.ereignisse == ["close"] and all(v["ok"] for v in erg.values())
    assert len(capsys.readouterr().out.splitlines()) == 2


def test_leeren_meldet_fehler_und_macht_mit_der_naechsten_weiter(datenbank: SimpleNamespace) -> None:
    Graph.fehler_bei["alt"] = GraphFehler("Verbindung abgebrochen")
    erg = einspeisen.leeren(["alt", "abgeschaltet"])
    assert erg["alt"] == {"ok": False, "fehler": "GraphFehler: Verbindung abgebrochen"}
    assert erg["abgeschaltet"]["ok"] is True
    assert datenbank.conn.ereignisse == ["rollback", "close"]


# ── main ─────────────────────────────────────────────────────────────────────


def main_mit(monkeypatch: pytest.MonkeyPatch, ergebnis: dict | None = None, *, fehler: Exception | None = None) -> list[dict]:
    """`main` mit einem `lauf`, der aufzeichnet, was er bekam."""
    aufrufe: list[dict] = []

    def lauf(namen: list[str], *, trocken: bool, db_url: str | None, ctx: Kontext | None = None) -> dict:
        aufrufe.append({"namen": namen, "trocken": trocken, "db_url": db_url, "ctx_trocken": ctx.trocken if ctx else None})
        if fehler is not None:
            raise fehler
        return ergebnis or {}

    monkeypatch.setattr(einspeisen, "lauf", lauf)
    return aufrufe


def test_main_ohne_angabe_nimmt_alle_quellen(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GRAPH_QUELLEN", raising=False)
    aufrufe = main_mit(monkeypatch, {"k8s": {"ok": True}})
    assert einspeisen.main([]) == 0
    assert aufrufe == [{"namen": list(einspeisen.QUELLEN), "trocken": False, "db_url": None, "ctx_trocken": False}]


def test_main_quellen_aus_der_umgebung_und_die_option_gewinnt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GRAPH_QUELLEN", " k8s , flux ,")
    aufrufe = main_mit(monkeypatch, {"k8s": {"ok": True}})
    assert einspeisen.main(["--trocken"]) == 0
    assert aufrufe[-1] == {"namen": ["k8s", "flux"], "trocken": True, "db_url": None, "ctx_trocken": True}
    assert einspeisen.main(["--quellen", "caddy"]) == 0
    assert aufrufe[-1]["namen"] == ["caddy"]


@pytest.mark.parametrize(
    "ergebnis,code,hinweis",
    [
        ({"a": {"ok": True}, "b": {"ok": False}, "c": {"ok": None}}, 0, False),  # eine genügt
        ({"a": {"ok": False}, "b": {"ok": None}}, 1, False),
        ({"a": {"ok": False}}, 1, False),
        ({"a": {"ok": None}, "b": {"ok": None}}, 1, True),  # nichts eingerichtet: Rückgabe 1, mit Hinweis
    ],
)
def test_main_rueckgabewert(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], ergebnis: dict, code: int, hinweis: bool) -> None:
    main_mit(monkeypatch, ergebnis)
    assert einspeisen.main(["--quellen", "k8s"]) == code
    assert ("keine der gewählten Quellen ist eingerichtet" in capsys.readouterr().err) is hinweis


@pytest.mark.parametrize("quellen", ["k8s,gibtsnicht", "gibtsnicht", " , "])
def test_main_falscher_aufruf_ist_2_und_startet_nichts(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], quellen: str) -> None:
    aufrufe = main_mit(monkeypatch, {"k8s": {"ok": True}})
    assert einspeisen.main(["--quellen", quellen]) == 2
    assert aufrufe == [] and "unbekannte Quelle" in capsys.readouterr().err


def test_main_datenbank_nicht_erreichbar_ist_1_ohne_adresse_in_der_meldung(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    main_mit(monkeypatch, fehler=GraphFehler("GRAPH_DB_URL ist nicht gesetzt"))
    assert einspeisen.main(["--quellen", "k8s"]) == 1
    assert "Datenbank nicht erreichbar: GraphFehler: GRAPH_DB_URL ist nicht gesetzt" in capsys.readouterr().err


def test_main_leeren(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    gesehen: list[list[str]] = []

    def leeren(namen: list[str], **kw: Any) -> dict:
        gesehen.append(namen)
        return {n: {"ok": True} for n in namen}

    monkeypatch.setattr(einspeisen, "leeren", leeren)
    assert einspeisen.main(["--leeren", "abgeschaltet, alt"]) == 0 and gesehen == [["abgeschaltet", "alt"]]
    monkeypatch.setattr(einspeisen, "leeren", lambda namen, **kw: {"abgeschaltet": {"ok": False}})
    assert einspeisen.main(["--leeren", "abgeschaltet"]) == 1
    assert einspeisen.main(["--leeren", " , "]) == 2

    def weg(namen: list[str], **kw: Any) -> dict:
        raise GraphFehler("GRAPH_DB_URL ist nicht gesetzt")

    monkeypatch.setattr(einspeisen, "leeren", weg)
    assert einspeisen.main(["--leeren", "abgeschaltet"]) == 1
    assert "Datenbank nicht erreichbar" in capsys.readouterr().err


def test_main_einrichten_geht_an_die_einrichtung_der_datenbank(monkeypatch: pytest.MonkeyPatch) -> None:
    gesehen: list[Any] = []

    def einrichten(argv: list[str] | None = None) -> int:
        gesehen.append(argv)
        return 7

    monkeypatch.setattr(db_einrichten, "main", einrichten)
    assert einspeisen.main(["einrichten", "--pruefen"]) == 7
    assert gesehen == [["--pruefen"]]


def test_main_hilfe_nennt_aufrufe_und_quellen(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as ende:
        einspeisen.main(["--help"])
    assert ende.value.code == 0
    text = capsys.readouterr().out
    for stichwort in ("fyos-graph-einspeisen", "--trocken", "--leeren", "einrichten", *einspeisen.QUELLEN):
        assert stichwort in text, stichwort


# ── ganze Läufe mit den echten Quellen, ohne Cluster und ohne Datenbank ──────


EINGERICHTET = ("GRAPH_RECHNER_DATEN", "GRAPH_CADDYFILE", "GRAPH_CADDY_CONFIGMAP", "EXPERIMENTE_DIR", "GRAPH_BESTAND_REDIS_URL", "GRAPH_QUELLEN")


def _zeilen(ausgabe: str) -> dict[str, dict]:
    return {name: wert for zeile in ausgabe.splitlines() for name, wert in json.loads(zeile).items()}


def test_trocken_mit_echten_quellen_nur_was_eingerichtet_ist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str], ohne_vorgangsquelle: None
) -> None:
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("a.example.org {\n    reverse_proxy http://192.0.2.10:8080\n}\n", encoding="utf-8")
    ablage = tmp_path / "experimente"
    ablage.mkdir()
    (ablage / "e1.json").write_text(json.dumps({"id": "e1", "titel": "basis-q4 auf dem Modellserver"}), encoding="utf-8")
    for name in EINGERICHTET:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GRAPH_RECHNER_DATEN", str(BEISPIEL))
    monkeypatch.setenv("GRAPH_CADDYFILE", str(caddyfile))
    monkeypatch.setenv("EXPERIMENTE_DIR", str(ablage))

    code = einspeisen.main(["--trocken", "--quellen", "rechner,caddy,experimente,bestand,itsm"])

    erg = _zeilen(capsys.readouterr().out)
    assert code == 0
    assert erg["rechner"]["ok"] is True and erg["rechner"]["typen"] == {"rechner": 3, "speicher": 2, "artefakt": 2, "profil": 2}
    # Der Upstream 192.0.2.10:8080 ist laut Datei der Modellserver; die Route hängt an ihm
    assert erg["caddy"]["ok"] is True and erg["caddy"]["typen"] == {"route": 1} and erg["caddy"]["arten"] == {"routet_zu": 1}
    # Der Steckbrief nennt das Artefakt und (aus den Quellen davor) den Modellserver
    assert erg["experimente"]["ok"] is True and erg["experimente"]["arten"] == {"misst": 1, "betrifft": 1}
    assert erg["bestand"]["ok"] is None and "GRAPH_BESTAND_REDIS_URL" in erg["bestand"]["aus"]
    assert erg["itsm"]["ok"] is None  # keine Vorgangs-Quelle: aus, kein Fehler


def test_trocken_ohne_jede_einrichtung_ist_1_mit_hinweis(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], ohne_vorgangsquelle: None
) -> None:
    for name in EINGERICHTET:
        monkeypatch.delenv(name, raising=False)
    code = einspeisen.main(["--trocken", "--quellen", "caddy,rechner,experimente,bestand,itsm"])
    gedruckt = capsys.readouterr()
    assert code == 1 and "keine der gewählten Quellen ist eingerichtet" in gedruckt.err
    assert {n: v["ok"] for n, v in _zeilen(gedruckt.out).items()} == {n: None for n in ("caddy", "rechner", "experimente", "bestand", "itsm")}


# ── python -m ────────────────────────────────────────────────────────────────


def test_python_m_ruft_den_einspeiser() -> None:
    umgebung = {**os.environ, "PYTHONPATH": str(WURZEL)}
    r = subprocess.run(
        [sys.executable, "-m", "fyos_plugin_betrieb.graph_einspeisen", "--quellen", "gibtsnicht"],
        cwd=WURZEL, env=umgebung, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 2 and "unbekannte Quelle" in r.stderr
    r = subprocess.run(
        [sys.executable, "-m", "fyos_plugin_betrieb.graph_einspeisen", "--help"],
        cwd=WURZEL, env=umgebung, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0 and "--trocken" in r.stdout
