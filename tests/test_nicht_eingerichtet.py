"""Ohne Einstellungen ist jede Fähigkeit „nicht eingerichtet“: kein Ausnahmefehler, keine leere Liste als Antwort, sondern
der eigene Zustand mit dem Weg zum Einrichten (lesend) bzw. `ok: false` mit demselben Hinweis (schreibend).

Dazu: die Namen der Einstellungen, leere Werte zählen als nicht gesetzt, `_redis()` wirft nie."""

from __future__ import annotations

import pytest

from conftest import ALLE_NAMEN
from fyos_plugin_betrieb import _umgebung, arbeit, betrieb, cluster, fle_apps, git_unified, grow, lage, pipelines, repos
from fyos_plugin_betrieb.kerberuz_proxy import KerberuzProxyPlugin
from fyos_plugin_betrieb.omada_proxy import OmadaProxyPlugin
from fyos_plugin_betrieb.truenas_proxy import TrueNASProxyPlugin


@pytest.fixture()
def leer(monkeypatch, tmp_path):
    """Keine einzige Einstellung des Pakets in der Umgebung, kein Cluster-Zugang."""
    for name in ALLE_NAMEN:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("GITEA_TOKEN", raising=False)
    monkeypatch.setenv("KUBECONFIG", str(tmp_path / "gibt-es-nicht"))  # nie die Kubeconfig des Rechners benutzen
    monkeypatch.setattr(betrieb, "REPO_KLON", None)  # Import-Stand der Konstante


def nicht_eingerichtet(antwort) -> bool:
    """Ist die Antwort der eigene Zustand „nicht eingerichtet“ (lesend) mit Weg zum Einrichten?"""
    return (
        isinstance(antwort, dict)
        and antwort.get("zustand") == _umgebung.NICHT_EINGERICHTET
        and antwort.get("eingerichtet") is False
        and bool(antwort.get("einrichten"))
        and bool(antwort.get("satz"))
    )


def abgelehnt(antwort) -> bool:
    """Schreibend: `ok: false`, nichts geändert, mit Weg zum Einrichten."""
    return (
        isinstance(antwort, dict)
        and antwort.get("ok") is False
        and antwort.get("zustand") == _umgebung.NICHT_EINGERICHTET
        and bool(antwort.get("einrichten"))
        and "nichts geändert" in str(antwort.get("error"))
    )


# ── Grundbausteine ───────────────────────────────────────────────────────────
def test_zustand_und_abgelehnt_haben_die_vereinbarte_form():
    z = _umgebung.zustand("Der Dienst", "Setze X.")
    assert z == {"zustand": "nicht eingerichtet", "eingerichtet": False, "satz": "Der Dienst ist nicht eingerichtet.", "einrichten": "Setze X."}
    a = _umgebung.abgelehnt("Der Dienst", "Setze X.")
    assert a["ok"] is False and a["zustand"] == "nicht eingerichtet" and a["einrichten"] == "Setze X."


def test_nicht_eingerichtet_ist_eine_runtime_error():
    """Aufrufer, die Quellenausfälle als RuntimeError abfangen (Leitstand), fangen den Fall mit."""
    with pytest.raises(RuntimeError):
        _umgebung.verlangen("NIE_GESETZT_ABC", "Das Ding")


def test_redis_wirft_nie_ohne_adresse(leer):
    r = lage._redis()  # kein Ausnahmefehler beim Holen
    with pytest.raises(_umgebung.NichtEingerichtet):
        r.get("x")


def test_gitea_prometheus_dienst_ohne_einstellung(leer):
    for aufruf in (
        lambda: lage.gitea("/x"),
        lambda: lage.prometheus("/api/v1/query", {"query": "1"}),
        lambda: lage.dienst("agent", "/x"),
        lambda: lage.dienst("aufgaben", "/x"),
        lambda: lage.dienst("einsichten", "/x"),
    ):
        with pytest.raises(_umgebung.NichtEingerichtet) as e:
            aufruf()
        assert e.value.einrichten and nicht_eingerichtet(e.value.lesend()) and abgelehnt(e.value.schreibend())


# ── Einstellungen lesen ──────────────────────────────────────────────────────
def test_leere_einstellung_zaehlt_als_nicht_gesetzt(monkeypatch):
    monkeypatch.setenv("FYOS_BEISPIEL_X", "  ")
    assert _umgebung.lesen("FYOS_BEISPIEL_X") == ""
    monkeypatch.setenv("FYOS_BEISPIEL_X", " wert ")
    assert _umgebung.lesen("FYOS_BEISPIEL_X") == "wert"


def test_agent_token_wird_als_bearer_geschickt(monkeypatch):
    gesehen = {}

    class Antwort:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    monkeypatch.setattr(lage.urllib.request, "urlopen", lambda req, timeout=6: gesehen.update(dict(req.header_items())) or Antwort())
    monkeypatch.delenv("FYOS_AGENTDIENST_TOKEN", raising=False)
    lage.dienst("agent", "/x")
    assert "Authorization" not in gesehen
    monkeypatch.setenv("FYOS_AGENTDIENST_TOKEN", "beispiel-token")
    lage.dienst("agent", "/x")
    assert gesehen["Authorization"] == "Bearer beispiel-token"


def test_gateway_der_kopplung_ist_nicht_der_agenten_dienst(monkeypatch):
    """FYOS_AGENT_URL/FYOS_AGENT_TOKEN gehören dem OpenClaw-Gateway (Kopplung im Kern). Der Agenten-Dienst nimmt sie
    nie: weder als Adresse noch als Bearer."""
    gesehen = {}

    class Antwort:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"

    monkeypatch.setattr(lage.urllib.request, "urlopen", lambda req, timeout=6: gesehen.update(url=req.full_url, kopf=dict(req.header_items())) or Antwort())
    monkeypatch.setenv("FYOS_AGENT_URL", "http://gateway.example.org:18789")
    monkeypatch.setenv("FYOS_AGENT_TOKEN", "gateway-token")
    monkeypatch.delenv("FYOS_AGENTDIENST_URL", raising=False)
    with pytest.raises(_umgebung.NichtEingerichtet):
        lage.dienst("agent", "/nexus/agents")
    monkeypatch.setenv("FYOS_AGENTDIENST_URL", "http://agent.example.org:8000")
    lage.dienst("agent", "/nexus/agents")
    assert gesehen["url"].startswith("http://agent.example.org:8000/")
    assert "Authorization" not in gesehen["kopf"]


def test_aufgaben_url_und_nur_diese_dienstnamen(monkeypatch):
    monkeypatch.setenv("FYOS_AUFGABEN_URL", "http://neu.example.org/")
    assert lage.dienst_url("aufgaben") == "http://neu.example.org"
    assert set(lage.DIENSTE) == {"agent", "aufgaben", "einsichten"}
    with pytest.raises(KeyError):
        lage.dienst_url("unbekannt")


def test_fle_daten_aus_der_umgebung(monkeypatch, tmp_path):
    monkeypatch.setenv("FYOS_FLE_DATEN", str(tmp_path / "neu"))
    assert str(fle_apps.FleAppsPlugin().daten) == str(tmp_path / "neu")
    monkeypatch.delenv("FYOS_FLE_DATEN")
    assert not fle_apps.FleAppsPlugin().daten


def test_pane_kopfzeile_nur_mit_wert(monkeypatch):
    monkeypatch.delenv("FYOS_PANE_AUTH", raising=False)
    assert "X-FYOS-Pane-Auth" not in lage.pane_auth()
    monkeypatch.setenv("FYOS_PANE_AUTH", "wert")
    assert lage.pane_auth()["X-FYOS-Pane-Auth"] == "wert"


def test_fenster_knoten_ohne_einstellung_leer_und_mit_einstellung_die_liste(monkeypatch):
    monkeypatch.delenv("FYOS_FENSTER_KNOTEN", raising=False)
    assert lage.fenster_knoten() == []
    monkeypatch.setenv("FYOS_FENSTER_KNOTEN", "a, b ,c")
    assert lage.fenster_knoten() == ["a", "b", "c"]


def test_fenster_setzen_ohne_knoten_ist_nicht_eingerichtet(lage_plugin, monkeypatch):
    monkeypatch.delenv("FYOS_FENSTER_KNOTEN", raising=False)
    assert abgelehnt(lage_plugin.call("fenster_setzen", {"grund": "messen"}))
    schema = next(t for t in lage_plugin.describe_tools() if t.name == "fenster_setzen").input_schema
    assert "enum" not in schema["properties"]["knoten"]
    monkeypatch.setenv("FYOS_FENSTER_KNOTEN", "a,b")
    schema = next(t for t in lage_plugin.describe_tools() if t.name == "fenster_setzen").input_schema
    assert schema["properties"]["knoten"]["enum"] == ["a", "b"] and schema["properties"]["knoten"]["default"] == "a"


# ── lage ─────────────────────────────────────────────────────────────────────
@pytest.fixture()
def lage_plugin(leer):
    return lage.LagePlugin()


def test_lage_health_sagt_nicht_eingerichtet(lage_plugin):
    h = lage_plugin.health()
    assert h["ok"] is True and h["status"] == "nicht eingerichtet" and h["einrichten"]


@pytest.mark.parametrize("werkzeug", ["ampel", "fristen", "metrik", "nutzung", "anwesenheit", "chronik", "suche"])
def test_lage_lesend_ohne_einstellung_wirft_nicht(lage_plugin, werkzeug):
    antwort = lage_plugin.call(werkzeug, {"q": "abc", "key": "req_gesamt"})
    assert isinstance(antwort, dict)
    # entweder der eigene Zustand oder eine Antwort, in der jeder Teil als „nicht eingerichtet“ benannt ist
    text = repr(antwort)
    assert "nicht eingerichtet" in text or "nicht_eingerichtet" in text, antwort


def test_lage_ampel_nennt_die_fehlenden_quellen(lage_plugin):
    a = lage_plugin.call("ampel", {})
    assert a["nicht_eingerichtet"], a
    assert a["zustand"] != "gruen"  # ohne Quellen nie grün geraten


def test_lage_fenster_setzen_ohne_klon_ist_abgelehnt(lage_plugin, monkeypatch):
    monkeypatch.setenv("FYOS_FENSTER_KNOTEN", "alpha,beta")
    knoten = lage.fenster_knoten()[0]
    assert abgelehnt(lage_plugin.call("fenster_setzen", {"knoten": knoten, "grund": "messen"}))


def test_lage_fenster_setzen_falsche_eingabe_bleibt_value_error(lage_plugin, monkeypatch):
    monkeypatch.setenv("FYOS_FENSTER_KNOTEN", "alpha,beta")
    with pytest.raises(ValueError):
        lage_plugin.call("fenster_setzen", {"knoten": "gibt-es-nicht", "grund": "x"})


def test_fristen_zertifikat_ohne_hosts_ist_nicht_eingerichtet(lage_plugin):
    f = lage_plugin.call("fristen", {})
    assert "nicht eingerichtet" in repr(f)


# ── betrieb ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "werkzeug,args",
    [
        ("baukette", {}),
        ("ci_laeufe", {}),
        ("ci_fehler", {"sha": "a" * 40}),
        ("notfallwege", {}),
    ],
)
def test_betrieb_ohne_einstellung(leer, werkzeug, args):
    assert nicht_eingerichtet(betrieb.BetriebPlugin().call(werkzeug, args))


def test_betrieb_prozesskarte_ohne_katalog_ist_nicht_eingerichtet(leer):
    assert nicht_eingerichtet(betrieb.BetriebPlugin().call("prozesskarte", {}))


def test_betrieb_werkzeuge_ohne_katalog_ist_nicht_eingerichtet(leer):
    assert nicht_eingerichtet(betrieb.BetriebPlugin().call("werkzeuge", {}))


# ── arbeit ───────────────────────────────────────────────────────────────────
def test_arbeit_lesend_ohne_einstellung(leer):
    p = arbeit.ArbeitPlugin()
    assert nicht_eingerichtet(p.call("kandidaten", {}))
    assert nicht_eingerichtet(p.call("diff", {"zweig": "agent/x"}))
    assert nicht_eingerichtet(p.call("ergebnis", {"job": "arbeit-pruefen-abcd1234"}))


def test_arbeit_pruefen_ohne_einstellung_ist_abgelehnt(leer):
    r = arbeit.ArbeitPlugin().call("pruefen", {"zweig": "agent/x", "nodeid": "tests/test_x.py"})
    assert abgelehnt(r)


def test_arbeit_health(leer):
    assert arbeit.ArbeitPlugin().health()["status"] == "nicht eingerichtet"


# ── grow, pipelines, repos ───────────────────────────────────────────────────
def test_grow_und_pipelines_ohne_adresse(leer):
    assert nicht_eingerichtet(grow.GrowPlugin().call("zustand", {}))
    assert nicht_eingerichtet(pipelines.PipelinesPlugin().call("zustand", {}))
    assert grow.GrowPlugin().health()["status"] == "nicht eingerichtet"
    assert pipelines.PipelinesPlugin().health()["status"] == "nicht eingerichtet"


def test_repos_ohne_git_api(leer):
    p = repos.ReposPlugin()
    assert nicht_eingerichtet(p.call("uebersicht", {}))
    assert nicht_eingerichtet(p.call("detail", {"repo": "x"}))
    assert p.health()["status"] == "nicht eingerichtet"


# ── git, fle, Proxys ─────────────────────────────────────────────────────────
def test_git_ohne_klon(leer):
    p = git_unified.GitUnifiedPlugin()
    p.config = {}
    p.register()
    assert nicht_eingerichtet(p.call("status_consensus", {}))
    assert nicht_eingerichtet(p.call("log_unified", {}))
    for schreibend in ("push_all", "tag_broadcast", "sync_repair"):
        assert abgelehnt(p.call(schreibend, {"tag": "t", "message": "m", "target_remote": "origin"}))
    assert p.health()["status"] == "nicht eingerichtet"


def test_git_repo_pfad_aus_der_umgebung(leer, monkeypatch, tmp_path):
    monkeypatch.setenv("FYOS_REPO_PATH", str(tmp_path))
    p = git_unified.GitUnifiedPlugin()
    p.config = {}
    p.register()
    assert p.repo_path == str(tmp_path)


def test_fle_ohne_daten(leer):
    p = fle_apps.FleAppsPlugin()
    assert nicht_eingerichtet(p.call("messungen", {}))
    assert p.health()["status"] == "nicht eingerichtet"


@pytest.mark.parametrize(
    "klasse,lesend,schreibend",
    [
        (OmadaProxyPlugin, ["server_status", "list_devices"], []),
        (TrueNASProxyPlugin, ["pool_status", "server_status"], [("snapshot_create", {"dataset": "tank/x"})]),
        (KerberuzProxyPlugin, ["budget_score"], [("register", {"component": "c", "interests": [], "threshold": 1})]),
    ],
)
def test_proxys_ohne_adresse(leer, klasse, lesend, schreibend):
    p = klasse()
    p.config = {}
    p.register()
    for name in lesend:
        assert nicht_eingerichtet(p.call(name, {}))
    for name, args in schreibend:
        assert abgelehnt(p.call(name, args))
    assert p.health()["status"] == "nicht eingerichtet"


def test_proxy_adresse_aus_der_umgebung(leer, monkeypatch):
    monkeypatch.setenv("FYOS_OMADA_URL", "http://omada.example.org:8011/")
    monkeypatch.setenv("FYOS_TRUENAS_URL", "http://speicher.example.org:8012/")
    monkeypatch.setenv("FYOS_KERBERUZ_URL", "http://daemon.example.org:8020/")
    for klasse, url in (
        (OmadaProxyPlugin, "http://omada.example.org:8011"),
        (TrueNASProxyPlugin, "http://speicher.example.org:8012"),
        (KerberuzProxyPlugin, "http://daemon.example.org:8020"),
    ):
        p = klasse()
        p.config = {}
        p.register()
        assert p.server_url == url
        p2 = klasse()
        p2.config = {"server_url": "http://eigene.example.org"}  # die Konfiguration des Plugins gewinnt
        p2.register()
        assert p2.server_url == "http://eigene.example.org"


# ── cluster ──────────────────────────────────────────────────────────────────
def test_cluster_ohne_messknoten_und_ohne_klon(leer):
    assert cluster.messknoten() == ""
    assert cluster.messspuren() == ()
    assert cluster.nie_absperren() == ()
    assert cluster.messknoten_bezug({"metadata": {"name": "knoten-waechter"}}) == []
    assert cluster.suspend_grund_aus_repo({"metadata": {}}, "") is None
    assert cluster.ClusterPlugin().repo == ""


def test_cluster_einstellungen(monkeypatch):
    monkeypatch.setenv("FYOS_CLUSTER_MESSKNOTEN", "beta")
    monkeypatch.setenv("FYOS_CLUSTER_MESSSPUREN", "192.0.2.12, modell")
    monkeypatch.setenv("FYOS_CLUSTER_NIE_ABSPERREN", "alpha, delta")
    assert cluster.messknoten() == "beta"
    assert cluster.messspuren() == ("beta", "192.0.2.12", "modell")
    assert cluster.nie_absperren() == ("alpha", "delta")
