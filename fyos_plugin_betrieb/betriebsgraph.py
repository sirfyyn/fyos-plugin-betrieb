"""betriebsgraph — der Betriebsgraph in Postgres (Datenbank `fyos_graph`).

Ein Graph, den Agenten in echten Abläufen abfragen: Was hängt an X, was fällt aus, wenn X ausfällt, wie hängen A und B
zusammen, was hat sich an X geändert. Keine Graph-Datenbank: Knoten und Kanten liegen in zwei Tabellen, Abfragen sind
`WITH RECURSIVE`.

Dieses Modul hat zwei Hälften und braucht nur die Standardbibliothek plus psycopg (3) oder psycopg2
(`pip install "fyos-plugin-betrieb[graph]"`):

    Schreiben  `Graph.einspeisen(quelle, knoten, kanten)` — gleicht EINE Quelle mit dem Stand ab. Idempotent,
               Historie statt Überschreiben: was sich ändert oder wegfällt, wird mit `gueltig_bis` geschlossen,
               die neue Fassung bekommt eine neue Zeile. Läuft im Einspeiser (`fyos-graph-einspeisen`, Paket
               `fyos_plugin_betrieb.graph_einspeisen`), meist als CronJob.
    Lesen      `Graph.aufloesen / abhaengig / auswirkung / pfad / verlauf / zustand` — nur SELECT; die
               Hub-Werkzeuge `graph.*` (Plugin `fyos_plugin_betrieb.graph`) benutzen es.

Kantenrichtung: **`von` braucht `nach`** (Abhängigkeitsrichtung). „Was hängt an qdrant" folgt den Kanten also
rückwärts (alle `von`, die auf qdrant zeigen), „was braucht X" vorwärts.

Kantenarten: TRAGEND ist, was Ausfall weitergibt; INFO ist Zusatzwissen (Ursprung, Zuständigkeit, Vorgänge).
`laeuft_auf` ist eine ODER-Gruppe: ein Dienst auf zwei Knoten fällt erst aus, wenn beide ausfallen.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
import re
import time
from typing import Any, Iterable, Sequence

# ── Vokabular ────────────────────────────────────────────────────────────────

#: Kantenarten, die einen Ausfall weitergeben (von braucht nach).
TRAGEND = frozenset(
    {
        "laeuft_auf",  # dienst/cronjob → knoten (ODER-Gruppe; attribute.pin = hart|frei)
        "waehlt",  # service → dienst (Selektor)
        "routet_zu",  # route → service/dienst
        "mountet",  # dienst → pvc
        "bindet",  # pvc → pv
        "liegt_auf",  # pv → knoten/speicher; speicher → pool
        "nutzt",  # dienst/plugin → dienst/service (festgestellt: Umgebung, Konfiguration, Bestandsbuch); weich, außer attribute.hart
        "gehoert_zu",  # werkzeug → plugin
        "braucht",  # allgemein (kuratiert, Rechner, Experimente)
        "laedt",  # profil → artefakt
        "liegt_in",  # artefakt → datei-ort / speicher
    }
)
#: Zusatzwissen, gibt keinen Ausfall weiter.
INFO = frozenset(
    {
        "nutzt_image",  # dienst → image (attribute.tag)
        "verwaltet_von",  # dienst/service/… → kustomization
        "betrifft",  # vorgang → dienst (vertrauen < 1: Textfund)
        "beschreibt",  # bestand/doku → ding
        "misst",  # experiment → profil/artefakt
        "erwaehnt",  # schwach
    }
)
ALLE_ARTEN = TRAGEND | INFO
#: Arten, die einen Ausfall sicher weitergeben. `nutzt` fehlt hier mit Absicht: eine Adresse in der Konfiguration sagt,
#: dass A mit B spricht, nicht ob A ohne B stehen bleibt. Wer `nutzt` verwendet, ist „beeinträchtigt", nicht „ausgefallen" –
#: es sei denn, die Kante trägt attribute.hart (kuratiert).
HART = TRAGEND - {"nutzt"}
#: Arten, die eine ODER-Gruppe bilden: fällt aus, wenn ALLE Ziele der Gruppe ausfallen.
ODER_ARTEN = frozenset({"laeuft_auf"})

#: Typen, die in Zusammenfassungen als „Dienste“ zählen (`rechner`: Dienste auf Rechnern ohne Kubernetes).
DIENST_TYPEN = ("dienst", "cronjob", "plugin", "rechner")

TIEFE_MAX = 12
STANDARD_TIEFE = 3
#: Ausbleiben von mehr als der Hälfte der Knoten einer Quelle ist ein Lesefehler, kein Abbau.
SCHWUND_ANTEIL = 0.5
SCHWUND_AB = 20

SCHEMA_VERSION = 1

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS knoten (
    zeile       bigserial PRIMARY KEY,
    id          text        NOT NULL,
    typ         text        NOT NULL,
    name        text        NOT NULL,
    attribute   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    quelle      text        NOT NULL,
    gueltig_ab  timestamptz NOT NULL DEFAULT now(),
    gueltig_bis timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS knoten_aktuell ON knoten (id) WHERE gueltig_bis IS NULL;
CREATE INDEX IF NOT EXISTS knoten_typ ON knoten (typ) WHERE gueltig_bis IS NULL;
CREATE INDEX IF NOT EXISTS knoten_name ON knoten (lower(name) text_pattern_ops) WHERE gueltig_bis IS NULL;
CREATE INDEX IF NOT EXISTS knoten_quelle ON knoten (quelle) WHERE gueltig_bis IS NULL;
CREATE INDEX IF NOT EXISTS knoten_verlauf ON knoten (id, gueltig_ab DESC);
CREATE INDEX IF NOT EXISTS knoten_zeit ON knoten (gueltig_ab);

CREATE TABLE IF NOT EXISTS kanten (
    zeile       bigserial PRIMARY KEY,
    von         text        NOT NULL,
    nach        text        NOT NULL,
    art         text        NOT NULL,
    gewicht     real        NOT NULL DEFAULT 1,
    vertrauen   real        NOT NULL DEFAULT 1,
    attribute   jsonb       NOT NULL DEFAULT '{}'::jsonb,
    quelle      text        NOT NULL,
    beleg       text        NOT NULL DEFAULT '',
    gueltig_ab  timestamptz NOT NULL DEFAULT now(),
    gueltig_bis timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS kanten_aktuell ON kanten (von, nach, art, quelle) WHERE gueltig_bis IS NULL;
CREATE INDEX IF NOT EXISTS kanten_von ON kanten (von, art) WHERE gueltig_bis IS NULL;
CREATE INDEX IF NOT EXISTS kanten_nach ON kanten (nach, art) WHERE gueltig_bis IS NULL;
CREATE INDEX IF NOT EXISTS kanten_quelle ON kanten (quelle) WHERE gueltig_bis IS NULL;
CREATE INDEX IF NOT EXISTS kanten_zeit ON kanten (gueltig_ab);
CREATE INDEX IF NOT EXISTS kanten_zeit_bis ON kanten (gueltig_bis) WHERE gueltig_bis IS NOT NULL;

CREATE TABLE IF NOT EXISTS laeufe (
    id          bigserial PRIMARY KEY,
    quelle      text        NOT NULL,
    beginn      timestamptz NOT NULL DEFAULT now(),
    ende        timestamptz,
    ok          boolean,
    knoten      integer,
    kanten      integer,
    knoten_neu  integer,
    knoten_zu   integer,
    kanten_neu  integer,
    kanten_zu   integer,
    fehler      text
);
CREATE INDEX IF NOT EXISTS laeufe_quelle ON laeufe (quelle, beginn DESC);

CREATE TABLE IF NOT EXISTS nutzung (
    id          bigserial PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now(),
    werkzeug    text        NOT NULL,
    aufrufer    text        NOT NULL DEFAULT 'unbekannt',
    ziel        text,
    treffer     integer,
    dauer_ms    integer,
    ok          boolean     NOT NULL DEFAULT true
);
CREATE INDEX IF NOT EXISTS nutzung_zeit ON nutzung (ts DESC);

CREATE TABLE IF NOT EXISTS markierungen (
    schluessel  text PRIMARY KEY,
    wert        jsonb       NOT NULL DEFAULT '{}'::jsonb,
    ts          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS schema_stand (
    version     integer PRIMARY KEY,
    ts          timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_stand (version) VALUES (1) ON CONFLICT DO NOTHING;

DO $grant$ BEGIN
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'fyos_graph_lesen') THEN
        GRANT INSERT ON nutzung TO fyos_graph_lesen;
        GRANT USAGE ON SEQUENCE nutzung_id_seq TO fyos_graph_lesen;
    END IF;
END $grant$;
"""


# ── Datentypen ───────────────────────────────────────────────────────────────


@dataclasses.dataclass(frozen=True)
class Knoten:
    id: str
    typ: str
    name: str
    attribute: dict[str, Any] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Kante:
    von: str
    nach: str
    art: str
    gewicht: float = 1.0
    vertrauen: float = 1.0
    beleg: str = ""
    attribute: dict[str, Any] = dataclasses.field(default_factory=dict)


class GraphFehler(RuntimeError):
    """Der Graph ist nicht lesbar oder eine Quelle würde ihn leerräumen."""


def _normal(wert: Any) -> Any:
    """JSON-Rundlauf: gleicher Stand ⇒ gleiche Struktur, egal ob aus Python oder aus jsonb."""
    return json.loads(json.dumps(wert, sort_keys=True, default=str))


# ── Verbindung ───────────────────────────────────────────────────────────────


def verbinden(url: str | None = None, *, schema: str | None = None, frist_s: int = 5, anweisung_s: int = 20) -> Any:
    """Verbindung zum Graphen (psycopg 3, sonst psycopg2). `url` oder `GRAPH_DB_URL`.

    `schema` setzt den search_path (Tests legen ein eigenes Schema an). Die Adresse enthält das Passwort und steht nie in
    einer Fehlermeldung."""
    url = url or os.getenv("GRAPH_DB_URL") or ""
    if not url:
        raise GraphFehler("GRAPH_DB_URL ist nicht gesetzt")
    optionen = f"-c statement_timeout={int(anweisung_s) * 1000}"
    if schema:
        optionen += f" -c search_path={schema}"
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError:
        psycopg = None
    if psycopg is not None:
        return psycopg.connect(url, connect_timeout=frist_s, options=optionen)
    try:
        import psycopg2  # type: ignore[import-not-found]
    except ImportError as exc:
        raise GraphFehler('kein Postgres-Treiber installiert: pip install "fyos-plugin-betrieb[graph]"') from exc
    return psycopg2.connect(url, connect_timeout=frist_s, options=optionen)


def _zeitpunkt(wert: Any) -> dt.datetime | None:
    if wert is None or wert == "":
        return None
    if isinstance(wert, dt.datetime):
        return wert if wert.tzinfo else wert.replace(tzinfo=dt.timezone.utc)
    s = str(wert).strip()
    m = re.fullmatch(r"(\d+)\s*([smhdw])", s.lower())
    if m:
        n = int(m.group(1))
        einheit = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=n * einheit)
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"Zeit nicht lesbar: {wert!r} (z. B. 7d, 36h oder 2026-10-01)") from e
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


# ── Graph ────────────────────────────────────────────────────────────────────


class Graph:
    """Schreib- und Lesezugriff auf eine offene Verbindung. Schreibende Methoden commiten selbst."""

    def __init__(self, conn: Any):
        self.conn = conn

    # -- Hilfen --
    def _ausfuehren(self, sql: str, params: Any = None) -> list[tuple]:
        cur = self.conn.cursor()
        try:
            cur.execute(sql, params)
            return list(cur.fetchall()) if cur.description else []
        finally:
            cur.close()

    def _viele(self, sql: str, zeilen: Sequence[Sequence[Any]]) -> None:
        if not zeilen:
            return
        cur = self.conn.cursor()
        try:
            cur.executemany(sql, zeilen)
        finally:
            cur.close()

    def schema_anlegen(self) -> None:
        cur = self.conn.cursor()
        try:
            cur.execute(SCHEMA_SQL)
        finally:
            cur.close()
        self.conn.commit()

    # ── Schreiben ────────────────────────────────────────────────────────────
    def lauf_beginnen(self, quelle: str) -> int:
        zeilen = self._ausfuehren("INSERT INTO laeufe (quelle) VALUES (%s) RETURNING id", (quelle,))
        self.conn.commit()
        return int(zeilen[0][0])

    def lauf_beenden(self, lauf: int, *, ok: bool, stand: dict[str, Any] | None = None, fehler: str | None = None) -> None:
        s = stand or {}
        self._ausfuehren(
            "UPDATE laeufe SET ende = now(), ok = %s, knoten = %s, kanten = %s, knoten_neu = %s, knoten_zu = %s,"
            " kanten_neu = %s, kanten_zu = %s, fehler = %s WHERE id = %s",
            (
                ok,
                s.get("knoten"),
                s.get("kanten"),
                s.get("knoten_neu"),
                s.get("knoten_zu"),
                s.get("kanten_neu"),
                s.get("kanten_zu"),
                (fehler or None) and fehler[:1000],
                lauf,
            ),
        )
        self.conn.commit()

    def einspeisen(
        self,
        quelle: str,
        knoten: Iterable[Knoten],
        kanten: Iterable[Kante],
        *,
        schutz: bool = True,
    ) -> dict[str, Any]:
        """Gleicht die Zeilen von `quelle` mit dem übergebenen Stand ab (eine Transaktion).

        Neu → eingefügt. Verändert (Typ, Name, Attribute; bei Kanten Gewicht, Vertrauen, Attribute, Beleg) →
        alte Zeile mit `gueltig_bis = now()` geschlossen, neue eingefügt. Nicht mehr genannt → geschlossen.
        Knoten, die schon eine andere Quelle besitzt, werden übersprungen (`kollisionen`).
        `schutz`: schrumpft eine Quelle um mehr als die Hälfte (ab 20 Zeilen), bricht der Abgleich ab."""
        k_neu: dict[str, Knoten] = {}
        for k in knoten:
            if not k.id or not k.typ:
                continue
            k_neu[k.id] = k
        e_neu: dict[tuple[str, str, str], Kante] = {}
        unbekannte_arten: set[str] = set()
        for e in kanten:
            if not e.von or not e.nach or e.von == e.nach:
                continue
            if e.art not in ALLE_ARTEN:
                unbekannte_arten.add(e.art)
            e_neu[(e.von, e.nach, e.art)] = e

        try:
            ist_k = {
                r[1]: r
                for r in self._ausfuehren(
                    "SELECT zeile, id, typ, name, attribute FROM knoten WHERE quelle = %s AND gueltig_bis IS NULL", (quelle,)
                )
            }
            ist_e = {
                (r[1], r[2], r[3]): r
                for r in self._ausfuehren(
                    "SELECT zeile, von, nach, art, gewicht, vertrauen, attribute, beleg FROM kanten"
                    " WHERE quelle = %s AND gueltig_bis IS NULL",
                    (quelle,),
                )
            }
            if schutz:
                for was, ist, neu in (("Knoten", ist_k, k_neu), ("Kanten", ist_e, e_neu)):
                    if len(ist) >= SCHWUND_AB and len(neu) < len(ist) * SCHWUND_ANTEIL:
                        raise GraphFehler(
                            f"{quelle}: {was} schrumpfen von {len(ist)} auf {len(neu)} — Lesefehler vermutet, nichts geändert"
                        )
            fremd: set[str] = set()
            if k_neu:
                fremd = {
                    r[0]
                    for r in self._ausfuehren(
                        "SELECT id FROM knoten WHERE gueltig_bis IS NULL AND quelle <> %s AND id = ANY(%s::text[])",
                        (quelle, list(k_neu)),
                    )
                }
            for i in fremd:
                k_neu.pop(i, None)

            # Knoten
            zu_k: list[int] = []
            neu_k: list[Knoten] = []
            geaendert_k = 0
            for i, k in k_neu.items():
                alt = ist_k.get(i)
                if alt is None:
                    neu_k.append(k)
                    continue
                if alt[2] != k.typ or alt[3] != k.name or _normal(alt[4]) != _normal(k.attribute):
                    zu_k.append(alt[0])
                    neu_k.append(k)
                    geaendert_k += 1
            weg_k = [r[0] for i, r in ist_k.items() if i not in k_neu]
            self._schliessen("knoten", zu_k + weg_k)
            self._viele(
                "INSERT INTO knoten (id, typ, name, attribute, quelle) VALUES (%s, %s, %s, %s::jsonb, %s)",
                [(k.id, k.typ, k.name[:300], json.dumps(_normal(k.attribute), ensure_ascii=False), quelle) for k in neu_k],
            )

            # Kanten
            zu_e: list[int] = []
            neu_e: list[Kante] = []
            geaendert_e = 0
            for schluessel, e in e_neu.items():
                alt = ist_e.get(schluessel)
                if alt is None:
                    neu_e.append(e)
                    continue
                if (
                    abs(float(alt[4]) - e.gewicht) > 1e-6
                    or abs(float(alt[5]) - e.vertrauen) > 1e-6
                    or _normal(alt[6]) != _normal(e.attribute)
                    or (alt[7] or "") != e.beleg[:500]
                ):
                    zu_e.append(alt[0])
                    neu_e.append(e)
                    geaendert_e += 1
            weg_e = [r[0] for s, r in ist_e.items() if s not in e_neu]
            self._schliessen("kanten", zu_e + weg_e)
            self._viele(
                "INSERT INTO kanten (von, nach, art, gewicht, vertrauen, attribute, quelle, beleg)"
                " VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)",
                [
                    (
                        e.von,
                        e.nach,
                        e.art,
                        e.gewicht,
                        e.vertrauen,
                        json.dumps(_normal(e.attribute), ensure_ascii=False),
                        quelle,
                        e.beleg[:500],
                    )
                    for e in neu_e
                ],
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return {
            "quelle": quelle,
            "knoten": len(k_neu),
            "kanten": len(e_neu),
            "knoten_neu": len(neu_k) - geaendert_k,
            "knoten_geaendert": geaendert_k,
            "knoten_zu": len(weg_k),
            "kanten_neu": len(neu_e) - geaendert_e,
            "kanten_geaendert": geaendert_e,
            "kanten_zu": len(weg_e),
            "kollisionen": sorted(fremd)[:20],
            "unbekannte_arten": sorted(unbekannte_arten),
        }

    def _schliessen(self, tabelle: str, zeilen: list[int]) -> None:
        if not zeilen:
            return
        assert tabelle in ("knoten", "kanten")
        self._ausfuehren(f"UPDATE {tabelle} SET gueltig_bis = now() WHERE zeile = ANY(%s::bigint[])", (zeilen,))

    def markierung(self, schluessel: str) -> dict[str, Any] | None:
        z = self._ausfuehren("SELECT wert FROM markierungen WHERE schluessel = %s", (schluessel,))
        return z[0][0] if z else None

    def markieren(self, schluessel: str, wert: dict[str, Any]) -> None:
        self._ausfuehren(
            "INSERT INTO markierungen (schluessel, wert) VALUES (%s, %s::jsonb)"
            " ON CONFLICT (schluessel) DO UPDATE SET wert = EXCLUDED.wert, ts = now()",
            (schluessel, json.dumps(wert, ensure_ascii=False)),
        )
        self.conn.commit()

    # ── Lesen ────────────────────────────────────────────────────────────────
    def aufloesen(self, text: str, *, typ: str | None = None, grenze: int = 25) -> dict[str, Any]:
        """Wen meint `text`? Reihenfolge: genaue ID · genauer Name · Pod-Name (Hash gekürzt) · Teilstring.

        Rückgabe: {"ids": [...], "wie": "id|name|pod|teil|keiner", "treffer": [{id, typ, name}], "weitere": n}"""
        t = (text or "").strip()
        if not t:
            raise ValueError("ziel ist leer")
        tl = t.lower()
        filt = "AND typ = %(typ)s" if typ else ""
        p = {"t": t, "tl": tl, "typ": typ, "g": grenze}

        def holen(sql: str, par: dict[str, Any]) -> list[tuple]:
            return self._ausfuehren(sql, par)

        z = holen(f"SELECT id, typ, name FROM knoten WHERE gueltig_bis IS NULL AND id = %(t)s {filt}", p)
        wie = "id"
        if not z:
            z = holen(
                f"SELECT id, typ, name FROM knoten WHERE gueltig_bis IS NULL AND lower(name) = %(tl)s {filt}"
                " ORDER BY typ, id LIMIT %(g)s",
                p,
            )
            wie = "name"
        if not z:
            gekuerzt = pod_zu_name(t)
            if gekuerzt and gekuerzt != t:
                p["pn"] = gekuerzt.lower()
                z = holen(
                    f"SELECT id, typ, name FROM knoten WHERE gueltig_bis IS NULL AND lower(name) = %(pn)s {filt}"
                    " ORDER BY typ, id LIMIT %(g)s",
                    p,
                )
                wie = "pod"
        if not z:
            p["like"] = "%" + tl.replace("%", "").replace("_", r"\_") + "%"
            z = holen(
                f"SELECT id, typ, name FROM knoten WHERE gueltig_bis IS NULL AND (lower(name) LIKE %(like)s"
                f" OR lower(id) LIKE %(like)s) {filt} ORDER BY length(name), id LIMIT %(g)s",
                p,
            )
            wie = "teil"
        if not z:
            return {"ids": [], "wie": "keiner", "treffer": [], "weitere": 0}
        treffer = [{"id": r[0], "typ": r[1], "name": r[2]} for r in z]
        return {"ids": [r[0] for r in z], "wie": wie, "treffer": treffer, "weitere": 0}

    def knoten_info(self, ids: Sequence[str]) -> dict[str, dict[str, Any]]:
        if not ids:
            return {}
        z = self._ausfuehren(
            "SELECT id, typ, name, attribute, quelle, gueltig_ab FROM knoten WHERE gueltig_bis IS NULL AND id = ANY(%s::text[])",
            (list(ids),),
        )
        return {r[0]: {"id": r[0], "typ": r[1], "name": r[2], "attribute": r[3], "quelle": r[4], "seit": r[5]} for r in z}

    def abhaengig(
        self,
        start: Sequence[str],
        *,
        tiefe: int = STANDARD_TIEFE,
        richtung: str = "an",
        arten: Iterable[str] | None = None,
        hart_nutzt: bool = False,
    ) -> list[dict[str, Any]]:
        """Alles, was (rekursiv) an `start` hängt (`richtung="an"`) oder was `start` braucht (`"braucht"`).

        Je Knoten die kürzeste Tiefe, der Nachbar davor (`via`) und die Kantenart. `hart_nutzt`: `nutzt`-Kanten mit
        attribute.hart zählen auch dann mit, wenn `nutzt` nicht in `arten` steht. Ein weiches `nutzt` (ohne
        attribute.hart) steht im Ergebnis, wird aber nicht weiterverfolgt."""
        tiefe = max(1, min(int(tiefe), TIEFE_MAX))
        arten_l = sorted(set(arten) if arten is not None else TRAGEND)
        if richtung == "an":
            nach_spalte, ziel_spalte = "nach", "von"
        elif richtung == "braucht":
            nach_spalte, ziel_spalte = "von", "nach"
        else:
            raise ValueError("richtung: 'an' oder 'braucht'")
        # Eine weiche `nutzt`-Kante (nicht attribute.hart) wird gezeigt, aber nicht weiterverfolgt: wer nur ein bisschen
        # an X hängt, zieht nicht alles mit, was von ihm abhängt (sonst hängt über den Hub jedes Plugin an Qdrant).
        sql = f"""
            WITH RECURSIVE r(id, tiefe, via, art, weich) AS (
                SELECT s, 0, NULL::text, NULL::text, false FROM unnest(%(start)s::text[]) AS s
              UNION
                SELECT k.{ziel_spalte}, r.tiefe + 1, r.id, k.art, (k.art = 'nutzt' AND COALESCE(k.attribute->>'hart', '') <> 'true')
                FROM r JOIN kanten k ON k.{nach_spalte} = r.id
                WHERE k.gueltig_bis IS NULL AND r.tiefe < %(tiefe)s AND NOT r.weich
                  AND (k.art = ANY(%(arten)s::text[]) OR (%(hart_nutzt)s AND k.art = 'nutzt' AND k.attribute->>'hart' = 'true'))
            )
            SELECT DISTINCT ON (r.id) r.id, r.tiefe, r.via, r.art, n.typ, n.name
            FROM r LEFT JOIN knoten n ON n.id = r.id AND n.gueltig_bis IS NULL
            WHERE r.tiefe > 0 AND NOT r.id = ANY(%(start)s::text[])
            ORDER BY r.id, r.tiefe
        """
        z = self._ausfuehren(sql, {"start": list(start), "arten": arten_l, "tiefe": tiefe, "hart_nutzt": bool(hart_nutzt)})
        out = [
            {"id": r[0], "tiefe": r[1], "via": r[2], "art": r[3], "typ": r[4] or typ_aus_id(r[0]), "name": r[5] or name_aus_id(r[0])}
            for r in z
        ]
        out.sort(key=lambda d: (d["tiefe"], d["typ"], d["name"]))
        return out

    def auswirkung(self, start: Sequence[str], *, tiefe: int = TIEFE_MAX) -> dict[str, Any]:
        """Was fällt aus, wenn `start` ausfällt?

        Kandidaten: alles, was rekursiv über tragende Kanten an `start` hängt. Dann ein Fixpunkt:
        ein Knoten fällt aus, wenn eines seiner harten Ziele ausfällt oder wenn alle Ziele einer ODER-Gruppe
        (`laeuft_auf`) ausfallen und mindestens eine davon hart gepinnt ist. Sind alle Ziele weg, aber keines
        gepinnt, wird er neu eingeplant (`umgeplant`); ist nur ein Teil der Gruppe weg, läuft er `eingeschraenkt`.

        Nur harte Kanten (HART, dazu `nutzt` mit attribute.hart) geben den Ausfall weiter. Wer den Ausgefallenen nur
        über eine weiche `nutzt`-Kante verwendet, steht unter `beeintraechtigt` (läuft, kann aber Funktionen verlieren)."""
        start = list(start)
        kand = self.abhaengig(start, tiefe=tiefe, richtung="an", arten=HART, hart_nutzt=True)
        kand_ids = [k["id"] for k in kand]
        if not kand_ids:
            weich = self._beeintraechtigt(start, set(), tiefe)
            return {"ausgefallen": [], "umgeplant": [], "eingeschraenkt": [], "beeintraechtigt": weich, "kandidaten": 0}
        z = self._ausfuehren(
            "SELECT von, nach, art, attribute FROM kanten WHERE gueltig_bis IS NULL"
            " AND (art = ANY(%s::text[]) OR (art = 'nutzt' AND attribute->>'hart' = 'true'))"
            " AND von = ANY(%s::text[])",
            (sorted(HART), kand_ids),
        )
        aus_kanten: dict[str, list[tuple[str, str, dict[str, Any]]]] = {}
        for von, nach, art, attr in z:
            aus_kanten.setdefault(von, []).append((nach, art, attr or {}))
        info = {k["id"]: k for k in kand}
        ausgefallen: dict[str, dict[str, Any]] = {s: {"runde": 0, "grund": None} for s in start}
        umgeplant: dict[str, dict[str, Any]] = {}
        eingeschraenkt: dict[str, dict[str, Any]] = {}
        runde = 0
        geaendert = True
        while geaendert and runde < TIEFE_MAX + 5:
            geaendert = False
            runde += 1
            for kid in kand_ids:
                if kid in ausgefallen:
                    continue
                grund = None
                weich_fehlt = None
                gruppen: dict[str, list[tuple[str, dict[str, Any]]]] = {}
                for nach, art, attr in aus_kanten.get(kid, []):
                    if art in ODER_ARTEN:
                        gruppen.setdefault(art, []).append((nach, attr))
                    elif nach in ausgefallen and grund is None:
                        grund = (nach, art)
                if grund is None:
                    for art, ziele in gruppen.items():
                        weg = [n for n, _ in ziele if n in ausgefallen]
                        if not weg:
                            continue
                        if len(weg) == len(ziele):
                            gepinnt = any((a or {}).get("pin") == "hart" for _, a in ziele)
                            if gepinnt:
                                grund = (weg[0], art)
                            else:
                                weich_fehlt = (weg[0], art)
                        else:
                            eingeschraenkt.setdefault(kid, {"via": weg[0], "art": art, "von_n": len(ziele), "weg_n": len(weg)})
                if grund is not None:
                    ausgefallen[kid] = {"runde": runde, "grund": grund}
                    umgeplant.pop(kid, None)
                    eingeschraenkt.pop(kid, None)
                    geaendert = True
                elif weich_fehlt is not None and kid not in umgeplant:
                    umgeplant[kid] = {"via": weich_fehlt[0], "art": weich_fehlt[1]}

        def zeile(kid: str, extra: dict[str, Any]) -> dict[str, Any]:
            i = info.get(kid) or {}
            return {"id": kid, "typ": i.get("typ") or typ_aus_id(kid), "name": i.get("name") or name_aus_id(kid), **extra}

        schon = set(ausgefallen) | set(umgeplant) | set(eingeschraenkt)
        weich = self._beeintraechtigt(start, {k for k in ausgefallen if k not in start}, tiefe, ausnahmen=schon)
        return {
            "ausgefallen": sorted(
                (
                    zeile(k, {"tiefe": v["runde"], "via": v["grund"][0], "art": v["grund"][1]})
                    for k, v in ausgefallen.items()
                    if k not in start and v["grund"]
                ),
                key=lambda d: (d["tiefe"], d["typ"], d["name"]),
            ),
            "umgeplant": sorted(
                (zeile(k, {"via": v["via"], "art": v["art"]}) for k, v in umgeplant.items() if k not in ausgefallen),
                key=lambda d: (d["typ"], d["name"]),
            ),
            "eingeschraenkt": sorted(
                (
                    zeile(k, {"via": v["via"], "art": v["art"], "von_n": v["von_n"], "weg_n": v["weg_n"]})
                    for k, v in eingeschraenkt.items()
                    if k not in ausgefallen
                ),
                key=lambda d: (d["typ"], d["name"]),
            ),
            "beeintraechtigt": weich,
            "kandidaten": len(kand_ids),
        }

    def _beeintraechtigt(self, start: list[str], ausgefallen: set[str], tiefe: int, *, ausnahmen: set[str] | None = None) -> list[dict[str, Any]]:
        """Wer nutzt `start` oder einen Ausgefallenen nur weich (`nutzt` ohne attribute.hart), ohne selbst
        auszufallen? Das sind die, die weiterlaufen, aber Funktionen verlieren können. Nur der direkte Schritt:
        was von ihnen wiederum abhängt, merkt vom Ausfall nichts, solange sie selbst laufen."""
        ausnahmen = (ausnahmen or set()) | set(start)
        quellen = sorted(set(start) | ausgefallen)
        z = self._ausfuehren(
            "SELECT DISTINCT ON (k.von) k.von, k.nach, n.typ, n.name FROM kanten k"
            " LEFT JOIN knoten n ON n.id = k.von AND n.gueltig_bis IS NULL"
            " WHERE k.gueltig_bis IS NULL AND k.art = 'nutzt' AND COALESCE(k.attribute->>'hart', '') <> 'true'"
            " AND k.nach = ANY(%s::text[]) ORDER BY k.von, k.nach",
            (quellen,),
        )
        out = [
            {"id": von, "tiefe": 1, "via": nach, "art": "nutzt", "typ": typ or typ_aus_id(von), "name": name or name_aus_id(von)}
            for von, nach, typ, name in z
            if von not in ausnahmen
        ]
        out.sort(key=lambda d: (d["typ"], d["name"]))
        return out

    def pfad(self, von: Sequence[str], nach: Sequence[str], *, tiefe: int = 6) -> dict[str, Any]:
        """Kürzester Weg zwischen zwei Knotenmengen, Kanten in beide Richtungen. Zuerst nur über tragende
        Kanten; gibt es so keinen, über alle (`nur_info = True`)."""
        tiefe = max(1, min(int(tiefe), 10))
        ziel = set(nach)
        for arten, nur_info in ((sorted(TRAGEND), False), (sorted(ALLE_ARTEN), True)):
            sql = """
                WITH RECURSIVE nb(a, b, art, rich) AS (
                    SELECT von, nach, art, 'vor' FROM kanten WHERE gueltig_bis IS NULL AND art = ANY(%(arten)s::text[])
                  UNION ALL
                    SELECT nach, von, art, 'zurueck' FROM kanten WHERE gueltig_bis IS NULL AND art = ANY(%(arten)s::text[])
                ), w(id, tiefe, eltern, art, rich) AS (
                    SELECT s, 0, NULL::text, NULL::text, NULL::text FROM unnest(%(start)s::text[]) AS s
                  UNION
                    SELECT nb.b, w.tiefe + 1, w.id, nb.art, nb.rich FROM w JOIN nb ON nb.a = w.id WHERE w.tiefe < %(tiefe)s
                )
                SELECT id, tiefe, eltern, art, rich FROM w
            """
            z = self._ausfuehren(sql, {"arten": arten, "start": list(von), "tiefe": tiefe})
            beste: dict[str, tuple[int, str | None, str | None, str | None]] = {}
            for i, t, eltern, art, rich in z:
                if i not in beste or t < beste[i][0]:
                    beste[i] = (t, eltern, art, rich)
            treffer = sorted((beste[i][0], i) for i in ziel if i in beste)
            if not treffer:
                continue
            _, ende = treffer[0]
            kette: list[dict[str, Any]] = []
            cur = ende
            while True:
                t, eltern, art, rich = beste[cur]
                kette.append({"id": cur, "art": art, "richtung": rich})
                if eltern is None:
                    break
                cur = eltern
            kette.reverse()
            return {"gefunden": True, "laenge": len(kette) - 1, "kette": kette, "nur_info": nur_info}
        return {"gefunden": False, "laenge": None, "kette": [], "nur_info": False}

    def verlauf(self, ids: Sequence[str], seit: Any, *, grenze: int = 200) -> list[dict[str, Any]]:
        """Änderungen an `ids` (Knoten) und ihren Kanten seit `seit`: neu, weg, geändert (mit Attribut-Unterschied)."""
        t0 = _zeitpunkt(seit) or (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7))
        ereignisse: list[dict[str, Any]] = []
        ids_l = list(ids)
        zk = self._ausfuehren(
            "SELECT id, typ, name, attribute, quelle, gueltig_ab, gueltig_bis FROM knoten WHERE id = ANY(%s::text[])"
            " ORDER BY id, gueltig_ab",
            (ids_l,),
        )
        letzte: dict[str, tuple] = {}
        for i, typ, name, attr, quelle, ab, bis in zk:
            vorher = letzte.get(i)
            if ab >= t0:
                if vorher is None:
                    ereignisse.append({"ts": ab, "was": "neu", "ding": "knoten", "id": i, "text": f"{typ} {name} entsteht ({quelle})"})
                else:
                    diff = attribut_unterschied(vorher[3], attr)
                    if vorher[1] != typ or vorher[2] != name:
                        diff.append(f"typ/name {vorher[1]}/{vorher[2]} → {typ}/{name}")
                    ereignisse.append({"ts": ab, "was": "geaendert", "ding": "knoten", "id": i, "text": "; ".join(diff) or "Attribute geändert"})
            if bis is not None and bis >= t0:
                ereignisse.append({"ts": bis, "was": "weg", "ding": "knoten", "id": i, "text": f"{typ} {name} verschwindet"})
            letzte[i] = (i, typ, name, attr)
        ek = self._ausfuehren(
            "SELECT von, nach, art, attribute, quelle, beleg, gueltig_ab, gueltig_bis FROM kanten"
            " WHERE (von = ANY(%s::text[]) OR nach = ANY(%s::text[])) AND (gueltig_ab >= %s OR gueltig_bis >= %s)"
            " ORDER BY gueltig_ab",
            (ids_l, ids_l, t0, t0),
        )
        for von, nach, art, attr, quelle, beleg, ab, bis in ek:
            kurz = f"{von} —{art}→ {nach}"
            if ab >= t0:
                tag = ""
                if art == "nutzt_image" and (attr or {}).get("tag"):
                    tag = f" ({attr['tag']})"
                ereignisse.append({"ts": ab, "was": "neu", "ding": "kante", "id": kurz, "text": kurz + tag + f" [{quelle}]"})
            if bis is not None and bis >= t0:
                ereignisse.append({"ts": bis, "was": "weg", "ding": "kante", "id": kurz, "text": kurz + f" [{quelle}]"})
        ereignisse.sort(key=lambda e: e["ts"], reverse=True)
        return ereignisse[:grenze]

    def zustand(self) -> dict[str, Any]:
        """Zahlen je Quelle, letzter Lauf, Nutzung (30 Tage), offene Kanten ohne Knoten."""
        k = self._ausfuehren("SELECT quelle, count(*) FROM knoten WHERE gueltig_bis IS NULL GROUP BY quelle ORDER BY quelle")
        e = self._ausfuehren("SELECT quelle, count(*) FROM kanten WHERE gueltig_bis IS NULL GROUP BY quelle ORDER BY quelle")
        l = self._ausfuehren(
            "SELECT DISTINCT ON (quelle) quelle, beginn, ende, ok, fehler FROM laeufe ORDER BY quelle, beginn DESC"
        )
        l_ok = self._ausfuehren(
            "SELECT quelle, max(ende) FROM laeufe WHERE ok GROUP BY quelle"
        )
        waise = self._ausfuehren(
            "SELECT count(*) FROM kanten k WHERE k.gueltig_bis IS NULL AND (NOT EXISTS (SELECT 1 FROM knoten n"
            " WHERE n.id = k.von AND n.gueltig_bis IS NULL) OR NOT EXISTS (SELECT 1 FROM knoten n WHERE n.id = k.nach"
            " AND n.gueltig_bis IS NULL))"
        )
        n = self._ausfuehren(
            "SELECT werkzeug, aufrufer, count(*) FROM nutzung WHERE ts > now() - interval '30 days'"
            " GROUP BY werkzeug, aufrufer ORDER BY 3 DESC LIMIT 60"
        )
        typen = self._ausfuehren("SELECT typ, count(*) FROM knoten WHERE gueltig_bis IS NULL GROUP BY typ ORDER BY 2 DESC")
        quellen: dict[str, dict[str, Any]] = {}
        for q, c in k:
            quellen.setdefault(q, {})["knoten"] = c
        for q, c in e:
            quellen.setdefault(q, {})["kanten"] = c
        for q, beginn, ende, ok, fehler in l:
            quellen.setdefault(q, {}).update({"letzter_lauf": beginn, "letzter_ok": ok, "fehler": fehler})
        for q, ende in l_ok:
            quellen.setdefault(q, {})["zuletzt_ok"] = ende
        return {
            "quellen": quellen,
            "typen": {t: c for t, c in typen},
            "kanten_ohne_knoten": int(waise[0][0]) if waise else 0,
            "nutzung_30t": [{"werkzeug": w, "aufrufer": a, "anzahl": c} for w, a, c in n],
        }

    def nutzung_vermerken(self, werkzeug: str, aufrufer: str, ziel: str | None, treffer: int | None, dauer_ms: int, ok: bool) -> None:
        try:
            self._ausfuehren(
                "INSERT INTO nutzung (werkzeug, aufrufer, ziel, treffer, dauer_ms, ok) VALUES (%s, %s, %s, %s, %s, %s)",
                (werkzeug, (aufrufer or "unbekannt")[:60], (ziel or "")[:200] or None, treffer, dauer_ms, ok),
            )
            self.conn.commit()
        except Exception:  # noqa: BLE001 — Zählen darf nie eine Abfrage kosten
            try:
                self.conn.rollback()
            except Exception:  # noqa: BLE001
                pass

    # ── Betrachtung für Menschen: alle gültigen Knoten und Kanten, für Ansichten ──
    def aktuelle_knoten(self, *, typ: str | None = None, grenze: int = 5000) -> list[tuple]:
        if typ:
            return self._ausfuehren(
                "SELECT id, typ, name, attribute, quelle FROM knoten WHERE gueltig_bis IS NULL AND typ = %s ORDER BY id LIMIT %s",
                (typ, grenze),
            )
        return self._ausfuehren(
            "SELECT id, typ, name, attribute, quelle FROM knoten WHERE gueltig_bis IS NULL ORDER BY id LIMIT %s", (grenze,)
        )

    def aktuelle_kanten(self, *, grenze: int = 20000) -> list[tuple]:
        return self._ausfuehren(
            "SELECT von, nach, art, gewicht, vertrauen, quelle FROM kanten WHERE gueltig_bis IS NULL LIMIT %s", (grenze,)
        )


# ── reine Hilfsfunktionen ────────────────────────────────────────────────────

_POD_STS = re.compile(r"^(?P<n>.+)-\d{1,3}$")
_POD_RS = re.compile(r"^(?P<n>.+)-[a-f0-9]{6,10}-[a-z0-9]{5}$")
_POD_DS = re.compile(r"^(?P<n>.+)-[a-z0-9]{5}$")


def pod_zu_name(pod: str) -> str:
    """`qdrant-64f566945f-bq4gw` → `qdrant`; `loki-stack-0` → `loki-stack`; sonst unverändert."""
    for rx in (_POD_RS, _POD_DS, _POD_STS):
        m = rx.match(pod)
        if m:
            return m.group("n")
    return pod


def typ_aus_id(i: str) -> str:
    return i.split(":", 1)[0] if ":" in i else "?"


def name_aus_id(i: str) -> str:
    rest = i.split(":", 1)[1] if ":" in i else i
    return rest.rsplit("/", 1)[-1]


def attribut_unterschied(alt: dict[str, Any] | None, neu: dict[str, Any] | None) -> list[str]:
    a, n = alt or {}, neu or {}
    out = []
    for k in sorted(set(a) | set(n)):
        if a.get(k) != n.get(k):
            out.append(f"{k}: {_kurz(a.get(k))} → {_kurz(n.get(k))}")
    return out


def _kurz(w: Any, n: int = 60) -> str:
    s = "—" if w is None else (w if isinstance(w, str) else json.dumps(w, ensure_ascii=False))
    return s if len(s) <= n else s[: n - 1] + "…"


def zeit_text(t: Any) -> str:
    """Datum für Sätze: 2026-10-08 23:30 (Berlin)."""
    if not isinstance(t, dt.datetime):
        return str(t)
    try:
        from zoneinfo import ZoneInfo

        t = t.astimezone(ZoneInfo("Europe/Berlin"))
    except Exception:  # noqa: BLE001
        pass
    return t.strftime("%Y-%m-%d %H:%M")


def gruppiert(zeilen: Sequence[dict[str, Any]], *, je_typ: int = 8) -> dict[str, Any]:
    """{typ: {"anzahl": n, "namen": [... höchstens je_typ ...]}} für knappe Antworten."""
    out: dict[str, dict[str, Any]] = {}
    for z in zeilen:
        g = out.setdefault(z["typ"], {"anzahl": 0, "namen": []})
        g["anzahl"] += 1
        if len(g["namen"]) < je_typ:
            g["namen"].append(z["name"])
    return out


def jetzt_ms() -> float:
    return time.monotonic() * 1000.0
