"""Betriebsgraph als Teil des Pakets: Entry-Point, Konsolenskript, Extras, Pakete im Wheel, Beispiel-Manifeste und
die Zusage, dass nichts Persönliches mitreist."""

from __future__ import annotations

import re
import tomllib
from importlib import import_module
from pathlib import Path
from typing import Any

import pytest
import yaml

import fyos_plugin_betrieb

WURZEL = Path(__file__).resolve().parent.parent
PAKET = WURZEL / "fyos_plugin_betrieb"
DEPLOY = WURZEL / "deploy" / "graph"
PYPROJECT = tomllib.loads((WURZEL / "pyproject.toml").read_text(encoding="utf-8"))

#: Die Platzhalter der Beispiel-Manifeste; jeder steht in der README und wird in mindestens einer Datei gebraucht.
PLATZHALTER = {"<NAMESPACE>", "<IMAGE>", "<GRAPH_DB_SECRET>", "<POSTGRES_APP>", "<HUB_APP>"}


# ── pyproject ────────────────────────────────────────────────────────────────


def test_entry_point_graph_und_konsolenskript() -> None:
    assert PYPROJECT["project"]["entry-points"]["mcp_hub.plugins"]["graph"] == "fyos_plugin_betrieb.graph:GraphPlugin"
    ziel = PYPROJECT["project"]["scripts"]["fyos-graph-einspeisen"]
    modul, _, funktion = ziel.partition(":")
    assert callable(getattr(import_module(modul), funktion))
    assert ziel == "fyos_plugin_betrieb.graph_einspeisen.einspeisen:main"


def test_extra_graph_bringt_den_postgres_treiber_mit() -> None:
    graph = PYPROJECT["project"]["optional-dependencies"]["graph"]
    assert len(graph) == 1 and graph[0].startswith("psycopg[binary]")
    # Das Paket selbst braucht den Treiber nicht: ohne ihn lädt das Plugin und meldet sich „aus" bzw. ungesund
    assert not any(d.lower().startswith("psycopg") for d in PYPROJECT["project"]["dependencies"])


def test_version_stimmt_mit_dem_paket_ueberein() -> None:
    assert PYPROJECT["project"]["version"] == fyos_plugin_betrieb.__version__


def test_jedes_unterpaket_steht_in_der_paketliste() -> None:
    """Ein Unterordner mit `__init__.py`, der in `packages` fehlt, fehlt im Wheel: das Konsolenskript liefe ins Leere."""
    gefunden = {
        ".".join(p.parent.relative_to(WURZEL).parts) for p in PAKET.rglob("__init__.py") if "__pycache__" not in p.parts
    }
    assert gefunden == set(PYPROJECT["tool"]["setuptools"]["packages"])
    assert {"fyos_plugin_betrieb.graph_einspeisen", "fyos_plugin_betrieb.graph_einspeisen.quellen"} <= gefunden


def test_die_beispieldatei_reist_im_wheel_mit() -> None:
    assert "daten/*" in PYPROJECT["tool"]["setuptools"]["package-data"]["fyos_plugin_betrieb"]
    assert (PAKET / "daten" / "rechner.beispiel.yaml").is_file()


def test_das_plugin_laedt_ohne_postgres_treiber_und_ohne_adresse() -> None:
    from fyos_plugin_betrieb.graph import GraphPlugin

    plugin = GraphPlugin()
    assert plugin.name == "graph" and plugin.describe_tools()
    gesund = plugin.health()
    assert gesund["ok"] is False and gesund["status"] == "aus"  # ohne Einrichtung: aus, kein Alarm im Lagebild


# ── Beispiel-Manifeste ───────────────────────────────────────────────────────


def _dokumente(name: str) -> list[dict[str, Any]]:
    return [d for d in yaml.safe_load_all((DEPLOY / name).read_text(encoding="utf-8")) if d]


def _alle() -> list[dict[str, Any]]:
    return [d for datei in sorted(DEPLOY.glob("*.yaml")) if datei.name != "kustomization.yaml" for d in _dokumente(datei.name)]


def _texte(wert: Any):
    if isinstance(wert, str):
        yield wert
    elif isinstance(wert, dict):
        for v in wert.values():
            yield from _texte(v)
    elif isinstance(wert, list):
        for v in wert:
            yield from _texte(v)


def test_die_manifeste_sind_gueltiges_yaml_mit_den_erwarteten_arten() -> None:
    arten = sorted(d["kind"] for d in _alle())
    assert arten == sorted(["ServiceAccount", "ClusterRole", "ClusterRoleBinding", "CronJob", *["NetworkPolicy"] * 4])
    assert all(d.get("apiVersion") and d["metadata"].get("name") for d in _alle())


def test_kein_secret_im_repo() -> None:
    """Das Secret mit den Datenbank-Adressen legt der Betreiber an; es gehört in keine Datei des Repos."""
    assert not {"Secret", "SealedSecret"} & {d["kind"] for d in _alle()}
    for datei in DEPLOY.glob("*"):
        text = datei.read_text(encoding="utf-8")
        assert not re.search(r"postgres(ql)?://[^\s<>]*:[^\s<>@]+@", text), datei.name  # keine Adresse mit Passwort


def test_die_leserechte_sind_nur_lesen_und_ohne_secrets() -> None:
    (rolle,) = [d for d in _alle() if d["kind"] == "ClusterRole"]
    for regel in rolle["rules"]:
        assert set(regel["verbs"]) <= {"get", "list"}, regel
        assert "*" not in regel["resources"] and "secrets" not in regel["resources"], regel
    freigegeben = {r for regel in rolle["rules"] for r in regel["resources"]}
    # Alles, was die Kubernetes-Zugriffe des Einspeisers lesen (k8s_zugang.ARTEN und .CRD), ist freigegeben
    from fyos_plugin_betrieb.graph_einspeisen.k8s_zugang import ARTEN, CRD

    gebraucht = {
        {"persistent_volume_claim": "persistentvolumeclaims", "persistent_volume": "persistentvolumes", "config_map": "configmaps",
         "stateful_set": "statefulsets", "daemon_set": "daemonsets", "cron_job": "cronjobs"}.get(einzahl, einzahl + "s")
        for (_api, einzahl, _gebunden, _kind) in ARTEN.values()
    } | {plural for (_gruppe, plural) in CRD.values()}
    assert gebraucht <= freigegeben, gebraucht - freigegeben


def test_cronjob_rbac_und_netzregeln_passen_zusammen() -> None:
    (cron,) = [d for d in _alle() if d["kind"] == "CronJob"]
    pod = cron["spec"]["jobTemplate"]["spec"]["template"]
    konto = pod["spec"]["serviceAccountName"]
    (sa,) = [d for d in _alle() if d["kind"] == "ServiceAccount"]
    (rolle,) = [d for d in _alle() if d["kind"] == "ClusterRole"]
    (bindung,) = [d for d in _alle() if d["kind"] == "ClusterRoleBinding"]
    assert sa["metadata"]["name"] == konto and pod["spec"]["automountServiceAccountToken"] is True
    assert bindung["roleRef"]["name"] == rolle["metadata"]["name"]
    assert bindung["subjects"] == [{"kind": "ServiceAccount", "name": konto, "namespace": sa["metadata"]["namespace"]}]
    # Die Netzregeln zielen auf die Beschriftung der Pods des CronJobs
    beschriftung = pod["metadata"]["labels"]["app"]
    regeln = [d for d in _alle() if d["kind"] == "NetworkPolicy"]
    assert any(
        beschriftung in {sel.get("podSelector", {}).get("matchLabels", {}).get("app") for e in r["spec"].get("ingress", []) for sel in e["from"]}
        for r in regeln
    )
    assert any(r["spec"]["podSelector"]["matchLabels"].get("app") == beschriftung and "Egress" in r["spec"]["policyTypes"] for r in regeln)


def test_cronjob_startet_das_konsolenskript_und_holt_die_adresse_aus_dem_secret() -> None:
    (cron,) = [d for d in _alle() if d["kind"] == "CronJob"]
    (container,) = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"]
    assert container["command"] == ["fyos-graph-einspeisen"]
    umgebung = {e["name"]: e for e in container["env"]}
    adresse = umgebung["GRAPH_DB_URL"]
    assert "value" not in adresse and adresse["valueFrom"]["secretKeyRef"]["key"] == "url_einspeiser"
    assert container["securityContext"]["readOnlyRootFilesystem"] is True and container["securityContext"]["allowPrivilegeEscalation"] is False
    assert cron["spec"]["concurrencyPolicy"] == "Forbid" and cron["spec"]["jobTemplate"]["spec"]["activeDeadlineSeconds"] > 0
    # Einrichten der Quellen steht nur auskommentiert da: nichts Fremdes ist vorbelegt
    assert set(umgebung) == {"PYTHONUNBUFFERED", "GRAPH_DB_URL"}


def test_platzhalter_gehoeren_zum_dokumentierten_satz() -> None:
    benutzt: set[str] = set()
    for dokument in _alle():
        for text in _texte(dokument):
            benutzt |= set(re.findall(r"<[^<>\s]+>", text))
    assert benutzt == PLATZHALTER


def test_die_readme_erklaert_jeden_platzhalter() -> None:
    readme = (WURZEL / "README.md").read_text(encoding="utf-8")
    fehlt = [p for p in sorted(PLATZHALTER) if p not in readme]
    assert not fehlt, fehlt


def test_kustomization_nennt_die_dateien_des_ordners() -> None:
    liste = yaml.safe_load((DEPLOY / "kustomization.yaml").read_text(encoding="utf-8"))["resources"]
    assert sorted(liste) == sorted(p.name for p in DEPLOY.glob("*.yaml") if p.name != "kustomization.yaml")


# ── nichts Persönliches ──────────────────────────────────────────────────────

#: Namen aus der Umgebung des Autors, die in allgemeinem Code nichts zu suchen haben.
#: Rückwärts geschrieben, damit diese Liste selbst kein Treffer der Namenssuche ist.
_NAMEN_RUECKWAERTS = ("natit", "aehr", "ynnig", "sanuert", "nyyfris", "rezibtam", "yrtsiger-baltig", "erotsdloc", "nostej", "anerhplys", "zurebrek")
NAMEN = re.compile(r"\b(" + "|".join(n[::-1] for n in _NAMEN_RUECKWAERTS) + r")\b", re.I)
MAX = re.compile(r"\bMax\b")  # der Vorname, nicht `max(`
#: Adressen aus privaten Bereichen und dem CGNAT-Bereich (Tailscale); die Beispiele nutzen 192.0.2.x.
ADRESSE = re.compile(
    r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}"
    r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3})\b"
)


def _mitgebrachte_dateien() -> list[Path]:
    dateien = [PAKET / "graph.py", PAKET / "betriebsgraph.py", PAKET / "daten" / "rechner.beispiel.yaml"]
    dateien += sorted((PAKET / "graph_einspeisen").rglob("*.py"))
    dateien += sorted(DEPLOY.glob("*"))
    dateien += sorted(WURZEL.glob("tests/test_graph*.py")) + [WURZEL / "tests" / "test_betriebsgraph.py"]
    return [d for d in dateien if d.name != "test_graph_paket.py"]  # diese Datei nennt die Namen selbst


def test_die_mitgebrachten_dateien_gibt_es() -> None:
    namen = {d.name for d in _mitgebrachte_dateien()}
    assert {"graph.py", "betriebsgraph.py", "rechner.beispiel.yaml", "einspeisen.py", "itsm.py", "einspeisen-cronjob.yaml"} <= namen


@pytest.mark.parametrize("datei", _mitgebrachte_dateien(), ids=lambda p: str(p.relative_to(WURZEL)))
def test_nichts_persoenliches_im_mitgebrachten_code(datei: Path) -> None:
    gefunden = []
    for nr, zeile in enumerate(datei.read_text(encoding="utf-8").splitlines(), 1):
        if NAMEN.search(zeile) or MAX.search(zeile) or ADRESSE.search(zeile):
            gefunden.append(f"{nr}: {zeile.strip()[:120]}")
    assert not gefunden, gefunden
