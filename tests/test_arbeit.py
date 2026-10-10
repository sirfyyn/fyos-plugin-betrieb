"""arbeit — Prüfstand der Arbeitsschleife: Validierung, Job-Manifest, Log-Auswertung, Diff, Ergebnis.

Alles ohne Cluster und ohne Netz (Attrappen für Gitea und Kubernetes).
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace as NS

import pytest

from fyos_plugin_betrieb import arbeit

SHA = "a" * 40


@pytest.fixture(autouse=True)
def pruefstand(monkeypatch):
    """Ein eingerichteter Prüfstand mit erfundenen Werten (ohne diese ist das Plugin nicht eingerichtet)."""
    for name, wert in {
        "FYOS_ARBEIT_WORKFLOW": "tests.yml",
        "FYOS_ARBEIT_WORKFLOW_NAME": "Tests",
        "FYOS_ARBEIT_BILD": "registry.example.org/beispiel/projekt/app:main",
        "FYOS_ARBEIT_TOKEN_SECRET": "git-token",
        "FYOS_ARBEIT_TESTWURZEL": "app/tests",
        "FYOS_ARBEIT_PYTHONPATH": "app:.",
        "FYOS_ARBEIT_MEIDE_KNOTEN": "knoten-a",
    }.items():
        monkeypatch.setenv(name, wert)


# ── Validierung ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("z", ["main", "agent/fix-test_1.2", "agent/" + "a" * 60])
def test_zweig_gut(z):
    assert arbeit.zweig_pruefen(z) == z


@pytest.mark.parametrize(
    "z",
    [
        "",
        None,
        5,
        "master",
        "agent/",
        "agent/" + "a" * 61,
        "agent/UPPER",
        "agent/a b",
        "agent/a;rm -rf /",
        "agent/$(id)",
        "agent/a\n",
        "main\n",
        "agent/..",
        "agent/a..b",
        "agent/x.lock",
        "agent/x/y",
        "-x",
        "--upload-pack=x",
        "feature/x",
    ],
)
def test_zweig_boese(z):
    with pytest.raises(ValueError):
        arbeit.zweig_pruefen(z)


@pytest.mark.parametrize(
    "n",
    [
        "app/tests/test_x.py",
        "app/tests/sub/test_x.py::test_y",
        "app/tests/test_x.py::test_y[a-1]",
    ],
)
def test_nodeid_gut(n):
    assert arbeit.nodeid_pruefen(n) == n


@pytest.mark.parametrize(
    "n",
    [
        "",
        None,
        "app/tests/",
        "app/tests/x.py::",
        "app/tests/x.py::t y",
        "app/tests/x.py --co",
        "-k foo",
        "--rootdir=/",
        "app/tests/../../etc.py",
        "app/tests/x.py;id",
        "app/tests/x.py::t|id",
        "app/tests/x.py::t\n",
        "app/tests/x.py::t::u",
        "tests/x.py",
        "/app/tests/x.py",
        "app/tests/x.sh",
        "app/tests/$(id).py",
    ],
)
def test_nodeid_boese(n):
    with pytest.raises(ValueError):
        arbeit.nodeid_pruefen(n)


def test_zeitlimit():
    assert arbeit.zeitlimit_pruefen(None) == 600
    assert arbeit.zeitlimit_pruefen(900) == 900
    assert arbeit.zeitlimit_pruefen(99999) == 1200
    assert arbeit.zeitlimit_pruefen(1) == 30
    for schlecht in ("600", True, [1]):
        with pytest.raises(ValueError):
            arbeit.zeitlimit_pruefen(schlecht)


def test_pruefen_lehnt_boeses_ab_ohne_cluster(monkeypatch):
    def nie(*a, **k):
        raise AssertionError("darf nicht erreicht werden")

    monkeypatch.setattr(arbeit, "gitea", nie)
    monkeypatch.setattr(arbeit, "_k8s_batch_core", nie)
    p = arbeit.ArbeitPlugin()
    for z, n in [
        ("main; id", "app/tests/x.py"),
        ("main", "app/tests/x.py -x"),
        ("agent/ok", "-k x"),
    ]:
        r = p.call("pruefen", {"zweig": z, "nodeid": n})
        assert r["ok"] is False and r["fehler"]
    assert (
        p.call("pruefen", {"zweig": "main", "nodeid": "app/tests/x.py", "zeitlimit_s": "x"})["ok"]
        is False
    )


# ── Job-Manifest ─────────────────────────────────────────────────────────────
def _manifest(zweig="agent/fix-1", nodeid="app/tests/test_x.py::test_y", limit=600):
    return arbeit.job_manifest("arbeit-pruefen-abcd1234", zweig, nodeid, limit, SHA)


def test_manifest_job_felder():
    m = _manifest(limit=900)
    assert m["metadata"]["name"] == "arbeit-pruefen-abcd1234"
    assert m["metadata"]["namespace"] == "fyos"
    assert m["metadata"]["labels"] == {
        "app": "arbeit-pruefen",
        "fyos/zweig": "agent_fix-1",
        "fyos/commit": SHA,
    }
    assert m["metadata"]["annotations"]["fyos/zweig-voll"] == "agent/fix-1"
    s = m["spec"]
    assert s["ttlSecondsAfterFinished"] == 3600
    assert s["activeDeadlineSeconds"] == 900
    assert s["backoffLimit"] == 0
    assert s["template"]["spec"]["restartPolicy"] == "Never"


def test_manifest_meidet_knoten_weich():
    aff = _manifest()["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]
    assert "requiredDuringSchedulingIgnoredDuringExecution" not in aff
    ausdruck = aff["preferredDuringSchedulingIgnoredDuringExecution"][0]["preference"][
        "matchExpressions"
    ][0]
    assert ausdruck == {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["knoten-a"]}


def test_manifest_pytest_ist_argv_ohne_shell():
    nodeid = "app/tests/test_x.py::test_y[a-1]"
    c = _manifest(nodeid=nodeid)["spec"]["template"]["spec"]["containers"][0]
    assert c["command"] == [
        "python", "-m", "pytest", nodeid, "-q", "--tb=short", "-p", "no:cacheprovider",
    ]  # fmt: skip
    assert c["workingDir"] == "/work/fyos"
    assert {"name": "PYTHONPATH", "value": "app:."} in c["env"]
    assert "sh" not in c["command"][0]


def test_manifest_klon_nimmt_zweig_nur_ueber_env():
    zweig = "agent/fix-1"
    init = _manifest(zweig=zweig)["spec"]["template"]["spec"]["initContainers"][0]
    skript = init["command"][2]
    assert zweig not in skript and "app/tests" not in skript  # Eingaben stehen nicht im Skript
    assert '"$ZWEIG"' in skript and "--depth 1" in skript
    assert {"name": "ZWEIG", "value": zweig} in init["env"]
    secrets = {e["name"]: e["valueFrom"]["secretKeyRef"] for e in init["env"] if "valueFrom" in e}
    assert secrets["GITEA_TOKEN"] == {"name": "git-token", "key": "GITEA_TOKEN"}
    assert secrets["GITEA_USER"]["name"] == "git-token"


def test_manifest_boeser_zweig_aendert_skript_nicht():
    assert (
        _manifest(zweig="main")["spec"]["template"]["spec"]["initContainers"][0]["command"]
        == (_manifest(zweig="agent/x")["spec"]["template"]["spec"]["initContainers"][0]["command"])
    )


def test_manifest_gleiches_bild_und_haertung():
    p = _manifest()["spec"]["template"]["spec"]
    assert p["initContainers"][0]["image"] == p["containers"][0]["image"] == arbeit.bild()
    assert p["automountServiceAccountToken"] is False
    assert p["securityContext"]["runAsNonRoot"] is True


def test_label_werte_gueltig():
    import re

    wert = arbeit._label("agent/" + "a" * 60)
    assert re.fullmatch(r"[A-Za-z0-9]([-_.A-Za-z0-9]*[A-Za-z0-9])?", wert) and len(wert) <= 63


class _Gitea:
    def __init__(self, antworten):
        self.antworten = antworten
        self.pfade = []

    def __call__(self, pfad, timeout=6):
        self.pfade.append(pfad)
        for muster, antwort in self.antworten.items():
            if pfad.startswith(muster):
                if isinstance(antwort, Exception):
                    raise antwort
                return antwort
        raise KeyError(pfad)


def test_pruefen_legt_job_an(monkeypatch):
    gitea = _Gitea({"/repos/beispiel/projekt/branches/agent/fix-1": {"commit": {"id": SHA}}})
    angelegt = []
    batch = NS(create_namespaced_job=lambda ns, body: angelegt.append((ns, body)))
    monkeypatch.setattr(arbeit, "gitea", gitea)
    monkeypatch.setattr(arbeit, "_k8s_batch_core", lambda: (batch, None))
    r = arbeit.ArbeitPlugin().call(
        "pruefen",
        {"zweig": "agent/fix-1", "nodeid": "app/tests/test_x.py::test_y", "zeitlimit_s": 5000},
    )
    assert r["ok"] is True and r["zweig"] == "agent/fix-1" and r["commit"] == SHA
    assert r["nodeid"] == "app/tests/test_x.py::test_y"
    assert r["job"].startswith("arbeit-pruefen-") and arbeit.JOB_RE.fullmatch(r["job"])
    ns, body = angelegt[0]
    assert ns == "fyos" and body["metadata"]["name"] == r["job"]
    assert body["spec"]["activeDeadlineSeconds"] == 1200
    assert body["metadata"]["labels"]["fyos/commit"] == SHA


def test_pruefen_zweig_fehlt(monkeypatch):
    monkeypatch.setattr(arbeit, "gitea", _Gitea({"/repos": OSError("404")}))
    r = arbeit.ArbeitPlugin().call("pruefen", {"zweig": "agent/x", "nodeid": "app/tests/a.py"})
    assert r["ok"] is False and "Zweig nicht lesbar" in r["fehler"]


# ── Log-Auswertung ───────────────────────────────────────────────────────────
LOG = """\
2026-10-06T10:00:00.1234567Z ============ test session starts ============
2026-10-06T10:00:05.0000000Z _____________________ test_a _____________________
2026-10-06T10:00:05.0000001Z app/tests/test_x.py:12: in test_a
2026-10-06T10:00:05.0000002Z     assert f() == 1
2026-10-06T10:00:05.0000003Z E   assert 2 == 1
2026-10-06T10:00:05.0000004Z E    +  where 2 = f()
2026-10-06T10:00:06.0000000Z _____________ test_b[x-1] _____________
2026-10-06T10:00:06.0000001Z E   KeyError: 'foo'
2026-10-06T10:00:07.0000000Z =========== short test summary info ===========
2026-10-06T10:00:07.0000001Z FAILED app/tests/test_x.py::test_a
2026-10-06T10:00:07.0000002Z \x1b[31mFAILED\x1b[0m app/tests/test_x.py::test_b[x-1] - KeyError: 'foo'
2026-10-06T10:00:07.0000003Z FAILED app/tests/test_x.py::test_b[x-1] - KeyError: 'foo'
2026-10-06T10:00:07.0000004Z ERROR app/tests/test_z.py - ImportError: no module named q
2026-10-06T10:00:07.0000005Z FAILED tests/andere.py::test_c - nicht app
2026-10-06T10:00:07.0000006Z FAILED app/tests/x.py::t;id - boese
2026-10-06T10:00:08.0000000Z ===== 3 failed in 5.00s =====
"""


def test_rote_tests_aus_log():
    r = arbeit.rote_tests(LOG)
    assert r == [
        {
            "nodeid": "app/tests/test_x.py::test_a",
            "fehler": "assert 2 == 1",
        },  # E-Zeile aus dem Abschnitt
        {"nodeid": "app/tests/test_x.py::test_b[x-1]", "fehler": "KeyError: 'foo'"},
        {"nodeid": "app/tests/test_z.py", "fehler": "ImportError: no module named q"},
    ]


def test_rote_tests_gruen_und_leer():
    assert arbeit.rote_tests("") == []
    assert arbeit.rote_tests("=== 5 passed in 1s ===") == []


def test_fehler_gekuerzt():
    log = "FAILED app/tests/t.py::t - " + "x" * 900
    assert len(arbeit.rote_tests(log)[0]["fehler"]) == 300


# ── kandidaten ───────────────────────────────────────────────────────────────
def _lauf(i, conclusion, **kw):
    return {
        "id": i,
        "name": "Tests",
        "path": "tests.yml@refs/heads/main",
        "head_branch": "main",
        "head_sha": f"{i:040x}",
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": "2026-10-06T10:10:00Z",
        "html_url": f"https://git.example.org/beispiel/projekt/actions/runs/{i}",
        **kw,
    }


def _fake_kandidaten(monkeypatch, laeufe, log=LOG):
    gitea = _Gitea(
        {
            "/repos/beispiel/projekt/actions/runs?": {"workflow_runs": laeufe},
            "/repos/beispiel/projekt/actions/runs/": {"jobs": [{"id": 77, "conclusion": "failure"}]},
        }
    )
    monkeypatch.setattr(arbeit, "gitea", gitea)
    monkeypatch.setattr(arbeit, "_gitea_text", lambda pfad, timeout=15: log)
    return gitea


def test_kandidaten_aus_letztem_roten_lauf(monkeypatch):
    laeufe = [
        _lauf(5, "success"),
        _lauf(9, "failure", started_at="x"),
        _lauf(8, "success", name="Anderes", path="build.yml@refs/heads/main"),
        _lauf(10, "failure", name="Anderes", path="build.yml@refs/heads/main"),
        _lauf(11, None, status="running"),
    ]
    _fake_kandidaten(monkeypatch, laeufe)
    r = arbeit.ArbeitPlugin().call("kandidaten", {})
    assert r["quelle_ok"] is True
    ids = [k["id"] for k in r["kandidaten"]]
    assert ids == [
        "test:app/tests/test_x.py::test_a",
        "test:app/tests/test_x.py::test_b[x-1]",
        "test:app/tests/test_z.py",
    ]
    k = r["kandidaten"][0]
    assert k["art"] == "roter_test" and k["workflow"] == "tests.yml"
    assert k["datei"] == "app/tests/test_x.py" and k["nodeid"] == "app/tests/test_x.py::test_a"
    assert k["commit"] == f"{9:040x}" and k["seit"] == "2026-10-06T10:10:00Z"
    assert k["lauf_url"].endswith("/runs/9") and k["fehler"] == "assert 2 == 1"


def test_kandidaten_gruen_ist_leer(monkeypatch):
    _fake_kandidaten(monkeypatch, [_lauf(9, "success"), _lauf(4, "failure")])
    assert arbeit.ArbeitPlugin().call("kandidaten", {}) == {"kandidaten": [], "quelle_ok": True}


def test_kandidaten_abgebrochener_lauf_zaehlt_nicht(monkeypatch):
    # 06.10. gemessen: jüngster abgeschlossener Lauf `cancelled`, davor `failure` → die roten Tests zählen.
    _fake_kandidaten(monkeypatch, [_lauf(12, "cancelled"), _lauf(13, "skipped"), _lauf(9, "failure")])
    r = arbeit.ArbeitPlugin().call("kandidaten", {})
    assert r["kandidaten"] and r["kandidaten"][0]["lauf_url"].endswith("/runs/9")


def test_kandidaten_hoechstens_zehn(monkeypatch):
    log = "\n".join(f"FAILED app/tests/t{i}.py::test_{i} - boom" for i in range(25))
    _fake_kandidaten(monkeypatch, [_lauf(9, "failure")], log=log)
    assert len(arbeit.ArbeitPlugin().call("kandidaten", {})["kandidaten"]) == 10


def test_kandidaten_quelle_aus(monkeypatch):
    monkeypatch.setattr(arbeit, "gitea", _Gitea({"/repos": OSError("weg")}))
    r = arbeit.ArbeitPlugin().call("kandidaten", {})
    assert r["kandidaten"] == [] and r["quelle_ok"] is False and "OSError" in r["grund"]


# ── diff ─────────────────────────────────────────────────────────────────────
def test_diff_antwort(monkeypatch):
    gitea = _Gitea(
        {
            "/repos/beispiel/projekt/compare/main...agent/fix-1": {
                "total_commits": 2,
                "commits": [
                    {"sha": "1" * 40, "commit": {"message": "Titel eins\n\nText"}},
                    {"sha": "2" * 40, "commit": {"message": "Titel zwei"}},
                ],
                "files": [{"filename": "app/a.py"}, {"filename": "app/tests/test_a.py"}],
            }
        }
    )
    monkeypatch.setattr(arbeit, "gitea", gitea)
    r = arbeit.ArbeitPlugin().call("diff", {"zweig": "agent/fix-1"})
    assert r == {
        "ok": True,
        "zweig": "agent/fix-1",
        "voraus": 2,
        "dateien": ["app/a.py", "app/tests/test_a.py"],
        "commits": [
            {"sha": "1" * 40, "titel": "Titel eins"},
            {"sha": "2" * 40, "titel": "Titel zwei"},
        ],
    }


def test_diff_fehler_und_boese_eingabe(monkeypatch):
    monkeypatch.setattr(arbeit, "gitea", _Gitea({"/repos": OSError("404")}))
    r = arbeit.ArbeitPlugin().call("diff", {"zweig": "agent/fix-1"})
    assert r["ok"] is False and "OSError" in r["fehler"]
    assert arbeit.ArbeitPlugin().call("diff", {"zweig": "x;y"})["ok"] is False


# ── ergebnis ─────────────────────────────────────────────────────────────────
def _t(sekunden):
    return dt.datetime(2026, 10, 6, 10, 0, 0, tzinfo=dt.timezone.utc) + dt.timedelta(
        seconds=sekunden
    )


def _job(succeeded=None, failed=None, bedingungen=(), start=0, ende=None):
    return NS(
        metadata=NS(labels={"fyos/commit": SHA}),
        status=NS(
            succeeded=succeeded,
            failed=failed,
            conditions=[
                NS(type=t, status="True", reason=r, last_transition_time=_t(ende or 0))
                for t, r in bedingungen
            ],
            start_time=_t(start),
            completion_time=_t(ende) if ende is not None and succeeded else None,
        ),
    )


def _hauptzustand(rc):
    return NS(name="pruefen", state=NS(terminated=NS(exit_code=rc), running=None, waiting=None))


def test_ergebnis_gruen():
    e = arbeit.ergebnis_bauen(_job(succeeded=1, ende=42), _hauptzustand(0), None, "1 passed")
    assert e == {
        "fertig": True,
        "gruen": True,
        "rc": 0,
        "commit": SHA,
        "dauer_s": 42.0,
        "ausgabe": "1 passed",
    }


def test_ergebnis_rot():
    e = arbeit.ergebnis_bauen(
        _job(failed=1, bedingungen=[("Failed", "BackoffLimitExceeded")], ende=10),
        _hauptzustand(1),
        None,
        "x",
    )
    assert e["fertig"] is True and e["gruen"] is False and e["rc"] == 1 and "grund" not in e


def test_ergebnis_laeuft():
    e = arbeit.ergebnis_bauen(
        _job(),
        NS(name="pruefen", state=NS(terminated=None, running=object(), waiting=None)),
        None,
        "",
    )
    assert e["fertig"] is False and e["gruen"] is None and e["rc"] is None


def test_ergebnis_klon_gescheitert_ist_nicht_rot():
    init = NS(name="klon", state=NS(terminated=NS(exit_code=128), waiting=None, running=None))
    e = arbeit.ergebnis_bauen(
        _job(failed=1, bedingungen=[("Failed", "BackoffLimitExceeded")], ende=5),
        None,
        init,
        "fatal",
    )
    assert e["fertig"] is True and e["gruen"] is None and e["grund"] == "Klon fehlgeschlagen"


def test_ergebnis_zeitlimit():
    e = arbeit.ergebnis_bauen(
        _job(failed=1, bedingungen=[("Failed", "DeadlineExceeded")], ende=600), None, None, ""
    )
    assert e["gruen"] is None and e["grund"] == "DeadlineExceeded"


def test_ergebnis_letzte_60_zeilen():
    log = "\n".join(str(i) for i in range(100))
    e = arbeit.ergebnis_bauen(_job(succeeded=1, ende=1), _hauptzustand(0), None, log)
    zeilen = e["ausgabe"].splitlines()
    assert len(zeilen) == 60 and zeilen[0] == "40" and zeilen[-1] == "99"


def test_ergebnis_lehnt_fremde_jobnamen_ab():
    r = arbeit.ArbeitPlugin().call("ergebnis", {"job": "testsuite-ftt"})
    assert r["fertig"] is False and "fehler" in r


def test_ergebnis_liest_job_und_pod(monkeypatch):
    pod = NS(
        metadata=NS(name="p1", creation_timestamp=_t(0)),
        status=NS(container_statuses=[_hauptzustand(0)], init_container_statuses=[]),
    )
    gelesen = {}

    def log(name, ns, container, tail_lines):
        gelesen.update(name=name, ns=ns, container=container, tail=tail_lines)
        return "ok"

    batch = NS(read_namespaced_job=lambda n, ns: _job(succeeded=1, ende=3))
    core = NS(
        list_namespaced_pod=lambda ns, label_selector: (
            NS(items=[pod])
            if label_selector == "job-name=arbeit-pruefen-abcd1234"
            else NS(items=[])
        ),
        read_namespaced_pod_log=log,
    )
    monkeypatch.setattr(arbeit, "_k8s_batch_core", lambda: (batch, core))
    e = arbeit.ArbeitPlugin().call("ergebnis", {"job": "arbeit-pruefen-abcd1234"})
    assert e["gruen"] is True and e["ausgabe"] == "ok"
    assert gelesen == {"name": "p1", "ns": "fyos", "container": "pruefen", "tail": 60}


# ── Registrierung ────────────────────────────────────────────────────────────
def test_werkzeuge_und_tags():
    t = {d.name: d for d in arbeit.ArbeitPlugin().describe_tools()}
    assert set(t) == {"kandidaten", "pruefen", "ergebnis", "diff"}
    assert "write" in t["pruefen"].tags
    assert all("write" not in t[n].tags for n in ("kandidaten", "ergebnis", "diff"))
    with pytest.raises(ValueError):
        arbeit.ArbeitPlugin().call("loeschen", {})
