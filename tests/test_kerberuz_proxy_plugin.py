"""Tests für fyos_plugin_betrieb.kerberuz_proxy (NO-TEST-Hook-Fund 2026-06-12).

Mock-basiert (kein echtes mcp-kerberuz): _rpc_call wird gekapselt bzw. der
httpx-Client durch einen Stub ersetzt — geprüft werden Tool-Mapping,
JSON-RPC-Body-Form, Fehlerpfade und Health-Verhalten.
"""

from __future__ import annotations

from typing import Any

import pytest

from fyos_plugin_betrieb.kerberuz_proxy import KerberuzProxyPlugin


@pytest.fixture()
def plugin() -> KerberuzProxyPlugin:
    p = KerberuzProxyPlugin()
    p.config = {"server_url": "http://kerberuz.test:8020", "timeout_sec": 5}
    p.register()
    return p


class _StubResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._payload = payload
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self) -> dict[str, Any]:
        return self._payload


class _StubClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[tuple[str, dict]] = []

    def post(self, path: str, json: dict) -> _StubResponse:  # noqa: A002
        self.calls.append((path, json))
        return _StubResponse(self.payload)

    def close(self) -> None:
        pass


def test_describe_tools_names(plugin: KerberuzProxyPlugin) -> None:
    names = {t.name for t in plugin.describe_tools()}
    assert names == {"register", "unregister", "push_event", "budget_score", "set_last_input"}


def test_register_reads_config(plugin: KerberuzProxyPlugin) -> None:
    assert plugin.server_url == "http://kerberuz.test:8020"
    assert plugin.timeout == 5.0


def test_call_maps_tool_names_to_rpc(plugin: KerberuzProxyPlugin) -> None:
    stub = _StubClient({"result": {"structuredContent": {"ok": True}}})
    plugin._client = stub  # type: ignore[assignment]
    out = plugin.call("push_event", {"event": "x"})
    assert out == {"ok": True}
    path, body = stub.calls[0]
    assert path == "/"
    assert body["method"] == "tools/call"
    assert body["params"]["name"] == "kerberuz_push_event"
    assert body["params"]["arguments"] == {"event": "x"}


def test_call_unknown_tool_raises(plugin: KerberuzProxyPlugin) -> None:
    with pytest.raises(ValueError):
        plugin.call("nope", {})


def test_rpc_error_raises_runtime(plugin: KerberuzProxyPlugin) -> None:
    plugin._client = _StubClient({"error": {"code": -1, "message": "boom"}})  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        plugin.call("register", {})


def test_result_without_structured_content_passthrough(plugin: KerberuzProxyPlugin) -> None:
    plugin._client = _StubClient({"result": {"plain": 1}})  # type: ignore[assignment]
    assert plugin.call("budget_score", {"component": "x"}) == {"plain": 1}


def test_health_ok_on_probe(plugin: KerberuzProxyPlugin) -> None:
    plugin._client = _StubClient({"result": {"structuredContent": {"score": 0}}})  # type: ignore[assignment]
    h = plugin.health()
    assert h["status"] == "ok"


def test_health_unreachable_on_error(plugin: KerberuzProxyPlugin) -> None:
    class _Boom:
        def post(self, *a: Any, **k: Any) -> Any:
            raise ConnectionError("refused")

        def close(self) -> None:
            pass

    plugin._client = _Boom()  # type: ignore[assignment]
    h = plugin.health()
    assert h["status"] == "unreachable"


def test_shutdown_closes_client(plugin: KerberuzProxyPlugin) -> None:
    stub = _StubClient({"result": {}})
    plugin._client = stub  # type: ignore[assignment]
    plugin.shutdown()
    assert plugin._client is None
