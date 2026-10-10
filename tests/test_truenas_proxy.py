"""mcp_hub.tests.test_truenas_proxy — S-MCP-HUB.1.10 proxy-plugin tests."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from mcp_hub.plugin import ToolDescriptor
from fyos_plugin_betrieb.truenas_proxy import TrueNASProxyPlugin


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

    def set_rpc_result(self, tool: str, payload: Any, err: Any = None) -> None:
        if err:
            body = {"jsonrpc": "2.0", "error": err, "id": 1}
        else:
            body = {"jsonrpc": "2.0", "result": {"structuredContent": payload}, "id": 1}
        self.responses[tool] = DummyResponse(200, body)

    def post(self, url: str, json: dict[str, Any] = None, **kw: Any) -> DummyResponse:
        self.calls.append({"url": url, "json": json})
        tool = json.get("params", {}).get("name", "")
        return self.responses.get(
            tool, DummyResponse(200, {"jsonrpc": "2.0", "result": {}, "id": 1})
        )

    def close(self) -> None:
        return None


@pytest.fixture
def plugin() -> TrueNASProxyPlugin:
    p = TrueNASProxyPlugin()
    p.config = {"server_url": "http://test-truenas:8013"}
    p.register()
    p._client = DummyClient()  # type: ignore[assignment]
    return p


def test_plugin_metadata():
    p = TrueNASProxyPlugin()
    assert p.name == "truenas"
    assert p.version == "0.1.0"


def test_describe_tools_has_7(plugin: TrueNASProxyPlugin):
    tools = plugin.describe_tools()
    assert len(tools) == 7
    names = {t.name for t in tools}
    assert names == {
        "server_status",
        "pool_status",
        "disk_health",
        "system_alerts",
        "dataset_usage",
        "backup_summary",
        "snapshot_create",
    }


def test_pool_status_dispatch(plugin: TrueNASProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("pool_status", {"pools": [{"name": "tank", "state": "ONLINE"}]})
    result = plugin.call("pool_status", {})
    assert result["pools"][0]["name"] == "tank"


def test_disk_health_dispatch(plugin: TrueNASProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("disk_health", {"disks": [{"name": "sda", "smart": "PASSED"}]})
    result = plugin.call("disk_health", {})
    assert len(result["disks"]) == 1


def test_snapshot_create_passes_dataset(plugin: TrueNASProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("snapshot_create", {"snapshot": "tank/data@2026-04-19T22:30:00"})
    result = plugin.call("snapshot_create", {"dataset": "tank/data"})
    assert "snapshot" in result
    rpc = dummy.calls[0]["json"]
    assert rpc["params"]["arguments"]["dataset"] == "tank/data"


def test_system_alerts_dispatch(plugin: TrueNASProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("system_alerts", {"alerts": []})
    result = plugin.call("system_alerts", {})
    assert result["alerts"] == []


def test_dataset_usage_dispatch(plugin: TrueNASProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("dataset_usage", {"datasets": [{"name": "tank", "used_gb": 500}]})
    result = plugin.call("dataset_usage", {})
    assert result["datasets"][0]["used_gb"] == 500


def test_backup_summary_dispatch(plugin: TrueNASProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("backup_summary", {"last_backup": "2026-04-18T10:00:00Z"})
    result = plugin.call("backup_summary", {})
    assert "last_backup" in result


def test_rpc_error_raises(plugin: TrueNASProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("pool_status", None, err={"code": -32000, "message": "upstream down"})
    with pytest.raises(RuntimeError, match="mcp-truenas error"):
        plugin.call("pool_status", {})


def test_unknown_tool(plugin: TrueNASProxyPlugin):
    with pytest.raises(ValueError, match="unknown tool"):
        plugin.call("destroy_pool", {})


def test_shutdown_idempotent(plugin: TrueNASProxyPlugin):
    plugin.shutdown()
    plugin.shutdown()


def test_health_ok(plugin: TrueNASProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("server_status", {"ok": True})
    result = plugin.health()
    assert result["status"] == "ok"


# ─── Audit 2026-10-02 ────────────────────────────────────────────────────────


def test_error_ergebnis_ist_fehler(plugin: TrueNASProxyPlugin):
    """`{"error": ...}` von mcp-truenas darf nicht als Datensatz (ok:true) durchgehen."""
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("pool_status", {"error": "TrueNAS nicht erreichbar: Code 1008"})
    with pytest.raises(RuntimeError, match="1008"):
        plugin.call("pool_status", {})


def test_error_ergebnis_wird_ueber_registry_zum_fehler():
    """Auch ohne Proxy-Pruefung: die Registry wertet `{"error": ...}` ohne `ok` als Fehler."""
    from mcp_hub.plugin import HubPlugin
    from mcp_hub.registry import PluginRegistry, _register_one, call

    class Attrappe(HubPlugin):
        name = "attrappe"

        def register(self):
            return None

        def describe_tools(self):
            return [ToolDescriptor(name="lesen", description="", input_schema={"type": "object"})]

        def call(self, method, args):
            return {"error": "Quelle nicht erreichbar"}

    reg = PluginRegistry()
    _register_one(reg, "attrappe", Attrappe())
    res = call(reg, "attrappe.lesen", {})
    assert res.status == "error"
    assert res.kind == "tool_failed"
    assert "nicht erreichbar" in (res.error or "")


def test_health_tief_meldet_kaputten_abruf(plugin: TrueNASProxyPlugin):
    """Server antwortet, aber pool_status liefert keine Daten -> unhealthy mit Grund."""
    from mcp_hub.registry import plugin_healthy

    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("server_status", {"ok": True})
    dummy.set_rpc_result("pool_status", {"error": "TrueNAS nicht erreichbar"})
    h = plugin.health()
    assert h["status"] == "unhealthy"
    assert "pool_status" in h["reason"]
    assert plugin_healthy(h) is False


def test_health_tief_ok_wenn_pools_kommen(plugin: TrueNASProxyPlugin):
    dummy: DummyClient = plugin._client  # type: ignore[assignment]
    dummy.set_rpc_result("pool_status", {"pools": [{"name": "Files"}]})
    assert plugin.health()["status"] == "ok"
    assert any(c["json"]["params"]["name"] == "pool_status" for c in dummy.calls)
