"""fle_apps — FLE-Werkzeug: Messungen, 2026-09-29.

Stand 2026-10-08: `fle.messungen` hat keine eigene Oberflaeche (die Sicht „Messungen“ steckt in der App Experimente).
Das Shadow-Duell (Blindvergleich zweier Modellantworten, DPO-Zeilen) ist abgebaut.

Ueber Dateien entkoppelt vom Messrechner, damit der Hub nichts von ihm wissen muss und er nichts vom Hub:

    <daten>/messungen/*.json   ein Messlauf je Datei (schreibt der Messrechner, liest der Hub)

`<daten>` ist FYOS_FLE_DATEN oder `daten` in der Konfiguration, z. B. ein gemeinsam eingehängtes
Verzeichnis. Ohne die Einstellung ist das Werkzeug nicht eingerichtet (Zustand mit Weg zum Einrichten).

Messlauf (messungen/<lauf>.json):
    {"lauf": "modell-a-b1", "datum": "2026-09-23", "modell": "...", "gruppe": "modell-a",
     "kennzahlen": {"tok_s": 41.2, "kld_mean": 0.155, "top1": 0.912}, "notiz": "..."}
"""

from __future__ import annotations

import datetime
import json
import os
import re
from pathlib import Path
from typing import Any

from mcp_hub.plugin import HubPlugin, ToolDescriptor

from . import _umgebung

EINRICHTEN = (
    "Setze FYOS_FLE_DATEN (Verzeichnis mit dem Unterordner messungen/, je Messlauf eine JSON-Datei) oder `daten` in der "
    "Konfiguration des Plugins."
)

# Buchhaltung eines Laufs, keine Messgroesse — im Cockpit nur Rauschen.
_KEINE_KENNZAHL = {"rows", "expected", "valid_block"}
#: Welche Kennzahl die knappe Ansicht zuerst nimmt, und welche Namen „kleiner ist besser“ bedeuten (wie im Cockpit).
_VORAUSWAHL = ("median_decode_tps", "tok_s", "kld_mean_F", "kld_mean", "mtp_acceptance_percent")
_TIEF = re.compile(r"kld|ppl|latenz|latency|_ms|seconds|fehler|vram|_gb|groesse|size", re.I)


class FleAppsPlugin(HubPlugin):
    name = "fle"
    description = (
        "FLE: Messlaeufe vergleichen (Oberflaeche: Sicht „Messungen“ in der App Experimente)."
    )
    version = "0.3.0"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        cfg = config or {}
        roh = str(cfg.get("daten") or _umgebung.lesen("FYOS_FLE_DATEN"))
        self.daten: Path | None = Path(roh) if roh else None

    # ── Lebenszyklus ────────────────────────────────────────────────────────
    def register(self) -> None:
        return None

    def health(self) -> dict[str, Any]:
        if self.daten is None:
            return {"ok": True, "status": _umgebung.NICHT_EINGERICHTET, "einrichten": EINRICHTEN}
        ok = self.daten.is_dir()
        return {
            "ok": ok,
            "status": "ok" if ok else "unhealthy",
            "daten": str(self.daten),
            "messungen": len(list((self.daten / "messungen").glob("*.json"))) if ok else 0,
        }

    # ── Beschreibung ────────────────────────────────────────────────────────
    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="messungen",
                title="FLE-Messungen",
                description=(
                    "Messlaeufe (Tempo, KLD, Top-1 …) vergleichen. Die Oberflaeche dazu ist die Sicht „Messungen“ "
                    "in Experimente (experiment.zeigen mit sicht=\"messungen\"); dieses Werkzeug hat keine eigene. "
                    "Knapp (Standard): Satz mit Zaehler und Alter des neuesten Laufs, dazu die besten 5 Laeufe nach einer "
                    "Kennzahl (`kennzahl`, `n`). `voll: true` liefert alle Laeufe mit allen Kennzahlen (rund 45 KB; "
                    "die Oberflaeche nutzt das). `filter` = Teilstring in Lauf/Gruppe/Modell, `laeufe` = genau diese."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "filter": {"type": "string", "description": "Teilstring in Lauf, Gruppe oder Modell"},
                        "laeufe": {"type": "array", "items": {"type": "string"}, "description": "genau diese Laeufe"},
                        "voll": {"type": "boolean", "default": False, "description": "Alle Laeufe mit allen Kennzahlen (gross)"},
                        "kennzahl": {"type": "string", "description": "Nur knapp: Rangliste nach dieser Kennzahl (z. B. median_decode_tps)"},
                        "n": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5, "description": "Nur knapp: so viele beste Laeufe"},
                    },
                    "additionalProperties": False,
                },
                tags=["fle", "read"],
                meta=None,
            ),
        ]

    # ── Aufrufe ─────────────────────────────────────────────────────────────
    def call(self, method: str, args: dict[str, Any]) -> Any:
        if method == "messungen":
            if self.daten is None:
                return _umgebung.zustand("Die FLE-Messungen", EINRICHTEN)
            return self._messungen(args.get("filter") or "", args.get("laeufe") or [], bool(args.get("voll")),
                                   str(args.get("kennzahl") or ""), args.get("n"))
        raise ValueError(f"fle hat kein Werkzeug {method!r}")

    # Messungen ---------------------------------------------------------------
    def _messungen(self, filt: str, laeufe: list[str], voll: bool = False, kennzahl: str = "", n: Any = None) -> dict[str, Any]:
        """Ohne `voll` knapp (KI): Satz, Zähler, Alter und die besten `n` Läufe nach einer Kennzahl — nicht alle 100 Läufe (46 KB)."""
        rows: list[dict[str, Any]] = []
        for p in sorted((self.daten / "messungen").glob("*.json")):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            d.setdefault("lauf", p.stem)
            if laeufe and d["lauf"] not in laeufe:
                continue
            heu = " ".join(str(d.get(k, "")) for k in ("lauf", "gruppe", "modell")).lower()
            if filt and filt.lower() not in heu:
                continue
            rows.append(d)
        for r in rows:
            r["kennzahlen"] = {k: v for k, v in (r.get("kennzahlen") or {}).items() if k not in _KEINE_KENNZAHL}
        namen = sorted({k for r in rows for k in r["kennzahlen"]})
        daten = sorted(str(r["datum"]) for r in rows if r.get("datum"))
        neuestes = daten[-1] if daten else None
        alter = None
        if neuestes:
            try:
                alter = (datetime.date.today() - datetime.date.fromisoformat(neuestes[:10])).days
            except ValueError:
                alter = None
        kopf = f"{len(rows)} Läufe" + (f", neuester vom {neuestes[8:10]}.{neuestes[5:7]}.{neuestes[:4]}" + (f" ({alter} Tage alt)" if alter else "") if neuestes else "")
        if voll:
            return {"satz": kopf + ".", "laeufe": rows, "kennzahlen": namen, "anzahl": len(rows), "neuestes": neuestes, "alter_tage": alter}
        # Knapp: die Rangliste nach einer Kennzahl (Vorgabe: Tempo, sonst KLD)
        kz = kennzahl if kennzahl in namen else next((k for k in _VORAUSWAHL if k in namen), namen[0] if namen else "")
        tief = bool(_TIEF.search(kz))
        try:
            top = max(1, min(int(n), 50)) if n is not None else 5
        except (TypeError, ValueError):
            top = 5
        if laeufe:  # ausdrücklich verlangte Läufe: mit allen Kennzahlen
            zeilen = rows[:max(top, 10)]
        else:
            mit = sorted((r for r in rows if isinstance(r["kennzahlen"].get(kz), (int, float))), key=lambda r: r["kennzahlen"][kz], reverse=not tief)
            zeilen = [{"lauf": r["lauf"], "datum": r.get("datum"), "modell": r.get("modell"), "gruppe": r.get("gruppe"),
                       kz: r["kennzahlen"][kz]} for r in mit[:top]]
        satz = kopf + "."
        if not laeufe and zeilen:
            satz += f" Beste nach {kz} ({'kleiner' if tief else 'höher'} ist besser): {zeilen[0]['lauf']}, {zeilen[0][kz]:g}."
        return {"satz": satz, "knapp": True, "anzahl": len(rows), "neuestes": neuestes, "alter_tage": alter,
                "kennzahl": kz, "tiefer_ist_besser": tief, "kennzahlen": namen, "laeufe": zeilen,
                "mehr": "Alle Läufe mit allen Kennzahlen: fle.messungen {voll: true}; andere Rangliste: {kennzahl, n}; Auswahl: {filter} oder {laeufe}."}
