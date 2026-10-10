"""Quelle caddy — Routen aus dem Caddyfile zu den Diensten dahinter.

Das Caddyfile kommt aus einer Datei (`GRAPH_CADDYFILE`; ein ConfigMap-Manifest mit `data.<Schlüssel>` geht auch) oder aus
einer ConfigMap im Cluster (`GRAPH_CADDY_CONFIGMAP=<namespace>/<name>`, Schlüssel `GRAPH_CADDY_SCHLUESSEL`, Vorgabe
`Caddyfile`). Ist keines von beidem gesetzt, ist die Quelle aus.

Knoten
    route:<host>        route   (pfade: die `handle`-Pfade mit Upstream, höchstens 12)
    extern:<name>       extern  Geräte außerhalb des Clusters (aus der Rechner-Datei, Abschnitt `extern`)
Kanten
    route   routet_zu   service:<ns>/<name>   Upstream `reverse_proxy http://<name>.<ns>.svc…`
    route   routet_zu   <Dienst auf einem Rechner>  Upstream auf Adresse+Port eines Dienstes aus der Rechner-Datei
    route   routet_zu   knoten:<name>         Upstream auf einer Rechner-Adresse, Port unbekannt
    route   routet_zu   extern:<name>
    route   braucht     <GRAPH_CADDY_DIENST>  die Route lebt im Caddy (Graph-ID des Dienstes; bei einer ConfigMap
                                              ohne Angabe `dienst:<namespace>/<name>` der ConfigMap)
    route   braucht     <GRAPH_CADDY_AUTH>    wenn der Block `forward_auth` oder einen Anmelde-Snippet importiert
                                              (Graph-ID des Anmeldedienstes, z. B. `service:ns/authentik`; ohne
                                              Angabe keine Kante; Snippet-Namen: `GRAPH_CADDY_AUTH_SNIPPETS`,
                                              kommagetrennt, Vorgabe `authentik`)
Der Parser zählt Klammern; Platzhalter wie `{host}` stehen immer in einer Zeile und heben sich auf.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ...betriebsgraph import Kante, Knoten
from ..kontext import Ausgeschaltet
from . import rechner

NAME = "caddy"

_SVC = re.compile(r"^([a-z0-9-]+)\.([a-z0-9-]+)\.svc(?:\.cluster\.local)?$")
_UPSTREAM = re.compile(r"^\s*reverse_proxy\s+(?:@\S+\s+)?(\S+)")
_GEHT_AUF = re.compile(r"\{\s*$")
PFADE_MAX = 12


def _ohne_kommentar(zeile: str) -> str:
    # Caddy kennt `#` nur am Wortanfang als Kommentar; für unsere Zwecke genügt: ab ` #` oder Zeilenanfang abschneiden
    s = zeile.strip()
    if s.startswith("#"):
        return ""
    return re.sub(r"\s#\s.*$", "", s)


#: Snippet-Namen, deren `import` eine Anmeldung bedeutet, auch wenn das Snippet nicht im Caddyfile steht.
AUTH_SNIPPETS = ("authentik",)


def parse(text: str, auth_snippets: tuple[str, ...] = AUTH_SNIPPETS) -> list[dict[str, Any]]:
    """Caddyfile → [{hosts, upstreams: [(url, pfad)], auth}] je Seitenblock.

    Snippets `(name) { … }` werden eingelesen und über `import name` in die Seitenblöcke gefaltet (rekursiv)."""
    seiten: list[dict[str, Any]] = []
    snippets: dict[str, dict[str, Any]] = {}
    tiefe = 0
    aktuell: dict[str, Any] | None = None
    stapel: list[tuple[int, str]] = []  # (Tiefe vor dem Öffnen, Kopfzeile)
    for roh in text.splitlines():
        z = _ohne_kommentar(roh)
        if not z:
            continue
        netto = z.count("{") - z.count("}")
        if tiefe == 0 and _GEHT_AUF.search(z) and netto > 0:
            kopf = z[:-1].strip()
            stapel = []
            if kopf.startswith("("):
                aktuell = {"hosts": [], "upstreams": [], "auth": False, "imports": []}
                snippets[kopf[1 : kopf.index(")")]] = aktuell
            elif kopf and not kopf.startswith("{"):
                hosts = [h.strip().removeprefix("https://").removeprefix("http://") for h in re.split(r"[,\s]+", kopf) if h.strip()]
                aktuell = {"hosts": hosts, "upstreams": [], "auth": False, "imports": []}
                seiten.append(aktuell)
            else:
                aktuell = None
            tiefe += netto
            continue
        if aktuell is not None:
            m = _UPSTREAM.match(z)
            if m:
                pfad = next((k for _, k in reversed(stapel) if k.startswith(("handle", "route"))), "")
                aktuell["upstreams"].append((m.group(1), pfad))
            if z.startswith("forward_auth"):
                aktuell["auth"] = True
            if z.startswith("import "):
                aktuell["imports"].append(z.split()[1])
            if netto > 0 and _GEHT_AUF.search(z):
                stapel.append((tiefe, z[:-1].strip()))
        tiefe += netto
        while stapel and stapel[-1][0] >= tiefe:
            stapel.pop()
        if tiefe <= 0:
            tiefe = 0
            aktuell = None if netto < 0 else aktuell

    def falten(s: dict[str, Any], gesehen: tuple[str, ...]) -> None:
        for name in s.get("imports", []):
            if name in auth_snippets:
                s["auth"] = True
            sn = snippets.get(name)
            if not sn or name in gesehen:
                continue
            falten(sn, gesehen + (name,))
            s["upstreams"] += sn["upstreams"]
            s["auth"] = s["auth"] or sn["auth"]
        s["imports"] = []

    for s in seiten:
        falten(s, ())
    return seiten


def upstream_ziel(url: str, adressen: dict[str, str], dienst_ports: dict[tuple[str, int], str], extern: dict[str, dict[str, Any]]) -> tuple[str, str] | None:
    """Upstream-Adresse → (ziel_id, wie). None, wenn dynamisch ({env…}) oder nicht erkennbar."""
    if "{" in url:
        return None
    rest = re.sub(r"^[a-z]+://", "", url).split("/")[0]
    host, _, port = rest.partition(":")
    m = _SVC.match(host)
    if m:
        return f"service:{m.group(2)}/{m.group(1)}", "service"
    if host in extern:
        return extern[host]["id"], "extern"
    if host in adressen:
        if port.isdigit() and (host, int(port)) in dienst_ports:
            return dienst_ports[(host, int(port))], "rechner"
        return adressen[host], "knoten"
    return None


def caddyfile(ctx: Any) -> tuple[str, str, str]:
    """(Text, Herkunft für den Beleg, Graph-ID des Caddy-Dienstes laut ConfigMap oder leer)."""
    datei = ctx.einstellung("GRAPH_CADDYFILE")
    schluessel = ctx.einstellung("GRAPH_CADDY_SCHLUESSEL", "Caddyfile")
    if datei:
        pfad = Path(datei)
        try:
            roh = pfad.read_text(encoding="utf-8")
        except OSError as e:
            raise RuntimeError(f"Caddyfile {datei} nicht lesbar ({type(e).__name__})") from e
        if pfad.suffix in (".yaml", ".yml"):
            manifest = yaml.safe_load(roh)
            text = ((manifest.get("data") if isinstance(manifest, dict) else None) or {}).get(schluessel)
        else:
            text = roh
        if not text:
            raise RuntimeError(f"{datei}: kein Caddyfile (Schlüssel data.{schluessel})")
        return str(text), str(datei), ""
    ref = ctx.einstellung("GRAPH_CADDY_CONFIGMAP")
    if ref:
        ns, _, name = ref.partition("/")
        if not ns or not name:
            raise RuntimeError("GRAPH_CADDY_CONFIGMAP muss <namespace>/<name> heißen")
        for c in ctx.k8s_liste("configmaps"):
            meta = c.get("metadata") or {}
            if meta.get("namespace") == ns and meta.get("name") == name:
                text = (c.get("data") or {}).get(schluessel)
                if not text:
                    raise RuntimeError(f"ConfigMap {ref}: kein Schlüssel {schluessel}")
                return str(text), f"configmap {ref}", f"dienst:{ref}"
        raise RuntimeError(f"ConfigMap {ref} nicht gefunden")
    raise Ausgeschaltet("kein Caddyfile eingerichtet (GRAPH_CADDYFILE oder GRAPH_CADDY_CONFIGMAP)")


def sammeln(ctx: Any) -> tuple[list[Knoten], list[Kante]]:
    text, herkunft, standard_dienst = caddyfile(ctx)
    caddy_dienst = ctx.einstellung("GRAPH_CADDY_DIENST") or standard_dienst
    auth_dienst = ctx.einstellung("GRAPH_CADDY_AUTH")
    auth_snippets = tuple(s.strip() for s in ctx.einstellung("GRAPH_CADDY_AUTH_SNIPPETS").split(",") if s.strip()) or AUTH_SNIPPETS
    kur = rechner.laden(ctx) or {}
    adressen = {str(a): str(k) for a, k in (kur.get("adressen") or {}).items()}
    extern = {str(a): dict(e) for a, e in (kur.get("extern") or {}).items()}
    dienst_ports: dict[tuple[str, int], str] = {}
    standard_rechner = kur.get("standard_rechner")
    for d in kur.get("dienste") or []:
        if d.get("port"):
            ihr_rechner = d.get("rechner") or standard_rechner
            for ip, k in adressen.items():
                if k == ihr_rechner:
                    dienst_ports[(ip, int(d["port"]))] = d["id"]

    knoten: dict[str, Knoten] = {}
    kanten: dict[tuple[str, str, str], Kante] = {}
    for s in parse(text, auth_snippets):
        if not s["upstreams"]:
            continue
        hosts = [h for h in s["hosts"] if "." in h or h.startswith("localhost")]
        if not hosts:
            continue
        host = hosts[0]
        rid = f"route:{host}"
        pfade: list[str] = []
        ziele: dict[str, str] = {}
        for url, pfad in s["upstreams"]:
            z = upstream_ziel(url, adressen, dienst_ports, extern)
            if z is None:
                continue
            ziele.setdefault(z[0], z[1])
            if pfad and pfad not in pfade:
                pfade.append(pfad)
        if not ziele:
            continue
        attr: dict[str, Any] = {"hosts": hosts[:6]}
        if pfade:
            attr["pfade"] = sorted(pfade)[:PFADE_MAX]
        knoten[rid] = Knoten(rid, "route", host, attr)
        for zid in sorted(ziele):
            kanten[(rid, zid, "routet_zu")] = Kante(rid, zid, "routet_zu", beleg=herkunft)
            if zid.startswith("extern:"):
                e = next(v for v in extern.values() if v["id"] == zid)
                knoten[zid] = Knoten(zid, "extern", e.get("name") or zid, {"beleg": e.get("beleg")})
        if caddy_dienst:
            kanten[(rid, caddy_dienst, "braucht")] = Kante(rid, caddy_dienst, "braucht", beleg="Caddy bedient die Route")
        if s["auth"] and auth_dienst:
            kanten[(rid, auth_dienst, "braucht")] = Kante(rid, auth_dienst, "braucht", beleg="forward_auth / import der Anmeldung")
    return list(knoten.values()), list(kanten.values())
