"""betrieb.ci_fehler — warum ist die CI zu einem Commit rot? (2026-10-09)

Ein Aufruf ersetzt die Klickerei in Gitea: je Workflow-Lauf Name, Status und Einordnung
(gruen / fehler / abgebrochen / laeuft / wartet); fuer jeden fehlgeschlagenen Job ein Logauszug,
der die Fehlerstelle zeigt, und ein Satz oben („2 von 17 rot: Tests-Baum (1 Test: ...), ...").

Der Logauszug ist nicht das Ende des Logs — das ist bei act Aufraeum-Rauschen (Container und
Volumes entfernen, git config). Stattdessen:

- pytest: die Bloecke „FAILURES" und „short test summary info" samt Ergebniszeile; je Test eine
  Ursache aus den `E`-Zeilen (nicht aus dem abgeschnittenen Ende der FAILED-Zeile).
- sonst: die Ausgabe des fehlgeschlagenen Schritts (von „::group::Run" bis zur act-Zeile
  „❌  Failure - ..."), davon das Ende.
- ohne beides: das Log-Ende ohne Aufraeumzeilen.

Dazu der Abgleich mit main: war derselbe Test (bzw. Job) im letzten abgeschlossenen Lauf desselben
Workflows auf main auch rot („vorbestehend") oder nicht („neu")?

Reine Funktionen und der Ablauf liegen hier; das Plugin `betrieb` reicht nur Gitea-Zugriff und
Zwischenspeicher durch (so lassen sich beide in Tests ersetzen).
"""

from __future__ import annotations

import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from .lage import gitea_api, repo

ZEILEN_VORGABE = 40
ZEILEN_MIN, ZEILEN_MAX = 5, 200
MAX_LOG_BYTES = 24_000_000  # darueber: nur das Ende (dort steht der Fehler)
MAX_JOBS_MIT_LOG = 12  # mehr rote Jobs: der Rest steht ohne Auszug da
MAX_VERGLEICHE = 6  # Workflows, die gegen main verglichen werden
SEITE = 50  # Gitea liefert hoechstens 50 Laeufe je Seite
MAX_SEITEN = 4

GRUEN, FEHLER, ABGEBROCHEN, LAEUFT, WARTET, UEBERSPRUNGEN, UNKLAR = (
    "grün",
    "fehler",
    "abgebrochen",
    "läuft",
    "wartet",
    "übersprungen",
    "unklar",
)

Holen = Callable[..., Any]


# ── Gitea ───────────────────────────────────────────────────────────────────────
def gitea_text(pfad: str, timeout: float = 20.0) -> str:
    """GET gegen die Gitea-API, Antwort als Text (Job-Logs sind kein JSON). Wirft bei Fehlern."""
    token = os.getenv("GITEA_TOKEN", "")
    req = urllib.request.Request(
        f"{gitea_api()}{pfad}", headers={"Authorization": f"token {token}"} if token else {}
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        roh = r.read()
    if len(roh) > MAX_LOG_BYTES:
        roh = roh[-MAX_LOG_BYTES:]
    return roh.decode("utf-8", errors="replace")


# ── Einordnung ──────────────────────────────────────────────────────────────────
def einordnen(status: Any, ergebnis: Any) -> str:
    """Gitea-Status und -Ergebnis eines Laufs oder Jobs -> grün | fehler | abgebrochen | läuft | wartet ..."""
    s = str(status or "").lower()
    c = str(ergebnis or "").lower()
    if s in {"in_progress", "running"}:
        return LAEUFT
    if s in {"queued", "waiting", "pending", "blocked"}:
        return WARTET
    # Manche Gitea-Staende tragen das Ergebnis schon im Status.
    for wert in (c, s):
        if wert in {"success", "successful"}:
            return GRUEN
        if wert in {"cancelled", "canceled"}:
            return ABGEBROCHEN
        if wert == "skipped":
            return UEBERSPRUNGEN
        if wert in {"failure", "failed", "timed_out", "timeout", "error"}:
            return FEHLER
    return UNKLAR


def workflow_stamm(pfad: Any) -> str:
    """„test-tests-baum.yml@refs/heads/x" -> „test-tests-baum.yml"."""
    return str(pfad or "").split("@", 1)[0].rsplit("/", 1)[-1]


# ── Log lesen ───────────────────────────────────────────────────────────────────
_ZEIT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z ?")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_KOPF = re.compile(r"^=+ (.+?) =+$")
_UNTER = re.compile(r"^_{2,} (.+?) _{2,}$")
_SUMME = re.compile(r"^=+ short test summary info =+$")
_MELDUNG = re.compile(r"^(FAILED|ERROR) (.+?)(?: - (.*))?$")
_ERGEBNIS = re.compile(r"^(?:=+ )?\d+ (?:failed|passed|error|errors|skipped)\b.*?(?: =+)?$")
_SCHRITT_FEHL = re.compile(r"^\s*❌\s+Failure - (?:Main )?(.+?)\s*$")
_FEHLERZEILE = re.compile(
    r"(Error:|error:|\bERROR\b|\bFAIL(?:ED)?\b|Traceback|Exception|✖|✗|##\[error\]|fatal:|npm ERR!|"
    r"No such file|Permission denied|command not found)"
)
_RAUSCHEN = re.compile(
    r"(Cleaning up|docker volume rm|Removed container|\[command\]/usr/bin/git|Post Checkout|"
    r"Success - Post|Run Post|🏁|Job '.*' (?:failed|succeeded)|evaluating expression|"
    r"expression '.*' evaluated|Skipping step|exitcode '\d+': failure|^::(?:end)?group::)"
)


def log_zeilen(text: str) -> list[str]:
    """Zeitstempel und Farbcodes weg, Leerzeilen weg."""
    aus = []
    for roh in (text or "").splitlines():
        z = _ANSI.sub("", _ZEIT.sub("", roh)).rstrip()
        if z.strip():
            aus.append(z)
    return aus


def _eng(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip())
    return re.sub(r"^AssertionError: ?", "", s)


def _ursache(e_zeilen: list[str], grund: str) -> str:
    """Eine Zeile Ursache: erste E-Zeile; endet sie auf „:", steht die Sache in der naechsten."""
    sauber = [x for x in (_eng(e[1:]) for e in e_zeilen) if x]
    if sauber:
        if sauber[0].endswith(":") and len(sauber) > 1:
            return sauber[1]
        return sauber[0]
    return _eng(grund or "")


def _bloecke(z: list[str], ab: int) -> dict[str, list[str]]:
    """Einzelfehler-Bloecke hinter „= FAILURES =" / „= ERRORS =" (Kopfzeile `___ name ___`)."""
    out: dict[str, list[str]] = {}
    name: str | None = None
    for x in z[ab + 1 :]:
        k = _KOPF.match(x)
        if k and k.group(1) not in {"FAILURES", "ERRORS"}:
            break
        u = _UNTER.match(x)
        if u:
            name = u.group(1)
            out.setdefault(name, [])
        elif name is not None:
            out[name].append(x)
    return out


def _ohne_param(s: str) -> str:
    return re.sub(r"\[.*\]$", "", s)


def pytest_auswerten(z: list[str]) -> dict[str, Any] | None:
    """Tests, Ursachen und Auszug aus pytest-Ausgabe; None, wenn das Log keine enthaelt."""
    i_sum = next((i for i, x in enumerate(z) if _SUMME.match(x)), None)
    i_fail = next((i for i, x in enumerate(z) if re.match(r"^=+ (?:FAILURES|ERRORS) =+$", x)), None)
    if i_sum is None and i_fail is None:
        return None
    bloecke = _bloecke(z, i_fail) if i_fail is not None else {}
    verbraucht: set[str] = set()

    def e_zeilen(test_id: str) -> list[str]:
        basis = _ohne_param(test_id.split("::")[-1])
        for name, zeilen in bloecke.items():
            if name in verbraucht:
                continue
            if _ohne_param(name.split(".")[-1]) == basis or _ohne_param(name) == basis:
                verbraucht.add(name)
                return [x for x in zeilen if x.startswith("E ")]
        return []

    tests: list[dict[str, str]] = []
    i_erg: int | None = None
    if i_sum is not None:
        for i in range(i_sum + 1, len(z)):
            x = z[i]
            if x.startswith("!!!!"):
                continue
            if _ERGEBNIS.match(x):
                i_erg = i
                break
            m = _MELDUNG.match(x)
            if m:
                tests.append(
                    {"id": m.group(2), "ursache": _ursache(e_zeilen(m.group(2)), m.group(3) or "")}
                )
    else:  # kein -r-Block: Namen und Ursachen aus den Einzelbloecken
        for name, zeilen in bloecke.items():
            tests.append({"id": name, "ursache": _ursache([x for x in zeilen if x.startswith("E ")], "")})
        i_erg = next((i for i, x in enumerate(z) if _ERGEBNIS.match(x)), None)

    teil_a: list[str] = []
    if i_fail is not None:
        for x in z[i_fail:]:
            k = _KOPF.match(x)
            if k and k.group(1) not in {"FAILURES", "ERRORS"}:
                break
            teil_a.append(x)
    teil_b: list[str] = []
    if i_sum is not None:
        teil_b = z[i_sum : (i_erg + 1) if i_erg is not None else len(z)]
    elif i_erg is not None:
        teil_b = [z[i_erg]]
    return {
        "tests": tests,
        "teil_a": teil_a,
        "teil_b": teil_b,
        "ergebnis_zeile": _eng(z[i_erg]).strip("= ") if i_erg is not None else None,
    }


def schritt_fenster(z: list[str]) -> tuple[str | None, list[str]]:
    """(Name, Ausgabe) des fehlgeschlagenen Schritts nach der act-Zeile „❌  Failure - Main <Schritt>"."""
    for i, x in enumerate(z):
        m = _SCHRITT_FEHL.match(x)
        if not m or m.group(1).startswith("Post "):
            continue
        j = i - 1
        while j >= 0 and not z[j].startswith("::group::Run "):
            j -= 1
        fenster = z[j + 1 : i]
        for k, y in enumerate(fenster):  # Befehlsecho und shell-Zeile bis ::endgroup:: ueberspringen
            if y.startswith("::endgroup::"):
                fenster = fenster[k + 1 :]
                break
        return m.group(1), [y for y in fenster if not y.startswith(("::group::", "::endgroup::"))]
    return None, []


def log_auswerten(text: str, zeilen: int = ZEILEN_VORGABE) -> dict[str, Any]:
    """Fehlerstelle eines Job-Logs: schritt, art (pytest | schritt | ende), tests, ursache, auszug."""
    z = log_zeilen(text)
    schritt, fenster = schritt_fenster(z)
    py = pytest_auswerten(z)
    if py is not None and (py["tests"] or py["teil_a"]):
        # Zusammenfassung hat Vorrang vor den Einzelbloecken, wenn das Budget knapp ist.
        b = py["teil_b"][:zeilen]
        a = py["teil_a"][: max(0, zeilen - len(b))]
        tests = py["tests"]
        return {
            "art": "pytest",
            "schritt": schritt,
            "tests": tests,
            "ergebnis_zeile": py["ergebnis_zeile"],
            "auszug": a + b,
        }
    if fenster:
        quelle = fenster
        art = "schritt"
    else:
        quelle = [x for x in z if not _RAUSCHEN.search(x)]
        art = "ende"
    treffer = next((x for x in quelle if _FEHLERZEILE.search(x)), None)
    ursache = treffer or (quelle[-1] if quelle else "")
    return {
        "art": art,
        "schritt": schritt,
        "tests": [],
        "ursache": re.sub(r"\s+", " ", ursache.strip()),
        "ergebnis_zeile": None,
        "auszug": quelle[-zeilen:],
    }


# ── Ablauf ──────────────────────────────────────────────────────────────────────
def _kurz(sha: str) -> str:
    return (sha or "")[:9]


def _liste(antwort: Any, schluessel: str) -> list[dict[str, Any]]:
    if isinstance(antwort, dict):
        antwort = antwort.get(schluessel)
    return [x for x in (antwort or []) if isinstance(x, dict)]


def _laeufe_holen(holen: Holen, abfrage: str) -> list[dict[str, Any]]:
    """Alle Seiten eines `actions/runs?...`-Aufrufs (hoechstens MAX_SEITEN)."""
    alle: list[dict[str, Any]] = []
    for seite in range(1, MAX_SEITEN + 1):
        teil = _liste(
            holen(f"/repos/{repo()}/actions/runs?{abfrage}&limit={SEITE}&page={seite}", timeout=15),
            "workflow_runs",
        )
        alle.extend(teil)
        if len(teil) < SEITE:
            break
    return alle


def _neueste_je_workflow(laeufe: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ein Lauf je Workflow und Zweig — bei Wiederholungen der juengste."""
    je: dict[str, dict[str, Any]] = {}
    for r in laeufe:
        schluessel = str(r.get("path") or r.get("id"))
        if schluessel not in je or int(r.get("id") or 0) > int(je[schluessel].get("id") or 0):
            je[schluessel] = r
    return sorted(je.values(), key=lambda r: int(r.get("id") or 0))


def _workflow_namen(holen: Holen, cache: Any) -> dict[str, str]:
    def laden() -> dict[str, str]:
        try:
            ws = _liste(holen(f"/repos/{repo()}/actions/workflows", timeout=10), "workflows")
        except Exception:  # noqa: BLE001 — Name faellt auf den Dateinamen zurueck
            return {}
        namen: dict[str, str] = {}
        for w in ws:
            if w.get("name"):
                namen[workflow_stamm(w.get("path") or w.get("id"))] = str(w["name"])
        return namen

    return cache.holen("cifehler:workflows", 600, laden) if cache is not None else laden()


def _name(namen: dict[str, str], stamm: str) -> str:
    return namen.get(stamm) or re.sub(r"\.ya?ml$", "", stamm)


def _job_auswerten(
    holen: Holen, holen_text: Holen, cache: Any, job: dict[str, Any], zeilen: int
) -> dict[str, Any]:
    """Log eines roten Jobs holen und auswerten; Quellfehler stehen als `log_fehler`."""
    schritte = _liste(job, "steps")
    api_schritt = next(
        (s.get("name") for s in schritte if einordnen(s.get("status"), s.get("conclusion")) == FEHLER),
        None,
    )

    def laden() -> dict[str, Any]:
        try:
            text = holen_text(f"/repos/{repo()}/actions/jobs/{job.get('id')}/logs", timeout=25)
        except Exception as e:  # noqa: BLE001 — Quellfehler ist Nutzdaten
            return {"log_fehler": f"{type(e).__name__}: {e}"[:160], "tests": [], "auszug": []}
        return log_auswerten(text, zeilen)

    fertig = einordnen(job.get("status"), job.get("conclusion")) in {FEHLER, GRUEN, ABGEBROCHEN}
    if cache is not None and fertig:
        # Fehlgeschlagene Antworten (Netz) nicht lange festhalten.
        a = cache.holen(f"cifehler:log:{job.get('id')}:{zeilen}", 900, laden)
        if a.get("log_fehler"):
            a = laden()
    else:
        a = laden()
    return {"name": job.get("name"), "id": job.get("id"), **a, "schritt": api_schritt or a.get("schritt")}


def _rote_jobs(holen: Holen, holen_text: Holen, cache: Any, run: dict[str, Any], zeilen: int,
               budget: list[int]) -> list[dict[str, Any]]:
    """Analysen der roten Jobs eines Laufs; `budget` zaehlt die noch erlaubten Log-Abrufe."""
    try:
        jobs = _liste(holen(f"/repos/{repo()}/actions/runs/{run['id']}/jobs", timeout=15), "jobs")
    except Exception as e:  # noqa: BLE001
        return [{"name": None, "log_fehler": f"Jobs nicht lesbar: {type(e).__name__}: {e}"[:160],
                 "tests": [], "auszug": []}]
    rot = [j for j in jobs if einordnen(j.get("status"), j.get("conclusion")) == FEHLER]
    erlaubt, rest = rot[: max(0, budget[0])], rot[max(0, budget[0]) :]
    budget[0] -= len(erlaubt)
    with ThreadPoolExecutor(max_workers=4) as pool:
        aus = list(pool.map(lambda j: _job_auswerten(holen, holen_text, cache, j, zeilen), erlaubt))
    aus += [{"name": j.get("name"), "id": j.get("id"), "tests": [], "auszug": [],
             "log_fehler": f"Log nicht geholt (Grenze {MAX_JOBS_MIT_LOG} Jobs je Aufruf)"} for j in rest]
    return aus


def _letzter_main_lauf(main_laeufe: list[dict[str, Any]], stamm: str) -> dict[str, Any] | None:
    for r in main_laeufe:  # Gitea liefert den neuesten zuerst
        if workflow_stamm(r.get("path")) == stamm and einordnen(r.get("status"), r.get("conclusion")) in {
            GRUEN,
            FEHLER,
        }:
            return r
    return None


def _gegen_main(
    holen: Holen, holen_text: Holen, cache: Any, lauf: dict[str, Any], main_laeufe: list[dict[str, Any]],
    zeilen: int, budget: list[int],
) -> None:
    """Setzt `main` am Lauf und `main` je Test/Job: vorbestehend | neu | unbekannt."""
    m = _letzter_main_lauf(main_laeufe, lauf["workflow"])
    if m is None:
        commits = len({r.get("head_sha") for r in main_laeufe})
        lauf["main"] = {
            "vergleich": "unbekannt",
            "grund": f"dieser Workflow lief auf main in den letzten {len(main_laeufe)} Läufen ({commits} Commits) nicht",
        }
        for j in lauf["jobs"]:
            j["main"] = "unbekannt"
            for t in j["tests"]:
                t["main"] = "unbekannt"
        return
    gruen = einordnen(m.get("status"), m.get("conclusion")) == GRUEN
    lauf["main"] = {"vergleich": "grün" if gruen else "rot", "sha": _kurz(m.get("head_sha") or ""), "lauf": m.get("id")}
    if gruen:
        main_jobs: list[dict[str, Any]] = []
    else:
        main_jobs = _rote_jobs(holen, holen_text, cache, m, zeilen, budget)
    main_namen = {j.get("name") for j in main_jobs}
    main_tests = {t["id"] for j in main_jobs for t in j.get("tests") or []}
    for j in lauf["jobs"]:
        if j.get("log_fehler") and not j["tests"]:
            j["main"] = "unbekannt"
            continue
        for t in j["tests"]:
            t["main"] = "vorbestehend" if t["id"] in main_tests else "neu"
        if j["tests"]:  # der Job ist so alt wie seine Tests
            urteile = {t["main"] for t in j["tests"]}
            j["main"] = urteile.pop() if len(urteile) == 1 else "teils"
        else:
            j["main"] = "vorbestehend" if j.get("name") in main_namen else "neu"


def _test_detail(jobs: list[dict[str, Any]]) -> str:
    tests = [t for j in jobs for t in j.get("tests") or []]
    if tests:
        if len(tests) == 1:
            t = tests[0]
            kurz = t["id"].split("::")[-1]
            return f"1 Test: {kurz} — {t['ursache']}" if t.get("ursache") else f"1 Test: {kurz}"
        namen = [t["id"].split("::")[-1] for t in tests]
        return f"{len(tests)} Tests: " + ", ".join(namen[:3]) + (", …" if len(namen) > 3 else "")
    if not jobs:
        return "Jobs nicht gefunden"
    j = jobs[0]
    if j.get("log_fehler"):
        return f"Job {j.get('name') or '?'}: {j['log_fehler']}"
    teile = [f"Job {j.get('name')}"]
    if j.get("schritt"):
        teile.append(str(j["schritt"]))
    if j.get("ursache"):
        teile.append(str(j["ursache"])[:140])
    text = ": ".join(teile)
    return text + (f" (+{len(jobs) - 1} weitere Jobs)" if len(jobs) > 1 else "")


def _satz(sha: str, laeufe: list[dict[str, Any]]) -> str:
    n = len(laeufe)
    if not n:
        return f"Keine Workflow-Läufe zu {_kurz(sha)} gefunden."
    z = Counter(r["ergebnis"] for r in laeufe)
    rot = [r for r in laeufe if r["ergebnis"] == FEHLER]
    extras = []
    if z[ABGEBROCHEN]:
        extras.append(f"{z[ABGEBROCHEN]} abgebrochen")
    if z[LAEUFT]:
        extras.append(f"{z[LAEUFT]} laufen")
    if z[WARTET]:
        extras.append(f"{z[WARTET]} warten")
    if rot:
        satz = f"{len(rot)} von {n} rot"
        if extras:
            satz += ", " + ", ".join(extras)
        satz += ": " + ", ".join(f"{r['name']} ({_test_detail(r.get('jobs') or [])})" for r in rot)
        urteil: dict[str, list[str]] = {"vorbestehend": [], "neu": [], "teils": []}
        for r in rot:
            if (r.get("main") or {}).get("vergleich") not in {"grün", "rot"}:
                continue  # Vergleich nicht moeglich: nichts behaupten
            js = {j.get("main") for j in r["jobs"] if not j.get("log_fehler")}
            if len(js) == 1 and next(iter(js)) in urteil:
                urteil[next(iter(js))].append(r["name"])
            elif js:
                urteil["teils"].append(r["name"])
        if urteil["vorbestehend"]:
            satz += f". Schon auf main rot: {', '.join(urteil['vorbestehend'])}"
        if urteil["teils"]:
            satz += f". Teils schon auf main rot: {', '.join(urteil['teils'])}"
        if urteil["neu"]:
            satz += f". Neu gegenüber main: {', '.join(urteil['neu'])}"
        return satz + "."
    if z[LAEUFT] or z[WARTET]:
        return f"Noch nichts rot, aber {n - z[GRUEN] - z[UEBERSPRUNGEN]} von {n} Läufen sind nicht fertig ({', '.join(extras)})."
    if z[ABGEBROCHEN]:
        namen = ", ".join(r["name"] for r in laeufe if r["ergebnis"] == ABGEBROCHEN)
        return f"Nichts rot, aber {z[ABGEBROCHEN]} von {n} abgebrochen: {namen}."
    return f"Alles grün ({n} Läufe)."


def _gesamt(laeufe: list[dict[str, Any]]) -> str:
    z = {r["ergebnis"] for r in laeufe}
    if not z:
        return "leer"
    if FEHLER in z:
        return FEHLER
    if LAEUFT in z or WARTET in z:
        return LAEUFT
    if ABGEBROCHEN in z:
        return ABGEBROCHEN
    return GRUEN


def _zuschneiden(lauf: dict[str, Any], voll: bool) -> dict[str, Any]:
    breite = 400 if voll else 200
    max_tests = 30 if voll else 5
    out = {k: v for k, v in lauf.items() if k != "jobs"}
    if not voll and lauf.get("status") == "completed":
        out.pop("status", None)  # steht schon in `ergebnis`
    jobs = []
    for j in lauf.get("jobs") or []:
        j = dict(j)
        if not voll:
            j.pop("id", None)
        tests = j.get("tests") or []
        j["tests"] = [
            {**t, "ursache": (t.get("ursache") or "")[:breite]} for t in tests[:max_tests]
        ]
        if len(tests) > max_tests:
            j["weitere_tests"] = len(tests) - max_tests
        j["auszug"] = [x[:breite] for x in j.get("auszug") or []]
        if j.get("ursache"):
            j["ursache"] = str(j["ursache"])[:breite]
        jobs.append({k: v for k, v in j.items() if v not in (None, [], "")})
    out["jobs"] = jobs
    return out


_OHNE_CI = re.compile(r"\[(?:skip ci|ci skip|no ci|skip actions|actions skip)\]", re.I)


def _nachricht(c: dict[str, Any]) -> str:
    return str((c.get("commit") or {}).get("message") or "")


def aufloesen(holen: Holen, sha: str, zweig: str) -> tuple[str, str]:
    """(voller Commit-SHA, Commit-Nachricht) zu sha (voll oder kurz) bzw. zur Spitze von zweig."""
    if sha:
        c = holen(f"/repos/{repo()}/git/commits/{urllib.parse.quote(sha, safe='')}", timeout=10)
        return str(c["sha"]), _nachricht(c)
    liste = holen(f"/repos/{repo()}/commits?sha={urllib.parse.quote(zweig, safe='')}&limit=1", timeout=10)
    if not liste:
        raise LookupError(f"Zweig {zweig!r} ohne Commits")
    return str(liste[0]["sha"]), _nachricht(liste[0])


def _letzter_mit_ci(holen: Holen, zweig: str, ohne: str) -> tuple[str, str] | None:
    """Neuester Commit des Zweigs ohne [skip ci] (davor: Spitze `ohne`), sonst None."""
    liste = holen(f"/repos/{repo()}/commits?sha={urllib.parse.quote(zweig, safe='')}&limit=30", timeout=10)
    for c in liste or []:
        if c.get("sha") != ohne and not _OHNE_CI.search(_nachricht(c)):
            return str(c["sha"]), _nachricht(c)
    return None


def eingabe_pruefen(args: dict[str, Any]) -> tuple[str, str, int, bool]:
    sha = str(args.get("sha") or "").strip()
    zweig = str(args.get("zweig") or "").strip()
    if sha and not re.fullmatch(r"[0-9a-fA-F]{4,40}", sha):
        raise ValueError(f"ci_fehler: sha {sha!r} ist keine Commit-Kennung (4-40 Hex-Zeichen)")
    if zweig and (len(zweig) > 200 or re.search(r"[\x00-\x1f\s]", zweig)):
        raise ValueError("ci_fehler: zweig ungültig")
    if not sha and not zweig:
        zweig = "main"
    try:
        zeilen = int(args.get("zeilen") or ZEILEN_VORGABE)
    except (TypeError, ValueError) as e:
        raise ValueError("ci_fehler: zeilen muss eine Zahl sein") from e
    return sha.lower(), zweig, max(ZEILEN_MIN, min(zeilen, ZEILEN_MAX)), args.get("voll") is True


def ci_fehler(
    args: dict[str, Any], holen: Holen, holen_text: Holen | None = None, cache: Any = None
) -> dict[str, Any]:
    """Warum ist die CI zu diesem Commit rot? Siehe Modul-Doku."""
    holen_text = holen_text or gitea_text
    sha_roh, zweig, zeilen, voll = eingabe_pruefen(args)
    try:
        sha, nachricht = aufloesen(holen, sha_roh, zweig)
    except urllib.error.HTTPError as e:
        wen = f"Commit {sha_roh}" if sha_roh else f"Zweig {zweig}"
        return {"fehler": f"{wen} nicht gefunden (HTTP {e.code})", "satz": f"{wen} nicht gefunden."}
    except Exception as e:  # noqa: BLE001 — Quellfehler ist Nutzdaten
        return {"fehler": f"{type(e).__name__}: {e}"[:200], "satz": "Gitea nicht erreichbar oder Zweig leer."}

    out: dict[str, Any] = {"commit": sha, "kurz": _kurz(sha), "zweig": zweig or None}
    try:
        alle = _laeufe_holen(holen, f"head_sha={sha}")
    except Exception as e:  # noqa: BLE001
        out["fehler"] = f"Läufe nicht lesbar: {type(e).__name__}: {e}"[:200]
        out["satz"] = f"Läufe zu {_kurz(sha)} nicht lesbar."
        return out
    hinweis = None
    if not alle and _OHNE_CI.search(nachricht):
        hinweis = f"Commit {_kurz(sha)} trägt [skip ci] — dazu laufen keine Workflows."
        if zweig and not sha_roh:  # Zweigspitze ohne CI: der letzte Commit mit CI sagt mehr
            try:
                davor = _letzter_mit_ci(holen, zweig, sha)
                if davor:
                    sha = davor[0]
                    hinweis += f" Gezeigt wird der letzte Commit mit CI: {_kurz(sha)}."
                    out.update(commit=sha, kurz=_kurz(sha))
                    alle = _laeufe_holen(holen, f"head_sha={sha}")
            except Exception:  # noqa: BLE001 — Zusatz, die Auskunft oben bleibt gueltig
                pass
    if hinweis:
        out["hinweis"] = hinweis
    if zweig:
        nur = [r for r in alle if r.get("head_branch") == zweig]
        alle = nur or alle
    runs = _neueste_je_workflow(alle)
    if runs and not zweig:
        out["zweig"] = Counter(r.get("head_branch") for r in runs).most_common(1)[0][0]
    try:
        out["commit_status"] = holen(f"/repos/{repo()}/commits/{sha}/status", timeout=10).get("state")
    except Exception:  # noqa: BLE001 — Zusatzangabe
        out["commit_status"] = None

    namen = _workflow_namen(holen, cache)
    laeufe: list[dict[str, Any]] = []
    for r in runs:
        stamm = workflow_stamm(r.get("path"))
        laeufe.append(
            {
                "id": r.get("id"),
                "name": _name(namen, stamm),
                "workflow": stamm,
                "zweig": r.get("head_branch"),
                "status": str(r.get("status") or ""),
                "ergebnis": einordnen(r.get("status"), r.get("conclusion")),
                "url": r.get("html_url"),
                "jobs": [],
            }
        )

    # Rote Laeufe: Jobs und Logs
    budget = [MAX_JOBS_MIT_LOG]
    for lauf, r in zip(laeufe, runs):
        if lauf["ergebnis"] == FEHLER:
            lauf["jobs"] = _rote_jobs(holen, holen_text, cache, r, zeilen, budget)

    # Abgleich mit main (nicht, wenn der Commit selbst auf main liegt)
    rote = [x for x in laeufe if x["ergebnis"] == FEHLER]
    auf_main = bool(runs) and all(r.get("head_branch") == "main" for r in runs)
    if rote and not auf_main:
        try:
            main_laeufe = _laeufe_holen(holen, "branch=main")
        except Exception:  # noqa: BLE001
            main_laeufe = []
        vergl_budget = [MAX_JOBS_MIT_LOG]
        for lauf in rote[:MAX_VERGLEICHE]:
            _gegen_main(holen, holen_text, cache, lauf, main_laeufe, zeilen, vergl_budget)

    out["gesamt"] = _gesamt(laeufe)
    out["satz"] = _satz(sha, laeufe) if (laeufe or not hinweis) else hinweis
    out["zaehler"] = {
        "gesamt": len(laeufe),
        **{k: n for k in (GRUEN, FEHLER, ABGEBROCHEN, LAEUFT, WARTET, UEBERSPRUNGEN, UNKLAR)
           if (n := sum(1 for x in laeufe if x["ergebnis"] == k))},
    }
    if voll:
        sichtbar = laeufe
    else:
        sichtbar = [x for x in laeufe if x["ergebnis"] not in {GRUEN, UEBERSPRUNGEN}]
        out["gruen"] = out["zaehler"].get(GRUEN, 0)
    out["laeufe"] = [_zuschneiden(x, voll) for x in sichtbar]
    return out
