"""json5.py — kleiner JSON5-Leser fuer Konfigurationen (2026-09-30).

Genug fuer die OpenClaw-Konfiguration (openclaw.json5): Kommentare (// und /* */),
unquotierte Schluessel, einfache Anfuehrungszeichen, Kommas am Ende. Kein vollstaendiger
JSON5-Parser (keine Hex-Zahlen, kein +Infinity); fuer mehr: pyjson5.
"""

from __future__ import annotations

import json
import re
from typing import Any

_BEZEICHNER = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*")


def nach_json(text: str) -> str:
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "\"'":  # Zeichenkette — unveraendert, einfache Quotes in doppelte umsetzen
            q = c
            j = i + 1
            teil = []
            while j < n and text[j] != q:
                if text[j] == "\\" and j + 1 < n:
                    teil.append(text[j : j + 2])
                    j += 2
                    continue
                teil.append('\\"' if (q == "'" and text[j] == '"') else text[j])
                j += 1
            out.append('"' + "".join(teil) + '"')
            i = j + 1
        elif text.startswith("//", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
        elif c == ",":
            # Komma am Ende vor } oder ] (ueber Leerraum/Kommentare hinweg) entfernen
            j = i + 1
            while j < n:
                if text[j].isspace():
                    j += 1
                elif text.startswith("//", j):
                    k = text.find("\n", j)
                    j = n if k < 0 else k
                elif text.startswith("/*", j):
                    k = text.find("*/", j + 2)
                    j = n if k < 0 else k + 2
                else:
                    break
            if j < n and text[j] in "}]":
                i += 1
            else:
                out.append(c)
                i += 1
        else:
            m = _BEZEICHNER.match(text, i)
            if m and (not out or not out[-1][-1:].isalnum()):
                wort = m.group(0)
                k = m.end()
                while k < n and text[k] in " \t":
                    k += 1
                if k < n and text[k] == ":" and wort not in ("true", "false", "null"):
                    out.append(f'"{wort}"')
                else:
                    out.append(wort)
                i = m.end()
            else:
                out.append(c)
                i += 1
    return "".join(out)


def laden(text: str) -> Any:
    return json.loads(nach_json(text))
