"""mcp_hub.tests.test_k8s_plugin — S-MCP-HUB.1.5 Gate tests.

Covers the K8sCorePlugin contract:
    - Plugin-Protocol compliance (name/version/description/8 ToolDescriptors)
    - Lifecycle: register() respects config, degraded without kubeconfig
    - API dispatch via DummyK8sClient (no real kube-apiserver)
    - Happy-paths for all 8 tools (cluster_status / get_pods / get_logs /
      restart_deployment / scale_deployment / describe_pod / get_events /
      exec_pod)
    - Error cases: 401 auth, 403 RBAC, 404 not-found, ApiException wrapping
    - Integration: registry.call wraps plugin-crash → ToolInvocationResult(error)

Design:
    We inject DummyCoreV1 + DummyAppsV1 stubs directly onto the plugin instance
    (``p._core_v1 = DummyCoreV1(...)``). The lazy ``_ensure_clients()`` short-
    circuits when the client attrs are already populated, so no real
    ``kubernetes`` package import is needed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Optional

import pytest

from mcp_hub import registry as hub_registry
from mcp_hub.plugin import ToolDescriptor, ToolInvocationResult
from fyos_plugin_betrieb.k8s_core import (
    K8sCorePlugin,
    _shape_events,
    _shape_nodes,
    _shape_pod_detail,
    _shape_pods,
)

# ---------------------------------------------------------------------------
# Stub ApiException — mirrors kubernetes.client.exceptions.ApiException shape
# without importing the real package (tests must run without it installed).
# ---------------------------------------------------------------------------


class FakeApiException(Exception):
    def __init__(self, status: int, reason: str = "", body: str = ""):
        super().__init__(f"({status}) {reason} {body}")
        self.status = status
        self.reason = reason
        self.body = body


# ---------------------------------------------------------------------------
# Dummy k8s clients — expose exactly the methods the plugin calls.
# ---------------------------------------------------------------------------


class DummyCoreV1:
    def __init__(
        self,
        pods_response: Any = None,
        nodes_response: Any = None,
        logs_response: str = "",
        describe_response: Any = None,
        events_response: Any = None,
        raise_on: Optional[Exception] = None,
    ):
        self.calls: list[dict[str, Any]] = []
        self._pods = pods_response
        self._nodes = nodes_response
        self._logs = logs_response
        self._describe = describe_response
        self._events = events_response
        self._raise = raise_on

    def _maybe_raise(self):
        if self._raise is not None:
            raise self._raise

    def list_namespaced_pod(self, namespace: str, **kwargs: Any):
        self.calls.append({"op": "list_namespaced_pod", "namespace": namespace, **kwargs})
        self._maybe_raise()
        return self._pods

    def list_pod_for_all_namespaces(self, **kwargs: Any):
        self.calls.append({"op": "list_pod_for_all_namespaces", **kwargs})
        self._maybe_raise()
        return self._pods

    def list_node(self, **kwargs: Any):
        self.calls.append({"op": "list_node", **kwargs})
        self._maybe_raise()
        # CHG-2315: der Dummy gab bis hierher IMMER alle Knoten zurueck und ignorierte
        # `field_selector`. Damit war der Zweig "Knoten gibt es nicht" im Test nicht
        # erreichbar — ein Gerueest, das jede Anfrage beantwortet, kann keinen Fehlschlag
        # nachstellen. Die echte API filtert; hier jetzt auch.
        sel = kwargs.get("field_selector") or ""
        if sel.startswith("metadata.name=") and self._nodes is not None:
            gesucht = sel.split("=", 1)[1]
            items = [
                n
                for n in (getattr(self._nodes, "items", None) or [])
                if getattr(getattr(n, "metadata", None), "name", None) == gesucht
            ]
            return SimpleNamespace(items=items)
        return self._nodes

    def read_namespaced_pod_log(self, **kwargs: Any):
        self.calls.append({"op": "read_namespaced_pod_log", **kwargs})
        self._maybe_raise()
        return self._logs

    def read_namespaced_pod(self, name: str, namespace: str, **kwargs: Any):
        self.calls.append(
            {"op": "read_namespaced_pod", "name": name, "namespace": namespace, **kwargs}
        )
        self._maybe_raise()
        return self._describe

    def list_namespaced_event(self, namespace: str, **kwargs: Any):
        self.calls.append({"op": "list_namespaced_event", "namespace": namespace, **kwargs})
        self._maybe_raise()
        return self._events

    def connect_get_namespaced_pod_exec(self, *args: Any, **kwargs: Any):
        self.calls.append({"op": "connect_get_namespaced_pod_exec", "args": args, "kwargs": kwargs})
        self._maybe_raise()
        return None


class DummyAppsV1:
    def __init__(self, raise_on: Optional[Exception] = None):
        self.calls: list[dict[str, Any]] = []
        self._raise = raise_on

    def _maybe_raise(self):
        if self._raise is not None:
            raise self._raise

    def read_namespaced_deployment(self, name: str, namespace: str, **kwargs: Any):
        # 2026-10-03: restart/scale lesen vorher das Deployment (Fenster-Sperre, Flux-Hinweis).
        # Lesen zaehlt nicht in `calls` — die Tests pruefen dort die schreibenden Aufrufe.
        self._maybe_raise()
        return SimpleNamespace(
            metadata=SimpleNamespace(name=name, labels={}, managed_fields=[]),
            spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"app": name}, match_expressions=None)),
        )

    def patch_namespaced_deployment(self, name: str, namespace: str, body: Any, **kwargs: Any):
        self.calls.append(
            {
                "op": "patch_namespaced_deployment",
                "name": name,
                "namespace": namespace,
                "body": body,
            }
        )
        self._maybe_raise()
        return SimpleNamespace(spec=SimpleNamespace(replicas=2))

    def patch_namespaced_deployment_scale(
        self, name: str, namespace: str, body: Any, **kwargs: Any
    ):
        self.calls.append(
            {
                "op": "patch_namespaced_deployment_scale",
                "name": name,
                "namespace": namespace,
                "body": body,
            }
        )
        self._maybe_raise()
        desired = body["spec"]["replicas"] if isinstance(body, dict) else 1
        return SimpleNamespace(
            spec=SimpleNamespace(replicas=desired),
            status=SimpleNamespace(replicas=desired),
        )


# ---------------------------------------------------------------------------
# Helpers to build response objects
# ---------------------------------------------------------------------------


def _pod(
    name: str,
    namespace: str = "fyos",
    phase: str = "Running",
    ready_containers: int = 1,
    total_containers: int = 1,
    node: str = "beta",
    pod_ip: str = "198.51.100.1",
) -> SimpleNamespace:
    statuses = [
        SimpleNamespace(
            ready=(i < ready_containers),
            name=f"c{i}",
            image="img",
            restart_count=0,
            state=SimpleNamespace(running=True, waiting=None, terminated=None),
        )
        for i in range(total_containers)
    ]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, namespace=namespace),
        spec=SimpleNamespace(node_name=node),
        status=SimpleNamespace(
            phase=phase,
            pod_ip=pod_ip,
            host_ip="192.0.2.12",
            start_time=datetime(2026, 4, 19, 10, 0, 0, tzinfo=timezone.utc),
            container_statuses=statuses,
            conditions=[
                SimpleNamespace(
                    type="Ready",
                    status="True",
                    reason=None,
                    message=None,
                    last_transition_time=datetime(2026, 4, 19, 10, 0, 5, tzinfo=timezone.utc),
                ),
            ],
        ),
    )


def _node(name: str, ready: bool = True, unschedulable: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        spec=SimpleNamespace(unschedulable=unschedulable),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status="True" if ready else "False")],
            node_info=SimpleNamespace(kubelet_version="v1.30.0"),
        ),
    )


def _event(
    name: str, reason: str, message: str, pod_name: str, seconds_ago: int = 0
) -> SimpleNamespace:
    from datetime import timedelta

    ts = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name),
        type="Normal",
        reason=reason,
        message=message,
        count=1,
        involved_object=SimpleNamespace(kind="Pod", name=pod_name, namespace="fyos"),
        last_timestamp=ts,
        event_time=ts,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _ohne_fenster(monkeypatch):
    """Logs und Exec fragen die Fenster-Sperre (lage). Ohne eigenes Fenster gilt: kein Knoten gesperrt —
    und kein Test erreicht dafuer das echte Redis. Die Fixture `fenster` ersetzt das gezielt."""
    from fyos_plugin_betrieb import lage
    from test_lage import FakeRedis

    monkeypatch.setattr(lage, "_redis", lambda: FakeRedis())


@pytest.fixture
def plugin_ok():
    p = K8sCorePlugin()
    p.config = {
        "in_cluster": False,
        "kubeconfig_path": "/tmp/fake-kubeconfig",
        "default_namespace": "fyos",
        "timeout_sec": 5,
    }
    p.register()
    # bypass _ensure_clients by pre-populating the attrs
    p._core_v1 = DummyCoreV1()
    p._apps_v1 = DummyAppsV1()
    p._auth_ok = True
    return p


@pytest.fixture
def plugin_no_auth(monkeypatch):
    monkeypatch.delenv("KUBECONFIG", raising=False)
    p = K8sCorePlugin()
    p.config = {"in_cluster": False, "kubeconfig_path": None}
    p.register()
    return p


def _inject_core(p: K8sCorePlugin, core: DummyCoreV1) -> None:
    p._core_v1 = core
    p._apps_v1 = p._apps_v1 or DummyAppsV1()
    p._auth_ok = True


def _inject_apps(p: K8sCorePlugin, apps: DummyAppsV1) -> None:
    p._core_v1 = p._core_v1 or DummyCoreV1()
    p._apps_v1 = apps
    p._auth_ok = True


# ---------------------------------------------------------------------------
# Plugin-protocol compliance
# ---------------------------------------------------------------------------


def test_k8s_plugin_protocol_metadata(plugin_ok):
    assert plugin_ok.name == "k8s"
    assert plugin_ok.version == "0.1.0"
    assert plugin_ok.description
    assert "kubernetes" in plugin_ok.description.lower() or "k8s" in plugin_ok.description.lower()


def test_k8s_describe_tools_returns_nine_tools(plugin_ok):
    # CHG-2315: `node_hardware` kam dazu — was die Maschinen SIND, nicht wie es ihnen geht.
    tools = plugin_ok.describe_tools()
    assert len(tools) == 9
    names = {t.name for t in tools}
    assert names == {
        "cluster_status",
        "node_hardware",
        "get_pods",
        "get_logs",
        "restart_deployment",
        "scale_deployment",
        "describe_pod",
        "get_events",
        "exec_pod",
    }
    for t in tools:
        assert isinstance(t, ToolDescriptor)
        assert t.description
        assert t.input_schema
        assert "k8s" in t.tags


def test_k8s_health_degraded_without_auth(plugin_no_auth):
    h = plugin_no_auth.health()
    assert h["ok"] is False
    assert h["status"] == "degraded"
    assert "kubeconfig" in h["reason"].lower()


def test_k8s_health_healthy_with_kubeconfig(plugin_ok):
    h = plugin_ok.health()
    assert h["ok"] is True
    assert h["status"] == "healthy"
    assert h["mode"] == "kubeconfig"


def test_k8s_metrics_shape(plugin_ok):
    m = plugin_ok.metrics()
    assert m["plugin"] == "k8s"
    assert m["tools_exposed"] == 8
    assert m["default_namespace"] == "fyos"


def test_k8s_register_never_raises_without_auth(plugin_no_auth):
    # Fixture already exercised register(); no raise proves graceful-degradation.
    assert plugin_no_auth.in_cluster is False
    assert plugin_no_auth.kubeconfig_path is None


# ---------------------------------------------------------------------------
# cluster_status
# ---------------------------------------------------------------------------


def test_cluster_status_aggregates_pods_and_nodes(plugin_ok):
    core = DummyCoreV1(
        pods_response=SimpleNamespace(
            items=[
                _pod("agent-1", ready_containers=1, total_containers=1),
                _pod("aria-1", ready_containers=0, total_containers=1, phase="Pending"),
            ]
        ),
        nodes_response=SimpleNamespace(items=[_node("beta"), _node("pi-01")]),
    )
    _inject_core(plugin_ok, core)

    result = plugin_ok.call("cluster_status", {})
    assert result["pod_count"] == 2
    assert result["pods_ready"] == 1
    assert result["node_count"] == 2
    assert result["nodes_ready"] == 2
    assert any(c["op"] == "list_pod_for_all_namespaces" for c in core.calls)


def test_cluster_status_respects_namespace_filter(plugin_ok):
    core = DummyCoreV1(
        pods_response=SimpleNamespace(items=[_pod("agent-1", namespace="fyos")]),
        nodes_response=SimpleNamespace(items=[_node("beta")]),
    )
    _inject_core(plugin_ok, core)

    plugin_ok.call("cluster_status", {"namespace": "fyos"})
    assert any(c["op"] == "list_namespaced_pod" and c["namespace"] == "fyos" for c in core.calls)


# ---------------------------------------------------------------------------
# get_pods
# ---------------------------------------------------------------------------


def test_get_pods_applies_label_selector(plugin_ok):
    core = DummyCoreV1(pods_response=SimpleNamespace(items=[_pod("agent-1")]))
    _inject_core(plugin_ok, core)

    result = plugin_ok.call("get_pods", {"namespace": "fyos", "label_selector": "app=agent"})
    assert len(result) == 1
    assert result[0]["name"] == "agent-1"
    assert core.calls[0]["label_selector"] == "app=agent"
    assert core.calls[0]["namespace"] == "fyos"


# ---------------------------------------------------------------------------
# get_logs
# ---------------------------------------------------------------------------


def _log_aufruf(core: "DummyCoreV1") -> dict:
    """Der Log-Abruf selbst — davor liest die Fenster-Sperre den Pod (seit 03.10.)."""
    return next(c for c in core.calls if c["op"] == "read_namespaced_pod_log")


def test_get_logs_returns_string_with_tail(plugin_ok):
    core = DummyCoreV1(logs_response="line1\nline2\nline3\n")
    _inject_core(plugin_ok, core)

    result = plugin_ok.call("get_logs", {"pod": "agent-1", "namespace": "fyos", "lines": 3})
    assert result == "line1\nline2\nline3\n"
    assert _log_aufruf(core)["tail_lines"] == 3


def test_get_logs_passes_container_name(plugin_ok):
    core = DummyCoreV1(logs_response="")
    _inject_core(plugin_ok, core)

    plugin_ok.call(
        "get_logs",
        {"pod": "agent-1", "namespace": "fyos", "container": "sidecar", "lines": 10},
    )
    assert _log_aufruf(core)["container"] == "sidecar"


def test_get_logs_clamps_lines_to_max(plugin_ok):
    core = DummyCoreV1(logs_response="ok")
    _inject_core(plugin_ok, core)

    plugin_ok.call("get_logs", {"pod": "x", "lines": 99999})
    assert _log_aufruf(core)["tail_lines"] == 2000  # MAX_LOG_LINES


# ---------------------------------------------------------------------------
# restart_deployment + scale_deployment
# ---------------------------------------------------------------------------


def test_restart_deployment_patches_annotation(plugin_ok):
    apps = DummyAppsV1()
    _inject_apps(plugin_ok, apps)

    result = plugin_ok.call("restart_deployment", {"deployment": "agent", "namespace": "fyos"})
    assert result["deployment"] == "agent"
    assert result["namespace"] == "fyos"
    assert "restarted_at" in result
    call = apps.calls[0]
    assert call["op"] == "patch_namespaced_deployment"
    ann = call["body"]["spec"]["template"]["metadata"]["annotations"]
    assert "kubectl.kubernetes.io/restartedAt" in ann


def test_scale_deployment_to_zero_suspends(plugin_ok):
    apps = DummyAppsV1()
    _inject_apps(plugin_ok, apps)

    result = plugin_ok.call(
        "scale_deployment",
        {"deployment": "beta-32b", "replicas": 0, "namespace": "fyos"},
    )
    assert result["desired_replicas"] == 0
    assert apps.calls[0]["op"] == "patch_namespaced_deployment_scale"
    assert apps.calls[0]["body"]["spec"]["replicas"] == 0


def test_scale_deployment_to_three(plugin_ok):
    apps = DummyAppsV1()
    _inject_apps(plugin_ok, apps)

    result = plugin_ok.call(
        "scale_deployment",
        {"deployment": "aria", "replicas": 3, "namespace": "fyos"},
    )
    assert result["desired_replicas"] == 3


def test_scale_deployment_requires_replicas(plugin_ok):
    with pytest.raises(ValueError):
        plugin_ok.call("scale_deployment", {"deployment": "x"})


# ---------------------------------------------------------------------------
# describe_pod
# ---------------------------------------------------------------------------


def test_describe_pod_returns_spec_status_and_events(plugin_ok):
    core = DummyCoreV1(
        describe_response=_pod("agent-1", ready_containers=1, total_containers=1),
        events_response=SimpleNamespace(
            items=[_event("ev1", "Started", "container started", "agent-1")]
        ),
    )
    _inject_core(plugin_ok, core)

    result = plugin_ok.call("describe_pod", {"pod": "agent-1"})
    assert result["name"] == "agent-1"
    assert result["phase"] == "Running"
    assert len(result["containers"]) == 1
    assert result["containers"][0]["ready"] is True
    assert len(result["events"]) == 1
    assert result["events"][0]["reason"] == "Started"


def test_describe_pod_event_fetch_failure_is_soft(plugin_ok):
    """describe_pod MUST still return the pod detail if events-list raises."""

    # A core client that succeeds on read_namespaced_pod but raises on events.
    class MixedCore(DummyCoreV1):
        def list_namespaced_event(self, namespace: str, **kwargs: Any):  # type: ignore[override]
            raise FakeApiException(500, "etcd flaky")

    core = MixedCore(
        describe_response=_pod("agent-1"),
    )
    _inject_core(plugin_ok, core)

    result = plugin_ok.call("describe_pod", {"pod": "agent-1"})
    assert result["name"] == "agent-1"
    assert result["events"] == []


# ---------------------------------------------------------------------------
# get_events
# ---------------------------------------------------------------------------


def test_get_events_filters_by_since_seconds(plugin_ok):
    core = DummyCoreV1(
        events_response=SimpleNamespace(
            items=[
                _event("ev1", "Pulled", "pulled image", "agent-1", seconds_ago=30),
                _event("ev2", "Pulled", "old event", "aria-1", seconds_ago=9999),
            ]
        )
    )
    _inject_core(plugin_ok, core)

    result = plugin_ok.call("get_events", {"namespace": "fyos", "since_seconds": 300})
    # Only the 30-seconds-ago event fits 300s window
    assert len(result) == 1
    assert result[0]["reason"] == "Pulled"
    assert result[0]["involved_object"]["name"] == "agent-1"


# ---------------------------------------------------------------------------
# exec_pod (mock-only; no real stream)
# ---------------------------------------------------------------------------


def test_exec_pod_requires_list_command(plugin_ok):
    with pytest.raises(ValueError) as excinfo:
        plugin_ok.call("exec_pod", {"pod": "x", "command": "ls -la"})
    assert "list[str]" in str(excinfo.value)


def test_exec_pod_rejects_empty_command(plugin_ok):
    with pytest.raises(ValueError):
        plugin_ok.call("exec_pod", {"pod": "x", "command": []})


def test_exec_pod_streams_stdout_and_honors_timeout(plugin_ok, monkeypatch):
    """We monkeypatch kubernetes.stream.stream to return a fake websocket that
    closes immediately, proving the plugin loops + drains stdout correctly."""

    class FakeWS:
        def __init__(self):
            self._open_calls = 0
            self._stdout_calls = 0
            self._stderr_calls = 0
            self.closed = False

        def is_open(self):
            self._open_calls += 1
            return self._open_calls <= 2  # open for two iterations then close

        def update(self, timeout: int = 0):
            return None

        def read_stdout(self, timeout: int = 0):
            self._stdout_calls += 1
            if self._stdout_calls == 1:
                return "hello\n"
            return ""

        def read_stderr(self, timeout: int = 0):
            return ""

        def close(self):
            self.closed = True

    fake_ws = FakeWS()

    import sys
    import types as _types

    fake_module_stream = _types.ModuleType("kubernetes.stream")
    fake_module_stream.stream = lambda *args, **kwargs: fake_ws  # type: ignore
    fake_module_k8s = sys.modules.get("kubernetes") or _types.ModuleType("kubernetes")
    monkeypatch.setitem(sys.modules, "kubernetes", fake_module_k8s)
    monkeypatch.setitem(sys.modules, "kubernetes.stream", fake_module_stream)

    result = plugin_ok.call(
        "exec_pod",
        {"pod": "agent-1", "command": ["echo", "hello"], "timeout": 2},
    )
    assert result["stdout"] == "hello\n"
    assert result["timed_out"] is False
    assert fake_ws.closed is True


# Audit 2026-10-02: kubernetes 36 — Logs als "b'...'" und exec-Fehler als NoneType.decode.


def test_get_logs_dekodiert_bytes(plugin_ok):
    core = DummyCoreV1(logs_response=SimpleNamespace(data="zeile1\nümlaut\n".encode()))
    _inject_core(plugin_ok, core)

    result = plugin_ok.call("get_logs", {"pod": "agent-1", "namespace": "fyos", "lines": 2})
    assert result == "zeile1\nümlaut\n"
    assert not result.startswith("b'")
    assert _log_aufruf(core)["_preload_content"] is False


def _stream_attrappe(monkeypatch, ws=None, fehler=None):
    import sys
    import types as _types

    def _stream(*args, **kwargs):
        if fehler is not None:
            raise fehler
        return ws

    modul = _types.ModuleType("kubernetes.stream")
    modul.stream = _stream  # type: ignore[attr-defined]
    monkeypatch.setitem(
        sys.modules, "kubernetes", sys.modules.get("kubernetes") or _types.ModuleType("kubernetes")
    )
    monkeypatch.setitem(sys.modules, "kubernetes.stream", modul)


class _BeendeterWS:
    """Exec, das sofort fertig ist; Fehlerkanal (3) traegt einen v1.Status."""

    def __init__(self, status: str, stdout: bytes | str = b""):
        self._status, self._stdout = status, stdout

    def is_open(self):
        return False

    def update(self, timeout: int = 0):
        return None

    def read_stdout(self, timeout: int = 0):
        out, self._stdout = self._stdout, b""
        return out

    def read_stderr(self, timeout: int = 0):
        return ""

    def read_channel(self, channel: int, timeout: int = 0):
        return self._status if channel == 3 else ""

    def close(self):
        pass


def test_exec_pod_exit_code_null(plugin_ok, monkeypatch):
    _inject_core(plugin_ok, DummyCoreV1(describe_response=_pod("agent-1")))
    _stream_attrappe(monkeypatch, ws=_BeendeterWS('{"status":"Success"}'))
    result = plugin_ok.call("exec_pod", {"pod": "agent-1", "command": ["true"]})
    assert result["exit_code"] == 0
    assert "ok" not in result


def test_exec_pod_exit_code_ungleich_null_ist_fehler(plugin_ok, monkeypatch):
    _inject_core(plugin_ok, DummyCoreV1(describe_response=_pod("agent-1")))
    status = (
        '{"status":"Failure","message":"command terminated with non-zero exit code: 3",'
        '"reason":"NonZeroExitCode","details":{"causes":[{"reason":"ExitCode","message":"3"}]}}'
    )
    _stream_attrappe(monkeypatch, ws=_BeendeterWS(status, stdout=b"teil\n"))
    result = plugin_ok.call("exec_pod", {"pod": "agent-1", "command": ["sh", "-c", "exit 3"]})
    assert result["exit_code"] == 3
    assert result["ok"] is False
    assert result["stdout"] == "teil\n"


def test_exec_pod_unlesbarer_fehlerkanal_ist_kein_stiller_erfolg(plugin_ok, monkeypatch):
    """Lässt sich der Exit-Status nicht lesen, meldet exec_pod ok=false statt eines Erfolgs ohne Code."""

    class _KaputterKanal(_BeendeterWS):
        def read_channel(self, channel: int, timeout: int = 0):
            raise OSError("Connection reset")

    _inject_core(plugin_ok, DummyCoreV1(describe_response=_pod("agent-1")))
    _stream_attrappe(monkeypatch, ws=_KaputterKanal("", stdout=b"fertig\n"))
    result = plugin_ok.call("exec_pod", {"pod": "agent-1", "command": ["echo", "fertig"]})
    assert result["ok"] is False and "Exit-Status nicht lesbar" in result["error"]
    assert result["exit_code"] is None and result["stdout"] == "fertig\n"


def test_exec_pod_falscher_container_klar_benannt(plugin_ok, monkeypatch):
    pod = SimpleNamespace(spec=SimpleNamespace(containers=[SimpleNamespace(name="c0")]))
    _inject_core(plugin_ok, DummyCoreV1(describe_response=pod))
    _stream_attrappe(monkeypatch, ws=_BeendeterWS('{"status":"Success"}'))
    with pytest.raises(ValueError) as excinfo:
        plugin_ok.call("exec_pod", {"pod": "agent-1", "container": "falsch", "command": ["ls"]})
    assert "falsch" in str(excinfo.value) and "c0" in str(excinfo.value)


def test_exec_pod_handshake_fehler_lesbar(plugin_ok, monkeypatch):
    _inject_core(plugin_ok, DummyCoreV1(describe_response=_pod("agent-1")))
    _stream_attrappe(
        monkeypatch, fehler=AttributeError("'NoneType' object has no attribute 'decode'")
    )
    with pytest.raises(RuntimeError) as excinfo:
        plugin_ok.call("exec_pod", {"pod": "agent-1", "command": ["ls"]})
    assert "Handshake" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Error-path wrapping (401 / 403 / 404 / ApiException)
# ---------------------------------------------------------------------------


def test_k8s_401_auth_wraps_to_runtime_error(plugin_ok):
    core = DummyCoreV1(raise_on=FakeApiException(401, "Unauthorized", "bad token"))
    _inject_core(plugin_ok, core)

    with pytest.raises(RuntimeError) as excinfo:
        plugin_ok.call("cluster_status", {})
    assert "HTTP 401" in str(excinfo.value)
    assert "ServiceAccount" in str(excinfo.value)


def test_k8s_403_rbac_wraps_to_runtime_error(plugin_ok):
    apps = DummyAppsV1(raise_on=FakeApiException(403, "Forbidden", "RBAC denied"))
    _inject_apps(plugin_ok, apps)

    with pytest.raises(RuntimeError) as excinfo:
        plugin_ok.call("scale_deployment", {"deployment": "x", "replicas": 1})
    assert "HTTP 403" in str(excinfo.value)
    assert "RBAC" in str(excinfo.value)


def test_k8s_404_not_found_wraps(plugin_ok):
    core = DummyCoreV1(raise_on=FakeApiException(404, "Not Found", "pod ghost missing"))
    _inject_core(plugin_ok, core)

    with pytest.raises(RuntimeError) as excinfo:
        plugin_ok.call("describe_pod", {"pod": "ghost"})
    assert "404" in str(excinfo.value)


def test_k8s_500_includes_body_snippet(plugin_ok):
    core = DummyCoreV1(raise_on=FakeApiException(500, "Internal", "etcd unreachable"))
    _inject_core(plugin_ok, core)

    with pytest.raises(RuntimeError) as excinfo:
        plugin_ok.call("get_pods", {"namespace": "fyos"})
    assert "500" in str(excinfo.value)
    assert "etcd" in str(excinfo.value)


def test_k8s_unknown_tool_raises(plugin_ok):
    with pytest.raises(ValueError) as excinfo:
        plugin_ok.call("nonexistent", {})
    assert "nonexistent" in str(excinfo.value)


def test_k8s_call_without_auth_fails_clear(plugin_no_auth):
    # No clients + no auth-config → _ensure_clients should raise with a clear msg.
    with pytest.raises(RuntimeError) as excinfo:
        plugin_no_auth.call("cluster_status", {})
    assert "auth-config" in str(excinfo.value) or "kubeconfig" in str(excinfo.value)


def test_k8s_describe_pod_requires_name(plugin_ok):
    with pytest.raises(ValueError):
        plugin_ok.call("describe_pod", {})
    with pytest.raises(ValueError):
        plugin_ok.call("describe_pod", {"pod": ""})


# ---------------------------------------------------------------------------
# Integration: registry.call wraps plugin errors
# ---------------------------------------------------------------------------


def test_registry_wraps_k8s_crash_into_tool_invocation_result():
    plugin = K8sCorePlugin()
    plugin.config = {"in_cluster": False, "kubeconfig_path": "/tmp/fake"}

    reg = hub_registry.PluginRegistry()
    hub_registry._register_one(reg, "k8s", plugin)

    # Inject dummy AFTER registration. _auth_ok is toggled manually to bypass
    # real kubeconfig-loading in _ensure_clients().
    plugin._core_v1 = DummyCoreV1(raise_on=FakeApiException(403, "Forbidden", "rbac"))
    plugin._apps_v1 = DummyAppsV1()
    plugin._auth_ok = True

    res = hub_registry.call(reg, "k8s.get_pods", {"namespace": "fyos"})
    assert isinstance(res, ToolInvocationResult)
    assert res.status == "error"
    assert res.kind == "plugin_error"
    assert "403" in (res.error or "")


def test_registry_call_k8s_happy_path():
    plugin = K8sCorePlugin()
    plugin.config = {"in_cluster": False, "kubeconfig_path": "/tmp/fake"}

    reg = hub_registry.PluginRegistry()
    hub_registry._register_one(reg, "k8s", plugin)

    plugin._core_v1 = DummyCoreV1(pods_response=SimpleNamespace(items=[_pod("agent-1")]))
    plugin._apps_v1 = DummyAppsV1()
    plugin._auth_ok = True

    res = hub_registry.call(reg, "k8s.get_pods", {"namespace": "fyos"})
    assert res.status == "ok"
    assert isinstance(res.result, list)
    assert len(res.result) == 1
    assert res.result[0]["name"] == "agent-1"
    assert res.latency_ms >= 0


# ---------------------------------------------------------------------------
# Pure-function shaping tests (no API client at all)
# ---------------------------------------------------------------------------


def test_shape_pods_handles_empty_container_statuses():
    resp = SimpleNamespace(
        items=[
            SimpleNamespace(
                metadata=SimpleNamespace(name="x", namespace="fyos"),
                spec=SimpleNamespace(node_name="beta"),
                status=SimpleNamespace(
                    phase="Pending",
                    pod_ip=None,
                    host_ip=None,
                    start_time=None,
                    container_statuses=None,
                    conditions=None,
                ),
            )
        ]
    )
    out = _shape_pods(resp)
    assert len(out) == 1
    assert out[0]["ready"] is False
    assert out[0]["ready_ratio"] == "0/0"


def test_shape_nodes_detects_unready():
    resp = SimpleNamespace(
        items=[
            SimpleNamespace(
                metadata=SimpleNamespace(name="n1"),
                spec=SimpleNamespace(unschedulable=False),
                status=SimpleNamespace(
                    conditions=[SimpleNamespace(type="Ready", status="False")],
                    node_info=SimpleNamespace(kubelet_version="v1.30.0"),
                ),
            )
        ]
    )
    out = _shape_nodes(resp)
    assert out[0]["ready"] is False


def test_shape_pod_detail_classifies_waiting_state():
    pod = SimpleNamespace(
        metadata=SimpleNamespace(name="p", namespace="fyos"),
        spec=SimpleNamespace(node_name=None),
        status=SimpleNamespace(
            phase="Pending",
            pod_ip=None,
            host_ip=None,
            start_time=None,
            conditions=[],
            container_statuses=[
                SimpleNamespace(
                    name="c",
                    image="i",
                    ready=False,
                    restart_count=0,
                    state=SimpleNamespace(
                        running=None,
                        waiting=SimpleNamespace(reason="ImagePullBackOff"),
                        terminated=None,
                    ),
                ),
            ],
        ),
    )
    out = _shape_pod_detail(pod, [])
    assert out["containers"][0]["state"] == "waiting"
    assert out["containers"][0]["reason"] == "ImagePullBackOff"


def test_shape_events_extracts_involved_object():
    resp = SimpleNamespace(items=[_event("ev1", "Failed", "boom", "agent-1")])
    out = _shape_events(resp)
    assert out[0]["reason"] == "Failed"
    assert out[0]["involved_object"]["name"] == "agent-1"


# ---------------------------------------------------------------------------
# node_hardware (CHG-2315)
# ---------------------------------------------------------------------------
#
# Warum es dieses Werkzeug gibt: die Werkzeugkarte hatte 20 Domaenen und rund 130
# Werkzeuge, und KEINES beantwortete "welche Hardware haben wir". Weil der Abrufweg
# fehlte, wurde die Hardware in den Wissensgraphen geschrieben — und stand dort seit
# April, seit dem Umbau am 2026-07-18 falsch. Auf die Frage nach den GPUs gewann eine
# April-Antwort ("RTX 4090 24GB", importance critical) gegen die richtige August-Antwort.
#
# Wo eine Live-Quelle fehlt, fuellt sich die Luecke mit gespeicherten Behauptungen.


def _knoten(name, labels=None, allocatable=None, capacity=None):
    """Ein Node-Objekt in der Form, die der kubernetes-Client liefert (snake_case)."""
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels or {}),
        spec=SimpleNamespace(unschedulable=False),
        status=SimpleNamespace(
            conditions=[SimpleNamespace(type="Ready", status="True")],
            capacity=capacity or {"cpu": "8", "memory": "16Gi", "pods": "110"},
            allocatable=allocatable or {"cpu": "8", "memory": "15Gi"},
            node_info=SimpleNamespace(
                os_image="Ubuntu 24.04",
                kernel_version="6.17.0",
                architecture="amd64",
                kubelet_version="v1.34.9+k3s1",
                container_runtime_version="containerd://2.0",
            ),
        ),
    )


def test_node_hardware_nennt_karte_und_vergebbare_zahl(plugin_ok):
    """Beides, nicht eines: Etikett sagt was steckt, die Zahl was vergeben werden kann."""
    plugin_ok._core_v1 = DummyCoreV1(
        nodes_response=SimpleNamespace(
            items=[
                _knoten(
                    "beta",
                    labels={
                        "fyos/gpu": "rtx-pro-4500-blackwell",
                        "fyos/gpu-secondary": "rtx-pro-2000-blackwell",
                        "kubernetes.io/hostname": "beta",
                    },
                    allocatable={"cpu": "32", "memory": "121Gi", "nvidia.com/gpu": "1"},
                )
            ]
        )
    )
    res = plugin_ok.call("node_hardware", {})
    n = res["nodes"][0]

    assert n["gpu"]["labels"] == {
        "fyos/gpu": "rtx-pro-4500-blackwell",
        "fyos/gpu-secondary": "rtx-pro-2000-blackwell",
    }, (
        "Die GPU-Etiketten fehlen. Dann ist die Antwort auf 'welche Karte steckt drin' "
        "wieder nur im Gedaechtnis zu finden — genau der Zustand, gegen den dieses "
        "Werkzeug gebaut wurde."
    )
    assert n["gpu"]["vergebbar"] == "1", "Die vergebbare Zahl fehlt."
    assert n["cpu"]["vergebbar"] == "32", "vergebbare CPU fehlt"
    assert n["cpu"]["kapazitaet"] == "8", "kapazitaet kommt aus capacity, nicht allocatable"
    assert n["os"] == "Ubuntu 24.04"


def test_node_hardware_filtert_das_rauschen_weg(plugin_ok):
    """Ein Knoten traegt Dutzende Etiketten. Ungefiltert gehen die zwei wichtigen unter."""
    plugin_ok._core_v1 = DummyCoreV1(
        nodes_response=SimpleNamespace(
            items=[
                _knoten(
                    "beta",
                    labels={
                        "fyos/gpu": "rtx-pro-4500-blackwell",
                        "kubernetes.io/arch": "amd64",
                        "kustomize.toolkit.fluxcd.io/name": "irgendwas",
                        "node-role.kubernetes.io/control-plane": "true",
                        "beta.kubernetes.io/os": "linux",
                    },
                )
            ]
        )
    )
    hw = plugin_ok.call("node_hardware", {})["nodes"][0]["hardware_labels"]
    assert "fyos/gpu" in hw and "kubernetes.io/arch" in hw
    assert not any("fluxcd" in k for k in hw), (
        "Flux-Etiketten stehen in der Hardware-Auskunft. Eine Wand aus Etiketten ist "
        "keine Antwort — sie verschiebt das Suchen nur vom Cluster in die Ausgabe."
    )


def test_gpu_knoten_ohne_etikett_fallen_nicht_raus(plugin_ok):
    """Der Fehler, den ich am laufenden Cluster gemacht habe.

    `gamma` traegt KEIN `fyos/gpu`-Etikett (nur `fyos.io/hw=gamma`), hat aber eine
    vergebbare GPU. Eine `gpu_nodes`-Liste nur nach Etiketten haette ihn uebersehen —
    bei genau der Frage, wegen der es dieses Werkzeug gibt.
    """
    plugin_ok._core_v1 = DummyCoreV1(
        nodes_response=SimpleNamespace(
            items=[
                _knoten(
                    "gamma",
                    labels={"fyos.io/hw": "gamma"},
                    allocatable={"cpu": "6", "memory": "7Gi", "nvidia.com/gpu": "1"},
                ),
                _knoten("delta", labels={}, allocatable={"cpu": "4", "memory": "1Gi"}),
                _knoten(
                    "beta",
                    labels={"fyos/gpu": "rtx-pro-4500-blackwell"},
                    allocatable={"cpu": "32", "memory": "121Gi", "nvidia.com/gpu": "1"},
                ),
            ]
        )
    )
    res = plugin_ok.call("node_hardware", {})
    assert set(res["gpu_nodes"]) == {"gamma", "beta"}, (
        f"gpu_nodes={res['gpu_nodes']} — erwartet gamma UND beta. gamma hat eine "
        f"vergebbare GPU ohne GPU-Etikett; wer nur nach Etiketten filtert, meldet ihn "
        f"als GPU-los."
    )
    assert "delta" not in res["gpu_nodes"], "delta hat keine GPU und darf nicht drin stehen."


def test_unbekannter_knoten_ist_ein_fehler_kein_leeres_ergebnis(plugin_ok):
    """'Knoten gibt es nicht' und 'Knoten hat keine Hardware' sind verschiedene Aussagen."""
    plugin_ok._core_v1 = DummyCoreV1(nodes_response=SimpleNamespace(items=[_knoten("beta")]))
    with pytest.raises(Exception) as e:
        plugin_ok.call("node_hardware", {"node": "gibtsnicht"})
    assert "gibtsnicht" in str(e.value), (
        "Der Fehler nennt den gesuchten Knoten nicht. Eine leere Liste waere hier "
        "besonders schaedlich: sie laese sich als 'dieser Knoten hat keine Hardware' "
        "lesen (Merkposten `leer_statt_kaputt`)."
    )


# ---------------------------------------------------------------------------
# Schutzregeln wie im Steuerpult (2026-10-03): Fenster-Sperre, Flux-Hinweis
# ---------------------------------------------------------------------------


@pytest.fixture
def fenster(monkeypatch):
    """beta: Dev-Fenster offen; die anderen Knoten frei (echte lage.sperrgrund, Redis-Attrappe)."""
    import json as _json

    from fyos_plugin_betrieb import lage
    from test_lage import FakeRedis

    r = FakeRedis(keys={"fyos:wartung:beta": _json.dumps({"knoten": "beta", "grund": "[dev] Modell messen"})},
                  ttl={"fyos:wartung:beta": 7200})
    monkeypatch.setattr(lage, "_redis", lambda: r)
    return r


def _k8s_core(pods_auf: list[str], *, flux: bool = False):
    from fyos_plugin_betrieb.k8s_core import K8sCorePlugin

    class Apps:
        def __init__(self):
            self.calls = []

        def read_namespaced_deployment(self, name, namespace):
            mf = [SimpleNamespace(manager="kustomize-controller", operation="Apply", subresource=None,
                                  fields_v1={"f:spec": {"f:replicas": {}}})] if flux else []
            return SimpleNamespace(metadata=SimpleNamespace(labels={"kustomize.toolkit.fluxcd.io/name": "fyos-x"}, managed_fields=mf),
                                   spec=SimpleNamespace(selector=SimpleNamespace(match_labels={"app": name}, match_expressions=None)))

        def patch_namespaced_deployment(self, name, namespace, body):
            self.calls.append(("restart", name))
            return SimpleNamespace(spec=SimpleNamespace(replicas=1))

        def patch_namespaced_deployment_scale(self, name, namespace, body):
            self.calls.append(("scale", name, body["spec"]["replicas"]))
            return SimpleNamespace(spec=SimpleNamespace(replicas=body["spec"]["replicas"]), status=SimpleNamespace(replicas=1))

    class Core:
        def list_namespaced_pod(self, namespace, label_selector=None):
            assert label_selector == "app=x"
            return SimpleNamespace(items=[SimpleNamespace(spec=SimpleNamespace(node_name=k)) for k in pods_auf])

    p = K8sCorePlugin()
    p.config = {"in_cluster": False, "kubeconfig_path": "/tmp/x"}
    p.register()
    p._core_v1, p._apps_v1, p._auth_ok = Core(), Apps(), True
    return p


def test_k8s_core_restart_im_fenster_gesperrt(fenster):
    p = _k8s_core(["beta"])
    with pytest.raises(RuntimeError, match="gesperrt: Dev-Fenster auf beta"):
        p.call("restart_deployment", {"deployment": "x"})
    assert p._apps_v1.calls == []
    frei = _k8s_core(["alpha"])
    assert frei.call("restart_deployment", {"deployment": "x"})["deployment"] == "x"
    assert frei._apps_v1.calls == [("restart", "x")]


def test_k8s_core_scale_flux_nur_mit_trotzdem(fenster):
    p = _k8s_core(["alpha"], flux=True)
    with pytest.raises(RuntimeError, match="Flux .* verwaltet spec.replicas"):
        p.call("scale_deployment", {"deployment": "x", "replicas": 3})
    assert p._apps_v1.calls == []
    assert p.call("scale_deployment", {"deployment": "x", "replicas": 3, "trotzdem": True})["desired_replicas"] == 3
    schema = next(t for t in p.describe_tools() if t.name == "scale_deployment").input_schema
    assert schema["properties"]["trotzdem"]["default"] is False and set(schema["required"]) == {"deployment", "replicas"}


def test_logs_und_exec_auf_beta_im_fenster_gesperrt(plugin_ok, fenster, monkeypatch):
    """Nachtrag Etappe 4a: Logs und Exec waren ein Nebenweg auf beta, waehrend dort gemessen wird."""
    core = DummyCoreV1(describe_response=_pod("modell-x", node="beta"), logs_response="zeile")
    _inject_core(plugin_ok, core)
    with pytest.raises(RuntimeError, match="gesperrt: Dev-Fenster auf beta"):
        plugin_ok.call("get_logs", {"pod": "modell-x", "namespace": "fyos"})
    with pytest.raises(RuntimeError, match="gesperrt: Dev-Fenster auf beta"):
        plugin_ok.call("exec_pod", {"pod": "modell-x", "namespace": "fyos", "command": ["ls"]})
    assert not any(c["op"] in ("read_namespaced_pod_log", "connect_get_namespaced_pod_exec") for c in core.calls)


def test_logs_auf_anderem_knoten_frei(plugin_ok, fenster):
    core = DummyCoreV1(describe_response=_pod("agent-1", node="alpha"), logs_response="zeile")
    _inject_core(plugin_ok, core)
    assert "zeile" in plugin_ok.call("get_logs", {"pod": "agent-1", "namespace": "fyos"})


def test_logs_bleiben_lesbar_wenn_redis_klemmt_exec_nicht(plugin_ok, monkeypatch):
    from fyos_plugin_betrieb import lage

    def kaputt():
        raise ConnectionError("redis weg")

    monkeypatch.setattr(lage, "_redis", kaputt)
    core = DummyCoreV1(describe_response=_pod("modell-x", node="beta"), logs_response="zeile")
    _inject_core(plugin_ok, core)
    assert "zeile" in plugin_ok.call("get_logs", {"pod": "modell-x", "namespace": "fyos"})
    with pytest.raises(RuntimeError, match="nicht lesbar"):
        plugin_ok.call("exec_pod", {"pod": "modell-x", "namespace": "fyos", "command": ["ls"]})
