"""mcp_hub.plugins.truenas_proxy — TrueNAS Storage Plugin (S-MCP-HUB.1.10).

PROXY-pattern (analog omada_proxy.py): JSON-RPC 2.0 Proxy zu standalone
mcp-truenas server. Migration-Step 1.10.

Seven tools aus mcp-truenas (read + guarded-write):

    truenas.server_status
    truenas.pool_status
    truenas.disk_health
    truenas.system_alerts
    truenas.snapshot_create(dataset)
    truenas.dataset_usage
    truenas.backup_summary

Adresse (streamable_http): `server_url` in der Konfiguration oder FYOS_TRUENAS_URL, z. B.
http://truenas-mcp.example.org:8012/. Ohne Adresse ist das Plugin nicht eingerichtet: lesend ein Zustand mit Weg zum
Einrichten, schreibend (snapshot_create) `ok: false`.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

import httpx

from mcp_hub.plugin import HubPlugin, ToolDescriptor, meldet_fehler, tiefe_health

from . import _umgebung

logger = logging.getLogger(__name__)

# Keine Vorgabe-Adresse: eine falsche Vorgabe unter einer richtigen Ueberschreibung schlaegt erst zu, wenn jemand die
# Ueberschreibung entfernt — und dann zeigt sie auf einen fremden, LEBENDEN Dienst statt ins Leere, antwortet also,
# statt zu scheitern. Darum: ohne Adresse ist das Plugin nicht eingerichtet.
DEFAULT_TIMEOUT = 20.0
EINRICHTEN = "Setze FYOS_TRUENAS_URL (Adresse des mcp-truenas) oder `server_url` in der Konfiguration des Plugins."


class TrueNASProxyPlugin(HubPlugin):
    """Seven-tool TrueNAS storage proxy to standalone mcp-truenas server."""

    name = "truenas"
    description = (
        "TrueNAS storage plugin — proxies to mcp-truenas standalone MCP-server. "
        "Pool/disk/alerts/snapshots/datasets/backup. Write-tool snapshot_create "
        "runs directly (audit logs it, does not gate it)."
    )
    version = "0.1.0"

    config: dict[str, Any]
    server_url: str
    timeout: float
    _client: Optional[httpx.Client]

    def register(self) -> None:
        cfg: dict[str, Any] = getattr(self, "config", {}) or {}
        self.config = cfg
        self.server_url = str(cfg.get("server_url") or _umgebung.lesen("FYOS_TRUENAS_URL")).rstrip("/")
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
                logger.debug("truenas_proxy close failed: %s", exc)

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
            raise RuntimeError(f"mcp-truenas error: {result['error']}")
        data = result.get("result", {})
        # Wie omada (PRB-2180): ein werfendes Werkzeug kommt als Ergebnis mit
        # `isError: true` zurueck. Ohne diese Pruefung meldete der Hub `ok: true`.
        if isinstance(data, dict) and data.get("isError"):
            texte = [str(t.get("text", "")) for t in data.get("content", []) if isinstance(t, dict)]
            grund = " ".join(x for x in texte if x) or "ohne Begruendung"
            raise RuntimeError(f"mcp-truenas {tool_name} fehlgeschlagen: {grund}")
        if isinstance(data, dict) and "structuredContent" in data:
            data = data["structuredContent"]
        # Audit 2026-10-02: mcp-truenas meldet Fehlschlaege als Ergebnis `{"error": "..."}`
        # (z. B. „TrueNAS nicht erreichbar: ..."). Das ist ein Fehler, kein Datensatz.
        grund = meldet_fehler(data)
        if grund:
            raise RuntimeError(f"mcp-truenas {tool_name}: {grund}")
        return data

    def describe_tools(self) -> list[ToolDescriptor]:
        empty = {"type": "object", "properties": {}, "additionalProperties": False}
        return [
            ToolDescriptor(
                name="server_status",
                description="mcp-truenas runtime metadata + connection status.",
                input_schema=empty,
                tags=["truenas", "storage", "read"],
            ),
            ToolDescriptor(
                name="pool_status",
                description="ZFS pool health: state, checksums, scrub status.",
                input_schema=empty,
                tags=["truenas", "storage", "read"],
            ),
            ToolDescriptor(
                name="disk_health",
                description="Per-disk SMART + temperature + error-counters.",
                input_schema=empty,
                tags=["truenas", "storage", "read"],
            ),
            ToolDescriptor(
                name="system_alerts",
                description="Active TrueNAS system alerts (severity + message).",
                input_schema=empty,
                tags=["truenas", "monitoring", "read"],
            ),
            ToolDescriptor(
                name="dataset_usage",
                description="Per-dataset used/available space + compression ratios.",
                input_schema=empty,
                tags=["truenas", "storage", "read"],
            ),
            ToolDescriptor(
                name="backup_summary",
                description="Backup cloud-sync + replication status summary.",
                input_schema=empty,
                tags=["truenas", "backup", "read"],
            ),
            ToolDescriptor(
                name="snapshot_create",
                description="Create a ZFS snapshot for the given dataset. Write-op, audit-logged.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "dataset": {
                            "type": "string",
                            "description": "Dataset name (e.g. 'tank/data')",
                        },
                    },
                    "required": ["dataset"],
                    "additionalProperties": False,
                },
                tags=["truenas", "storage", "write"],
            ),
        ]

    def call(self, method: str, args: dict[str, Any]) -> Any:
        if not self.server_url and method in (
            "server_status", "pool_status", "disk_health", "system_alerts", "dataset_usage", "backup_summary",
            "snapshot_create",
        ):  # keine Adresse: nicht eingerichtet, kein Verbindungsfehler
            if method == "snapshot_create":
                return _umgebung.abgelehnt("Der Speicher-Dienst", EINRICHTEN)
            return _umgebung.zustand("Der Speicher-Dienst", EINRICHTEN)
        if method in (
            "server_status",
            "pool_status",
            "disk_health",
            "system_alerts",
            "dataset_usage",
            "backup_summary",
        ):
            return self._rpc_call(method, {})
        if method == "snapshot_create":
            return self._rpc_call("snapshot_create", {"dataset": args["dataset"]})
        raise ValueError(f"unknown tool: {method}")

    def health(self) -> dict[str, Any]:
        # Audit 2026-10-02: nicht nur „antwortet der Server", sondern ein lesender Abruf
        # (pool_status) muss Daten liefern — siehe plugin.tiefe_health.
        if not self.server_url:
            return {"ok": True, "status": _umgebung.NICHT_EINGERICHTET, "einrichten": EINRICHTEN}
        return tiefe_health(
            self,
            lambda: self._rpc_call("server_status", {}),
            "pool_status",
            lambda: self._rpc_call("pool_status", {}),
        )

    def metrics(self) -> dict[str, Any]:
        return {"plugin": self.name, "version": self.version, "proxy_target": self.server_url}
