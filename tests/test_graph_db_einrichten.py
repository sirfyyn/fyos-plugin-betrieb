"""Einrichtung der Graph-Datenbank (`graph_einspeisen/db_einrichten.py`): Rollen, Datenbank, Rechte, Schema.

Ohne Postgres: die Verbindung ist eine Attrappe, die jede SQL-Anweisung aufzeichnet und aus einem kleinen Serverstand
antwortet. Geprüft werden Reihenfolge und Inhalt der Anweisungen, der Umgang mit Passwörtern und die Rückgabewerte; ob
Postgres die Anweisungen annimmt, zeigt ein Lauf gegen ein echtes Postgres (siehe README, „Betriebsgraph einrichten")."""

from __future__ import annotations

import json
from typing import Any

import pytest

from fyos_plugin_betrieb import betriebsgraph
from fyos_plugin_betrieb.graph_einspeisen import db_einrichten as einrichten

ADMIN = "postgresql://verwalter:adminpasswort@db-host:5432/postgres"


class Server:
    """Der Stand, aus dem die Attrappe antwortet; jede Verbindung hängt hier ihre Anweisungen an."""

    def __init__(self, *, rollen: list[str] | None = None, datenbank: bool = False, schema: int | None = None, fehler_bei: str = "") -> None:
        self.rollen = rollen or []
        self.datenbank = datenbank
        self.schema = schema  # Schemaversion; None = noch kein Schema
        self.fehler_bei = fehler_bei
        self.verbindungen: list[Verbindung] = []

    def verbinde(self, url: str) -> Verbindung:
        verbindung = Verbindung(self, url)
        self.verbindungen.append(verbindung)
        return verbindung

    def alle_anweisungen(self) -> list[str]:
        return [s for v in self.verbindungen for s in v.anweisungen]


class Cursor:
    def __init__(self, verbindung: Verbindung) -> None:
        self.verbindung = verbindung
        self.description: Any = None
        self._zeilen: list[tuple] = []

    def execute(self, sql: str) -> None:
        server = self.verbindung.server
        self.verbindung.anweisungen.append(sql)
        if server.fehler_bei and server.fehler_bei in sql:
            raise RuntimeError(f"Postgres lehnt ab: {sql[:80]}")
        text = " ".join(sql.split())
        if text.startswith("SELECT rolname FROM pg_roles"):
            self._zeilen = [(r,) for r in server.rollen]
        elif text.startswith(("SELECT datname FROM pg_database", "SELECT 1 FROM pg_database")):
            self._zeilen = [("fyos_graph",)] if server.datenbank else []
        elif text.startswith("SELECT max(version) FROM schema_stand"):
            if server.schema is None:
                raise RuntimeError('relation "schema_stand" does not exist')
            self._zeilen = [(server.schema,)]
        else:
            self._zeilen = []
            return
        self.description = [("spalte",)]

    def fetchall(self) -> list[tuple]:
        return self._zeilen

    def close(self) -> None:
        pass


class Verbindung:
    def __init__(self, server: Server, url: str) -> None:
        self.server = server
        self.url = url
        self.anweisungen: list[str] = []
        self.geschlossen = False
        self.autocommit = False

    def cursor(self) -> Cursor:
        return Cursor(self)

    def close(self) -> None:
        self.geschlossen = True


# ── Hilfen ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "wert,erwartet",
    [
        ("einfach", "'einfach'"),
        ("mit'Hochkomma", "'mit''Hochkomma'"),
        ("mit$$dollar%und\"quote", "'mit$$dollar%und\"quote'"),
        ("mit\\backslash", "E'mit\\\\backslash'"),  # mit Backslash die E-Form, unabhängig von standard_conforming_strings
        ("beides\\'", "E'beides\\\\'''"),
    ],
)
def test_quoten(wert: str, erwartet: str) -> None:
    assert einrichten.quoten(wert) == erwartet


def test_quoten_lehnt_nul_ab() -> None:
    with pytest.raises(ValueError, match="NUL"):
        einrichten.quoten("a\x00b")


def test_datenbank_url_tauscht_nur_den_pfad() -> None:
    assert einrichten.datenbank_url(ADMIN, "fyos_graph") == "postgresql://verwalter:adminpasswort@db-host:5432/fyos_graph"
    assert einrichten.datenbank_url("postgresql://u:p@h/postgres?sslmode=require", "x") == "postgresql://u:p@h/x?sslmode=require"
    assert einrichten.datenbank_url("postgresql://u:p@h", "x") == "postgresql://u:p@h/x"


def test_ohne_geheimnisse() -> None:
    assert einrichten.ohne_geheimnisse("Passwort pw1 und pw2 und pw1", "pw1", "", "pw2") == "Passwort *** und *** und ***"
    assert einrichten.ohne_geheimnisse("unverändert") == "unverändert"


def test_die_rollennamen_stimmen_mit_dem_schema_ueberein() -> None:
    """`SCHEMA_SQL` nennt die Leserolle beim Namen; weichen die Namen voneinander ab, bekäme der Leser kein INSERT."""
    assert f"'{einrichten.LESER}'" in betriebsgraph.SCHEMA_SQL
    assert einrichten.DB == einrichten.EIGNER == "fyos_graph"


def test_admin_verbindung_ist_ohne_transaktion(monkeypatch: pytest.MonkeyPatch) -> None:
    gesehen: dict[str, Any] = {}

    def verbinden(url: str, **kw: Any) -> Verbindung:
        gesehen.update(url=url, **kw)
        return Verbindung(Server(), url)

    monkeypatch.setattr(einrichten, "verbinden", verbinden)
    conn = einrichten._admin(ADMIN)
    assert conn.autocommit is True  # CREATE DATABASE läuft nicht in einer Transaktion
    assert gesehen == {"url": ADMIN, "frist_s": 10, "anweisung_s": 120}


# ── anlegen ──────────────────────────────────────────────────────────────────


def test_anlegen_auf_einem_frischen_server() -> None:
    server = Server()
    einrichten.anlegen(ADMIN, "pw-einspeiser", "pw-leser", verbinde=server.verbinde)

    admin, datenbank = server.verbindungen
    assert admin.url == ADMIN and datenbank.url == "postgresql://verwalter:adminpasswort@db-host:5432/fyos_graph"
    assert admin.geschlossen and datenbank.geschlossen

    rollen, vorhanden, erzeugen, rechte = [" ".join(s.split()) for s in admin.anweisungen]
    assert "CREATE ROLE fyos_graph LOGIN" in rollen and "CREATE ROLE fyos_graph_lesen LOGIN" in rollen
    assert "ALTER ROLE fyos_graph WITH LOGIN PASSWORD 'pw-einspeiser' CONNECTION LIMIT 8" in rollen
    assert "ALTER ROLE fyos_graph_lesen WITH LOGIN PASSWORD 'pw-leser' CONNECTION LIMIT 12" in rollen
    assert vorhanden == "SELECT 1 FROM pg_database WHERE datname = 'fyos_graph'"
    assert erzeugen == "CREATE DATABASE fyos_graph OWNER fyos_graph"
    assert "REVOKE ALL ON DATABASE fyos_graph FROM PUBLIC" in rechte and "GRANT CONNECT ON DATABASE fyos_graph TO fyos_graph_lesen" in rechte

    oeffentlich, schema = [" ".join(s.split()) for s in datenbank.anweisungen]
    assert "REVOKE ALL ON SCHEMA public FROM PUBLIC" in oeffentlich and "GRANT USAGE ON SCHEMA public TO fyos_graph_lesen" in oeffentlich
    assert "GRANT ALL ON SCHEMA public TO fyos_graph" in oeffentlich
    assert "ALTER DEFAULT PRIVILEGES FOR ROLE fyos_graph IN SCHEMA public GRANT SELECT ON TABLES TO fyos_graph_lesen" in oeffentlich
    # Das Schema legt der Eigentümer an (Rollenwechsel), danach darf der Leser lesen
    assert schema.startswith("SET ROLE fyos_graph;") and "CREATE TABLE IF NOT EXISTS knoten" in schema and "CREATE TABLE IF NOT EXISTS nutzung" in schema
    assert schema.endswith("GRANT SELECT ON ALL TABLES IN SCHEMA public TO fyos_graph_lesen;")


def test_anlegen_ist_idempotent_die_datenbank_wird_nicht_zweimal_erzeugt() -> None:
    server = Server(rollen=["fyos_graph", "fyos_graph_lesen"], datenbank=True, schema=1)
    einrichten.anlegen(ADMIN, "neu-1", "neu-2", verbinde=server.verbinde)
    assert not any("CREATE DATABASE" in s for s in server.alle_anweisungen())
    assert any("PASSWORD 'neu-1'" in s and "PASSWORD 'neu-2'" in s for s in server.alle_anweisungen())  # die Passwörter werden neu gesetzt


def test_die_passwoerter_stehen_nur_in_der_anweisung_die_sie_setzt() -> None:
    server = Server()
    einrichten.anlegen(ADMIN, "geheim-e", "geheim-l", verbinde=server.verbinde)
    mit_passwort = [s for s in server.alle_anweisungen() if "geheim-e" in s or "geheim-l" in s]
    assert len(mit_passwort) == 1 and "ALTER ROLE" in mit_passwort[0]


def test_anlegen_schliesst_die_verbindungen_auch_im_fehlerfall() -> None:
    server = Server(fehler_bei="CREATE DATABASE")
    with pytest.raises(RuntimeError, match="Postgres lehnt ab"):
        einrichten.anlegen(ADMIN, "a", "b", verbinde=server.verbinde)
    assert len(server.verbindungen) == 1 and server.verbindungen[0].geschlossen  # zur Datenbank kommt es nicht mehr

    server = Server(fehler_bei="SET ROLE")
    with pytest.raises(RuntimeError):
        einrichten.anlegen(ADMIN, "a", "b", verbinde=server.verbinde)
    assert len(server.verbindungen) == 2 and all(v.geschlossen for v in server.verbindungen)


def test_ein_ungueltiges_passwort_faellt_auf_bevor_der_server_angefasst_wird() -> None:
    server = Server()
    with pytest.raises(ValueError, match="NUL"):
        einrichten.anlegen(ADMIN, "a\x00b", "ok", verbinde=server.verbinde)
    with pytest.raises(ValueError, match="NUL"):
        einrichten.anlegen(ADMIN, "ok", "a\x00b", verbinde=server.verbinde)
    assert server.verbindungen == []


# ── prüfen ───────────────────────────────────────────────────────────────────


def test_pruefen_meldet_rollen_datenbank_und_schemaversion() -> None:
    server = Server(rollen=["fyos_graph", "fyos_graph_lesen"], datenbank=True, schema=1)
    assert einrichten.pruefen(ADMIN, verbinde=server.verbinde) == {
        "rollen": ["fyos_graph", "fyos_graph_lesen"], "datenbank": ["fyos_graph"], "schema_version": 1,
    }
    assert all(v.geschlossen for v in server.verbindungen)
    assert server.verbindungen[1].url.endswith("/fyos_graph")


def test_pruefen_ohne_datenbank_oeffnet_keine_zweite_verbindung() -> None:
    server = Server(rollen=["fyos_graph"])
    assert einrichten.pruefen(ADMIN, verbinde=server.verbinde) == {"rollen": ["fyos_graph"], "datenbank": [], "schema_version": None}
    assert len(server.verbindungen) == 1


def test_pruefen_ohne_schema_gibt_none() -> None:
    server = Server(datenbank=True, schema=None)
    assert einrichten.pruefen(ADMIN, verbinde=server.verbinde)["schema_version"] is None
    assert len(server.verbindungen) == 2 and all(v.geschlossen for v in server.verbindungen)


# ── main ─────────────────────────────────────────────────────────────────────


@pytest.fixture()
def umgebung(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    for name in ("GRAPH_ADMIN_URL", "GRAPH_EINSPEISER_PASSWORT", "GRAPH_LESER_PASSWORT"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def passwoerter_setzen(umgebung: pytest.MonkeyPatch, einspeiser: str = "pw-e", leser: str = "pw-l") -> None:
    umgebung.setenv("GRAPH_ADMIN_URL", ADMIN)
    umgebung.setenv("GRAPH_EINSPEISER_PASSWORT", einspeiser)
    umgebung.setenv("GRAPH_LESER_PASSWORT", leser)


def test_main_ohne_adresse_ist_2(umgebung: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert einrichten.main([]) == 2
    assert "GRAPH_ADMIN_URL" in capsys.readouterr().err


def test_main_ohne_passwoerter_ist_2_nur_pruefen_braucht_sie_nicht(umgebung: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    umgebung.setenv("GRAPH_ADMIN_URL", ADMIN)
    aufrufe: list[str] = []
    umgebung.setattr(einrichten, "anlegen", lambda *a, **k: aufrufe.append("anlegen"))
    umgebung.setattr(einrichten, "pruefen", lambda url, **k: {"rollen": [], "datenbank": [], "schema_version": None})
    assert einrichten.main([]) == 2
    assert "GRAPH_EINSPEISER_PASSWORT" in capsys.readouterr().err and aufrufe == []
    assert einrichten.main(["--pruefen"]) == 0
    assert json.loads(capsys.readouterr().out) == {"rollen": [], "datenbank": [], "schema_version": None}


def test_main_legt_an_und_gibt_den_stand_aus_ohne_passwoerter(umgebung: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    passwoerter_setzen(umgebung)
    gesehen: list[tuple] = []
    umgebung.setattr(einrichten, "anlegen", lambda url, e, l, **k: gesehen.append((url, e, l)))
    umgebung.setattr(einrichten, "pruefen", lambda url, **k: {"rollen": ["fyos_graph", "fyos_graph_lesen"], "datenbank": ["fyos_graph"], "schema_version": 1})
    assert einrichten.main([]) == 0
    assert gesehen == [(ADMIN, "pw-e", "pw-l")]
    ausgabe = capsys.readouterr()
    assert json.loads(ausgabe.out) == {"angelegt": True, "rollen": ["fyos_graph", "fyos_graph_lesen"], "datenbank": ["fyos_graph"], "schema_version": 1}
    for geheim in ("pw-e", "pw-l", "adminpasswort"):
        assert geheim not in ausgabe.out + ausgabe.err


def test_main_ungueltige_eingabe_ist_2_ohne_passwort_in_der_meldung(umgebung: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    passwoerter_setzen(umgebung)

    def anlegen(url: str, e: str, l: str, **kw: Any) -> None:
        raise ValueError(f"das Passwort {e} ist unbrauchbar")  # Umgebungsvariablen können kein NUL tragen: die Eingabe prüft `anlegen`

    umgebung.setattr(einrichten, "anlegen", anlegen)
    assert einrichten.main([]) == 2
    fehler = capsys.readouterr().err
    assert "ungültige Eingabe: das Passwort *** ist unbrauchbar" in fehler and "pw-e" not in fehler


def test_main_fehlschlag_ist_1_und_nennt_kein_passwort(umgebung: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    passwoerter_setzen(umgebung)

    def anlegen(url: str, e: str, l: str, **kw: Any) -> None:
        # Fehlertexte von Postgres können die Anweisung samt Passwort wiedergeben
        raise RuntimeError(f"syntax error at ... PASSWORD '{e}' ... {l} ... adminpasswort")

    umgebung.setattr(einrichten, "anlegen", anlegen)
    assert einrichten.main([]) == 1
    fehler = capsys.readouterr().err
    assert "Einrichtung fehlgeschlagen: RuntimeError" in fehler and "***" in fehler
    for geheim in ("pw-e", "pw-l", "adminpasswort"):
        assert geheim not in fehler


def test_main_bis_zum_ende_nur_die_verbindung_ist_die_attrappe(umgebung: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    passwoerter_setzen(umgebung)
    server = Server()
    umgebung.setattr(einrichten, "_admin", server.verbinde)
    assert einrichten.main([]) == 0
    assert json.loads(capsys.readouterr().out)["angelegt"] is True
    assert any(s.lstrip().startswith("CREATE DATABASE") for s in server.alle_anweisungen())
    assert all(v.geschlossen for v in server.verbindungen)
    # `--pruefen` ändert nichts: nur Abfragen, keine Passwörter nötig
    umgebung.delenv("GRAPH_EINSPEISER_PASSWORT")
    umgebung.delenv("GRAPH_LESER_PASSWORT")
    neu = Server(rollen=["fyos_graph"])
    umgebung.setattr(einrichten, "_admin", neu.verbinde)
    assert einrichten.main(["--pruefen"]) == 0
    assert all(s.lstrip().startswith("SELECT") for s in neu.alle_anweisungen())


def test_hilfe_nennt_die_umgebungsvariablen(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as ende:
        einrichten.main(["--help"])
    assert ende.value.code == 0
    text = capsys.readouterr().out
    for stichwort in ("GRAPH_ADMIN_URL", "GRAPH_EINSPEISER_PASSWORT", "GRAPH_LESER_PASSWORT", "--pruefen"):
        assert stichwort in text, stichwort
