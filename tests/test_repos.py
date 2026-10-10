"""repos: Übersicht und Detail gegen eine nachgebaute Gitea-API, die Helfer einzeln, die Oberfläche nach den Hub-Regeln."""

from __future__ import annotations

import datetime as dt
import json
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path

import pytest

from fyos_plugin_betrieb import lage
from fyos_plugin_betrieb import repos as R
from fyos_plugin_betrieb.repos import GiteaFehler, ReposPlugin

UI = Path(R.__file__).with_name("ui") / "repos.html"
JETZT = dt.datetime.now(dt.timezone.utc)

SHA_OS = "d87a490767bb642fd17d39a1e2d8c40d2edbb849"
SHA_OS_ALT = "5e53a687aae7ce983f7e75469ebcb1af1fcd2ab5"
SHA_GITHUB = "f185b866eba8cb9ddbb87e0821de341b421b4175"
SHA_LIZENZ = "7f8aee0bf096a1f1ff36ae38ff49a9ea580bbb69"
SHA_FYOS = "a97f3484cd5a3573c436305a991d2772e7e5f716"
SHA_VORLAGE = "2750fd6b0c3a1e9f7d5b4a3c2e1f0a9b8c7d6e5f"
SHA_ALT = "0123456789abcdef0123456789abcdef01234567"


def iso(minuten: float) -> str:
    return (JETZT - dt.timedelta(minutes=minuten)).isoformat(timespec="seconds")


def repo(name: str, **kw) -> dict:
    r = {"name": name, "description": f"Repo {name}", "private": True, "archived": False, "empty": False,
         "mirror": False, "default_branch": "main", "branch_count": 2, "open_pr_counter": 0, "updated_at": iso(60),
         "html_url": f"https://git.example/beispiel/{name}"}
    r.update(kw)
    return r


def zweig(name: str, sha: str, minuten: float, titel: str = "Commit", geschuetzt: bool = False) -> dict:
    return {"name": name, "protected": geschuetzt, "commit": {
        "id": sha, "message": f"{titel}\n\nLanger Text", "url": f"https://git.example/beispiel/x/commit/{sha}",
        "timestamp": iso(minuten), "author": {"name": "FYOS", "username": ""}}}


def status(zustand: str, *jobs: tuple[str, str]) -> dict:
    return {"state": zustand, "statuses": [
        {"status": s, "context": c, "target_url": f"/beispiel/x/actions/runs/1/jobs/{i}"} for i, (s, c) in enumerate(jobs)]}


def version(name: str, ver: str, minuten: float, art: str = "container", repo_name: str | None = None) -> dict:
    return {"type": art, "name": name, "version": ver, "created_at": iso(minuten),
            "html_url": f"https://git.example/beispiel/-/packages/{art}/{name}/{ver}",
            "repository": {"name": repo_name} if repo_name else None}


def lauf(nr: int, ereignis: str, pfad: str, zustand: str, ergebnis: str | None, sha: str, minuten: float) -> dict:
    return {"id": nr, "run_number": nr, "display_title": f"Lauf {nr}", "event": ereignis, "path": pfad,
            "status": zustand, "conclusion": ergebnis, "head_sha": sha, "head_branch": None,
            "started_at": iso(minuten), "completed_at": iso(minuten - 2) if zustand == "completed" else "0001-01-01T00:00:00Z",
            "html_url": f"https://git.example/beispiel/x/actions/runs/{nr}"}


def pr(nr: int, titel: str, von: str, mergebar: bool = True) -> dict:
    return {"number": nr, "title": titel, "user": {"login": "beispiel"}, "head": {"ref": von}, "base": {"ref": "main"},
            "updated_at": iso(10), "draft": False, "mergeable": mergebar, "comments": 0, "labels": [{"name": "dist"}],
            "html_url": f"https://git.example/beispiel/fyos-os/pulls/{nr}"}


class FakeGitea:
    """Antwortet wie die Gitea-API: Schlüssel ist der Pfad, bei /packages zusätzlich type und q, bei Läufen nach Zweig
    `<pfad>?branch=<zweig>`, falls es den Schlüssel gibt. Ab Seite 2 leer."""

    def __init__(self, antworten: dict, pakete_status: int = 200):
        self.antworten = antworten
        self.pakete_status = pakete_status
        self.aufrufe: list[tuple[str, bool]] = []

    def __call__(self, pfad: str, paket: bool):
        self.aufrufe.append((pfad, paket))
        basis, _, abfrage = pfad.partition("?")
        q = {k: v[0] for k, v in urllib.parse.parse_qs(abfrage, keep_blank_values=True).items()}
        if q.get("page", "1") != "1":
            return []
        schluessel = f"{basis}?type={q.get('type', '')}&q={q.get('q', '')}" if basis.startswith("/packages/") else basis
        if "branch" in q and f"{basis}?branch={q['branch']}" in self.antworten:
            schluessel = f"{basis}?branch={q['branch']}"
        if basis.startswith("/packages/") and self.pakete_status != 200:
            raise GiteaFehler(self.pakete_status, f"Gitea antwortet {self.pakete_status} auf {basis}")
        if schluessel not in self.antworten:
            if basis.startswith("/packages/"):
                return []
            raise GiteaFehler(404, f"Gitea antwortet 404 auf {basis}")
        antwort = self.antworten[schluessel]
        if isinstance(antwort, Exception):
            raise antwort
        return json.loads(json.dumps(antwort))  # Kopie, wie frisch vom Netz

    def pfade(self) -> list[str]:
        return [p.split("?")[0] for p, _ in self.aufrufe]


PYPI = [
    version("fyos-hub", "0.1.0.dev19", 600, "pypi"),
    version("fyos-hub", "0.1.0.dev17", 700, "pypi"),
    version("fyos-plugin-vorlage", "0.1.0", 100, "pypi"),
    version("fyos-plugin-betrieb", "0.1.0.dev1", 900, "pypi"),
]
BILDER_OS = [
    version("fyos-os/hub", SHA_OS, 500),
    version("fyos-os/hub", "main", 500),
    version("fyos-os/hub", SHA_OS_ALT, 560),
    version("fyos-os/web", SHA_OS_ALT, 561),
    version("fyos-os/web", "main", 561),
    version("fyos-os/web", "sha256:" + "a" * 64, 562),
]
BILDER_FYOS = [
    version("fyos/mcp-hub", SHA_FYOS, 10),
    version("fyos/mcp-hub", "main", 10),
    version("fyos/agent", "c" * 40, 90),
    version("fyos/agent-api", "c" * 40, 91),
    version("fyos/fyos-web", "26b2fb165fdbbf9cf2d6915c088fae096d9a7b93", 700),
    version("fyos/caddy-cf", "2.10", 4000),
]


def antworten_uebersicht() -> dict:
    return {
        "/users/beispiel/repos": [
            repo("fyos-os", open_pr_counter=2, branch_count=3),
            repo("fyos", open_pr_counter=11),
            repo("fyos-plugin-vorlage", private=False),
            repo("leer", empty=True),
            repo("alt", archived=True, updated_at=iso(60 * 24 * 40)),
        ],
        "/repos/beispiel/fyos-os/branches/main": zweig("main", SHA_OS, 30, "Glas: Unschärfe auch in Chrome"),
        "/repos/beispiel/fyos-os/commits/main/status": status("success", ("success", "ci / pruefen (push)"),
                                                             ("success", "ci / bauen (hub) (push)")),
        "/repos/beispiel/fyos/branches/main": zweig("main", SHA_FYOS, 5, "Hub: neue Werkzeuge"),
        "/repos/beispiel/fyos/commits/main/status": status("failure", ("success", "ci / bauen (push)"),
                                                          ("failure", "ci / test (push)")),
        "/repos/beispiel/fyos-plugin-vorlage/branches/main": zweig("main", SHA_VORLAGE, 120, "Vorlage"),
        "/repos/beispiel/fyos-plugin-vorlage/commits/main/status": {"state": "", "statuses": []},
        "/repos/beispiel/alt/branches/main": zweig("main", SHA_ALT, 60 * 24 * 40, "Alt"),
        "/repos/beispiel/alt/commits/main/status": GiteaFehler(500, "Gitea antwortet 500 auf /repos/beispiel/alt/commits/main/status"),
        "/packages/beispiel?type=pypi&q=": PYPI,
        "/packages/beispiel?type=container&q=fyos-os/": BILDER_OS,
        "/packages/beispiel?type=container&q=fyos/": BILDER_FYOS,
    }


def plugin_mit(antworten: dict, pakete_status: int = 200) -> tuple[ReposPlugin, FakeGitea]:
    p = ReposPlugin(config={"besitzer": "beispiel"})
    fake = FakeGitea(antworten, pakete_status=pakete_status)
    p._api = fake
    return p, fake


# ── Übersicht ───────────────────────────────────────────────────────────────────────────────────


def test_uebersicht_zeigt_je_repo_zweig_commit_ampel_prs_pakete_und_bilder():
    p, _ = plugin_mit(antworten_uebersicht())
    d = p.uebersicht()
    assert d["quelle_ok"] and d["besitzer"] == "beispiel" and d["pakete_ok"] is True
    namen = [r["name"] for r in d["repos"]]
    assert namen == ["fyos", "fyos-os", "leer", "fyos-plugin-vorlage", "alt"], "zuletzt Bewegtes zuerst, Archiviertes zuletzt"
    r = {x["name"]: x for x in d["repos"]}

    os_ = r["fyos-os"]
    assert os_["standardzweig"] == "main" and os_["offene_prs"] == 2 and os_["zweige"] == 3
    assert os_["commit"]["kurz"] == "d87a490" and os_["commit"]["titel"] == "Glas: Unschärfe auch in Chrome"
    assert os_["ci"]["ampel"] == "gruen" and os_["ci"]["jobs"] == 2
    assert os_["ci"]["url"] == "https://git.example/beispiel/x/actions/runs/1/jobs/0", "Pfad der API wird zur Adresse"
    assert [(x["name"], x["version"], x["anzahl"]) for x in os_["pakete"]] == [("fyos-hub", "0.1.0.dev19", 2)]
    bilder = {b["kurz_name"]: b for b in os_["bilder"]}
    assert bilder["hub"]["tag"] == "d87a490" and bilder["hub"]["aktuell"] is True and bilder["hub"]["tags"] == ["main"]
    assert bilder["web"]["tag"] == "5e53a68" and bilder["web"]["aktuell"] is False
    assert all(not t.startswith("sha256:") for t in bilder["web"]["tags"])

    assert r["fyos"]["ci"]["ampel"] == "rot" and r["fyos"]["ci"]["fehlgeschlagen"] == ["ci / test (push)"]
    assert r["fyos"]["ci"]["url"].endswith("/jobs/1"), "das Ziel ist der rote Job"
    assert len(r["fyos"]["bilder"]) == R.BILDER_UEBERSICHT and r["fyos"]["bilder_gesamt"] == 5
    assert r["fyos"]["bilder"][0]["kurz_name"] == "mcp-hub"

    vorlage = r["fyos-plugin-vorlage"]
    assert vorlage["ci"]["ampel"] == "grau" and vorlage["ci"]["text"] == "keine CI" and vorlage["privat"] is False
    assert [x["name"] for x in vorlage["pakete"]] == ["fyos-plugin-vorlage"]

    assert r["leer"]["ci"]["text"] == "leeres Repo" and r["leer"]["commit"] is None
    assert r["alt"]["ci"]["ampel"] == "unbekannt" and "ci" in r["alt"]["fehlt"] and r["alt"]["commit"]["kurz"] == "0123456"

    assert d["zaehlung"] == {"repos": 5, "gruen": 1, "rot": 1, "gelb": 0, "grau": 2, "unbekannt": 1, "prs": 13}
    assert d["satz"] == "5 Repos von beispiel · CI: 1 grün, 1 rot (fyos), 2 ohne CI, 1 nicht lesbar · 13 offene PRs."
    assert _zeit_ok(d["stand"])


def _zeit_ok(text: str) -> bool:
    return abs((dt.datetime.fromisoformat(text) - dt.datetime.now(dt.timezone.utc)).total_seconds()) < 60


def test_leeres_repo_wird_nicht_weiter_abgefragt():
    p, fake = plugin_mit(antworten_uebersicht())
    p.uebersicht()
    assert not [x for x in fake.pfade() if x.startswith("/repos/beispiel/leer")]


def test_uebersicht_haelt_eine_minute_und_filtert_ohne_neuen_abruf():
    p, fake = plugin_mit(antworten_uebersicht())
    p.uebersicht()
    n = len(fake.aufrufe)
    assert [r["name"] for r in p.uebersicht(filter_text="Plugin")["repos"]] == ["fyos-plugin-vorlage"]
    gefiltert = p.uebersicht(nur_probleme=True)
    assert [r["name"] for r in gefiltert["repos"]] == ["fyos", "alt"] and gefiltert["nur_probleme"] is True
    assert gefiltert["zaehlung"]["repos"] == 5, "die Zählung gilt für alle Repos, nicht nur die gefilterten"
    assert len(fake.aufrufe) == n


def test_ohne_paketrecht_fehlen_nur_die_versionen():
    p, fake = plugin_mit(antworten_uebersicht(), pakete_status=403)
    d = p.uebersicht()
    assert d["quelle_ok"] and d["pakete_ok"] is False and "read:package" in d["pakete_grund"]
    r = {x["name"]: x for x in d["repos"]}
    assert r["fyos-os"]["ci"]["ampel"] == "gruen" and r["fyos-os"]["commit"]["kurz"] == "d87a490"
    assert r["fyos-os"]["pakete"] == [] and r["fyos-os"]["bilder"] == []
    assert sum(1 for _, paket in fake.aufrufe if paket) == 1, "nach dem ersten 403 keine weiteren Paketabfragen"


def test_ohne_status_kommt_die_ampel_aus_den_laeufen_auf_dem_zweig():
    a = antworten_uebersicht()
    a["/users/beispiel/repos"][2] = repo("fyos-plugin-vorlage", private=False, has_actions=True)
    a["/repos/beispiel/fyos-plugin-vorlage/actions/runs"] = {"workflow_runs": [
        lauf(30, "pull_request", "ci.yml@refs/pull/3/head", "completed", "failure", SHA_GITHUB, 5),
        lauf(29, "push", "tests.yml@refs/heads/main", "completed", "failure", SHA_ALT, 50),
        lauf(28, "push", "ci.yml@refs/heads/main", "completed", "success", SHA_ALT, 55),
        lauf(27, "push", "tests.yml@refs/heads/main", "completed", "success", SHA_ALT, 60),
    ]}
    p, fake = plugin_mit(a)
    d = p.uebersicht()
    ci = {x["name"]: x for x in d["repos"]}["fyos-plugin-vorlage"]["ci"]
    assert ci["ampel"] == "rot" and ci["aus_laeufen"] is True and ci["fehlgeschlagen"] == ["tests.yml"]
    assert ci["url"].endswith("/runs/29") and ci["jobs"] == 2, "je Workflow der neueste Lauf auf main, der PR zählt nicht"
    assert d["satz"] == "5 Repos von beispiel · CI: 1 grün, 2 rot (fyos, fyos-plugin-vorlage), 1 ohne CI, 1 nicht lesbar · 13 offene PRs."
    laeufe = [x for x, _ in fake.aufrufe if "/actions/runs" in x]
    assert laeufe == ["/repos/beispiel/fyos-plugin-vorlage/actions/runs?branch=main&limit=30"], "nur ohne Status und mit Actions"


def test_ohne_status_und_ohne_lesbare_laeufe_bleibt_es_bei_keine_ci():
    for laeufe in (GiteaFehler(500, "Gitea antwortet 500 auf /repos/beispiel/fyos-plugin-vorlage/actions/runs"),
                   {"workflow_runs": [lauf(9, "push", "ci.yml@refs/heads/github", "completed", "failure", SHA_ALT, 5)]}):
        a = antworten_uebersicht()
        a["/users/beispiel/repos"][2] = repo("fyos-plugin-vorlage", private=False, has_actions=True)
        a["/repos/beispiel/fyos-plugin-vorlage/actions/runs"] = laeufe
        p, _ = plugin_mit(a)
        zeile = {x["name"]: x for x in p.uebersicht()["repos"]}["fyos-plugin-vorlage"]
        assert zeile["ci"]["ampel"] == "grau" and zeile["ci"]["text"] == "keine CI" and "fehlt" not in zeile


def test_paketfehler_wird_nicht_gemerkt():
    p, fake = plugin_mit(antworten_uebersicht(), pakete_status=502)
    assert p._pypi("beispiel") == ([], "Paketversionen fehlen: Gitea antwortet 502 auf /packages/beispiel")
    fake.pakete_status = 200
    versionen, grund = p._pypi("beispiel")
    assert grund is None and len(versionen) == len(PYPI)


def test_gitea_weg_ist_ein_zustand_kein_fehler():
    p, _ = plugin_mit({"/users/beispiel/repos": GiteaFehler(0, "Gitea nicht erreichbar (URLError)")})
    d = p.uebersicht()
    assert d["quelle_ok"] is False and "nicht erreichbar" in d["grund"] and d["satz"]


def test_organisation_statt_konto():
    antworten = antworten_uebersicht()
    antworten["/orgs/beispiel/repos"] = antworten.pop("/users/beispiel/repos")
    p, fake = plugin_mit(antworten)
    assert len(p.uebersicht()["repos"]) == 5
    assert fake.pfade()[:2] == ["/users/beispiel/repos", "/orgs/beispiel/repos"]


def test_unbekannter_besitzer_sagt_das_statt_eines_http_codes():
    # Gitea: unbekannter Nutzer → 404, unbekannte Organisation → 403
    p, _ = plugin_mit({"/orgs/beispiel/repos": GiteaFehler(403, "Gitea antwortet 403 auf /orgs/beispiel/repos")})
    d = p.uebersicht()
    assert d["quelle_ok"] is False and "weder einen Nutzer noch eine Organisation „beispiel“" in d["grund"]
    p, _ = plugin_mit({"/orgs/beispiel/repos": GiteaFehler(0, "Gitea nicht erreichbar (URLError)")})
    assert "nicht erreichbar" in p.uebersicht()["grund"], "andere Fehler bleiben, wie sie sind"


def test_falsche_argumente():
    p, _ = plugin_mit(antworten_uebersicht())
    with pytest.raises(ValueError):
        p.call("uebersicht", {"filter": 3})
    with pytest.raises(ValueError):
        p.call("uebersicht", {"filter": "x" * (R.FILTER_MAX + 1)})
    with pytest.raises(ValueError):
        p.call("gibt_es_nicht", {})


def test_besitzer_und_zuordnung_aus_konfiguration_umgebung_vorgabe(monkeypatch):
    monkeypatch.delenv("FYOS_REPOS_BESITZER", raising=False)
    assert ReposPlugin().besitzer == lage.REPO.split("/")[0]
    monkeypatch.setenv("FYOS_REPOS_BESITZER", "aus-der-umgebung")
    assert ReposPlugin().besitzer == "aus-der-umgebung"
    assert ReposPlugin(config={"besitzer": "aus-der-config"}).besitzer == "aus-der-config"
    z = ReposPlugin(config={"pakete": {"mein-paket": "mein-repo"}}).zuordnung
    assert z["mein-paket"] == "mein-repo" and z["fyos-hub"] == "fyos-os"


def test_health_nennt_tokens_nie_ihren_wert(monkeypatch):
    monkeypatch.setenv("GITEA_TOKEN", "geheim-123")
    monkeypatch.setenv("GITEA_PAKET_TOKEN", "geheim-456")
    h = ReposPlugin(config={"besitzer": "beispiel"}).health()
    assert h["token"] is True and h["paket_token"] is True
    assert "geheim" not in json.dumps(h)


# ── Detail ──────────────────────────────────────────────────────────────────────────────────────


def antworten_detail() -> dict:
    a = antworten_uebersicht()
    a.update({
        "/repos/beispiel/fyos-os": repo("fyos-os", open_pr_counter=2, branch_count=3),
        "/repos/beispiel/fyos-os/branches": [
            zweig("github", SHA_GITHUB, 20, "GitHub-Workflows"),
            zweig("main", SHA_OS, 30, "Glas: Unschärfe auch in Chrome"),
            zweig("lizenz-und-uv", SHA_LIZENZ, 90, "Aufräumen", geschuetzt=True),
        ],
        "/repos/beispiel/fyos-os/commits": [
            {"sha": SHA_OS, "html_url": f"https://git.example/beispiel/fyos-os/commit/{SHA_OS}",
             "commit": {"message": "Glas: Unschärfe auch in Chrome\n\nmehr", "author": {"name": "FYOS", "date": iso(30)}}},
            {"sha": SHA_OS_ALT, "html_url": f"https://git.example/beispiel/fyos-os/commit/{SHA_OS_ALT}",
             "commit": {"message": "Doku: Vollabgleich", "author": {"name": "FYOS", "date": iso(61)}}},
        ],
        "/repos/beispiel/fyos-os/actions/runs": {"total_count": 4, "workflow_runs": [
            lauf(19923, "pull_request", "ci.yml@refs/pull/2/head", "completed", "success", SHA_GITHUB, 15),
            lauf(19890, "schedule", "doku-nachts.yml@refs/heads/main", "completed", "failure", SHA_OS, 25),
            lauf(19800, "push", "ci.yml@refs/heads/main", "completed", "success", SHA_OS, 29),
            lauf(19700, "push", "ci.yml@refs/tags/v0.1.0", "running", None, SHA_OS_ALT, 3),
        ]},
        "/repos/beispiel/fyos-os/pulls": [pr(2, "GitHub-Workflows", "github"), pr(1, "Lizenz", "lizenz-und-uv", mergebar=False)],
    })
    return a


def test_detail_zweige_commits_laeufe_prs_pakete():
    p, _ = plugin_mit(antworten_detail())
    d = p.detail("fyos-os")
    assert d["quelle_ok"] and "fehlt" not in d
    assert [z["name"] for z in d["zweige"]] == ["main", "github", "lizenz-und-uv"], "Standardzweig zuerst, dann zuletzt bewegt"
    assert d["zweige"][0]["standard"] and d["zweige"][2]["geschuetzt"] and d["zweige_gesamt"] == 3

    assert [c["kurz"] for c in d["commits"]] == ["d87a490", "5e53a68"]
    assert d["commits"][0]["ci"] == "gruen", "der Kopf-Commit trägt die Ampel aus dem Kopf (kombinierter Status)"
    assert d["commits"][1]["ci"] == "gelb"

    l = d["laeufe"]
    assert [x["ref"] for x in l] == ["PR #2", "main", "main", "v0.1.0"]
    assert [x["ampel"] for x in l] == ["gruen", "rot", "gruen", "gelb"]
    assert l[0]["workflow"] == "ci.yml" and l[0]["dauer_s"] == 120 and l[3]["dauer_s"] is None and l[3]["text"] == "läuft"

    assert [(x["nummer"], x["von"], x["nach"], x["mergebar"]) for x in d["prs"]] == [(2, "github", "main", True), (1, "lizenz-und-uv", "main", False)]
    assert d["prs"][0]["labels"] == ["dist"]
    assert [x["name"] for x in d["pakete"]] == ["fyos-hub"] and d["pakete"][0]["versionen"][1]["version"] == "0.1.0.dev17"
    assert {b["kurz_name"]: b["aktuell"] for b in d["bilder"]} == {"hub": True, "web": False}

    kopf = d["repo"]
    assert kopf["commit"]["kurz"] == "d87a490" and "ci" not in kopf["commit"] and kopf["ci"]["ampel"] == "gruen"
    assert d["satz"] == f"fyos-os: main bei d87a490 ({R.vor_text(iso(30))}) · CI grün · 2 offene PRs · 3 Zweige."


def test_detail_holt_den_standardzweig_dazu_und_zaehlt_aus_branch_count():
    a = antworten_detail()
    a["/repos/beispiel/fyos-os"] = repo("fyos-os", open_pr_counter=2, branch_count=141)
    a["/repos/beispiel/fyos-os/branches"] = [zweig("github", SHA_GITHUB, 20, "GitHub-Workflows")]  # main nicht unter den neuesten
    p, fake = plugin_mit(a)
    d = p.detail("fyos-os")
    assert [z["name"] for z in d["zweige"]] == ["main", "github"] and d["zweige"][0]["standard"]
    assert d["repo"]["commit"]["kurz"] == "d87a490" and "fehlt" not in d
    assert d["zweige_gesamt"] == 141 and d["satz"].endswith("· 141 Zweige.")
    assert f"/repos/beispiel/fyos-os/branches?limit={R.ZWEIGE_MAX}&page=1" in [x for x, _ in fake.aufrufe], "nur eine Seite"


def test_detail_nennt_einen_roten_letzten_lauf_im_satz():
    a = antworten_detail()
    a["/repos/beispiel/fyos-os/actions/runs"]["workflow_runs"].insert(
        0, lauf(19999, "push", "ci.yml@refs/heads/main", "completed", "failure", SHA_OS, 1))
    p, _ = plugin_mit(a)
    assert p.detail("fyos-os")["satz"].endswith("letzter Lauf fehlgeschlagen (ci.yml auf main).")


def test_detail_ohne_status_nimmt_die_laeufe_auf_dem_zweig():
    a = antworten_detail()
    a["/repos/beispiel/fyos-os"] = repo("fyos-os", open_pr_counter=2, branch_count=3, has_actions=True)
    a["/repos/beispiel/fyos-os/commits/main/status"] = {"state": "", "statuses": []}
    a["/repos/beispiel/fyos-os/commits"].append(
        {"sha": SHA_ALT, "html_url": f"https://git.example/beispiel/fyos-os/commit/{SHA_ALT}",
         "commit": {"message": "Älter", "author": {"name": "FYOS", "date": iso(300)}}})
    a["/repos/beispiel/fyos-os/actions/runs?branch=main"] = {"workflow_runs": [
        lauf(19890, "schedule", "doku-nachts.yml@refs/heads/main", "completed", "failure", SHA_OS, 25),
        lauf(19800, "push", "ci.yml@refs/heads/main", "completed", "success", SHA_OS, 29),
        lauf(18000, "push", "ci.yml@refs/heads/main", "completed", "failure", SHA_OS_ALT, 70),
        lauf(17000, "push", "ci.yml@refs/heads/main", "completed", "success", SHA_ALT, 300),
    ]}
    p, _ = plugin_mit(a)
    d = p.detail("fyos-os")
    ci = d["repo"]["ci"]
    assert ci["ampel"] == "rot" and ci["aus_laeufen"] is True and ci["fehlgeschlagen"] == ["doku-nachts.yml"]
    assert [c["ci"] for c in d["commits"]] == ["rot", "gelb", "gruen"], \
        "der neuere Tag-Lauf schlägt den älteren auf main; Läufe nur vom Zweig füllen Lücken"
    assert "fehlt" not in d and [x["nummer"] for x in d["laeufe"]] == [19923, 19890, 19800, 19700]
    assert "· CI rot (aus den letzten Läufen) ·" in d["satz"]


def test_detail_ohne_status_und_ohne_zweig_laeufe_bleibt_es_bei_keine_ci():
    a = antworten_detail()
    a["/repos/beispiel/fyos-os"] = repo("fyos-os", open_pr_counter=2, branch_count=3, has_actions=True)
    a["/repos/beispiel/fyos-os/commits/main/status"] = {"state": "", "statuses": []}
    a["/repos/beispiel/fyos-os/actions/runs?branch=main"] = GiteaFehler(500, "Gitea antwortet 500 auf /repos/beispiel/fyos-os/actions/runs")
    p, _ = plugin_mit(a)
    d = p.detail("fyos-os")
    assert d["repo"]["ci"]["ampel"] == "grau" and "aus_laeufen" not in d["repo"]["ci"]
    assert "fehlt" not in d and len(d["laeufe"]) == 4, "die Zugabe fehlt still, die Läufe der Ansicht bleiben"


def test_kopf_commit_rot_auch_wenn_nur_die_pr_pruefung_scheitert():
    # Gesehen am 10.10.: der Push-Lauf auf main ist grün, die PR-Prüfung desselben Commits rot; ihr Lauf steht
    # in keiner Liste, der kombinierte Status kennt sie. Ein älterer Commit hat beide Läufe in der Liste.
    a = antworten_detail()
    a["/repos/beispiel/fyos-os/commits/main/status"] = status(
        "failure", ("success", "ci / pruefen (push)"), ("failure", "ci / pruefen (pull_request)"))
    a["/repos/beispiel/fyos-os/actions/runs"] = {"workflow_runs": [
        lauf(30, "push", "ci.yml@refs/heads/main", "completed", "success", SHA_OS, 20),
        lauf(29, "push", "ci.yml@refs/heads/main", "completed", "success", SHA_OS_ALT, 50),
        lauf(28, "pull_request", "ci.yml@refs/pull/3/head", "completed", "failure", SHA_OS_ALT, 55),
    ]}
    p, _ = plugin_mit(a)
    d = p.detail("fyos-os")
    assert d["repo"]["ci"]["ampel"] == "rot"
    assert [c["ci"] for c in d["commits"]] == ["rot", "rot"], \
        "Kopf wie der Status; sonst je Workflow und Ereignis der neueste Lauf, rot vor grün"


def test_detail_mit_besitzer_und_ohne_actions():
    a = antworten_detail()
    a["/repos/beispiel/fyos-os/actions/runs"] = GiteaFehler(404, "Gitea antwortet 404 auf /repos/beispiel/fyos-os/actions/runs")
    p, _ = plugin_mit(a)
    d = p.detail("beispiel/fyos-os")
    assert d["quelle_ok"] and d["laeufe"] == [] and "laeufe" in d["fehlt"]
    assert [c["ci"] for c in d["commits"]] == ["gruen", None] and len(d["zweige"]) == 3, \
        "ohne Läufe kennt nur der Kopf-Commit seine Ampel (aus dem Status)"


def test_detail_leeres_repo_fragt_keine_commits():
    a = antworten_uebersicht()
    a["/repos/beispiel/leer"] = repo("leer", empty=True)
    a["/repos/beispiel/leer/pulls"] = []
    a["/repos/beispiel/leer/actions/runs"] = {"total_count": 0, "workflow_runs": []}
    p, fake = plugin_mit(a)
    d = p.detail("leer")
    assert d["quelle_ok"] and d["repo"]["ci"]["text"] == "leeres Repo" and d["commits"] == [] and d["zweige"] == []
    assert not [x for x in fake.pfade() if x.endswith(("/branches", "/commits", "/status"))]


def test_detail_ohne_paketrecht():
    p, _ = plugin_mit(antworten_detail(), pakete_status=401)
    d = p.detail("fyos-os")
    assert d["quelle_ok"] and d["pakete_ok"] is False and d["bilder"] == [] and d["pakete"] == []
    assert "read:package" in d["pakete_grund"]


def test_detail_falsche_namen_und_unbekanntes_repo():
    p, _ = plugin_mit(antworten_detail())
    for falsch in (None, "", "   ", "../etc", "a/b/c d", "x" * 120):
        with pytest.raises(ValueError):
            p.detail(falsch)
    with pytest.raises(ValueError, match="gibt es nicht"):
        p.detail("gibt-es-nicht")


def test_detail_gitea_weg():
    p, _ = plugin_mit({"/repos/beispiel/fyos-os": GiteaFehler(0, "Gitea nicht erreichbar (TimeoutError)")})
    d = p.detail("fyos-os")
    assert d["quelle_ok"] is False and d["repo"]["name"] == "fyos-os" and "nicht erreichbar" in d["grund"]


# ── Helfer ──────────────────────────────────────────────────────────────────────────────────────


def test_ci_ampel():
    assert R.ci_ampel(None)["ampel"] == "unbekannt"
    assert R.ci_ampel({"state": "", "statuses": []})["ampel"] == "grau"
    laeuft = R.ci_ampel(status("pending", ("success", "a"), ("pending", "b")))
    assert laeuft["ampel"] == "gelb" and laeuft["text"] == "CI läuft" and laeuft["laufen"] == ["b"]
    assert laeuft["url"] is None, "ohne Basis kein halber Link"
    assert R.ci_ampel(status("warning", ("warning", "a")))["text"] == "CI mit Warnung"
    assert R.ci_ampel(status("error", ("error", "a")), "https://g")["url"] == "https://g/beispiel/x/actions/runs/1/jobs/0"


def test_vor_text():
    jetzt = dt.datetime(2026, 10, 10, 12, 0, tzinfo=dt.timezone.utc)
    vor = lambda **kw: R.vor_text((jetzt - dt.timedelta(**kw)).isoformat(), jetzt)  # noqa: E731
    assert vor(seconds=30) == "gerade eben"
    assert vor(minutes=5) == "vor 5 Min."
    assert vor(hours=3) == "vor 3 Std."
    assert vor(days=1) == "vor 1 Tag" and vor(days=3) == "vor 3 Tagen"
    assert R.vor_text(None) == "nie" and R.vor_text("0001-01-01T00:00:00Z") == "nie" and R.vor_text("kaputt") == "nie"


def test_ref_text_und_laeufe():
    assert R.ref_text("ci.yml@refs/heads/main") == ("ci.yml", "main")
    assert R.ref_text("ci.yml@refs/pull/12/head") == ("ci.yml", "PR #12")
    assert R.ref_text("pypi.yml@refs/tags/v0.1.0") == ("pypi.yml", "v0.1.0")
    assert R.ref_text(None) == ("", "")
    assert R.lauf_kurz(lauf(1, "push", "a@refs/heads/x", "queued", None, SHA_OS, 1))["text"] == "wartet"
    assert R.lauf_kurz(lauf(1, "push", "a@refs/heads/x", "completed", "cancelled", SHA_OS, 9))["ampel"] == "grau"
    assert R.lauf_kurz(lauf(1, "push", "a@refs/heads/x", "completed", "skipped", SHA_OS, 9))["text"] == "übersprungen"


def test_lauf_der_nie_gestartet_ist_hat_weder_start_noch_dauer():
    # fle-engine am 10.10.: abgebrochen in der Warteschlange, Gitea setzt den Start auf 1970 (sonst „vor 20736 Tagen“)
    roh = lauf(1031, "push", "python-lint.yml@refs/tags/b1573", "completed", "cancelled", SHA_ALT, 9)
    roh["started_at"] = "1970-01-01T01:00:00+01:00"
    x = R.lauf_kurz(roh)
    assert x["start"] is None and x["dauer_s"] is None and x["text"] == "abgebrochen"
    roh["started_at"], roh["completed_at"] = "0001-01-01T00:00:00Z", "0001-01-01T00:00:00Z"
    assert R.lauf_kurz(roh)["start"] is None


def test_schlimmste_ampel():
    assert R.schlimmste_ampel(["gruen", "rot", "gelb"]) == "rot"
    assert R.schlimmste_ampel(["gruen", "gelb", "grau"]) == "gelb"
    assert R.schlimmste_ampel(["grau", "gruen"]) == "gruen"
    assert R.schlimmste_ampel(["grau"]) == "grau" and R.schlimmste_ampel([]) == "grau"


def test_ci_aus_laeufen():
    laeufe = [R.lauf_kurz(x) for x in (
        lauf(5, "pull_request", "ci.yml@refs/pull/1/head", "completed", "failure", SHA_GITHUB, 1),
        lauf(4, "push", "ci.yml@refs/heads/main", "running", None, SHA_OS, 2),
        lauf(3, "schedule", "nacht.yml@refs/heads/main", "completed", "success", SHA_OS, 30),
        lauf(2, "push", "ci.yml@refs/heads/main", "completed", "failure", SHA_OS_ALT, 60),
    )]
    gelb = R.ci_aus_laeufen(laeufe, "main")
    assert gelb["ampel"] == "gelb" and gelb["text"] == "CI läuft" and gelb["laufen"] == ["ci.yml"] and gelb["jobs"] == 2
    assert gelb["url"].endswith("/runs/4") and gelb["fehlgeschlagen"] == [], "der ältere rote ci.yml-Lauf ist überholt"
    assert R.ci_aus_laeufen(laeufe, "github") is None and R.ci_aus_laeufen([], "main") is None
    gruen = R.ci_aus_laeufen(laeufe[2:3], "main")
    assert gruen["ampel"] == "gruen" and gruen["text"] == "CI grün" and gruen["aus_laeufen"] is True
    grau = R.ci_aus_laeufen([R.lauf_kurz(lauf(1, "push", "ci.yml@refs/heads/main", "completed", "cancelled", SHA_OS, 9))], "main")
    assert grau["ampel"] == "grau" and grau["text"] == "CI abgebrochen"


def test_pr_mit_geloeschtem_quellzweig():
    weg = R.pr_kurz(pr(7, "Alt", "refs/pull/7/head"))
    assert weg["von"] == "" and weg["zweig_geloescht"] is True and weg["nach"] == "main"
    da = R.pr_kurz(pr(2, "Neu", "github"))
    assert da["von"] == "github" and da["zweig_geloescht"] is False


def test_bilder_ohne_commit_tag_zeigen_den_neuesten_tag():
    (b,) = R.bilder_fuer("fyos", [version("fyos/caddy-cf", "2.10", 5), version("fyos/caddy-cf", "2.9", 50)], SHA_FYOS)
    assert b["tag"] == "2.10" and b["commit"] is None and b["aktuell"] is None and b["tags"] == ["2.10", "2.9"]
    assert R.bilder_fuer("fyos", [version("fyos-os/hub", SHA_OS, 1)]) == [], "nur Bilder unter <repo>/"


def test_bildverlauf_ohne_mitwandernde_namen():
    hub = next(b for b in R.bilder_fuer("fyos-os", BILDER_OS, SHA_OS) if b["kurz_name"] == "hub")
    assert hub["tags"] == ["main"] and [v["version"] for v in hub["versionen"]] == ["d87a490", "5e53a68"], \
        "`main` steht schon als Tag da und ist kein Stand im Verlauf"
    assert hub["anzahl"] == 3


def test_bildseiten_nebeneinander_entdoppelt_und_gemerkt():
    seiten = {1: [version("fyos/a", f"{i:040x}", i) for i in range(50)],
              2: [version("fyos/a", f"{i:040x}", i) for i in range(49, 99)],  # eine Version rückte nach
              3: [version("fyos/a", f"{i:040x}", i) for i in range(99, 109)], 4: []}
    gefragt: list[int] = []

    def api(pfad: str, paket: bool):
        q = dict(urllib.parse.parse_qsl(pfad.partition("?")[2]))
        assert paket and q["type"] == "container" and q["q"] == "fyos/"
        gefragt.append(int(q["page"]))
        return json.loads(json.dumps(seiten[int(q["page"])]))

    p = ReposPlugin(config={"besitzer": "beispiel"})
    p._api = api
    roh, mehr = p._bilder_roh("beispiel", "fyos", R.BILD_SEITEN_DETAIL)
    assert len(roh) == 109 and len({v["version"] for v in roh}) == 109 and mehr is False
    assert sorted(gefragt) == [1, 2, 3, 4]
    assert p._bilder_roh("beispiel", "fyos", 1) == (roh[:50], True) and len(gefragt) == 4, "Seite 1 aus dem Zwischenspeicher"


def test_detail_nimmt_die_erste_bildseite_aus_der_uebersicht():
    p, fake = plugin_mit(antworten_detail())
    p.uebersicht()
    d = p.detail("fyos-os")
    assert {b["kurz_name"] for b in d["bilder"]} == {"hub", "web"} and "bilder_hinweis" not in d
    assert sum(1 for x, _ in fake.aufrufe if x.startswith("/packages/beispiel?type=container&q=fyos-os")) == 1


def test_pakete_zuordnung_vor_verknuepfung_vor_name():
    versionen = [version("x", "1", 1, "pypi", repo_name="verknuepft"), version("fyos-hub", "2", 1, "pypi", repo_name="anders")]
    assert [p["name"] for p in R.pakete_fuer("verknuepft", versionen, {})] == ["x"]
    assert R.pakete_fuer("x", versionen, {}) == [], "die Verknüpfung in Gitea schlägt den Namen"
    assert [p["name"] for p in R.pakete_fuer("fyos-os", versionen, R.PAKETE_VORGABE)] == ["fyos-hub"]


def test_erste_zeile():
    assert R.erste_zeile("Titel\n\nText") == "Titel"
    lang = R.erste_zeile("x" * 300, 50)
    assert len(lang) == 50 and lang.endswith("…")
    assert R.erste_zeile(None) == ""


# ── Beschreibung, Oberfläche ────────────────────────────────────────────────────────────────────


def test_werkzeuge_sind_lesende_app_werkzeuge():
    werkzeuge = {w.name: w for w in ReposPlugin().describe_tools()}
    assert set(werkzeuge) == {"uebersicht", "detail"}
    for w in werkzeuge.values():
        assert w.meta["ui"]["resourceUri"] == R.UI_URI and "visibility" not in w.meta["ui"]
        assert w.meta["app"]["titel"] == "Repos" and "read" in w.tags and "flaeche" in w.tags
    assert werkzeuge["detail"].input_schema["required"] == ["repo"]


def _skripte(roh: str) -> str:
    return "".join(re.findall(r"<script\b.*?</script>", roh, re.S | re.I))


def test_oberflaeche_folgt_den_regeln_des_hubs():
    roh = UI.read_text(encoding="utf-8")
    assert roh.count("/*STIL*/") == 1 and roh.count("/*BRUECKE*/") == 1
    assert "<style>/*STIL*/" in roh and re.search(r"<script>\s*/\*BRUECKE\*/", roh)
    assert "prefers-color-scheme" not in roh and not re.search(r":root\s*[{,]", roh), "Farben nur aus _stil.css"
    for aufruf in ("confirm(", "alert(", "prompt("):
        assert aufruf not in roh, aufruf
    assert re.search(r"<body[^>]*>\s*<!--KOPF-->", roh) and "<h1" not in roh and 'class="kopf"' not in roh
    assert "data-symbol" not in _skripte(roh), "im Skript baut App.symbol das Symbol"
    assert "<form" not in roh


def test_oberflaeche_ruft_nur_eigene_werkzeuge_und_nennt_nur_vorhandene_symbole():
    from mcp_hub.plugins import _identitaet

    roh = UI.read_text(encoding="utf-8")
    skripte = _skripte(roh)
    gerufen = set(re.findall(r"""(?<!App\.)\brufen\(\s*["'](\w+)["']""", skripte))
    assert gerufen and gerufen <= {w.name for w in ReposPlugin().describe_tools()}
    assert 'App.rufen("repos__" + name' in skripte
    bekannt = _identitaet.symbole()
    genannt = set(re.findall(r'data-symbol="([^"]*)"', roh))
    genannt |= set(re.findall(r"""\b(?:symbol|kachel|leerKarte)\(\s*["'`]([a-z0-9-]+)["'`]""", skripte))
    genannt |= set(re.findall(r"""\bsymbol:\s*["'`]([a-z0-9-]+)["'`]""", skripte))
    genannt |= set(re.findall(r"""marke\([^()]*?,\s*["'][a-z]*["'],\s*["']([a-z0-9-]+)["']\)""", skripte))
    fehlt = sorted(n for n in genannt if n not in bekannt)
    assert genannt and not fehlt, fehlt


def test_ausgeliefert_mit_kopf_konfig_und_gueltigem_skript(tmp_path):
    (res,) = ReposPlugin().describe_resources()
    assert res.uri == R.UI_URI
    html = res.read()
    assert "/*STIL*/" not in html and "/*BRUECKE*/" not in html and "/*KONFIG*/" not in html
    assert html.count("<h1>Repos</h1>") == 1 and html.count('id="kopf-satz"') == 1
    assert '"werkzeug":"repos__uebersicht"' in html
    node = shutil.which("node")
    if not node:
        pytest.skip("node fehlt: Skript nicht geprüft")
    skripte = re.findall(r"<script>(.*?)</script>", html, re.S)
    for i, s in enumerate(skripte):
        datei = tmp_path / f"skript{i}.js"
        datei.write_text(s, encoding="utf-8")
        r = subprocess.run([node, "--check", str(datei)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
