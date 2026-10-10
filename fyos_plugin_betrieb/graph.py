"""graph — der Betriebsgraph als Hub-Werkzeug.

Was hängt woran, was fällt aus, wenn etwas ausfällt, wie hängen zwei Dinge zusammen, was hat sich geändert?
Der Graph liegt in Postgres (Schema und Logik in `fyos_plugin_betrieb.betriebsgraph`) und wird vom Einspeiser
(`fyos-graph-einspeisen`, Paket `fyos_plugin_betrieb.graph_einspeisen`) aus Cluster, Flux, Caddy, Hub, Bestandsbuch,
eigenen Rechnern, Experimenten und Vorgängen gespeist, meist als CronJob. Dieses Plugin liest nur; das einzige, was es
schreibt, ist ein Zählvermerk je Aufruf (Tabelle `nutzung`), damit sichtbar wird, ob die Werkzeuge gebraucht werden.

Einrichtung: Postgres, Adresse in der Umgebung des Hubs (`GRAPH_DB_URL`, geheim; anderer Name über `config.url_env`),
Postgres-Treiber über `pip install "fyos-plugin-betrieb[graph]"`. Ohne Adresse meldet sich das Plugin als „aus“ (weder
gesund noch krank: es ist nicht eingerichtet); wer keinen Betriebsgraphen betreibt, kann es in der Hub-Konfiguration ganz
abschalten (`enabled: false`). Ist eine Adresse gesetzt und die Datenbank nicht erreichbar, ist es ungesund.

Werkzeuge:
    graph.abhaengig    Was hängt an X (oder, mit `richtung: braucht`, was braucht X)?
    graph.auswirkung   Was fällt aus / wird umgeplant / ist eingeschränkt, wenn X ausfällt?
    graph.pfad         Wie hängen A und B zusammen (kürzester Weg)?
    graph.verlauf      Was hat sich an X und seinen Kanten geändert (neu, weg, geändert)?
    graph.zustand      Stand der Einspeisung (Zahlen je Quelle, letzter Lauf, Nutzung)

`ziel` darf eine ID (`dienst:fyos/qdrant`), ein Name (`qdrant`), ein Pod-Name (`qdrant-64f566945f-bq4gw`) oder ein
Teilstring sein. Antworten sind knapp (Zahlen und Namen je Typ); `voll: true` liefert jede Zeile mit Weg und
Kantenart (höchstens 200).
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Callable

from mcp_hub.plugin import HubPlugin, ToolDescriptor

from . import betriebsgraph as bg

try:  # Aufrufer für den Zählvermerk; ohne Registry (Tests) bleibt es „unbekannt"
    from mcp_hub.registry import AUFRUFER
except Exception:  # noqa: BLE001  # pragma: no cover
    AUFRUFER = None  # type: ignore[assignment]

log = logging.getLogger("fyos_plugin_betrieb.graph")

VOLL_MAX = 200
MEHRDEUTIG_AB = 4  # Teilstring-Treffer: bis hierhin gelten alle als gemeint, darüber fragen wir nach
START_MAX = 12
NAMEN_JE_TYP = 8
#: Typen, die „laufen": bei gleichnamigen Treffern gelten diese als gemeint
LAUFEND = frozenset(bg.DIENST_TYPEN) | {"knoten"}


def _zeitwert(t: Any) -> str:
    return bg.zeit_text(t)


class GraphPlugin(HubPlugin):
    name = "graph"
    description = (
        "Betriebsgraph (Postgres): was hängt an X, was fällt aus, wenn X ausfällt, wie hängen A und B zusammen, "
        "was hat sich geändert. Quellen: Cluster, Flux, Caddy, Hub, Bestandsbuch, eigene Rechner, Experimente, Vorgänge."
    )
    version = "0.1.0"

    def __init__(self, config: dict[str, Any] | None = None, *, graph_factory: Callable[[], bg.Graph] | None = None):
        super().__init__()
        cfg = config or {}
        self.url_env = str(cfg.get("url_env") or "GRAPH_DB_URL")
        self.anweisung_s = int(cfg.get("anweisung_s", 20))
        self._factory = graph_factory
        self._graph: bg.Graph | None = None
        self._lock = threading.Lock()

    # ── Lebenslauf ──────────────────────────────────────────────────────────
    def register(self) -> None:
        return None

    #: Die Gesundheitsprobe fragt die Datenbank wirklich ab. Hubs, die das Attribut kennen, zeigen sie als „tief“;
    #: fyos-os liest es (noch) nicht, dort schadet die Klassenvariable nicht.
    HEALTH_TIEF = True

    def health(self) -> dict[str, Any]:
        if not self._factory and not os.getenv(self.url_env):
            # „aus“ statt „ungesund“: ein Betriebsgraph, den niemand eingerichtet hat, ist kein Ausfall
            return {"ok": False, "status": "aus", "reason": f"{self.url_env} ist nicht gesetzt: kein Betriebsgraph eingerichtet"}
        try:
            z = self._lesen(lambda g: g.zustand())
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "status": "unhealthy", "reason": f"{type(exc).__name__}: {exc}"[:300]}
        n = sum(int(q.get("knoten") or 0) for q in z["quellen"].values())
        return {"ok": n > 0, "status": "ok" if n > 0 else "unhealthy", "knoten": n, "quellen": len(z["quellen"])}

    def shutdown(self) -> None:
        with self._lock:
            self._schliessen()

    # ── Verbindung ──────────────────────────────────────────────────────────
    def _schliessen(self) -> None:
        if self._graph is not None:
            try:
                self._graph.conn.close()
            except Exception:  # noqa: BLE001
                pass
        self._graph = None

    def _neuer_graph(self) -> bg.Graph:
        if self._factory:
            return self._factory()
        conn = bg.verbinden(os.getenv(self.url_env), anweisung_s=self.anweisung_s)
        return bg.Graph(conn)

    def _lesen(self, fn: Callable[[bg.Graph], Any]) -> Any:
        """`fn(graph)` unter Sperre; bei einer toten Verbindung einmal neu verbinden. Danach Rollback, damit keine
        Transaktion offen bleibt (der Lesezugriff schreibt nichts)."""
        with self._lock:
            letzter: Exception | None = None
            for _ in range(2):
                try:
                    if self._graph is None:
                        self._graph = self._neuer_graph()
                    try:
                        return fn(self._graph)
                    finally:
                        try:
                            self._graph.conn.rollback()
                        except Exception:  # noqa: BLE001
                            pass
                except (ValueError, bg.GraphFehler):
                    raise
                except Exception as exc:  # noqa: BLE001 — Verbindung tot oder SQL-Fehler: einmal frisch versuchen
                    letzter = exc
                    self._schliessen()
            assert letzter is not None
            raise letzter

    def _vermerken(self, werkzeug: str, ziel: str, treffer: int | None, start: float, ok: bool) -> None:
        try:
            wer = (AUFRUFER.get() if AUFRUFER is not None else "") or "unbekannt"
            dauer = int((time.monotonic() - start) * 1000)
            with self._lock:
                if self._graph is not None:
                    self._graph.nutzung_vermerken(werkzeug, wer, ziel, treffer, dauer, ok)
        except Exception:  # noqa: BLE001 — Zählen darf nie eine Abfrage kosten
            pass

    # ── Werkzeuge ───────────────────────────────────────────────────────────
    def describe_tools(self) -> list[ToolDescriptor]:
        ziel = {"type": "string", "description": "ID, Name, Pod-Name oder Teilstring, z. B. 'qdrant' oder 'dienst:fyos/qdrant'"}
        voll = {"type": "boolean", "description": "Jede Zeile mit Weg und Kantenart statt der Kurzfassung (höchstens 200)"}
        return [
            ToolDescriptor(
                name="abhaengig",
                title="Was hängt an X",
                description=(
                    "Was hängt (rekursiv) an `ziel` — oder mit richtung='braucht': was braucht `ziel`? Dienste, "
                    "Cronjobs, Plugins, Routen, Speicher, Images, Dienste auf eigenen Rechnern. Kurz: Zahl und Namen je Typ. "
                    "`tiefe` (1-12, Standard 3), `arten` schränkt die Kantenarten ein (laeuft_auf, mountet, bindet, "
                    "nutzt, nutzt_image, braucht, routet_zu, verwaltet_von, …)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "ziel": ziel,
                        "richtung": {"type": "string", "enum": ["an", "braucht"], "description": "an = was hängt an ziel (Standard)"},
                        "tiefe": {"type": "integer", "minimum": 1, "maximum": bg.TIEFE_MAX},
                        "arten": {"type": "array", "items": {"type": "string"}},
                        "typ": {"type": "string", "description": "Nur Ergebnisse dieses Typs (dienst, cronjob, plugin, route, …)"},
                        "voll": voll,
                    },
                    "required": ["ziel"],
                    "additionalProperties": False,
                },
                tags=["graph", "read"],
            ),
            ToolDescriptor(
                name="auswirkung",
                title="Was fällt aus",
                description=(
                    "Was fällt aus, wenn `ziel` ausfällt? Unterscheidet ausgefallen (harte Abhängigkeit), umgeplant "
                    "(Pod kann auf einen anderen Knoten), eingeschränkt (ein Teil der Knoten weg) und beeinträchtigt "
                    "(nutzt es nur weich: läuft weiter, kann Funktionen verlieren). Vor Neustart, Update oder Abschalten "
                    "fragen. Mehrere Ziele mit Komma."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"ziel": ziel, "voll": voll},
                    "required": ["ziel"],
                    "additionalProperties": False,
                },
                tags=["graph", "read"],
            ),
            ToolDescriptor(
                name="pfad",
                title="Wie hängen A und B zusammen",
                description=(
                    "Kürzester Weg zwischen zwei Dingen im Graphen, Kanten in beide Richtungen. Zuerst über tragende "
                    "Kanten, sonst über alle (dann `nur_info: true`). Gibt die Kette mit Kantenart und Richtung."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "von": ziel,
                        "nach": ziel,
                        "tiefe": {"type": "integer", "minimum": 1, "maximum": 10},
                        "voll": voll,
                    },
                    "required": ["von", "nach"],
                    "additionalProperties": False,
                },
                tags=["graph", "read"],
            ),
            ToolDescriptor(
                name="verlauf",
                title="Was hat sich geändert",
                description=(
                    "Änderungen an `ziel` und seinen Kanten seit `seit` (7d, 36h, 2026-10-01; Standard 7d): neu, weg, "
                    "geändert (mit Attribut-Unterschied, z. B. Image-Tag). Der Graph führt eine Historie, nichts wird "
                    "überschrieben."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"ziel": ziel, "seit": {"type": "string"}, "voll": voll},
                    "required": ["ziel"],
                    "additionalProperties": False,
                },
                tags=["graph", "read"],
            ),
            ToolDescriptor(
                name="zustand",
                title="Stand des Graphen",
                description="Knoten und Kanten je Quelle, letzter Lauf und Fehler je Quelle, Nutzung der Werkzeuge in 30 Tagen.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                tags=["graph", "read"],
            ),
        ]

    def call(self, method: str, args: dict[str, Any]) -> Any:
        args = args or {}
        start = time.monotonic()
        ziel_text = str(args.get("ziel") or args.get("von") or "")
        try:
            if method == "abhaengig":
                r = self.abhaengig(args)
            elif method == "auswirkung":
                r = self.auswirkung(args)
            elif method == "pfad":
                r = self.pfad(args)
            elif method == "verlauf":
                r = self.verlauf(args)
            elif method == "zustand":
                r = self.zustand()
            else:
                raise ValueError(f"graph hat kein Werkzeug {method!r}")
        except Exception:
            self._vermerken(f"graph.{method}", ziel_text, None, start, False)
            raise
        self._vermerken(f"graph.{method}", ziel_text, r.get("anzahl") if isinstance(r, dict) else None, start, True)
        return r

    # ── Auflösen ────────────────────────────────────────────────────────────
    def _aufloesen(self, g: bg.Graph, text: Any, *, typ: str | None = None) -> tuple[list[str], dict[str, Any] | None]:
        """(ids, None) oder ([], Antwort) wenn nichts oder zu vieles passt."""
        teile = [t.strip() for t in str(text or "").split(",") if t.strip()]
        if not teile:
            raise ValueError("ziel fehlt")
        ids: list[str] = []
        for t in teile:
            a = g.aufloesen(t, typ=typ)
            if not a["ids"]:
                return [], {"satz": f"Kein Knoten zu „{t}“ im Graphen.", "gefunden": False, "ziel": t, "anzahl": 0}
            if a["wie"] == "teil" and len(a["ids"]) >= MEHRDEUTIG_AB:
                return [], {
                    "satz": f"„{t}“ ist mehrdeutig ({len(a['ids'])}+ Treffer) — bitte genauer, z. B. eine dieser IDs.",
                    "gefunden": False,
                    "mehrdeutig": True,
                    "ziel": t,
                    "kandidaten": [x["id"] for x in a["treffer"]][:20],
                    "anzahl": 0,
                }
            gefunden = a["ids"]
            if a["wie"] in ("name", "pod"):
                # Gleichnamig sind oft Dienst, Service und Image: gemeint ist, was läuft — nicht das Image oder der Service
                laufend = [x["id"] for x in a["treffer"] if x["typ"] in LAUFEND]
                if laufend:
                    gefunden = laufend
            ids.extend(gefunden)
        ids = list(dict.fromkeys(ids))[:START_MAX]
        return ids, None

    @staticmethod
    def _zeile(d: dict[str, Any]) -> dict[str, Any]:
        out = {"id": d["id"], "typ": d["typ"], "name": d["name"]}
        for k in ("tiefe", "via", "art", "von_n", "weg_n"):
            if d.get(k) is not None:
                out[k] = d[k]
        return out

    # ── abhaengig ───────────────────────────────────────────────────────────
    def abhaengig(self, args: dict[str, Any]) -> dict[str, Any]:
        richtung = str(args.get("richtung") or "an")
        tiefe = int(args.get("tiefe") or bg.STANDARD_TIEFE)
        arten = args.get("arten") or None
        if arten is not None:
            unbekannt = sorted(set(arten) - bg.ALLE_ARTEN)
            if unbekannt:
                raise ValueError(f"unbekannte Kantenart {unbekannt}; bekannt: {', '.join(sorted(bg.ALLE_ARTEN))}")
        nur_typ = str(args.get("typ") or "") or None
        voll = bool(args.get("voll"))

        def fn(g: bg.Graph) -> dict[str, Any]:
            ids, antwort = self._aufloesen(g, args.get("ziel"))
            if antwort:
                return antwort
            z = g.abhaengig(ids, tiefe=tiefe, richtung=richtung, arten=arten)
            if nur_typ:
                z = [d for d in z if d["typ"] == nur_typ]
            return self._abhaengig_antwort(ids, z, richtung, tiefe, voll, nur_typ)

        return self._lesen(fn)

    def _abhaengig_antwort(self, ids: list[str], z: list[dict[str, Any]], richtung: str, tiefe: int, voll: bool, nur_typ: str | None) -> dict[str, Any]:
        wer = ", ".join(ids[:3]) + (f" (+{len(ids) - 3})" if len(ids) > 3 else "")
        frage = "hängt an" if richtung == "an" else "braucht"
        verteilung = bg.gruppiert(z, je_typ=NAMEN_JE_TYP)
        teile = ", ".join(f"{v['anzahl']} {t}" for t, v in verteilung.items())
        if richtung == "an":
            satz = f"An {wer} hängen bis Tiefe {tiefe}: {len(z)} ({teile})." if z else f"An {wer} hängt (bis Tiefe {tiefe}) nichts."
        else:
            satz = f"{wer} braucht bis Tiefe {tiefe}: {len(z)} ({teile})." if z else f"{wer} braucht (bis Tiefe {tiefe}) nichts."
        out: dict[str, Any] = {"satz": satz, "ziel": ids, "richtung": richtung, "tiefe": tiefe, "anzahl": len(z)}
        if nur_typ:
            out["nur_typ"] = nur_typ
        if voll:
            out["eintraege"] = [self._zeile(d) for d in z[:VOLL_MAX]]
            if len(z) > VOLL_MAX:
                out["gekuerzt"] = f"{len(z) - VOLL_MAX} weitere nicht gezeigt — tiefe, typ oder arten eingrenzen"
        else:
            out["nach_typ"] = verteilung
            if z:
                out["hinweis"] = f"{frage}: voll=true zeigt jede Zeile mit Weg (via) und Kantenart."
        return out

    # ── auswirkung ──────────────────────────────────────────────────────────
    def auswirkung(self, args: dict[str, Any]) -> dict[str, Any]:
        voll = bool(args.get("voll"))

        def fn(g: bg.Graph) -> dict[str, Any]:
            ids, antwort = self._aufloesen(g, args.get("ziel"))
            if antwort:
                return antwort
            return self._auswirkung_antwort(ids, g.auswirkung(ids), voll)

        return self._lesen(fn)

    def _auswirkung_antwort(self, ids: list[str], a: dict[str, Any], voll: bool) -> dict[str, Any]:
        wer = ", ".join(ids[:3]) + (f" (+{len(ids) - 3})" if len(ids) > 3 else "")
        gruppen = {k: a[k] for k in ("ausgefallen", "umgeplant", "eingeschraenkt", "beeintraechtigt")}
        zahlen = {k: len(v) for k, v in gruppen.items()}
        if not any(zahlen.values()):
            satz = f"Fällt {wer} aus, bleibt alles andere im Graphen unberührt (keine Abhängigen)."
        else:
            teile = []
            if zahlen["ausgefallen"]:
                teile.append(f"{zahlen['ausgefallen']} fallen aus")
            if zahlen["umgeplant"]:
                teile.append(f"{zahlen['umgeplant']} werden umgeplant")
            if zahlen["eingeschraenkt"]:
                teile.append(f"{zahlen['eingeschraenkt']} laufen eingeschränkt")
            if zahlen["beeintraechtigt"]:
                teile.append(f"{zahlen['beeintraechtigt']} sind beeinträchtigt (weiche Abhängigkeit)")
            satz = f"Fällt {wer} aus: " + ", ".join(teile) + "."
        out: dict[str, Any] = {"satz": satz, "ziel": ids, "anzahl": zahlen["ausgefallen"], "zahlen": zahlen}
        if voll:
            for k, v in gruppen.items():
                out[k] = [self._zeile(d) for d in v[:VOLL_MAX]]
        else:
            for k, v in gruppen.items():
                if v:
                    out[k] = bg.gruppiert(v, je_typ=NAMEN_JE_TYP)
            out["hinweis"] = (
                "ausgefallen = harte Abhängigkeit; beeinträchtigt = nutzt nur weich. voll=true zeigt Weg und Kantenart."
            )
        return out

    # ── pfad ────────────────────────────────────────────────────────────────
    def pfad(self, args: dict[str, Any]) -> dict[str, Any]:
        tiefe = int(args.get("tiefe") or 6)
        voll = bool(args.get("voll"))

        def fn(g: bg.Graph) -> dict[str, Any]:
            von, antwort = self._aufloesen(g, args.get("von"))
            if antwort:
                return antwort
            nach, antwort = self._aufloesen(g, args.get("nach"))
            if antwort:
                return antwort
            p = g.pfad(von, nach, tiefe=tiefe)
            info = g.knoten_info([k["id"] for k in p["kette"]]) if p["gefunden"] else {}
            return self._pfad_antwort(von, nach, p, info, voll)

        return self._lesen(fn)

    def _pfad_antwort(self, von: list[str], nach: list[str], p: dict[str, Any], info: dict[str, dict[str, Any]], voll: bool) -> dict[str, Any]:
        if not p["gefunden"]:
            return {"satz": f"Kein Weg von {von[0]} nach {nach[0]} (bis zur gewählten Tiefe).", "gefunden": False, "von": von, "nach": nach, "anzahl": 0}
        kette = []
        for k in p["kette"]:
            name = (info.get(k["id"]) or {}).get("name") or bg.name_aus_id(k["id"])
            kette.append({"id": k["id"], "name": name, "art": k["art"], "richtung": k["richtung"]})
        pfeile = []
        for k in kette:
            if k["art"] is None:
                pfeile.append(k["name"])
            else:
                pfeil = f"—{k['art']}→" if k["richtung"] == "vor" else f"←{k['art']}—"
                pfeile.append(f"{pfeil} {k['name']}")
        text = " ".join(pfeile)
        zusatz = " (nur über Informationskanten, keine tragende Abhängigkeit)" if p["nur_info"] else ""
        out: dict[str, Any] = {
            "satz": f"{p['laenge']} Schritte: {text}{zusatz}.",
            "gefunden": True,
            "laenge": p["laenge"],
            "nur_info": p["nur_info"],
            "anzahl": p["laenge"],
        }
        if voll:
            out["kette"] = kette
        return out

    # ── verlauf ─────────────────────────────────────────────────────────────
    def verlauf(self, args: dict[str, Any]) -> dict[str, Any]:
        seit = args.get("seit") or "7d"
        voll = bool(args.get("voll"))

        def fn(g: bg.Graph) -> dict[str, Any]:
            ids, antwort = self._aufloesen(g, args.get("ziel"))
            if antwort:
                return antwort
            e = g.verlauf(ids, seit, grenze=VOLL_MAX if voll else 60)
            return self._verlauf_antwort(ids, str(seit), e, voll)

        return self._lesen(fn)

    def _verlauf_antwort(self, ids: list[str], seit: str, e: list[dict[str, Any]], voll: bool) -> dict[str, Any]:
        wer = ", ".join(ids[:3]) + (f" (+{len(ids) - 3})" if len(ids) > 3 else "")
        if not e:
            return {"satz": f"An {wer} hat sich seit {seit} nichts geändert.", "ziel": ids, "seit": seit, "anzahl": 0}
        zaehler: dict[str, int] = {}
        for x in e:
            zaehler[x["was"]] = zaehler.get(x["was"], 0) + 1
        teile = ", ".join(f"{n} {w}" for w, n in sorted(zaehler.items()))
        zeilen = [{"ts": _zeitwert(x["ts"]), "was": x["was"], "ding": x["ding"], "text": x["text"]} for x in e]
        out: dict[str, Any] = {"satz": f"An {wer} seit {seit}: {teile}.", "ziel": ids, "seit": seit, "anzahl": len(e)}
        if voll:
            out["ereignisse"] = zeilen
        else:
            out["ereignisse"] = zeilen[:8]
            if len(zeilen) > 8:
                out["hinweis"] = f"{len(zeilen) - 8} ältere nicht gezeigt — voll=true zeigt bis zu {VOLL_MAX}."
        return out

    # ── zustand ─────────────────────────────────────────────────────────────
    def zustand(self) -> dict[str, Any]:
        z = self._lesen(lambda g: g.zustand())
        quellen = {}
        for q, d in z["quellen"].items():
            quellen[q] = {
                "knoten": d.get("knoten", 0),
                "kanten": d.get("kanten", 0),
                "letzter_lauf_ok": d.get("letzter_ok"),
                "zuletzt_ok": _zeitwert(d["zuletzt_ok"]) if d.get("zuletzt_ok") else None,
                **({"fehler": str(d["fehler"])[:200]} if d.get("fehler") else {}),
            }
        kn = sum(d["knoten"] for d in quellen.values())
        ka = sum(d["kanten"] for d in quellen.values())
        kaputt = [q for q, d in quellen.items() if d["letzter_lauf_ok"] is False]
        satz = f"{kn} Knoten, {ka} Kanten aus {len(quellen)} Quellen" + (f"; zuletzt fehlgeschlagen: {', '.join(kaputt)}." if kaputt else ".")
        return {
            "satz": satz,
            "anzahl": kn,
            "quellen": quellen,
            "typen": z["typen"],
            "kanten_ohne_knoten": z["kanten_ohne_knoten"],
            "nutzung_30t": z["nutzung_30t"][:15],
        }
