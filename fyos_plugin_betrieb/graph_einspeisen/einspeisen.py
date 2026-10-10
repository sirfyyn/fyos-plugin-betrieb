"""Betriebsgraph einspeisen — liest die Quellen und gleicht sie mit der Graph-Datenbank ab.

    fyos-graph-einspeisen                       alle eingerichteten Quellen einspeisen (Umgebung GRAPH_DB_URL)
    fyos-graph-einspeisen --trocken             Quellen lesen und zählen, nichts schreiben, keine Datenbank
    fyos-graph-einspeisen --quellen k8s,flux    nur diese Quellen (sonst GRAPH_QUELLEN, sonst alle)
    fyos-graph-einspeisen --leeren bestand      alle gültigen Zeilen einer (abgeschalteten) Quelle schließen
    fyos-graph-einspeisen einrichten            Datenbank und Rollen anlegen (siehe db_einrichten)
    python -m fyos_plugin_betrieb.graph_einspeisen ...   dasselbe ohne installiertes Konsolenskript

Läuft meist als CronJob (`deploy/graph/einspeisen-cronjob.yaml`), von Hand mit `--trocken`.

Eine Quelle ist ein Modul unter `fyos_plugin_betrieb/graph_einspeisen/quellen/<name>.py` mit

    NAME = "k8s"
    def sammeln(ctx: Kontext) -> tuple[list[Knoten], list[Kante]]       # liest, schreibt nichts
    def nachlauf(ctx: Kontext, graph: Graph) -> dict | None             # optional, nach dem Abgleich

`sammeln` ist von der Datenbank unabhängig (Rohdaten holen, in Knoten und Kanten übersetzen); der Abgleich
(`Graph.einspeisen`) schließt nur Zeilen DIESER Quelle. Fällt eine Quelle aus (Ausnahme), bleibt ihr Stand
unverändert und die anderen laufen weiter; der Fehler steht in der Tabelle `laeufe` und im Protokoll. Eine Quelle, die
nicht eingerichtet ist (`Ausgeschaltet`), wird übersprungen: kein Fehler, kein Eintrag in `laeufe`, alter Stand bleibt.

Rückgabewert: 0 wenn mindestens eine Quelle ok ist, 1 wenn alle scheitern oder übersprungen werden oder die Datenbank
fehlt, 2 bei falschem Aufruf. Gibt nie Zugangsdaten aus.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
import traceback
from typing import Any

from ..betriebsgraph import Graph, verbinden
from .kontext import Ausgeschaltet, Kontext

__all__ = ["QUELLEN", "Ausgeschaltet", "Kontext", "lauf", "leeren", "main", "quelle_laden"]

#: Reihenfolge: der Nachlauf von `itsm` (Kommentare an Vorgänge) braucht den Stand der anderen.
QUELLEN = ("k8s", "flux", "caddy", "hub", "bestand", "rechner", "experimente", "itsm")


def quelle_laden(name: str) -> Any:
    return importlib.import_module(f"{__package__}.quellen.{name}")


def lauf(namen: list[str], *, trocken: bool, db_url: str | None, ctx: Kontext | None = None) -> dict[str, Any]:
    ctx = ctx or Kontext(trocken=trocken)
    ergebnis: dict[str, Any] = {}
    graph: Graph | None = None
    if not trocken:
        conn = verbinden(db_url, anweisung_s=120)
        graph = Graph(conn)
        graph.schema_anlegen()
        ctx.bekannt |= {r[0] for r in graph._ausfuehren("SELECT id FROM knoten WHERE gueltig_bis IS NULL")}
    for name in namen:
        t0 = time.monotonic()
        lauf_id = None
        try:
            modul = quelle_laden(name)
            knoten, kanten = modul.sammeln(ctx)
            knoten, kanten = list(knoten), list(kanten)
            ctx.bekannt |= {k.id for k in knoten}
            if graph is None:
                art: dict[str, int] = {}
                for k in knoten:
                    art[k.typ] = art.get(k.typ, 0) + 1
                arten: dict[str, int] = {}
                for e in kanten:
                    arten[e.art] = arten.get(e.art, 0) + 1
                ergebnis[name] = {"ok": True, "knoten": len(knoten), "kanten": len(kanten), "typen": art, "arten": arten}
            else:
                lauf_id = graph.lauf_beginnen(name)
                stand = graph.einspeisen(name, knoten, kanten, schutz=getattr(modul, "SCHUTZ", True))
                nach = getattr(modul, "nachlauf", None)
                if nach is not None:
                    stand["nachlauf"] = nach(ctx, graph)
                graph.lauf_beenden(lauf_id, ok=True, stand=stand)
                ergebnis[name] = {"ok": True, **stand}
        except Ausgeschaltet as aus:
            ergebnis[name] = {"ok": None, "aus": aus.grund[:300]}
        except Exception as e:  # noqa: BLE001 — eine Quelle darf die anderen nicht mitnehmen
            fehler = f"{type(e).__name__}: {e}"
            traceback.print_exc(file=sys.stderr)
            ergebnis[name] = {"ok": False, "fehler": fehler[:500]}
            if graph is not None:
                try:
                    graph.conn.rollback()
                    if lauf_id is None:
                        lauf_id = graph.lauf_beginnen(name)
                    graph.lauf_beenden(lauf_id, ok=False, fehler=fehler)
                except Exception:  # noqa: BLE001
                    pass
        ergebnis[name]["sekunden"] = round(time.monotonic() - t0, 1)
        print(json.dumps({name: ergebnis[name]}, ensure_ascii=False, default=str), flush=True)
    if graph is not None:
        graph.conn.close()
    return ergebnis


def leeren(namen: list[str], *, db_url: str | None = None) -> dict[str, Any]:
    """Schließt alle gültigen Zeilen der genannten Quellen (Historie bleibt). Für Quellen, die abgeschaltet oder
    umbenannt wurden: ihr letzter Stand bliebe sonst für immer gültig."""
    ergebnis: dict[str, Any] = {}
    conn = verbinden(db_url, anweisung_s=120)
    graph = Graph(conn)
    try:
        for name in namen:
            lauf_id = graph.lauf_beginnen(name)
            try:
                stand = graph.einspeisen(name, [], [], schutz=False)
                graph.lauf_beenden(lauf_id, ok=True, stand=stand)
                ergebnis[name] = {"ok": True, **stand}
            except Exception as e:  # noqa: BLE001
                fehler = f"{type(e).__name__}: {e}"
                conn.rollback()
                graph.lauf_beenden(lauf_id, ok=False, fehler=fehler)
                ergebnis[name] = {"ok": False, "fehler": fehler[:500]}
            print(json.dumps({name: ergebnis[name]}, ensure_ascii=False, default=str), flush=True)
    finally:
        conn.close()
    return ergebnis


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "einrichten":
        from . import db_einrichten

        return db_einrichten.main(argv[1:])
    ap = argparse.ArgumentParser(prog="fyos-graph-einspeisen", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quellen", default=None, help=f"kommagetrennt (Vorgabe: GRAPH_QUELLEN, sonst alle: {','.join(QUELLEN)})")
    ap.add_argument("--trocken", action="store_true", help="nur lesen und zählen, keine Datenbank")
    ap.add_argument("--leeren", default=None, metavar="QUELLEN", help="alle gültigen Zeilen dieser Quellen schließen (kommagetrennt)")
    a = ap.parse_args(argv)
    if a.leeren is not None:
        namen = [n.strip() for n in a.leeren.split(",") if n.strip()]
        if not namen:
            print("--leeren braucht mindestens einen Quellnamen", file=sys.stderr)
            return 2
        try:
            erg = leeren(namen)
        except Exception as e:  # noqa: BLE001 — Datenbank nicht erreichbar
            print(f"Datenbank nicht erreichbar: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
            return 1
        return 0 if all(v.get("ok") for v in erg.values()) else 1
    roh = a.quellen if a.quellen is not None else (os.environ.get("GRAPH_QUELLEN") or ",".join(QUELLEN))
    namen = [n.strip() for n in roh.split(",") if n.strip()]
    unbekannt = [n for n in namen if n not in QUELLEN]
    if unbekannt or not namen:
        print(f"unbekannte Quelle(n): {unbekannt}; bekannt: {list(QUELLEN)}", file=sys.stderr)
        return 2
    try:
        erg = lauf(namen, trocken=a.trocken, db_url=None, ctx=Kontext(trocken=a.trocken))
    except Exception as e:  # noqa: BLE001 — Datenbank nicht erreichbar
        print(f"Datenbank nicht erreichbar: {type(e).__name__}: {str(e)[:200]}", file=sys.stderr)
        return 1
    if any(v.get("ok") for v in erg.values()):
        return 0
    if all(v.get("ok") is None for v in erg.values()):
        print("keine der gewählten Quellen ist eingerichtet; Gründe stehen oben", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
