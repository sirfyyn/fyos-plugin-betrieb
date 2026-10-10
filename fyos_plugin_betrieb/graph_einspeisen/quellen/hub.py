"""Quelle hub — die Plugins des MCP-Hubs und was hinter ihnen steht.

Der Hub lädt installierte Plugin-Pakete von selbst (Entry-Point-Gruppe `mcp_hub.plugins`); seine Konfiguration nennt nur
Plugins mit Konfiguration, Umgebung oder Abschaltung. Beides zusammen ergibt die Plugin-Liste:

  - Entry-Points: die der Umgebung des Einspeisers. Er läuft darum am besten im selben Bild wie der Hub.
  - Hub-Konfiguration (optional): eine Datei (`GRAPH_HUB_CONFIG`) oder eine ConfigMap im Cluster
    (`GRAPH_HUB_CONFIGMAP=<namespace>/<name>`, Schlüssel `GRAPH_HUB_CONFIG_SCHLUESSEL`, Vorgabe `config.yaml`); ein
    ConfigMap-Manifest als Datei geht auch. Ein Eintrag mit `enabled: false` macht ein Plugin zum inaktiven Knoten.

Die Werkzeuge je Plugin kommen aus dem Bestandsbuch (Quelle bestand), wenn eines eingerichtet ist.

Knoten
    plugin:<name>   plugin   (modul, aktiv)
Kanten
    plugin   braucht   <GRAPH_HUB_DIENST>   Plugins laufen im Prozess des Hubs (Graph-ID des Hub-Dienstes, z. B.
                                            `dienst:ns/hub`; ohne Angabe keine Kante)
    plugin   nutzt     service:<ns>/<name>  Dienstadresse im Plugin-Code oder in seiner Konfiguration
               beleg = Modul:Zeile (nur der Ort, nicht der Wert), vertrauen 0.8
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any, Iterable

import yaml

from ...betriebsgraph import Kante, Knoten

NAME = "hub"
GRUPPE = "mcp_hub.plugins"

_ADRESSE = re.compile(r"\b([a-z0-9][a-z0-9-]*)\.([a-z0-9][a-z0-9-]*)\.svc(?:\.cluster\.local)?\b")


def plugins_aus_config(text: str, schluessel: str = "config.yaml") -> dict[str, dict[str, Any]]:
    """Plugins aus der Hub-Konfiguration (Text der Konfiguration oder eines ConfigMap-Manifests, das sie unter
    `data.<schluessel>` trägt): Name → {modul (letzter Teil), voll (Modulpfad), aktiv, config}."""
    cfg = yaml.safe_load(text) or {}
    if isinstance(cfg, dict) and "plugins" not in cfg and isinstance(cfg.get("data"), dict):
        cfg = yaml.safe_load(str(cfg["data"].get(schluessel) or "")) or {}
    out: dict[str, dict[str, Any]] = {}
    for p in (cfg.get("plugins") if isinstance(cfg, dict) else None) or []:
        if not isinstance(p, dict) or not p.get("name"):
            continue
        voll = str(p.get("module") or "")
        out[str(p["name"])] = {
            "modul": voll.rsplit(".", 1)[-1],
            "voll": voll,
            "aktiv": bool(p.get("enabled", True)),
            "config": p.get("config") or {},
        }
    return out


def entry_points_lesen() -> list[tuple[str, str]]:
    """(Name, Modulpfad) je Entry-Point der Gruppe `mcp_hub.plugins`, ohne die Plugins zu laden."""
    from importlib.metadata import entry_points

    return [(ep.name, ep.value.split(":", 1)[0].strip()) for ep in entry_points(group=GRUPPE)]


def plugins_aus_entry_points(eintritte: Iterable[tuple[str, str]], schon: Iterable[str] = ()) -> dict[str, dict[str, Any]]:
    """Entry-Points, die die Konfiguration nicht schon nennt: Name → wie `plugins_aus_config`, aktiv, ohne Konfiguration."""
    vergeben = set(schon)
    out: dict[str, dict[str, Any]] = {}
    for name, modul in eintritte:
        if name in vergeben or name in out:
            continue
        out[name] = {"modul": modul.rsplit(".", 1)[-1], "voll": modul, "aktiv": True, "config": {}}
    return out


def modul_text(modul: str) -> str | None:
    """Der Quelltext eines Python-Moduls (ohne es auszuführen) oder `None`."""
    if not modul:
        return None
    try:
        spec = importlib.util.find_spec(modul)
    except (ImportError, ValueError, AttributeError):
        return None
    datei = getattr(spec, "origin", None)
    if not datei or not str(datei).endswith(".py"):
        return None
    try:
        return Path(datei).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def adressen_in(text: str) -> list[tuple[int, str, str]]:
    """(Zeile, ns, service) je Fundstelle – Zeilen mit Kommentarzeichen am Anfang zählen nicht."""
    out: list[tuple[int, str, str]] = []
    for nr, zeile in enumerate(text.splitlines(), 1):
        if zeile.lstrip().startswith("#"):
            continue
        for m in _ADRESSE.finditer(zeile):
            out.append((nr, m.group(2), m.group(1)))
    return out


def config_text(ctx: Any) -> tuple[str, str] | None:
    """(Text, Schlüssel) der Hub-Konfiguration, wenn eine eingerichtet ist."""
    schluessel = ctx.einstellung("GRAPH_HUB_CONFIG_SCHLUESSEL", "config.yaml")
    datei = ctx.einstellung("GRAPH_HUB_CONFIG")
    if datei:
        try:
            return Path(datei).read_text(encoding="utf-8"), schluessel
        except OSError as e:
            raise RuntimeError(f"Hub-Konfiguration {datei} nicht lesbar ({type(e).__name__})") from e
    ref = ctx.einstellung("GRAPH_HUB_CONFIGMAP")
    if ref:
        ns, _, name = ref.partition("/")
        if not ns or not name:
            raise RuntimeError("GRAPH_HUB_CONFIGMAP muss <namespace>/<name> heißen")
        for c in ctx.k8s_liste("configmaps"):
            meta = c.get("metadata") or {}
            if meta.get("namespace") == ns and meta.get("name") == name:
                text = (c.get("data") or {}).get(schluessel)
                if not text:
                    raise RuntimeError(f"ConfigMap {ref}: kein Schlüssel {schluessel}")
                return str(text), schluessel
        raise RuntimeError(f"ConfigMap {ref} nicht gefunden")
    return None


def sammeln(ctx: Any) -> tuple[list[Knoten], list[Kante]]:
    gelesen = config_text(ctx)
    konfig = plugins_aus_config(*gelesen) if gelesen else {}
    eintritte = entry_points_lesen()
    modul_zu_name = dict(eintritte)
    for name, p in konfig.items():
        if not p["voll"]:  # Eintrag ohne `module`: das Plugin kommt aus einem Entry-Point gleichen Namens
            p["voll"] = modul_zu_name.get(name, "")
            p["modul"] = p["voll"].rsplit(".", 1)[-1] or name
    plugins = dict(konfig)
    plugins.update(plugins_aus_entry_points(eintritte, konfig))
    hub_dienst = ctx.einstellung("GRAPH_HUB_DIENST")
    knoten: list[Knoten] = []
    kanten: dict[tuple[str, str, str], Kante] = {}
    for name in sorted(plugins):
        p = plugins[name]
        pid = f"plugin:{name}"
        knoten.append(Knoten(pid, "plugin", name, {"modul": p["modul"], "aktiv": p["aktiv"]}))
        if hub_dienst:
            kanten[(pid, hub_dienst, "braucht")] = Kante(pid, hub_dienst, "braucht", beleg="Plugins laufen im Prozess des Hubs")
        quellen: list[tuple[str, str]] = []
        text = modul_text(p["voll"])
        if text:
            quellen.append((p["voll"], text))
        if p["config"]:
            quellen.append((f"Hub-Konfiguration (plugin {name})", yaml.safe_dump(p["config"], allow_unicode=True)))
        for ort, text in quellen:
            for nr, ns, svc in adressen_in(text):
                sid = f"service:{ns}/{svc}"
                if not ctx.gibt(sid):
                    continue
                schluessel = (pid, sid, "nutzt")
                if schluessel not in kanten:
                    kanten[schluessel] = Kante(pid, sid, "nutzt", vertrauen=0.8, beleg=f"{ort}:{nr}")
    return knoten, list(kanten.values())
