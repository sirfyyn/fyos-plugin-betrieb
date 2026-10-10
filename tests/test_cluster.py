"""cluster — Steuerpult (Etappe 4a): lesen, Trockenlauf, Fenster-Sperre, Flux-Hinweis, Rechte.

Ohne Cluster: `FakeKube` hat dieselbe Schnittstelle wie `cluster.Kube` und liefert Objekte in der
Rohform der API (wie `kubectl -o json`). Das Fenster kommt ueber die echte lage.sperrgrund mit einer
Redis-Attrappe — so wird die gemeinsame Sperre mitgeprueft, nicht nachgebaut.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from mcp_hub import registry as hub_registry
from fyos_plugin_betrieb import cluster, lage
from fyos_plugin_betrieb.cluster import KubeFehler
from test_lage import FakeRedis

JETZT = time.time()


@pytest.fixture(autouse=True)
def messknoten(monkeypatch):
    """Erfundener Messknoten `beta`: CronJobs, die gegen ihn arbeiten, sperrt das Steuerpult in seinem Fenster."""
    monkeypatch.setenv("FYOS_CLUSTER_MESSKNOTEN", "beta")
    monkeypatch.setenv("FYOS_CLUSTER_MESSSPUREN", "192.0.2.12,modell,18420,18421")


def iso(sekunden_her: float = 0) -> str:
    return cluster._iso(JETZT - sekunden_her)


# ── Bausteine im Rohformat der API ───────────────────────────────────────────────────────
def knoten(name, *, ip, steuerebene=False, arm=False, gpu=0, cpu="12", ram="65706012Ki", unschedulable=False):
    labels = {"kubernetes.io/hostname": name, "kubernetes.io/arch": "arm64" if arm else "amd64",
              "fyos-tier": "t2-state" if steuerebene else "t1-gpu"}
    if steuerebene:
        labels["node-role.kubernetes.io/control-plane"] = "true"
    else:
        labels["node-role.kubernetes.io/worker"] = "worker"
    return {
        "metadata": {"name": name, "labels": labels,
                     "annotations": {"k3s.io/node-args": json.dumps(["agent", "--token", "geheim", "--node-name", name])}},
        "spec": {"taints": [{"key": "arch", "value": "arm64", "effect": "NoSchedule"}] if arm else None,
                 "unschedulable": unschedulable or None},
        "status": {
            "addresses": [{"type": "InternalIP", "address": ip}],
            "capacity": {"cpu": cpu, "memory": ram, "nvidia.com/gpu": str(gpu), "pods": "110", "ephemeral-storage": "100Gi"},
            "allocatable": {"cpu": cpu, "memory": ram, "nvidia.com/gpu": str(gpu), "pods": "110"},
            "conditions": [{"type": "Ready", "status": "True", "reason": "KubeletReady", "lastTransitionTime": iso(3600)},
                           {"type": "MemoryPressure", "status": "False"}],
            "nodeInfo": {"osImage": "Debian", "kernelVersion": "6.18", "kubeletVersion": "v1.34.9+k3s1",
                         "containerRuntimeVersion": "containerd://2.2.5", "architecture": "arm64" if arm else "amd64"},
        },
    }


def pod(name, ns, node, *, besitzer=None, phase="Running", bereit=True, neustarts=0, warten=None, letzter=None,
        labels=None, volumes=None, requests=None, start=600):
    status = {
        "phase": phase, "startTime": iso(start),
        "conditions": [{"type": "Ready", "status": "True" if bereit else "False", "lastTransitionTime": iso(start)}],
        "containerStatuses": [{"name": "app", "restartCount": neustarts,
                               "state": {"waiting": {"reason": warten, "message": "back-off"}} if warten else {"running": {}},
                               "lastState": {"terminated": letzter} if letzter else {}}],
    }
    meta = {"name": name, "namespace": ns, "labels": labels or {}}
    if besitzer:
        meta["ownerReferences"] = [{"kind": besitzer[0], "name": besitzer[1], "controller": True}]
    return {"metadata": meta, "spec": {"nodeName": node, "containers": [{"name": "app", "image": "git.x/y/app:main",
                                                                         "resources": {"requests": requests or {"cpu": "100m", "memory": "128Mi"}}}],
                                        "volumes": volumes or []},
            "status": status}


def deployment(name, ns, *, replicas=1, ready=1, flux_replikas=False, hpa=False, selector=None, tpl=None):
    mf = []
    if flux_replikas:
        mf.append({"manager": "kustomize-controller", "operation": "Apply", "fieldsV1": {"f:spec": {"f:replicas": {}}}})
    if hpa:
        mf.append({"manager": "k3s", "operation": "Update", "subresource": "scale", "fieldsV1": {"f:spec": {"f:replicas": {}}}})
    labels = {"kustomize.toolkit.fluxcd.io/name": f"fyos-{name}", "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}
    return {"metadata": {"name": name, "namespace": ns, "labels": labels, "managedFields": mf, "uid": f"uid-{name}"},
            "spec": {"replicas": replicas, "selector": {"matchLabels": selector or {"app": name}},
                     "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
                     "template": {"metadata": {"labels": selector or {"app": name}},
                                  "spec": tpl or {"containers": [{"name": "app", "image": "git.x/y/app:main@sha256:0123456789abcdef"}],
                                                  "affinity": {"nodeAffinity": {"preferredDuringSchedulingIgnoredDuringExecution": [
                                                      {"weight": 100, "preference": {"matchExpressions": [
                                                          {"key": "kubernetes.io/hostname", "operator": "In", "values": ["alpha"]}]}}]}}}}},
            "status": {"readyReplicas": ready, "replicas": replicas}}


def cronjob(name, ns="fyos", *, env=None, suspend=False, selector=None, annot=None, active=None, forbid=False):
    return {"metadata": {"name": name, "namespace": ns, "uid": f"uid-{name}", "annotations": annot or {}},
            "spec": {"schedule": "23 * * * *", "suspend": suspend, "concurrencyPolicy": "Forbid" if forbid else "Allow",
                     "jobTemplate": {"metadata": {"labels": {"job": name}}, "spec": {"backoffLimit": 0, "template": {"spec": {
                         "nodeSelector": selector or {"kubernetes.io/arch": "amd64"},
                         "containers": [{"name": "c", "image": "x", "env": env or []}], "restartPolicy": "Never"}}}}},
            "status": {"lastSuccessfulTime": iso(7200), "lastScheduleTime": iso(600), "active": active or []}}


class FakeKube:
    """Gleiche Schnittstelle wie cluster.Kube — haelt fest, was geschrieben (oder nur probiert) wurde."""

    def __init__(self, objekte: dict[str, list[dict]], verboten: tuple[str, ...] = ()):
        self.o = objekte
        self.verboten = set(verboten)
        self.patches: list[tuple] = []
        self.skaliert: list[tuple] = []
        self.jobs: list[tuple] = []
        self.logs_aufrufe: list[tuple] = []
        self.kubelet_aufrufe: list[str] = []
        self.kein_vorher = False

    def _pruef(self, schluessel: str) -> None:
        if schluessel in self.verboten:
            raise KubeFehler(403, f"HTTP 403: {schluessel} forbidden")

    def liste(self, art, ns=None, *, feld=None, selektor=None):
        self._pruef(f"list:{art}")
        items = [o for o in self.o.get(art, []) if not ns or cluster._ns(o) == ns]
        if feld == "type=Warning":
            items = [o for o in items if o.get("type") == "Warning"]
        if selektor:
            paare = dict(t.split("=", 1) for t in selektor.split(",") if "=" in t)
            items = [o for o in items if all(cluster._labels(o).get(k) == v for k, v in paare.items())]
        return items

    def lesen(self, art, name, ns=None):
        for o in self.o.get(art, []):
            if cluster._name(o) == name and (ns is None or cluster._ns(o) == ns):
                return o
        raise KubeFehler(404, f"HTTP 404: {art} {name} not found")

    def patchen(self, art, name, ns, koerper, *, trocken):
        self._pruef(f"patch:{art}")
        self.lesen(art, name, ns)
        self.patches.append((art, name, ns, koerper, trocken))
        return json.loads(json.dumps(koerper))

    def skalieren(self, name, ns, replikas, *, trocken):
        self._pruef("patch:deployments/scale")
        self.skaliert.append((name, ns, replikas, trocken))
        return {"spec": {"replicas": replikas}}

    def job_anlegen(self, ns, koerper, *, trocken):
        self._pruef("create:jobs")
        self.jobs.append((ns, koerper, trocken))
        return koerper

    def logs(self, name, ns, *, container, zeilen, vorher):
        self.logs_aufrufe.append((name, ns, container, zeilen, vorher))
        if vorher and self.kein_vorher:
            raise KubeFehler(400, "HTTP 400: previous terminated container not found")
        return "erste Zeile\nzweite Zeile\n"

    def kubelet_konfig(self, knoten):
        self._pruef("get:nodes/proxy")
        self.kubelet_aufrufe.append(knoten)
        return {"kubeletconfig": {"maxPods": 250, "cgroupDriver": "systemd", "anderes": 1}}

    def darf(self, verb, ressource, *, gruppe="", ns=None, unter=None):
        return f"{verb}:{ressource}{'/' + unter if unter else ''}" not in self.verboten


class Bus:
    def __init__(self):
        self.ereignisse: list[tuple[str, dict]] = []

    def xadd(self, strom, felder, **kw):
        self.ereignisse.append((strom, felder))


def prom(abfrage: str) -> list[dict]:
    """Prometheus-Attrappe: je Knoten-IP feste Werte, GPU auf beta, ein Alarm."""
    def je_ip(werte):
        return [{"metric": {"instance": f"{ip}:9100"}, "value": [JETZT, str(w)]} for ip, w in werte.items()]

    if "node_cpu_seconds_total" in abfrage:
        return je_ip({"192.0.2.11": 38.2, "192.0.2.12": 11.0})
    if "MemAvailable_bytes / node_memory" in abfrage:
        return je_ip({"192.0.2.11": 29.0, "192.0.2.12": 22.0})
    if "node_memory_MemTotal_bytes - " in abfrage:
        return je_ip({"192.0.2.11": 19.5 * 2**30})
    if abfrage.startswith("max by (instance) (node_boot_time_seconds)"):
        return je_ip({"192.0.2.11": JETZT - 86400, "192.0.2.12": JETZT - 600})
    if "changes(node_boot_time_seconds" in abfrage:
        return je_ip({"192.0.2.11": 2, "192.0.2.12": 4})
    if abfrage.startswith("node_filesystem_avail_bytes"):
        return [{"metric": {"instance": "192.0.2.11:9100", "fstype": "zfs"}, "value": [JETZT, str(96 * 2**30)]},
                {"metric": {"instance": "192.0.2.12:9100", "fstype": "ext4"}, "value": [JETZT, str(600 * 2**30)]}]
    if abfrage.startswith("node_filesystem_size_bytes"):
        return [{"metric": {"instance": "192.0.2.11:9100", "fstype": "zfs"}, "value": [JETZT, str(96.2 * 2**30)]},
                {"metric": {"instance": "192.0.2.12:9100", "fstype": "ext4"}, "value": [JETZT, str(1400 * 2**30)]}]
    if abfrage == "DCGM_FI_DEV_GPU_UTIL":
        return [{"metric": {"Hostname": "beta", "gpu": "0", "modelName": "Beispiel-GPU"}, "value": [JETZT, "48"]}]
    if abfrage == "DCGM_FI_DEV_FB_USED":
        return [{"metric": {"Hostname": "beta", "gpu": "0", "modelName": "Beispiel-GPU"}, "value": [JETZT, "31328"]}]
    if abfrage.startswith("ALERTS"):
        return [{"metric": {"alertname": "Watchdog", "severity": "none"}, "value": [JETZT, "1"]},
                {"metric": {"alertname": "NetzSchnittstelleFlappt", "severity": "critical", "instance": "192.0.2.12:9100",
                            "device": "enp9s0", "pod": "monitoring-prometheus-node-exporter-x"}, "value": [JETZT, "1"]},
                {"metric": {"alertname": "KubeJobFailed", "severity": "warning", "job_name": "serving-toks-canary-29849063",
                            "pod": "monitoring-kube-state-metrics-y"}, "value": [JETZT, "1"]}]
    if "container_cpu_usage_seconds_total" in abfrage:
        return [{"metric": {"namespace": "fyos", "pod": "mcp-hub-6854b7dd54-qdl8x"}, "value": [JETZT, "0.12"]}]
    if "container_memory_working_set_bytes" in abfrage:
        return [{"metric": {"namespace": "fyos", "pod": "mcp-hub-6854b7dd54-qdl8x"}, "value": [JETZT, str(512 * 2**20)]}]
    return []


def objekte() -> dict[str, list[dict]]:
    rs = [{"metadata": {"name": "mcp-hub-6854b7dd54", "namespace": "fyos", "ownerReferences": [{"kind": "Deployment", "name": "mcp-hub"}]}},
          {"metadata": {"name": "agent-7f9c8d6b5", "namespace": "fyos", "ownerReferences": [{"kind": "Deployment", "name": "agent"}]}},
          {"metadata": {"name": "web-5d8f7b9c4", "namespace": "fyos", "ownerReferences": [{"kind": "Deployment", "name": "web"}]}}]
    pods = [
        pod("mcp-hub-6854b7dd54-g4s97", "fyos", "beta", besitzer=("ReplicaSet", "mcp-hub-6854b7dd54"), labels={"app": "mcp-hub"}),
        pod("mcp-hub-6854b7dd54-qdl8x", "fyos", "alpha", besitzer=("ReplicaSet", "mcp-hub-6854b7dd54"), labels={"app": "mcp-hub"}),
        pod("agent-7f9c8d6b5-x2kq9", "fyos", "alpha", besitzer=("ReplicaSet", "agent-7f9c8d6b5"), labels={"app": "agent"},
            warten="CrashLoopBackOff", neustarts=7, bereit=False,
            letzter={"reason": "OOMKilled", "exitCode": 137, "finishedAt": iso(300)}),
        pod("web-5d8f7b9c4-h7k2m", "fyos", "alpha", besitzer=("ReplicaSet", "web-5d8f7b9c4"), labels={"app": "web"},
            volumes=[{"name": "daten", "persistentVolumeClaim": {"claimName": "web-daten"}}, {"name": "tmp", "emptyDir": {}}]),
        pod("node-exporter-abcde", "monitoring", "beta", besitzer=("DaemonSet", "node-exporter"), labels={"app": "node-exporter"}),
        pod("node-exporter-fghjk", "monitoring", "alpha", besitzer=("DaemonSet", "node-exporter"), labels={"app": "node-exporter"}),
        pod("aufraeumen", "default", "beta", phase="Succeeded"),
        pod("serving-toks-canary-29849063-rqvh8", "fyos", "beta", besitzer=("Job", "serving-toks-canary-29849063"), phase="Failed"),
    ]
    return {
        "nodes": [knoten("alpha", ip="192.0.2.11", steuerebene=True),
                  knoten("beta", ip="192.0.2.12", gpu=2, cpu="32", ram="126835312Ki"),
                  knoten("gamma", ip="192.0.2.13", arm=True, cpu="6", ram="7802628Ki")],
        "pods": pods,
        "replicasets": rs,
        "deployments": [deployment("mcp-hub", "fyos", replicas=2, ready=2, flux_replikas=True),
                        deployment("agent", "fyos", replicas=2, ready=1, hpa=True),
                        deployment("web", "fyos", replicas=1, ready=1),
                        deployment("coredns", "kube-system", replicas=1, ready=1)],
        "statefulsets": [{"metadata": {"name": "openclaw", "namespace": "openclaw-cleanroom"},
                          "spec": {"replicas": 1, "selector": {"matchLabels": {"app": "openclaw"}},
                                   "template": {"spec": {"containers": [{"name": "c", "image": "o"}]}}},
                          "status": {"readyReplicas": 1}}],
        "daemonsets": [{"metadata": {"name": "node-exporter", "namespace": "monitoring"},
                        "spec": {"selector": {"matchLabels": {"app": "node-exporter"}},
                                 "template": {"spec": {"containers": [{"name": "c", "image": "n"}],
                                                       "tolerations": [{"operator": "Exists"}]}}},
                        "status": {"desiredNumberScheduled": 2, "numberReady": 2}}],
        "cronjobs": [cronjob("serving-toks-canary", env=[{"name": "CANARY_MODEL", "value": "beta-modell/modell"}]),
                     cronjob("github-ruf-waechter", suspend=True, annot={"fyos.io/kontext.wo": "fyos/ops, k8s/base/ops/github-ruf-waechter.yaml"}),
                     cronjob("backup-verify", selector={"kubernetes.io/hostname": "alpha"}),
                     cronjob("laeuft-schon", active=[{"name": "laeuft-schon-1"}], forbid=True)],
        "jobs": [{"metadata": {"name": "serving-toks-canary-29849063", "namespace": "fyos",
                               "ownerReferences": [{"kind": "CronJob", "name": "serving-toks-canary"}]},
                  "status": {"conditions": [{"type": "Failed", "status": "True", "reason": "DeadlineExceeded",
                                             "lastTransitionTime": iso(1800)}]}}],
        "events": [
            {"metadata": {"name": "e1", "namespace": "fyos"}, "type": "Warning", "reason": "Unhealthy", "count": 3,
             "message": "Readiness probe failed", "lastTimestamp": iso(120),
             "involvedObject": {"kind": "Pod", "name": "agent-7f9c8d6b5-x2kq9", "namespace": "fyos"}, "source": {"host": "alpha"}},
            {"metadata": {"name": "e2", "namespace": "fyos"}, "type": "Warning", "reason": "Unhealthy", "count": 2,
             "message": "Liveness probe failed", "lastTimestamp": iso(60),
             "involvedObject": {"kind": "Pod", "name": "agent-7f9c8d6b5-zz9pq", "namespace": "fyos"}},
            {"metadata": {"name": "e3", "namespace": "monitoring"}, "type": "Warning", "reason": "DNSConfigForming", "count": 500,
             "message": "Nameserver limits exceeded", "lastTimestamp": iso(30),
             "involvedObject": {"kind": "Pod", "name": "node-exporter-abcde", "namespace": "monitoring"}},
            {"metadata": {"name": "e4", "namespace": "fyos"}, "type": "Warning", "reason": "BackOff", "count": 1,
             "message": "alt", "lastTimestamp": iso(3 * 86400),
             "involvedObject": {"kind": "Pod", "name": "x", "namespace": "fyos"}},
        ],
        "kustomizations": [
            {"metadata": {"name": "fyos-mcp-hub", "namespace": "flux-system"},
             "spec": {"interval": "10m", "path": "./k8s/base/mcp-hub", "sourceRef": {"kind": "GitRepository", "name": "fyos"}},
             "status": {"lastAppliedRevision": "main@sha1:abc", "conditions": [{"type": "Ready", "status": "True", "reason": "ReconciliationSucceeded"}]}},
            {"metadata": {"name": "fyos-kaputt", "namespace": "flux-system"},
             "spec": {"sourceRef": {"kind": "GitRepository", "name": "fyos"}},
             "status": {"conditions": [{"type": "Ready", "status": "False", "reason": "BuildFailed", "message": "kustomize build failed"}]}},
            {"metadata": {"name": "fyos-ruht", "namespace": "flux-system"},
             "spec": {"suspend": True, "sourceRef": {"kind": "GitRepository", "name": "fyos"}},
             "status": {"conditions": [{"type": "Ready", "status": "True"}]}},
        ],
        "gitrepositories": [{"metadata": {"name": "fyos", "namespace": "flux-system"}, "spec": {},
                             "status": {"artifact": {"revision": "main@sha1:abc"}, "conditions": [{"type": "Ready", "status": "True"}]}}],
        "pvcs": [{"metadata": {"name": "web-daten", "namespace": "fyos"}, "spec": {"storageClassName": "local-path"}}],
        "pvs": [{"metadata": {"name": "pvc-alt"}, "spec": {"capacity": {"storage": "500Gi"}, "storageClassName": "fyos-platinum",
                                                           "claimRef": {"namespace": "fyos", "name": "immich-uploads"},
                                                           "persistentVolumeReclaimPolicy": "Retain"},
                 "status": {"phase": "Released"}}],
        "services": [{"metadata": {"name": "fle-engine", "namespace": "default"}, "spec": {"selector": {"app": "fle"}}},
                     {"metadata": {"name": "mcp-hub", "namespace": "fyos"}, "spec": {"selector": {"app": "mcp-hub"}}}],
        "endpoints": [{"metadata": {"name": "mcp-hub", "namespace": "fyos"}, "subsets": [{"addresses": [{"ip": "198.51.100.1"}]}]}],
        "pdbs": [],
    }


@pytest.fixture()
def fenster(monkeypatch):
    """beta: Dev-Fenster offen (wie die Messkette bis 05.10.); die anderen frei."""
    r = FakeRedis(keys={"fyos:wartung:beta": json.dumps({"knoten": "beta", "grund": "[dev] Quant messen", "von": "betreiber"})},
                  ttl={"fyos:wartung:beta": 7200})
    monkeypatch.setattr(lage, "_redis", lambda: r)
    return r


@pytest.fixture()
def pult(fenster, tmp_path):
    p = cluster.ClusterPlugin()
    p.config = {"repo": str(tmp_path)}
    p.register()
    p.k = FakeKube(objekte())
    p._prom = prom
    p._bus = Bus()
    # Die Zertifikats-Uhr kommt aus lage.fristen (TLS an Caddy) — hier fest, ohne Netz.
    p._lage_p = SimpleNamespace(call=lambda m, a: {"uhren": [
        {"art": "zertifikat", "name": "www.example.org", "zustand": "ok", "text": "57.6 Tage", "in_tagen": 57.6},
        {"art": "platte", "name": "alpha /", "zustand": "ok", "text": "40% belegt"}]})
    return p


# ── reine Helfer ────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("wert, erwartet", [("250m", 0.25), ("2", 2.0), ("1Gi", 2.0**30), ("128974848", 128974848.0),
                                            ("1e3", 1000.0), ("500M", 5e8), ("65706012Ki", 65706012 * 1024.0),
                                            (None, 0.0), ("quatsch", 0.0), (3, 3.0)])
def test_menge(wert, erwartet):
    assert cluster.menge(wert) == pytest.approx(erwartet)


def test_replikas_verwalter_roh_und_typisiert():
    assert cluster.replikas_verwalter(deployment("a", "fyos", flux_replikas=True))["flux"] is True
    v = cluster.replikas_verwalter(deployment("a", "fyos", hpa=True))
    assert v["flux"] is False and v["autoskalierer"] is True
    assert cluster.replikas_verwalter(deployment("a", "fyos"))["kustomization"] == "flux-system/fyos-a"
    # k8s_core arbeitet mit typisierten Objekten des python-clients (snake_case)
    typ = SimpleNamespace(metadata=SimpleNamespace(labels={}, managed_fields=[
        SimpleNamespace(manager="kustomize-controller", operation="Apply", subresource=None, fields_v1={"f:spec": {"f:replicas": {}}})]))
    assert cluster.replikas_verwalter(typ)["flux"] is True
    hand = SimpleNamespace(metadata=SimpleNamespace(labels={}, managed_fields=[
        SimpleNamespace(manager="kubectl", operation="Update", subresource="scale", fields_v1={"f:spec": {"f:replicas": {}}})]))
    assert cluster.replikas_verwalter(hand) == {**cluster.replikas_verwalter(hand), "flux": False, "autoskalierer": False}


def test_moegliche_knoten_achtet_taints_selektor_und_absperrung():
    k = objekte()["nodes"] + [knoten("delta", ip="192.0.2.14", arm=True, unschedulable=True)]
    assert cluster.moegliche_knoten({"containers": []}, k) == ["alpha", "beta"]
    assert cluster.moegliche_knoten({"tolerations": [{"key": "arch", "operator": "Equal", "value": "arm64", "effect": "NoSchedule"}]}, k) \
        == ["alpha", "beta", "gamma"]
    assert cluster.moegliche_knoten({"nodeSelector": {"kubernetes.io/hostname": "alpha"}}, k) == ["alpha"]
    pflicht = {"affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
        {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["beta"]}]}]}}}}
    assert cluster.moegliche_knoten(pflicht, k) == ["alpha"]
    assert "delta" in cluster.moegliche_knoten({"tolerations": [{"operator": "Exists"}]}, k, abgesperrte_zaehlen=True)


def test_warum_hier_in_saetzen():
    s = cluster.warum_hier(deployment("a", "fyos")["spec"]["template"]["spec"])
    assert s == ["bevorzugt (100): kubernetes.io/hostname In alpha"]
    assert cluster.warum_hier({}) == ["keine Vorgabe — der Scheduler waehlt"]
    assert any("toleriert arch=arm64:NoSchedule" == x for x in cluster.warum_hier(
        {"tolerations": [{"key": "arch", "value": "arm64", "effect": "NoSchedule"}]}))


@pytest.mark.parametrize("art, name, basis", [
    ("Pod", "agent-7f9c8d6b5-x2kq9", "agent"), ("Pod", "serving-toks-canary-29849063-rqvh8", "serving-toks-canary"),
    ("Job", "serving-toks-canary-29849063", "serving-toks-canary"), ("Pod", "node-exporter-qphrd", "node-exporter"),
    ("Pod", "openclaw-0", "openclaw"), ("ReplicaSet", "mcp-hub-6854b7dd54", "mcp-hub"), ("Pod", "redis-cache", "redis-cache"),
    ("Deployment", "agent-7f9c8d6b5", "agent-7f9c8d6b5"),
])
def test_basisname(art, name, basis):
    assert cluster.basisname(art, name) == basis


def test_pod_problem_erkennt_die_typischen_faelle():
    assert cluster.pod_problem(pod("a", "fyos", "t", warten="CrashLoopBackOff"), JETZT)[1] == "CrashLoopBackOff"
    assert cluster.pod_problem(pod("a", "fyos", "t", bereit=False, start=1200), JETZT)[:2] == ("gelb", "nicht bereit")
    assert cluster.pod_problem(pod("a", "fyos", "t", bereit=False, start=60), JETZT) is None  # gerade gestartet
    oom = pod("a", "fyos", "t", letzter={"reason": "OOMKilled", "finishedAt": iso(600)})
    assert cluster.pod_problem(oom, JETZT)[1] == "OOMKilled"
    haengt = pod("a", "fyos", None, phase="Pending")
    haengt["status"]["conditions"] = [{"type": "PodScheduled", "status": "False", "message": "0/4 nodes are available"}]
    assert cluster.pod_problem(haengt, JETZT)[:2] == ("rot", "nicht einplanbar")
    assert cluster.pod_problem(pod("a", "fyos", "t"), JETZT) is None


def test_messknoten_bezug_name_env_gpu():
    assert cluster.messknoten_bezug(cronjob("serving-toks-canary", env=[{"name": "CANARY_MODEL", "value": "beta-modell/modell"}])) \
        == ["Env CANARY_MODEL nennt beta"]
    assert "Name nennt beta" in cluster.messknoten_bezug(cronjob("knoten-waechter-beta"))
    gpu = cronjob("x")
    gpu["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["resources"] = {"limits": {"nvidia.com/gpu": "1"}}
    assert cluster.messknoten_bezug(gpu) == ["braucht eine GPU"]
    assert cluster.messknoten_bezug(cronjob("backup-verify")) == []


def test_schwaerzen_laesst_kein_token_durch():
    assert cluster.schwaerzen(["agent", "--token", "abc", "--token-file=/x", "--node-name", "beta", "--agent-token=zz"]) \
        == ["agent", "--token", "***", "--token-file=***", "--node-name", "beta", "--agent-token=***"]


def test_pod_summen_init_und_overhead():
    p = {"spec": {"containers": [{"resources": {"requests": {"cpu": "100m", "memory": "64Mi"}, "limits": {"memory": "128Mi"}}},
                                 {"resources": {"requests": {"cpu": "200m"}}}],
                  "initContainers": [{"resources": {"requests": {"cpu": "1", "memory": "32Mi"}}},
                                     {"restartPolicy": "Always", "resources": {"requests": {"cpu": "50m"}}}],
                  "overhead": {"cpu": "10m"}}}
    req, lim = cluster.pod_summen(p)
    assert req["cpu"] == pytest.approx(1.01) and req["memory"] == 64 * 2**20
    assert lim["memory"] == 128 * 2**20


def test_suspend_grund_aus_dem_manifest(tmp_path):
    (tmp_path / "k8s/base/ops").mkdir(parents=True)
    (tmp_path / "k8s/base/ops/github-ruf-waechter.yaml").write_text(
        "spec:\n  schedule: x\n  # AUSGESETZT, und das ist ein Gate.\n  #\n  # Der Betreiber muss entscheiden.\n  suspend: true\n")
    cj = cronjob("github-ruf-waechter", suspend=True, annot={"fyos.io/kontext.wo": "fyos/ops, k8s/base/ops/github-ruf-waechter.yaml"})
    assert cluster.suspend_grund_aus_repo(cj, str(tmp_path)) == "AUSGESETZT, und das ist ein Gate. Der Betreiber muss entscheiden."
    boese = cronjob("x", annot={"fyos.io/kontext.wo": "../../etc/passwd.yaml"})
    assert cluster.suspend_grund_aus_repo(boese, str(tmp_path)) is None


# ── Werkzeug-Beschreibung ───────────────────────────────────────────────────────────────
def test_werkzeuge_tragen_app_tags_und_strenge_schemas():
    p = cluster.ClusterPlugin()
    tools = {t.name: t for t in p.describe_tools()}
    assert list(tools)[0] == "knoten"  # fyos-web oeffnet die Karte mit dem ersten Werkzeug ohne Pflichtfeld
    schreibend = {"neustarten", "skalieren", "flux_abgleichen", "cronjob_jetzt", "knoten_absperren"}
    for name, t in tools.items():
        assert "flaeche" in t.tags and t.input_schema.get("additionalProperties") is False
        assert t.meta["ui"]["resourceUri"] == cluster.UI_STEUERPULT
        if name in schreibend:
            assert "write" in t.tags and t.meta["ui"]["visibility"] == ["app"]
            assert t.input_schema["properties"]["ausfuehren"]["default"] is False
        else:
            assert "read" in t.tags and "visibility" not in t.meta["ui"]
    assert tools["knoten"].title.startswith("Steuerpult — ")


def test_oberflaeche_mit_bruecke():
    (res,) = cluster.ClusterPlugin().describe_resources()
    html = res.read()
    assert res.uri == "ui://fyos/steuerpult.html" and res.mime_type == "text/html;profile=mcp-app"
    assert "/*BRUECKE*/" not in html and "ui/initialize" in html and "fyos-steuerpult" in html
    # die App ruft nur Werkzeuge des eigenen Plugins (fyos-web erlaubt nichts anderes)
    assert 'App.werkzeug("cluster__" + name' in html


# ── lesend ──────────────────────────────────────────────────────────────────────────────
def test_knoten_knapp_ist_der_standard(pult):
    d = pult.call("knoten", {})
    voll = pult.call("knoten", {"voll": True})
    assert d["knapp"] is True and "knapp" not in voll
    assert d["satz"].startswith("3 Knoten, alle bereit") and "beta im Dev-Fenster" in d["satz"] and "CPU" in d["satz"]
    k = {x["name"]: x for x in d["knoten"]}
    assert k["beta"]["fenster"]["art"] == "dev" and k["beta"]["sperrgrund"].startswith("Dev-Fenster auf beta")
    assert k["beta"]["auslastung"]["cpu_prozent"] == 11.0 and k["beta"]["pods"]["anzahl"] == {x["name"]: x for x in voll["knoten"]}["beta"]["pods"]["anzahl"]
    for schwer in ("labels", "taints", "bedingungen", "system", "spuren", "aktionen"):
        assert schwer not in k["beta"]
    assert "liste" not in k["beta"]["pods"]
    assert len(json.dumps(d, ensure_ascii=False)) < len(json.dumps(voll, ensure_ascii=False)) / 2


def test_knoten_echte_werte_fenster_und_aktionen(pult):
    d = pult.call("knoten", {"voll": True})
    k = {x["name"]: x for x in d["knoten"]}
    assert [x["name"] for x in d["knoten"]][0] == "alpha"  # Steuerebene zuerst
    t = k["beta"]
    assert t["fenster"]["art"] == "dev" and t["sperrgrund"].startswith("Dev-Fenster auf beta bis ")
    assert t["auslastung"]["cpu_prozent"] == 11.0 and t["auslastung"]["gpus"][0]["auslastung_prozent"] == 48.0
    assert t["boot"]["boots_14t"] == 4 and t["kapazitaet"]["gpu"] == 2
    assert t["aktionen"]["absperren"]["moeglich"] is False and "Dev-Fenster" in t["aktionen"]["absperren"]["grund"]
    assert t["system"]["k3s_args"] == ["agent", "--token", "***", "--node-name", "beta"]
    tn = k["alpha"]
    assert tn["steuerebene"] and tn["aktionen"]["absperren"]["grund"].startswith("alpha wird nie abgesperrt")
    assert tn["angefordert"]["cpu"] == pytest.approx(0.4)  # 4 laufende Pods à 100m (Succeeded/Failed zaehlen nicht)
    assert tn["pods"]["anzahl"] == 4 and tn["auslastung"]["ram_gib"] == 19.5
    # ZFS: Prozent des Datasets waere irrefuehrend (alpha zeigte 0 %) — nur „frei"
    assert tn["auslastung"]["platte_prozent"] is None and tn["auslastung"]["platte_frei_gib"] == 96.0
    assert t["auslastung"]["platte_prozent"] == pytest.approx(57.1, abs=0.1) and t["auslastung"]["platte_fs"] == "ext4"
    assert k["gamma"]["aktionen"]["absperren"] == {"an": True, "moeglich": True, "grund": None}
    assert d["quellen"]["fenster"]["ok"] is True


def test_knoten_kubelet_auf_beta_im_fenster_gesperrt_ohne_abfrage(pult):
    t = pult.call("knoten", {"knoten": "beta", "mit_kubelet": True, "voll": True})["knoten"][0]
    assert t["kubelet"] == {"gesperrt": t["sperrgrund"]}
    assert pult.k.kubelet_aufrufe == []  # nichts landet auf beta
    tn = pult.call("knoten", {"knoten": "alpha", "mit_kubelet": True, "voll": True})["knoten"][0]
    assert tn["kubelet"]["auszug"] == {"maxPods": 250, "cgroupDriver": "systemd"}
    with pytest.raises(ValueError, match="gibt es nicht"):
        pult.call("knoten", {"knoten": "odin"})
    with pytest.raises(ValueError, match="nur zusammen mit knoten"):
        pult.call("knoten", {"mit_kubelet": True})


def test_knoten_kubelet_ohne_recht_sagt_was_zu_tun_ist(pult):
    pult.k.verboten.add("get:nodes/proxy")
    tn = pult.call("knoten", {"knoten": "alpha", "mit_kubelet": True, "voll": True})["knoten"][0]
    assert tn["kubelet"]["recht_fehlt"] == "Recht fehlt: nodes/proxy get – Betreiber: bash scripts/ops/steuerpult_rechte_ausrollen.sh --ja"


def test_fenster_unlesbar_heisst_gesperrt(pult, monkeypatch):
    kaputt = FakeRedis()
    kaputt.get = lambda k: (_ for _ in ()).throw(ConnectionError("weg"))
    monkeypatch.setattr(lage, "_redis", lambda: kaputt)
    k = {x["name"]: x for x in pult.call("knoten", {"voll": True})["knoten"]}
    assert "nicht lesbar" in k["gamma"]["aktionen"]["absperren"]["grund"]


def test_workloads_aktionen_sperren_flux_rechte(pult):
    pult.k.verboten.add("patch:statefulsets")
    d = pult.call("workloads", {})
    w = {(x["art"], x["name"]): x for x in d["workloads"]}
    hub = w[("deploy", "mcp-hub")]
    assert hub["knoten"] == ["alpha", "beta"] and hub["replikas_von"] == "flux"
    assert hub["aktionen"]["neustarten"]["moeglich"] is False and "Dev-Fenster" in hub["aktionen"]["neustarten"]["grund"]
    assert hub["aktionen"]["logs"]["moeglich"] is True  # der Pod auf alpha ist lesbar
    assert hub["nutzung"] == {"cpu": 0.12, "ram_mib": 512}
    assert hub["bilder"] == ["app:main@01234567"]
    agent = w[("deploy", "agent")]
    assert agent["probleme"][0] == "1 von 2 bereit" and agent["replikas_von"] == "autoscaler"
    assert agent["letzter_abbruch"]["grund"] == "OOMKilled" and agent["aktionen"]["skalieren"]["moeglich"] is True
    assert "Autoscaler" in agent["aktionen"]["skalieren"]["hinweis"]
    sts = w[("sts", "openclaw")]
    assert sts["aktionen"]["neustarten"]["grund"] == cluster.recht_fehlt("statefulsets patch")
    assert w[("deploy", "coredns")]["aktionen"]["neustarten"]["hinweis"] == "Systembereich — nur mit trotzdem"
    assert d["workloads"][0]["probleme"]  # Probleme zuerst
    nur = pult.call("workloads", {"knoten": "beta"})["workloads"]
    assert {x["name"] for x in nur} == {"mcp-hub", "node-exporter"}


def test_cronjobs_messknoten_bezug_ausgesetzt_forbid(pult, tmp_path):
    (tmp_path / "k8s/base/ops").mkdir(parents=True)
    (tmp_path / "k8s/base/ops/github-ruf-waechter.yaml").write_text("  # Ein Gate: CHG-3371.\n  suspend: true\n")
    c = {x["name"]: x for x in pult.call("workloads", {})["cronjobs"]}
    canary = c["serving-toks-canary"]
    assert canary["zustand"] == "rot" and canary["fehlschlaege_24h"] == 1
    assert canary["aktionen"]["jetzt"]["grund"].startswith("arbeitet gegen beta (Env CANARY_MODEL nennt beta) — Dev-Fenster")
    gh = c["github-ruf-waechter"]
    assert gh["grund_ausgesetzt"] == "Ein Gate: CHG-3371." and "Aussetzung umgehen" in gh["aktionen"]["jetzt"]["grund"]
    assert c["backup-verify"]["aktionen"]["jetzt"]["moeglich"] is True and c["backup-verify"]["knoten"] == ["alpha"]
    assert "parallele" in c["laeuft-schon"]["aktionen"]["jetzt"]["grund"]


def test_lage_gruppiert_was_nicht_stimmt(pult):
    d = pult.call("lage", {})
    g = {x["art"]: x for x in d["gruppen"]}
    assert d["zustand"] == "rot"
    assert g["pods"]["eintraege"][0]["name"] == "deploy/agent" and g["pods"]["eintraege"][0]["grund"] == "CrashLoopBackOff"
    assert all("canary" not in e["name"] for e in g["pods"]["eintraege"])  # Job-Pods stehen bei den Jobs
    job = g["jobs"]["eintraege"][0]
    assert job["name"] == "serving-toks-canary" and job["grund"] == "DeadlineExceeded" and job["seither_ok"] is False
    assert {e["name"] for e in g["flux"]["eintraege"]} == {"fyos-kaputt", "fyos-ruht"}
    assert g["cronjobs_aus"]["eintraege"][0]["name"] == "github-ruf-waechter"
    alarme = {a["name"]: a for a in g["alarme"]["eintraege"]}
    assert "Watchdog" not in alarme and alarme["NetzSchnittstelleFlappt"]["ziele"] == ["beta enp9s0"]
    assert alarme["KubeJobFailed"]["ziele"] == ["Job serving-toks-canary-29849063"]
    assert d["satz"].startswith("Nicht in Ordnung:")
    assert d["info"] == ["Zertifikat www.example.org: noch 58 Tage"] and "zertifikate" not in g


def test_lage_zertifikat_unter_21_tagen(pult):
    pult._lage_p = SimpleNamespace(call=lambda m, a: {"uhren": [{"art": "zertifikat", "name": "www.example.org", "in_tagen": 5.2}]})
    g = {x["art"]: x for x in pult.call("lage", {})["gruppen"]}
    assert g["zertifikate"]["eintraege"] == [{"name": "www.example.org", "grund": "läuft in 5 Tagen ab", "schwere": "rot"}]


def test_ereignisse_gruppiert_laerm_eingeklappt(pult):
    d = pult.call("ereignisse", {"stunden": 24})
    (g,) = d["gruppen"]
    assert (g["grund"], g["objekt"], g["anzahl"], g["objekte"]) == ("Unhealthy", "agent", 5, 2)
    assert g["meldung"] == "Liveness probe failed" and g["knoten"] == ["alpha"]
    assert d["laerm"] == [{"grund": "DNSConfigForming", "anzahl": 500, "objekte": 1}]


def test_logs_auf_beta_gesperrt_deploy_nimmt_den_auffaelligen_pod(pult):
    t = pult.call("logs", {"ns": "fyos", "pod": "mcp-hub-6854b7dd54-g4s97"})
    assert t["gesperrt"].startswith("Dev-Fenster auf beta") and t["text"] is None
    assert pult.k.logs_aufrufe == []
    d = pult.call("logs", {"ns": "fyos", "deploy": "mcp-hub", "zeilen": 50})
    assert d["pod"] == "mcp-hub-6854b7dd54-qdl8x" and d["text"].startswith("erste Zeile")  # der freie Pod
    assert pult.k.logs_aufrufe == [("mcp-hub-6854b7dd54-qdl8x", "fyos", "app", 50, False)]
    g = pult.call("logs", {"ns": "fyos", "deploy": "agent"})
    assert g["pod"] == "agent-7f9c8d6b5-x2kq9"
    pult.k.kein_vorher = True
    assert pult.call("logs", {"ns": "fyos", "pod": "agent-7f9c8d6b5-x2kq9", "vorher": True})["hinweis"] == "kein voriger Lauf dieses Containers"
    with pytest.raises(ValueError, match="genau eins"):
        pult.call("logs", {"ns": "fyos"})
    with pytest.raises(ValueError, match="Container 'nix'"):
        pult.call("logs", {"ns": "fyos", "pod": "agent-7f9c8d6b5-x2kq9", "container": "nix"})


def test_flux_zustand_und_fehlendes_leserecht(pult):
    pult.k.verboten.add("list:helmreleases")
    d = pult.call("flux_zustand", {})
    assert d["arten"]["kustomizations"]["gesamt"] == 3 and d["arten"]["kustomizations"]["bereit"] == 2
    assert d["arten"]["helmreleases"]["recht_fehlt"] is True
    assert d["arten"]["helmreleases"]["fehler"] == cluster.recht_fehlt("helmreleases list")
    assert d["anzahl_probleme"] == 2
    # ohne `voll`: alle Kustomizations (fuer das Abgleichen), Bereite ohne Routine-Meldung
    ks = {e["name"]: e for e in d["arten"]["kustomizations"]["eintraege"]}
    assert set(ks) == {"fyos-mcp-hub", "fyos-kaputt", "fyos-ruht"} and ks["fyos-kaputt"]["meldung"] == "kustomize build failed"
    assert d["arten"]["gitrepositories"]["eintraege"] == [] and d["arten"]["gitrepositories"]["gesamt"] == 1
    assert len(pult.call("flux_zustand", {"voll": True})["arten"]["gitrepositories"]["eintraege"]) == 1


def test_knoten_raeumen_ist_nur_probe(pult):
    d = pult.call("knoten_raeumen", {"knoten": "alpha"})
    p = {x["pod"]: x for x in d["pods"]}
    assert d["nur_probe"] and d["gesperrt"].startswith("alpha wird nie geräumt")
    assert p["web-5d8f7b9c4-h7k2m"]["zustand"] == "kann_nicht" and "PVC web-daten" in p["web-5d8f7b9c4-h7k2m"]["text"]
    assert p["node-exporter-fghjk"]["zustand"] == "bleibt"
    assert p["mcp-hub-6854b7dd54-qdl8x"]["zustand"] == "zieht_um" and p["mcp-hub-6854b7dd54-qdl8x"]["text"] == "→ beta"
    assert d["faellt_aus"] == ["fyos/deploy/web"]
    assert pult.k.patches == []  # die Probe aendert nichts
    assert "ausfuehren" not in next(t for t in pult.describe_tools() if t.name == "knoten_raeumen").input_schema["properties"]


def test_altlasten_liste_und_skript_loescht_nie(pult):
    d = pult.call("altlasten", {})
    assert d["released_pvs"][0]["name"] == "pvc-alt" and d["released_gib"] == 500.0
    assert {(x["ns"], x["name"]) for x in d["nackte_pods"]} == {("default", "aufraeumen")}
    assert [(x["ns"], x["name"]) for x in d["dienste_ohne_endpunkte"]] == [("default", "fle-engine")]
    s = d["skript"]
    assert 'JA=0; [ "${1:-}" = "--ja" ]' in s and "kubectl delete pv pvc-alt" in s and "= Released ]" in s
    assert "delete pod aufraeumen" in s and pult.k.patches == []


# ── schreibend ──────────────────────────────────────────────────────────────────────────
def test_neustarten_trockenlauf_ist_standard(pult):
    d = pult.call("neustarten", {"ns": "fyos", "art": "deploy", "name": "web"})
    assert d["trockenlauf"] is True and d["ausgefuehrt"] is False and d["ausfuehrbar"] is True
    assert d["plan"] == "Startet fyos/web neu: 1 Pod(s) auf alpha; erst der neue Pod, dann geht der alte — kein Ausfall."
    assert [p[4] for p in pult.k.patches] == [True]  # nur der dryRun am API-Server
    assert "Flux verwaltet" in d["hinweise"][0] and pult._bus.ereignisse == []
    with pytest.raises(ValueError, match="nur mit grund"):
        pult.call("neustarten", {"ns": "fyos", "art": "deploy", "name": "web", "ausfuehren": True})


def test_neustarten_ausfuehren_mit_grund_meldet_auf_den_bus(pult):
    d = pult.call("neustarten", {"ns": "fyos", "art": "deploy", "name": "web", "ausfuehren": True, "grund": "haengt"})
    assert d["ausgefuehrt"] is True and d["ergebnis"]["neustart_marke"]
    echt = [p for p in pult.k.patches if p[4] is False]
    assert echt[0][:3] == ("deployments", "web", "fyos")
    assert "kubectl.kubernetes.io/restartedAt" in echt[0][3]["spec"]["template"]["metadata"]["annotations"]
    ((strom, felder),) = pult._bus.ereignisse
    daten = json.loads(felder["data"])
    assert strom == "fyos:events:steuerpult.aktion" and daten["grund"] == "haengt" and daten["ergebnis"] == "ok"


def test_neustarten_auf_beta_im_fenster_gesperrt(pult):
    d = pult.call("neustarten", {"ns": "fyos", "art": "deploy", "name": "mcp-hub"})
    assert d["ausfuehrbar"] is False and d["blockiert"].startswith("Dev-Fenster auf beta")
    r = pult.call("neustarten", {"ns": "fyos", "art": "deploy", "name": "mcp-hub", "ausfuehren": True, "grund": "x"})
    assert r["ok"] is False and r["error"].startswith("nicht ausgeführt — Dev-Fenster auf beta")
    assert [p for p in pult.k.patches if p[4] is False] == [] and pult._bus.ereignisse == []


def test_neustarten_systembereich_nur_mit_trotzdem_und_recht_fehlt(pult):
    d = pult.call("neustarten", {"ns": "kube-system", "art": "deploy", "name": "coredns"})
    assert d["braucht"][0]["feld"] == "trotzdem" and d["ausfuehrbar"] is False
    assert pult.call("neustarten", {"ns": "kube-system", "art": "deploy", "name": "coredns", "trotzdem": True})["ausfuehrbar"] is True
    pult.k.verboten.add("patch:statefulsets")
    s = pult.call("neustarten", {"ns": "openclaw-cleanroom", "art": "sts", "name": "openclaw"})
    assert s["blockiert"] == "Recht fehlt: statefulsets patch – Betreiber: bash scripts/ops/steuerpult_rechte_ausrollen.sh --ja"
    with pytest.raises(ValueError, match="gibt es nicht"):
        pult.call("neustarten", {"ns": "fyos", "art": "deploy", "name": "gibtsnicht"})


def test_skalieren_flux_null_und_nichts_zu_tun(pult):
    pult.k.o["deployments"].append(deployment("frei", "fyos", replicas=1, flux_replikas=True))
    d = pult.call("skalieren", {"ns": "fyos", "name": "frei", "replikas": 0})
    felder = {b["feld"] for b in d["braucht"]}
    assert felder == {"trotzdem", "null_bestaetigt"} and d["replikas_von"] == "flux"
    assert "wird beim nächsten Abgleich auf 1 zurückgesetzt" in d["braucht"][0]["text"]
    r = pult.call("skalieren", {"ns": "fyos", "name": "frei", "replikas": 0, "ausfuehren": True, "grund": "Test"})
    assert r["ok"] is False and "bestätigen" in r["error"]
    ok = pult.call("skalieren", {"ns": "fyos", "name": "frei", "replikas": 0, "ausfuehren": True, "grund": "Test",
                                 "trotzdem": True, "null_bestaetigt": True})
    assert ok["ausgefuehrt"] is True and ok["ergebnis"] == {"vorher": 1, "nachher": 0}
    assert pult.k.skaliert[-1] == ("frei", "fyos", 0, False)
    assert pult.call("skalieren", {"ns": "fyos", "name": "web", "replikas": 1})["blockiert"].startswith("nichts zu tun")
    with pytest.raises(ValueError, match="replikas"):
        pult.call("skalieren", {"ns": "fyos", "name": "web", "replikas": 99})


def test_neustarten_ohne_wirkung_wird_nicht_angeboten(pult):
    pult.k.o["deployments"].append(deployment("ruht", "fyos", replicas=0, ready=0))
    assert pult.call("neustarten", {"ns": "fyos", "art": "deploy", "name": "ruht"})["blockiert"].startswith("nichts zu tun")
    pult.k.o["daemonsets"][0]["spec"]["updateStrategy"] = {"type": "OnDelete"}
    pult.k.o["pods"] = [p for p in pult.k.o["pods"] if p["spec"]["nodeName"] != "beta"]
    ds = pult.call("neustarten", {"ns": "monitoring", "art": "ds", "name": "node-exporter"})
    assert ds["blockiert"].startswith("updateStrategy OnDelete")


def test_skalieren_neue_pods_koennten_auf_beta_landen(pult):
    d = pult.call("skalieren", {"ns": "fyos", "name": "web", "replikas": 2})
    assert any(h.startswith("Neue Pods könnten auch auf beta landen") for h in d["hinweise"])


def test_flux_abgleichen_quelle_zuerst_und_ausgesetzt(pult):
    d = pult.call("flux_abgleichen", {"kustomization": "fyos-mcp-hub", "ausfuehren": True, "grund": "nach Push"})
    echt = [(p[0], p[1]) for p in pult.k.patches if p[4] is False]
    assert echt == [("gitrepositories", "fyos"), ("kustomizations", "fyos-mcp-hub")]
    assert "reconcile.fluxcd.io/requestedAt" in pult.k.patches[-1][3]["metadata"]["annotations"]
    assert d["ergebnis"]["angestossen"] == ["GitRepository flux-system/fyos", "Kustomization flux-system/fyos-mcp-hub"]
    assert d["plan"].startswith("Stößt an: GitRepository flux-system/fyos → Kustomization flux-system/fyos-mcp-hub")
    assert pult.call("flux_abgleichen", {"kustomization": "fyos-ruht"})["blockiert"].endswith("bis sie fortgesetzt wird")


def test_cronjob_jetzt_job_aus_vorlage_meidet_beta(pult):
    pult.k.o["cronjobs"].append(cronjob("frei"))
    d = pult.call("cronjob_jetzt", {"ns": "fyos", "name": "frei", "ausfuehren": True, "grund": "Probe"})
    assert d["ausgefuehrt"] is True
    ns, koerper, trocken = pult.k.jobs[-1]
    assert trocken is False and koerper["metadata"]["name"].startswith("frei-hand-")
    assert koerper["metadata"]["annotations"]["cronjob.kubernetes.io/instantiate"] == "manual"
    assert koerper["metadata"]["ownerReferences"][0] == {"apiVersion": "batch/v1", "kind": "CronJob", "name": "frei",
                                                         "uid": "uid-frei", "controller": True}
    terme = koerper["spec"]["template"]["spec"]["affinity"]["nodeAffinity"]["requiredDuringSchedulingIgnoredDuringExecution"]["nodeSelectorTerms"]
    assert terme == [{"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["beta"]}]}]
    assert d["knoten"] == ["alpha"] and any("meidet beta" in h for h in d["hinweise"])
    assert pult.call("cronjob_jetzt", {"ns": "fyos", "name": "serving-toks-canary"})["blockiert"].startswith("arbeitet gegen beta")


def test_cronjob_jetzt_ohne_recht(pult):
    pult.k.verboten.add("create:jobs")
    d = pult.call("cronjob_jetzt", {"ns": "fyos", "name": "backup-verify"})
    assert d["blockiert"] == cluster.recht_fehlt("jobs create")


def test_knoten_absperren_regeln(pult):
    assert pult.call("knoten_absperren", {"knoten": "alpha", "an": True})["blockiert"].startswith("alpha wird nie abgesperrt")
    assert pult.call("knoten_absperren", {"knoten": "beta", "an": True})["blockiert"].startswith("Dev-Fenster auf beta")
    assert pult.call("knoten_absperren", {"knoten": "gamma", "an": False})["blockiert"].startswith("nichts zu tun")
    d = pult.call("knoten_absperren", {"knoten": "gamma", "an": True, "ausfuehren": True, "grund": "Wartung Netzteil"})
    assert d["ausgefuehrt"] is True and d["ergebnis"] == {"abgesperrt": True}
    assert [p[3] for p in pult.k.patches if p[4] is False] == [{"spec": {"unschedulable": True}}]
    pult.k.verboten.add("patch:nodes")
    assert pult.call("knoten_absperren", {"knoten": "gamma", "an": True})["blockiert"] == cluster.recht_fehlt("nodes patch")


def test_registry_meldet_gesperrte_ausfuehrung_als_fehler_und_prueft_argumente(pult):
    reg = hub_registry.PluginRegistry()
    hub_registry._register_one(reg, "cluster", pult)
    r = hub_registry.call(reg, "cluster.neustarten", {"ns": "fyos", "art": "deploy", "name": "mcp-hub", "ausfuehren": True, "grund": "x"})
    assert r.status == "error" and r.kind == "tool_failed" and "Dev-Fenster" in r.error
    falsch = hub_registry.call(reg, "cluster.skalieren", {"ns": "fyos", "name": "web", "replicas": 2})
    assert falsch.kind == "bad_arguments"


def test_entry_point_registriert_das_steuerpult():
    """Im Monorepo prüfte der Test die ConfigMap; hier meldet der Entry-Point `cluster` das Plugin an."""
    import tomllib
    import pathlib

    pfad = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"
    eintraege = tomllib.loads(pfad.read_text(encoding="utf-8"))["project"]["entry-points"]["mcp_hub.plugins"]
    assert eintraege["cluster"] == "fyos_plugin_betrieb.cluster:ClusterPlugin"
    assert cluster.ClusterPlugin.name == "cluster"


def test_app_skript_ist_gueltiges_javascript(tmp_path):
    """Am 03.10. brach ein „…" mit geradem Schlusszeichen das ganze Skript — die App blieb
    bei „Wird geladen …" stehen, ohne Fehlermeldung. node --check findet das vor dem Ausrollen."""
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node fehlt — Syntaxprobe nur, wo node installiert ist")
    (res,) = cluster.ClusterPlugin().describe_resources()
    html = res.read()
    skript = html[html.index("<script>") + len("<script>"): html.rindex("</script>")]
    datei = tmp_path / "app.js"
    datei.write_text(skript, encoding="utf-8")
    r = subprocess.run([node, "--check", str(datei)], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr[-800:]


def test_knoten_spuren_aus_dem_verlauf_und_ohne_fuer_die_zentrale(pult):
    """04.10.: Spuren kommen über die Registry aus verlauf.zustand; spuren=False (Zentrale, 5-s-Frist) fragt ihn nicht."""
    aufrufe = []
    roh = {"gruppen": [{"kurven": [
        {"id": "cpu:192.0.2.12:9100", "ort": {"knoten": "beta"}, "punkte": [[1, 10.0], [2, 20.0]]},
        {"id": "gpu_last:beta:0", "ort": {"knoten": "beta", "gpu": "0"}, "punkte": [[1, 5.0], [2, 7.0]]},
        {"id": "dienst:modell", "punkte": [[1, 1.0]]}]}]}
    verlauf_p = SimpleNamespace(call=lambda m, a: aufrufe.append((m, a)) or roh)
    pult.registry_anbinden(SimpleNamespace(plugins={"verlauf": verlauf_p}))
    k = {x["name"]: x for x in pult.call("knoten", {"voll": True})["knoten"]}
    assert aufrufe == [("zustand", {"zeitraum": "6h", "punkte": True})]
    assert k["beta"]["spuren"] == {"cpu": [10.0, 20.0], "gpus": {"0": {"last": [5.0, 7.0]}}}
    assert "spuren" not in k["alpha"]
    aufrufe.clear()
    k = {x["name"]: x for x in pult.call("knoten", {"spuren": False, "voll": True})["knoten"]}
    assert aufrufe == [] and "spuren" not in k["beta"]
