"""Datenbank `fyos_graph` und ihre zwei Rollen in einer vorhandenen Postgres-Instanz anlegen (idempotent).

    Rolle fyos_graph          Eigentümer der Datenbank und der Tabellen (der Einspeiser: GRAPH_DB_URL des CronJobs)
    Rolle fyos_graph_lesen    nur SELECT (+ INSERT auf `nutzung`, per Schema-SQL) für das Hub-Plugin (dessen GRAPH_DB_URL)
    Datenbank fyos_graph      Eigentümer fyos_graph, Verbindungen für PUBLIC gesperrt

und legt das Schema an. Die Passwörter werden bei jedem Lauf neu gesetzt (Rotation = neue Passwörter, dieser Aufruf).
Aufruf mit einem Konto, das Rollen und Datenbanken anlegen darf (Superuser); Adresse und Passwörter kommen aus der
Umgebung, nie aus Argumenten, und werden nie ausgegeben:

    GRAPH_ADMIN_URL=postgresql://admin:…@host:5432/postgres \\
    GRAPH_EINSPEISER_PASSWORT=… GRAPH_LESER_PASSWORT=… \\
    fyos-graph-einspeisen einrichten            anlegen / angleichen
    fyos-graph-einspeisen einrichten --pruefen  nur prüfen, ob Rollen, Datenbank und Schema da sind

Danach: `GRAPH_DB_URL` des Einspeisers = `postgresql://fyos_graph:<Passwort>@host:5432/fyos_graph`, die des Hubs =
`postgresql://fyos_graph_lesen:<Passwort>@host:5432/fyos_graph`.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from typing import Any, Callable

from ..betriebsgraph import SCHEMA_SQL, verbinden

DB = "fyos_graph"
EIGNER = "fyos_graph"
LESER = "fyos_graph_lesen"
#: Name des Schemas und der Rollen sind fest: `SCHEMA_SQL` nennt `fyos_graph_lesen` beim Namen.


def quoten(wert: str) -> str:
    """Zeichenkette als SQL-Literal. Mit Backslash die E-Form, damit es von `standard_conforming_strings` unabhängig ist."""
    if "\x00" in wert:
        raise ValueError("ein Passwort darf kein NUL-Zeichen enthalten")
    if "\\" in wert:
        return "E'" + wert.replace("\\", "\\\\").replace("'", "''") + "'"
    return "'" + wert.replace("'", "''") + "'"


def datenbank_url(url: str, name: str) -> str:
    """Dieselbe Adresse, aber mit einer anderen Datenbank."""
    teile = urllib.parse.urlsplit(url)
    return urllib.parse.urlunsplit(teile._replace(path="/" + name))


def _admin(url: str) -> Any:
    conn = verbinden(url, frist_s=10, anweisung_s=120)
    conn.autocommit = True  # CREATE DATABASE läuft nicht in einer Transaktion
    return conn


def _sql(conn: Any, sql: str) -> list[tuple]:
    cur = conn.cursor()
    try:
        cur.execute(sql)
        return list(cur.fetchall()) if cur.description else []
    finally:
        cur.close()


def pruefen(admin_url: str, *, verbinde: Callable[[str], Any] | None = None) -> dict[str, Any]:
    """Welche Rollen, welche Datenbank, welche Schema-Version sind da?"""
    verbinde = verbinde or _admin
    conn = verbinde(admin_url)
    try:
        rollen = [r[0] for r in _sql(conn, f"SELECT rolname FROM pg_roles WHERE rolname IN ('{EIGNER}', '{LESER}') ORDER BY 1")]
        db = [r[0] for r in _sql(conn, f"SELECT datname FROM pg_database WHERE datname = '{DB}'")]
    finally:
        conn.close()
    version = None
    if db:
        conn = verbinde(datenbank_url(admin_url, DB))
        try:
            zeilen = _sql(conn, "SELECT max(version) FROM schema_stand")
            version = zeilen[0][0] if zeilen else None
        except Exception:  # noqa: BLE001 — noch kein Schema
            version = None
        finally:
            conn.close()
    return {"rollen": rollen, "datenbank": db, "schema_version": version}


def anlegen(admin_url: str, passwort_einspeiser: str, passwort_leser: str, *, verbinde: Callable[[str], Any] | None = None) -> None:
    """Rollen, Datenbank, Rechte und Schema. Idempotent; die Passwörter werden neu gesetzt."""
    # Ungültige Passwörter fallen auf, bevor der Server angefasst wird
    pw_einspeiser, pw_leser = quoten(passwort_einspeiser), quoten(passwort_leser)
    verbinde = verbinde or _admin
    conn = verbinde(admin_url)
    try:
        _sql(
            conn,
            f"""
            DO $$ BEGIN
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{EIGNER}') THEN CREATE ROLE {EIGNER} LOGIN; END IF;
              IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{LESER}') THEN CREATE ROLE {LESER} LOGIN; END IF;
            END $$;
            ALTER ROLE {EIGNER} WITH LOGIN PASSWORD {pw_einspeiser} CONNECTION LIMIT 8;
            ALTER ROLE {LESER} WITH LOGIN PASSWORD {pw_leser} CONNECTION LIMIT 12;
            """,
        )
        if not _sql(conn, f"SELECT 1 FROM pg_database WHERE datname = '{DB}'"):
            _sql(conn, f"CREATE DATABASE {DB} OWNER {EIGNER}")
        _sql(
            conn,
            f"""
            REVOKE ALL ON DATABASE {DB} FROM PUBLIC;
            GRANT CONNECT ON DATABASE {DB} TO {LESER};
            """,
        )
    finally:
        conn.close()
    conn = verbinde(datenbank_url(admin_url, DB))
    try:
        _sql(
            conn,
            f"""
            REVOKE ALL ON SCHEMA public FROM PUBLIC;
            GRANT USAGE ON SCHEMA public TO {LESER};
            GRANT ALL ON SCHEMA public TO {EIGNER};
            ALTER DEFAULT PRIVILEGES FOR ROLE {EIGNER} IN SCHEMA public GRANT SELECT ON TABLES TO {LESER};
            """,
        )
        # Schema über den Eigentümer: Rolle wechseln, damit die Tabellen ihm gehören
        _sql(conn, f"SET ROLE {EIGNER};\n{SCHEMA_SQL}\nGRANT SELECT ON ALL TABLES IN SCHEMA public TO {LESER};")
    finally:
        conn.close()


def ohne_geheimnisse(text: str, *geheimnisse: str) -> str:
    """Fehlertexte können Teile des SQL enthalten: nie ein Passwort zeigen."""
    for g in geheimnisse:
        if g:
            text = text.replace(g, "***")
    return text


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="fyos-graph-einspeisen einrichten", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pruefen", action="store_true", help="nur prüfen, nichts ändern")
    a = ap.parse_args(argv)
    url = os.environ.get("GRAPH_ADMIN_URL", "").strip()
    if not url:
        print("GRAPH_ADMIN_URL ist nicht gesetzt (Adresse mit einem Konto, das Rollen und Datenbanken anlegen darf)", file=sys.stderr)
        return 2
    pw_e = os.environ.get("GRAPH_EINSPEISER_PASSWORT", "")
    pw_l = os.environ.get("GRAPH_LESER_PASSWORT", "")
    geheim = (pw_e, pw_l, urllib.parse.urlsplit(url).password or "")
    try:
        if a.pruefen:
            print(json.dumps(pruefen(url), ensure_ascii=False))
            return 0
        if not pw_e or not pw_l:
            print("GRAPH_EINSPEISER_PASSWORT und GRAPH_LESER_PASSWORT müssen gesetzt sein", file=sys.stderr)
            return 2
        anlegen(url, pw_e, pw_l)
        print(json.dumps({"angelegt": True, **pruefen(url)}, ensure_ascii=False))
    except ValueError as e:
        print(f"ungültige Eingabe: {ohne_geheimnisse(str(e), *geheim)[:300]}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 — Datenbank nicht erreichbar, Rechte fehlen, …
        print(f"Einrichtung fehlgeschlagen: {type(e).__name__}: {ohne_geheimnisse(str(e), *geheim)[:600]}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
