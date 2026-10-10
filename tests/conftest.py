"""Gemeinsame Vorgaben: die Tests laufen ohne Redis, Qdrant, Cluster und Anmeldung, egal was die Umgebung setzt.

Das Paket hat keine Vorgabe auf eine bestimmte Installation (jede Einstellung ist leer = nicht eingerichtet). Die Tests
rechnen deshalb mit einer erfundenen Umgebung aus dem Beispielbereich (example.org, 192.0.2.0/24); Tests, die den
Zustand „nicht eingerichtet“ prüfen, nehmen die Einstellungen mit `monkeypatch.delenv` wieder weg."""

from __future__ import annotations

import os

import pytest

#: Erfundene Basis-Umgebung der Tests (Name -> Wert). Nichts davon gehört zu einer echten Installation.
BASIS_UMGEBUNG = {
    "GITEA_API_BASE": "https://git.example.org/api/v1",
    "GITEA_REPO": "beispiel/projekt",
    "PROMETHEUS_URL": "http://prometheus.example.org:9090",
    "REDIS_URL": "redis://redis.example.org:6379",
    "FYOS_REPO_PATH": "/pfad/zum/repo-klon",
    "FYOS_AGENTDIENST_URL": "http://agent.example.org:8000",
    "FYOS_AUFGABEN_URL": "http://aufgaben.example.org",
    "FYOS_EINSICHTEN_URL": "http://einsichten.example.org",
    "GROW_URL": "http://grow.example.org",
    "WIKI_API_BASE": "http://wiki.example.org/api/wiki",
    "FYOS_PANE_AUTH": "beispiel-kopfwert",
    "FYOS_FENSTER_KNOTEN": "alpha,beta,gamma,delta",
}

#: Alles, was das Paket aus der Umgebung liest und was ein Testlauf aus der Umgebung des Entwicklers nicht erben darf.
ALLE_NAMEN = (
    *BASIS_UMGEBUNG,
    "GITEA_TOKEN", "GITEA_PAKET_TOKEN", "FYOS_AGENTDIENST_TOKEN",
    "FYOS_AGENT_URL", "FYOS_AGENT_TOKEN",
    "FYOS_REPOS_BESITZER", "FRIST_CADDY", "FRIST_ZERT_HOSTS", "FRIST_BACKUP_LABEL", "FYOS_FENSTER_KNOTEN",
    "BETRIEB_NAMESPACE", "ARBEIT_NAMESPACE", "FYOS_BETRIEB_AUTOMATISMEN_CONFIGMAP", "FYOS_BETRIEB_WERKZEUGE_CONFIGMAP",
    "FYOS_BETRIEB_PFADPRAEFIXE", "FYOS_PROZESSKARTE_LANES", "FYOS_ARBEIT_WORKFLOW", "FYOS_ARBEIT_WORKFLOW_NAME",
    "FYOS_ARBEIT_BILD", "FYOS_ARBEIT_TOKEN_SECRET", "FYOS_ARBEIT_PULL_SECRETS", "FYOS_ARBEIT_MEIDE_KNOTEN",
    "FYOS_ARBEIT_ZWEIG_VORSATZ", "FYOS_ARBEIT_TESTWURZEL", "FYOS_ARBEIT_PYTHONPATH", "FYOS_CLUSTER_MESSKNOTEN",
    "FYOS_CLUSTER_MESSSPUREN", "FYOS_CLUSTER_NIE_ABSPERREN", "FYOS_FLE_DATEN", "FYOS_OMADA_URL", "FYOS_TRUENAS_URL",
    "FYOS_KERBERUZ_URL", "FYOS_VORGANG_LINK",
)


def pytest_configure(config):
    os.environ.setdefault("MCP_HUB_AUDIT_SKIP_QDRANT", "1")
    # `lage` liest einige Einstellungen beim Import (für Importeure, die Konstanten brauchen): vorher die Basis setzen.
    for name in ALLE_NAMEN:
        os.environ.pop(name, None)
    os.environ.update(BASIS_UMGEBUNG)


@pytest.fixture(autouse=True)
def basis_umgebung(monkeypatch):
    """Jeder Test beginnt mit der erfundenen Basis-Umgebung; alles andere ist nicht gesetzt."""
    for name in ALLE_NAMEN:
        monkeypatch.delenv(name, raising=False)
    for name, wert in BASIS_UMGEBUNG.items():
        monkeypatch.setenv(name, wert)


@pytest.fixture()
def ohne_vorgangsquelle(monkeypatch):
    """Weder Vorgangs-Dienst noch Workboard: die Quelle der Vorgänge ist nicht eingerichtet (und danach wieder so).

    Der Hub hält die Ablage und den Dienst prozessweit; ein Test, der sie setzt, räumt hinterher auf."""
    from mcp_hub import vorgaenge

    monkeypatch.delenv("FYOS_VORGAENGE_URL", raising=False)
    monkeypatch.delenv("FYOS_VORGAENGE_KOPF", raising=False)
    vorgaenge.dienst_anmelden(None)
    vorgaenge.ablage_anmelden(None)
    yield
    vorgaenge.dienst_anmelden(None)
    vorgaenge.ablage_anmelden(None)
