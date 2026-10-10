"""mcp_hub.plugins.k8s_core — Kubernetes Core Control-Plane Plugin (S-MCP-HUB.1.5).

Splits the monolithic ``mcp__fyos-control__k8s_*`` tools into a dedicated
HubPlugin that talks to the Kubernetes API via the official ``kubernetes``
Python-client — no fyos-control-api daisy-chain. Eight MVP tools covering
the read + controlled-write surface a Hub-caller typically needs:

    k8s.cluster_status(namespace=None)                → CoreV1: list pods + nodes
    k8s.get_pods(namespace, label_selector)           → CoreV1: list_namespaced_pod
    k8s.get_logs(pod, namespace, lines, container)    → CoreV1: read_namespaced_pod_log
    k8s.restart_deployment(deployment, namespace)     → AppsV1: patch annotation trick
    k8s.scale_deployment(deployment, replicas, ns)    → AppsV1: patch_namespaced_deployment_scale
    k8s.describe_pod(pod, namespace)                  → CoreV1: read_namespaced_pod + events
    k8s.get_events(namespace, since_seconds)          → CoreV1: list_namespaced_event
    k8s.exec_pod(pod, command, namespace, container)  → stream() with timeout

Why a direct-API plugin (ADR-105 §2.3 / Option A):
    - Lower latency — no extra Hub↔fyos-control-api hop.
    - Richer output — describe_pod exposes conditions + events for drilling.
    - Independent lifecycle — k8s plugin may be disabled without touching
      fyos-control.
    - Mirrors ``scripts/svc.sh`` / ``kubectl`` semantics — the goldene CLI
      from Session 181.

Auth (ADR-105 §2.6):
    - In-cluster ServiceAccount by default (``config.load_incluster_config()``).
    - Fallback: ``KUBECONFIG`` env-var path for dev (``config.load_kube_config()``).
    - Missing both → plugin registers in ``degraded`` state; ``call`` returns a
      structured ``kubeconfig_missing`` error (no crash, no Hub-startup-block
      per ADR-105 §3.2 graceful-degradation).

Error Surface → ToolInvocationResult.kind (via wrap-not-raise in registry.call):
    - ``plugin_error``  — ApiException (4xx/5xx) from kube-apiserver.
    - ``circuit_open``  — 5 consecutive failures → breaker fast-fails.
    - ``not_found``     — unknown tool name inside this plugin.

Plugin-Init is non-blocking: no startup-time probe to the apiserver (would
block Hub boot if apiserver is momentarily unreachable). ``register()`` only
resolves config; clients are lazily constructed on first ``call()``. This
mirrors argocd.py (S-MCP-HUB.1.4).

Security:
    - ``exec_pod`` executes arbitrary commands inside a running container.
      Mitigations:
        1. RBAC at ``k8s/base/mcp-hub/rbac.yaml`` is a ClusterRole: pods/exec
           and deployments patch/scale apply to EVERY namespace. Bewusst so
           belassen (Entscheidung des Betreibers, 2026-09-29: lieber Wirkung als Abschirmung).
        2. ``command`` is ``list[str]`` — no shell-expansion, no injection.
        3. Hard timeout (default 30s) via ``pre_load_content=True`` + read-
           loop. Child is killed on timeout-exceeded.
        4. Every call is audited via the audit-plugin on_call hook (1.3).
    - ``restart_deployment`` / ``scale_deployment`` tragen seit 2026-10-03 dieselben
      Schutzregeln wie das Steuerpult (cluster.neustarten/skalieren): gesperrt, solange ein
      Pod auf einem Knoten mit Dev/Test-Fenster laeuft (lage.sperrgrund); Skalieren eines von
      Flux oder einem Autoscaler verwalteten spec.replicas nur mit ``trotzdem: true``. So ist
      dieser Weg kein ungeschuetzter Nebenweg. Trockenlauf und Grund: das Plugin ``cluster``.

RBAC required (see k8s/base/mcp-hub/rbac.yaml):
    - pods: get, list, watch
    - pods/log: get
    - pods/exec: create
    - deployments: get, list, patch
    - deployments/scale: get, patch
    - events: list, watch
    - nodes: list, get        (cluster-scope; via ClusterRole)
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from mcp_hub.plugin import HubPlugin, ToolDescriptor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config defaults
# ---------------------------------------------------------------------------

DEFAULT_NAMESPACE = "fyos"
"""Primary FYOS namespace — tools fall back to this if caller omits namespace."""

DEFAULT_TIMEOUT = 30.0
"""Seconds cap for exec_pod / log-streams. Apiserver ops default to client TO."""

DEFAULT_LOG_LINES = 50
"""get_logs tail default. Caps implicit to guard tokens."""

MAX_LOG_LINES = 2000
"""get_logs hard cap — Hub audit-log + MCP transport both slow above ~200K chars."""


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class K8sCorePlugin(HubPlugin):
    """Eight-tool Kubernetes Core control-plane plugin (read + controlled-write)."""

    name = "k8s"
    description = (
        "Kubernetes Core plugin — cluster_status, get_pods, get_logs, "
        "restart_deployment, scale_deployment, describe_pod, get_events, "
        "exec_pod. Direct kube-apiserver (ADR-105 §2.3 Option A)."
    )
    version = "0.1.0"

    # Instance attributes (populated in register()).
    config: dict[str, Any]
    in_cluster: bool
    kubeconfig_path: Optional[str]
    default_namespace: str
    timeout: float
    _core_v1: Any
    _apps_v1: Any
    _auth_ok: bool
    _auth_error: Optional[str]

    # --- lifecycle ---------------------------------------------------------

    def register(self) -> None:
        """Resolve config + env. Does NOT open API connections (lazy on first call)."""
        cfg: dict[str, Any] = getattr(self, "config", {}) or {}
        self.config = cfg
        self.in_cluster = bool(cfg.get("in_cluster", True))
        kube_path = cfg.get("kubeconfig_path") or os.getenv("KUBECONFIG") or None
        self.kubeconfig_path = str(kube_path) if kube_path else None
        self.default_namespace = str(cfg.get("default_namespace") or DEFAULT_NAMESPACE)
        self.timeout = float(cfg.get("timeout_sec") or DEFAULT_TIMEOUT)

        # Clients are created lazily; track auth-state for health() reporting.
        self._core_v1 = None
        self._apps_v1 = None
        self._auth_ok = False
        self._auth_error = None

        if not self.in_cluster and not self.kubeconfig_path:
            logger.warning(
                "mcp_hub.plugins.k8s_core: no in_cluster and no kubeconfig — "
                "plugin registered in degraded state (calls return "
                "kubeconfig_missing)"
            )

    def init(self) -> None:
        # No cross-plugin wiring needed.
        return None

    def shutdown(self) -> None:
        """Drop client references (best-effort, never raises)."""
        self._core_v1 = None
        self._apps_v1 = None

    # --- tool descriptors --------------------------------------------------

    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="cluster_status",
                description=(
                    "Return a compact cluster overview — all pods (Ready/Running "
                    "count per namespace) + all nodes (Ready/SchedulingDisabled). "
                    "Optional namespace filter scopes pod-list."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "namespace": {
                            "type": "string",
                            "description": "Optional namespace filter for pods (nodes are cluster-scope).",
                        },
                    },
                    "additionalProperties": False,
                },
                tags=["k8s", "cluster", "read"],
            ),
            ToolDescriptor(
                name="node_hardware",
                description=(
                    "What the machines ARE: per node the hardware labels "
                    "(fyos/gpu, fyos/gpu-secondary, arch, instance-type), capacity and "
                    "allocatable (cpu / memory / nvidia.com/gpu), OS, kernel and kubelet. "
                    "Read this instead of recalling hardware from memory — cards, RAM and "
                    "nodes change, stored answers do not."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "node": {
                            "type": "string",
                            "description": "Optional single node name (as listed by the cluster).",
                        },
                    },
                    "additionalProperties": False,
                },
                tags=["k8s", "cluster", "hardware", "inventory", "gpu", "read"],
            ),
            ToolDescriptor(
                name="get_pods",
                description=(
                    "List pods in a namespace with name/phase/ready/node/age. "
                    "Optional label_selector filters the list "
                    "(e.g. 'app.kubernetes.io/name=web')."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string", "default": DEFAULT_NAMESPACE},
                        "label_selector": {
                            "type": "string",
                            "description": "K8s label-selector string.",
                        },
                    },
                    "additionalProperties": False,
                },
                tags=["k8s", "pods", "read"],
            ),
            ToolDescriptor(
                name="get_logs",
                description=(
                    "Tail logs from a container. Defaults to 50 lines, hard-cap "
                    f"{MAX_LOG_LINES}. If ``container`` is None the first "
                    "container in the pod is used."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "pod": {"type": "string", "description": "Pod name."},
                        "namespace": {"type": "string", "default": DEFAULT_NAMESPACE},
                        "lines": {
                            "type": "integer",
                            "default": DEFAULT_LOG_LINES,
                            "minimum": 1,
                            "maximum": MAX_LOG_LINES,
                        },
                        "container": {
                            "type": "string",
                            "description": "Container-name (multi-container pods).",
                        },
                    },
                    "required": ["pod"],
                    "additionalProperties": False,
                },
                tags=["k8s", "logs", "read"],
            ),
            ToolDescriptor(
                name="restart_deployment",
                description=(
                    "Trigger a rolling restart of a Deployment by patching the "
                    "kubectl.kubernetes.io/restartedAt annotation (equivalent to "
                    "``kubectl rollout restart``). Refused while a pod of the "
                    "Deployment runs on a node with an open dev/test window "
                    "(lage.sperrgrund). With dry run + reason: cluster.neustarten."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "deployment": {"type": "string"},
                        "namespace": {"type": "string", "default": DEFAULT_NAMESPACE},
                    },
                    "required": ["deployment"],
                    "additionalProperties": False,
                },
                tags=["k8s", "deployment", "write"],
            ),
            ToolDescriptor(
                name="scale_deployment",
                description=(
                    "Scale a Deployment's replica-count. replicas=0 suspends "
                    "the workload (e.g. an on-demand scale-from-zero pattern). Refused while "
                    "a pod runs on a node with an open dev/test window. If Flux (or "
                    "an autoscaler) manages spec.replicas, the next reconcile resets "
                    "it — then only with trotzdem=true; lasting change only via Git. "
                    "With dry run + reason: cluster.skalieren."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "deployment": {"type": "string"},
                        "replicas": {"type": "integer", "minimum": 0, "maximum": 100},
                        "namespace": {"type": "string", "default": DEFAULT_NAMESPACE},
                        "trotzdem": {
                            "type": "boolean",
                            "default": False,
                            "description": "scale although Flux/autoscaler will reset it",
                        },
                    },
                    "required": ["deployment", "replicas"],
                    "additionalProperties": False,
                },
                tags=["k8s", "deployment", "write"],
            ),
            ToolDescriptor(
                name="describe_pod",
                description=(
                    "Full detail on one pod: spec, status, conditions, container "
                    "statuses, + most-recent events involving the pod."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "pod": {"type": "string"},
                        "namespace": {"type": "string", "default": DEFAULT_NAMESPACE},
                    },
                    "required": ["pod"],
                    "additionalProperties": False,
                },
                tags=["k8s", "pods", "read", "drilling"],
            ),
            ToolDescriptor(
                name="get_events",
                description=(
                    "List recent events in a namespace. since_seconds filters "
                    "to recent events only (default 300s = 5 min)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "namespace": {"type": "string", "default": DEFAULT_NAMESPACE},
                        "since_seconds": {
                            "type": "integer",
                            "default": 300,
                            "minimum": 1,
                            "maximum": 86400,
                        },
                    },
                    "additionalProperties": False,
                },
                tags=["k8s", "events", "read"],
            ),
            ToolDescriptor(
                name="exec_pod",
                description=(
                    "Execute a command inside a container. command MUST be a "
                    "list[str] (no shell expansion). Timeout kills the stream "
                    "after ``timeout`` seconds (default 30, hard cap 120). "
                    "Works in every namespace (ClusterRole)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "pod": {"type": "string"},
                        "command": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                        },
                        "namespace": {"type": "string", "default": DEFAULT_NAMESPACE},
                        "container": {"type": "string"},
                        "timeout": {
                            "type": "integer",
                            "default": 30,
                            "minimum": 1,
                            "maximum": 120,
                        },
                    },
                    "required": ["pod", "command"],
                    "additionalProperties": False,
                },
                tags=["k8s", "exec", "write", "dangerous"],
            ),
        ]

    # --- dispatch ----------------------------------------------------------

    def call(self, method: str, args: dict[str, Any]) -> Any:
        """Dispatch a tool-call. Raises on unknown method or API errors —
        the registry wraps exceptions into ToolInvocationResult (kind=plugin_error).
        """
        if method == "node_hardware":
            n = args.get("node")
            return self._node_hardware(node=str(n) if n else None)
        if method == "cluster_status":
            ns = args.get("namespace")
            return self._cluster_status(namespace=str(ns) if ns else None)
        if method == "get_pods":
            return self._get_pods(
                namespace=str(args.get("namespace") or self.default_namespace),
                label_selector=args.get("label_selector"),
            )
        if method == "get_logs":
            return self._get_logs(
                pod=_require_str(args, "pod"),
                namespace=str(args.get("namespace") or self.default_namespace),
                lines=_clamp_int(args.get("lines"), DEFAULT_LOG_LINES, 1, MAX_LOG_LINES),
                container=args.get("container"),
            )
        if method == "restart_deployment":
            return self._restart_deployment(
                deployment=_require_str(args, "deployment"),
                namespace=str(args.get("namespace") or self.default_namespace),
            )
        if method == "scale_deployment":
            return self._scale_deployment(
                deployment=_require_str(args, "deployment"),
                replicas=_require_int(args, "replicas"),
                namespace=str(args.get("namespace") or self.default_namespace),
                trotzdem=args.get("trotzdem") is True,
            )
        if method == "describe_pod":
            return self._describe_pod(
                pod=_require_str(args, "pod"),
                namespace=str(args.get("namespace") or self.default_namespace),
            )
        if method == "get_events":
            return self._get_events(
                namespace=str(args.get("namespace") or self.default_namespace),
                since_seconds=_clamp_int(args.get("since_seconds"), 300, 1, 86400),
            )
        if method == "exec_pod":
            cmd = args.get("command")
            if not isinstance(cmd, list) or not cmd or not all(isinstance(c, str) for c in cmd):
                raise ValueError("k8s.exec_pod requires 'command' as non-empty list[str]")
            return self._exec_pod(
                pod=_require_str(args, "pod"),
                command=[str(c) for c in cmd],
                namespace=str(args.get("namespace") or self.default_namespace),
                container=args.get("container"),
                timeout=_clamp_int(args.get("timeout"), int(self.timeout), 1, 120),
            )
        raise ValueError(f"K8sCorePlugin has no tool {method!r}")

    # --- observability -----------------------------------------------------

    def health(self) -> dict[str, Any]:
        """Report config-state. Does NOT probe apiserver (readiness-checks)."""
        if not self.in_cluster and not self.kubeconfig_path:
            return {
                "ok": False,
                "status": "degraded",
                "plugin": self.name,
                "reason": "no in_cluster and no kubeconfig_path",
            }
        return {
            "ok": True,
            "status": "healthy",
            "plugin": self.name,
            "mode": "in_cluster" if self.in_cluster else "kubeconfig",
            "default_namespace": self.default_namespace,
        }

    def metrics(self) -> dict[str, Any]:
        return {
            "plugin": self.name,
            "tools_exposed": 8,
            "mode": "in_cluster" if self.in_cluster else "kubeconfig",
            "default_namespace": self.default_namespace,
            "client_initialized": self._auth_ok,
        }

    # --- internals: client plumbing ----------------------------------------

    def _ensure_clients(self) -> tuple[Any, Any]:
        """Lazily build CoreV1Api + AppsV1Api. Raises RuntimeError on auth fail
        (caught by ``registry.call`` + wrapped into ToolInvocationResult)."""
        if self._core_v1 is not None and self._apps_v1 is not None:
            return self._core_v1, self._apps_v1

        if not self.in_cluster and not self.kubeconfig_path:
            raise RuntimeError(
                "k8s plugin has no auth-config — set in_cluster=true (pod with "
                "ServiceAccount) OR provide kubeconfig_path / KUBECONFIG env-var"
            )

        try:
            from kubernetes import client as k8s_client  # noqa: WPS433
            from kubernetes import config as k8s_config  # noqa: WPS433
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                f"kubernetes python package missing: {exc} — add 'kubernetes>=29' "
                f"to mcp_hub/requirements.txt"
            ) from exc

        try:
            if self.in_cluster:
                k8s_config.load_incluster_config()
            else:
                k8s_config.load_kube_config(config_file=self.kubeconfig_path)
        except Exception as exc:  # noqa: BLE001
            self._auth_ok = False
            self._auth_error = str(exc)
            raise RuntimeError(
                f"k8s auth failed ({'in_cluster' if self.in_cluster else 'kubeconfig'}): {exc}"
            ) from exc

        self._core_v1 = k8s_client.CoreV1Api()
        self._apps_v1 = k8s_client.AppsV1Api()
        self._auth_ok = True
        self._auth_error = None
        return self._core_v1, self._apps_v1

    def _wrap_api_exc(self, exc: Exception, what: str) -> RuntimeError:
        """Normalize kubernetes.client.exceptions.ApiException → informative RuntimeError."""
        status = getattr(exc, "status", None)
        reason = getattr(exc, "reason", None)
        body = getattr(exc, "body", None)
        body_snip = (str(body) or "")[:500] if body is not None else ""
        if status == 401:
            return RuntimeError(f"k8s {what}: HTTP 401 — ServiceAccount token invalid or expired")
        if status == 403:
            return RuntimeError(
                f"k8s {what}: HTTP 403 — RBAC forbids this verb (check k8s/base/mcp-hub/rbac.yaml)"
            )
        if status == 404:
            return RuntimeError(f"k8s {what}: HTTP 404 not found")
        if status is not None:
            return RuntimeError(f"k8s {what}: HTTP {status} {reason or ''} {body_snip}".strip())
        return RuntimeError(f"k8s {what}: {type(exc).__name__}: {exc}")

    # --- internals: per-tool impls ----------------------------------------

    def _cluster_status(self, namespace: Optional[str]) -> dict[str, Any]:
        core_v1, _ = self._ensure_clients()
        try:
            if namespace:
                pods_resp = core_v1.list_namespaced_pod(namespace=namespace)
            else:
                pods_resp = core_v1.list_pod_for_all_namespaces()
            nodes_resp = core_v1.list_node()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_api_exc(exc, "cluster_status") from exc

        pods = _shape_pods(pods_resp)
        nodes = _shape_nodes(nodes_resp)
        ready_pods = sum(1 for p in pods if p["ready"])
        ready_nodes = sum(1 for n in nodes if n["ready"])
        return {
            "namespace_filter": namespace,
            "pods": pods,
            "pod_count": len(pods),
            "pods_ready": ready_pods,
            "nodes": nodes,
            "node_count": len(nodes),
            "nodes_ready": ready_nodes,
        }

    def _node_hardware(self, node: Optional[str]) -> dict[str, Any]:
        """Was die Maschinen SIND — live aus der k8s-API.

        ## Warum es dieses Werkzeug gibt (CHG-2315, 2026-08-12)

        Die Werkzeugkarte hatte 20 Domaenen und rund 130 Werkzeuge, und **keines**
        beantwortete „welche Hardware haben wir". `cluster_status` holt die Node-Objekte
        bereits und wirft Etiketten, Kapazitaet und OS weg (`_shape_nodes`).

        Die Folge war messbar: weil der Abrufweg fehlte, wurde die Hardware in den
        Wissensgraphen geschrieben — und dort steht sie seit April und ist seit dem
        Umbau falsch. Auf die Frage „welche GPUs hat dieser Knoten" gewann eine
        alte Antwort (importance: critical) gegen die richtige neuere Antwort (importance: medium).

        **Wo eine Live-Quelle fehlt, fuellt sich die Luecke mit gespeicherten
        Behauptungen — und die altern still.** Das ist der Mechanismus hinter
        `dynamisches_nie_statisch`; die Regel verbietet den festen Eintrag, aber der
        Grund fuer den Rueckfall ist meist ein fehlendes Werkzeug, nicht Bequemlichkeit.

        ## Grenzen, ausdruecklich benannt

        Geliefert wird, was die k8s-API weiss: Etiketten, Kapazitaet, OS/Kernel. Das ist
        **kein `nvidia-smi`** — aktuelle VRAM-Belegung, Temperatur und Taktraten stehen
        hier nicht. Dafuer liefert `dcgm-exporter` auf dem GPU-Knoten Metriken an Prometheus; das
        waere die Ausbaustufe. Wer hier eine Belegung sucht und keine findet, hat nicht
        gemessen, dass keine da ist — er hat die falsche Quelle gefragt.

        `nvidia.com/gpu` in `allocatable` zaehlt nur Karten, die ein Device-Plugin
        **exponiert**. Eine Karte ohne Plugin ist physisch vorhanden und hier eine 0.
        Deshalb stehen die Etiketten daneben: sie sagen, was steckt, die Zahl sagt, was
        der Scheduler vergeben kann. Beides zusammen ist die Auskunft, keines allein.
        """
        core_v1, _ = self._ensure_clients()
        try:
            if node:
                nodes_resp = core_v1.list_node(field_selector=f"metadata.name={node}")
            else:
                nodes_resp = core_v1.list_node()
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_api_exc(exc, "node_hardware") from exc

        nodes = _shape_node_hardware(nodes_resp)
        if node and not nodes:
            # Kein leeres Ergebnis als Antwort: „Knoten gibt es nicht" und „Knoten hat
            # keine Hardware" sind verschiedene Aussagen (Merkposten `leer_statt_kaputt`).
            raise RuntimeError(
                f"k8s node_hardware: Knoten '{node}' gibt es nicht. "
                f"Vorhanden: {', '.join(n['name'] for n in _shape_node_hardware(core_v1.list_node()))}"
            )
        return {
            "nodes": nodes,
            "node_count": len(nodes),
            # ODER, nicht UND: ein Edge-Knoten kann KEIN `fyos/gpu`-Etikett tragen (nur `fyos.io/hw=edge`),
            # hat aber eine vergebbare GPU. Eine Liste nur nach Etiketten haette ihn
            # uebersehen — und „welche Knoten haben GPUs" ist genau die Frage, wegen der
            # es dieses Werkzeug gibt. Am laufenden Cluster aufgefallen, nicht gedacht.
            "gpu_nodes": [
                n["name"]
                for n in nodes
                if n["gpu"]["labels"] or str(n["gpu"]["vergebbar"]) not in ("0", "None", "")
            ],
            "quelle": "kubernetes API (live)",
            "hinweis": (
                "Etiketten sagen, welche Karte steckt; nvidia.com/gpu sagt, wie viele "
                "der Scheduler vergeben kann. Fuer VRAM-Belegung/Temperatur: dcgm-exporter "
                "auf dem GPU-Knoten -> Prometheus, hier NICHT enthalten."
            ),
        }

    def _get_pods(self, namespace: str, label_selector: Optional[str]) -> list[dict[str, Any]]:
        core_v1, _ = self._ensure_clients()
        kwargs: dict[str, Any] = {"namespace": namespace}
        if label_selector:
            kwargs["label_selector"] = str(label_selector)
        try:
            resp = core_v1.list_namespaced_pod(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_api_exc(exc, f"get_pods({namespace})") from exc
        return _shape_pods(resp)

    def _fenster_sperre(self, core_v1: Any, pod: str, namespace: str, was: str, *, streng: bool) -> None:
        """Fenster-Sperre fuer Logs und Exec (2026-10-03, Nachtrag Etappe 4a): sonst waeren beide ein
        Nebenweg auf den Messknoten, waehrend dort gemessen wird — Logs gehen ueber das Kubelet des Knotens,
        Exec startet dort Prozesse.

        `streng` (Exec): ist der Fensterzustand nicht lesbar, wird gesperrt. Logs bleiben dann
        lesbar — sie werden gerade gebraucht, wenn etwas klemmt (auch Redis)."""
        try:
            knoten = _attr(_attr(core_v1.read_namespaced_pod(name=pod, namespace=namespace), "spec"), "node_name")
        except Exception:  # noqa: BLE001 — die eigentliche Abfrage meldet den Fehler lesbar
            return
        if not isinstance(knoten, str) or not knoten:
            return
        from . import lage  # noqa: PLC0415 — spaet, wie in _schutz

        if not streng:
            try:
                lage.fenster_offen(knoten)
            except Exception:  # noqa: BLE001
                return
        grund = lage.sperrgrund(knoten)
        if grund:
            raise RuntimeError(f"k8s {was}: gesperrt: {grund}")

    def _get_logs(self, pod: str, namespace: str, lines: int, container: Optional[str]) -> str:
        core_v1, _ = self._ensure_clients()
        self._fenster_sperre(core_v1, pod, namespace, f"get_logs({namespace}/{pod})", streng=False)
        kwargs: dict[str, Any] = {
            "name": pod,
            "namespace": namespace,
            "tail_lines": int(lines),
            "timestamps": False,
            # Audit 2026-10-02: kubernetes 36 liefert den Log-Text als `str(bytes)` —
            # `"b'...\\n...'"` statt Zeilen. Rohantwort holen und selbst dekodieren.
            "_preload_content": False,
        }
        if container:
            kwargs["container"] = str(container)
        try:
            resp = core_v1.read_namespaced_pod_log(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_api_exc(exc, f"get_logs({namespace}/{pod})") from exc
        return _als_text(getattr(resp, "data", resp))

    def _schutz(self, deployment: str, namespace: str, *, skalieren: bool, trotzdem: bool) -> None:
        """Dieselben Schutzregeln wie das Steuerpult (cluster.neustarten/skalieren), damit dieser Weg
        kein ungeschuetzter Nebenweg ist (Etappe 4a, 2026-10-03): Fenster-Sperre fuer die Knoten der
        Pods; beim Skalieren der Flux-/Autoscaler-Hinweis (nur mit trotzdem)."""
        core_v1, apps_v1 = self._ensure_clients()
        was = f"{'scale' if skalieren else 'restart'}_deployment({namespace}/{deployment})"
        try:
            dep = apps_v1.read_namespaced_deployment(name=deployment, namespace=namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_api_exc(exc, was) from exc
        selektor = _selektor_text(_attr(_attr(dep, "spec"), "selector"))
        pods = None
        if selektor:
            try:
                pods = core_v1.list_namespaced_pod(namespace=namespace, label_selector=selektor)
            except Exception as exc:  # noqa: BLE001
                raise self._wrap_api_exc(exc, was) from exc
        # Spaet importiert: cluster importiert lage — beim Laden von k8s_core soll nichts davon mitkommen.
        from .cluster import schutz_deployment  # noqa: PLC0415

        grund = schutz_deployment(dep, pods, skalieren=skalieren, trotzdem=trotzdem)
        if grund:
            raise RuntimeError(f"k8s {was}: {grund}")

    def _restart_deployment(self, deployment: str, namespace: str) -> dict[str, Any]:
        self._schutz(deployment, namespace, skalieren=False, trotzdem=False)
        _, apps_v1 = self._ensure_clients()
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        patch = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "kubectl.kubernetes.io/restartedAt": now_iso,
                        }
                    }
                }
            }
        }
        try:
            resp = apps_v1.patch_namespaced_deployment(
                name=deployment, namespace=namespace, body=patch
            )
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_api_exc(exc, f"restart_deployment({namespace}/{deployment})") from exc

        spec = getattr(resp, "spec", None)
        return {
            "deployment": deployment,
            "namespace": namespace,
            "restarted_at": now_iso,
            "replicas": getattr(spec, "replicas", None) if spec else None,
        }

    def _scale_deployment(
        self, deployment: str, replicas: int, namespace: str, trotzdem: bool = False
    ) -> dict[str, Any]:
        self._schutz(deployment, namespace, skalieren=True, trotzdem=trotzdem)
        _, apps_v1 = self._ensure_clients()
        body = {"spec": {"replicas": int(replicas)}}
        try:
            resp = apps_v1.patch_namespaced_deployment_scale(
                name=deployment, namespace=namespace, body=body
            )
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_api_exc(
                exc, f"scale_deployment({namespace}/{deployment}={replicas})"
            ) from exc

        spec = getattr(resp, "spec", None)
        status = getattr(resp, "status", None)
        return {
            "deployment": deployment,
            "namespace": namespace,
            "desired_replicas": getattr(spec, "replicas", replicas) if spec else replicas,
            "current_replicas": getattr(status, "replicas", None) if status else None,
        }

    def _describe_pod(self, pod: str, namespace: str) -> dict[str, Any]:
        core_v1, _ = self._ensure_clients()
        try:
            pod_obj = core_v1.read_namespaced_pod(name=pod, namespace=namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_api_exc(exc, f"describe_pod({namespace}/{pod})") from exc

        # Event-fetch is best-effort — don't fail the whole call if events list fails.
        events: list[dict[str, Any]] = []
        try:
            ev_resp = core_v1.list_namespaced_event(
                namespace=namespace,
                field_selector=f"involvedObject.name={pod}",
            )
            events = _shape_events(ev_resp)
        except Exception as exc:  # noqa: BLE001
            logger.debug("describe_pod event-fetch failed (soft): %s", exc)

        return _shape_pod_detail(pod_obj, events)

    def _get_events(self, namespace: str, since_seconds: int) -> list[dict[str, Any]]:
        core_v1, _ = self._ensure_clients()
        try:
            resp = core_v1.list_namespaced_event(namespace=namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_api_exc(exc, f"get_events({namespace})") from exc

        threshold = time.time() - int(since_seconds)
        all_events = _shape_events(resp)
        recent: list[dict[str, Any]] = []
        for ev in all_events:
            ts_epoch = ev.get("last_timestamp_epoch")
            if ts_epoch is None or ts_epoch >= threshold:
                recent.append(ev)
        return recent

    def _exec_pod(
        self,
        pod: str,
        command: list[str],
        namespace: str,
        container: Optional[str],
        timeout: int,
    ) -> dict[str, Any]:
        core_v1, _ = self._ensure_clients()
        # Zuerst die Fenster-Sperre: auf einem Knoten im Messfenster wird nichts gestartet.
        self._fenster_sperre(core_v1, pod, namespace, f"exec_pod({namespace}/{pod})", streng=True)
        try:
            from kubernetes.stream import stream  # noqa: WPS433
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(f"kubernetes.stream missing: {exc}") from exc

        kwargs: dict[str, Any] = {
            "name": pod,
            "namespace": namespace,
            "command": list(command),
            "stderr": True,
            "stdin": False,
            "stdout": True,
            "tty": False,
            "_preload_content": False,
        }
        if container:
            kwargs["container"] = str(container)

        # Audit 2026-10-02: Scheitert der Websocket-Handshake (Pod/Container gibt es nicht,
        # Rechte fehlen), wirft kubernetes 36 nur `'NoneType' object has no attribute
        # 'decode'` — der eigentliche Grund geht verloren. Deshalb vorher pruefen, was sich
        # pruefen laesst, und den Rest mit einer lesbaren Meldung versehen.
        self._exec_vorpruefung(core_v1, pod, namespace, container)
        try:
            ws = stream(core_v1.connect_get_namespaced_pod_exec, **kwargs)
        except AttributeError as exc:
            raise RuntimeError(
                f"k8s exec_pod({namespace}/{pod}): Exec-Handshake abgelehnt (Pod, Container "
                f"oder RBAC 'pods/exec' pruefen) — Client meldete: {exc}"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_api_exc(exc, f"exec_pod({namespace}/{pod})") from exc

        stdout_buf: list[str] = []
        stderr_buf: list[str] = []
        deadline = time.time() + int(timeout)
        timed_out = False
        try:
            while True:
                if time.time() >= deadline:
                    timed_out = True
                    break
                is_open = getattr(ws, "is_open", None)
                if callable(is_open) and not is_open():
                    break
                update_fn = getattr(ws, "update", None)
                if callable(update_fn):
                    update_fn(timeout=1)
                read_out = getattr(ws, "read_stdout", None)
                if callable(read_out):
                    part = read_out(timeout=0)
                    if part:
                        stdout_buf.append(_als_text(part))
                read_err = getattr(ws, "read_stderr", None)
                if callable(read_err):
                    part = read_err(timeout=0)
                    if part:
                        stderr_buf.append(_als_text(part))
                # Safety: if ws has no update() and no is_open(), bail early.
                if not callable(update_fn) and not callable(is_open):
                    break
            # Was nach dem letzten update() noch gepuffert liegt (Ausgabe kurz vor dem Ende).
            for name, ziel in (("read_stdout", stdout_buf), ("read_stderr", stderr_buf)):
                lesen = getattr(ws, name, None)
                if callable(lesen):
                    rest = lesen(timeout=0)
                    if rest:
                        ziel.append(_als_text(rest))
        finally:
            try:
                close_fn = getattr(ws, "close", None)
                if callable(close_fn):
                    close_fn()
            except Exception as exc:  # noqa: BLE001
                logger.debug("exec_pod ws.close() failed (soft): %s", exc)

        # Exit-Code und Fehlermeldung stehen im Fehlerkanal (3). Vorher wurde er nie gelesen:
        # ein unbekanntes Kommando oder `exit 3` kam als Erfolg mit leerer Ausgabe zurueck.
        exit_code, fehler = _exec_status(ws)
        out: dict[str, Any] = {
            "pod": pod,
            "namespace": namespace,
            "command": list(command),
            "stdout": "".join(stdout_buf),
            "stderr": "".join(stderr_buf),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "timeout_sec": int(timeout),
        }
        if timed_out or exit_code not in (None, 0) or fehler:
            out["ok"] = False
            out["error"] = (
                f"Zeitlimit {int(timeout)} s ueberschritten"
                if timed_out
                else (fehler or f"Kommando endete mit Exit-Code {exit_code}")
            )
        return out

    def _exec_vorpruefung(
        self, core_v1: Any, pod: str, namespace: str, container: Optional[str]
    ) -> None:
        """Pod und Container vor dem Exec pruefen — lesbare Fehler statt Handshake-Absturz."""
        try:
            pod_obj = core_v1.read_namespaced_pod(name=pod, namespace=namespace)
        except Exception as exc:  # noqa: BLE001
            raise self._wrap_api_exc(exc, f"exec_pod({namespace}/{pod})") from exc
        spec = getattr(pod_obj, "spec", None)
        namen = [getattr(c, "name", None) for c in (getattr(spec, "containers", None) or [])]
        namen = [n for n in namen if isinstance(n, str)]
        if container and namen and container not in namen:
            raise ValueError(
                f"k8s exec_pod({namespace}/{pod}): Container {container!r} gibt es nicht — "
                f"vorhanden: {namen}"
            )


# ---------------------------------------------------------------------------
# Shaping helpers (pure functions — easy to unit-test without a kube apiserver)
# ---------------------------------------------------------------------------


def _als_text(wert: Any) -> str:
    """Bytes/Text der Kubernetes-API als str — nie als `"b'...'"`."""
    if wert is None:
        return ""
    if isinstance(wert, (bytes, bytearray)):
        return bytes(wert).decode("utf-8", "replace")
    return str(wert)


def _exec_status(ws: Any) -> tuple[Optional[int], Optional[str]]:
    """(exit_code, Fehlermeldung) aus dem Fehlerkanal eines beendeten Exec.

    Der Kanal liefert ein v1.Status-JSON: Success -> 0; NonZeroExitCode -> Code aus
    details.causes[ExitCode]; sonst (z. B. Kommando nicht gefunden) -> message.
    """
    lesen = getattr(ws, "read_channel", None)
    if not callable(lesen):
        return None, None
    try:
        roh = lesen(3)
    except Exception as exc:  # noqa: BLE001
        # Nicht als „kein Fehler“ durchreichen: der Aufrufer soll wissen, dass der Exit-Code fehlt.
        logger.warning("exec_pod: Fehlerkanal nicht lesbar: %s", exc)
        return None, f"Exit-Status nicht lesbar ({type(exc).__name__}: {exc})"[:300]
    roh = _als_text(roh).strip()
    if not roh:
        return None, None
    try:
        status = json.loads(roh)
    except ValueError:
        return None, roh[:300]
    if not isinstance(status, dict):
        return None, roh[:300]
    if status.get("status") == "Success":
        return 0, None
    for ursache in (status.get("details") or {}).get("causes") or []:
        if ursache.get("reason") == "ExitCode":
            try:
                return int(ursache.get("message")), status.get("message")
            except (TypeError, ValueError):
                break
    return None, str(status.get("message") or roh)[:300]


def _selektor_text(selektor: Any) -> str:
    """V1LabelSelector (typisiert oder Dict) als label_selector-Text."""
    teile = [f"{k}={v}" for k, v in (_attr(selektor, "match_labels") or _attr(selektor, "matchLabels") or {}).items()]
    for a in _attr(selektor, "match_expressions") or _attr(selektor, "matchExpressions") or []:
        key, op = _attr(a, "key"), _attr(a, "operator")
        werte = ",".join(str(v) for v in (_attr(a, "values") or []))
        if op == "In":
            teile.append(f"{key} in ({werte})")
        elif op == "NotIn":
            teile.append(f"{key} notin ({werte})")
        elif op == "Exists":
            teile.append(str(key))
        elif op == "DoesNotExist":
            teile.append(f"!{key}")
    return ",".join(teile)


def _require_str(args: dict[str, Any], key: str) -> str:
    val = args.get(key)
    if not isinstance(val, str) or not val.strip():
        raise ValueError(f"k8s tool requires '{key}' (non-empty str)")
    return val.strip()


def _require_int(args: dict[str, Any], key: str) -> int:
    val = args.get(key)
    if not isinstance(val, int) or isinstance(val, bool):
        raise ValueError(f"k8s tool requires '{key}' (int)")
    return int(val)


def _clamp_int(val: Any, default: int, lo: int, hi: int) -> int:
    if not isinstance(val, int) or isinstance(val, bool):
        return default
    return max(lo, min(hi, int(val)))


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    """Safe attr-get that also tolerates dict-shaped objects (tests pass dicts)."""
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _to_epoch(ts: Any) -> Optional[float]:
    """Convert a datetime / ISO-string timestamp to epoch-seconds. None-safe."""
    if ts is None:
        return None
    if isinstance(ts, (int, float)):
        return float(ts)
    if hasattr(ts, "timestamp"):
        try:
            return float(ts.timestamp())
        except Exception:  # noqa: BLE001
            return None
    if isinstance(ts, str):
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except ValueError:
            return None
    return None


def _to_iso(ts: Any) -> Optional[str]:
    """Normalize a datetime / str to ISO-8601 string."""
    if ts is None:
        return None
    if isinstance(ts, str):
        return ts
    if hasattr(ts, "isoformat"):
        try:
            return ts.isoformat()
        except Exception:  # noqa: BLE001
            return None
    return None


def _shape_pods(resp: Any) -> list[dict[str, Any]]:
    items = _attr(resp, "items") or []
    out: list[dict[str, Any]] = []
    for pod in items:
        meta = _attr(pod, "metadata") or {}
        status = _attr(pod, "status") or {}
        spec = _attr(pod, "spec") or {}
        conds = _attr(status, "container_statuses") or []
        ready_containers = sum(1 for c in conds if _attr(c, "ready"))
        total_containers = len(conds)
        out.append(
            {
                "name": _attr(meta, "name"),
                "namespace": _attr(meta, "namespace"),
                "phase": _attr(status, "phase"),
                "ready": ready_containers == total_containers and total_containers > 0,
                "ready_ratio": f"{ready_containers}/{total_containers}",
                "node": _attr(spec, "node_name"),
                "pod_ip": _attr(status, "pod_ip"),
                "start_time": _to_iso(_attr(status, "start_time")),
            }
        )
    return out


def _shape_nodes(resp: Any) -> list[dict[str, Any]]:
    items = _attr(resp, "items") or []
    out: list[dict[str, Any]] = []
    for node in items:
        meta = _attr(node, "metadata") or {}
        status = _attr(node, "status") or {}
        spec = _attr(node, "spec") or {}
        conds = _attr(status, "conditions") or []
        ready = False
        for c in conds:
            if _attr(c, "type") == "Ready" and str(_attr(c, "status")) == "True":
                ready = True
                break
        out.append(
            {
                "name": _attr(meta, "name"),
                "ready": ready,
                "scheduling_disabled": bool(_attr(spec, "unschedulable") or False),
                "kubelet_version": _attr(_attr(status, "node_info") or {}, "kubelet_version"),
            }
        )
    return out


#: Etiketten, die etwas ueber die MASCHINE sagen. Bewusst eine Liste und kein
#: „alles ausser kubernetes.io/*": ein Knoten traegt Dutzende Etiketten aus
#: Scheduling, Rollen und Flux, und eine ungefilterte Ausgabe waere wieder eine
#: Wand, in der die zwei interessanten Zeilen untergehen.
_HW_LABEL_PRAEFIXE = (
    "fyos/gpu",
    "fyos/cpu",
    "fyos/ram",
    "fyos/hw",
    "fyos.io/hw",
    "fyos/node-type",
    "fyos/gpu-topology",
    "kubernetes.io/arch",
    "node.kubernetes.io/instance-type",
)


def _shape_node_hardware(resp: Any) -> list[dict[str, Any]]:
    """Ein Knoten, so wie die API ihn kennt — Etiketten, Kapazitaet, OS."""
    items = _attr(resp, "items") or []
    out: list[dict[str, Any]] = []
    for node in items:
        meta = _attr(node, "metadata") or {}
        status = _attr(node, "status") or {}
        spec = _attr(node, "spec") or {}
        info = _attr(status, "node_info") or {}
        labels = _attr(meta, "labels") or {}
        if not isinstance(labels, dict):
            labels = {}
        kapazitaet = _attr(status, "capacity") or {}
        vergebbar = _attr(status, "allocatable") or {}
        if not isinstance(kapazitaet, dict):
            kapazitaet = {}
        if not isinstance(vergebbar, dict):
            vergebbar = {}

        hw_labels = {
            k: v for k, v in labels.items() if any(k.startswith(p) for p in _HW_LABEL_PRAEFIXE)
        }
        gpu_labels = {k: v for k, v in hw_labels.items() if "gpu" in k}

        conds = _attr(status, "conditions") or []
        ready = any(
            _attr(c, "type") == "Ready" and str(_attr(c, "status")) == "True" for c in conds
        )

        out.append(
            {
                "name": _attr(meta, "name"),
                "ready": ready,
                "scheduling_disabled": bool(_attr(spec, "unschedulable") or False),
                "hardware_labels": hw_labels,
                "gpu": {
                    "labels": gpu_labels,
                    # Was der Scheduler vergeben kann — 0 heisst „kein Device-Plugin",
                    # NICHT „keine Karte". Deshalb steht `labels` daneben.
                    "vergebbar": vergebbar.get("nvidia.com/gpu", "0"),
                    "kapazitaet": kapazitaet.get("nvidia.com/gpu", "0"),
                },
                "cpu": {
                    "kapazitaet": kapazitaet.get("cpu"),
                    "vergebbar": vergebbar.get("cpu"),
                },
                "speicher": {
                    "kapazitaet": kapazitaet.get("memory"),
                    "vergebbar": vergebbar.get("memory"),
                },
                "pods_max": kapazitaet.get("pods"),
                "os": _attr(info, "os_image"),
                "kernel": _attr(info, "kernel_version"),
                "arch": _attr(info, "architecture"),
                "kubelet": _attr(info, "kubelet_version"),
                "laufzeit": _attr(info, "container_runtime_version"),
            }
        )
    return out


def _shape_pod_detail(pod: Any, events: list[dict[str, Any]]) -> dict[str, Any]:
    meta = _attr(pod, "metadata") or {}
    spec = _attr(pod, "spec") or {}
    status = _attr(pod, "status") or {}
    conds_raw = _attr(status, "conditions") or []
    conds: list[dict[str, Any]] = []
    for c in conds_raw:
        conds.append(
            {
                "type": _attr(c, "type"),
                "status": _attr(c, "status"),
                "reason": _attr(c, "reason"),
                "message": _attr(c, "message"),
                "last_transition_time": _to_iso(_attr(c, "last_transition_time")),
            }
        )
    containers_raw = _attr(status, "container_statuses") or []
    containers: list[dict[str, Any]] = []
    for c in containers_raw:
        state = _attr(c, "state") or {}
        running = _attr(state, "running")
        waiting = _attr(state, "waiting")
        terminated = _attr(state, "terminated")
        state_kind = (
            "running"
            if running
            else ("waiting" if waiting else ("terminated" if terminated else "unknown"))
        )
        containers.append(
            {
                "name": _attr(c, "name"),
                "image": _attr(c, "image"),
                "ready": bool(_attr(c, "ready") or False),
                "restart_count": int(_attr(c, "restart_count") or 0),
                "state": state_kind,
                "reason": _attr(waiting, "reason")
                if waiting
                else (_attr(terminated, "reason") if terminated else None),
            }
        )
    return {
        "name": _attr(meta, "name"),
        "namespace": _attr(meta, "namespace"),
        "node": _attr(spec, "node_name"),
        "phase": _attr(status, "phase"),
        "pod_ip": _attr(status, "pod_ip"),
        "host_ip": _attr(status, "host_ip"),
        "start_time": _to_iso(_attr(status, "start_time")),
        "conditions": conds,
        "containers": containers,
        "events": events,
    }


def _shape_events(resp: Any) -> list[dict[str, Any]]:
    items = _attr(resp, "items") or []
    out: list[dict[str, Any]] = []
    for ev in items:
        meta = _attr(ev, "metadata") or {}
        involved = _attr(ev, "involved_object") or {}
        last_ts = _attr(ev, "last_timestamp") or _attr(ev, "event_time")
        out.append(
            {
                "name": _attr(meta, "name"),
                "type": _attr(ev, "type"),
                "reason": _attr(ev, "reason"),
                "message": _attr(ev, "message"),
                "count": int(_attr(ev, "count") or 0),
                "involved_object": {
                    "kind": _attr(involved, "kind"),
                    "name": _attr(involved, "name"),
                    "namespace": _attr(involved, "namespace"),
                },
                "last_timestamp": _to_iso(last_ts),
                "last_timestamp_epoch": _to_epoch(last_ts),
            }
        )
    return out
