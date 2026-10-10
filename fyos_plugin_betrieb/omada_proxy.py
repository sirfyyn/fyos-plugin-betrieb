"""mcp_hub.plugins.omada_proxy — Omada Network Plugin (S-MCP-HUB.1.9).

PROXY-pattern: Hub-Plugin proxies tool-calls to the existing standalone
mcp-omada server via MCP JSON-RPC over HTTP. Avoids re-implementing network
logic — mcp-omada remains single source of truth, Hub just provides unified
surface + audit-logging per ADR-105.

Alle LESENDEN Werkzeuge des mcp-omada werden 1:1 durchgereicht (Tabelle
``LESEWERKZEUGE``, seit 2026-09-02 vierzehn statt vier). Schreibende
(`reboot_device` / `block_client`) bleiben draussen.

Why proxy (not re-implement):
    - mcp-omada has its own agent + tool dependency chain. Porting it to mcp_hub/
      would fork the tool logic.
    - Proxy keeps mcp-omada authoritative, Hub is the dispatching facade.
    - Migration-Step 1.9: bestehende MCPs unter Hub registrieren (nicht
      rewriten). Sprint-Spec 43-s-mcp-hub-1.md §1.9.

Adresse (streamable_http_path="/"): `server_url` in der Konfiguration oder FYOS_OMADA_URL, z. B.
http://omada.example.org:8011/. Ohne Adresse ist das Plugin nicht eingerichtet: jedes (lesende) Werkzeug liefert einen
Zustand mit Weg zum Einrichten statt eines Verbindungsfehlers.

Error Surface:
    - plugin_error   — MCP-RPC failure or network error
    - circuit_open   — 5 consecutive failures
    - not_found      — unknown tool name
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from mcp_hub.plugin import HubPlugin, ToolDescriptor, tiefe_health

from . import _umgebung

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
EINRICHTEN = "Setze FYOS_OMADA_URL (Adresse des mcp-omada) oder `server_url` in der Konfiguration des Plugins."

_MAC = {"mac": {"type": "string", "description": "Geraete-MAC, z.B. AA-BB-CC-DD-EE-FF."}}
_MAC_OPT = {"mac": {"type": "string", "description": "Optional: nur dieses Geraet (MAC)."}}

# Lesende Werkzeuge des mcp-omada, 1:1 durchgereicht. Bis 2026-09-02 standen hier vier,
# waehrend der Server siebzehn anbot — Agenten sahen ueber den Hub weder Ports noch WAN
# noch Alarme. Draussen bleiben: die schreibenden (reboot_device,
# block_client — Hub-Werkzeuge sind fuer Agenten ohne Rueckfrage aufrufbar) und
# qos_recommendations (Text aus dem Quelltext, keine Messung am Netz).
LESEWERKZEUGE: dict[str, tuple[str, dict[str, Any]]] = {
    "server_status": ("mcp-omada: laeuft der Server UND antwortet der Controller.", {}),
    "health": ("Dreistufige Bereitschaft: Controller, Anmeldung, Geraeteabruf.", {}),
    "network_health": ("FYOS-Knoten pingen/DNS pruefen (kein Omada-Aufruf).", {}),
    "netz_uebersicht": ("Dashboard-Zaehlwerte: verbunden/getrennt, Ports, PoE.", {}),
    "topology_summary": ("Geraete + Uplinks + Zaehlwerte der Site.", {}),
    "list_devices": ("Alle adoptierten Geraete (APs, Switches, Gateway).", {}),
    "list_clients": (
        "Verbundene Clients (Kabel + Funk) mit Name/IP/MAC/SSID/Signal.",
        {"limit": {"type": "integer", "default": 20, "description": "Maximum number of clients to return."}},
    ),
    "geraet_details": (
        "Einzelnes Geraet im Detail.",
        {"typ": {"type": "string", "enum": ["gateway", "switch", "ap"]}, **_MAC},
    ),
    "gateway_ports": ("Port-Belegung des Gateways: WAN/LAN, Link.", _MAC_OPT),
    "switch_ports": ("Alle Switch-Ports: Link, Geschwindigkeit, PoE, VLAN-Profil.", _MAC_OPT),
    "wan_konfiguration": ("WAN-Einstellungen: welche Ports tragen eine Leitung.", {}),
    "wifi_client_details": ("WiFi-Clients mit MCS, Kanal, Raten, Signal, AP.", _MAC_OPT),
    "alarme": (
        "Alarme des Controllers, offene (Vorgabe) oder archivierte, mit Kontrollzahl.",
        {
            "offen": {"type": "boolean", "default": True},
            "tage": {"type": "number", "default": 7},
            "groesse": {"type": "integer", "default": 50},
        },
    ),
    "ereignisse": (
        "Ereignisprotokoll des Controllers, neueste zuerst, mit Kontrollzahl.",
        {"tage": {"type": "number", "default": 1}, "groesse": {"type": "integer", "default": 50}},
    ),
}


class OmadaProxyPlugin(HubPlugin):
    """Four-tool Omada proxy to standalone mcp-omada server."""

    name = "omada"
    description = (
        "Omada network plugin — proxies to mcp-omada standalone MCP-server. "
        "Alle lesenden Werkzeuge (Status, Geraete, Clients, Ports, WAN, Alarme, "
        "Ereignisse). Schreibende (reboot/block) bewusst nicht ueber den Hub."
    )
    version = "0.2.0"

    config: dict[str, Any]
    server_url: str
    timeout: float
    _client: Optional[httpx.Client]

    def register(self) -> None:
        cfg: dict[str, Any] = getattr(self, "config", {}) or {}
        self.config = cfg
        self.server_url = str(cfg.get("server_url") or _umgebung.lesen("FYOS_OMADA_URL")).rstrip("/")
        self.timeout = float(cfg.get("timeout_sec") or DEFAULT_TIMEOUT)
        self._client = None

    def init(self) -> None:
        return None

    def shutdown(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                client.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("mcp_hub.plugins.omada_proxy: close failed: %s", exc)

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            # Iter-1609-B: StreamableMCP proxy braucht Accept-Header.
            self._client = httpx.Client(
                base_url=self.server_url,
                timeout=self.timeout,
                headers={
                    "Accept": "application/json, text/event-stream",
                    "Content-Type": "application/json",
                },
            )
        return self._client

    def _rpc_call(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        """MCP JSON-RPC 2.0 tools/call dispatch to standalone mcp-omada."""
        client = self._ensure_client()
        body = {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments or {}},
            "id": 1,
        }
        resp = client.post("/", json=body)
        resp.raise_for_status()
        result = resp.json()
        if "error" in result:
            raise RuntimeError(f"mcp-omada error: {result['error']}")
        # MCP result envelope: {"content": [{"type": "text", "text": "..."}]}
        # or direct payload wrapped in structuredContent.
        data = result.get("result", {})
        # PRB-2180: ein Werkzeug, das WIRFT, kommt nicht als JSON-RPC-`error` zurueck,
        # sondern als erfolgreiches Ergebnis mit `isError: true` darin. Ohne diese
        # Pruefung reicht der Hub den Fehlschlag als `ok: true` weiter — genau die
        # Huelle, wegen der die Sache ueberhaupt aufgefallen ist. Der Fix im Server
        # allein waere hier wirkungslos verpufft.
        if isinstance(data, dict) and data.get("isError"):
            texte = [str(t.get("text", "")) for t in data.get("content", []) if isinstance(t, dict)]
            grund = " ".join(x for x in texte if x) or "ohne Begruendung"
            raise RuntimeError(f"mcp-omada {tool_name} fehlgeschlagen: {grund}")
        if isinstance(data, dict) and "structuredContent" in data:
            return data["structuredContent"]
        return data

    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name=name,
                description=beschreibung,
                input_schema={
                    "type": "object",
                    "properties": felder,
                    "additionalProperties": False,
                },
                tags=["omada", "network", "read"],
            )
            for name, (beschreibung, felder) in LESEWERKZEUGE.items()
        ]

    def call(self, method: str, args: dict[str, Any]) -> Any:
        if method not in LESEWERKZEUGE:
            raise ValueError(f"unknown tool: {method}")
        if not self.server_url:  # keine Adresse: nicht eingerichtet, kein Verbindungsfehler
            return _umgebung.zustand("Das Omada-Netz", EINRICHTEN)
        erlaubt = LESEWERKZEUGE[method][1]
        # Nur bekannte Felder weiterreichen; sonst wandert ein Tippfehler als
        # Argument zum Server und faellt dort als Schema-Fehler an — ohne dass der
        # Aufrufer sieht, welches Feld gemeint war.
        weiter = {k: v for k, v in (args or {}).items() if k in erlaubt}
        if method == "list_clients":
            weiter.setdefault("limit", 20)
        return self._rpc_call(method, weiter)

    def health(self) -> dict[str, Any]:
        # Audit 2026-10-02: nicht nur „antwortet der Server", sondern ein lesender Abruf
        # (health) muss Daten liefern — siehe plugin.tiefe_health.
        if not self.server_url:
            return {"ok": True, "status": _umgebung.NICHT_EINGERICHTET, "einrichten": EINRICHTEN}
        return tiefe_health(
            self,
            lambda: self._rpc_call("server_status", {}),
            "health",
            lambda: self._rpc_call("health", {}),
        )

    def metrics(self) -> dict[str, Any]:
        return {"plugin": self.name, "version": self.version, "proxy_target": self.server_url}
