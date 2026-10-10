"""Kontext einer Einspeisung: was eine Quelle zum Lesen braucht.

Alles, was von außen kommt (Umgebung, Kubernetes-API, Vorgangs-Quelle), hängt hier und ist ersetzbar: Tests geben
Attrappen mit, ein Lauf ohne Datenbank (`--trocken`) liest dieselben Quellen und schreibt nichts.
"""

from __future__ import annotations

import dataclasses
import os
from typing import Any, Callable


class Ausgeschaltet(Exception):
    """Die Quelle ist nicht eingerichtet (keine Datei, keine Adresse, nichts, was sie lesen könnte).

    Das ist kein Fehler: der Einspeiser überspringt die Quelle, lässt ihren alten Stand unberührt und schreibt keinen
    Eintrag in `laeufe`. Ist dagegen etwas eingerichtet und nicht lesbar, wirft die Quelle einen gewöhnlichen Fehler."""

    def __init__(self, grund: str) -> None:
        super().__init__(grund)
        self.grund = grund


@dataclasses.dataclass
class Kontext:
    """Was eine Quelle zum Lesen braucht. Alles ersetzbar (Tests geben Attrappen mit)."""

    umgebung: dict[str, str] = dataclasses.field(default_factory=lambda: dict(os.environ))
    trocken: bool = False
    #: Liefert die rohen Objekte einer Art (siehe `k8s_zugang.ARTEN`); `None` = der Cluster über die Kubernetes-API.
    k8s: Callable[[str], list[dict[str, Any]]] | None = None
    #: Liefert die Quelle der Vorgänge (wie `mcp_hub.vorgaenge.quelle()`); `None` = die des Hubs aus der Umgebung.
    vorgaenge_quelle: Callable[[], Any] | None = None
    #: IDs, die der Graph kennt (aus der Datenbank, dann ergänzt um das, was frühere Quellen im selben Lauf lieferten).
    #: Leer = unbekannt: `gibt()` lässt dann alles durch (Einzelaufruf ohne Datenbank).
    bekannt: set[str] = dataclasses.field(default_factory=set)
    #: Ersatz für die Redis-Momentaufnahme des Bestandsbuchs (Tests, lokale Läufe).
    bestand_stand: dict[str, Any] | None = None
    _k8s_gemerkt: dict[str, list[dict[str, Any]]] = dataclasses.field(default_factory=dict, repr=False)

    def einstellung(self, name: str, vorgabe: str = "") -> str:
        """Eine Einstellung aus der Umgebung des Laufs, ohne Leerraum an den Rändern; leer = nicht gesetzt."""
        return str(self.umgebung.get(name, vorgabe) or "").strip()

    def gibt(self, i: str) -> bool:
        return not self.bekannt or i in self.bekannt

    def k8s_liste(self, art: str) -> list[dict[str, Any]]:
        """Alle Objekte einer Art (`nodes`, `deployments`, …) im Cluster. Eine Art wird je Lauf nur einmal gelesen."""
        if art not in self._k8s_gemerkt:
            if self.k8s is None:
                from .k8s_zugang import K8sZugang

                self.k8s = K8sZugang().liste
            self._k8s_gemerkt[art] = list(self.k8s(art))
        return self._k8s_gemerkt[art]

    def vorgangs_quelle(self) -> Any:
        """Die Quelle der Vorgänge: Vorgangs-Dienst (`FYOS_VORGAENGE_URL`, Kopfzeile `FYOS_VORGAENGE_KOPF`) oder, im
        Hub-Prozess, das Workboard; sonst eine Quelle, die nicht eingerichtet ist (`.eingerichtet` ist falsch)."""
        if self.vorgaenge_quelle is not None:
            return self.vorgaenge_quelle()
        from mcp_hub import vorgaenge

        return vorgaenge.quelle(self.einstellung("FYOS_VORGAENGE_URL") or None, self.einstellung("FYOS_VORGAENGE_KOPF") or None)

    def text_sperre(self) -> frozenset[str]:
        """Zusätzliche Wörter, die kein Textfund sein dürfen (`GRAPH_TEXT_SPERRE`, kommagetrennt, ohne Groß/Klein)."""
        return frozenset(w.strip().lower() for w in self.einstellung("GRAPH_TEXT_SPERRE").split(",") if w.strip())
