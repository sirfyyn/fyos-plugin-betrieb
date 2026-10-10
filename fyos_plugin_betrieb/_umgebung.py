"""Einstellungen aus der Umgebung des Hubs: Namen der Form `FYOS_<BEREICH>_<WAS>`.

Reihenfolge: ausdrückliche Einstellung vor Vorgabe; leere Werte zählen als nicht
gesetzt. Vorgaben auf Dienste, Domains oder Rechner einer bestimmten Installation gibt es nicht: Ist nichts gesetzt, ist
die Fähigkeit „nicht eingerichtet“ und sagt, wie man sie einrichtet (lesend: Zustand mit Weg zum Einrichten, schreibend:
`ok: false`). Allgemeine Vorgaben (Namespace, Label-Konvention, Port) bleiben und sind am Ort begründet.

Die Einstellungen werden bei jedem Aufruf gelesen, nicht beim Import: Tests und ein Hub, dessen Umgebung sich ändert,
sehen den aktuellen Stand.
"""

from __future__ import annotations

import os
from typing import Any

#: Zustandswort in Antworten und `health`
NICHT_EINGERICHTET = "nicht eingerichtet"

#: Allgemeine Vorgabe: der Namespace, in den eine FYOS-Installation ihre Dienste legt. Eine Adresse ist das nicht; wer
#: einen anderen Namespace nutzt, setzt BETRIEB_NAMESPACE bzw. ARBEIT_NAMESPACE.
NAMESPACE_VORGABE = "fyos"


def lesen(name: str) -> str:
    """Wert der Einstellung `name`; ist sie nicht gesetzt oder leer, ein leerer Text."""
    return (os.environ.get(name) or "").strip()


def liste(name: str) -> list[str]:
    """Eine kommagetrennte Einstellung als Liste ohne leere Einträge."""
    return [t.strip() for t in lesen(name).split(",") if t.strip()]


def zustand(was: str, einrichten: str) -> dict[str, Any]:
    """Antwort eines lesenden Werkzeugs, wenn `was` nicht eingerichtet ist: eigener Zustand mit Weg zum Einrichten."""
    return {
        "zustand": NICHT_EINGERICHTET,
        "eingerichtet": False,
        "satz": f"{was} ist nicht eingerichtet.",
        "einrichten": einrichten,
    }


def abgelehnt(was: str, einrichten: str) -> dict[str, Any]:
    """Antwort eines schreibenden Werkzeugs, wenn `was` nicht eingerichtet ist: nichts wurde getan."""
    return {
        "ok": False,
        "zustand": NICHT_EINGERICHTET,
        "eingerichtet": False,
        "error": f"{was} ist nicht eingerichtet; es wurde nichts geändert.",
        "einrichten": einrichten,
    }


class NichtEingerichtet(RuntimeError):
    """Eine Voraussetzung ist nicht eingerichtet (keine Adresse, kein Schlüssel). Trägt, wie man sie einrichtet.

    Eine `RuntimeError`, damit Aufrufer, die Ausfälle einer Quelle schon heute als Ausnahme behandeln (Redis, Gitea,
    Prometheus nicht erreichbar), auch diesen Fall abfangen."""

    def __init__(self, was: str, einrichten: str):
        super().__init__(f"{was} ist nicht eingerichtet. {einrichten}")
        self.was = was
        self.einrichten = einrichten

    def lesend(self) -> dict[str, Any]:
        return zustand(self.was, self.einrichten)

    def schreibend(self) -> dict[str, Any]:
        return abgelehnt(self.was, self.einrichten)


def verlangen(name: str, was: str, einrichten: str | None = None) -> str:
    """Wert der Einstellung `name`; ohne Wert `NichtEingerichtet(was, …)`."""
    wert = lesen(name)
    if not wert:
        raise NichtEingerichtet(was, einrichten or f"Setze {name} in der Umgebung des Hubs.")
    return wert
