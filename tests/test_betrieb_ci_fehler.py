"""betrieb.ci_fehler — Fehlerstelle aus Job-Logs, Einordnung der Laeufe, Abgleich mit main.

Gitea ist ersetzt (`betrieb.gitea` fuer JSON, `_ci_fehler.gitea_text` fuer Logs); die Log-Ausschnitte
stammen aus dem echten Lauf zu c29ab6c25 (Tests-Baum: LiteLLM-500, Test agent: Frist ueber dem Deckel).
"""

from __future__ import annotations

import urllib.error

import pytest

from fyos_plugin_betrieb import _ci_fehler, betrieb

SHA = "c29ab6c25eba83701fbd60c89f2370539e2edac8"
MAIN_SHA = "93762671e0000000000000000000000000000000"

AUFRAEUMEN = """\
2026-10-08T21:50:20.2159285Z [command]/usr/bin/git config --local --unset-all http.https://git.***.de/.extraheader
2026-10-08T21:50:20.2933923Z   ✅  Success - Post Checkout
2026-10-08T21:50:20.3075048Z Cleaning up container for job baum
2026-10-08T21:50:20.4353573Z Removed container: 7b5d487808e9ce8b68dbe9435b19cc679c763aa7a41990f95f4a95ba6ac344bb
2026-10-08T21:50:20.4365279Z   🐳  docker volume rm GITEA-ACTIONS-TASK-15196-WORKFLOW-Tests-Baum-JOB-baum
2026-10-08T21:50:21.2476152Z 🏁  Job failed
2026-10-08T21:50:21.2539450Z Job 'baum' failed
"""

LOG_BAUM = (
    """\
2026-10-08T21:49:33.1122949Z ::group::Run PYTHONPATH="agent:." python3 -m pytest tests/ -q --tb=short
2026-10-08T21:49:33.1123291Z PYTHONPATH="agent:." python3 -m pytest tests/ -q --tb=short
2026-10-08T21:49:33.1123389Z shell: bash --noprofile --norc -e -o pipefail {0}
2026-10-08T21:49:33.1123470Z ::endgroup::
2026-10-08T21:50:12.4408431Z ...........................................................sss.sF.ssssss [ 13%]
2026-10-08T21:50:19.3632350Z =================================== FAILURES ===================================
2026-10-08T21:50:19.3632491Z ________________ TestLiteLLMHealth.test_litellm_models_endpoint ________________
2026-10-08T21:50:19.3632591Z tests/e2e/test_backend_chain.py:148: in test_litellm_models_endpoint
2026-10-08T21:50:19.3632711Z     assert resp.status_code in (200, 401, 403), f"LiteLLM /v1/models: HTTP {resp.status_code}"
2026-10-08T21:50:19.3632817Z E   AssertionError: LiteLLM /v1/models: HTTP 500
2026-10-08T21:50:19.3632937Z E   assert 500 in (200, 401, 403)
2026-10-08T21:50:19.3633158Z =============================== warnings summary ===============================
2026-10-08T21:50:19.3633552Z   /opt/x/fastapi/testclient.py:1: StarletteDeprecationWarning: Using `httpx` is deprecated
2026-10-08T21:50:19.3634804Z =========================== short test summary info ============================
2026-10-08T21:50:19.3634899Z FAILED tests/e2e/test_backend_chain.py::TestLiteLLMHealth::test_litellm_models_endpoint - AssertionError: LiteLLM /v1/models: HTTP 500
2026-10-08T21:50:19.3635064Z assert 500 in (200, 401, 403)
2026-10-08T21:50:19.3635252Z 1 failed, 508 passed, 19 skipped, 3 warnings, 22 subtests passed in 45.23s
2026-10-08T21:50:19.9210353Z   ❌  Failure - Main pytest tests/
"""
    + AUFRAEUMEN
)

LOG_APP = (
    """\
2026-10-08T21:50:42.8318782Z =================================== FAILURES ===================================
2026-10-08T21:50:42.8318861Z __________________ test_keine_frist_ist_laenger_als_ihr_takt ___________________
2026-10-08T21:50:42.8319013Z app/tests/test_cronjobs.py:181: in test_keine_frist_ist_laenger_als_ihr_takt
2026-10-08T21:50:42.8319086Z E   AssertionError: Diese Fristen sind laenger als der Takt und stehen nicht in AUSNAHMEN. Begruendung ein:
2026-10-08T21:50:42.8319173Z E         modell-upload                 Frist=345600s ueber dem Deckel 7200s  k8s/jobs/modell-upload-cronjob.yaml
2026-10-08T21:50:42.8319258Z E   assert not ['    modell-upload                 Frist=345600s ueber dem Deckel 7200s']
2026-10-08T21:50:42.8319329Z =============================== warnings summary ===============================
2026-10-08T21:50:42.8320053Z ================================== Netzsperre ==================================
2026-10-08T21:50:42.8320111Z 28 Tests wollten 6 gesperrte Ziele erreichen und liefen ohne sie weiter.
2026-10-08T21:50:42.8321630Z =========================== short test summary info ============================
2026-10-08T21:50:42.8321724Z FAILED app/tests/test_cronjobs.py::test_keine_frist_ist_laenger_als_ihr_takt - AssertionError: Diese Fristen sind laenger
2026-10-08T21:50:42.8321835Z       modell-upload                 Frist=345600s ueber dem Deckel 7200s  k8s/jobs/modell-upload-cronjob.yaml
2026-10-08T21:50:42.8322053Z 1 failed, 891 passed, 9 skipped, 53 deselected, 2 warnings in 143.09s (0:02:23)
2026-10-08T21:50:44.0426533Z   ❌  Failure - Main Testsuite
"""
    + AUFRAEUMEN
)

LOG_LINT = (
    """\
2026-10-08T21:50:00.0000000Z ::group::Run ruff check .
2026-10-08T21:50:00.0000001Z ruff check .
2026-10-08T21:50:00.0000002Z shell: bash --noprofile --norc -e -o pipefail {0}
2026-10-08T21:50:00.0000003Z ::endgroup::
2026-10-08T21:50:01.0000000Z mcp_hub/x.py:12:5: F401 `os` imported but unused
2026-10-08T21:50:01.0000001Z Error: Found 1 error.
2026-10-08T21:50:01.0000002Z   ❌  Failure - Main ruff check .
"""
    + AUFRAEUMEN
)

LOG_ENDE = "2026-10-08T21:50:00.0000000Z build gescheitert: kein Platz mehr\n" + AUFRAEUMEN

LOG_APP_MAIN = LOG_APP  # gleicher Test auch auf main rot


# ── Log lesen ───────────────────────────────────────────────────────────────────
def test_pytest_zeigt_fehlerstelle_statt_aufraeumrauschen():
    a = _ci_fehler.log_auswerten(LOG_BAUM)
    assert a["art"] == "pytest" and a["schritt"] == "pytest tests/"
    assert a["tests"] == [
        {
            "id": "tests/e2e/test_backend_chain.py::TestLiteLLMHealth::test_litellm_models_endpoint",
            "ursache": "LiteLLM /v1/models: HTTP 500",
        }
    ]
    text = "\n".join(a["auszug"])
    assert "E   AssertionError: LiteLLM /v1/models: HTTP 500" in text
    assert "short test summary info" in text and "1 failed, 508 passed" in text
    for rauschen in ("Cleaning up", "docker volume rm", "Removed container", "extraheader", "warnings summary"):
        assert rauschen not in text
    assert a["ergebnis_zeile"].startswith("1 failed, 508 passed")


def test_pytest_ursache_steht_in_der_zeile_nach_dem_doppelpunkt():
    a = _ci_fehler.log_auswerten(LOG_APP)
    (t,) = a["tests"]
    assert t["ursache"] == "modell-upload Frist=345600s ueber dem Deckel 7200s k8s/jobs/modell-upload-cronjob.yaml"
    assert not any("Netzsperre" in x for x in a["auszug"])  # fremder Block zwischen den Teilen


def test_pytest_auszug_haelt_das_zeilenbudget_und_laesst_die_zusammenfassung_nicht_fallen():
    a = _ci_fehler.log_auswerten(LOG_APP, zeilen=5)
    assert len(a["auszug"]) == 5
    assert any("short test summary info" in x for x in a["auszug"])  # die Zusammenfassung hat Vorrang
    assert a["auszug"][-1].startswith("1 failed, 891 passed")


def test_anderer_schritt_zeigt_dessen_ausgabe():
    a = _ci_fehler.log_auswerten(LOG_LINT)
    assert a["art"] == "schritt" and a["schritt"] == "ruff check ."
    assert a["auszug"] == ["mcp_hub/x.py:12:5: F401 `os` imported but unused", "Error: Found 1 error."]
    assert a["ursache"] == "Error: Found 1 error." and a["tests"] == []


def test_ohne_schritt_und_pytest_faellt_auf_das_ende_ohne_rauschen_zurueck():
    a = _ci_fehler.log_auswerten(LOG_ENDE)
    assert a["art"] == "ende" and a["auszug"] == ["build gescheitert: kein Platz mehr"]
    assert a["ursache"] == "build gescheitert: kein Platz mehr"


def test_einordnung():
    e = _ci_fehler.einordnen
    assert e("completed", "success") == "grün"
    assert e("completed", "failure") == "fehler"
    assert e("completed", "timed_out") == "fehler"
    assert e("completed", "cancelled") == "abgebrochen"
    assert e("completed", "skipped") == "übersprungen"
    assert e("in_progress", "") == "läuft"
    assert e("queued", None) == "wartet"
    assert e("waiting", None) == "wartet"
    assert e("failure", "") == "fehler"  # Ergebnis im Status
    assert e("", "") == "unklar"


# ── Ablauf mit nachgebautem Gitea ───────────────────────────────────────────────
def _run(rid, pfad, status="completed", ergebnis="success", sha=SHA, zweig="pruefen/x"):
    return {
        "id": rid,
        "path": f"{pfad}@refs/heads/{zweig}",
        "status": status,
        "conclusion": ergebnis,
        "head_sha": sha,
        "head_branch": zweig,
        "html_url": f"https://git.example/runs/{rid}",
    }


class Gitea:
    def __init__(self, runs, main_runs=(), jobs=None, logs=None, commits=None):
        self.runs, self.main_runs = list(runs), list(main_runs)
        self.jobs, self.logs = jobs or {}, logs or {}
        self.commits = commits or {}
        self.aufrufe: list[str] = []

    def json(self, pfad, timeout=6):
        self.aufrufe.append(pfad)
        if "/git/commits/" in pfad:
            kurz = pfad.rsplit("/", 1)[-1]
            return {"sha": SHA, "commit": {"message": self.commits.get(kurz, "Mitteilung")}}
        if "/actions/workflows" in pfad:
            return {
                "workflows": [
                    {"id": "test-tests-baum.yml", "name": "Tests-Baum", "path": ".gitea/workflows/test-tests-baum.yml"},
                    {"id": "tests.yml", "name": "Test agent", "path": ".gitea/workflows/tests.yml"},
                ]
            }
        if "/actions/runs?" in pfad and "head_sha=" in pfad:
            sha = pfad.split("head_sha=")[1].split("&")[0]
            return {"workflow_runs": [r for r in self.runs if r["head_sha"] == sha]}
        if "/actions/runs?" in pfad and "branch=main" in pfad:
            return {"workflow_runs": self.main_runs if "page=1" in pfad else []}
        if "/actions/runs/" in pfad and pfad.endswith("/jobs"):
            rid = int(pfad.split("/runs/")[1].split("/")[0])
            return {"jobs": self.jobs[rid]}
        if "/commits/" in pfad and pfad.endswith("/status"):
            return {"state": "failure"}
        if "/commits?sha=" in pfad:
            return self.commits.get("liste", [{"sha": SHA, "commit": {"message": "Mitteilung"}}])
        raise AssertionError(f"unerwarteter Aufruf {pfad}")

    def text(self, pfad, timeout=20):
        self.aufrufe.append(pfad)
        jid = int(pfad.split("/jobs/")[1].split("/")[0])
        return self.logs[jid]


def _job(jid, name, ergebnis="failure"):
    return {"id": jid, "name": name, "status": "completed", "conclusion": ergebnis,
            "steps": [{"name": "Testsuite", "status": "completed", "conclusion": ergebnis}]}


def _plugin(monkeypatch, g):
    monkeypatch.setattr(betrieb, "gitea", g.json)
    monkeypatch.setattr(_ci_fehler, "gitea_text", g.text)
    return betrieb.BetriebPlugin()


def _standard():
    runs = [
        _run(1, "test-tests-baum.yml", ergebnis="failure"),
        _run(2, "tests.yml", ergebnis="failure"),
        _run(3, "lint-python.yml"),
        _run(4, "lint-docs.yml", ergebnis="cancelled"),
        _run(5, "lint-k8s.yml"),
    ]
    main_runs = [_run(90, "tests.yml", ergebnis="failure", sha=MAIN_SHA, zweig="main")]
    jobs = {1: [_job(11, "baum"), _job(12, "andere", "success")], 2: [_job(21, "pytest")], 90: [_job(91, "pytest")]}
    logs = {11: LOG_BAUM, 21: LOG_APP, 91: LOG_APP_MAIN}
    return Gitea(runs, main_runs, jobs, logs)


def test_ci_fehler_satz_einordnung_und_abgleich_mit_main(monkeypatch):
    r = _plugin(monkeypatch, _standard()).call("ci_fehler", {"sha": "c29ab6c25"})
    assert r["commit"] == SHA and r["zweig"] == "pruefen/x" and r["gesamt"] == "fehler"
    assert r["satz"] == (
        "2 von 5 rot, 1 abgebrochen: "
        "Tests-Baum (1 Test: test_litellm_models_endpoint — LiteLLM /v1/models: HTTP 500), "
        "Test agent (1 Test: test_keine_frist_ist_laenger_als_ihr_takt — "
        "modell-upload Frist=345600s ueber dem Deckel 7200s k8s/jobs/modell-upload-cronjob.yaml). "
        "Schon auf main rot: Test agent."
    )
    assert r["zaehler"] == {"gesamt": 5, "grün": 2, "fehler": 2, "abgebrochen": 1}
    # kurz: nur Auffaelliges, grün als Zahl
    assert {lf["name"] for lf in r["laeufe"]} == {"Tests-Baum", "Test agent", "lint-docs"}
    assert r["gruen"] == 2
    by = {lf["name"]: lf for lf in r["laeufe"]}
    assert by["lint-docs"]["ergebnis"] == "abgebrochen" and by["lint-docs"]["jobs"] == []
    agent = by["Test agent"]
    assert agent["main"]["vergleich"] == "rot" and agent["jobs"][0]["tests"][0]["main"] == "vorbestehend"
    baum = by["Tests-Baum"]
    assert baum["main"]["vergleich"] == "unbekannt"  # Workflow lief auf main nicht
    assert [j["name"] for j in baum["jobs"]] == ["baum"]  # gruener Job bleibt ohne Log


def test_ci_fehler_neuer_test_gegenueber_main(monkeypatch):
    g = _standard()
    g.main_runs = [_run(90, "tests.yml", ergebnis="success", sha=MAIN_SHA, zweig="main")]
    r = _plugin(monkeypatch, g).call("ci_fehler", {"sha": SHA})
    assert r["satz"].endswith("Neu gegenüber main: Test agent.")
    agent = next(lf for lf in r["laeufe"] if lf["name"] == "Test agent")
    assert agent["main"]["vergleich"] == "grün" and agent["jobs"][0]["main"] == "neu"


def test_ci_fehler_auf_main_selbst_kein_vergleich(monkeypatch):
    g = _standard()
    g.runs = [_run(2, "tests.yml", ergebnis="failure", zweig="main")]
    r = _plugin(monkeypatch, g).call("ci_fehler", {"sha": SHA})
    assert "auf main rot" not in r["satz"] and "gegenüber main" not in r["satz"]
    assert "main" not in r["laeufe"][0]  # Schluessel `main` (Vergleich) fehlt
    assert not any("branch=main" in a for a in g.aufrufe)


def test_ci_fehler_voll_zeigt_gruene_und_begrenzt_zeilen(monkeypatch):
    p = _plugin(monkeypatch, _standard())
    voll = p.call("ci_fehler", {"sha": SHA, "voll": True})
    assert {lf["name"] for lf in voll["laeufe"]} >= {"lint-python", "lint-k8s", "Test agent"}
    assert "gruen" not in voll
    knapp = p.call("ci_fehler", {"sha": SHA, "zeilen": 5, "voll": False})
    agent = next(lf for lf in knapp["laeufe"] if lf["name"] == "Test agent")
    assert len(agent["jobs"][0]["auszug"]) == 5


def test_ci_fehler_nur_wiederholung_zaehlt(monkeypatch):
    g = _standard()
    g.runs = [_run(1, "tests.yml", ergebnis="cancelled"), _run(7, "tests.yml")]
    r = _plugin(monkeypatch, g).call("ci_fehler", {"sha": SHA})
    assert r["satz"] == "Alles grün (1 Läufe)." and r["gesamt"] == "grün"


def test_ci_fehler_laeuft_und_wartet(monkeypatch):
    g = _standard()
    g.runs = [_run(1, "tests.yml", status="in_progress", ergebnis=""),
              _run(2, "lint-docs.yml", status="queued", ergebnis=""), _run(3, "lint-k8s.yml")]
    r = _plugin(monkeypatch, g).call("ci_fehler", {"sha": SHA})
    assert r["gesamt"] == "läuft" and r["satz"].startswith("Noch nichts rot")
    assert r["zaehler"]["läuft"] == 1 and r["zaehler"]["wartet"] == 1


def test_ci_fehler_zweig_ohne_ci_commit_geht_zum_letzten_mit_ci(monkeypatch):
    g = _standard()
    spitze = "a" * 40
    g.commits = {
        "liste": [
            {"sha": spitze, "commit": {"message": "Digest nachgefuehrt [skip ci]"}},
            {"sha": SHA, "commit": {"message": "echter Commit"}},
        ]
    }
    # die Spitze hat keine Laeufe, SHA hat welche
    orig = g.json

    def json(pfad, timeout=6):
        if "/commits?sha=" in pfad and "limit=1" in pfad:
            return g.commits["liste"][:1]
        return orig(pfad, timeout)

    monkeypatch.setattr(betrieb, "gitea", json)
    monkeypatch.setattr(_ci_fehler, "gitea_text", g.text)
    r = betrieb.BetriebPlugin().call("ci_fehler", {"zweig": "main"})
    assert r["commit"] == SHA and "[skip ci]" in r["hinweis"] and "letzte Commit mit CI" in r["hinweis"]
    assert r["satz"].startswith("2 von 5 rot")


def test_ci_fehler_skip_ci_ohne_ausweich(monkeypatch):
    g = _standard()
    g.runs = []
    g.commits = {"c29ab6c25": "nur Digest [skip ci]"}
    r = _plugin(monkeypatch, g).call("ci_fehler", {"sha": "c29ab6c25"})
    assert r["gesamt"] == "leer" and "[skip ci]" in r["satz"] and r["laeufe"] == []


def test_ci_fehler_logfehler_ist_nutzdaten(monkeypatch):
    g = _standard()

    def kaputt(pfad, timeout=20):
        raise OSError("Verbindung weg")

    monkeypatch.setattr(betrieb, "gitea", g.json)
    monkeypatch.setattr(_ci_fehler, "gitea_text", kaputt)
    r = betrieb.BetriebPlugin().call("ci_fehler", {"sha": SHA})
    baum = next(lf for lf in r["laeufe"] if lf["name"] == "Tests-Baum")
    assert "OSError" in baum["jobs"][0]["log_fehler"]
    assert "Job baum: OSError" in r["satz"]


def test_ci_fehler_unbekannter_commit_und_gitea_aus(monkeypatch):
    def nein(pfad, timeout=6):
        raise urllib.error.HTTPError("u", 404, "nf", {}, None)

    monkeypatch.setattr(betrieb, "gitea", nein)
    r = betrieb.BetriebPlugin().call("ci_fehler", {"sha": "deadbeef"})
    assert "nicht gefunden" in r["satz"] and "404" in r["fehler"]

    def aus(pfad, timeout=6):
        raise OSError("down")

    monkeypatch.setattr(betrieb, "gitea", aus)
    assert "fehler" in betrieb.BetriebPlugin().call("ci_fehler", {"zweig": "x"})


def test_ci_fehler_eingabe_wird_geprueft():
    p = betrieb.BetriebPlugin()
    with pytest.raises(ValueError, match="Commit-Kennung"):
        p.call("ci_fehler", {"sha": "../etc"})
    with pytest.raises(ValueError, match="zeilen"):
        p.call("ci_fehler", {"sha": SHA, "zeilen": "viele"})
    assert _ci_fehler.eingabe_pruefen({}) == ("", "main", 40, False)
    assert _ci_fehler.eingabe_pruefen({"sha": "ABC1234", "zeilen": 9999, "voll": True}) == ("abc1234", "", 200, True)
    assert _ci_fehler.eingabe_pruefen({"zweig": "a/b", "zeilen": 1})[2] == 5


def test_ci_fehler_beschreibung():
    t = {x.name: x for x in betrieb.BetriebPlugin().describe_tools()}["ci_fehler"]
    assert t.tags == ["betrieb", "read", "flaeche"]
    assert set(t.input_schema["properties"]) == {"sha", "zweig", "zeilen", "voll"}
    assert t.input_schema["additionalProperties"] is False
