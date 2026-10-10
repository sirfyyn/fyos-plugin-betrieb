"""mcp_hub.plugins.kerberuz_proxy — KERBERUZ Proactive-Daemon Plugin (S-MCP-HUB.1.11).

PROXY zu standalone mcp-kerberuz (ADR-074). Fünf Subscriber-Management-Tools:

    kerberuz.register(component, interests, threshold)  — register subscriber
    kerberuz.unregister(component)
    kerberuz.push_event(interest, event_data)
    kerberuz.budget_score(component)
    kerberuz.set_last_input(component)

Adresse des Dienstes: `server_url` in der Konfiguration oder FYOS_KERBERUZ_URL (z. B. http://kerberuz.example.org:30820/).
Ohne Adresse ist das Plugin nicht eingerichtet: lesend ein Zustand mit Weg zum Einrichten, schreibend `ok: false`.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from mcp_hub.plugin import HubPlugin, ToolDescriptor

from . import _umgebung

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
EINRICHTEN = "Setze FYOS_KERBERUZ_URL (Adresse des mcp-kerberuz) oder `server_url` in der Konfiguration des Plugins."


class KerberuzProxyPlugin(HubPlugin):
    name = "kerberuz"
    description = (
        "KERBERUZ Proactive-Daemon plugin (ADR-074) — proxies subscriber-management "
        "+ event-push + budget-score calls to standalone mcp-kerberuz."
    )
    version = "0.1.0"

    config: dict[str, Any]
    server_url: str
    timeout: float
    _client: Optional[httpx.Client]

    def register(self) -> None:
        cfg = getattr(self, "config", {}) or {}
        self.config = cfg
        self.server_url = str(cfg.get("server_url") or _umgebung.lesen("FYOS_KERBERUZ_URL")).rstrip("/")
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
            except Exception:  # noqa: BLE001
                pass

    def _ensure_client(self) -> httpx.Client:
        if self._client is None:
            # Iter-1609-B: StreamableMCP proxy braucht Accept-Header
            # (siehe knowledge_proxy Iter-1608).
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
            raise RuntimeError(f"mcp-kerberuz error: {result['error']}")
        data = result.get("result", {})
        # Wie omada (PRB-2180): ein werfendes Werkzeug kommt als Ergebnis mit
        # `isError: true` zurueck. Ohne diese Pruefung meldete der Hub `ok: true`.
        if isinstance(data, dict) and data.get("isError"):
            texte = [str(t.get("text", "")) for t in data.get("content", []) if isinstance(t, dict)]
            grund = " ".join(x for x in texte if x) or "ohne Begruendung"
            raise RuntimeError(f"mcp-kerberuz {tool_name} fehlgeschlagen: {grund}")
        return data.get("structuredContent", data) if isinstance(data, dict) else data

    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="register",
                description="Register a new KERBERUZ subscriber-component with interests + threshold.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "component": {"type": "string"},
                        "interests": {"type": "array", "items": {"type": "string"}},
                        "threshold": {"type": "number", "default": 0.7},
                    },
                    "required": ["component", "interests"],
                    "additionalProperties": False,
                },
                tags=["kerberuz", "subscriber", "write"],
            ),
            ToolDescriptor(
                name="unregister",
                description="Unregister a KERBERUZ subscriber.",
                input_schema={
                    "type": "object",
                    "properties": {"component": {"type": "string"}},
                    "required": ["component"],
                    "additionalProperties": False,
                },
                tags=["kerberuz", "subscriber", "write"],
            ),
            ToolDescriptor(
                name="push_event",
                description="Push an event for an interest-channel. KERBERUZ scores + triggers subscribers.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "interest": {"type": "string"},
                        "event_data": {"type": "object"},
                    },
                    "required": ["interest", "event_data"],
                    "additionalProperties": False,
                },
                tags=["kerberuz", "event", "write"],
            ),
            ToolDescriptor(
                name="budget_score",
                description="Get current inactivity + budget score for component.",
                input_schema={
                    "type": "object",
                    "properties": {"component": {"type": "string"}},
                    "required": ["component"],
                    "additionalProperties": False,
                },
                tags=["kerberuz", "read"],
            ),
            ToolDescriptor(
                name="set_last_input",
                description="Mark now as last-input for component (reset inactivity timer).",
                input_schema={
                    "type": "object",
                    "properties": {"component": {"type": "string"}},
                    "required": ["component"],
                    "additionalProperties": False,
                },
                tags=["kerberuz", "write"],
            ),
        ]

    def call(self, method: str, args: dict[str, Any]) -> Any:
        mapping = {
            "register": "kerberuz_register",
            "unregister": "kerberuz_unregister",
            "push_event": "kerberuz_push_event",
            "budget_score": "kerberuz_budget_score",
            "set_last_input": "kerberuz_set_last_input",
        }
        if method not in mapping:
            raise ValueError(f"unknown tool: {method}")
        if not self.server_url:  # keine Adresse: nicht eingerichtet, kein Verbindungsfehler
            if method == "budget_score":
                return _umgebung.zustand("Der Kerberuz-Dienst", EINRICHTEN)
            return _umgebung.abgelehnt("Der Kerberuz-Dienst", EINRICHTEN)
        return self._rpc_call(mapping[method], args or {})

    def health(self) -> dict[str, Any]:
        if not self.server_url:
            return {"ok": True, "status": _umgebung.NICHT_EINGERICHTET, "einrichten": EINRICHTEN}
        try:
            # No dedicated /status — use harmless read-call
            result = self._rpc_call("kerberuz_budget_score", {"component": "__hub_health_probe__"})
            return {"status": "ok", "probe_response": bool(result)}
        except Exception as exc:  # noqa: BLE001
            return {"status": "unreachable", "error": str(exc)}

    def metrics(self) -> dict[str, Any]:
        return {"plugin": self.name, "version": self.version, "proxy_target": self.server_url}
