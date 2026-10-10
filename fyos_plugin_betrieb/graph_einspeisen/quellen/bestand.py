"""Quelle bestand — Werkzeuge, Skripte, Geräte, Seiten, Modelle aus dem Bestandsbuch.

Das Bestandsbuch (ein Hub-Plugin, das alles mit Zweck, Zustand und `abhaengig_von`-Listen sammelt) legt seine
Momentaufnahme in Redis ab (Schlüssel `fyos:bestand:stand`, abweichend `GRAPH_BESTAND_SCHLUESSEL`). Diese Quelle liest sie
und übernimmt, was der Cluster nicht kennt. Eingerichtet ist sie mit `GRAPH_BESTAND_REDIS_URL` (die Adresse des Redis, das
die Momentaufnahme hält); ohne die Variable ist sie aus. Wer sie einrichtet, bekommt einen lauten Fehler, wenn Redis
ausfällt oder die Momentaufnahme leer ist – der alte Stand bleibt dann stehen.

Knoten   werkzeug, skript, geraet, seite, modell  (id und name wie im Bestandsbuch; attribute.zweck)
         Dienste, CronJobs, Knoten und Plugins gehören den Quellen k8s und hub und werden hier nicht neu angelegt.
Kanten   aus `abhaengig_von`:
           werkzeug → plugin     gehoert_zu
           alles andere → Ziel   nutzt
         Ziele, die der Graph nicht kennt, werden übersprungen (ctx.gibt).
"""

from __future__ import annotations

import json
from typing import Any

from ...betriebsgraph import Kante, Knoten
from ..kontext import Ausgeschaltet

NAME = "bestand"
UMGEBUNG = "GRAPH_BESTAND_REDIS_URL"
SCHLUESSEL = "fyos:bestand:stand"
STAND_FORM = 2
#: Arten, die das Bestandsbuch selbst führt und andere Quellen besitzen.
FREMD = frozenset({"dienst", "cronjob", "knoten", "plugin"})


def stand_laden(ctx: Any) -> dict[str, Any]:
    if getattr(ctx, "bestand_stand", None) is not None:
        return ctx.bestand_stand
    url = ctx.einstellung(UMGEBUNG)
    if not url:
        raise Ausgeschaltet(f"kein Bestandsbuch eingerichtet ({UMGEBUNG} ist nicht gesetzt)")
    import redis  # Abhängigkeit des Pakets

    schluessel = ctx.einstellung("GRAPH_BESTAND_SCHLUESSEL", SCHLUESSEL)
    roh = redis.Redis.from_url(url, socket_timeout=5, decode_responses=True).get(schluessel)
    if not roh:
        raise RuntimeError(f"{schluessel} ist leer – das Bestandsbuch hat noch keinen Stand")
    return json.loads(roh)


def sammeln(ctx: Any) -> tuple[list[Knoten], list[Kante]]:
    stand = stand_laden(ctx)
    eintraege = stand.get("eintraege") or []
    if not eintraege:
        raise RuntimeError("Bestandsbuch ohne Einträge – nichts übernommen")
    knoten: list[Knoten] = []
    for e in eintraege:
        i, art = e.get("id"), e.get("art")
        if i and art and art not in FREMD:
            attr = {"zweck": (e.get("zweck") or "")[:160]} if e.get("zweck") else {}
            knoten.append(Knoten(i, art, str(e.get("name") or i.split(":", 1)[-1]), attr))
    eigene = {k.id for k in knoten}
    kanten: dict[tuple[str, str, str], Kante] = {}
    for e in eintraege:
        i, art = e.get("id"), e.get("art")
        if not i or not art:
            continue
        for ziel in e.get("abhaengig_von") or []:
            if not isinstance(ziel, str) or ziel == i or not (ziel in eigene or ctx.gibt(ziel)):
                continue
            kante_art = "gehoert_zu" if art == "werkzeug" and ziel.startswith("plugin:") else "nutzt"
            kanten[(i, ziel, kante_art)] = Kante(i, ziel, kante_art, beleg="Bestandsbuch abhaengig_von")
    return knoten, list(kanten.values())
