"""mcp_hub.tests.test_omada_proxy — S-MCP-HUB.1.9 proxy-plugin tests."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from fyos_plugin_betrieb.omada_proxy import OmadaProxyPlugin


class DummyResponse:
    def __init__(self, status_code: int, payload: Any = None):
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{self.status_code}",
                request=httpx.Request("POST", "http://dummy"),
                response=httpx.Response(self.status_code),
            )


class DummyClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.responses: dict[str, DummyResponse] = {}

    def set_rpc_result(self, tool_name: str, payload: Any, err: Any = None) -> None:
        # Simulate MCP JSON-RPC envelope
        if err:
            body = {"jsonrpc": "2.0", "error": err, "id": 1}
        else:
            body = {"jsonrpc": "2.0", "result": {"structuredContent": payload}, "id": 1}
        self.responses[tool_name] = DummyResponse(200, body)

    def set_raw_result(self, tool_name: str, ergebnis: Any) -> None:
        """Die `result`-Huelle unveraendert setzen.

        `set_rpc_result` packt alles in `structuredContent`. Echtes MCP legt
        `isError` und `content` DANEBEN, nicht darin — wer den Fehlerfall ueber
        `set_rpc_result` nachbaut, prueft eine Form, die es so nicht gibt.
        """
        self.responses[tool_name] = DummyResponse(
            200, {"jsonrpc": "2.0", "result": ergebnis, "id": 1}
        )

    def post(self, url: str, json: dict[str, Any] = None, **kw: Any) -> DummyResponse:
        self.calls.append({"url": url, "json": json})
        tool_name = json.get("params", {}).get("name", "")
        return self.responses.get(
            tool_name, DummyResponse(200, {"jsonrpc": "2.0", "result": {}, "id": 1})
        )

    def close(self) -> None:
        return None


@pytest.fixture
def plugin() -> OmadaProxyPlugin:
    p = OmadaProxyPlugin()
    p.config = {"server_url": "http://test-omada:8011"}
    p.register()
    p._client = DummyClient()  # type: ignore[assignment]
    return p


def test_plugin_metadata():
    p = OmadaProxyPlugin()
    assert p.name == "omada"
    assert p.version == "0.2.0"


def test_describe_tools_reicht_alle_lesewerkzeuge_durch(plugin: OmadaProxyPlugin):
    """Bis 2026-09-02 vier von fuenfzehn — Ports, WAN, Alarme fehlten Agent (CHG-3190)."""
    tools = plugin.describe_tools()
    names = {t.name for t in tools}
    assert len(tools) == 14, names
    assert {"switch_ports", "wan_konfiguration", "alarme", "ereignisse", "health"} <= names
    assert not {"reboot_device", "block_client"} & names, "schreibende bleiben draussen"


def test_unbekannte_felder_wandern_nicht_zum_server(plugin: OmadaProxyPlugin):
    dummy = plugin._client
    dummy.set_rpc_result("alarme", {"alarme": [], "gesamt": 0})
    plugin.call("alarme", {"offen": False, "tippfehler": 1})
    rpc = dummy.calls[-1]["json"]
    assert rpc["params"]["arguments"] == {"offen": False}


def test_server_status_rpc_dispatch(plugin: OmadaProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("server_status", {"ok": True, "server": "mcp_omada"})
    result = plugin.call("server_status", {})
    assert result["ok"] is True
    assert result["server"] == "mcp_omada"
    # Verify RPC payload-shape
    rpc = dummy.calls[0]["json"]
    assert rpc["method"] == "tools/call"
    assert rpc["params"]["name"] == "server_status"


def test_list_clients_passes_limit(plugin: OmadaProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("list_clients", {"clients": [{"mac": "aa:bb", "ip": "192.0.2.10"}]})
    result = plugin.call("list_clients", {"limit": 50})
    assert "clients" in result
    rpc = dummy.calls[0]["json"]
    assert rpc["params"]["arguments"]["limit"] == 50


def test_network_health_dispatch(plugin: OmadaProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("network_health", {"status": "healthy", "devices_up": 5})
    result = plugin.call("network_health", {})
    assert result["status"] == "healthy"
    assert result["devices_up"] == 5


def test_topology_summary_dispatch(plugin: OmadaProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("topology_summary", {"devices": 3, "uplinks": 2})
    result = plugin.call("topology_summary", {})
    assert result["devices"] == 3


def test_rpc_error_raises(plugin: OmadaProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("server_status", None, err={"code": -32000, "message": "upstream down"})
    with pytest.raises(RuntimeError, match="mcp-omada error"):
        plugin.call("server_status", {})


def test_werkzeugfehler_reist_als_fehler(plugin: OmadaProxyPlugin):
    """Ein werfendes Werkzeug kommt als `isError` zurueck, nicht als JSON-RPC-Fehler.

    PRB-2180: `test_rpc_error_raises` deckt nur den Fall ab, dass die GEGENSTELLE
    einen JSON-RPC-Fehler schickt. Wirft dagegen das Werkzeug selbst, antwortet MCP
    mit einem erfolgreichen Ergebnis, in dem `isError: true` steht. Ohne Pruefung
    reichte der Hub das als `ok: true` weiter — und der Fix im Server waere
    wirkungslos verpufft, weil der Fehlschlag eine Schicht spaeter wieder zum
    Ergebnis geworden waere.
    """
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_raw_result(
        "topology_summary",
        {
            "isError": True,
            "content": [
                {"type": "text", "text": "Topologie nicht abrufbar: Omada nicht erreichbar"}
            ],
        },
    )
    with pytest.raises(RuntimeError, match="Omada nicht erreichbar"):
        plugin.call("topology_summary", {})


def test_unknown_tool(plugin: OmadaProxyPlugin):
    with pytest.raises(ValueError, match="unknown tool"):
        plugin.call("reboot_device", {"mac": "aa:bb"})


def test_shutdown_idempotent(plugin: OmadaProxyPlugin):
    plugin.shutdown()
    plugin.shutdown()


def test_health_upstream_ok(plugin: OmadaProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("server_status", {"ok": True})
    result = plugin.health()
    assert result["status"] == "ok"
    assert result["upstream"]["ok"] is True
