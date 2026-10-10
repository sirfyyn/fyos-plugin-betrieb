"""Textfunde: welche bekannten Dinge nennt ein Text? Gemeinsam für itsm und experimente.

Bewusst eng: ein Name zählt nur als ganzes Wort (Bindestrich gehört zum Wort, `mail-relay` trifft nicht `mail`), zu
allgemeine Namen stehen auf der Sperrliste, und jeder Fund ist nur ein Hinweis (vertrauen < 1), kein Beleg.
Eigene Sperrwörter, etwa Namen von Diensten, die in jedem Text vorkommen: Umgebung `GRAPH_TEXT_SPERRE`."""

from __future__ import annotations

import re
from typing import Iterable

#: Namen, die in fast jedem Text vorkommen und nichts über den Gegenstand sagen.
SPERRLISTE = frozenset(
    {
        "core", "mcp", "db", "web", "hub", "app", "api", "job", "test", "main", "data", "home", "stack", "dev",
        "net", "git", "ops", "log", "auth", "cache", "base", "all", "ci", "cd", "ui", "os", "ha",
        # gewöhnliche Wörter, die zufällig wie ein Dienst heißen („Tempo" der Messung, „Vektor")
        "tempo", "vector", "loki", "alert",
    }
)
MINDESTLAENGE = 4
#: Graph-IDs, deren Namen ein Text nennen darf (Dienste, CronJobs, Dienste auf Rechnern, Knoten).
PRAEFIXE = ("dienst:", "cronjob:", "rechner:", "knoten:")


def name_aus_id(i: str) -> str:
    rest = i.split(":", 1)[1] if ":" in i else i
    return rest.rsplit("/", 1)[-1]


class Index:
    """Name → IDs, mit einem Regex über alle Namen (längste zuerst)."""

    def __init__(self, ids: Iterable[str], *, praefixe: tuple[str, ...] = PRAEFIXE, sperre: Iterable[str] = ()):
        gesperrt = SPERRLISTE | {s.lower() for s in sperre}
        self.nach_name: dict[str, list[str]] = {}
        for i in ids:
            if not i.startswith(praefixe):
                continue
            n = name_aus_id(i).lower()
            if n.endswith(".service"):
                n = n[: -len(".service")]
            if len(n) < MINDESTLAENGE or n in gesperrt:
                continue
            self.nach_name.setdefault(n, []).append(i)
        namen = sorted(self.nach_name, key=len, reverse=True)
        self._rx = re.compile(r"(?<![\w-])(" + "|".join(re.escape(n) for n in namen) + r")(?![\w-])") if namen else None

    def finde(self, text: str) -> set[str]:
        if not self._rx or not text:
            return set()
        out: set[str] = set()
        for m in self._rx.finditer(text.lower()):
            out.update(self.nach_name.get(m.group(1), []))
        return out
