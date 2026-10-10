#!/usr/bin/env python3
"""Sauberkeits-Pruefung: findet persoenliche Inhalte, bevor sie in ein Repo gelangen.

Geprueft wird jede versionierte Textdatei (und ihr Pfad) auf
  * private Netze (RFC 1918, CGNAT 100.64/10) und echte E-Mail-Adressen,
  * Geheimnis-Muster (private Schluessel, bekannte Token-Praefixe),
  * verbotene Woerter und Domains. Diese stehen nur als SHA-256-Hash in
    `.sauber/verboten.sha256`, damit die Liste selbst nichts verraet.
    Zeilenformat: `<hash> [exakt]`. Ohne `exakt` wird kleingeschrieben verglichen
    (auch Domain-Suffixe), mit `exakt` nur die Originalschreibweise eines Wortes
    (fuer Namen, die auch als gewoehnliches Wort vorkommen).

Ausnahmen:
  * `.sauber/erlaubt.txt`: Pfad-Praefixe (eine Zeile je Praefix), die uebersprungen werden.
  * Zeilen mit dem Kommentar `sauber: ok` werden nicht gemeldet.

Vor dem Veroeffentlichen auch die Git-Historie: Namen und Adressen von Autor und Committer sowie die
Commit-Texte aller Zweige, mit denselben Regeln. Adressen muessen Platzhalter sein (example.com,
*.invalid, noreply@…); die Domain prueft trotzdem die Wortliste.

Aufruf:
  python3 tools/sauber.py [WURZEL]                      pruefen, Exitcode 1 bei Befund
  python3 tools/sauber.py --historie [WURZEL]           Git-Historie pruefen, Exitcode 1 bei Befund
  python3 tools/sauber.py --hash [--exakt] WORT [WORT…]  Zeilen fuer verboten.sha256
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import subprocess
import sys
from pathlib import Path

TEXT_ENDUNGEN = {
    ".py", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".astro", ".json", ".json5", ".yaml", ".yml",
    ".md", ".txt", ".html", ".css", ".toml", ".sh", ".conf", ".lua", ".scpt", ".applescript",
    ".svg", ".env", ".ini", ".cfg", ".tmpl", "",
}

IPV4 = re.compile(r"(?<![\d.])(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})(?![\d.])")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
EMAIL_ERLAUBT = re.compile(
    r"@(example\.(com|org|net)|beispiel\.(de|org)|localhost|users\.noreply\.)|\.invalid$|^noreply@", re.I
)
GEHEIM = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(ghp|gho|github_pat|glpat|xox[abpr])[-_][A-Za-z0-9_]{16,}"),
    re.compile(r"\bsk-[A-Za-z0-9]{32,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"),
]
WORT = re.compile(r"[A-Za-z0-9ÄÖÜäöüß@._-]+")
TEIL = re.compile(r"[._@-]")
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # sauber: ok
# Fuer Beispiele reserviert (RFC 5737), verraten nichts.
DOKU_NETZE = [ipaddress.ip_network(n) for n in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")]


def _h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _formen(token: str) -> tuple[set[str], set[str]]:
    """(Originalformen, Kleinformen) eines Tokens: das Token, seine Teile, Domain-Suffixe."""
    token = token.strip("._-")
    if not token:
        return set(), set()
    original = {token} | {t for t in TEIL.split(token) if t}
    klein = {f.lower() for f in original}
    teile = token.lower().split(".")
    klein |= {".".join(teile[i:]) for i in range(1, len(teile) - 1)}
    return original, klein


def _verboten(wurzel: Path) -> tuple[set[str], set[str]]:
    datei = wurzel / ".sauber" / "verboten.sha256"
    exakt: set[str] = set()
    klein: set[str] = set()
    if datei.exists():
        for zeile in datei.read_text().splitlines():
            zeile = zeile.split("#", 1)[0].split()
            if zeile:
                (exakt if zeile[1:2] == ["exakt"] else klein).add(zeile[0])
    return exakt, klein


def _treffer(text: str, exakt: set[str], klein: set[str]) -> str | None:
    for token in WORT.findall(text):
        original, kleinformen = _formen(token)
        for f in original:
            if _h(f) in exakt:
                return _h(f)[:10]
        for f in kleinformen:
            if _h(f) in klein:
                return _h(f)[:10]
    return None


def _dateien(wurzel: Path) -> list[Path]:
    try:
        aus = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard"], cwd=wurzel, capture_output=True, text=True, check=True
        ).stdout
        return [wurzel / p for p in aus.splitlines() if p]
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [
            p for p in wurzel.rglob("*")
            if p.is_file() and not {".git", "node_modules", "dist", ".venv"} & set(p.parts)
        ]


def _privat(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if a.is_loopback or a.is_unspecified or a.is_link_local or ip.startswith("0."):
        return False
    if any(a in netz for netz in DOKU_NETZE):
        return False
    return a.is_private or a in CGNAT


def _zeile(zeile: str, exakt: set[str], klein: set[str]) -> list[str]:
    """Befunde einer Textzeile (Datei oder Commit-Text), ohne Ort."""
    if "sauber: ok" in zeile:
        return []
    befunde = [f"privates Netz {ip}" for ip in IPV4.findall(zeile) if _privat(ip)]
    befunde += ["E-Mail-Adresse" for mail in EMAIL.findall(zeile) if not EMAIL_ERLAUBT.search(mail)]
    if any(m.search(zeile) for m in GEHEIM):
        befunde.append("Geheimnis-Muster")
    if (h := _treffer(zeile, exakt, klein)):
        befunde.append(f"verbotenes Wort ({h})")
    return befunde


def pruefen(wurzel: Path) -> list[str]:
    exakt, klein = _verboten(wurzel)
    erlaubt_datei = wurzel / ".sauber" / "erlaubt.txt"
    erlaubt = (
        [z.strip() for z in erlaubt_datei.read_text().splitlines() if z.strip() and not z.startswith("#")]
        if erlaubt_datei.exists() else []
    )
    befunde: list[str] = []
    for pfad in _dateien(wurzel):
        rel = pfad.relative_to(wurzel).as_posix()
        if rel.startswith(".sauber/") or any(rel.startswith(e) for e in erlaubt):
            continue
        if (h := _treffer(rel, exakt, klein)):
            befunde.append(f"{rel}: Pfad enthaelt verbotenes Wort ({h})")
        if pfad.suffix.lower() not in TEXT_ENDUNGEN or not pfad.is_file():
            continue
        try:
            text = pfad.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for nr, zeile in enumerate(text.splitlines(), 1):
            befunde += [f"{rel}:{nr}: {b}" for b in _zeile(zeile, exakt, klein)]
    return befunde


FELD, ENDE = "\x1f", "\x1e"


def historie_pruefen(wurzel: Path) -> list[str]:
    """Autor, Committer und Text jedes Commits aller Zweige, mit den Regeln fuer Dateien."""
    exakt, klein = _verboten(wurzel)
    aus = subprocess.run(
        ["git", "log", "--all", f"--format=%h{FELD}%an{FELD}%ae{FELD}%cn{FELD}%ce{FELD}%B{ENDE}"],
        cwd=wurzel, capture_output=True, text=True, check=True,
    ).stdout
    befunde: list[str] = []
    for eintrag in aus.split(ENDE):
        teile = eintrag.lstrip("\n").split(FELD)
        if len(teile) != 6:
            continue
        sha, autor, autor_mail, committer, committer_mail, text = teile
        for rolle, name, mail in (("Autor", autor, autor_mail), ("Committer", committer, committer_mail)):
            if not EMAIL_ERLAUBT.search(mail):
                befunde.append(f"{sha}: {rolle}-Adresse ist kein Platzhalter")
            if (h := _treffer(f"{name} {mail}", exakt, klein)):
                befunde.append(f"{sha}: {rolle} enthaelt verbotenes Wort ({h})")
        for nr, zeile in enumerate(text.splitlines(), 1):
            befunde += [f"{sha}: Text Zeile {nr}: {b}" for b in _zeile(zeile, exakt, klein)]
    return befunde


def main(argv: list[str]) -> int:
    if argv and argv[0] == "--hash":
        ist_exakt = "--exakt" in argv
        for wort in (a for a in argv[1:] if a != "--exakt"):
            print(_h(wort) + " exakt" if ist_exakt else _h(wort.lower()))
        return 0
    historie = bool(argv) and argv[0] == "--historie"
    rest = argv[1:] if historie else argv
    wurzel = Path(rest[0] if rest else ".").resolve()
    befunde = historie_pruefen(wurzel) if historie else pruefen(wurzel)
    for b in befunde:
        print(b)
    art = "in der Historie von" if historie else "in"
    print(f"sauber: {len(befunde)} Befund(e) {art} {wurzel.name}", file=sys.stderr)
    return 1 if befunde else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
