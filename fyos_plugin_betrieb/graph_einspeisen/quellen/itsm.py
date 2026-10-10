"""Quelle itsm — offene Vorgänge und die Dienste, die sie betreffen.

Die Vorgänge kommen aus der Vorgangs-Quelle des Hubs (`mcp_hub.vorgaenge`): ein Vorgangs-Dienst (`FYOS_VORGAENGE_URL`,
Kopfzeile `FYOS_VORGAENGE_KOPF`) oder, im Hub-Prozess, das Workboard des Agenten. Der Einspeiser als eigener Prozess
(CronJob) erreicht nur den Vorgangs-Dienst: das Workboard lebt im Prozess des Hubs. Ist keine Quelle eingerichtet, ist
die Quelle aus (keine Vorgangs-Knoten, kein Fehler).

Knoten   vorgang:<ID>   (art INC/PRB/CHG/…, status, schwere, titel, zustaendig) – nur offene Vorgänge; wird einer
         geschlossen, endet sein Knoten (Historie bleibt). `art` ist das Feld `type`, sonst der Anfang der ID.
Kanten   vorgang betrifft dienst/cronjob/rechner/knoten/plugin   Textfund in Titel (0.8) oder Beschreibung (0.5)
         Kante nur Info (gibt keinen Ausfall weiter). Gleiche Regel wie überall: Hinweis, kein Beleg.

Nachlauf (Triage), nur auf Wunsch (`GRAPH_ITSM_KOMMENTARE=1`): zu neuen offenen INCs (höchstens 3 Tage alt) mit Textfund
hängt der Lauf EINEN Kommentar an („[graph] …": betroffene Dienste, was bei deren Ausfall mitfällt, wer sie nutzt). Die
Marke `[graph]` im Verlauf macht es einmalig. Höchstens 5 je Lauf; Akteur `GRAPH_ITSM_AKTEUR` (Vorgabe `api`). Das ist
das Einzige, was der Einspeiser außerhalb der Graph-Datenbank schreibt, darum ist es standardmäßig aus.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

from ...betriebsgraph import Graph, Kante, Knoten
from ..kontext import Ausgeschaltet
from ._text import Index

NAME = "itsm"
#: Offene Vorgänge wechseln in Schüben (Kehrläufe schließen Dutzende): der Schwundschutz würde die Quelle sperren.
SCHUTZ = False
#: Obergrenze je Abfrage laut Vorgangs-Vertrag.
GRENZE = 500
MARKE = "[graph]"
KOMMENTAR_MAX = 5
KOMMENTAR_ALTER_TAGE = 3
FUND_TITEL = 0.8
FUND_TEXT = 0.5
#: Typen von Knoten, die ein Vorgang betreffen darf (und die der Kommentar nennt).
BETROFFEN_PRAEFIXE = ("dienst", "cronjob", "rechner", "knoten", "plugin")
DIENST_TYPEN = ("dienst", "cronjob", "rechner", "plugin")
AN = frozenset({"1", "true", "ja", "an", "yes", "on"})


def _quelle(ctx: Any) -> Any:
    q = ctx.vorgangs_quelle()
    if not getattr(q, "eingerichtet", False):
        raise Ausgeschaltet("keine Quelle für Vorgänge eingerichtet (FYOS_VORGAENGE_URL oder das Workboard im Hub)")
    return q


def offene_vorgaenge(quelle: Any) -> list[dict[str, Any]]:
    return [t for t in quelle.offen(GRENZE) if isinstance(t, dict)]


def _art(t: dict[str, Any]) -> str:
    return str(t.get("type") or str(t.get("id") or "").split("-", 1)[0]).upper()


def _beschreibung(quelle: Any, t: dict[str, Any]) -> str:
    if "description" in t:  # die Liste trägt sie schon (Workboard, manche Dienste)
        return str(t.get("description") or "")
    try:
        return str((quelle.holen(str(t["id"])) or {}).get("description") or "")
    except Exception:  # noqa: BLE001 — ohne Beschreibung bleibt der Titel
        return ""


def sammeln(ctx: Any) -> tuple[list[Knoten], list[Kante]]:
    quelle = _quelle(ctx)
    tickets = offene_vorgaenge(quelle)
    if not tickets:
        gekannt = [i for i in ctx.bekannt if i.startswith("vorgang:")]
        if gekannt and not quelle.liste(None, 1):
            # Die Quelle antwortet, kennt aber überhaupt keinen Vorgang mehr: eher ein Lesefehler als „alles erledigt"
            raise RuntimeError(f"Die Vorgangs-Quelle lieferte keinen einzigen Vorgang, der Graph kennt {len(gekannt)} offene – nichts übernommen")
        return [], []
    index = Index(ctx.bekannt, sperre=ctx.text_sperre())
    plugin_rx = _plugin_muster(ctx.bekannt)
    knoten: list[Knoten] = []
    kanten: dict[tuple[str, str, str], Kante] = {}
    for t in tickets:
        tid = t.get("id")
        if not tid:
            continue
        vid = f"vorgang:{tid}"
        titel = str(t.get("title") or "")
        beschreibung = _beschreibung(quelle, t)
        knoten.append(
            Knoten(
                vid,
                "vorgang",
                str(tid),
                {
                    "art": _art(t),
                    "status": t.get("status"),
                    "schwere": t.get("severity") or t.get("priority"),
                    "titel": titel[:140],
                    "zustaendig": t.get("assignee") or None,
                },
            )
        )
        im_titel = index.finde(titel) | _plugins(plugin_rx, titel)
        im_text = (index.finde(beschreibung) | _plugins(plugin_rx, beschreibung)) - im_titel
        for z in sorted(im_titel):
            kanten[(vid, z, "betrifft")] = Kante(vid, z, "betrifft", vertrauen=FUND_TITEL, beleg="Name im Titel")
        for z in sorted(im_text)[:12]:
            kanten[(vid, z, "betrifft")] = Kante(vid, z, "betrifft", vertrauen=FUND_TEXT, beleg="Name in der Beschreibung")
    return knoten, list(kanten.values())


def _plugin_muster(bekannt: set[str]) -> re.Pattern[str] | None:
    namen = sorted((i.split(":", 1)[1] for i in bekannt if i.startswith("plugin:")), key=len, reverse=True)
    namen = [n for n in namen if len(n) >= 3]
    if not namen:
        return None
    return re.compile(r"(?<![\w.-])(" + "|".join(re.escape(n) for n in namen) + r")\.[a-z_]{3,}\b", re.I)


def _plugins(rx: re.Pattern[str] | None, text: str) -> set[str]:
    if not rx or not text:
        return set()
    return {f"plugin:{m.group(1).lower()}" for m in rx.finditer(text)}


# ── Nachlauf: Triage-Kommentar ───────────────────────────────────────────────


def kommentar_text(graph: Graph, vid: str, ziele: list[tuple[str, float]]) -> str | None:
    """Der Kommentar zu einem Vorgang oder None, wenn der Graph nichts Brauchbares weiß."""
    info = graph.knoten_info([z for z, _ in ziele])
    zeilen = [f"{MARKE} Betroffen laut Text (Hinweis, nicht gemessen): " + ", ".join(
        f"{info[z]['name'] if z in info else z.split(':', 1)[-1]} ({info[z]['typ'] if z in info else '?'})" for z, _ in ziele[:6]
    )]
    ids = [z for z, _ in ziele if z in info]
    if not ids:
        return None
    wirkung = graph.auswirkung(ids)
    aus = [z["name"] for z in wirkung["ausgefallen"] if z["typ"] in DIENST_TYPEN]
    mit = [z["name"] for z in wirkung["beeintraechtigt"] if z["typ"] in DIENST_TYPEN]
    if aus:
        zeilen.append(f"Fällt mit aus ({len(aus)}): " + ", ".join(aus[:8]) + (" …" if len(aus) > 8 else ""))
    if mit:
        zeilen.append(f"Beeinträchtigt, weil sie es nutzen ({len(mit)}): " + ", ".join(mit[:8]) + (" …" if len(mit) > 8 else ""))
    if not aus and not mit:
        zeilen.append("Daran hängt laut Graph kein weiterer Dienst.")
    zeilen.append("Mehr: graph.auswirkung / graph.abhaengig im Hub.")
    return "\n".join(zeilen)


def _zeit(wert: Any) -> dt.datetime | None:
    """ISO-Text oder Epoche (Sekunden oder Millisekunden) → Zeitpunkt mit Zeitzone; unlesbar → None."""
    if isinstance(wert, (int, float)) and not isinstance(wert, bool):
        sekunden = float(wert) / 1000 if float(wert) > 1e11 else float(wert)
        try:
            return dt.datetime.fromtimestamp(sekunden, dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    try:
        t = dt.datetime.fromisoformat(str(wert).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def nachlauf(ctx: Any, graph: Graph) -> dict[str, Any]:
    if ctx.einstellung("GRAPH_ITSM_KOMMENTARE", "0").lower() not in AN:
        return {"kommentare": "aus"}
    quelle = _quelle(ctx)
    akteur = ctx.einstellung("GRAPH_ITSM_AKTEUR", "api")
    grenze = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=KOMMENTAR_ALTER_TAGE)
    gepostet: list[str] = []
    uebersprungen = 0
    for t in offene_vorgaenge(quelle):
        if len(gepostet) >= KOMMENTAR_MAX:
            break
        if _art(t) != "INC" or not t.get("id"):
            continue
        angelegt = _zeit(t.get("created_at"))
        if angelegt is None or angelegt < grenze:
            continue
        vid = f"vorgang:{t['id']}"
        z = graph._ausfuehren(
            "SELECT nach, vertrauen FROM kanten WHERE gueltig_bis IS NULL AND von = %s AND art = 'betrifft' ORDER BY vertrauen DESC, nach",
            (vid,),
        )
        ziele = [(r[0], float(r[1])) for r in z if r[0].split(":", 1)[0] in BETROFFEN_PRAEFIXE]
        if not ziele:
            continue
        try:
            verlauf = quelle.verlauf(str(t["id"]), 200)
        except Exception:  # noqa: BLE001
            continue
        if any(MARKE in str(h.get("detail") or "") for h in verlauf or []):
            uebersprungen += 1
            continue
        text = kommentar_text(graph, vid, ziele)
        if not text:
            continue
        quelle.kommentieren(str(t["id"]), text, akteur)
        gepostet.append(str(t["id"]))
    return {"kommentare": gepostet, "schon_kommentiert": uebersprungen}
