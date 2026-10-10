"""Quelle experimente — Experimente, ihre Artefakte und Profile, nur lesend.

Liest die Steckbriefe der Experiment-App (Dateien `<id>.json` in einem Verzeichnis, Umgebung `EXPERIMENTE_DIR`) und
verknüpft sie mit den Artefakten und Profilen aus der Rechner-Datei (Quelle rechner, wenn eingerichtet). Ist
`EXPERIMENTE_DIR` nicht gesetzt, ist die Quelle aus.

Knoten   experiment:<id>   (titel, status)
Kanten   experiment misst artefakt:*/profil:*   Name als ganzes Wort in Titel, Frage, Armen, Ablauf (0.6; Profil-ID 0.8)
         experiment betrifft dienst/rechner/…   wie bei Vorgängen (0.5), nur Hinweis
Das Verzeichnis ist gesetzt, aber nicht lesbar oder leer (Pod ohne Einhängung, falscher Pfad): die Quelle meldet das laut,
statt „keine Experimente" vorzutäuschen.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ...betriebsgraph import Kante, Knoten
from ..kontext import Ausgeschaltet
from . import rechner
from ._text import Index

NAME = "experimente"
SCHUTZ = False  # neue Experimente kommen und gehen in Schüben; die Ablage ist die Wahrheit
UMGEBUNG = "EXPERIMENTE_DIR"
FELDER = ("titel", "frage", "ablauf", "ort")


def _text(d: dict[str, Any]) -> str:
    teile = [str(d.get(f) or "") for f in FELDER]
    for a in d.get("arme") or []:
        if isinstance(a, dict):
            teile.append(f"{a.get('name') or ''} {a.get('was') or ''}")
    return "\n".join(teile)


def artefakt_muster(artefakte: list[dict[str, Any]]) -> dict[str, re.Pattern[str]]:
    """Artefakt-ID → Regex auf den Kurznamen (`artefakt:q4` → `\\bq4\\b`).

    Nur Artefakte mit Kurznamen von mindestens zwei Zeichen; `experimente: false` nimmt eines heraus."""
    out: dict[str, re.Pattern[str]] = {}
    for a in artefakte:
        i = str(a["id"])
        kurz = i.split(":", 1)[-1]
        if len(kurz) < 2 or a.get("experimente") is False:
            continue
        out[i] = re.compile(rf"(?<![\w-]){re.escape(kurz)}(?![\w-])", re.I)
    return out


def sammeln(ctx: Any) -> tuple[list[Knoten], list[Kante]]:
    pfad = ctx.einstellung(UMGEBUNG)
    if not pfad:
        raise Ausgeschaltet(f"kein Verzeichnis für Experimente eingerichtet ({UMGEBUNG} ist nicht gesetzt)")
    verz = Path(pfad)
    if not verz.is_dir():
        raise RuntimeError(f"Experiment-Ablage {verz} nicht lesbar")
    dateien = sorted(verz.glob("*.json"))
    if not dateien:
        raise RuntimeError(f"keine Experimente in {verz}")
    kur = rechner.laden(ctx) or {}
    qm = artefakt_muster(rechner.abschnitt(kur, "artefakte", "id"))
    profile = [str(p["id"]) for p in rechner.abschnitt(kur, "profile", "id")]
    index = Index(ctx.bekannt, sperre=ctx.text_sperre())
    knoten: list[Knoten] = []
    kanten: dict[tuple[str, str, str], Kante] = {}
    for f in dateien:
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(d, dict):
            continue
        eid = str(d.get("id") or f.stem)
        xid = f"experiment:{eid}"
        knoten.append(Knoten(xid, "experiment", eid, {"titel": str(d.get("titel") or "")[:140], "status": d.get("status")}))
        text = _text(d)
        for aid, rx in qm.items():
            if rx.search(text):
                kanten[(xid, aid, "misst")] = Kante(xid, aid, "misst", vertrauen=0.6, beleg="Name im Steckbrief")
        for pid in profile:
            if pid.split(":", 1)[-1] in text:
                kanten[(xid, pid, "misst")] = Kante(xid, pid, "misst", vertrauen=0.8, beleg="Profil-ID im Steckbrief")
        for z in sorted(index.finde(text))[:10]:
            kanten[(xid, z, "betrifft")] = Kante(xid, z, "betrifft", vertrauen=0.5, beleg="Name im Steckbrief")
    return knoten, list(kanten.values())
