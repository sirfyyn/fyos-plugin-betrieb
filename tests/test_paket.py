"""Paket als Ganzes: Entry-Points, Katalog, Skills, gemeinsame Hilfen von `lage`, Lade-Probe aller Plugins."""

from __future__ import annotations

import re
import tomllib
from importlib import metadata
from pathlib import Path

import pytest
import yaml

import fyos_plugin_betrieb
from fyos_plugin_betrieb import _umgebung, katalog_pfad

WURZEL = Path(__file__).resolve().parent.parent
PAKET = WURZEL / "fyos_plugin_betrieb"

# Hilfen, die die anderen Plugin-Repos von `fyos_plugin_betrieb.lage` importieren.
LAGE_HILFEN = [
    "_Zwischenspeicher", "_redis", "_iso", "PANE_AUTH", "dienst", "DIENSTE", "gitea", "REPO", "GITEA_API",
    "prometheus", "fenster_offen", "sperrgrund", "_bis_text", "fenster_argv", "fenster_setzen",
    "FENSTER_KNOTEN", "FENSTER_ARTEN", "FENSTER_MAX_MINUTEN", "LagePlugin",
]


def _eintraege():
    daten = tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))
    return daten["project"]["entry-points"]["mcp_hub.plugins"]


def test_lage_hilfen_sind_importierbar():
    from fyos_plugin_betrieb import lage

    fehlt = [n for n in LAGE_HILFEN if not hasattr(lage, n)]
    assert not fehlt, fehlt


def test_lage_importiert_keine_andere_plugin_gruppe():
    """Keine Rückkante: das Basis-Paket importiert kein anderes persönliches Plugin-Paket."""
    for datei in PAKET.glob("*.py"):
        text = datei.read_text(encoding="utf-8")
        for anderes in ("fyos_plugin_konten", "fyos_plugin_modell", "fyos_plugin_haus", "fyos_plugin_leitstand"):
            assert anderes not in text, f"{datei.name}: {anderes}"


def test_entry_points_zeigen_auf_vorhandene_klassen():
    from importlib import import_module

    from mcp_hub.plugin import HubPlugin

    for name, ziel in _eintraege().items():
        modul, klasse = ziel.split(":")
        cls = getattr(import_module(modul), klasse)
        assert issubclass(cls, HubPlugin), name
        assert cls.name == name or name in ("git",), (name, cls.name)


def test_entry_points_sind_installiert():
    installiert = {ep.name for ep in metadata.entry_points(group="mcp_hub.plugins")}
    fehlt = set(_eintraege()) - installiert
    if fehlt == set(_eintraege()):
        pytest.skip("Paket nicht installiert")
    assert not fehlt


def test_katalog_hat_die_pflichtfelder_und_ein_bild():
    pfad = katalog_pfad()
    daten = yaml.safe_load(pfad.read_text(encoding="utf-8"))
    (b,) = daten["bausteine"]
    for feld in ("id", "titel", "kurz", "was_daraus_wird", "art", "bilder", "quelle", "konfiguration"):
        assert b[feld], feld
    assert b["art"] == "plugin" and "\n" not in b["kurz"].strip()
    for bild in b["bilder"]:
        assert (pfad.parent / "bilder" / bild).is_file(), bild
    quelltext = "\n".join(p.read_text(encoding="utf-8") for p in PAKET.glob("*.py"))
    for feld in b["konfiguration"]:
        assert feld["umgebung"] in quelltext, feld["umgebung"]


def test_katalog_besteht_die_pruefung_des_installers():
    katalog = pytest.importorskip("fyos_installer.katalog", reason="fyos-os/installer nicht installiert")
    bausteine = katalog.lade_datei(katalog_pfad())
    assert [b.id for b in bausteine] == ["betrieb"]
    assert katalog.bilder_von(bausteine[0]), "Bild nicht gefunden"


def test_skills_reisen_im_paket_mit():
    daten = tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))
    assert daten["project"]["entry-points"]["fyos.skills"]["betrieb"] == "fyos_plugin_betrieb"
    assert "skills/*/*.md" in daten["tool"]["setuptools"]["package-data"]["fyos_plugin_betrieb"]
    for name in ("fyos-git-remotes", "fyos-rollout-verifikation"):
        assert (PAKET / "skills" / name / "SKILL.md").is_file()


def test_skills_auffindbar():
    from mcp_hub.plugins.skills import SkillsPlugin

    skills = SkillsPlugin(config={"quellen": [str(PAKET / "skills")]})
    namen = {s["name"] for s in skills.call("suchen", {"q": "rollout"})["skills"]}
    assert "fyos-rollout-verifikation" in namen


#: Adressen, die zu einer bestimmten Installation gehören: Rechner im LAN, Namen im Heimnetz.
_LAN = re.compile(
    r"\b(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b|\.fyos\.lan"
)
#: Namen aus der Umgebung des Autors. Rückwärts geschrieben, damit diese Liste selbst kein Treffer der Namenssuche ist.
_EIGENNAMEN = re.compile("|".join(n[::-1] for n in ("ynnig", "aehr", "natit", "nyyfris", "anerhplys", "rezibtam")), re.I)
#: Stellen, an denen ein Treffer der Namenssuche kein Name ist.
_ERLAUBT = tuple(
    e.lower()
    for e in (
        "overhead",  # Wortteil (Pod-Overhead), kein Name
        "sauber: ok",  # Urheberzeile der Lizenz und Verweis auf das Hauptrepo (wie bei tools/sauber.py)
    )
)
_TEXT = {".py", ".yaml", ".yml", ".md", ".toml", ".json", ".sh", ".html", ".txt"}
_UEBERSPRINGEN = {".git", "__pycache__", ".venv", "build", "dist", ".pytest_cache", "fyos_plugin_betrieb.egg-info"}


def _textdateien():
    for datei in sorted(WURZEL.rglob("*")):
        if datei.is_file() and datei.suffix in _TEXT and not (_UEBERSPRINGEN & set(datei.relative_to(WURZEL).parts)):
            yield datei


def test_paket_nennt_keine_lan_adressen_und_keine_cluster_dienste():
    """Kein Code, Text, Beispiel und Test zeigt auf LAN-Adressen; Dienste im Cluster nur als Platzhalter oder Parser-Eingabe."""
    funde = []
    for datei in _textdateien():
        if datei.name == Path(__file__).name or datei.name == "test_graph_paket.py":
            continue  # diese Tests nennen die Muster selbst
        for nr, zeile in enumerate(datei.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            cluster = ".svc.cluster.local" in zeile and "<" not in zeile and datei.name != "test_graph_quellen.py"
            if _LAN.search(zeile) or cluster:
                funde.append(f"{datei.relative_to(WURZEL)}:{nr}: {zeile.strip()[:100]}")
    assert not funde, "\n".join(funde)


def test_paket_nennt_keine_eigennamen():
    funde = []
    for datei in _textdateien():
        if datei.name in (Path(__file__).name, "test_graph_paket.py", "uv.lock"):
            continue
        for nr, zeile in enumerate(datei.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _EIGENNAMEN.search(zeile) and not any(e in zeile.lower() for e in _ERLAUBT):
                funde.append(f"{datei.relative_to(WURZEL)}:{nr}: {zeile.strip()[:100]}")
    assert not funde, "\n".join(funde)


def test_readme_nennt_die_einstellungen_der_dienste():
    readme = (WURZEL / "README.md").read_text(encoding="utf-8")
    for name in ("FYOS_AGENTDIENST_TOKEN", "FYOS_AUFGABEN_URL", "FYOS_FLE_DATEN", "FYOS_FENSTER_KNOTEN"):
        assert f"`{name}`" in readme, name
    assert not hasattr(_umgebung, "ALIAS"), "es gibt keine Alias-Namen für Einstellungen"


def test_katalog_nennt_die_einstellungen_ohne_vorgabe_auf_dienste():
    daten = yaml.safe_load(katalog_pfad().read_text(encoding="utf-8"))
    (b,) = daten["bausteine"]
    namen = [k["umgebung"] for k in b["konfiguration"]]
    assert len(set(namen)) == len(namen)
    assert {"FYOS_AGENTDIENST_TOKEN", "FYOS_AUFGABEN_URL", "FYOS_FLE_DATEN"} <= set(namen)
    readme = (WURZEL / "README.md").read_text(encoding="utf-8")
    for k in b["konfiguration"]:
        assert k["standard"] == "", f"{k['umgebung']}: eine Vorgabe auf einen Dienst gehört nicht in den Katalog"
        assert f"`{k['umgebung']}`" in readme, k["umgebung"]


def test_beispielkonfiguration_ist_neutral():
    text = (WURZEL / "hub-config.beispiel.yaml").read_text(encoding="utf-8")
    assert yaml.safe_load(text)["plugins"]
    assert "example.org" in text and not _LAN.search(text)


def test_lade_probe_alle_plugins_und_die_app():
    """Jedes Plugin lässt sich bauen, beschreibt seine Werkzeuge, und die App-Ressource löst sich auf."""
    from importlib import import_module

    gesamt = 0
    for name, ziel in _eintraege().items():
        modul, klasse = ziel.split(":")
        plugin = getattr(import_module(modul), klasse)()
        werkzeuge = plugin.describe_tools()
        assert werkzeuge, name
        gesamt += len(werkzeuge)
        for r in plugin.describe_resources():
            html = r.read()
            assert "<html" in html.lower() or "<!doctype" in html.lower(), r.uri
            assert "/*STIL*/" not in html and "/*BRUECKE*/" not in html, r.uri
    assert gesamt > 50
    assert fyos_plugin_betrieb.__version__
