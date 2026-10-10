"""repos — die Repositories auf einen Blick (App „Repos“, 2026-10-10).

Eine App „Repos“ (10.10.). Je Repository des Besitzers: Standardzweig, letzter
Commit, CI-Ampel, offene PRs, Paket- und Bildversion; im Detail Zweige, Commits, CI-Läufe, offene PRs und Pakete.
Quelle ist die Gitea-API wie bei lage und betrieb. Nur lesend: kein Werkzeug hier ändert etwas.

Werkzeuge (Oberfläche ui://fyos/repos.html):
    repos.uebersicht  {filter?, nur_probleme?}
        -> {quelle_ok, besitzer, repos: [{name, standardzweig, commit, ci, offene_prs, pakete, bilder, …}],
            zaehlung, satz, stand, pakete_ok, pakete_grund?}
    repos.detail      {repo}
        -> {quelle_ok, repo, zweige, zweige_gesamt, commits, laeufe, prs, pakete, bilder, fehlt, satz, stand}

CI-Ampel: der kombinierte Commit-Status des Standardzweigs (Gitea Actions schreibt je Job einen Status):
    gruen = success · rot = failure/error · gelb = pending/warning · grau = kein Status (keine CI) · unbekannt = nicht lesbar.
Hat der letzte Commit keinen Status (etwa `[skip ci]`) und das Repo Actions, kommt die Ampel aus den neuesten Läufen
auf dem Standardzweig, je Workflow der neueste (`aus_laeufen: true`).
Pakete: PyPI-Pakete mit dem Namen des Repos oder laut Zuordnung `pakete` (Paketname → Repo; Vorgabe: fyos-hub und
fyos-installer gehören zu fyos-os), dazu die Bilder `<repo>/<bild>` der Container-Registry. Die Paket-API verlangt ein
Token mit read:package: GITEA_PAKET_TOKEN, sonst GITEA_TOKEN. Fehlt das Recht, fehlen nur die Versionen
(`pakete_ok: false` mit Grund), der Rest bleibt.

Konfiguration (config: vor Umgebung vor Vorgabe): `besitzer` (FYOS_REPOS_BESITZER; Vorgabe: der Besitzer aus
GITEA_REPO) und `pakete` (Zuordnung, ergänzt die Vorgabe). Die Adresse der API ist GITEA_API_BASE wie bei lage; ohne sie
(oder ohne Besitzer) ist die App nicht eingerichtet: ein Zustand mit Weg zum Einrichten, kein Fehler.

Ein Ausfall ist nach außen kein Fehler, sondern `{quelle_ok: false, grund, satz}`. Ein falsches Argument (ein Repo, das
es nicht gibt oder das das Token nicht sieht) ist ein ValueError.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Iterable

from mcp_hub.plugin import HubPlugin, ResourceDescriptor, ToolDescriptor
from mcp_hub.plugins import _identitaet
from mcp_hub.plugins._identitaet import identitaet
from mcp_hub.plugins._oberflaeche import oberflaeche

from . import _umgebung, lage

UI_URI = "ui://fyos/repos.html"
UI_DATEI = "repos.html"
FRIST_S = 8.0
#: So lange gilt eine Übersicht bzw. ein Detail, bevor die API neu gefragt wird.
TTL_UEBERSICHT_S = 60.0
TTL_DETAIL_S = 30.0
#: Bildseiten länger: die Paketsuche ist der langsamste Abruf (Gitea beantwortet sie nacheinander, 1–6 s je Seite),
#: und Bilder ändern sich nur bei einem Bau. Ob ein Bild zum letzten Commit passt, rechnet jede Übersicht frisch.
TTL_BILDER_S = 300.0
SEITE = 50
SEITEN_MAX = 10
REPOS_MAX = 100
ZWEIGE_MAX = 30
COMMITS_MAX = 15
LAEUFE_MAX = 15
#: Läufe auf dem Standardzweig, aus denen die Ampel kommt, wenn der letzte Commit keinen Status hat.
LAEUFE_ZWEIG_MAX = 30
PRS_MAX = 20
VERSIONEN_MAX = 5
#: Bilder: die Übersicht liest die neuesten 50 Versionen je Repo, das Detail bis zu 200.
BILD_SEITEN_UEBERSICHT = 1
BILD_SEITEN_DETAIL = 4
BILDER_UEBERSICHT = 3
FILTER_MAX = 100
#: PyPI-Pakete, deren Name nicht der des Repos ist (Paketname → Repo). `pakete` in der Konfiguration ergänzt das.
PAKETE_VORGABE = {"fyos-hub": "fyos-os", "fyos-installer": "fyos-os"}
AMPEL_TEXT = {"gruen": "CI grün", "rot": "CI rot", "gelb": "CI läuft", "grau": "keine CI", "unbekannt": "CI unbekannt"}
LAUF_TEXT = {"gruen": "erfolgreich", "rot": "fehlgeschlagen", "gelb": "läuft", "grau": "abgebrochen"}
_SHA = re.compile(r"^[0-9a-f]{40}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_TAGS = ["repos", "read", "flaeche"]

# Die App anmelden (Titel, Untertitel, Symbol aus ui/_symbole.json des Hubs, Farbton, Kategorie).
_identitaet.registriere_app(
    "repos",
    {
        "titel": "Repos",
        "untertitel": "Zweige, CI und offene PRs je Repository",
        "symbol": "git-branch",
        "ton": 18,
        "kategorie": "Betrieb",
    },
    datei=UI_DATEI,
    hauptwerkzeug="repos__uebersicht",
)


class GiteaFehler(RuntimeError):
    """Antwort der Gitea-API, die keine Daten ist; `status` 0 heißt: nicht erreichbar."""

    def __init__(self, status: int, text: str):
        super().__init__(text)
        self.status = status


# ── Kleine Helfer (ohne Netz, einzeln getestet) ─────────────────────────────────────────────────


def _jetzt_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def _zeit(roh: Any) -> dt.datetime | None:
    if not roh:
        return None
    try:
        d = dt.datetime.fromisoformat(str(roh).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=dt.timezone.utc)


def _lauf_zeit(roh: Any) -> dt.datetime | None:
    """Zeit eines Laufs; Gitea setzt bei Läufen, die nie gestartet sind (abgebrochen in der Warteschlange), 1970 ein."""
    d = _zeit(roh)
    return d if d and d.year >= 2000 else None


def _ts(roh: Any) -> float:
    d = _zeit(roh)
    return d.timestamp() if d else 0.0


def vor_text(roh: Any, jetzt: dt.datetime | None = None) -> str:
    """„vor 5 Min.“ für den Satz an das Modell (die Oberfläche rechnet selbst)."""
    d = _zeit(roh)
    if d is None or d.year < 1971:
        return "nie"
    s = ((jetzt or dt.datetime.now(dt.timezone.utc)) - d).total_seconds()
    if s < 90:
        return "gerade eben"
    if s < 3600:
        return f"vor {round(s / 60)} Min."
    if s < 86400:
        return f"vor {round(s / 3600)} Std."
    tage = round(s / 86400)
    return "vor 1 Tag" if tage == 1 else f"vor {tage} Tagen"


def erste_zeile(text: Any, laenge: int = 120) -> str:
    zeile = str(text or "").strip().split("\n", 1)[0].strip()
    return zeile if len(zeile) <= laenge else zeile[: laenge - 1].rstrip() + "…"


def _web_basis(html_url: str) -> str:
    """https://git.example/besitzer/repo → https://git.example"""
    teile = urllib.parse.urlsplit(html_url or "")
    return f"{teile.scheme}://{teile.netloc}" if teile.scheme and teile.netloc else ""


def _absolut(url: Any, basis: str) -> str | None:
    """Gitea liefert die Ziele der Commit-Status als Pfad (/besitzer/repo/actions/runs/…)."""
    u = str(url or "")
    if u.startswith(("http://", "https://")):
        return u
    return basis + u if basis and u.startswith("/") else None


def ci_ampel(status: dict | None, basis: str = "") -> dict[str, Any]:
    """Kombinierter Commit-Status (GET /repos/{b}/{r}/commits/{ref}/status) → Ampel mit Text und Ziel-Adresse."""
    if not isinstance(status, dict):
        return {"ampel": "unbekannt", "text": AMPEL_TEXT["unbekannt"], "zustand": None, "jobs": 0,
                "fehlgeschlagen": [], "laufen": [], "url": None}
    einzeln = [s for s in status.get("statuses") or [] if isinstance(s, dict)]
    zustand = str(status.get("state") or "")
    if not einzeln:
        ampel = "grau"
    elif zustand == "success":
        ampel = "gruen"
    elif zustand in ("failure", "error"):
        ampel = "rot"
    else:  # pending, warning
        ampel = "gelb"
    rot = [s for s in einzeln if s.get("status") in ("failure", "error")]
    laufen = [s for s in einzeln if s.get("status") == "pending"]
    ziel = (rot or laufen or einzeln or [{}])[0]
    return {
        "ampel": ampel,
        "text": "CI mit Warnung" if zustand == "warning" else AMPEL_TEXT[ampel],
        "zustand": zustand or None,
        "jobs": len(einzeln),
        "fehlgeschlagen": [str(s.get("context") or "")[:80] for s in rot][:8],
        "laufen": [str(s.get("context") or "")[:80] for s in laufen][:8],
        "url": _absolut(ziel.get("target_url"), basis),
    }


def commit_aus_zweig(zweig: Any) -> dict[str, Any] | None:
    """Letzter Commit aus GET /repos/{b}/{r}/branches/{zweig} (oder einem Eintrag der Zweigliste)."""
    c = zweig.get("commit") if isinstance(zweig, dict) else None
    if not isinstance(c, dict) or not c.get("id"):
        return None
    autor = c.get("author") or {}
    sha = str(c["id"])
    return {"sha": sha, "kurz": sha[:7], "titel": erste_zeile(c.get("message")),
            "autor": autor.get("name") or autor.get("username") or "", "zeit": c.get("timestamp"), "url": c.get("url")}


def commit_aus_liste(e: dict) -> dict[str, Any]:
    """Ein Eintrag aus GET /repos/{b}/{r}/commits."""
    c = e.get("commit") or {}
    autor = c.get("author") or {}
    sha = str(e.get("sha") or "")
    return {"sha": sha, "kurz": sha[:7], "titel": erste_zeile(c.get("message")),
            "autor": autor.get("name") or (e.get("author") or {}).get("login") or "",
            "zeit": autor.get("date"), "url": e.get("html_url")}


def ref_text(pfad: Any) -> tuple[str, str]:
    """`ci.yml@refs/heads/main` → ("ci.yml", "main"); `…@refs/pull/2/head` → (…, "PR #2"); Tags ohne Präfix."""
    workflow, _, ref = str(pfad or "").partition("@")
    if ref.startswith("refs/heads/"):
        ref = ref[len("refs/heads/"):]
    elif ref.startswith("refs/tags/"):
        ref = ref[len("refs/tags/"):]
    elif m := re.match(r"^refs/pull/(\d+)/", ref):
        ref = f"PR #{m.group(1)}"
    return workflow, ref


def lauf_kurz(lauf: dict) -> dict[str, Any]:
    """Ein Lauf aus GET /repos/{b}/{r}/actions/runs."""
    status = str(lauf.get("status") or "")
    ergebnis = lauf.get("conclusion") or None
    if status != "completed":
        ampel, text = "gelb", ("wartet" if status in ("queued", "waiting", "blocked") else "läuft")
    elif ergebnis == "success":
        ampel, text = "gruen", LAUF_TEXT["gruen"]
    elif ergebnis in ("failure", "timed_out", "startup_failure"):
        ampel, text = "rot", LAUF_TEXT["rot"]
    else:
        ampel, text = "grau", ("übersprungen" if ergebnis == "skipped" else LAUF_TEXT["grau"])
    start, ende = _lauf_zeit(lauf.get("started_at")), _lauf_zeit(lauf.get("completed_at"))
    dauer = int((ende - start).total_seconds()) if start and ende and ende >= start else None
    workflow, ref = ref_text(lauf.get("path"))
    sha = str(lauf.get("head_sha") or "")
    return {"id": lauf.get("id"), "nummer": lauf.get("run_number"), "titel": erste_zeile(lauf.get("display_title"), 100),
            "workflow": workflow, "ref": ref or str(lauf.get("head_branch") or ""), "ereignis": lauf.get("event") or "",
            "sha": sha, "kurz": sha[:7], "status": status, "ergebnis": ergebnis, "ampel": ampel, "text": text,
            "start": lauf.get("started_at") if start else None, "dauer_s": dauer, "url": lauf.get("html_url")}


def schlimmste_ampel(ampeln: Iterable[str]) -> str:
    """Rot vor gelb vor grün; sonst grau (abgebrochen, übersprungen)."""
    werte = set(ampeln)
    return next((a for a in ("rot", "gelb", "gruen") if a in werte), "grau")


def ci_aus_laeufen(laeufe: list[dict], zweig: str) -> dict[str, Any] | None:
    """Ampel aus den Läufen auf dem Standardzweig, wenn der letzte Commit keinen Status hat (etwa `[skip ci]`).

    Je Workflow zählt sein neuester Lauf auf `zweig` (Push oder Zeitplan); rot vor gelb vor grün. None ohne Lauf."""
    je_workflow: dict[str, dict] = {}
    for lauf in laeufe:  # neueste zuerst
        if lauf["ref"] == zweig and lauf["workflow"] not in je_workflow:
            je_workflow[lauf["workflow"]] = lauf
    if not je_workflow:
        return None
    werte = list(je_workflow.values())
    rot = [x for x in werte if x["ampel"] == "rot"]
    gelb = [x for x in werte if x["ampel"] == "gelb"]
    ampel = schlimmste_ampel(x["ampel"] for x in werte)
    ziel = (rot or gelb or werte)[0]
    return {
        "ampel": ampel, "text": "CI abgebrochen" if ampel == "grau" else AMPEL_TEXT[ampel], "zustand": None,
        "jobs": len(werte), "fehlgeschlagen": [x["workflow"] for x in rot][:8], "laufen": [x["workflow"] for x in gelb][:8],
        "url": ziel.get("url"), "aus_laeufen": True,
    }


def pr_kurz(pr: dict) -> dict[str, Any]:
    """Ein offener Pull-Request aus GET /repos/{b}/{r}/pulls. Ist der Quellzweig gelöscht, nennt Gitea `refs/pull/N/head`."""
    von = str((pr.get("head") or {}).get("ref") or "")
    return {"nummer": pr.get("number"), "titel": erste_zeile(pr.get("title"), 140),
            "autor": (pr.get("user") or {}).get("login") or "",
            "von": "" if von.startswith("refs/pull/") else von, "zweig_geloescht": von.startswith("refs/pull/"),
            "nach": (pr.get("base") or {}).get("ref") or "", "aktualisiert": pr.get("updated_at"),
            "entwurf": bool(pr.get("draft")), "mergebar": pr.get("mergeable"), "kommentare": pr.get("comments") or 0,
            "labels": [str(lb.get("name")) for lb in pr.get("labels") or [] if isinstance(lb, dict)][:6],
            "url": pr.get("html_url")}


def _neueste(versionen: list[dict]) -> list[dict]:
    return sorted(versionen, key=lambda v: str(v.get("created_at") or ""), reverse=True)


def pakete_fuer(repo: str, versionen: list[Any], zuordnung: dict[str, str]) -> list[dict[str, Any]]:
    """PyPI-Versionen (Paket-API, type=pypi) → je Paket dieses Repos die neueste Version und die letzten davor.

    Zu einem Repo gehört ein Paket laut `zuordnung`, sonst laut Verknüpfung in Gitea, sonst bei gleichem Namen."""
    gruppen: dict[str, list[dict]] = {}
    for v in versionen:
        if not isinstance(v, dict):
            continue
        name = str(v.get("name") or "")
        verknuepft = v.get("repository").get("name") if isinstance(v.get("repository"), dict) else None
        if (zuordnung.get(name) or verknuepft or name) == repo:
            gruppen.setdefault(name, []).append(v)
    out = []
    for name, vs in sorted(gruppen.items()):
        vs = _neueste(vs)
        out.append({"name": name, "art": "pypi", "version": vs[0].get("version"), "zeit": vs[0].get("created_at"),
                    "url": vs[0].get("html_url"), "anzahl": len(vs),
                    "versionen": [{"version": v.get("version"), "zeit": v.get("created_at")} for v in vs[:VERSIONEN_MAX]]})
    return out


def bilder_fuer(repo: str, versionen: list[Any], head_sha: str | None = None) -> list[dict[str, Any]]:
    """Container-Versionen (type=container) → je Bild `<repo>/<name>` der neueste Stand.

    Gebaute Bilder tragen den Commit als Tag (40 Hex) und dazu Namen wie `main`; gezeigt wird der neueste Commit-Tag,
    ohne einen der neueste Tag überhaupt. `aktuell`: gebaut aus dem letzten Commit des Standardzweigs."""
    praefix = repo + "/"
    gruppen: dict[str, list[dict]] = {}
    for v in versionen:
        if isinstance(v, dict) and str(v.get("name") or "").startswith(praefix):
            gruppen.setdefault(str(v["name"]), []).append(v)
    out = []
    for name, vs in gruppen.items():
        vs = _neueste(vs)
        commit = next((v for v in vs if _SHA.match(str(v.get("version") or ""))), None)
        neu = commit or vs[0]
        tag = str(neu.get("version") or "")
        namen = [str(v.get("version")) for v in vs
                 if not _SHA.match(str(v.get("version") or "")) and not str(v.get("version") or "").startswith("sha256:")]
        # Namen wie `main` wandern mit und stehen schon in `tags`; als Verlauf zählen dann nur die Commit-Tags.
        verlauf = [v for v in vs if _SHA.match(str(v.get("version") or ""))] if commit else vs
        out.append({
            "name": name, "kurz_name": name[len(praefix):], "art": "container",
            "tag": tag[:7] if commit else tag, "commit": tag if commit else None, "zeit": neu.get("created_at"),
            "tags": list(dict.fromkeys(namen))[:4],
            "aktuell": (tag == head_sha) if (commit and head_sha) else None,
            "url": neu.get("html_url"), "anzahl": len(vs),
            "versionen": [{"version": (str(v.get("version"))[:7] if _SHA.match(str(v.get("version") or "")) else v.get("version")),
                           "zeit": v.get("created_at")} for v in verlauf[:VERSIONEN_MAX]],
        })
    out.sort(key=lambda b: str(b["zeit"] or ""), reverse=True)
    return out


def zaehlen(repos: list[dict]) -> dict[str, int]:
    z = {"repos": len(repos), "gruen": 0, "rot": 0, "gelb": 0, "grau": 0, "unbekannt": 0, "prs": 0}
    for r in repos:
        z[r["ci"]["ampel"]] += 1
        z["prs"] += int(r.get("offene_prs") or 0)
    return z


def _prs_text(n: int) -> str:
    return "1 offener PR" if n == 1 else f"{n} offene PRs"


def satz_uebersicht(besitzer: str, repos: list[dict], z: dict[str, int]) -> str:
    if not repos:
        return f"Keine Repos von {besitzer} sichtbar (sieht das Token sie?)."
    rot = [r["name"] for r in repos if r["ci"]["ampel"] == "rot"]
    ci = []
    if z["gruen"]:
        ci.append(f"{z['gruen']} grün")
    if rot:
        ci.append(f"{len(rot)} rot ({', '.join(rot[:3])}{', …' if len(rot) > 3 else ''})")
    if z["gelb"]:
        ci.append(f"{z['gelb']} läuft")
    if z["grau"]:
        ci.append(f"{z['grau']} ohne CI")
    if z["unbekannt"]:
        ci.append(f"{z['unbekannt']} nicht lesbar")
    return f"{z['repos']} Repos von {besitzer} · CI: {', '.join(ci)} · {_prs_text(z['prs'])}."


def satz_detail(kopf: dict, zweige_gesamt: int, laeufe: list[dict]) -> str:
    c = kopf.get("commit")
    teile = [f"{kopf['name']}: {kopf['standardzweig']}"
             + (f" bei {c['kurz']} ({vor_text(c['zeit'])})" if c else " ohne Commit")]
    teile.append(kopf["ci"]["text"] + (" (aus den letzten Läufen)" if kopf["ci"].get("aus_laeufen") else ""))
    teile.append(_prs_text(int(kopf.get("offene_prs") or 0)))
    teile.append("1 Zweig" if zweige_gesamt == 1 else f"{zweige_gesamt} Zweige")
    rot = next((lauf for lauf in laeufe if lauf["ampel"] == "rot"), None)
    if laeufe and laeufe[0]["ampel"] == "rot" and rot:
        teile.append(f"letzter Lauf fehlgeschlagen ({rot['workflow']} auf {rot['ref']})")
    return " · ".join(teile) + "."


# ── Plugin ──────────────────────────────────────────────────────────────────────────────────────


class ReposPlugin(HubPlugin):
    name = "repos"
    description = (
        "Repositories auf einen Blick: je Repo Standardzweig, letzter Commit, CI-Ampel, offene PRs, Paket- und "
        "Bildversion; im Detail Zweige, Commits, CI-Läufe, PRs und Pakete. Nur lesend (Gitea-API)."
    )
    version = "0.1.0"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        self.config = dict(config or {})
        #: Tests: (pfad ab /api/v1 mit Abfrage, paket) → JSON-Antwort der Gitea-API
        self._api: Callable[[str, bool], Any] | None = None
        self._cache = lage._Zwischenspeicher()
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="repos")
        #: Weitere Bildseiten eines Detail-Auftrags; eigener Pool, weil der Auftrag selbst schon in `_pool` läuft.
        self._seiten = ThreadPoolExecutor(max_workers=BILD_SEITEN_DETAIL, thread_name_prefix="repos-seiten")

    # ── Lebenslauf und Konfiguration ─────────────────────────────────────────
    def register(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        if self._api is None and not _umgebung.lesen("GITEA_API_BASE"):
            return {"ok": True, "status": _umgebung.NICHT_EINGERICHTET, "einrichten": self._EINRICHTEN}
        # Nur ob die Geheimnisse da sind, nie ihr Wert.
        return {"ok": True, "status": "ok", "besitzer": self.besitzer,
                "token": bool(os.getenv("GITEA_TOKEN")), "paket_token": bool(os.getenv("GITEA_PAKET_TOKEN"))}

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
        self._seiten.shutdown(wait=False, cancel_futures=True)

    _EINRICHTEN = "Setze GITEA_API_BASE (z. B. https://git.example.org/api/v1), GITEA_TOKEN und GITEA_REPO oder FYOS_REPOS_BESITZER (wessen Repos die App zeigt)."

    @property
    def besitzer(self) -> str:
        return str(
            self.config.get("besitzer") or os.getenv("FYOS_REPOS_BESITZER") or _umgebung.lesen("GITEA_REPO").split("/")[0]
        ).strip()

    @property
    def zuordnung(self) -> dict[str, str]:
        z = dict(PAKETE_VORGABE)
        eigen = self.config.get("pakete")
        if isinstance(eigen, dict):
            z.update({str(k): str(v) for k, v in eigen.items()})
        return z

    # ── Gitea ────────────────────────────────────────────────────────────────
    def _hole(self, pfad: str, paket: bool = False) -> Any:
        if self._api is not None:
            return self._api(pfad, paket)
        token = (os.getenv("GITEA_PAKET_TOKEN", "") if paket else "") or os.getenv("GITEA_TOKEN", "")
        kopf = {"Accept": "application/json"}
        if token:
            kopf["Authorization"] = f"token {token}"
        req = urllib.request.Request(f"{lage.gitea_api()}{pfad}", headers=kopf)
        try:
            with urllib.request.urlopen(req, timeout=FRIST_S) as r:
                roh = r.read().decode()
        except urllib.error.HTTPError as e:
            raise GiteaFehler(e.code, f"Gitea antwortet {e.code} auf {pfad.split('?')[0]}") from e
        except (urllib.error.URLError, OSError) as e:
            raise GiteaFehler(0, f"Gitea nicht erreichbar ({type(e).__name__})") from e
        try:
            return json.loads(roh or "null")
        except ValueError as e:  # ein ValueError hieße für den Hub „falsches Argument“
            raise GiteaFehler(0, f"Gitea antwortet kein JSON auf {pfad.split('?')[0]}") from e

    def _alle(self, pfad: str, paket: bool = False, seiten: int = SEITEN_MAX) -> list[dict]:
        trenner = "&" if "?" in pfad else "?"
        out: list[dict] = []
        for seite in range(1, seiten + 1):
            teil = self._hole(f"{pfad}{trenner}limit={SEITE}&page={seite}", paket)
            if not isinstance(teil, list):
                break
            out.extend(t for t in teil if isinstance(t, dict))
            if len(teil) < SEITE:
                break
        return out

    @staticmethod
    def _q(text: str) -> str:
        return urllib.parse.quote(text, safe="")

    @staticmethod
    def _paket_grund(e: GiteaFehler) -> str:
        if e.status in (401, 403):
            return "Paketversionen fehlen: das Token braucht read:package (GITEA_PAKET_TOKEN)."
        return f"Paketversionen fehlen: {e}"

    @staticmethod
    def _versuch(f: Callable[[], Any]) -> tuple[Any, str | None]:
        try:
            return f(), None
        except GiteaFehler as e:
            return None, str(e)[:160]
        except Exception as e:  # noqa: BLE001 — ein Teil weg heißt nicht „nichts da“
            return None, f"{type(e).__name__}: {e}"[:160]

    def _repos(self, besitzer: str) -> list[dict]:
        b = self._q(besitzer)
        try:
            return self._alle(f"/users/{b}/repos")
        except GiteaFehler as e:
            if e.status != 404:
                raise
        try:
            return self._alle(f"/orgs/{b}/repos")
        except GiteaFehler as e:
            # Gitea antwortet auf eine unbekannte Organisation mit 403 statt 404 – ohne Nutzer heißt beides: kein Konto.
            if e.status not in (403, 404):
                raise
            raise GiteaFehler(404, f"Gitea kennt weder einen Nutzer noch eine Organisation „{besitzer}“ "
                                   "(oder das Token sieht sie nicht)") from e

    def _pypi(self, besitzer: str) -> tuple[list[dict], str | None]:
        try:
            versionen = self._cache.holen(f"pypi:{besitzer}", TTL_BILDER_S,
                                          lambda: self._alle(f"/packages/{self._q(besitzer)}?type=pypi", paket=True))
        except GiteaFehler as e:  # nicht gemerkt: der nächste Aufruf fragt neu
            return [], self._paket_grund(e)
        return versionen, None

    def _bild_seite(self, besitzer: str, repo: str, seite: int) -> list[dict]:
        """Eine Seite Container-Versionen unter `<repo>/`, je Seite gemerkt (TTL_BILDER_S): das Detail übernimmt die
        erste Seite aus der Übersicht, und die Aktualisierung im Minutentakt fragt die Paketsuche nicht jedes Mal."""
        pfad = f"/packages/{self._q(besitzer)}?type=container&q={self._q(repo + '/')}&limit={SEITE}&page={seite}"

        def laden() -> list[dict]:
            teil = self._hole(pfad, True)
            return [t for t in teil if isinstance(t, dict)] if isinstance(teil, list) else []

        return self._cache.holen(f"bilder:{besitzer}/{repo}:{seite}", TTL_BILDER_S, laden)

    def _bilder_roh(self, besitzer: str, repo: str, seiten: int) -> tuple[list[dict], bool]:
        """Die neuesten Container-Versionen des Repos, bis zu `seiten` Seiten; dazu, ob es vermutlich mehr gibt.

        Ist die erste Seite voll, kommen die weiteren nebeneinander. Rückt zwischen zwei gemerkten Seiten eine neue
        Version nach, stünde eine Version doppelt da: entdoppelt nach Name und Tag."""
        erste = self._bild_seite(besitzer, repo, 1)
        if seiten <= 1 or len(erste) < SEITE:
            return erste, len(erste) >= SEITE
        weitere = list(self._seiten.map(lambda s: self._bild_seite(besitzer, repo, s), range(2, seiten + 1)))
        out: list[dict] = []
        gesehen: set[tuple[Any, Any]] = set()
        voll = True
        for teil in [erste, *weitere]:
            for v in teil:
                if (v.get("name"), v.get("version")) not in gesehen:
                    gesehen.add((v.get("name"), v.get("version")))
                    out.append(v)
            voll = len(teil) >= SEITE
            if not voll:
                break
        return out, voll

    def _laeufe_zweig(self, b: str, n: str, zweig: str) -> list[dict]:
        roh = self._hole(f"/repos/{b}/{n}/actions/runs?branch={self._q(zweig)}&limit={LAEUFE_ZWEIG_MAX}")
        return [lauf_kurz(x) for x in (roh or {}).get("workflow_runs") or [] if isinstance(x, dict)]

    def _ci(self, b: str, n: str, zweig: str, basis: str, status: Any, mit_actions: bool) -> dict[str, Any]:
        """CI-Ampel des Standardzweigs: der kombinierte Status des letzten Commits. Hat der keinen (etwa `[skip ci]`),
        kommt sie aus den neuesten Läufen auf dem Zweig; klappt das nicht, bleibt es bei „keine CI“."""
        ci = ci_ampel(status, basis)
        if ci["ampel"] != "grau" or not mit_actions:
            return ci
        try:
            return ci_aus_laeufen(self._laeufe_zweig(b, n, zweig), zweig) or ci
        except GiteaFehler:
            return ci

    # ── repos.uebersicht ─────────────────────────────────────────────────────
    def _zeile(self, besitzer: str, r: dict, pypi: list[dict], pakete_ok: bool) -> dict[str, Any]:
        name = str(r.get("name") or "")
        zweig = str(r.get("default_branch") or "main")
        basis = _web_basis(str(r.get("html_url") or ""))
        b, n, z = self._q(besitzer), self._q(name), self._q(zweig)
        zeile: dict[str, Any] = {
            "name": name, "beschreibung": erste_zeile(r.get("description"), 200), "privat": bool(r.get("private")),
            "archiviert": bool(r.get("archived")), "leer": bool(r.get("empty")), "spiegel": bool(r.get("mirror")),
            "standardzweig": zweig, "zweige": r.get("branch_count"), "offene_prs": int(r.get("open_pr_counter") or 0),
            "aktualisiert": r.get("updated_at"), "url": r.get("html_url"),
            "commit": None, "ci": ci_ampel(None), "pakete": [], "bilder": [], "bilder_gesamt": 0,
        }
        if zeile["leer"]:
            zeile["ci"] = {**ci_ampel({"statuses": []}), "text": "leeres Repo"}
            return zeile
        fehlt: dict[str, str] = {}
        # Innerhalb einer Zeile nacheinander: die Zeilen selbst laufen schon nebeneinander im Pool.
        zw, grund = self._versuch(lambda: self._hole(f"/repos/{b}/{n}/branches/{z}"))
        if grund:
            fehlt["commit"] = grund
        else:
            zeile["commit"] = commit_aus_zweig(zw)
        st, grund = self._versuch(lambda: self._hole(f"/repos/{b}/{n}/commits/{z}/status"))
        if grund:
            fehlt["ci"] = grund
        else:
            zeile["ci"] = self._ci(b, n, zweig, basis, st, bool(r.get("has_actions")))
        zeile["pakete"] = pakete_fuer(name, pypi, self.zuordnung)
        if pakete_ok:
            try:
                roh, _ = self._bilder_roh(besitzer, name, BILD_SEITEN_UEBERSICHT)
            except GiteaFehler as e:
                fehlt["bilder"] = self._paket_grund(e)
            else:
                bilder = bilder_fuer(name, roh, (zeile["commit"] or {}).get("sha"))
                zeile["bilder"], zeile["bilder_gesamt"] = bilder[:BILDER_UEBERSICHT], len(bilder)
        if fehlt:
            zeile["fehlt"] = fehlt
        return zeile

    def _uebersicht_laden(self, besitzer: str) -> dict[str, Any]:
        roh = self._repos(besitzer)[:REPOS_MAX]
        pypi, pakete_grund = self._pypi(besitzer)
        laeufe = [self._pool.submit(self._zeile, besitzer, r, pypi, pakete_grund is None) for r in roh]
        repos = [lauf.result() for lauf in laeufe]
        # Zuletzt Bewegtes zuerst, Archiviertes ans Ende.
        repos.sort(key=lambda z: (z["archiviert"], -_ts((z["commit"] or {}).get("zeit") or z["aktualisiert"])))
        z = zaehlen(repos)
        out: dict[str, Any] = {
            "quelle_ok": True, "besitzer": besitzer, "repos": repos, "zaehlung": z,
            "satz": satz_uebersicht(besitzer, repos, z), "stand": _jetzt_iso(),
            "pakete_ok": pakete_grund is None,
        }
        if pakete_grund:
            out["pakete_grund"] = pakete_grund
        return out

    def uebersicht(self, filter_text: Any = None, nur_probleme: bool = False) -> dict[str, Any]:
        if filter_text is not None and not isinstance(filter_text, str):
            raise ValueError("filter muss ein Text sein")
        q = " ".join((filter_text or "").split()).lower()
        if len(q) > FILTER_MAX:
            raise ValueError(f"filter ist zu lang (höchstens {FILTER_MAX} Zeichen)")
        besitzer = self.besitzer
        if not besitzer:
            return _umgebung.zustand("Die App Repos", self._EINRICHTEN)
        try:
            daten = self._cache.holen(f"uebersicht:{besitzer}", TTL_UEBERSICHT_S, lambda: self._uebersicht_laden(besitzer))
        except GiteaFehler as e:
            return {"quelle_ok": False, "besitzer": besitzer, "grund": str(e)[:200],
                    "satz": "Die Repos sind gerade nicht abrufbar.", "stand": _jetzt_iso()}
        repos = daten["repos"]
        if q:
            repos = [r for r in repos if q in r["name"].lower() or q in r["beschreibung"].lower()]
        if nur_probleme:
            repos = [r for r in repos if r["ci"]["ampel"] in ("rot", "unbekannt")]
        return {**daten, "repos": repos, "filter": q or None, "nur_probleme": bool(nur_probleme)}

    # ── repos.detail ─────────────────────────────────────────────────────────
    def _detail_laden(self, besitzer: str, name: str) -> dict[str, Any]:
        b, n = self._q(besitzer), self._q(name)
        r = self._hole(f"/repos/{b}/{n}")
        if not isinstance(r, dict):
            raise GiteaFehler(0, f"Gitea liefert für {besitzer}/{name} kein Repo")
        zweig = str(r.get("default_branch") or "main")
        z = self._q(zweig)
        basis = _web_basis(str(r.get("html_url") or ""))
        leer = bool(r.get("empty"))
        auftraege: dict[str, Callable[[], Any]] = {
            "prs": lambda: self._hole(f"/repos/{b}/{n}/pulls?state=open&sort=recentupdate&limit={PRS_MAX}"),
            "laeufe": lambda: self._hole(f"/repos/{b}/{n}/actions/runs?limit={LAEUFE_MAX}"),
        }
        if not leer:
            # Gitea liefert die Zweige zuletzt bewegt zuerst; jeder kostet einen Git-Zugriff (fyos: 50 Zweige ≈ 2–4 s).
            # Also nur so viele, wie die Ansicht zeigt, den Standardzweig einzeln dazu, die Zahl aus `branch_count`.
            auftraege["zweige"] = lambda: self._hole(f"/repos/{b}/{n}/branches?limit={ZWEIGE_MAX}&page=1")
            auftraege["standard"] = lambda: self._hole(f"/repos/{b}/{n}/branches/{z}")
            auftraege["commits"] = lambda: self._hole(
                f"/repos/{b}/{n}/commits?sha={z}&limit={COMMITS_MAX}&stat=false&verification=false&files=false")
            auftraege["ci"] = lambda: self._hole(f"/repos/{b}/{n}/commits/{z}/status")
            if r.get("has_actions"):
                auftraege["laeufe_zweig"] = lambda: self._laeufe_zweig(b, n, zweig)
        pypi, pakete_grund = self._pypi(besitzer)
        if pakete_grund is None:
            def bilder_laden() -> tuple[list[dict], bool]:
                try:
                    return self._bilder_roh(besitzer, name, BILD_SEITEN_DETAIL)
                except GiteaFehler as e:
                    raise GiteaFehler(e.status, self._paket_grund(e)) from e

            auftraege["bilder"] = bilder_laden
        laeufe_im_pool = {k: self._pool.submit(self._versuch, f) for k, f in auftraege.items()}
        erg = {k: lauf.result() for k, lauf in laeufe_im_pool.items()}
        fehlt: dict[str, str] = {}

        def nimm(k: str, art: type, vorgabe: Any) -> Any:
            """Ergebnis eines Auftrags in der erwarteten Form; ein Fehler landet in `fehlt`."""
            wert, grund = erg.get(k, (None, None))
            if grund:
                fehlt[k] = grund
            return wert if isinstance(wert, art) else vorgabe

        laeufe = [lauf_kurz(x) for x in nimm("laeufe", dict, {}).get("workflow_runs") or [] if isinstance(x, dict)]
        laeufe_zweig = nimm("laeufe_zweig", list, [])
        fehlt.pop("laeufe_zweig", None)  # nur Zugabe für die Ampel, kein eigener Teil der Ansicht
        # Je Commit zählt der neueste Lauf jedes Workflows und Ereignisses (Push und PR-Prüfung getrennt), rot vor gelb
        # vor grün. Gitea zählt die Lauf-IDs hoch; ein Lauf, der in beiden Listen steht, schadet nicht.
        je_commit: dict[str, dict[tuple[str, str], str]] = {}
        for lauf in sorted(laeufe + laeufe_zweig, key=lambda x: x["id"] if isinstance(x["id"], int) else -1, reverse=True):
            if lauf["sha"]:
                je_commit.setdefault(lauf["sha"], {}).setdefault((lauf["workflow"], lauf["ereignis"]), lauf["ampel"])
        commits = []
        for e in nimm("commits", list, []):
            if isinstance(e, dict):
                c = commit_aus_liste(e)
                c["ci"] = schlimmste_ampel(je_commit[c["sha"]].values()) if c["sha"] in je_commit else None
                commits.append(c)
        roh_zweige = [x for x in nimm("zweige", list, []) if isinstance(x, dict)]
        standard = nimm("standard", dict, None)
        fehlt.pop("standard", None)  # nur Zugabe: ohne ihn kommt der Kopf-Commit aus der Commit-Liste
        if standard and not any(x.get("name") == zweig for x in roh_zweige):
            roh_zweige.append(standard)
        zweige = [{"name": str(x.get("name") or ""), "standard": x.get("name") == zweig,
                   "geschuetzt": bool(x.get("protected")), "commit": commit_aus_zweig(x)} for x in roh_zweige]
        zweige.sort(key=lambda x: (not x["standard"], -_ts((x["commit"] or {}).get("zeit"))))
        anzahl = r.get("branch_count")
        zweige_gesamt = max(anzahl, len(zweige)) if isinstance(anzahl, int) and not isinstance(anzahl, bool) else len(zweige)
        kopf_commit = next((x["commit"] for x in zweige if x["standard"]), None) or (commits[0] if commits else None)
        if kopf_commit:
            kopf_commit = {k: v for k, v in kopf_commit.items() if k != "ci"}
        st = nimm("ci", dict, None)
        bilder_roh, bilder_mehr = nimm("bilder", tuple, ([], False))
        if leer:
            ci = {**ci_ampel({"statuses": []}), "text": "leeres Repo"}
        elif "ci" in fehlt:
            ci = ci_ampel(None)
        else:
            ci = ci_ampel(st, basis)
            if ci["ampel"] == "grau":
                ci = ci_aus_laeufen(laeufe_zweig, zweig) or ci
        # Der Kopf-Commit trägt die Ampel aus dem Kopf der Ansicht, wie Gitea ihn selbst markiert: der kombinierte Status
        # kennt auch PR-Prüfungen, deren Läufe in keiner der beiden Listen stehen.
        if kopf_commit and ci["ampel"] in ("gruen", "gelb", "rot"):
            for c in commits:
                if c["sha"] == kopf_commit["sha"]:
                    c["ci"] = ci["ampel"]
        kopf = {
            "name": name, "besitzer": besitzer, "beschreibung": erste_zeile(r.get("description"), 300),
            "privat": bool(r.get("private")), "archiviert": bool(r.get("archived")), "leer": leer,
            "standardzweig": zweig, "url": r.get("html_url"), "offene_prs": int(r.get("open_pr_counter") or 0),
            "aktualisiert": r.get("updated_at"), "commit": kopf_commit, "ci": ci,
        }
        prs = [pr_kurz(p) for p in nimm("prs", list, []) if isinstance(p, dict)][:PRS_MAX]
        out: dict[str, Any] = {
            "quelle_ok": True, "repo": kopf,
            "zweige": zweige[:ZWEIGE_MAX], "zweige_gesamt": zweige_gesamt,
            "commits": commits, "laeufe": laeufe, "prs": prs,
            "pakete": pakete_fuer(name, pypi, self.zuordnung),
            "bilder": bilder_fuer(name, bilder_roh, (kopf_commit or {}).get("sha")),
            "pakete_ok": pakete_grund is None,
            "satz": satz_detail(kopf, zweige_gesamt, laeufe), "stand": _jetzt_iso(),
        }
        if bilder_mehr:
            out["bilder_hinweis"] = f"Bilder aus den {len(bilder_roh)} neuesten Versionen"
        if pakete_grund:
            out["pakete_grund"] = pakete_grund
        if fehlt:
            out["fehlt"] = fehlt
        return out

    def detail(self, repo: Any) -> dict[str, Any]:
        if not isinstance(repo, str) or not repo.strip():
            raise ValueError("repo fehlt (Name des Repositorys, oder besitzer/name)")
        besitzer, _, name = repo.strip().rpartition("/")
        besitzer = besitzer or self.besitzer
        if not _NAME.match(name) or not _NAME.match(besitzer or ""):
            raise ValueError(f"Kein gültiger Repo-Name: {repo.strip()[:120]!r}")
        try:
            return self._cache.holen(f"detail:{besitzer}/{name}", TTL_DETAIL_S, lambda: self._detail_laden(besitzer, name))
        except GiteaFehler as e:
            if e.status == 404:
                raise ValueError(f"Repo {besitzer}/{name} gibt es nicht, oder das Token sieht es nicht.") from e
            return {"quelle_ok": False, "repo": {"name": name, "besitzer": besitzer}, "grund": str(e)[:200],
                    "satz": f"{name} ist gerade nicht abrufbar.", "stand": _jetzt_iso()}

    # ── Beschreibung ─────────────────────────────────────────────────────────
    @staticmethod
    def _meta() -> dict[str, Any]:
        return {"ui": {"resourceUri": UI_URI}, "app": identitaet("repos")}

    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="uebersicht",
                title="Repos",
                description=(
                    "Alle Repositories des Besitzers (Gitea) auf einen Blick: Standardzweig, letzter Commit, CI-Ampel "
                    "des Standardzweigs (grün, rot, läuft, keine CI), offene PRs, PyPI-Paketversion und Bild-Tags, dazu "
                    "ein kurzer Satz. `filter` sucht in Name und Beschreibung, `nur_probleme` zeigt nur Repos mit roter "
                    "oder unlesbarer CI. Öffnet die App „Repos“. Nur lesend."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "filter": {"type": "string", "maxLength": FILTER_MAX,
                                   "description": "Teil von Name oder Beschreibung (optional)"},
                        "nur_probleme": {"type": "boolean", "default": False,
                                         "description": "Nur Repos mit roter oder unlesbarer CI"},
                    },
                    "additionalProperties": False,
                },
                tags=_TAGS,
                meta=self._meta(),
            ),
            ToolDescriptor(
                name="detail",
                title="Repo im Detail",
                description=(
                    "Ein Repository im Detail: Zweige mit letztem Commit, die letzten Commits des Standardzweigs mit "
                    "CI-Ergebnis, CI-Läufe (Workflow, Zweig oder PR, Ergebnis, Dauer), offene PRs, Paket- und "
                    "Bildversionen. `repo` ist der Name (oder besitzer/name). Nur lesend."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"repo": {"type": "string", "minLength": 1, "maxLength": 201,
                                            "description": "Name des Repositorys, z. B. fyos-os (oder besitzer/name)"}},
                    "required": ["repo"],
                    "additionalProperties": False,
                },
                tags=_TAGS,
                meta=self._meta(),
            ),
        ]

    def describe_resources(self) -> list[ResourceDescriptor]:
        return [
            ResourceDescriptor(
                uri=UI_URI,
                name="Repos",
                description="Repositories mit Standardzweig, CI-Ampel, offenen PRs, Paket- und Bildversion",
                read=lambda: oberflaeche(UI_DATEI, __package__),
                meta={"ui": {"prefersBorder": True}},
            )
        ]

    # ── Aufrufe ──────────────────────────────────────────────────────────────
    def call(self, method: str, args: dict[str, Any]) -> Any:
        args = args or {}
        if self._api is None and not _umgebung.lesen("GITEA_API_BASE"):  # ohne Git-API: ein Zustand, kein Fehler
            return _umgebung.zustand("Die App Repos", self._EINRICHTEN)
        if method == "uebersicht":
            return self.uebersicht(args.get("filter"), args.get("nur_probleme") is True)
        if method == "detail":
            return self.detail(args.get("repo"))
        raise ValueError(f"repos hat kein Werkzeug {method!r}")
