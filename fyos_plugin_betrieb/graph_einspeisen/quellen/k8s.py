"""Quelle k8s — Knoten, Dienste, CronJobs, Services, Volumes und Bilder aus dem Cluster.

Liest nur (Kubernetes-API über `k8s_zugang`, im Pod mit dem ServiceAccount, sonst mit der kubeconfig). Secrets werden
nie gelesen, ConfigMaps nur nach Dienstadressen durchsucht; Werte landen nicht im Graphen, nur der NAME der Variablen
oder der ConfigMap als Beleg. Die Quelle ist immer an; ohne Zugang zum Cluster scheitert sie laut.

Knoten (id → typ)
    knoten:<name>                     knoten   Node (arch, tier, ip, bereit, version)
    dienst:<ns>/<name>                dienst   Deployment, StatefulSet, DaemonSet (art, soll, bereit)
    cronjob:<ns>/<name>               cronjob  (zeitplan, ausgesetzt)
    service:<ns>/<name>               service  (typ, ports)
    pvc:<ns>/<name>, pv:<name>        pvc, pv  (klasse, groesse, phase)
    image:<repo ohne Tag>             image
    speicher:nfs/<server><pfad>       speicher NFS-Freigabe, die ein Dienst direkt einhängt

Kanten (von braucht nach; Arten siehe betriebsgraph.py)
    dienst/cronjob  laeuft_auf   knoten      ODER-Gruppe; attribute.pin = hart (nur ein Knoten kommt in Frage) | frei
    service         waehlt       dienst      Selektor trifft die Pod-Vorlage
    dienst/cronjob  mountet      pvc         (auch StatefulSet-Vorlagen)
    pvc             bindet       pv
    pv              liegt_auf    knoten      Knotenbindung des Volumes (local-path) bzw. NFS-Server
    dienst/cronjob  nutzt        service     Adresse in Umgebung/Argumenten/ConfigMap (beleg nennt die Stelle, nie den Wert)
    dienst/cronjob  nutzt_image  image       attribute.tag / digest
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from ...betriebsgraph import Kante, Knoten

NAME = "k8s"

_DNS = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
SVC_VOLL = re.compile(rf"\b({_DNS})\.({_DNS})\.svc(?:\.cluster\.local)?\b")
SVC_KURZ = re.compile(rf"://({_DNS})(?::\d+)?(?=[/\s\"'?#]|$)")
#: Namensräume, die wir nicht aufnehmen (k3s-Interna sind Rauschen: pro Knoten ein Pod, nie von uns geändert).
AUSGENOMMEN_NS: frozenset[str] = frozenset({"kube-public", "kube-node-lease"})


# ── reine Hilfen (ohne Cluster testbar) ──────────────────────────────────────


def bild_zerlegen(ref: str) -> tuple[str, str | None, str | None]:
    """`reg/pfad/name:tag@sha256:abc…` → (`reg/pfad/name`, tag, digest)."""
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    tag = None
    letztes = ref.rsplit("/", 1)[-1]
    if ":" in letztes:
        ref, tag = ref.rsplit(":", 1)
    return ref, tag, digest


def _menge(text: Any) -> float:
    """Kubernetes-Menge für Größenangaben (Gi → Gibibyte als Zahl); alles andere 0."""
    s = str(text or "").strip()
    faktoren = {"Ki": 1 / 1024**2, "Mi": 1 / 1024, "Gi": 1, "Ti": 1024, "K": 1e3 / 1024**3, "M": 1e6 / 1024**3, "G": 1e9 / 1024**3}
    for suf, f in faktoren.items():
        if s.endswith(suf):
            try:
                return float(s[: -len(suf)]) * f
            except ValueError:
                return 0.0
    try:
        return float(s) / 1024**3
    except ValueError:
        return 0.0


def groesse_text(text: Any) -> str:
    g = _menge(text)
    if g <= 0:
        return str(text or "")
    return f"{g:g}Gi" if g >= 1 else f"{g * 1024:g}Mi"


def _passt_ausdruck(labels: dict[str, str], ausdruck: dict[str, Any]) -> bool:
    key, op = ausdruck.get("key"), ausdruck.get("operator")
    werte = [str(v) for v in ausdruck.get("values") or []]
    ist = labels.get(key)
    if op == "In":
        return ist in werte
    if op == "NotIn":
        return ist is None or ist not in werte
    if op == "Exists":
        return key in labels
    if op == "DoesNotExist":
        return key not in labels
    if op in ("Gt", "Lt") and ist is not None and werte:
        try:
            return float(ist) > float(werte[0]) if op == "Gt" else float(ist) < float(werte[0])
        except ValueError:
            return False
    return False


def _toleriert(taint: dict[str, Any], tolerationen: list[dict[str, Any]]) -> bool:
    for t in tolerationen:
        if t.get("operator") == "Exists" and not t.get("key"):
            return True
        if t.get("key") != taint.get("key"):
            continue
        if t.get("effect") and t.get("effect") != taint.get("effect"):
            continue
        if t.get("operator") == "Exists" or (t.get("operator", "Equal") == "Equal" and t.get("value", "") == taint.get("value", "")):
            return True
    return False


def kandidaten(pod_spec: dict[str, Any], knoten: list[dict[str, Any]]) -> list[str]:
    """Knoten, auf denen eine Pod-Vorlage laufen dürfte: nodeName, nodeSelector, nodeAffinity (required),
    Taints/Tolerationen und erweiterte Ressourcen (nvidia.com/gpu) berücksichtigt."""
    spec = pod_spec or {}
    sel = spec.get("nodeSelector") or {}
    terms = (
        ((spec.get("affinity") or {}).get("nodeAffinity") or {})
        .get("requiredDuringSchedulingIgnoredDuringExecution", {})
        .get("nodeSelectorTerms")
        or []
    )
    tol = spec.get("tolerations") or []
    erweitert: set[str] = set()
    for c in list(spec.get("containers") or []) + list(spec.get("initContainers") or []):
        res = c.get("resources") or {}
        for gruppe in (res.get("limits") or {}, res.get("requests") or {}):
            erweitert |= {k for k in gruppe if k.startswith("nvidia.com/")}
    out: list[str] = []
    for n in knoten:
        name = n["metadata"]["name"]
        labels = n["metadata"].get("labels") or {}
        if spec.get("nodeName") and spec["nodeName"] != name:
            continue
        if any(labels.get(k) != v for k, v in sel.items()):
            continue
        if terms:
            ok = False
            for t in terms:
                ausdruecke = t.get("matchExpressions") or []
                felder = t.get("matchFields") or []
                if all(_passt_ausdruck(labels, a) for a in ausdruecke) and all(
                    _passt_ausdruck({"metadata.name": name}, f) for f in felder
                ):
                    ok = True
                    break
            if not ok:
                continue
        taints = [t for t in (n.get("spec") or {}).get("taints") or [] if t.get("effect") in ("NoSchedule", "NoExecute")]
        if any(not _toleriert(t, tol) for t in taints):
            continue
        alloc = (n.get("status") or {}).get("allocatable") or {}
        if any(float(alloc.get(k, 0) or 0) <= 0 for k in erweitert):
            continue
        out.append(name)
    return out


def _vorlage(obj: dict[str, Any]) -> dict[str, Any]:
    spec = obj.get("spec") or {}
    if obj.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec")) or {}
    return spec.get("template") or {}


def _container(vorlage: dict[str, Any]) -> list[dict[str, Any]]:
    s = vorlage.get("spec") or {}
    return list(s.get("initContainers") or []) + list(s.get("containers") or [])


def _wert_fundstellen(vorlage: dict[str, Any]) -> list[tuple[str, str]]:
    """(Beleg, Text) – Texte, in denen eine Adresse stehen kann. Beleg nennt den Ort, nie den Wert."""
    out: list[tuple[str, str]] = []
    for c in _container(vorlage):
        for e in c.get("env") or []:
            if isinstance(e.get("value"), str):
                out.append((f"env {e.get('name')}", e["value"]))
        for i, a in enumerate((c.get("command") or []) + (c.get("args") or [])):
            if isinstance(a, str):
                out.append((f"{c.get('name')} Argument", a))
    return out


def _configmaps(vorlage: dict[str, Any]) -> list[str]:
    namen: list[str] = []
    s = vorlage.get("spec") or {}
    for v in s.get("volumes") or []:
        cm = (v.get("configMap") or {}).get("name")
        if cm:
            namen.append(cm)
        for q in ((v.get("projected") or {}).get("sources")) or []:
            n = (q.get("configMap") or {}).get("name")
            if n:
                namen.append(n)
    for c in _container(vorlage):
        for ef in c.get("envFrom") or []:
            n = (ef.get("configMapRef") or {}).get("name")
            if n:
                namen.append(n)
        for e in c.get("env") or []:
            n = (((e.get("valueFrom") or {}).get("configMapKeyRef")) or {}).get("name")
            if n:
                namen.append(n)
    return sorted(set(namen))


def adressen(text: str, ns: str, bekannte_services: set[tuple[str, str]], *, kurz: bool) -> set[tuple[str, str]]:
    """Dienstadressen in einem Text → {(ns, service)}, nur bekannte Services."""
    funde: set[tuple[str, str]] = set()
    for m in SVC_VOLL.finditer(text):
        s = (m.group(2), m.group(1))
        if s in bekannte_services:
            funde.add(s)
    if kurz:
        for m in SVC_KURZ.finditer(text):
            s = (ns, m.group(1))
            if s in bekannte_services:
                funde.add(s)
    return funde


# ── Sammeln ──────────────────────────────────────────────────────────────────


def _mit_art(items: Iterable[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    """Objekte mit `kind` (Listen einzelner Arten tragen es nicht von sich aus), ohne Namensräume, die wir auslassen."""
    return [
        {**o, "kind": o.get("kind") or kind}
        for o in items
        if (o.get("metadata") or {}).get("namespace") not in AUSGENOMMEN_NS
    ]


def sammeln(ctx: Any) -> tuple[list[Knoten], list[Kante]]:
    nodes = ctx.k8s_liste("nodes")
    workloads = [
        w
        for art, kind in (("deployments", "Deployment"), ("statefulsets", "StatefulSet"), ("daemonsets", "DaemonSet"))
        for w in _mit_art(ctx.k8s_liste(art), kind)
    ]
    cronjobs = _mit_art(ctx.k8s_liste("cronjobs"), "CronJob")
    services = _mit_art(ctx.k8s_liste("services"), "Service")
    pods = ctx.k8s_liste("pods")
    pvcs = ctx.k8s_liste("pvcs")
    pvs = ctx.k8s_liste("pvs")
    try:
        cms = {(c["metadata"]["namespace"], c["metadata"]["name"]): c for c in ctx.k8s_liste("configmaps")}
    except Exception:  # noqa: BLE001 — ConfigMaps sind nur Zusatz; ohne sie fehlen einzelne nutzt-Kanten
        cms = {}

    k_liste: dict[str, Knoten] = {}
    e_liste: dict[tuple[str, str, str], Kante] = {}

    def knoten(i: str, typ: str, name: str, **attr: Any) -> None:
        k_liste.setdefault(i, Knoten(i, typ, name, {k: v for k, v in attr.items() if v is not None}))

    def kante(von: str, nach: str, art: str, *, beleg: str = "", vertrauen: float = 1.0, **attr: Any) -> None:
        if von == nach:
            return
        e_liste.setdefault((von, nach, art), Kante(von, nach, art, vertrauen=vertrauen, beleg=beleg, attribute=attr))

    # ── Knoten ──
    ip_zu_knoten: dict[str, str] = {}
    for n in nodes:
        name = n["metadata"]["name"]
        labels = n["metadata"].get("labels") or {}
        adr = {a["type"]: a["address"] for a in (n.get("status") or {}).get("addresses") or []}
        if adr.get("InternalIP"):
            ip_zu_knoten[adr["InternalIP"]] = name
        bereit = any(c.get("type") == "Ready" and c.get("status") == "True" for c in (n.get("status") or {}).get("conditions") or [])
        knoten(
            f"knoten:{name}",
            "knoten",
            name,
            arch=labels.get("kubernetes.io/arch"),
            tier=labels.get("fyos-tier"),
            ip=adr.get("InternalIP"),
            bereit=bereit,
            version=((n.get("status") or {}).get("nodeInfo") or {}).get("kubeletVersion"),
        )

    # ── Services (zuerst: die Adressensuche braucht die Menge) ──
    bekannte: set[tuple[str, str]] = set()
    for s in services:
        ns, name = s["metadata"]["namespace"], s["metadata"]["name"]
        bekannte.add((ns, name))
        sp = s.get("spec") or {}
        ports = sorted({f"{p.get('port')}/{p.get('protocol', 'TCP')}" for p in sp.get("ports") or []})
        knoten(f"service:{ns}/{name}", "service", name, ns=ns, art=sp.get("type"), ports=ports, ohne_selektor=not sp.get("selector"))

    # ── Pods → Besitzer ──
    wl_namen = {(w["metadata"]["namespace"], w["metadata"]["name"]): w for w in workloads}
    pods_je_besitzer: dict[tuple[str, str], dict[str, int]] = {}
    for p in pods:
        ns = p["metadata"]["namespace"]
        node_name = (p.get("spec") or {}).get("nodeName")
        if not node_name or (p.get("status") or {}).get("phase") not in ("Running", "Pending"):
            continue
        for o in p["metadata"].get("ownerReferences") or []:
            if o.get("kind") == "ReplicaSet":
                basis = re.sub(r"-[a-z0-9]{6,10}$", "", o["name"])
            elif o.get("kind") == "Job":
                basis = re.sub(r"-\d+$", "", o["name"])
            else:
                basis = o["name"]
            if (ns, basis) in wl_namen or o.get("kind") == "Job":
                pods_je_besitzer.setdefault((ns, basis), {}).setdefault(node_name, 0)
                pods_je_besitzer[(ns, basis)][node_name] += 1
            break

    pvc_namen = {(c["metadata"]["namespace"], c["metadata"]["name"]) for c in pvcs}

    def gemeinsames(obj: dict[str, Any], dienst_id: str, ns: str, *, ist_cron: bool) -> None:
        vorlage = _vorlage(obj)
        vspec = vorlage.get("spec") or {}
        # Bilder
        for c in _container(vorlage):
            if not c.get("image"):
                continue
            repo, tag, digest = bild_zerlegen(c["image"])
            knoten(f"image:{repo}", "image", repo.rsplit("/", 1)[-1], repo=repo)
            attr = {k: v for k, v in (("tag", tag), ("digest", digest[7:19] if digest else None)) if v}
            if (dienst_id, f"image:{repo}", "nutzt_image") not in e_liste:
                kante(dienst_id, f"image:{repo}", "nutzt_image", **attr)
        # Volumes
        for v in vspec.get("volumes") or []:
            claim = (v.get("persistentVolumeClaim") or {}).get("claimName")
            if claim and (ns, claim) in pvc_namen:
                kante(dienst_id, f"pvc:{ns}/{claim}", "mountet", beleg=f"volume {v.get('name')}")
            nfs = v.get("nfs")
            if nfs and nfs.get("server"):
                sid = f"speicher:nfs/{nfs['server']}{nfs.get('path', '')}"
                knoten(sid, "speicher", (nfs.get("path") or "/").rstrip("/").rsplit("/", 1)[-1] or nfs["server"], server=nfs["server"], pfad=nfs.get("path"))
                kante(dienst_id, sid, "mountet", beleg=f"volume {v.get('name')}")
                if nfs["server"] in ip_zu_knoten:
                    kante(sid, f"knoten:{ip_zu_knoten[nfs['server']]}", "liegt_auf", pin="hart", beleg="NFS-Server ist Clusterknoten")
        # Adressen
        gesehen: set[tuple[str, str]] = set()
        for beleg, text in _wert_fundstellen(vorlage):
            for s in adressen(text, ns, bekannte, kurz=True):
                if s not in gesehen:
                    gesehen.add(s)
                    kante(dienst_id, f"service:{s[0]}/{s[1]}", "nutzt", beleg=beleg, vertrauen=0.9)
        for cm_name in _configmaps(vorlage):
            cm = cms.get((ns, cm_name))
            if not cm:
                continue
            for schluessel, text in (cm.get("data") or {}).items():
                if not isinstance(text, str):
                    continue
                for s in adressen(text, ns, bekannte, kurz=False):
                    if s not in gesehen:
                        gesehen.add(s)
                        kante(dienst_id, f"service:{s[0]}/{s[1]}", "nutzt", beleg=f"configmap {cm_name}/{schluessel}", vertrauen=0.8)
        # Platzierung
        cand = kandidaten(vspec, nodes)
        hart = len(cand) == 1
        besitzer = pods_je_besitzer.get((ns, obj["metadata"]["name"]), {})
        ziele = sorted(besitzer) if not ist_cron else []
        if not ziele and hart:
            ziele = list(cand)
        if ist_cron and not hart:
            ziele = []
        for z in ziele:
            kante(dienst_id, f"knoten:{z}", "laeuft_auf", pin="hart" if hart else "frei")

    # ── Workloads ──
    for w in workloads:
        ns, name, art = w["metadata"]["namespace"], w["metadata"]["name"], w["kind"]
        sp, st = w.get("spec") or {}, w.get("status") or {}
        if art == "DaemonSet":
            soll, bereit = st.get("desiredNumberScheduled", 0), st.get("numberReady", 0)
        else:
            soll, bereit = sp.get("replicas", 1), st.get("readyReplicas", 0) or 0
        did = f"dienst:{ns}/{name}"
        knoten(did, "dienst", name, ns=ns, art=art, soll=soll, bereit=bereit >= soll)
        gemeinsames(w, did, ns, ist_cron=False)
        # StatefulSet-Vorlagen: PVC <vorlage>-<sts>-<n>
        for vct in sp.get("volumeClaimTemplates") or []:
            vn = vct["metadata"]["name"]
            for (pns, pname) in sorted(pvc_namen):
                if pns == ns and re.fullmatch(rf"{re.escape(vn)}-{re.escape(name)}-\d+", pname):
                    kante(did, f"pvc:{ns}/{pname}", "mountet", beleg=f"volumeClaimTemplate {vn}")

    # ── CronJobs ──
    for c in cronjobs:
        ns, name = c["metadata"]["namespace"], c["metadata"]["name"]
        sp = c.get("spec") or {}
        cid = f"cronjob:{ns}/{name}"
        knoten(cid, "cronjob", name, ns=ns, zeitplan=sp.get("schedule"), ausgesetzt=bool(sp.get("suspend")))
        gemeinsames(c, cid, ns, ist_cron=True)

    # ── Service → Dienst (Selektor) ──
    for s in services:
        ns, name = s["metadata"]["namespace"], s["metadata"]["name"]
        sel = (s.get("spec") or {}).get("selector") or {}
        if not sel:
            continue
        for w in workloads:
            if w["metadata"]["namespace"] != ns:
                continue
            labels = (_vorlage(w).get("metadata") or {}).get("labels") or {}
            if all(labels.get(k) == v for k, v in sel.items()):
                kante(f"service:{ns}/{name}", f"dienst:{ns}/{w['metadata']['name']}", "waehlt")

    # ── Volumes ──
    for c in pvcs:
        ns, name = c["metadata"]["namespace"], c["metadata"]["name"]
        sp, st = c.get("spec") or {}, c.get("status") or {}
        pid = f"pvc:{ns}/{name}"
        knoten(
            pid,
            "pvc",
            name,
            ns=ns,
            klasse=sp.get("storageClassName"),
            groesse=groesse_text(((st.get("capacity") or {}).get("storage")) or ((sp.get("resources") or {}).get("requests") or {}).get("storage")),
            phase=st.get("phase"),
        )
        if sp.get("volumeName"):
            kante(pid, f"pv:{sp['volumeName']}", "bindet")
    for v in pvs:
        name = v["metadata"]["name"]
        sp, st = v.get("spec") or {}, v.get("status") or {}
        claim = sp.get("claimRef") or {}
        knoten(
            f"pv:{name}",
            "pv",
            name,
            klasse=sp.get("storageClassName"),
            groesse=groesse_text((sp.get("capacity") or {}).get("storage")),
            phase=st.get("phase"),
            anspruch=f"{claim.get('namespace')}/{claim.get('name')}" if claim else None,
        )
        ziele: list[str] = []
        for t in (((sp.get("nodeAffinity") or {}).get("required") or {}).get("nodeSelectorTerms")) or []:
            for a in t.get("matchExpressions") or []:
                if a.get("key") == "kubernetes.io/hostname" and a.get("operator") == "In":
                    ziele += [str(x) for x in a.get("values") or []]
        nfs = sp.get("nfs")
        if nfs and nfs.get("server") in ip_zu_knoten:
            ziele.append(ip_zu_knoten[nfs["server"]])
        for z in sorted(set(ziele)):
            kante(f"pv:{name}", f"knoten:{z}", "liegt_auf", pin="hart", beleg="Volume ist an diesen Knoten gebunden")

    return list(k_liste.values()), list(e_liste.values())
