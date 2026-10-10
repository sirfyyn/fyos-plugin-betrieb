"""betrieb — prozesskarte, betroffen, werkzeuge, notfallwege (Etappe 4, 2026-09-30).

Ersatz der früheren Routen /api/fyos/{prozesskarte,betroffen,werkzeuge,wiki-wege}. Cluster,
Wiki und Registry sind durch kleine Attrappen ersetzt; geprueft werden Datenform (so wie
fyos-web sie liest), Quellen-Rueckfall und ehrliche Luecken.
"""

from __future__ import annotations

import json
import pathlib
from types import SimpleNamespace

import pytest
import yaml

from fyos_plugin_betrieb import betrieb

_WURZEL = pathlib.Path(__file__).resolve().parents[2]


# ── Lanes-Datei ──────────────────────────────────────────────────────────────
def test_lanes_vorlage_ist_in_sich_stimmig():
    d = yaml.safe_load(betrieb.LANES_BEISPIEL.read_text(encoding="utf-8"))
    assert [lane["id"] for lane in d["lanes"]] == ["backup", "waechter", "cicd"]
    for lane in d["lanes"]:
        spalten = {n for c in lane["columns"] for n in c}
        for a, b in lane["edges"]:
            assert a in spalten and b in spalten, f"{lane['id']}: Kante {a}->{b} ohne Knoten"
        for n in spalten:
            if n.startswith("_"):
                assert n in d["synth"], f"synthetischer Knoten {n} ohne Eintrag"


def test_lanes_vorlage_liegt_im_paket(monkeypatch):
    """Die Vorlage reist als package-data mit; ohne eigene Datei gilt sie."""
    assert betrieb.LANES_BEISPIEL.is_file()
    assert betrieb.LANES_BEISPIEL.is_relative_to(pathlib.Path(betrieb.__file__).resolve().parent)
    assert betrieb.LANES_DATEI == betrieb.LANES_BEISPIEL  # früherer Name
    monkeypatch.delenv("FYOS_PROZESSKARTE_LANES", raising=False)
    assert betrieb.lanes_datei() == betrieb.LANES_BEISPIEL


def test_eigene_lanes_datei_gewinnt(monkeypatch, tmp_path):
    eigene = tmp_path / "ketten.yaml"
    eigene.write_text("lanes: []\nsynth: {}\nalias: {}\n", encoding="utf-8")
    monkeypatch.setenv("FYOS_PROZESSKARTE_LANES", str(eigene))
    assert betrieb.lanes_datei() == eigene


@pytest.mark.skip(reason="braucht einen Automatismen-Katalog (docs/automatismen-katalog.yaml) im Repo-Klon")
def test_echte_karte_aus_repo_katalog():
    """Echter Katalog + echte Lanes: fast alle Kartenknoten finden ihren Eintrag."""
    kat = yaml.safe_load((_WURZEL / "docs/automatismen-katalog.yaml").read_text(encoding="utf-8"))
    karte = betrieb.prozesskarte_bauen(kat, betrieb._lanes())
    assert karte["anzahl_katalog"] == len(kat["automatismen"])
    assert len(karte["lanes"]) >= 8
    assert len(karte["nodes"]) > 80
    assert karte["nodes"]["_meldung"]["synth"] is True
    # Kurzname -> Katalog-Suffix (.timer/.service) und Alias greifen
    assert all("w" in n for n in karte["nodes"].values() if not n["synth"])


# ── Prozesskarte / betroffen ─────────────────────────────────────────────────
_KAT = {
    "aktualisiert": "2026-09-30",
    "automatismen": {
        "pg-dump-gitea": {
            "beschreibung": "Gitea-DB dumpen",
            "kategorie": "backup",
            "wann": "taeglich 02:00",
        },
        "backup-pruefer": {"beschreibung": "Backups pruefen", "kategorie": "backup"},
        "backup-rueckholprobe.timer": {"beschreibung": "Restore testen", "kategorie": "backup"},
        "crowdsec.service + crowdsec-hubupdate.timer": {
            "beschreibung": "IPS",
            "kategorie": "sicherheit",
        },
        "verwaist": {"beschreibung": "ohne Platz", "kategorie": "ops"},
    },
}
_LANES = {
    "lanes": [
        {
            "id": "backup",
            "titel": "Backup-Pyramide",
            "sub": "Nacht",
            "kategorie": "backup",
            "columns": [
                ["pg-dump-gitea"],
                ["backup-pruefer"],
                ["backup-rueckholprobe", "_meldung"],
            ],
            "edges": [
                ["pg-dump-gitea", "backup-pruefer"],
                ["backup-pruefer", "backup-rueckholprobe"],
                ["backup-pruefer", "_meldung"],
            ],
        },
        {
            "id": "sicherheit",
            "titel": "Sicherheit",
            "sub": "",
            "kategorie": "sicherheit",
            "columns": [["crowdsec"]],
            "edges": [],
        },
        {
            "id": "leer",
            "titel": "Leer",
            "sub": "",
            "kategorie": "ops",
            "columns": [["gibt-es-nicht"]],
            "edges": [],
        },
    ],
    "synth": {"_meldung": {"name": "Meldung", "kurz": "ITSM", "hinweis": "Senke"}},
    "alias": {"crowdsec": "crowdsec.service + crowdsec-hubupdate.timer"},
}


def test_prozesskarte_form_wie_fyos_web_sie_liest(monkeypatch):
    monkeypatch.setattr(
        betrieb, "_katalog", lambda cm, datei: (_KAT, "ConfigMap fyos/automatik-kontext")
    )
    monkeypatch.setattr(betrieb, "_lanes", lambda: _LANES)
    k = betrieb.BetriebPlugin().call("prozesskarte", {})
    assert k["stand"] == "2026-09-30" and k["anzahl_katalog"] == 5
    assert [lane["id"] for lane in k["lanes"]] == ["backup", "sicherheit"]  # leere Lane faellt weg
    assert (
        k["nodes"]["backup-rueckholprobe"]["w"]["beschreibung"] == "Restore testen"
    )  # .timer-Suffix
    assert k["nodes"]["crowdsec"]["kategorie"] == "sicherheit"  # Alias
    assert k["nodes"]["_meldung"] == {
        "name": "Meldung",
        "kurz": "ITSM",
        "hinweis": "Senke",
        "synth": True,
        "kategorie": "backup",
        "lane": "backup",
        "schedule": {"kind": "sink"},
    }
    assert "gibt-es-nicht" not in k["nodes"]
    assert k["nicht_zugeordnet"] == ["verwaist"]
    assert k["quelle"].startswith("ConfigMap")


def test_katalog_faellt_auf_repo_klon_zurueck(monkeypatch, tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/automatismen-katalog.yaml").write_text(yaml.safe_dump(_KAT))
    monkeypatch.setattr(betrieb, "REPO_KLON", tmp_path)

    def cm_weg(name, schluessel="katalog.yaml"):
        raise PermissionError("forbidden")

    monkeypatch.setattr(betrieb, "_configmap_yaml", cm_weg)
    kat, quelle = betrieb._katalog("automatik-kontext", "docs/automatismen-katalog.yaml")
    assert kat["aktualisiert"] == "2026-09-30" and quelle.startswith("Repo-Klon")


def test_katalog_ohne_quelle_wirft_mit_beiden_gruenden(monkeypatch, tmp_path):
    monkeypatch.setattr(betrieb, "REPO_KLON", tmp_path)
    monkeypatch.setattr(
        betrieb, "_configmap_yaml", lambda *a, **k: (_ for _ in ()).throw(PermissionError("x"))
    )
    with pytest.raises(
        RuntimeError, match="ConfigMap automatik-kontext.*automatismen-katalog.yaml"
    ):
        betrieb._katalog("automatik-kontext", "docs/automatismen-katalog.yaml")


def test_betroffen_automatik(monkeypatch):
    monkeypatch.setattr(betrieb, "_katalog", lambda cm, datei: (_KAT, "ConfigMap"))
    monkeypatch.setattr(betrieb, "_lanes", lambda: _LANES)
    p = betrieb.BetriebPlugin()
    r = p.call("betroffen", {"von": "pg-dump-gitea"})
    assert r["von"] == ["pg-dump-gitea"] and r["ebene"] == "automatik"
    assert {x["name"] for x in r["abhaengig"]} == {
        "backup-pruefer",
        "backup-rueckholprobe",
        "_meldung",
    }
    assert next(x for x in r["abhaengig"] if x["name"] == "backup-pruefer")["ueber"] == [
        "pg-dump-gitea"
    ]
    assert r["lanes_betroffen"] == ["Backup-Pyramide"]
    with pytest.raises(ValueError, match="nicht in der Prozesskarte"):
        p.call("betroffen", {"von": "zzz"})
    with pytest.raises(ValueError, match="erforderlich"):
        p.call("betroffen", {"von": "  "})
    with pytest.raises(ValueError, match="ebene"):
        p.call("betroffen", {"von": "pg", "ebene": "netz"})


class _Obj:
    """Antwort der Kubernetes-Listen; sanitize_for_serialization liefert das JSON."""

    def __init__(self, daten):
        self.daten = daten


def _fake_k8s(nodes, deploys, cronjobs=None, pods=None, services=()):
    core = SimpleNamespace(
        list_node=lambda: _Obj(nodes),
        list_namespaced_pod=lambda ns: _Obj(pods or {"items": []}),
        list_namespaced_service=lambda ns: SimpleNamespace(
            items=[SimpleNamespace(metadata=SimpleNamespace(name=s)) for s in services]
        ),
        read_namespaced_config_map=None,
    )
    apps = SimpleNamespace(list_namespaced_deployment=lambda ns: _Obj(deploys))
    batch = SimpleNamespace(list_namespaced_cron_job=lambda ns: _Obj(cronjobs or {"items": []}))
    api = SimpleNamespace(sanitize_for_serialization=lambda o: o.daten)
    return lambda: (core, apps, batch, api)


def test_betroffen_infra_ueber_kubernetes_api(monkeypatch):
    nodes = {
        "items": [
            {
                "metadata": {"name": "beta", "labels": {"fyos-tier": "t1-gpu"}},
                "status": {"conditions": [{"type": "Ready", "status": "False"}]},
            },
            {
                "metadata": {"name": "alpha", "labels": {"fyos-tier": "t2-state"}},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
        ]
    }
    deploys = {
        "items": [
            {
                "metadata": {"name": "gitea-act-runner"},
                "spec": {
                    "template": {"spec": {"nodeSelector": {"kubernetes.io/hostname": "beta"}}}
                },
            },
        ]
    }
    monkeypatch.setattr(betrieb, "_k8s", _fake_k8s(nodes, deploys))
    r = betrieb.BetriebPlugin().call("betroffen", {"von": "beta", "ebene": "infra"})
    assert r["ebene"] == "infra" and r["ist_node"] is True and r["node_ready"] is False
    assert [w["name"] for w in r["gepinnte_workloads"]] == ["gitea-act-runner"]


# ── Werkzeug-Register ────────────────────────────────────────────────────────
def test_repo_ziel_aus_wo_text(monkeypatch):
    monkeypatch.setenv("FYOS_BETRIEB_PFADPRAEFIXE", "/home/beispiel/projekt/")
    z = betrieb._repo_ziel
    assert z("macbook · scripts/ops/anweisungen_pruefen.py") == "scripts/ops/anweisungen_pruefen.py"
    assert (
        z("beta · scripts/ops/fle/ernte.py, liest /mnt/perf/x.log") == "scripts/ops/fle/ernte.py"
    )
    assert z("scripts/fle/iq4nl_avx512_probe.c (Probe) · x") == "scripts/fle/iq4nl_avx512_probe.c"
    assert z("/home/beispiel/projekt/.claude/skills/code-review") == ".claude/skills/code-review"
    assert z("/pfad/zum/repo-klon/scripts/x.py") == "scripts/x.py"  # der Repo-Pfad des Hubs zählt auch
    monkeypatch.delenv("FYOS_BETRIEB_PFADPRAEFIXE")
    assert z("/home/beispiel/projekt/scripts/x.py") is None  # ein fremder absoluter Pfad ist kein Repo-Ziel
    assert z("macbook · ~/.claude/hooks/zeitanker.sh, verdrahtet") is None
    assert z("k8s ns fyos") is None and z("hermes mcp config") is None and z("") is None


def test_werkzeug_status_regeln(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/da.py").write_text("")
    s = betrieb.werkzeug_status
    d, h = {"agent-admin"}, {"lage.ampel"}
    assert s("x", {"kategorie": "Ops-Skript (archiviert)"}, d, h, tmp_path)[0] == "SUPERSEDED"
    assert s("agent-admin", {"kategorie": "Domain-Daemon"}, d, h, tmp_path)[0] == "LEBT"
    assert s("agent-weg", {"kategorie": "Domain-Daemon"}, d, h, tmp_path)[0] == "TOT"
    assert s("agent-weg", {"kategorie": "Domain-Daemon"}, None, h, tmp_path) == (
        "?",
        "Service-Liste nicht lesbar",
    )
    assert s("lage.ampel", {"kategorie": "MCP-Hub Tool"}, d, h, tmp_path)[0] == "LEBT"
    assert s("altes.werkzeug", {"kategorie": "MCP-Hub Tool"}, d, h, tmp_path)[0] == "TOT"
    assert s("altes.werkzeug", {"kategorie": "MCP-Hub Tool"}, d, None, tmp_path)[0] == "?"
    assert (
        s("da", {"kategorie": "Ops-Skript", "wo": "beta · scripts/da.py"}, d, h, tmp_path)[0]
        == "LEBT"
    )
    assert s("weg", {"kategorie": "Skill", "wo": "scripts/weg.py"}, d, h, tmp_path) == (
        "TOT",
        "Ziel fehlt im Repo: scripts/weg.py",
    )
    assert s("weg", {"kategorie": "Skill", "wo": "scripts/weg.py"}, d, h, None)[0] == "?"
    assert (
        s("hook", {"kategorie": "Hook (Claude)", "wo": "~/.claude/hooks/h.sh"}, d, h, None)[0]
        == "LEBT"
    )


_WKAT = {
    "aktualisiert": "2026-08-26",
    "werkzeuge": {
        "betrieb.werkzeuge": {"kategorie": "MCP-Hub Tool", "bereich": "Cluster", "service": "Hub"},
        "weg.werkzeug": {"kategorie": "MCP-Hub Tool", "bereich": "Cluster", "service": "Hub"},
        "agent-admin": {"kategorie": "Domain-Daemon", "bereich": "Daemons", "service": "Admin"},
        "da.py": {"kategorie": "Ops-Skript", "bereich": "Cluster", "wo": "scripts/da.py"},
    },
}


def test_werkzeuge_form_und_filter(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/da.py").write_text("")
    monkeypatch.setattr(betrieb, "REPO_KLON", tmp_path)
    monkeypatch.setattr(
        betrieb, "_katalog", lambda cm, datei: (_WKAT, "ConfigMap fyos/werkzeug-katalog")
    )
    monkeypatch.setattr(betrieb, "_k8s", _fake_k8s({}, {}, services=["agent-admin"]))
    p = betrieb.BetriebPlugin()
    p.registry_anbinden(
        SimpleNamespace(tools={"betrieb.werkzeuge": SimpleNamespace(fqtn="betrieb.werkzeuge")})
    )
    r = p.call("werkzeuge", {})
    for feld in ("stand", "live_stand", "anzahl", "verteilung", "eintraege", "bereiche"):
        assert feld in r  # Vertrag aus test_werkzeug_register.py 
    assert r["anzahl"] == 4 and r["verteilung"] == {"LEBT": 3, "TOT": 1}
    assert r["bereiche"] == ["Cluster", "Daemons"]
    tot = next(e for e in r["eintraege"] if e["alive"] == "TOT")
    assert tot["name"] == "weg.werkzeug" and tot["alive_detail"] == "nicht im Hub geladen"
    f = p.call("werkzeuge", {"bereich": "Cluster", "status": "LEBT"})
    assert [e["name"] for e in f["eintraege"]] == ["betrieb.werkzeuge", "da.py"]
    assert (
        f["anzahl"] == 2 and f["verteilung"] == r["verteilung"] and f["filter"]["status"] == "LEBT"
    )


def test_werkzeuge_ohne_registry_und_cluster_sagen_fragezeichen(monkeypatch, tmp_path):
    monkeypatch.setattr(betrieb, "REPO_KLON", tmp_path)  # ohne .git
    monkeypatch.setattr(betrieb, "_katalog", lambda cm, datei: (_WKAT, "Repo-Klon"))

    def kein_cluster():
        raise RuntimeError("kein ServiceAccount")

    monkeypatch.setattr(betrieb, "_k8s", kein_cluster)
    r = betrieb.BetriebPlugin().call("werkzeuge", {})
    assert r["verteilung"] == {"?": 4}
    assert r["quellen"]["hub"].startswith("nicht lesbar") and r["quellen"]["services"].startswith(
        "nicht lesbar"
    )


def test_server_bindet_registry_an_betrieb():
    """build_app reicht die Registry an Plugins mit registry_anbinden (hier: betrieb)."""
    import inspect

    from mcp_hub import server

    quelle = inspect.getsource(server.build_app)
    assert "registry_anbinden" in quelle


# ── Notfallwege ──────────────────────────────────────────────────────────────
_SEITE_MD = """# Der Agent antwortet nicht

**Symptom:** Chat laedt endlos.

## Diagnose — laeuft das Gehirn?

```
kubectl get pods -n fyos
```

Lies die letzte Spalte.

## Fix 1

```bash
kubectl scale deploy llama --replicas=1
```
"""

_SEITE_BLOCK = json.dumps(
    [
        {"id": "a", "type": "heading", "level": 1, "text": "Speicher voll"},
        {"id": "b", "type": "paragraph", "text": "**Symptom:** Alles wird langsam"},
        {"id": "c", "type": "heading", "level": 2, "text": "Diagnose"},
        {"id": "d", "type": "code", "text": "df -h"},
        {"id": "e", "type": "paragraph", "text": "Mehr als 90 % ist knapp."},
    ]
)


def _fake_wiki(seiten):
    def get(pfad, timeout=8):
        if pfad == "/pages":
            return {"items": [{k: v for k, v in s.items() if k != "content"} for s in seiten]}
        pid = int(pfad.rsplit("/", 1)[1])
        return next(s for s in seiten if s["id"] == pid)

    return get


def test_notfallwege_aus_wiki_api(monkeypatch):
    seiten = [
        {"id": 7, "parentId": None, "title": "Service-Maintenance", "position": 1},
        {
            "id": 90,
            "parentId": None,
            "title": "🚨 Notfall — START HIER",
            "position": 0,
            "content": "# Index",
        },
        {
            "id": 96,
            "parentId": 90,
            "title": "Speicher voll",
            "position": 2,
            "content": _SEITE_BLOCK,
            "updatedAt": "2026-06-12T16:22:35Z",
        },
        {
            "id": 91,
            "parentId": 90,
            "title": "Der Agent antwortet nicht",
            "position": 1,
            "content": _SEITE_MD,
            "updatedAt": "2026-06-12T16:22:35Z",
        },
        {"id": 200, "parentId": 7, "title": "Anderes", "position": 0, "content": "## x"},
    ]
    monkeypatch.setattr(betrieb, "_wiki_get", _fake_wiki(seiten))
    r = betrieb.BetriebPlugin().call("notfallwege", {})
    assert r["anzahl"] == 2 and r["kommandos"] == 3
    assert [w["id"] for w in r["wege"]] == [91, 96]
    md, block = r["wege"]
    assert md["symptom"] == "Chat laedt endlos." and md["aktualisiert"] == "2026-06-12T16:22:35Z"
    assert [s["titel"] for s in md["schritte"]] == ["Diagnose — laeuft das Gehirn?", "Fix 1"]
    assert md["schritte"][0]["befehle"] == ["kubectl get pods -n fyos\n"]
    assert md["schritte"][0]["text"] == "Lies die letzte Spalte."
    assert block["symptom"] == "Alles wird langsam"
    assert block["schritte"] == [
        {"titel": "Diagnose", "befehle": ["df -h\n"], "text": "Mehr als 90 % ist knapp."}
    ]
    assert "Wurzel 90" in r["quelle"]


def test_notfallwege_ohne_wurzel_wirft(monkeypatch):
    monkeypatch.setattr(
        betrieb, "_wiki_get", _fake_wiki([{"id": 1, "parentId": None, "title": "Start"}])
    )
    with pytest.raises(LookupError, match="keine Wurzelseite"):
        betrieb.BetriebPlugin().call("notfallwege", {})


def test_werkzeug_beschreibungen_tragen_scope_und_flaeche():
    tools = {t.name: t for t in betrieb.BetriebPlugin().describe_tools()}
    for name in ("prozesskarte", "betroffen", "werkzeuge", "notfallwege"):
        assert tools[name].tags == ["betrieb", "read", "flaeche"]
    assert tools["betroffen"].input_schema["required"] == ["von"]


# ── aus dem Hauptstrang: baukette/ci_laeufe ─────────────────────────────────
def test_betrieb_baukette_liest_state_und_beachtet_sha(monkeypatch):
    from fyos_plugin_betrieb import betrieb

    pfade = []

    def fake(pfad, timeout=6):
        pfade.append(pfad)
        if "commits?limit=1" in pfad:
            return [{"sha": "head000"}]
        return {"state": "failure", "statuses": [{"context": "build", "status": "failure"}]}

    monkeypatch.setattr(betrieb, "gitea", fake)
    p = betrieb.BetriebPlugin()
    k = p.call("baukette", {})
    assert (
        k["commit"] == "head000"
        and k["ci"]["status"] == "failure"
        and k["ci"]["jobs"][0]["name"] == "build"
    )
    assert (
        p.call("baukette", {"sha": "abc123"})["commit"] == "abc123"
    )  # eigener Cache-Eintrag je sha


def test_betrieb_ci_laeufe_zaehlt_warteschlange(monkeypatch):
    from fyos_plugin_betrieb import betrieb

    def fake(pfad, timeout=6):
        if "actions/runs" in pfad:
            return {
                "workflow_runs": [
                    {"status": "queued"},
                    {"status": "in_progress"},
                    {"status": "completed", "conclusion": "failure"},
                ]
            }
        return []

    monkeypatch.setattr(betrieb, "gitea", fake)
    r = betrieb.BetriebPlugin().call("ci_laeufe", {})
    assert r["queue"] == {"queued": 1, "running": 1, "failed": 1} and r["source"]["state"] == "live"


def test_betrieb_ci_laeufe_zeitstempel_aus_gitea_feldern(monkeypatch):
    """Audit 2026-10-02: Gitea 1.27 hat started_at/completed_at — created_at/updated_at waren null."""
    from fyos_plugin_betrieb import betrieb

    def fake(pfad, timeout=6):
        if "actions/runs" in pfad:
            return {
                "workflow_runs": [
                    {
                        "status": "completed",
                        "conclusion": "success",
                        "started_at": "2026-10-02T15:58:06+02:00",
                        "completed_at": "2026-10-02T15:58:30+02:00",
                    },
                    {
                        "status": "queued",
                        "started_at": "1970-01-01T01:00:00+01:00",
                        "completed_at": "1970-01-01T01:00:00+01:00",
                    },
                ]
            }
        return []

    monkeypatch.setattr(betrieb, "gitea", fake)
    lauf, wartend = betrieb.BetriebPlugin().call("ci_laeufe", {})["runs"]
    assert lauf["created_at"] == "2026-10-02T15:58:06+02:00"
    assert lauf["updated_at"] == "2026-10-02T15:58:30+02:00"
    assert wartend["created_at"] is None and wartend["updated_at"] is None


def test_werkzeuge_ohne_abgeschaltete_mit_q_und_limit(monkeypatch, tmp_path):
    """Audit 2026-10-02: abgeschaltete Plugins (echo) nicht als TOT; q und limit begrenzen."""
    kat = {
        "aktualisiert": "2026-10-02",
        "werkzeuge": {
            **_WKAT["werkzeuge"],
            "echo.say": {"kategorie": "MCP-Hub Tool", "bereich": "Cluster", "service": "Hub"},
        },
    }
    monkeypatch.setattr(betrieb, "REPO_KLON", tmp_path)
    monkeypatch.setattr(betrieb, "_katalog", lambda cm, datei: (kat, "ConfigMap"))
    monkeypatch.setattr(betrieb, "_k8s", _fake_k8s({}, {}, services=["agent-admin"]))
    p = betrieb.BetriebPlugin()
    p.registry_anbinden(
        SimpleNamespace(
            tools={"betrieb.werkzeuge": SimpleNamespace(fqtn="betrieb.werkzeuge")},
            abgeschaltet={"echo"},
        )
    )
    r = p.call("werkzeuge", {})
    assert "echo.say" not in {e["name"] for e in r["eintraege"]}
    assert "echo" in r["quellen"]["ausgeblendet"]
    q = p.call("werkzeuge", {"q": "WEG"})
    assert [e["name"] for e in q["eintraege"]] == ["weg.werkzeug"]
    eins = p.call("werkzeuge", {"limit": 1})
    assert len(eins["eintraege"]) == 1 and eins["anzahl"] == 4 and eins["abgeschnitten"] is True
