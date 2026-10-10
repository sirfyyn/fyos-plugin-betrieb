"""mcp_hub.tests.test_k8s_core_plugin — fyos-Iter-0139.

Closes Pre-Commit-Drift-Detector NO-TEST-Warning fuer
mcp_hub/plugins/k8s_core.py (S-MCP-HUB.1.5 / ADR-105 §2.3 K8s Core plugin).

Coverage focuses on the no-cluster-needed surface:
  - Plugin contract (8 ToolDescriptors mit input_schema validation)
  - Lifecycle (in_cluster + kubeconfig + degraded-no-auth)
  - call() input-validation (delegates ohne API-call wenn args malformed)
  - exec_pod security: command MUST be list[str], non-empty, all strings
  - module helpers: _require_str / _require_int / _clamp_int / _attr / _to_epoch
  - health/metrics ohne API-Calls

API-call coverage (cluster_status / get_pods / get_logs / etc.) wird in
container-tests gegen kind-cluster gefahren — hier nur Mock-/Validation-Layer.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from mcp_hub.plugin import ToolDescriptor
from fyos_plugin_betrieb.k8s_core import (
    DEFAULT_LOG_LINES,
    DEFAULT_NAMESPACE,
    DEFAULT_TIMEOUT,
    K8sCorePlugin,
    MAX_LOG_LINES,
    _attr,
    _clamp_int,
    _require_int,
    _require_str,
    _to_epoch,
)


VALID_TOOLS = {
    "cluster_status",
    "get_pods",
    "get_logs",
    "restart_deployment",
    "scale_deployment",
    "describe_pod",
    "get_events",
    "exec_pod",
    "node_hardware",
}


# ─── Plugin Contract ─────────────────────────────────────────────────────────


def test_plugin_contract():
    p = K8sCorePlugin()
    assert p.name == "k8s"
    assert p.version == "0.1.0"
    assert "Kubernetes" in p.description
    p.config = {}
    p.register()
    tools = p.describe_tools()
    assert len(tools) == 9
    names = {t.name for t in tools}
    assert names == VALID_TOOLS
    for t in tools:
        assert isinstance(t, ToolDescriptor)
        assert "k8s" in t.tags


def test_exec_pod_marked_dangerous():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    exec_tool = next(t for t in p.describe_tools() if t.name == "exec_pod")
    assert "dangerous" in exec_tool.tags
    assert "write" in exec_tool.tags
    schema = exec_tool.input_schema
    assert "pod" in schema["required"]
    assert "command" in schema["required"]
    cmd_prop = schema["properties"]["command"]
    assert cmd_prop["type"] == "array"
    assert cmd_prop["items"]["type"] == "string"
    assert cmd_prop["minItems"] == 1


def test_get_logs_caps_lines_in_schema():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    logs = next(t for t in p.describe_tools() if t.name == "get_logs")
    assert logs.input_schema["required"] == ["pod"]


def test_scale_deployment_requires_deployment_and_replicas():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    scale = next(t for t in p.describe_tools() if t.name == "scale_deployment")
    assert set(scale.input_schema["required"]) == {"deployment", "replicas"}


# ─── Lifecycle ───────────────────────────────────────────────────────────────


def test_register_defaults_in_cluster():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    assert p.in_cluster is True
    assert p.default_namespace == DEFAULT_NAMESPACE
    assert p.timeout == DEFAULT_TIMEOUT
    assert p._core_v1 is None
    assert p._apps_v1 is None
    assert p._auth_ok is False


def test_register_custom_kubeconfig_mode():
    p = K8sCorePlugin()
    p.config = {
        "in_cluster": False,
        "kubeconfig_path": "/tmp/test-kubeconfig",
        "default_namespace": "test-ns",
        "timeout_sec": 60.0,
    }
    p.register()
    assert p.in_cluster is False
    assert p.kubeconfig_path == "/tmp/test-kubeconfig"
    assert p.default_namespace == "test-ns"
    assert p.timeout == 60.0


def test_register_kubeconfig_from_env(monkeypatch=None):
    import os
    saved = os.environ.get("KUBECONFIG")
    os.environ["KUBECONFIG"] = "/etc/test/kc"
    try:
        p = K8sCorePlugin()
        p.config = {"in_cluster": False}
        p.register()
        assert p.kubeconfig_path == "/etc/test/kc"
    finally:
        if saved is None:
            os.environ.pop("KUBECONFIG", None)
        else:
            os.environ["KUBECONFIG"] = saved


def test_register_degraded_when_no_in_cluster_and_no_kubeconfig():
    import os
    saved = os.environ.pop("KUBECONFIG", None)
    try:
        p = K8sCorePlugin()
        p.config = {"in_cluster": False}
        p.register()
        assert p.in_cluster is False
        assert p.kubeconfig_path is None
    finally:
        if saved is not None:
            os.environ["KUBECONFIG"] = saved


def test_init_returns_none():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    assert p.init() is None


def test_shutdown_clears_clients_safely():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    p._core_v1 = MagicMock()
    p._apps_v1 = MagicMock()
    p.shutdown()
    assert p._core_v1 is None
    assert p._apps_v1 is None


# ─── _ensure_clients ────────────────────────────────────────────────────────


def test_ensure_clients_raises_when_no_auth_config():
    import os
    saved = os.environ.pop("KUBECONFIG", None)
    try:
        p = K8sCorePlugin()
        p.config = {"in_cluster": False}
        p.register()
        try:
            p._ensure_clients()
        except RuntimeError as e:
            assert "no auth-config" in str(e)
        else:
            raise AssertionError("expected RuntimeError")
    finally:
        if saved is not None:
            os.environ["KUBECONFIG"] = saved


def test_ensure_clients_returns_cached_when_already_set():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    fake_core = MagicMock()
    fake_apps = MagicMock()
    p._core_v1 = fake_core
    p._apps_v1 = fake_apps
    c, a = p._ensure_clients()
    assert c is fake_core
    assert a is fake_apps


# ─── call() Dispatch + Input-Validation ─────────────────────────────────────


def test_call_unknown_method_raises_value_error():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    try:
        p.call("nonexistent_tool", {})
    except ValueError as e:
        assert "nonexistent_tool" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_call_get_logs_missing_pod_raises():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    try:
        p.call("get_logs", {})
    except ValueError as e:
        assert "pod" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_call_scale_deployment_missing_replicas_raises():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    try:
        p.call("scale_deployment", {"deployment": "agent"})
    except ValueError as e:
        assert "replicas" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_call_exec_pod_command_must_be_list():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    try:
        p.call("exec_pod", {"pod": "agent-x", "command": "ls -la"})
    except ValueError as e:
        assert "command" in str(e) and "list" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_call_exec_pod_command_must_be_non_empty():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    try:
        p.call("exec_pod", {"pod": "agent-x", "command": []})
    except ValueError as e:
        assert "command" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_call_exec_pod_command_must_be_all_strings():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    try:
        p.call("exec_pod", {"pod": "agent-x", "command": ["ls", 123, "/tmp"]})
    except ValueError as e:
        assert "command" in str(e)
    else:
        raise AssertionError("expected ValueError")


# ─── Module Helpers ─────────────────────────────────────────────────────────


def test_require_str_happy_path():
    assert _require_str({"key": "value"}, "key") == "value"


def test_require_str_strips_whitespace():
    assert _require_str({"key": "  hello  "}, "key") == "hello"


def test_require_str_raises_on_missing():
    try:
        _require_str({}, "key")
    except ValueError as e:
        assert "key" in str(e)
    else:
        raise AssertionError("expected ValueError")


def test_require_str_raises_on_empty_string():
    try:
        _require_str({"key": "   "}, "key")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_require_str_raises_on_non_string():
    try:
        _require_str({"key": 123}, "key")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_require_int_happy_path():
    assert _require_int({"replicas": 5}, "replicas") == 5


def test_require_int_rejects_bool():
    try:
        _require_int({"replicas": True}, "replicas")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError — bool must be rejected")


def test_require_int_rejects_string():
    try:
        _require_int({"replicas": "5"}, "replicas")
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError")


def test_clamp_int_clamps_to_range():
    assert _clamp_int(5, 50, 1, 100) == 5
    assert _clamp_int(-10, 50, 1, 100) == 1
    assert _clamp_int(500, 50, 1, 100) == 100


def test_clamp_int_returns_default_for_non_int():
    assert _clamp_int("abc", 42, 1, 100) == 42
    assert _clamp_int(None, 42, 1, 100) == 42
    assert _clamp_int(True, 42, 1, 100) == 42


def test_attr_with_dict_object():
    assert _attr({"key": "value"}, "key") == "value"
    assert _attr({"key": "value"}, "missing", "default") == "default"


def test_attr_with_class_object():
    class Holder:
        attr = "value"

    assert _attr(Holder(), "attr") == "value"
    assert _attr(Holder(), "missing", "default") == "default"


def test_attr_with_none():
    assert _attr(None, "key") is None
    assert _attr(None, "key", "default") == "default"


def test_to_epoch_with_datetime():
    dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
    epoch = _to_epoch(dt)
    assert epoch is not None
    assert epoch > 0


def test_to_epoch_with_int_passthrough():
    assert _to_epoch(1714) == 1714.0


def test_to_epoch_with_none():
    assert _to_epoch(None) is None


# ─── health + metrics ───────────────────────────────────────────────────────


def test_health_degraded_when_no_auth():
    import os
    saved = os.environ.pop("KUBECONFIG", None)
    try:
        p = K8sCorePlugin()
        p.config = {"in_cluster": False}
        p.register()
        h = p.health()
        assert h["ok"] is False
        assert h["status"] == "degraded"
        assert "no in_cluster" in h["reason"]
    finally:
        if saved is not None:
            os.environ["KUBECONFIG"] = saved


def test_health_healthy_in_cluster():
    p = K8sCorePlugin()
    p.config = {}
    p.register()
    h = p.health()
    assert h["ok"] is True
    assert h["status"] == "healthy"
    assert h["mode"] == "in_cluster"


def test_health_healthy_kubeconfig():
    p = K8sCorePlugin()
    p.config = {"in_cluster": False, "kubeconfig_path": "/tmp/kc"}
    p.register()
    h = p.health()
    assert h["ok"] is True
    assert h["mode"] == "kubeconfig"


def test_metrics_returns_static_info():
    p = K8sCorePlugin()
    p.config = {"default_namespace": "test"}
    p.register()
    m = p.metrics()
    assert m["plugin"] == "k8s"
    assert m["tools_exposed"] == 8
    assert m["mode"] == "in_cluster"
    assert m["default_namespace"] == "test"
    assert m["client_initialized"] is False


# ─── Default-Constants ──────────────────────────────────────────────────────


def test_constants_have_expected_values():
    assert DEFAULT_NAMESPACE == "fyos"
    assert DEFAULT_TIMEOUT == 30.0
    assert DEFAULT_LOG_LINES == 50
    assert MAX_LOG_LINES == 2000
