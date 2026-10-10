"""fyos_plugin_betrieb — die Betriebs-Plugins von FYOS (Basis der persönlichen Plugin-Repos).

Die Plugins melden sich über den Entry-Point `mcp_hub.plugins` beim Hub an, der Katalogeintrag über `fyos.katalog`.

`fyos_plugin_betrieb.lage` trägt die gemeinsamen Hilfen (_Zwischenspeicher, _redis, _iso, PANE_AUTH, dienst, DIENSTE,
gitea, REPO, GITEA_API, prometheus, fenster_*, sperrgrund, …); die anderen Plugin-Repos importieren sie von dort.
"""

from __future__ import annotations

from pathlib import Path

__version__ = "0.3.2"

KATALOG_DATEI = Path(__file__).resolve().parent / "fyos-katalog.yaml"


def katalog_pfad() -> Path:
    """Pfad der Katalogdatei dieses Pakets. Die Bilder liegen daneben unter `bilder/`."""
    return KATALOG_DATEI
