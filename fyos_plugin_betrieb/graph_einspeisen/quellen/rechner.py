"""Quelle rechner — Dienste, Speicher, Artefakte und Profile auf Rechnern ohne Kubernetes, kuratiert aus einer Datei.

Nicht alles läuft im Cluster: ein systemd-Dienst auf einer Werkbank, ein Modellserver, ein NAS. Ein Cluster-Objekt
verrät davon nichts, darum liest diese Quelle NICHT per ssh, sondern übernimmt das Belegte aus einer YAML-Datei. Pfad:
Umgebung `GRAPH_RECHNER_DATEN`. Aufbau und Beispiel: `daten/rechner.beispiel.yaml` im Paket (Abschnitte `dienste`,
`speicher`, `artefakte`, `profile`, `querverbindungen`, dazu `adressen`, `extern` und `standard_rechner`). Ohne gesetzten
Pfad ist die Quelle aus; ist ein Pfad gesetzt und die Datei fehlt oder ist kaputt, scheitert sie laut.

Knoten    rechner:<dienst> (Typ `rechner` = ein Dienst auf einem Rechner), speicher:*, artefakt:*, profil:*
Kanten    Dienst    laeuft_auf   <Rechner>   (Graph-ID des Rechners, meist `knoten:<Name>`; pin hart)
          Dienst    braucht      <ID>        (aus `braucht`)
          Rechner   braucht      Dienst      (mit `rechner_haengt_dran`: der Dienst trägt den Rechner, etwa der Agent
                                              eines Cluster-Knotens)
          speicher  liegt_auf    <Rechner>
          artefakt  liegt_in     speicher
          profil    laedt        artefakt;  profil beschreibt <ID> (mit `beschreibt`)
          Querverbindungen aus der Datei (z. B. ein Plugin nutzt einen Dienst auf dem Rechner)
Die Datei ist Wissen, das sich nicht lesen lässt: jeder Eintrag nennt einen `beleg`, woher es stammt.
Die Abschnitte `adressen` und `extern` liest zusätzlich die Quelle caddy, `artefakte` und `profile` die Quelle experimente.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from ...betriebsgraph import ALLE_ARTEN, Kante, Knoten
from ..kontext import Ausgeschaltet

NAME = "rechner"
UMGEBUNG = "GRAPH_RECHNER_DATEN"


def laden(ctx: Any) -> dict[str, Any] | None:
    """Die Rechner-Datei als Zuordnung; `None`, wenn kein Pfad eingerichtet ist."""
    pfad = ctx.einstellung(UMGEBUNG)
    if not pfad:
        return None
    try:
        text = Path(pfad).read_text(encoding="utf-8")
    except OSError as e:
        raise RuntimeError(f"Rechner-Datei {pfad} nicht lesbar ({type(e).__name__})") from e
    try:
        d = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise RuntimeError(f"Rechner-Datei {pfad} ist kein gültiges YAML") from e
    if d is None:
        return {}
    if not isinstance(d, dict):
        raise RuntimeError(f"Rechner-Datei {pfad}: die oberste Ebene muss eine Zuordnung sein")
    return d


def abschnitt(d: dict[str, Any], name: str, *pflicht: str) -> list[dict[str, Any]]:
    """Die Einträge eines Abschnitts; fehlt ein Pflichtfeld, sagt der Fehler, welcher Eintrag es ist."""
    eintraege = d.get(name) or []
    if not isinstance(eintraege, list):
        raise RuntimeError(f"Rechner-Datei: Abschnitt {name!r} muss eine Liste sein")
    for n, e in enumerate(eintraege, 1):
        if not isinstance(e, dict):
            raise RuntimeError(f"Rechner-Datei: {name} Nr. {n} muss eine Zuordnung sein")
        fehlt = [f for f in pflicht if not e.get(f)]
        if fehlt:
            raise RuntimeError(f"Rechner-Datei: {name} Nr. {n}: {', '.join(fehlt)} fehlt")
    return eintraege


def sammeln(ctx: Any) -> tuple[list[Knoten], list[Kante]]:
    d = laden(ctx)
    if d is None:
        raise Ausgeschaltet(f"keine Rechner-Datei eingerichtet ({UMGEBUNG} ist nicht gesetzt)")
    standard = str(d.get("standard_rechner") or "")
    knoten: dict[str, Knoten] = {}
    kanten: dict[tuple[str, str, str], Kante] = {}

    def k(i: str, typ: str, name: Any, **attr: Any) -> None:
        knoten[i] = Knoten(i, typ, str(name), {a: v for a, v in attr.items() if v})

    def e(von: str, nach: str, art: str, beleg: str = "", **attr: Any) -> None:
        kanten[(von, nach, art)] = Kante(von, nach, art, beleg=str(beleg or ""), attribute=attr)

    for s in abschnitt(d, "dienste", "id", "name"):
        i = str(s["id"])
        k(i, "rechner", s["name"], port=s.get("port"), was=s.get("was"))
        rechner = str(s.get("rechner") or standard)
        if rechner:
            e(i, rechner, "laeuft_auf", s.get("beleg", ""), pin="hart")
        for b in s.get("braucht") or []:
            e(i, str(b), "braucht", s.get("beleg", ""))
        if s.get("rechner_haengt_dran") and rechner:
            e(rechner, i, "braucht", s.get("beleg", ""))
    for s in abschnitt(d, "speicher", "id", "name"):
        i = str(s["id"])
        k(i, "speicher", s["name"], was=s.get("was"))
        rechner = str(s.get("rechner") or standard)
        if rechner:
            e(i, rechner, "liegt_auf", s.get("beleg", ""), pin="hart")
    for a in abschnitt(d, "artefakte", "id", "name"):
        i = str(a["id"])
        k(i, "artefakt", a["name"], datei=a.get("datei"), was=a.get("was"))
        if a.get("speicher"):
            e(i, str(a["speicher"]), "liegt_in", a.get("beleg", ""))
    for p in abschnitt(d, "profile", "id"):
        i = str(p["id"])
        k(i, "profil", i.split(":", 1)[-1], was=p.get("was"))
        for a in p.get("laedt") or []:
            e(i, str(a), "laedt", p.get("beleg", ""))
        if p.get("beschreibt"):
            e(i, str(p["beschreibt"]), "beschreibt", p.get("beleg", ""))
    for q in abschnitt(d, "querverbindungen", "von", "nach"):
        art = str(q.get("art") or "braucht")
        if art not in ALLE_ARTEN:
            raise RuntimeError(f"Rechner-Datei: Querverbindung {q['von']} -> {q['nach']}: Kantenart {art!r} unbekannt")
        if ctx.gibt(str(q["von"])) or str(q["von"]) in knoten:
            e(str(q["von"]), str(q["nach"]), art, q.get("beleg", ""))
    return list(knoten.values()), list(kanten.values())
