"""lage — Ampel, Metrik, Nutzung (Ersatz der frühere Routen, 2026-09-30).

Die Quellen (Redis, Gitea, Prometheus) sind durch kleine Attrappen ersetzt; geprueft wird
die Logik: Zustandsregeln, „nie gruen bei Luecke", Tab-Dubletten, Kurvenbau.
"""

from __future__ import annotations

import json
import time
import types

import pytest

from fyos_plugin_betrieb import lage


class FakeRedis:
    def __init__(self, keys=None, ttl=None, streams=None, kaputt=False):
        self.keys = keys or {}
        self._ttl = ttl or {}
        self.streams = streams or {}
        self.kaputt = kaputt

    def _pruef(self):
        if self.kaputt:
            raise ConnectionError("redis weg")

    def scan_iter(self, match="*", count=100):
        self._pruef()
        praefix = match.rstrip("*")
        return [k for k in self.keys if k.startswith(praefix)]

    def get(self, k):
        return self.keys.get(k)

    def ttl(self, k):
        return self._ttl.get(k, -1)

    def xrevrange(self, name, max="+", min="-", count=30):
        self._pruef()
        ms = lambda eid: int(str(eid).split("-")[0])  # noqa: E731
        unten = -1 if min == "-" else ms(min)
        return [e for e in reversed(self.streams.get(name, [])) if ms(e[0]) >= unten][:count]

    def xrange(self, name, min="-", count=1000):
        self._pruef()
        eintraege = self.streams.get(name, [])
        if min.startswith("("):
            ab = min[1:]
            idx = next((i for i, (eid, _) in enumerate(eintraege) if eid == ab), -1)
            eintraege = eintraege[idx + 1 :]
        return eintraege[:count]

    def ping(self):
        self._pruef()
        return True


@pytest.fixture()
def plugin():
    return lage.LagePlugin()


def _gitea(state):
    def fake(pfad, timeout=6):
        if "commits?limit=1" in pfad:
            return [{"sha": "abcdef1234567"}]
        return {"state": state}
    return fake


def test_ampel_gruen_ohne_befund(plugin, monkeypatch):
    monkeypatch.setattr(lage, "_redis", lambda: FakeRedis())
    monkeypatch.setattr(lage, "gitea", _gitea("success"))
    a = plugin.call("ampel", {})
    assert a["zustand"] == "gruen" and a["vollstaendig"] is True


def test_ampel_rot_bei_ci_failure_und_fenster_art(plugin, monkeypatch):
    r = FakeRedis(
        keys={"fyos:wartung:beta": json.dumps({"knoten": "beta", "grund": "[dev] Quant messen"}),
              "fyos:wartung:spur:beta": "x"},
        ttl={"fyos:wartung:beta": 3600},
    )
    monkeypatch.setattr(lage, "_redis", lambda: r)
    monkeypatch.setattr(lage, "gitea", _gitea("success"))
    a = plugin._ampel()
    assert a["zustand"] == "dev"
    assert a["fenster"][0]["art"] == "dev" and a["fenster"][0]["_rest_min"] == 60
    assert len(a["fenster"]) == 1  # die Spur zaehlt nicht
    # die Art steht im Grund, damit das Lagebild „Dev-Fenster“ statt immer „Wartungsfenster“ sagen kann
    assert a["warum"][0] == "Fenster beta (dev): Quant messen, noch 60 min"
    monkeypatch.setattr(lage, "gitea", _gitea("failure"))
    assert plugin._ampel()["zustand"] == "rot"


def test_ampel_nie_gruen_wenn_eine_quelle_fehlt(plugin, monkeypatch):
    monkeypatch.setattr(lage, "_redis", lambda: FakeRedis(kaputt=True))
    monkeypatch.setattr(lage, "gitea", _gitea("success"))
    a = plugin._ampel()
    assert a["zustand"] == "blau" and a["vollstaendig"] is False
    assert any("nicht lesbar" in w for w in a["warum"])


def _eid(vor_s):
    return f"{int((time.time() - vor_s) * 1000)}-0"


def test_ampel_blau_bei_fehlerereignissen(plugin, monkeypatch):
    r = FakeRedis(streams={"fyos:events:errors": [
        (_eid(120), {"severity": "error"}), (_eid(60), {"data": json.dumps({"severity": "info"})}),
        (_eid(30), {"severity": "warn"}),
    ]})
    monkeypatch.setattr(lage, "_redis", lambda: r)
    monkeypatch.setattr(lage, "gitea", _gitea("success"))
    a = plugin._ampel()
    assert a["zustand"] == "blau" and a["warum"][0] == "1 Fehler in der letzten Stunde, dazu 1 Warnungen"


def test_ampel_alte_fehler_und_einzelne_warnungen_lassen_gruen(plugin, monkeypatch):
    # App-Prüfung 06.10.: die Ampel stand dauerhaft auf „30 Fehler-Ereignisse“ (kein Zeitfenster, warn zählte mit).
    alt = [(_eid(7200 + i), {"severity": "error"}) for i in range(30)]
    warn = [(_eid(10 + i), {"severity": "warn"}) for i in range(3)]
    monkeypatch.setattr(lage, "_redis", lambda: FakeRedis(streams={"fyos:events:errors": sorted(alt + warn)}))
    monkeypatch.setattr(lage, "gitea", _gitea("success"))
    a = plugin._ampel()
    assert a["zustand"] == "gruen" and a["warum"] == []


def test_ampel_viele_warnungen_werden_genannt_ohne_farbe(plugin, monkeypatch):
    warn = [(_eid(10 + i), {"severity": "warning"}) for i in range(12)]
    monkeypatch.setattr(lage, "_redis", lambda: FakeRedis(streams={"fyos:events:errors": sorted(warn)}))
    monkeypatch.setattr(lage, "gitea", _gitea("success"))
    a = plugin._ampel()
    assert a["zustand"] == "gruen" and a["warum"] == ["12 Warnungen in der letzten Stunde"]


def test_metrik_baut_24_punkte_aus_einer_bereichsabfrage(plugin, monkeypatch):
    aufrufe = []

    def fake(pfad, params, timeout=8):
        aufrufe.append((pfad, params))
        s, e, st = params["start"], params["end"], params["step"]
        return {"result": [{"values": [[t, str(t % 7)] for t in range(s + st, e + 1, st)]}]}

    monkeypatch.setattr(lage, "prometheus", fake)
    m = plugin.call("metrik", {"key": "gpu_util", "stunden": 24})
    assert len(aufrufe) == 1 and aufrufe[0][0] == "/api/v1/query_range"
    assert len(m["punkte"]) == 24 and m["aktuell"] is not None
    with pytest.raises(ValueError):
        plugin.call("metrik", {"key": "gibts_nicht"})


def test_nutzung_zaehlt_tab_dubletten_einmal(plugin, monkeypatch):
    jetzt = "2099-01-01T10:00"
    stream = [
        ("1-0", {"pane": "home", "start": jetzt, "dauer_ms": "3600000"}),
        ("2-0", {"pane": "home", "start": jetzt, "dauer_ms": "3600000"}),  # zweiter Tab
        ("3-0", {"pane": "finance", "start": "2099-01-01T11:00", "dauer_ms": "1800000"}),
    ]
    monkeypatch.setattr(lage, "_redis", lambda: FakeRedis(streams={"fyos:pane:dwell": stream}))
    n = plugin.call("nutzung", {"tage": 7})
    assert n["gesamt_stunden"] == 1.5
    assert n["flaechen"][0] == {"pane": "home", "stunden": 1.0, "anteil": 66.7, "besuche": 1}
    assert n["spruenge"] == [("home -> finance", 1)]


def test_alle_werkzeuge_sind_flaechen_und_lesend_bis_auf_fenster(plugin):
    for d in plugin.describe_tools():
        assert "flaeche" in d.tags
        assert ("write" if d.name == "fenster_setzen" else "read") in d.tags


# ── Fristen, Anwesenheit (Etappe 2b) ─────────────────────────────────────────
def test_fristen_backup_uhr_misst_statt_fest_ok(plugin, monkeypatch):
    import datetime as dt
    import sys
    import types

    jetzt = dt.datetime.now(dt.timezone.utc)
    def cj(name, stunden, suspend=False):
        return types.SimpleNamespace(
            metadata=types.SimpleNamespace(name=name),
            spec=types.SimpleNamespace(suspend=suspend),
            status=types.SimpleNamespace(last_successful_time=None if stunden is None else jetzt - dt.timedelta(hours=stunden)),
        )
    liste = [cj("pg-dump-gitea", 5), cj("offsite-backup", 50), cj("alt", 100), cj("nie", None), cj("aus", 500, suspend=True)]
    fake_k8s = types.ModuleType("kubernetes")
    fake_k8s.config = types.SimpleNamespace(load_incluster_config=lambda: None)
    fake_k8s.client = types.SimpleNamespace(BatchV1Api=lambda: types.SimpleNamespace(
        list_namespaced_cron_job=lambda ns, label_selector: types.SimpleNamespace(items=liste)))
    monkeypatch.setitem(sys.modules, "kubernetes", fake_k8s)
    uhren = {u["name"]: u["zustand"] for u in plugin._backups()}
    assert uhren == {"pg-dump-gitea": "ok", "offsite-backup": "knapp", "alt": "kritisch", "nie": "kritisch"}


def test_fristen_platten_trend(plugin, monkeypatch):
    def fake(pfad, params, timeout=8):
        alt = "offset 30d" in params["query"]
        return {"result": [{"metric": {"instance": "beta:9100"}, "value": [0, "60" if alt else "90"]}]}
    monkeypatch.setattr(lage, "prometheus", fake)
    u = plugin._platten()[0]
    assert u["zustand"] == "knapp" and "voll in ~10 d" in u["text"]


def test_anwesenheit_nur_im_zeitfenster(plugin, monkeypatch):
    import datetime as dt
    jetzt = dt.datetime.now(dt.timezone.utc)
    commits = [
        {"author": {"login": "claude"}, "commit": {"author": {"date": (jetzt - dt.timedelta(minutes=5)).isoformat()}}},
        {"author": {"login": "codex"}, "commit": {"author": {"date": (jetzt - dt.timedelta(hours=10)).isoformat()}}},
    ]
    monkeypatch.setattr(lage, "gitea", lambda pfad, timeout=6: commits)
    a = plugin.call("anwesenheit", {"stunden": 4})
    assert [h["name"] for h in a["hände"]] == ["claude"] and a["fehler"] == []


# ── Chronik, Suche (Etappe 5) ────────────────────────────────────────────────
def test_chronik_mischt_quellen_und_meldet_ausfall(plugin, monkeypatch):
    import datetime as dt
    jetzt = dt.datetime.now(dt.timezone.utc)
    commits = [{"sha": "a" * 40, "author": {"login": "claude"}, "commit": {"message": "fix: x\nlang", "author": {"date": jetzt.isoformat()}}}]
    def fake_gitea(pfad, timeout=6):
        return commits if "commits?limit" in pfad else {"state": "success"}
    def fake_dienst(name, pfad, timeout=6):
        if name == "einsichten":
            raise ConnectionError("weg")
        return {"agents": [{"name": "graph-rebuild", "last_run": jetzt.isoformat(), "status": "ok", "duration_s": 12}]}
    monkeypatch.setattr(lage, "gitea", fake_gitea)
    monkeypatch.setattr(lage, "dienst", fake_dienst)
    monkeypatch.setattr(lage, "_redis", lambda: FakeRedis(streams={"fyos:events:errors": [("1-0", {"severity": "critical", "ts": str(jetzt.timestamp()), "message": "boom"})]}))
    c = plugin.call("chronik", {"stunden": 2})
    arten = sorted(e["art"] for e in c["ereignisse"])
    assert arten == ["commit", "daemon", "vorfall"]
    assert any(e["detail"] == "CI: success" for e in c["ereignisse"])
    assert c["fehler"] and c["fehler"][0].startswith("Insights")


def test_suche_meldet_quellausfall_statt_stumm_leer(plugin, monkeypatch):
    def fake_dienst(name, pfad, timeout=6):
        if name == "aufgaben":
            raise TimeoutError()
        return []
    monkeypatch.setattr(lage, "dienst", fake_dienst)
    tickets = [{"id": "CHG-3272", "title": "Modell Quant", "status": "open"}]
    monkeypatch.setattr(lage.vorgaenge, "quelle", lambda: types.SimpleNamespace(liste=lambda **k: tickets))
    monkeypatch.setenv("FYOS_VORGANG_LINK", "/vorgaenge?ref={id}")
    s = plugin.call("suche", {"q": "modell"})
    assert s["treffer"][0]["ziel"] == "/vorgaenge?ref=CHG-3272"
    assert s["fehler"] == ["Aufgaben: TimeoutError"]
    assert plugin.call("suche", {"q": "x"})["hinweis"]


class _Workboard:
    """Ablage in der Form des Hub-Plugins openclaw (Grundstufe ohne Ticketdienst)."""

    def ablage_liste(self, status="", grenze=50):
        return [{"id": "WB-1a2b3c4d", "title": "Modell neu starten", "status": "in_progress"}]


@pytest.fixture()
def ohne_ticketdienst(monkeypatch):
    monkeypatch.delenv("FYOS_VORGAENGE_URL", raising=False)
    monkeypatch.delenv("FYOS_VORGANG_LINK", raising=False)
    monkeypatch.setattr(lage, "dienst", lambda name, pfad, timeout=6: [])
    lage.vorgaenge.dienst_anmelden(None)
    yield
    lage.vorgaenge.ablage_anmelden(None)


def test_suche_findet_vorgaenge_im_workboard(plugin, ohne_ticketdienst):
    lage.vorgaenge.ablage_anmelden(_Workboard())
    s = plugin.call("suche", {"q": "modell"})
    assert s["treffer"] == [{"typ": "ticket", "titel": "WB-1a2b3c4d Modell neu starten", "ziel": "", "sub": "in_progress"}]
    assert s["fehler"] == []


def test_suche_ohne_quelle_fuer_vorgaenge_ist_kein_fehler(plugin, ohne_ticketdienst):
    s = plugin.call("suche", {"q": "modell"})
    assert s["treffer"] == [] and s["fehler"] == []


def test_platten_offset_steht_am_selektor(plugin, monkeypatch):
    abfragen = []
    def fake(pfad, params, timeout=8):
        abfragen.append(params["query"])
        return {"result": []}
    monkeypatch.setattr(lage, "prometheus", fake)
    plugin._platten()
    alt = abfragen[1]
    assert alt.count("offset 30d") == 2 and not alt.rstrip().endswith(") offset 30d")


def test_dienst_agent_schickt_api_token(monkeypatch):
    gesehen = {}
    class Antwort:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b"{}"
    def fake_urlopen(req, timeout=6):
        gesehen.update(dict(req.header_items()))
        return Antwort()
    monkeypatch.setenv("FYOS_AGENTDIENST_TOKEN", "t0k")
    monkeypatch.setattr(lage.urllib.request, "urlopen", fake_urlopen)
    lage.dienst("agent", "/nexus/agents")
    assert gesehen.get("Authorization") == "Bearer t0k"



# ── Fenster setzen (Etappe 6) ────────────────────────────────────────────────
def test_fenster_argv_prueft_und_uebersetzt():
    a = lage.fenster_argv({"knoten": "beta", "grund": "Quant messen", "art": "dev", "minuten": 90}, "/r/f.py")
    assert a[:4] == ["python3", "/r/f.py", "an", "beta"]
    assert a[a.index("--minuten") + 1] == "90" and a[a.index("--art") + 1] == "dev"
    b = lage.fenster_argv({"grund": "x", "bis": "2026-10-01T06:00"}, "/r/f.py")
    assert b[b.index("--bis") + 1] == "2026-10-01T06:00" and "--minuten" not in b
    assert lage.fenster_argv({"knoten": "beta", "aus": True}, "/r/f.py") == ["python3", "/r/f.py", "aus", "beta"]
    for falsch in ({"knoten": "beta; rm -rf /", "grund": "x"}, {"grund": ""}, {"grund": "x", "minuten": 10081},
                   {"grund": "x", "art": "urlaub"}, {"grund": "x", "bis": "--help"}):
        with pytest.raises(ValueError):
            lage.fenster_argv(falsch, "/r/f.py")


def test_fenster_setzen_meldet_fehlschlag_und_leert_ampel(plugin, monkeypatch, tmp_path):
    (tmp_path / "scripts" / "ops").mkdir(parents=True)
    (tmp_path / "scripts" / "ops" / "fenster.py").write_text("")
    monkeypatch.setenv("FYOS_REPO_PATH", str(tmp_path))
    plugin._cache._d["ampel"] = (time.monotonic(), {"zustand": "gruen"})
    monkeypatch.setattr(lage.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=0, stdout="Fenster offen", stderr=""))
    r = plugin.call("fenster_setzen", {"knoten": "beta", "grund": "messen"})
    assert r["aktion"] == "an" and "Fenster offen" in r["ausgabe"] and "ampel" not in plugin._cache._d
    monkeypatch.setattr(lage.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stdout="", stderr="kaputt"))
    with pytest.raises(RuntimeError, match="rc=1"):
        plugin.call("fenster_setzen", {"knoten": "beta", "aus": True})


# ── Fenster-Sperre (gemeinsam fuer Steuerpult und Modelle) ────────────────────────────────
def test_sperrgrund_dev_fenster_sperrt_mit_ende_und_grund(monkeypatch):
    r = FakeRedis(
        keys={"fyos:wartung:beta": json.dumps({"knoten": "beta", "grund": "[dev] Quant messen", "von": "betreiber"})},
        ttl={"fyos:wartung:beta": 7200},
    )
    monkeypatch.setattr(lage, "_redis", lambda: r)
    f = lage.fenster_offen("beta")
    assert f["art"] == "dev" and f["grund"] == "Quant messen" and f["rest_min"] == 120 and f["bis"]
    satz = lage.sperrgrund("beta")
    assert satz.startswith("Dev-Fenster auf beta bis ") and satz.endswith("Quant messen")
    assert lage.sperrgrund("alpha") is None  # anderer Knoten frei


def test_sperrgrund_wartungsfenster_sperrt_nicht(monkeypatch):
    r = FakeRedis(keys={"fyos:wartung:delta": json.dumps({"grund": "[wartung] Platte tauschen"})}, ttl={"fyos:wartung:delta": 600})
    monkeypatch.setattr(lage, "_redis", lambda: r)
    assert lage.fenster_offen("delta")["art"] == "wartung"
    assert lage.sperrgrund("delta") is None


def test_sperrgrund_sperrt_wenn_redis_nicht_lesbar(monkeypatch):
    class Kaputt(FakeRedis):
        def get(self, k):
            raise ConnectionError("redis weg")

    monkeypatch.setattr(lage, "_redis", lambda: Kaputt())
    assert "nicht lesbar" in lage.sperrgrund("beta")
    assert lage.sperrgrund(None) is None
