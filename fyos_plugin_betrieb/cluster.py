"""cluster — das Steuerpult fuer den k3s-Cluster (Etappe 4a, 2026-10-03).

Ein k3s-Steuerpult: jede Knoten-Konfiguration, echte Werte, alles gut erreichbar, keine tiefen Menüs.
Oberflaeche: MCP-App ui://fyos/steuerpult.html — jedes Werkzeug traegt `meta.ui`.

Lesend
    cluster.knoten          je Knoten: Rollen, Labels, Taints, Kapazitaet/vergebbar, Summe der
                            Requests/Limits, Auslastung (Prometheus), OS/Kernel/Kubelet/Laufzeit,
                            k3s-Startargumente, Bedingungen, abgesperrt, letzter Boot und Boots in
                            14 Tagen, Pods, offenes Fenster. `mit_kubelet`: Kubelet-Konfiguration.
    cluster.lage            was gerade nicht stimmt, gruppiert (Pods, Jobs, Flux, ausgesetzte
                            CronJobs, Knoten, Alarme, Zertifikat)
    cluster.workloads       Deployments/StatefulSets/DaemonSets und CronJobs: bereit/soll,
                            Neustarts, letzter Abbruch, Knoten, „warum hier", Flux, Nutzung, Aktionen
    cluster.ereignisse      Warnungen gruppiert nach Grund und Objekt-Basisname, Laerm eingeklappt
    cluster.logs            Logs eines Pods oder Deployments (Pods auf gesperrten Knoten: gesperrt)
    cluster.flux_zustand    Kustomizations, Quellen, HelmReleases, Image-Policies/-Repos/-Automation
    cluster.knoten_raeumen  NUR Trockenlauf: was beim Raeumen umzoege, was nicht kann, was stuende
    cluster.altlasten       Released-PVs, nackte Pods, Dienste ohne Endpunkte — Liste + Skript fuer den Betreiber
Schreibend (Trockenlauf ist Standard)
    cluster.neustarten, cluster.skalieren, cluster.flux_abgleichen, cluster.cronjob_jetzt,
    cluster.knoten_absperren

Namen: Der Entwurf fuehrt flux.zustand, cronjob.jetzt, knoten.absperren … als eigene Plugins.
Hier heissen sie cluster.flux_zustand usw.: fyos-web laesst eine App nur Werkzeuge IHRES Plugins
rufen (fyos-web/src/lib/mcp-apps/host.ts, „Nur Werkzeuge desselben Plugins"). Mit eigenen
Plugins waeren Flux, CronJobs und Knoten-Aktionen im Steuerpult in fyos-web tot.

Schutzregeln — im Werkzeug, keine neue Freigabeschicht (offen statt abgeschirmt):
  * Trockenlauf ist Standard; `ausfuehren: true` nur mit `grund`. Der Trockenlauf schickt die
    Aenderung mit dryRun=All an den API-Server: der prueft Rechte und Gueltigkeit, aendert nichts.
  * Fenster-Sperre (lage.sperrgrund) fuer den Zielknoten bzw. die Knoten der Pods eines Workloads.
    Auch Lesen, das auf dem Knoten selbst landet (Logs, Kubelet), ist dort gesperrt.
  * Flux: verwaltet Flux (oder ein Autoscaler) spec.replicas, sagt cluster.skalieren das vorher und
    skaliert nur mit `trotzdem: true`. 0 Replikas nur mit `null_bestaetigt: true`.
  * Fehlt ein Recht, nennt das Werkzeug es samt Befehl fuer den Betreiber — statt mit 403 zu scheitern.
  * Nichts loeschen, kein Drain, Knoten der Steuerebene und die Knoten aus FYOS_CLUSTER_NIE_ABSPERREN werden nie abgesperrt.
  * Jede ausgefuehrte Aktion: Audit (Registry-Hook, mit `grund`) und Bus-Ereignis
    `fyos:events:steuerpult.aktion`.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from mcp_hub.plugin import HubPlugin, ResourceDescriptor, ToolDescriptor
from . import _umgebung, lage
from mcp_hub.plugins import _identitaet
from mcp_hub.plugins._identitaet import identitaet
from mcp_hub.plugins._oberflaeche import oberflaeche

UI_DIR = Path(__file__).with_name("ui")
UI_STEUERPULT = "ui://fyos/steuerpult.html"

# Die App anmelden (im Monorepo steht die Identität fest in _identitaet.py, in der Distribution meldet sie das Plugin an).
_identitaet.registriere_app(
    "steuerpult",
    {
        "titel": "Steuerpult",
        "untertitel": "Den Cluster prüfen und gezielt eingreifen",
        "symbol": "server",
        "ton": 222,
        "kategorie": "System",
    },
    datei="steuerpult.html",
    hauptwerkzeug="cluster__knoten",
)

_LESEN = ["cluster", "read", "flaeche"]
_SCHREIBEN = ["cluster", "write", "flaeche"]
_UI_LESEN = {"ui": {"resourceUri": UI_STEUERPULT}, "app": identitaet("steuerpult")}
#: Schreibende Werkzeuge ruft die App; das Modell sieht sie ueber hub__call_tool trotzdem.
_UI_SCHREIBEN = {
    "ui": {"resourceUri": UI_STEUERPULT, "visibility": ["app"]},
    "app": identitaet("steuerpult"),
}

RECHTE_SKRIPT = "bash scripts/ops/steuerpult_rechte_ausrollen.sh --ja"
STROM = "fyos:events:steuerpult.aktion"
FELDMANAGER = "fyos-steuerpult"
#: Nie absperren: jeder Knoten mit Control-Plane-Rolle, dazu die Knoten, die der Betreiber nennt (z. B. weil lokale Daten
#: dort liegen): FYOS_CLUSTER_NIE_ABSPERREN, kommagetrennt. Ohne Einstellung gilt allein die Rolle.
FLUX_MANAGER = ("kustomize-controller", "helm-controller")
#: Der HPA schreibt spec.replicas ueber /scale; in k3s laeuft er im Prozess `k3s`.
AUTOSKALIERER = ("k3s", "kube-controller-manager")
SYSTEM_NS = ("flux-system", "kube-system")


def nie_absperren() -> tuple[str, ...]:
    return tuple(_umgebung.liste("FYOS_CLUSTER_NIE_ABSPERREN"))


def messknoten() -> str:
    """Knoten, auf dem Messungen laufen und der deshalb ein Dev/Test-Fenster haben kann (FYOS_CLUSTER_MESSKNOTEN)."""
    return _umgebung.lesen("FYOS_CLUSTER_MESSKNOTEN")


def messspuren() -> tuple[str, ...]:
    """Spuren, an denen ein CronJob „gegen den Messknoten arbeitet“ (Name, Env, Befehl, Volumes): Knotenname, Adresse,
    Dienstnamen oder Ports (FYOS_CLUSTER_MESSSPUREN, kommagetrennt). Der Knotenname zählt immer mit."""
    k = messknoten()
    return tuple(dict.fromkeys(([k] if k else []) + _umgebung.liste("FYOS_CLUSTER_MESSSPUREN")))

#: Ereignisse, die nur Laerm sind: eingeklappt, aber mitgezaehlt (Befund 9).
LAERM = frozenset({"DNSConfigForming"})
#: Routine bei nur_warnungen=false — ebenfalls eingeklappt.
LAERM_NORMAL = frozenset({
    "ReconciliationSucceeded", "Pulled", "Pulling", "Created", "Started", "Scheduled",
    "SuccessfulCreate", "SuccessfulDelete", "Completed", "SawCompletedJob", "Succeeded",
    "ArtifactUpToDate", "GitOperationSucceeded", "NewArtifact", "Progressing", "ScalingReplicaSet",
})
LOG_ZEILEN = 200
LOG_MAX_ZEILEN = 2000
LOG_MAX_BYTES = 512 * 1024
REPLIKAS_MAX = 50
GRUND_MAX = 300
#: Zertifikat: ab hier gelb/rot (Befund 14: „Warnung < 21 Tage").
ZERT_GELB_TAGE = 21
ZERT_ROT_TAGE = 7
#: Ein Pod, der so lange nicht bereit ist oder beim Start haengt, gilt als Problem.
HAENGT_MIN = 5
_GRUND_LEER = "ausfuehren nur mit grund — warum soll das passieren?"


def recht_fehlt(was: str) -> str:
    return f"Recht fehlt: {was} – Betreiber: {RECHTE_SKRIPT}"


# ── kleine Helfer ───────────────────────────────────────────────────────────────────────────
_MENGE = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)([A-Za-z]*)$")
_FAKTOR = {
    "": 1.0, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15,
    "E": 1e18, "Ki": 2.0**10, "Mi": 2.0**20, "Gi": 2.0**30, "Ti": 2.0**40, "Pi": 2.0**50, "Ei": 2.0**60,
}


def menge(wert: Any) -> float:
    """Kubernetes-Menge als Zahl: "250m" → 0.25, "1Gi" → 1073741824.0. Unlesbares → 0."""
    if wert is None:
        return 0.0
    if isinstance(wert, (int, float)):
        return float(wert)
    t = str(wert).strip()
    m = _MENGE.match(t)
    if not m or m.group(2) not in _FAKTOR:
        try:
            return float(t)
        except ValueError:
            return 0.0
    return float(m.group(1)) * _FAKTOR[m.group(2)]


def _ts(wert: Any) -> float | None:
    """ISO-Zeitpunkt der API ("2026-10-03T06:25:50Z") als Epoche."""
    if not wert:
        return None
    try:
        return dt.datetime.fromisoformat(str(wert).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _iso(epoche: float | None) -> str | None:
    if epoche is None:
        return None
    return dt.datetime.fromtimestamp(epoche, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _kurz(text: Any, n: int = 200) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def _g(d: Any, *pfad: str, vorgabe: Any = None) -> Any:
    """Verschachtelt lesen: _g(pod, "status", "phase")."""
    for k in pfad:
        if not isinstance(d, dict):
            return vorgabe
        d = d.get(k)
    return vorgabe if d is None else d


def _name(o: dict) -> str:
    return str(_g(o, "metadata", "name", vorgabe=""))


def _ns(o: dict) -> str:
    return str(_g(o, "metadata", "namespace", vorgabe=""))


def _labels(o: dict) -> dict[str, str]:
    return dict(_g(o, "metadata", "labels", vorgabe={}) or {})


def _annot(o: dict) -> dict[str, str]:
    return dict(_g(o, "metadata", "annotations", vorgabe={}) or {})


_GEHEIM = re.compile(r"(token|secret|passw|password|key)", re.I)


def schwaerzen(argumente: list[Any]) -> list[str]:
    """k3s-Startargumente ohne Geheimnisse. k3s schwaerzt selbst schon (********) — hier noch
    einmal, damit nie ein Token im Steuerpult steht, auch wenn k3s es einmal vergisst."""
    out: list[str] = []
    naechstes_weg = False
    for a in argumente or []:
        s = str(a)
        if naechstes_weg:
            out.append("***")
            naechstes_weg = False
            continue
        if s.startswith("-") and _GEHEIM.search(s):
            if "=" in s:
                out.append(s.split("=", 1)[0] + "=***")
            else:
                out.append(s)
                naechstes_weg = True
            continue
        out.append(s)
    return out


# ── Analyse (reine Funktionen, ohne Cluster testbar) ───────────────────────────────────────
def _mf(eintrag: Any, roh: str, typ: str) -> Any:
    """managedFields-Eintrag lesen — roh (camelCase-Dict) oder typisiert (python-client)."""
    if isinstance(eintrag, dict):
        return eintrag.get(roh)
    return getattr(eintrag, typ, None)


def replikas_verwalter(obj: Any) -> dict[str, Any]:
    """Wer bestimmt spec.replicas? Aus den managedFields — fuer Rohdaten und typisierte Objekte.

    Flux (kustomize-controller, Server-Side-Apply) setzt beim naechsten Abgleich den Wert aus Git
    zurueck (Befund 5). Ein HPA schreibt ueber die Unterressource /scale. Beides heisst: eine
    Aenderung von Hand haelt nicht.
    """
    if isinstance(obj, dict):
        meta = obj.get("metadata") or {}
        eintraege = meta.get("managedFields") or []
        labels = meta.get("labels") or {}
    else:
        meta = getattr(obj, "metadata", None)
        eintraege = getattr(meta, "managed_fields", None) or []
        labels = getattr(meta, "labels", None) or {}
    flux: list[str] = []
    andere: list[str] = []
    auto = False
    for e in eintraege:
        felder = _mf(e, "fieldsV1", "fields_v1") or {}
        if not isinstance(felder, dict) or "f:replicas" not in (felder.get("f:spec") or {}):
            continue
        manager = str(_mf(e, "manager", "manager") or "")
        unter = str(_mf(e, "subresource", "subresource") or "")
        if manager in FLUX_MANAGER:
            flux.append(manager)
        elif manager in AUTOSKALIERER and unter == "scale":
            auto = True
        else:
            andere.append(manager)
    ks = labels.get("kustomize.toolkit.fluxcd.io/name")
    hr = labels.get("helm.toolkit.fluxcd.io/name")
    return {
        "flux": bool(flux),
        "manager": flux[0] if flux else None,
        "autoskalierer": auto,
        "kustomization": f"{labels.get('kustomize.toolkit.fluxcd.io/namespace', 'flux-system')}/{ks}" if ks else None,
        "helmrelease": f"{labels.get('helm.toolkit.fluxcd.io/namespace', '')}/{hr}" if hr else None,
        "andere": andere,
    }


def flux_verwaltet(obj: dict) -> str | None:
    """"flux-system/fyos-mcp-hub" bzw. "HelmRelease monitoring/monitoring", wenn Flux das Objekt fuehrt."""
    lab = _labels(obj)
    if lab.get("kustomize.toolkit.fluxcd.io/name"):
        return f"{lab.get('kustomize.toolkit.fluxcd.io/namespace', 'flux-system')}/{lab['kustomize.toolkit.fluxcd.io/name']}"
    if lab.get("helm.toolkit.fluxcd.io/name"):
        return f"HelmRelease {lab.get('helm.toolkit.fluxcd.io/namespace', '')}/{lab['helm.toolkit.fluxcd.io/name']}"
    return None


_RESSOURCEN = ("cpu", "memory", "nvidia.com/gpu")


def pod_summen(pod: dict) -> tuple[dict[str, float], dict[str, float]]:
    """(requests, limits) eines Pods wie der Scheduler sie zaehlt: max(Summe der Container,
    groesster Init-Container) plus Overhead; Sidecar-Init-Container (restartPolicy Always) zaehlen
    wie normale Container."""
    spec = pod.get("spec") or {}

    def zeile(c: dict, art: str) -> dict[str, float]:
        r = (c.get("resources") or {}).get(art) or {}
        return {k: menge(r.get(k)) for k in _RESSOURCEN}

    def summe(liste: list[dict[str, float]]) -> dict[str, float]:
        return {k: sum(x[k] for x in liste) for k in _RESSOURCEN}

    out = []
    for art in ("requests", "limits"):
        apps = [zeile(c, art) for c in spec.get("containers") or []]
        init = []
        for c in spec.get("initContainers") or []:
            (apps if c.get("restartPolicy") == "Always" else init).append(zeile(c, art))
        s = summe(apps)
        for i in init:
            for k in _RESSOURCEN:
                s[k] = max(s[k], i[k])
        for k, v in (spec.get("overhead") or {}).items():
            if k in s:
                s[k] += menge(v)
        out.append(s)
    return out[0], out[1]


def _ausdruck_passt(ausdruck: dict, labels: dict[str, str]) -> bool:
    key, op = ausdruck.get("key"), ausdruck.get("operator")
    werte = [str(v) for v in ausdruck.get("values") or []]
    da = key in labels
    if op == "In":
        return da and labels[key] in werte
    if op == "NotIn":
        return not da or labels[key] not in werte
    if op == "Exists":
        return da
    if op == "DoesNotExist":
        return not da
    if op in ("Gt", "Lt") and da and werte:
        try:
            a, b = int(labels[key]), int(werte[0])
        except ValueError:
            return False
        return a > b if op == "Gt" else a < b
    return False


def _term_passt(term: dict, knoten: dict) -> bool:
    labels = _labels(knoten)
    felder = {"metadata.name": _name(knoten)}
    return all(_ausdruck_passt(a, labels) for a in term.get("matchExpressions") or []) and all(
        _ausdruck_passt(a, felder) for a in term.get("matchFields") or []
    )


def _toleriert(taint: dict, tols: list[dict]) -> bool:
    for t in tols or []:
        if t.get("effect") and t.get("effect") != taint.get("effect"):
            continue
        if t.get("operator") == "Exists":
            if not t.get("key") or t.get("key") == taint.get("key"):
                return True
            continue
        if t.get("key") == taint.get("key") and str(t.get("value") or "") == str(taint.get("value") or ""):
            return True
    return False


def moegliche_knoten(spec: dict, knoten: list[dict], *, abgesperrte_zaehlen: bool = False) -> list[str]:
    """Knoten, auf denen ein Pod mit dieser Spec landen DARF (nodeSelector, Pflicht-Affinitaet,
    Taints). Vorlieben (preferred) zaehlen nicht — die entscheidet der Scheduler."""
    sel = spec.get("nodeSelector") or {}
    pflicht = _g(spec, "affinity", "nodeAffinity", "requiredDuringSchedulingIgnoredDuringExecution", "nodeSelectorTerms") or []
    out = []
    for k in knoten:
        if spec.get("nodeName") and spec["nodeName"] != _name(k):
            continue
        if not abgesperrte_zaehlen and _g(k, "spec", "unschedulable"):
            continue
        labels = _labels(k)
        if any(labels.get(a) != str(b) for a, b in sel.items()):
            continue
        if pflicht and not any(_term_passt(t, k) for t in pflicht):
            continue
        taints = [t for t in _g(k, "spec", "taints", vorgabe=[]) if t.get("effect") in ("NoSchedule", "NoExecute")]
        if any(not _toleriert(t, spec.get("tolerations") or []) for t in taints):
            continue
        out.append(_name(k))
    return out


def warum_hier(spec: dict) -> list[str]:
    """Die Platzierungsregeln einer Pod-Vorlage in kurzen Saetzen („warum hier")."""
    out: list[str] = []
    for k, v in (spec.get("nodeSelector") or {}).items():
        out.append(f"fest: {k}={v}")
    aff = spec.get("affinity") or {}
    for term in _g(aff, "nodeAffinity", "requiredDuringSchedulingIgnoredDuringExecution", "nodeSelectorTerms") or []:
        teile = [f"{a.get('key')} {a.get('operator')} {','.join(map(str, a.get('values') or []))}".strip()
                 for a in term.get("matchExpressions") or []]
        if teile:
            out.append("muss: " + " und ".join(teile))
    for p in _g(aff, "nodeAffinity", "preferredDuringSchedulingIgnoredDuringExecution") or []:
        teile = [f"{a.get('key')} {a.get('operator')} {','.join(map(str, a.get('values') or []))}".strip()
                 for a in _g(p, "preference", "matchExpressions") or []]
        if teile:
            out.append(f"bevorzugt ({p.get('weight')}): " + " und ".join(teile))
    for art, text in (("podAffinity", "neben"), ("podAntiAffinity", "nicht neben")):
        for t in _g(aff, art, "requiredDuringSchedulingIgnoredDuringExecution") or []:
            lab = _g(t, "labelSelector", "matchLabels") or {}
            out.append(f"{text}: " + ",".join(f"{k}={v}" for k, v in lab.items()) + f" ({t.get('topologyKey')})")
    for t in spec.get("topologySpreadConstraints") or []:
        out.append(f"verteilt nach {str(t.get('topologyKey', '')).split('/')[-1]} (maxSkew {t.get('maxSkew')})")
    for t in spec.get("tolerations") or []:
        if t.get("key") in ("node.kubernetes.io/not-ready", "node.kubernetes.io/unreachable"):
            continue  # setzt Kubernetes bei jedem Pod
        if t.get("operator") == "Exists" and not t.get("key"):
            out.append("toleriert jeden Taint")
        else:
            out.append(f"toleriert {t.get('key')}{'=' + str(t['value']) if t.get('value') else ''}:{t.get('effect') or 'alle'}")
    return out or ["keine Vorgabe — der Scheduler waehlt"]


_SICHER = "bcdfghjklmnpqrstvwxz2456789"  # Alphabet der generierten Namensendungen (ohne Vokale)
_RE_JOBPOD = re.compile(r"^(.*)-\d{6,}-[" + _SICHER + r"]{5}$")
_RE_JOB = re.compile(r"^(.*)-\d{6,}$")
_RE_DEPPOD = re.compile(r"^(.*)-[" + _SICHER + r"]{6,10}-[" + _SICHER + r"]{5}$")
_RE_KURZ = re.compile(r"^(.*)-[" + _SICHER + r"]{5}$")
_RE_RS = re.compile(r"^(.*)-[" + _SICHER + r"]{6,10}$")
_RE_STS = re.compile(r"^(.*)-\d+$")


def basisname(art: str, name: str) -> str:
    """Objektname ohne generierte Endung: web-7f9c8d6b5-x2kq9 → web, canary-29849063 → canary."""
    for muster in (_RE_JOBPOD, _RE_JOB) if art in ("Pod", "Job") else ():
        m = muster.match(name)
        if m:
            return m.group(1)
    if art == "Pod":
        for muster in (_RE_DEPPOD, _RE_KURZ, _RE_STS):
            m = muster.match(name)
            if m:
                return m.group(1)
    if art == "ReplicaSet":
        m = _RE_RS.match(name)
        if m:
            return m.group(1)
    return name


def besitzer(pod: dict, rs_zu_dep: dict[tuple[str, str], str]) -> tuple[str, str] | None:
    """(art, name) des Workloads, dem der Pod gehoert — deploy/sts/ds/job/rs/statisch — oder None."""
    refs = _g(pod, "metadata", "ownerReferences", vorgabe=[])
    ref = next((r for r in refs if r.get("controller")), refs[0] if refs else None)
    if not ref:
        return None
    art, name = ref.get("kind"), str(ref.get("name") or "")
    if art == "ReplicaSet":
        dep = rs_zu_dep.get((_ns(pod), name))
        return ("deploy", dep) if dep else ("rs", name)
    return {"StatefulSet": ("sts", name), "DaemonSet": ("ds", name), "Job": ("job", name), "Node": ("statisch", name)}.get(
        str(art), (str(art).lower(), name)
    )


def pod_bereit(pod: dict) -> bool:
    return any(c.get("type") == "Ready" and c.get("status") == "True" for c in _g(pod, "status", "conditions", vorgabe=[]))


def pod_neustarts(pod: dict) -> int:
    return sum(int(c.get("restartCount") or 0) for c in _g(pod, "status", "containerStatuses", vorgabe=[]))


def letzter_abbruch(pods: list[dict]) -> dict[str, Any] | None:
    """Juengster beendeter Container-Lauf (lastState.terminated) ueber alle Pods."""
    bester: dict[str, Any] | None = None
    for p in pods:
        for c in _g(p, "status", "containerStatuses", vorgabe=[]):
            t = _g(c, "lastState", "terminated")
            if not t:
                continue
            wann = _ts(t.get("finishedAt"))
            if bester is None or (wann or 0) > (bester["_t"] or 0):
                bester = {"_t": wann, "grund": t.get("reason") or "?", "code": t.get("exitCode"),
                          "wann": t.get("finishedAt"), "container": c.get("name"), "pod": _name(p)}
    if bester:
        bester.pop("_t", None)
    return bester


_WARTEN_ROT = ("CrashLoopBackOff", "ImagePullBackOff", "ErrImagePull", "CreateContainerConfigError",
               "CreateContainerError", "InvalidImageName", "RunContainerError")


def pod_problem(pod: dict, jetzt: float) -> tuple[str, str, str] | None:
    """(schwere rot|gelb, grund, detail) — oder None, wenn der Pod in Ordnung ist."""
    st = pod.get("status") or {}
    phase = st.get("phase")
    if phase == "Succeeded":
        return None
    for c in (st.get("initContainerStatuses") or []) + (st.get("containerStatuses") or []):
        w = _g(c, "state", "waiting")
        if w and w.get("reason") in _WARTEN_ROT:
            return "rot", str(w["reason"]), _kurz(f"{c.get('name')}: {w.get('message') or ''}", 160)
    seit = _ts(st.get("startTime")) or _ts(_g(pod, "metadata", "creationTimestamp"))
    alter_min = (jetzt - seit) / 60 if seit else 0
    if phase == "Pending":
        geplant = next((c for c in st.get("conditions") or [] if c.get("type") == "PodScheduled"), None)
        if geplant and geplant.get("status") == "False":
            return "rot", "nicht einplanbar", _kurz(geplant.get("message"), 160)
        if alter_min >= HAENGT_MIN:
            return "gelb", "haengt beim Start", f"seit {int(alter_min)} min Pending"
        return None
    if phase == "Failed":
        return "rot", f"beendet: {st.get('reason') or 'Failed'}", _kurz(st.get("message"), 160)
    if phase == "Unknown":
        return "rot", "Zustand unbekannt", "Knoten meldet sich nicht"
    if phase == "Running" and not pod_bereit(pod):
        bereit = next((c for c in st.get("conditions") or [] if c.get("type") == "Ready"), {})
        nicht_seit = _ts(bereit.get("lastTransitionTime")) or seit
        if nicht_seit and (jetzt - nicht_seit) / 60 >= HAENGT_MIN:
            return "gelb", "nicht bereit", f"seit {int((jetzt - nicht_seit) // 60)} min"
    for c in st.get("containerStatuses") or []:
        t = _g(c, "lastState", "terminated") or {}
        ende = _ts(t.get("finishedAt"))
        if t.get("reason") == "OOMKilled" and ende and jetzt - ende < 6 * 3600:
            return "gelb", "OOMKilled", f"{c.get('name')} vor {int((jetzt - ende) // 60)} min"
    return None


def messknoten_bezug(cj: dict) -> list[str]:
    """Woran man sieht, dass ein CronJob gegen den Messknoten arbeitet (Name, Env, Befehl, Bindung, GPU).

    Ohne FYOS_CLUSTER_MESSKNOTEN gibt es keinen Messknoten: leere Liste."""
    spuren: list[str] = []
    knoten = messknoten().lower()
    if not knoten:
        return spuren
    muster = tuple(m.lower() for m in messspuren())
    name = _name(cj).lower()
    if knoten in name:
        spuren.append(f"Name nennt {knoten}")
    spec = _g(cj, "spec", "jobTemplate", "spec", "template", "spec", vorgabe={})

    def treffer(text: str) -> str | None:
        t = text.lower()
        return next((m for m in muster if m in t), None)

    for c in (spec.get("containers") or []) + (spec.get("initContainers") or []):
        for e in c.get("env") or []:
            m = treffer(f"{e.get('name')}={e.get('value') or ''}")
            if m:
                spuren.append(f"Env {e.get('name')} nennt {m}")
        m = treffer(" ".join(map(str, (c.get("command") or []) + (c.get("args") or []))))
        if m:
            spuren.append(f"Befehl von {c.get('name')} nennt {m}")
        for art in ("limits", "requests"):
            if menge(_g(c, "resources", art, "nvidia.com/gpu")) > 0:
                spuren.append("braucht eine GPU")
                break
    bindung = json.dumps({"s": spec.get("nodeSelector"), "a": _g(spec, "affinity", "nodeAffinity",
                                                                "requiredDuringSchedulingIgnoredDuringExecution")})
    if knoten in bindung.lower():
        spuren.append(f"Knotenbindung an {knoten}")
    m = treffer(json.dumps(spec.get("volumes") or []))
    if m:
        spuren.append(f"Volume nennt {m}")
    return list(dict.fromkeys(spuren))


def suspend_grund_aus_repo(cj: dict, repo: str) -> str | None:
    """Grund einer Aussetzung aus dem Manifest: die Kommentarzeilen direkt ueber `suspend: true`.

    Gefunden ueber die Annotation fyos.io/kontext.wo („fyos/ops, k8s/base/ops/x.yaml"). Am
    02.10. hatte github-ruf-waechter keine Grund-Annotation, der Grund stand nur als Kommentar
    im Manifest (Befund 12)."""
    wo = _annot(cj).get("fyos.io/kontext.wo") or ""
    if not repo:
        return None
    pfad = next((t.strip() for t in wo.split(",") if t.strip().endswith((".yaml", ".yml"))), None)
    if not pfad or ".." in pfad:
        return None
    datei = Path(repo) / pfad
    try:
        zeilen = datei.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for i, z in enumerate(zeilen):
        if z.strip() != "suspend: true":
            continue
        block: list[str] = []
        j = i - 1
        while j >= 0 and zeilen[j].strip().startswith("#"):
            block.insert(0, zeilen[j].strip().lstrip("#").strip())
            j -= 1
        text = " ".join(b for b in block if b)
        return _kurz(text, 400) if text else None
    return None


# ── Zugang zum API-Server ───────────────────────────────────────────────────────────────────
class KubeFehler(RuntimeError):
    def __init__(self, status: int | None, text: str):
        super().__init__(text)
        self.status = status


#: art → (API, Singular fuer Methodennamen, namensraumgebunden)
_KERN: dict[str, tuple[str, str, bool]] = {
    "nodes": ("core", "node", False),
    "pods": ("core", "pod", True),
    "events": ("core", "event", True),
    "pvcs": ("core", "persistent_volume_claim", True),
    "pvs": ("core", "persistent_volume", False),
    "services": ("core", "service", True),
    "endpoints": ("core", "endpoints", True),
    "deployments": ("apps", "deployment", True),
    "statefulsets": ("apps", "stateful_set", True),
    "daemonsets": ("apps", "daemon_set", True),
    "replicasets": ("apps", "replica_set", True),
    "cronjobs": ("batch", "cron_job", True),
    "jobs": ("batch", "job", True),
    "pdbs": ("policy", "pod_disruption_budget", True),
}
#: Flux-Objekte: art → (Gruppe, Version, Plural)
_CRD: dict[str, tuple[str, str, str]] = {
    "kustomizations": ("kustomize.toolkit.fluxcd.io", "v1", "kustomizations"),
    "gitrepositories": ("source.toolkit.fluxcd.io", "v1", "gitrepositories"),
    "helmrepositories": ("source.toolkit.fluxcd.io", "v1", "helmrepositories"),
    "ocirepositories": ("source.toolkit.fluxcd.io", "v1", "ocirepositories"),
    "helmreleases": ("helm.toolkit.fluxcd.io", "v2", "helmreleases"),
    "imagepolicies": ("image.toolkit.fluxcd.io", "v1", "imagepolicies"),
    "imagerepositories": ("image.toolkit.fluxcd.io", "v1", "imagerepositories"),
    "imageupdateautomations": ("image.toolkit.fluxcd.io", "v1", "imageupdateautomations"),
}
_PATCH_ART = {"deploy": "deployments", "sts": "statefulsets", "ds": "daemonsets"}
_ART_NAME = {"kustomizations": "Kustomization", "gitrepositories": "GitRepository", "ocirepositories": "OCIRepository",
             "helmrepositories": "HelmRepository"}


class Kube:
    """Duenner Zugang zum API-Server: Antworten roh als JSON (wie `kubectl -o json`).

    Roh statt typisiert: schneller (keine Modell-Deserialisierung fuer 170 Pods), und die Tests
    koennen echte kubectl-Ausgaben als Vorlage nehmen. In-cluster mit ServiceAccount, sonst
    KUBECONFIG (lokal auf dem Mac)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._apis: dict[str, Any] | None = None

    def _api(self, welche: str) -> Any:
        with self._lock:
            if self._apis is None:
                from kubernetes import client, config  # im Hub-Image (kubernetes>=30)

                if os.getenv("KUBERNETES_SERVICE_HOST"):
                    config.load_incluster_config()
                else:
                    config.load_kube_config(config_file=os.getenv("KUBECONFIG") or None)
                basis = client.ApiClient()
                self._apis = {
                    "core": client.CoreV1Api(basis), "apps": client.AppsV1Api(basis),
                    "batch": client.BatchV1Api(basis), "policy": client.PolicyV1Api(basis),
                    "custom": client.CustomObjectsApi(basis), "authz": client.AuthorizationV1Api(basis),
                }
            return self._apis[welche]

    @staticmethod
    def _roh(fn: Callable[..., Any], *a: Any, **kw: Any) -> Any:
        try:
            r = fn(*a, _preload_content=False, **kw)
        except Exception as e:  # noqa: BLE001 — ApiException und Netzfehler einheitlich
            status = getattr(e, "status", None)
            text = getattr(e, "body", None) or str(e)
            try:
                text = json.loads(text).get("message") or text
            except (TypeError, ValueError, AttributeError):
                pass
            raise KubeFehler(status, f"HTTP {status}: {_kurz(text, 300)}" if status else _kurz(text, 300)) from e
        daten = getattr(r, "data", r)
        return daten

    def _json(self, fn: Callable[..., Any], *a: Any, **kw: Any) -> dict:
        daten = self._roh(fn, *a, **kw)
        if isinstance(daten, (bytes, bytearray)):
            daten = daten.decode("utf-8", "replace")
        return json.loads(daten) if isinstance(daten, str) and daten else (daten or {})

    def liste(self, art: str, ns: str | None = None, *, feld: str | None = None, selektor: str | None = None) -> list[dict]:
        kw: dict[str, Any] = {"_request_timeout": 30}
        if feld:
            kw["field_selector"] = feld
        if selektor:
            kw["label_selector"] = selektor
        if art in _CRD:
            g, v, p = _CRD[art]
            api = self._api("custom")
            if ns:
                return self._json(api.list_namespaced_custom_object, g, v, ns, p, **kw).get("items") or []
            return self._json(api.list_cluster_custom_object, g, v, p, **kw).get("items") or []
        welche, s, gebunden = _KERN[art]
        api = self._api(welche)
        if not gebunden:
            return self._json(getattr(api, f"list_{s}"), **kw).get("items") or []
        if ns:
            return self._json(getattr(api, f"list_namespaced_{s}"), ns, **kw).get("items") or []
        return self._json(getattr(api, f"list_{s}_for_all_namespaces"), **kw).get("items") or []

    def lesen(self, art: str, name: str, ns: str | None = None) -> dict:
        if art in _CRD:
            g, v, p = _CRD[art]
            return self._json(self._api("custom").get_namespaced_custom_object, g, v, ns, p, name, _request_timeout=15)
        welche, s, gebunden = _KERN[art]
        api = self._api(welche)
        if not gebunden:
            return self._json(getattr(api, f"read_{s}"), name, _request_timeout=15)
        return self._json(getattr(api, f"read_namespaced_{s}"), name, ns, _request_timeout=15)

    def patchen(self, art: str, name: str, ns: str | None, koerper: dict, *, trocken: bool) -> dict:
        kw: dict[str, Any] = {"field_manager": FELDMANAGER, "_request_timeout": 20}
        if trocken:
            kw["dry_run"] = "All"
        if art in _CRD:
            g, v, p = _CRD[art]
            # CRDs kennen kein Strategic-Merge; der Client wuerde es bei einem Dict waehlen.
            return self._json(self._api("custom").patch_namespaced_custom_object, g, v, ns, p, name, koerper,
                              _content_type="application/merge-patch+json", **kw)
        welche, s, gebunden = _KERN[art]
        api = self._api(welche)
        if not gebunden:
            return self._json(getattr(api, f"patch_{s}"), name, koerper,
                              _content_type="application/merge-patch+json", **kw)
        return self._json(getattr(api, f"patch_namespaced_{s}"), name, ns, koerper,
                          _content_type="application/strategic-merge-patch+json", **kw)

    def skalieren(self, name: str, ns: str, replikas: int, *, trocken: bool) -> dict:
        kw: dict[str, Any] = {"field_manager": FELDMANAGER, "_request_timeout": 20}
        if trocken:
            kw["dry_run"] = "All"
        return self._json(self._api("apps").patch_namespaced_deployment_scale, name, ns,
                          {"spec": {"replicas": int(replikas)}},
                          _content_type="application/merge-patch+json", **kw)

    def job_anlegen(self, ns: str, koerper: dict, *, trocken: bool) -> dict:
        kw: dict[str, Any] = {"field_manager": FELDMANAGER, "_request_timeout": 20}
        if trocken:
            kw["dry_run"] = "All"
        return self._json(self._api("batch").create_namespaced_job, ns, koerper, **kw)

    def logs(self, name: str, ns: str, *, container: str | None, zeilen: int, vorher: bool) -> str:
        kw: dict[str, Any] = {"tail_lines": int(zeilen), "previous": bool(vorher), "limit_bytes": LOG_MAX_BYTES,
                              "timestamps": False, "_request_timeout": 30}
        if container:
            kw["container"] = container
        daten = self._roh(self._api("core").read_namespaced_pod_log, name, ns, **kw)
        return daten.decode("utf-8", "replace") if isinstance(daten, (bytes, bytearray)) else str(daten or "")

    def kubelet_konfig(self, knoten: str) -> dict:
        return self._json(self._api("core").connect_get_node_proxy_with_path, knoten, "configz", _request_timeout=15)

    def darf(self, verb: str, ressource: str, *, gruppe: str = "", ns: str | None = None, unter: str | None = None) -> bool:
        attr: dict[str, Any] = {"verb": verb, "resource": ressource, "group": gruppe}
        if ns:
            attr["namespace"] = ns
        if unter:
            attr["subresource"] = unter
        antwort = self._json(self._api("authz").create_self_subject_access_review,
                             {"apiVersion": "authorization.k8s.io/v1", "kind": "SelfSubjectAccessReview",
                              "spec": {"resourceAttributes": attr}}, _request_timeout=10)
        return bool(_g(antwort, "status", "allowed"))


#: Rechte, die das Steuerpult prueft: Name → (verb, ressource, gruppe, unterressource, namespace)
RECHTE: dict[str, tuple[str, str, str, str | None, str | None]] = {
    "deployments patch": ("patch", "deployments", "apps", None, None),
    "deployments/scale patch": ("patch", "deployments", "apps", "scale", None),
    "statefulsets patch": ("patch", "statefulsets", "apps", None, None),
    "daemonsets patch": ("patch", "daemonsets", "apps", None, None),
    "jobs create": ("create", "jobs", "batch", None, "fyos"),
    "nodes patch": ("patch", "nodes", "", None, None),
    "nodes/proxy get": ("get", "nodes", "", "proxy", None),
    "kustomizations patch": ("patch", "kustomizations", "kustomize.toolkit.fluxcd.io", None, "flux-system"),
    "persistentvolumes list": ("list", "persistentvolumes", "", None, None),
    "helmreleases list": ("list", "helmreleases", "helm.toolkit.fluxcd.io", None, None),
}
_RECHT_ZUM_NEUSTART = {"deploy": "deployments patch", "sts": "statefulsets patch", "ds": "daemonsets patch"}


def _selektor(sel: dict | None) -> str:
    """LabelSelector (matchLabels + matchExpressions) als Text fuer label_selector."""
    teile = [f"{k}={v}" for k, v in ((sel or {}).get("matchLabels") or {}).items()]
    for a in (sel or {}).get("matchExpressions") or []:
        op, werte = a.get("operator"), ",".join(map(str, a.get("values") or []))
        if op == "In":
            teile.append(f"{a['key']} in ({werte})")
        elif op == "NotIn":
            teile.append(f"{a['key']} notin ({werte})")
        elif op == "Exists":
            teile.append(str(a["key"]))
        elif op == "DoesNotExist":
            teile.append(f"!{a['key']}")
    return ",".join(teile)


def _bild_kurz(bild: str) -> str:
    """registry.example.org/team/hub:main@sha256:8468… → hub:main@84685…"""
    rest = bild.rsplit("/", 1)[-1]
    if "@sha256:" in rest:
        vorn, digest = rest.split("@sha256:", 1)
        return f"{vorn}@{digest[:8]}"
    return rest


def ereignis_zeit(e: dict) -> float | None:
    return (_ts(e.get("lastTimestamp")) or _ts(_g(e, "series", "lastObservedTime")) or _ts(e.get("eventTime"))
            or _ts(_g(e, "metadata", "creationTimestamp")))


def ereignis_anzahl(e: dict) -> int:
    return int(e.get("count") or _g(e, "series", "count") or 1)


def _bedingung(o: dict, typ: str = "Ready") -> dict:
    return next((c for c in _g(o, "status", "conditions", vorgabe=[]) if c.get("type") == typ), {})


#: Spurlänge je Messung in den Knotenkarten (6 h ≈ alle 12 min ein Wert)
SPUR_WERTE = 30
_SPUR_ART = {"cpu": "cpu", "ram": "ram", "platte": "platte", "gpu_last": "last", "gpu_speicher": "speicher", "gpu_temp": "temp"}


def spuren_je_knoten(verlauf: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """verlauf.zustand → je Knoten die Spuren der Messungen (rein, ohne Netz; 04.10.).

    Ergebnis: {knoten: {cpu: [..], ram: [..], platte: [..], gpus: {nr: {last, speicher, temp}}}}. Zuordnung über
    `ort` der Kurven (roher Knotenname), nicht über den Anzeigenamen.
    """
    out: dict[str, dict[str, Any]] = {}
    for g in verlauf.get("gruppen") or []:
        for k in g.get("kurven") or []:
            ort = k.get("ort") if isinstance(k, dict) else None
            art = _SPUR_ART.get(str(k.get("id", "")).split(":")[0]) if ort else None
            if not art or not ort.get("knoten"):
                continue
            werte = [p[1] for p in k.get("punkte") or [] if isinstance(p, (list, tuple)) and len(p) == 2]
            werte = werte[:: max(1, len(werte) // SPUR_WERTE)][-SPUR_WERTE:]
            ziel = out.setdefault(ort["knoten"], {})
            if "gpu" in ort:
                ziel.setdefault("gpus", {}).setdefault(ort["gpu"], {})[art] = werte
            else:
                ziel[art] = werte
    return out


class ClusterPlugin(HubPlugin):
    name = "cluster"
    description = (
        "Steuerpult für den k3s-Cluster: Knoten mit echter Konfiguration und Auslastung, was nicht "
        "stimmt, Workloads, Ereignisse, Logs, Flux — Neustarten, Skalieren, Abgleichen, CronJob "
        "starten, Absperren nur mit Trockenlauf, Grund und Fenster-Sperre."
    )
    version = "0.1.0"

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__()
        self.config = config or {}
        self.k: Any = None  # Zugang zum API-Server; Tests setzen eine Attrappe mit derselben Schnittstelle
        self._prom: Callable[[str], list[dict]] | None = None  # Tests: Abfrage → result-Liste
        self._bus: Any = None  # Tests: Redis-Attrappe fuer das Bus-Ereignis
        self._cache = lage._Zwischenspeicher()
        self._lage_p: Any = None
        self._pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="cluster")
        self.repo = _umgebung.lesen("FYOS_REPO_PATH")  # leer: Aussetzungsgründe nur aus Annotationen
        self._registry: Any = None

    def registry_anbinden(self, registry: Any) -> None:
        """Vom Server gerufen: Zugang zu anderen Plugins (hier: verlauf für die Spuren je Knoten)."""
        self._registry = registry

    def _verlauf_spuren(self) -> dict[str, dict[str, Any]]:
        p = (getattr(self._registry, "plugins", None) or {}).get("verlauf")
        if p is None:
            return {}
        return spuren_je_knoten(p.call("zustand", {"zeitraum": "6h", "punkte": True}))

    def register(self) -> None:
        cfg = getattr(self, "config", {}) or {}
        self.repo = str(cfg.get("repo") or _umgebung.lesen("FYOS_REPO_PATH"))
        return None

    def health(self) -> dict[str, Any]:
        if os.getenv("KUBERNETES_SERVICE_HOST"):
            return {"ok": True, "status": "ok", "modus": "in-cluster"}
        if os.getenv("KUBECONFIG") or Path("~/.kube/config").expanduser().exists():
            return {"ok": True, "status": "ok", "modus": "kubeconfig"}
        return {"ok": False, "status": "degraded", "reason": "kein Zugang zum API-Server (weder in-cluster noch KUBECONFIG)"}

    # ── Zugaenge ────────────────────────────────────────────────────────────────────────────
    def kube(self) -> Any:
        if self.k is None:
            self.k = Kube()
        return self.k

    def _liste(self, art: str, ttl: float = 15.0) -> list[dict]:
        return self._cache.holen(f"liste:{art}", ttl, lambda: self.kube().liste(art))

    def _cache_leeren(self) -> None:
        self._cache = lage._Zwischenspeicher()

    def _parallel(self, auftraege: dict[str, Callable[[], Any]], rechte: dict[str, str] | None = None) -> dict[str, tuple[Any, str | None]]:
        """Mehrere Quellen gleichzeitig holen; je Quelle (wert, fehler). Ein 403 wird zu „Recht fehlt"."""
        laeufe = {k: self._pool.submit(f) for k, f in auftraege.items()}
        out: dict[str, tuple[Any, str | None]] = {}
        for k, lauf in laeufe.items():
            try:
                out[k] = (lauf.result(), None)
            except KubeFehler as e:
                if e.status == 403:
                    out[k] = (None, recht_fehlt((rechte or {}).get(k) or f"{k} list"))
                else:
                    out[k] = (None, _kurz(str(e), 240))
            except Exception as e:  # noqa: BLE001 — eine Quelle weg heisst nicht „nichts da"
                out[k] = (None, _kurz(f"{type(e).__name__}: {e}", 240))
        return out

    def _abfrage(self, q: str) -> list[dict]:
        if self._prom is not None:
            return self._prom(q)
        return self._cache.holen(f"prom:{q}", 30, lambda: lage.prometheus("/api/v1/query", {"query": q})["result"])

    def _sperre(self, knoten: str | None) -> str | None:
        """lage.sperrgrund (Dev/Test/Debug sperrt, Wartung nicht, im Zweifel gesperrt), 10 s gehalten."""
        if not knoten:
            return None
        return self._cache.holen(f"sperre:{knoten}", 10, lambda: lage.sperrgrund(knoten))

    def _fenster(self, knoten: str) -> tuple[dict | None, str | None]:
        try:
            return self._cache.holen(f"fenster:{knoten}", 10, lambda: lage.fenster_offen(knoten)), None
        except Exception as e:  # noqa: BLE001
            return None, f"Fensterzustand nicht lesbar ({type(e).__name__})"

    def _darf(self, recht: str) -> bool | None:
        """Hat der Hub dieses Recht? None = unbekannt (dann entscheidet der Trockenlauf am API-Server)."""
        verb, ressource, gruppe, unter, ns = RECHTE[recht]
        try:
            return self._cache.holen(
                f"recht:{recht}", 300,
                lambda: self.kube().darf(verb, ressource, gruppe=gruppe, ns=ns, unter=unter),
            )
        except Exception:  # noqa: BLE001
            return None

    def _rechte(self, *namen: str) -> dict[str, bool | None]:
        return {n: self._darf(n) for n in namen}

    def _wer(self) -> str:
        try:
            from mcp_hub.registry import AUFRUFER

            return AUFRUFER.get()
        except Exception:  # noqa: BLE001
            return "unbekannt"

    def _melden(self, werkzeug: str, ziel: str, grund: str, ergebnis: str, knoten: list[str] | None = None) -> None:
        """Ausgefuehrte Aktion als Bus-Ereignis (fyos:events:steuerpult.aktion). Der Bus darf die
        Aktion nie verhindern — darum still bei Fehlern; das Audit kommt ohnehin ueber den Hook."""
        daten = {"werkzeug": werkzeug, "ziel": ziel, "grund": grund, "ergebnis": ergebnis,
                 "knoten": knoten or [], "wer": self._wer()}
        try:
            r = self._bus if self._bus is not None else lage._redis()
            r.xadd(STROM, {"data": json.dumps(daten, ensure_ascii=False), "source": "mcp-hub/cluster",
                           "ts": str(int(time.time()))}, maxlen=1000, approximate=True)
        except Exception:  # noqa: BLE001
            pass

    def _lage_plugin(self) -> Any:
        if self._lage_p is None:
            self._lage_p = lage.LagePlugin()
        return self._lage_p

    @staticmethod
    def _rs_zu_dep(replicasets: list[dict]) -> dict[tuple[str, str], str]:
        out = {}
        for rs in replicasets or []:
            for ref in _g(rs, "metadata", "ownerReferences", vorgabe=[]):
                if ref.get("kind") == "Deployment":
                    out[(_ns(rs), _name(rs))] = str(ref.get("name"))
        return out

    # ── Werkzeuge ──────────────────────────────────────────────────────────────────────────
    def describe_tools(self) -> list[ToolDescriptor]:
        leer = {"type": "object", "properties": {}, "additionalProperties": False}
        grund = {"type": "string", "maxLength": GRUND_MAX, "description": "Pflicht beim Ausführen: warum"}
        ausfuehren = {"type": "boolean", "default": False, "description": "false = Trockenlauf (Standard)"}
        return [
            ToolDescriptor(
                name="knoten",
                title="Steuerpult — Knoten",
                description=(
                    "Steuerpult (App): ein Satz, dazu je Knoten Rollen, Kapazität (CPU/RAM/GPU), Anforderungen in %, "
                    "Auslastung jetzt (Prometheus, auch GPU), Boot, Pods (Zahl), abgesperrt, offenes Fenster und "
                    "Sperrgrund — knapp, unter 10 KB. Mit voll: true zusätzlich Labels, Taints, Bedingungen, "
                    "Summe der Requests/Limits, OS/Kernel/Kubelet, k3s-Startargumente, Pod-Liste und die Spuren der "
                    "letzten 6 h (rund 70 KB; die Oberfläche nutzt das). mit_kubelet (nur mit knoten): die Kubelet-"
                    "Konfiguration — auf Knoten mit Dev/Test-Fenster gesperrt."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "knoten": {"type": "string", "description": "nur dieser Knoten (Name wie in der Knotenliste)"},
                        "mit_kubelet": {"type": "boolean", "default": False},
                        "voll": {"type": "boolean", "default": False,
                                 "description": "Alle Felder je Knoten (gross; die Oberfläche nutzt das). Ohne: knappe Ansicht"},
                        "spuren": {"type": "boolean", "default": True,
                                   "description": "nur mit voll: je Knoten die letzten 6 h aus dem Verlauf (CPU, RAM, Platte, GPU)"},
                    },
                    "additionalProperties": False,
                },
                tags=_LESEN,
                meta=_UI_LESEN,
            ),
            ToolDescriptor(
                name="lage",
                title="Steuerpult — was nicht stimmt",
                description=(
                    "Was im Cluster gerade nicht stimmt, kurz und gruppiert: Pods mit Problemen (CrashLoop, "
                    "ImagePull, nicht einplanbar, nicht bereit, OOM), gescheiterte Jobs (24 h), Flux nicht "
                    "bereit oder ausgesetzt, ausgesetzte CronJobs mit Grund, Knoten, Alarme (Prometheus), "
                    "Zertifikatsablauf. zustand rot/gelb/gruen und ein Satz."
                ),
                input_schema=leer,
                tags=_LESEN,
                meta=_UI_LESEN,
            ),
            ToolDescriptor(
                name="workloads",
                title="Steuerpult — Workloads",
                description=(
                    "Deployments, StatefulSets, DaemonSets und CronJobs: bereit/soll, Neustarts, letzter "
                    "Abbruchgrund, Knoten, warum dort (nodeSelector, Affinität, Toleranzen), ob Flux sie "
                    "verwaltet, CPU/RAM jetzt und welche Aktionen gerade gehen (sonst der Grund)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "ns": {"type": "string", "description": "nur dieser Namespace"},
                        "knoten": {"type": "string", "description": "nur Workloads mit Pods auf diesem Knoten"},
                        "nur_probleme": {"type": "boolean", "default": False},
                    },
                    "additionalProperties": False,
                },
                tags=_LESEN,
                meta=_UI_LESEN,
            ),
            ToolDescriptor(
                name="ereignisse",
                title="Steuerpult — Ereignisse",
                description=(
                    "Kubernetes-Ereignisse der letzten Stunden, gruppiert nach Grund und Objekt-Basisname "
                    "(web-7f9c…-x2kq9 → web). Lärm wie DNSConfigForming wird eingeklappt mitgezählt."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "ns": {"type": "string"},
                        "stunden": {"type": "integer", "minimum": 1, "maximum": 168, "default": 24},
                        "nur_warnungen": {"type": "boolean", "default": True},
                    },
                    "additionalProperties": False,
                },
                tags=_LESEN,
                meta=_UI_LESEN,
            ),
            ToolDescriptor(
                name="logs",
                title="Steuerpult — Logs",
                description=(
                    "Die letzten Zeilen eines Pods oder eines Deployments (dann der auffälligste Pod, die "
                    "anderen stehen in `pods`). vorher=true: der vorige, abgestürzte Lauf. Pods auf einem "
                    "Knoten mit Dev/Test-Fenster: gesperrt (Logs kommen vom Knoten selbst)."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "ns": {"type": "string"},
                        "pod": {"type": "string"},
                        "deploy": {"type": "string"},
                        "container": {"type": "string"},
                        "zeilen": {"type": "integer", "minimum": 1, "maximum": LOG_MAX_ZEILEN, "default": LOG_ZEILEN},
                        "vorher": {"type": "boolean", "default": False},
                    },
                    "required": ["ns"],
                    "additionalProperties": False,
                },
                tags=_LESEN,
                meta=_UI_LESEN,
            ),
            ToolDescriptor(
                name="flux_zustand",
                title="Steuerpult — Flux",
                description=(
                    "Flux: Kustomizations, Git-/Helm-/OCI-Quellen, HelmReleases, ImagePolicies, "
                    "ImageRepositories und ImageUpdateAutomation mit bereit, Revision, ausgesetzt und "
                    "letztem Fehler. Fehlt ein Leserecht, steht es dabei."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "nur_probleme": {"type": "boolean", "default": False},
                        "voll": {"type": "boolean", "default": False,
                                 "description": "alle Objekte jeder Art mit Meldung (sonst: alle Kustomizations, von den übrigen nur Probleme)"},
                    },
                    "additionalProperties": False,
                },
                tags=_LESEN,
                meta=_UI_LESEN,
            ),
            ToolDescriptor(
                name="knoten_raeumen",
                title="Steuerpult — Räumen (nur Probe)",
                description=(
                    "NUR Trockenlauf, führt nie etwas aus: welche Pods beim Räumen eines Knotens umziehen "
                    "würden, welche nicht können (lokale Volumes, feste Bindung, nackte Pods) und was dann "
                    "stehen bliebe."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"knoten": {"type": "string"}},
                    "required": ["knoten"],
                    "additionalProperties": False,
                },
                tags=_LESEN,
                meta=_UI_LESEN,
            ),
            ToolDescriptor(
                name="altlasten",
                title="Steuerpult — Altlasten",
                description=(
                    "Liste für den Betreiber, löscht nie: Released-PVs (mit Größe), nackte Pods ohne Besitzer, Dienste "
                    "ohne Endpunkte — dazu ein Skript mit Trockenlauf, das der Betreiber selbst ausführt."
                ),
                input_schema=leer,
                tags=_LESEN,
                meta=_UI_LESEN,
            ),
            ToolDescriptor(
                name="neustarten",
                title="Steuerpult — neu starten",
                description=(
                    "Startet ein Deployment, StatefulSet oder DaemonSet neu (rollout restart). Ohne "
                    "ausfuehren nur Trockenlauf (Plan, betroffene Knoten, Sperren, Rechte). Ausführen nur mit "
                    "grund; gesperrt, wenn ein Pod auf einem Knoten mit Dev/Test-Fenster läuft; flux-system/"
                    "kube-system nur mit trotzdem."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "ns": {"type": "string"},
                        "art": {"type": "string", "enum": ["deploy", "sts", "ds"]},
                        "name": {"type": "string"},
                        "grund": grund,
                        "ausfuehren": ausfuehren,
                        "trotzdem": {"type": "boolean", "default": False, "description": "Systembereich bestätigt"},
                    },
                    "required": ["ns", "art", "name"],
                    "additionalProperties": False,
                },
                tags=_SCHREIBEN,
                meta=_UI_SCHREIBEN,
            ),
            ToolDescriptor(
                name="skalieren",
                title="Steuerpult — skalieren",
                description=(
                    "Setzt die Replikas eines Deployments. Ohne ausfuehren nur Trockenlauf. Verwaltet Flux "
                    "(oder ein Autoscaler) spec.replicas, wird es beim nächsten Abgleich zurückgesetzt — "
                    "dann nur mit trotzdem; dauerhaft nur per Git-Änderung. 0 Replikas nur mit null_bestaetigt."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "ns": {"type": "string"},
                        "name": {"type": "string"},
                        "replikas": {"type": "integer", "minimum": 0, "maximum": REPLIKAS_MAX},
                        "grund": grund,
                        "ausfuehren": ausfuehren,
                        "trotzdem": {"type": "boolean", "default": False,
                                     "description": "Flux/Autoscaler setzt zurück — trotzdem skalieren"},
                        "null_bestaetigt": {"type": "boolean", "default": False,
                                            "description": "ja, auf 0 — der Dienst ist dann aus"},
                    },
                    "required": ["ns", "name", "replikas"],
                    "additionalProperties": False,
                },
                tags=_SCHREIBEN,
                meta=_UI_SCHREIBEN,
            ),
            ToolDescriptor(
                name="flux_abgleichen",
                title="Steuerpult — Flux abgleichen",
                description=(
                    "Stößt den Flux-Abgleich einer Kustomization an (Annotation reconcile.fluxcd.io/"
                    "requestedAt, wie scripts/ops/ausrollen.py), mit_quelle=true holt vorher die Quelle neu. "
                    "Ohne ausfuehren nur Trockenlauf."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "kustomization": {"type": "string"},
                        "ns": {"type": "string", "default": "flux-system"},
                        "mit_quelle": {"type": "boolean", "default": True},
                        "grund": grund,
                        "ausfuehren": ausfuehren,
                    },
                    "required": ["kustomization"],
                    "additionalProperties": False,
                },
                tags=_SCHREIBEN,
                meta=_UI_SCHREIBEN,
            ),
            ToolDescriptor(
                name="cronjob_jetzt",
                title="Steuerpult — CronJob jetzt starten",
                description=(
                    "Startet einen CronJob sofort einmal (Job aus dem jobTemplate, wie kubectl create job "
                    "--from). Ohne ausfuehren nur Trockenlauf. Gesperrt: ausgesetzte CronJobs, CronJobs, die "
                    "gegen einen Knoten mit Dev/Test-Fenster arbeiten (FYOS_CLUSTER_MESSKNOTEN), laufende bei Forbid. Der "
                    "Job meidet Knoten mit offenem Fenster."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "ns": {"type": "string"},
                        "name": {"type": "string"},
                        "grund": grund,
                        "ausfuehren": ausfuehren,
                    },
                    "required": ["ns", "name"],
                    "additionalProperties": False,
                },
                tags=_SCHREIBEN,
                meta=_UI_SCHREIBEN,
            ),
            ToolDescriptor(
                name="knoten_absperren",
                title="Steuerpult — Knoten absperren",
                description=(
                    "Sperrt einen Knoten für neue Pods (cordon, an=true) oder gibt ihn frei (an=false). "
                    "Laufende Pods bleiben. Steuerebene und FYOS_CLUSTER_NIE_ABSPERREN nie; Knoten mit Dev/Test-"
                    "Fenster gesperrt. Ohne ausfuehren nur Trockenlauf."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "knoten": {"type": "string"},
                        "an": {"type": "boolean"},
                        "grund": grund,
                        "ausfuehren": ausfuehren,
                    },
                    "required": ["knoten", "an"],
                    "additionalProperties": False,
                },
                tags=_SCHREIBEN,
                meta=_UI_SCHREIBEN,
            ),
        ]

    def describe_resources(self) -> list[ResourceDescriptor]:
        return [
            ResourceDescriptor(
                uri=UI_STEUERPULT,
                name="Steuerpult",
                description="k3s-Cluster bedienen: Knoten, was nicht stimmt, Workloads, Flux — mit Trockenlauf und Fenster-Sperre",
                read=lambda: oberflaeche("steuerpult.html", __package__),
                meta={"ui": {"prefersBorder": True}},
            )
        ]

    def call(self, method: str, args: dict[str, Any]) -> Any:
        args = args or {}
        werkzeuge: dict[str, Callable[[dict[str, Any]], Any]] = {
            "knoten": self.knoten, "lage": self.lage, "workloads": self.workloads, "ereignisse": self.ereignisse,
            "logs": self.logs, "flux_zustand": self.flux_zustand, "knoten_raeumen": self.knoten_raeumen,
            "altlasten": self.altlasten, "neustarten": self.neustarten, "skalieren": self.skalieren,
            "flux_abgleichen": self.flux_abgleichen, "cronjob_jetzt": self.cronjob_jetzt,
            "knoten_absperren": self.knoten_absperren,
        }
        if method not in werkzeuge:
            raise ValueError(f"cluster hat kein Werkzeug {method!r}")
        return werkzeuge[method](args)

    # ── cluster.knoten ─────────────────────────────────────────────────────────────────────
    def _knoten_metriken(self) -> dict[str, Any]:
        """Auslastung je Knoten aus Prometheus (node-exporter, dcgm-exporter) — liest nur Prometheus,
        nie den Knoten selbst. Schluessel: Knoten-IP (instance ohne Port); GPUs je Hostname."""
        fs = 'mountpoint="/",fstype!~"tmpfs|overlay"'
        abfragen = {
            "cpu": '100 * (1 - avg by (instance) (rate(node_cpu_seconds_total{mode="idle"}[5m])))',
            "ram": "100 * (1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)",
            "ram_belegt": "node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes",
            "last1": "node_load1",
            "boot": "max by (instance) (node_boot_time_seconds)",
            "boots_14t": "sum by (instance) (changes(node_boot_time_seconds[14d]))",
        }
        je_ip: dict[str, dict[str, Any]] = {}
        for schluessel, q in abfragen.items():
            for r in self._abfrage(q):
                ip = str((r.get("metric") or {}).get("instance") or "").rsplit(":", 1)[0]
                try:
                    je_ip.setdefault(ip, {})[schluessel] = float(r["value"][1])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
        # Platte „/": bei ZFS ist „belegt in Prozent" die Belegung des Datasets, nicht des Pools
        # (ein Knoten mit lokalen Daten zeigte 0 %) — dort zaehlt nur, was frei ist.
        platte: dict[str, dict[str, Any]] = {}
        for schluessel, q in (("frei", f"node_filesystem_avail_bytes{{{fs}}}"), ("gross", f"node_filesystem_size_bytes{{{fs}}}")):
            for r in self._abfrage(q):
                m = r.get("metric") or {}
                ip = str(m.get("instance") or "").rsplit(":", 1)[0]
                try:
                    platte.setdefault(ip, {"fs": m.get("fstype")})[schluessel] = float(r["value"][1])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
        for ip, pl in platte.items():
            ziel = je_ip.setdefault(ip, {})
            if "frei" in pl:
                ziel["platte_frei"] = pl["frei"]
            if pl.get("gross") and "frei" in pl and pl.get("fs") != "zfs":
                ziel["platte"] = 100 * (1 - pl["frei"] / pl["gross"])
            ziel["platte_fs"] = pl.get("fs")
        gpus: dict[str, dict[str, dict[str, Any]]] = {}
        for schluessel, q in (("auslastung_prozent", "DCGM_FI_DEV_GPU_UTIL"), ("vram_mib", "DCGM_FI_DEV_FB_USED"),
                              ("vram_frei_mib", "DCGM_FI_DEV_FB_FREE"),
                              ("temperatur_c", "DCGM_FI_DEV_GPU_TEMP")):
            for r in self._abfrage(q):
                m = r.get("metric") or {}
                wirt = str(m.get("Hostname") or str(m.get("instance") or "").rsplit(":", 1)[0])
                g = gpus.setdefault(wirt, {}).setdefault(str(m.get("gpu")), {"nr": m.get("gpu"), "modell": m.get("modelName")})
                try:
                    g[schluessel] = round(float(r["value"][1]), 1)
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
        return {"je_ip": je_ip, "gpus": {w: sorted(v.values(), key=lambda x: str(x.get("nr"))) for w, v in gpus.items()}}

    def knoten(self, args: dict[str, Any]) -> dict[str, Any]:
        nur = str(args.get("knoten") or "").strip() or None
        mit_kubelet = bool(args.get("mit_kubelet"))
        if mit_kubelet and not nur:
            raise ValueError("mit_kubelet nur zusammen mit knoten — die Kubelet-Konfiguration wird je Knoten gelesen")
        alle = self._liste("nodes")
        namen = sorted(_name(n) for n in alle)
        if nur and nur not in namen:
            raise ValueError(f"Knoten {nur!r} gibt es nicht — vorhanden: {', '.join(namen)}")
        # Ohne `voll` die knappe Ansicht (KI, ~6 KB statt 70): keine Spuren, Labels, Bedingungen, Taints, Pod-Liste, k3s-Argumente.
        voll = bool(args.get("voll"))
        # Die Zentrale fragt ohne Spuren: sie liest den Verlauf selbst, und ihre Frist je Quelle ist kurz.
        mit_spuren = voll and args.get("spuren", True) is not False
        r = self._parallel({
            "pods": lambda: self._liste("pods"),
            "replicasets": lambda: self._liste("replicasets"),
            "metriken": self._knoten_metriken,
            "rechte": lambda: self._rechte("nodes patch", "nodes/proxy get"),
            **({"verlauf": self._verlauf_spuren} if mit_spuren else {}),
        })
        spuren = (r.get("verlauf") or (None, None))[0] or {}
        pods, pods_fehler = r["pods"]
        metriken, prom_fehler = r["metriken"]
        rechte = r["rechte"][0] or {}
        rs_zu_dep = self._rs_zu_dep(r["replicasets"][0] or [])
        je_knoten: dict[str, list[dict]] = {}
        for p in pods or []:
            je_knoten.setdefault(str(_g(p, "spec", "nodeName", vorgabe="")), []).append(p)
        jetzt = time.time()
        fenster_fehler = None
        out = []
        for n in alle:
            if nur and _name(n) != nur:
                continue
            f, ff = self._fenster(_name(n))
            fenster_fehler = fenster_fehler or ff
            eintrag = self._knoten_eintrag(n, je_knoten.get(_name(n), []), metriken or {}, f, rechte,
                                           rs_zu_dep, mit_kubelet, jetzt)
            if _name(n) in spuren:
                eintrag["spuren"] = spuren[_name(n)]
            out.append(eintrag)
        out.sort(key=lambda k: (not k["steuerebene"], -k["kapazitaet"]["cpu"], k["name"]))
        ergebnis = {
            "satz": self._knoten_satz(out),
            "knoten": out if voll else [self._knoten_knapp(k) for k in out],
            "quellen": {
                "kubernetes": {"ok": True},
                "pods": {"ok": pods_fehler is None, "fehler": pods_fehler},
                "prometheus": {"ok": prom_fehler is None, "fehler": prom_fehler},
                "fenster": {"ok": fenster_fehler is None, "fehler": fenster_fehler},
            },
            "ts": _iso(jetzt),
        }
        if not voll:
            ergebnis["knapp"] = True
            ergebnis["mehr"] = ("Alles je Knoten (Labels, Bedingungen, Taints, Pods, Verlauf, k3s-Argumente): "
                                "cluster.knoten {voll: true}; Pods: cluster.workloads; Kubelet: {knoten, mit_kubelet: true}.")
        return ergebnis

    @staticmethod
    def _knoten_satz(knoten: list[dict[str, Any]]) -> str:
        """„4 Knoten, alle bereit · knoten-a im Dev-Fenster (noch 45 h) · CPU 21–36 %.“"""
        if not knoten:
            return "Keine Knoten gemeldet."
        schlecht = [k["name"] for k in knoten if not k["bereit"]]
        teile = [f"{len(knoten)} Knoten, " + ("alle bereit" if not schlecht else f"nicht bereit: {', '.join(schlecht)}")]
        for k in knoten:
            f = k.get("fenster")
            if f:
                rest = f.get("rest_min")
                teile.append(f"{k['name']} im {str(f.get('art') or 'Fenster').capitalize()}-Fenster"
                             + (f" (noch {round(rest / 60)} h)" if isinstance(rest, (int, float)) and rest >= 120 else
                                f" (noch {round(rest)} min)" if isinstance(rest, (int, float)) else ""))
            elif k.get("abgesperrt"):
                teile.append(f"{k['name']} abgesperrt")
        druck = [k["name"] for k in knoten if k.get("probleme") and k["name"] not in schlecht]
        if druck:
            teile.append("Druck auf " + ", ".join(druck))
        cpu = [k["auslastung"]["cpu_prozent"] for k in knoten if isinstance((k.get("auslastung") or {}).get("cpu_prozent"), (int, float))]
        if cpu:
            teile.append(f"CPU {round(min(cpu))}–{round(max(cpu))} %")
        return " · ".join(teile) + "."

    @staticmethod
    def _knoten_knapp(k: dict[str, Any]) -> dict[str, Any]:
        """Eine Zeile je Knoten für die KI: was jetzt zählt, ohne Labels, Taints, Bedingungen, Pod-Liste, Spuren, k3s-Argumente."""
        f = k.get("fenster")
        pods = k.get("pods") or {}
        a = k.get("auslastung") or {}
        return {
            "name": k["name"], "adresse": k.get("adresse"), "bereit": k["bereit"], "steuerebene": k["steuerebene"],
            "rollen": k["rollen"], "aufgaben": k["aufgaben"], "abgesperrt": k["abgesperrt"], "probleme": k["probleme"],
            "kapazitaet": k["kapazitaet"],
            "angefordert": {x: k["angefordert"].get(x) for x in ("cpu_prozent", "ram_prozent", "gpu")},
            "auslastung": {x: a.get(x) for x in ("cpu_prozent", "ram_prozent", "ram_gib", "platte_prozent", "platte_frei_gib")}
            | {"gpus": a.get("gpus") or []},
            "boot": {"laeuft_h": (k.get("boot") or {}).get("laeuft_h"), "boots_14t": (k.get("boot") or {}).get("boots_14t")},
            "pods": {"anzahl": pods.get("anzahl"), "max": pods.get("max"), "nicht_bereit": pods.get("nicht_bereit")},
            "fenster": ({x: f.get(x) for x in ("art", "rest_min", "bis")} | {"grund": _kurz(f.get("grund"), 100)}) if f else None,
            "sperrgrund": _kurz(k.get("sperrgrund"), 120) if k.get("sperrgrund") else None,
            **({"kubelet": k["kubelet"]} if "kubelet" in k else {}),
        }

    def _knoten_eintrag(self, n: dict, pods: list[dict], metriken: dict, fenster: dict | None,
                        rechte: dict[str, bool | None], rs_zu_dep: dict, mit_kubelet: bool, jetzt: float) -> dict[str, Any]:
        name = _name(n)
        lab = _labels(n)
        st = n.get("status") or {}
        ip = next((a.get("address") for a in st.get("addresses") or [] if a.get("type") == "InternalIP"), None)
        rollen = sorted({k.split("/", 1)[1] for k in lab if k.startswith("node-role.kubernetes.io/")})
        aufgaben = sorted({k.split("role-", 1)[1] for k, v in lab.items() if k.startswith("fyos.node/role-") and v == "true"})
        steuerebene = bool({"control-plane", "master"} & set(rollen))
        kap, verg = st.get("capacity") or {}, st.get("allocatable") or {}
        laufend = [p for p in pods if _g(p, "status", "phase") not in ("Succeeded", "Failed")]
        req = {k: 0.0 for k in _RESSOURCEN}
        lim = {k: 0.0 for k in _RESSOURCEN}
        for p in laufend:
            a, b = pod_summen(p)
            for k in _RESSOURCEN:
                req[k] += a[k]
                lim[k] += b[k]
        cpu_v, ram_v = menge(verg.get("cpu")), menge(verg.get("memory"))

        def anteil(x: float, gesamt: float) -> float | None:
            return round(100 * x / gesamt, 1) if gesamt else None

        bedingungen = [{"typ": c.get("type"), "status": c.get("status"), "grund": c.get("reason"),
                        "seit": c.get("lastTransitionTime"), "meldung": _kurz(c.get("message"), 160)}
                       for c in st.get("conditions") or []]
        probleme = []
        druck = {"MemoryPressure": "Speicherdruck", "DiskPressure": "Plattendruck", "PIDPressure": "Prozessdruck",
                 "NetworkUnavailable": "Netz fehlt"}
        for c in bedingungen:
            if c["typ"] == "Ready" and c["status"] != "True":
                probleme.append(f"nicht bereit ({c['grund'] or c['status']})")
            elif c["typ"] in druck and c["status"] == "True":
                probleme.append(druck[c["typ"]])
        abgesperrt = bool(_g(n, "spec", "unschedulable"))
        m = (metriken.get("je_ip") or {}).get(ip or "", {})
        gpus = (metriken.get("gpus") or {}).get(name) or (metriken.get("gpus") or {}).get(ip or "") or []
        info = st.get("nodeInfo") or {}
        try:
            k3s_args = schwaerzen(json.loads(_annot(n).get("k3s.io/node-args") or "[]"))
        except (TypeError, ValueError):
            k3s_args = []
        sperre = self._sperre(name)
        bereit_seit = next((c["seit"] for c in bedingungen if c["typ"] == "Ready"), None)
        boot = m.get("boot")
        pods_liste = []
        for p in sorted(pods, key=lambda p: (_ns(p), _name(p))):
            b = besitzer(p, rs_zu_dep)
            pods_liste.append({"ns": _ns(p), "name": _name(p), "phase": _g(p, "status", "phase"),
                               "bereit": pod_bereit(p), "neustarts": pod_neustarts(p),
                               "workload": f"{b[0]}/{b[1]}" if b else None})
        eintrag: dict[str, Any] = {
            "name": name,
            "adresse": ip,
            "bereit": not any(p.startswith("nicht bereit") for p in probleme)
            and any(c["typ"] == "Ready" and c["status"] == "True" for c in bedingungen),
            "bereit_seit": bereit_seit,
            "steuerebene": steuerebene,
            "rollen": rollen,
            "aufgaben": aufgaben,
            "stufe": lab.get("fyos-tier"),
            "abgesperrt": abgesperrt,
            "probleme": probleme,
            "bedingungen": bedingungen,
            "labels": lab,
            "taints": [{"key": t.get("key"), "wert": t.get("value"), "wirkung": t.get("effect")} for t in _g(n, "spec", "taints", vorgabe=[])],
            "kapazitaet": {"cpu": menge(kap.get("cpu")), "ram_gib": round(menge(kap.get("memory")) / 2**30, 1),
                           "gpu": int(menge(kap.get("nvidia.com/gpu"))), "pods": int(menge(kap.get("pods"))),
                           "platte_gib": round(menge(kap.get("ephemeral-storage")) / 2**30, 1)},
            "vergebbar": {"cpu": cpu_v, "ram_gib": round(ram_v / 2**30, 1), "gpu": int(menge(verg.get("nvidia.com/gpu"))),
                          "pods": int(menge(verg.get("pods")))},
            "angefordert": {"cpu": round(req["cpu"], 2), "cpu_prozent": anteil(req["cpu"], cpu_v),
                            "ram_gib": round(req["memory"] / 2**30, 2), "ram_prozent": anteil(req["memory"], ram_v),
                            "gpu": int(req["nvidia.com/gpu"])},
            "limits": {"cpu": round(lim["cpu"], 2), "cpu_prozent": anteil(lim["cpu"], cpu_v),
                       "ram_gib": round(lim["memory"] / 2**30, 2), "ram_prozent": anteil(lim["memory"], ram_v)},
            "auslastung": {
                "cpu_prozent": round(m["cpu"], 1) if "cpu" in m else None,
                "ram_prozent": round(m["ram"], 1) if "ram" in m else None,
                "ram_gib": round(m["ram_belegt"] / 2**30, 1) if "ram_belegt" in m else None,
                "last1": round(m["last1"], 2) if "last1" in m else None,
                "platte_prozent": round(m["platte"], 1) if "platte" in m else None,
                "platte_frei_gib": round(m["platte_frei"] / 2**30, 1) if "platte_frei" in m else None,
                "platte_fs": m.get("platte_fs"),
                "gpus": gpus,
            },
            "system": {"os": info.get("osImage"), "kernel": info.get("kernelVersion"), "kubelet": info.get("kubeletVersion"),
                       "laufzeit": info.get("containerRuntimeVersion"), "arch": info.get("architecture"),
                       "swap_gib": round(menge(_g(info, "swap", "capacity")) / 2**30, 1) if _g(info, "swap", "capacity") else 0,
                       "k3s_args": k3s_args},
            "boot": {"zuletzt": _iso(boot) if boot else None, "boots_14t": int(m["boots_14t"]) if "boots_14t" in m else None,
                     "laeuft_h": round((jetzt - boot) / 3600, 1) if boot else None},
            "pods": {"anzahl": len(laufend), "max": int(menge(verg.get("pods"))),
                     "nicht_bereit": sum(1 for p in laufend if not pod_bereit(p) and _g(p, "status", "phase") == "Running"),
                     "liste": pods_liste},
            "fenster": fenster,
            "sperrgrund": sperre,
            "aktionen": self._knoten_aktionen(name, steuerebene, abgesperrt, sperre, rechte),
        }
        if mit_kubelet:
            eintrag["kubelet"] = self._kubelet(name, sperre, rechte.get("nodes/proxy get"))
        return eintrag

    def _knoten_aktionen(self, name: str, steuerebene: bool, abgesperrt: bool, sperre: str | None,
                         rechte: dict[str, bool | None]) -> dict[str, Any]:
        an = not abgesperrt  # die naheliegende Aktion: absperren, wenn frei — freigeben, wenn abgesperrt
        grund = None
        if an and (steuerebene or name in nie_absperren()):
            grund = f"{name} wird nie abgesperrt (Steuerebene, lokale Daten)"
        elif sperre:
            grund = sperre
        elif rechte.get("nodes patch") is False:
            grund = recht_fehlt("nodes patch")
        return {
            "absperren": {"an": an, "moeglich": grund is None, "grund": grund},
            "raeumen_probe": {"moeglich": True, "grund": None},
            "kubelet": {"moeglich": not sperre and rechte.get("nodes/proxy get") is not False,
                        "grund": sperre or (recht_fehlt("nodes/proxy get") if rechte.get("nodes/proxy get") is False else None)},
        }

    def _kubelet(self, name: str, sperre: str | None, recht: bool | None) -> dict[str, Any]:
        if sperre:
            return {"gesperrt": sperre}
        if recht is False:
            return {"recht_fehlt": recht_fehlt("nodes/proxy get")}
        try:
            cfg = (self.kube().kubelet_konfig(name) or {}).get("kubeletconfig") or {}
        except KubeFehler as e:
            if e.status == 403:
                return {"recht_fehlt": recht_fehlt("nodes/proxy get")}
            return {"fehler": _kurz(str(e), 240)}
        wichtig = ("maxPods", "evictionHard", "evictionSoft", "systemReserved", "kubeReserved", "cgroupDriver",
                   "imageGCHighThresholdPercent", "imageGCLowThresholdPercent", "containerLogMaxSize",
                   "containerLogMaxFiles", "resolvConf", "clusterDNS", "serializeImagePulls", "maxParallelImagePulls",
                   "shutdownGracePeriod", "shutdownGracePeriodCriticalPods", "cpuManagerPolicy", "memoryManagerPolicy",
                   "topologyManagerPolicy", "featureGates", "failSwapOn", "memorySwap")
        return {"auszug": {k: cfg[k] for k in wichtig if k in cfg}, "alles": cfg}

    # ── cluster.workloads ──────────────────────────────────────────────────────────────────
    def _pod_nutzung(self) -> dict[tuple[str, str], dict[str, float]]:
        """CPU (Kerne) und RAM (Bytes) je Pod aus Prometheus (cAdvisor)."""
        out: dict[tuple[str, str], dict[str, float]] = {}
        for schluessel, q in (
            ("cpu", 'sum by (namespace, pod) (rate(container_cpu_usage_seconds_total{container!="",container!="POD"}[5m]))'),
            ("ram", 'sum by (namespace, pod) (container_memory_working_set_bytes{container!="",container!="POD"})'),
        ):
            for r in self._abfrage(q):
                m = r.get("metric") or {}
                try:
                    out.setdefault((str(m.get("namespace")), str(m.get("pod"))), {})[schluessel] = float(r["value"][1])
                except (KeyError, IndexError, TypeError, ValueError):
                    continue
        return out

    def _sperren(self, knoten: list[dict]) -> dict[str, str | None]:
        return {_name(k): self._sperre(_name(k)) for k in knoten}

    def workloads(self, args: dict[str, Any]) -> dict[str, Any]:
        ns_filter = str(args.get("ns") or "").strip() or None
        knoten_filter = str(args.get("knoten") or "").strip() or None
        nur_probleme = bool(args.get("nur_probleme"))
        arten = ("deployments", "statefulsets", "daemonsets", "replicasets", "pods", "cronjobs", "jobs", "nodes")
        r = self._parallel({**{a: (lambda a=a: self._liste(a)) for a in arten},
                            "nutzung": self._pod_nutzung,
                            "rechte": lambda: self._rechte(*_RECHT_ZUM_NEUSTART.values(), "deployments/scale patch",
                                                           "jobs create")})
        for a in ("deployments", "pods", "nodes"):
            if r[a][1]:
                raise RuntimeError(f"{a} nicht lesbar: {r[a][1]}")
        knoten = r["nodes"][0]
        if knoten_filter and knoten_filter not in {_name(k) for k in knoten}:
            raise ValueError(f"Knoten {knoten_filter!r} gibt es nicht — vorhanden: {', '.join(sorted(_name(k) for k in knoten))}")
        sperren = self._sperren(knoten)
        rechte = r["rechte"][0] or {}
        nutzung = r["nutzung"][0] or {}
        rs_zu_dep = self._rs_zu_dep(r["replicasets"][0] or [])
        je: dict[tuple[str, str, str], list[dict]] = {}
        for p in r["pods"][0]:
            b = besitzer(p, rs_zu_dep)
            if b:
                je.setdefault((b[0], _ns(p), b[1]), []).append(p)
        jetzt = time.time()
        liste = []
        for art, quelle in (("deploy", "deployments"), ("sts", "statefulsets"), ("ds", "daemonsets")):
            for o in r[quelle][0] or []:
                if ns_filter and _ns(o) != ns_filter:
                    continue
                pods = je.get((art, _ns(o), _name(o)), [])
                e = self._workload_eintrag(art, o, pods, nutzung, sperren, rechte, knoten, jetzt)
                if knoten_filter and knoten_filter not in e["knoten"]:
                    continue
                if nur_probleme and not e["probleme"]:
                    continue
                liste.append(e)
        liste.sort(key=lambda e: (not e["probleme"], e["ns"], e["name"]))
        cronjobs = []
        jobs_je_cj: dict[tuple[str, str], list[dict]] = {}
        for j in r["jobs"][0] or []:
            ref = next((x for x in _g(j, "metadata", "ownerReferences", vorgabe=[]) if x.get("kind") == "CronJob"), None)
            if ref:
                jobs_je_cj.setdefault((_ns(j), str(ref.get("name"))), []).append(j)
        for cj in r["cronjobs"][0] or []:
            if ns_filter and _ns(cj) != ns_filter:
                continue
            e = self._cronjob_eintrag(cj, jobs_je_cj.get((_ns(cj), _name(cj)), []), sperren, rechte, knoten, jetzt)
            if knoten_filter and knoten_filter not in e["knoten"]:
                continue
            if nur_probleme and e["zustand"] == "ok" and not e["ausgesetzt"]:
                continue
            cronjobs.append(e)
        cronjobs.sort(key=lambda e: (e["zustand"] == "ok", e["ns"], e["name"]))
        return {
            "workloads": liste,
            "cronjobs": cronjobs,
            "zaehlung": {"workloads": len(liste), "mit_problem": sum(1 for e in liste if e["probleme"]),
                         "cronjobs": len(cronjobs), "cronjobs_rot": sum(1 for c in cronjobs if c["zustand"] == "rot")},
            "sperren": {k: v for k, v in sperren.items() if v},
            "filter": {"ns": ns_filter, "knoten": knoten_filter, "nur_probleme": nur_probleme},
            "quellen": {k: {"ok": v[1] is None, "fehler": v[1]} for k, v in r.items()},
            "ts": _iso(jetzt),
        }

    def _workload_eintrag(self, art: str, o: dict, pods: list[dict], nutzung: dict, sperren: dict[str, str | None],
                          rechte: dict[str, bool | None], alle_knoten: list[dict], jetzt: float) -> dict[str, Any]:
        spec, st = o.get("spec") or {}, o.get("status") or {}
        if art == "ds":
            soll, bereit = int(st.get("desiredNumberScheduled") or 0), int(st.get("numberReady") or 0)
        else:
            soll = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
            bereit = int(st.get("readyReplicas") or 0)
        tpl = _g(spec, "template", "spec", vorgabe={})
        knoten = sorted({str(_g(p, "spec", "nodeName")) for p in pods if _g(p, "spec", "nodeName")})
        probleme = []
        if bereit < soll:
            probleme.append(f"{bereit} von {soll} bereit")
        for p in pods:
            pr = pod_problem(p, jetzt)
            if pr:
                probleme.append(f"{_name(p)}: {pr[1]}")
        cpu = sum((nutzung.get((_ns(p), _name(p))) or {}).get("cpu", 0.0) for p in pods)
        ram = sum((nutzung.get((_ns(p), _name(p))) or {}).get("ram", 0.0) for p in pods)
        gesperrt = next((sperren[k] for k in knoten if sperren.get(k)), None)
        aktionen: dict[str, Any] = {}
        recht = rechte.get(_RECHT_ZUM_NEUSTART[art])
        if gesperrt:
            aktionen["neustarten"] = {"moeglich": False, "grund": gesperrt}
        elif recht is False:
            aktionen["neustarten"] = {"moeglich": False, "grund": recht_fehlt(_RECHT_ZUM_NEUSTART[art])}
        else:
            aktionen["neustarten"] = {"moeglich": True, "grund": None,
                                      "hinweis": "Systembereich — nur mit trotzdem" if _ns(o) in SYSTEM_NS else None}
        verwalter = replikas_verwalter(o) if art == "deploy" else None
        if art == "deploy":
            if gesperrt:
                aktionen["skalieren"] = {"moeglich": False, "grund": gesperrt}
            elif rechte.get("deployments/scale patch") is False:
                aktionen["skalieren"] = {"moeglich": False, "grund": recht_fehlt("deployments/scale patch")}
            else:
                hinweis = None
                if verwalter and verwalter["flux"]:
                    hinweis = "Flux setzt die Replikas beim nächsten Abgleich zurück — dauerhaft nur per Git"
                elif verwalter and verwalter["autoskalierer"]:
                    hinweis = "ein Autoscaler (HPA) steuert die Replikas"
                aktionen["skalieren"] = {"moeglich": True, "grund": None, "hinweis": hinweis}
        frei = [p for p in pods if not sperren.get(str(_g(p, "spec", "nodeName", vorgabe="")))]
        aktionen["logs"] = {"moeglich": bool(frei) or not pods,
                            "grund": None if frei or not pods else gesperrt}
        return {
            "art": art,
            "ns": _ns(o),
            "name": _name(o),
            "bereit": bereit,
            "soll": soll,
            "neustarts": sum(pod_neustarts(p) for p in pods),
            "letzter_abbruch": letzter_abbruch(pods),
            "probleme": probleme[:6],
            "knoten": knoten,
            "pods": [{"name": _name(p), "knoten": _g(p, "spec", "nodeName"), "phase": _g(p, "status", "phase"),
                      "bereit": pod_bereit(p), "neustarts": pod_neustarts(p),
                      "gesperrt": sperren.get(str(_g(p, "spec", "nodeName", vorgabe=""))) or None}
                     for p in sorted(pods, key=_name)],
            "warum_hier": ["DaemonSet: ein Pod je passendem Knoten"] + warum_hier(tpl) if art == "ds" else warum_hier(tpl),
            "moegliche_knoten": moegliche_knoten(tpl, alle_knoten, abgesperrte_zaehlen=art == "ds"),
            "bilder": [_bild_kurz(str(c.get("image") or "")) for c in tpl.get("containers") or []],
            "flux": flux_verwaltet(o),
            "replikas_von": ("flux" if verwalter["flux"] else "autoscaler" if verwalter["autoskalierer"] else "hand")
            if verwalter else None,
            "nutzung": {"cpu": round(cpu, 3), "ram_mib": round(ram / 2**20)} if nutzung else None,
            "aktionen": aktionen,
        }

    def _cronjob_eintrag(self, cj: dict, jobs: list[dict], sperren: dict[str, str | None],
                         rechte: dict[str, bool | None], alle_knoten: list[dict], jetzt: float) -> dict[str, Any]:
        spec, st = cj.get("spec") or {}, cj.get("status") or {}
        tpl = _g(spec, "jobTemplate", "spec", "template", "spec", vorgabe={})
        aus = bool(spec.get("suspend"))
        grund_aus = None
        if aus:
            a = _annot(cj)
            grund_aus = a.get("fyos.io/ausgesetzt-grund") or a.get("fyos/ausgesetzt-grund") or suspend_grund_aus_repo(cj, self.repo)
            if a.get("fyos.io/ausgesetzt-bis"):
                grund_aus = f"{grund_aus or 'ausgesetzt'} (bis {a['fyos.io/ausgesetzt-bis']})"
        fehler = []
        for j in jobs:
            c = _bedingung(j, "Failed")
            if c.get("status") == "True":
                fehler.append((_ts(c.get("lastTransitionTime")) or 0, c.get("reason") or "Failed", _name(j)))
        fehler.sort(reverse=True)
        erfolg = _ts(st.get("lastSuccessfulTime"))
        rot = bool(fehler) and (erfolg is None or erfolg < fehler[0][0])
        moegl = moegliche_knoten(tpl, alle_knoten)
        bezug = messknoten_bezug(cj)
        gesperrte = [k for k in moegl if sperren.get(k)]
        grund = None
        if aus:
            grund = "ausgesetzt — von Hand starten würde die Aussetzung umgehen"
        elif bezug and sperren.get(messknoten()):
            grund = f"arbeitet gegen {messknoten()} ({bezug[0]}) — {sperren[messknoten()]}"
        elif moegl and len(gesperrte) == len(moegl):
            grund = sperren[gesperrte[0]]
        elif not moegl:
            grund = "kein Knoten passt zur Vorlage (nodeSelector/Taints)"
        elif rechte.get("jobs create") is False:
            grund = recht_fehlt("jobs create")
        elif st.get("active") and spec.get("concurrencyPolicy") == "Forbid":
            grund = "läuft gerade — der CronJob verbietet parallele Läufe"
        gebunden = moegl if len(moegl) == 1 else []
        return {
            "ns": _ns(cj),
            "name": _name(cj),
            "zeitplan": spec.get("schedule"),
            "ausgesetzt": aus,
            "grund_ausgesetzt": grund_aus,
            "zuletzt_geplant": st.get("lastScheduleTime"),
            "zuletzt_erfolgreich": st.get("lastSuccessfulTime"),
            "aktiv": len(st.get("active") or []),
            "fehlschlaege_24h": sum(1 for f in fehler if jetzt - f[0] < 86400),
            "letzter_fehler": {"wann": _iso(fehler[0][0]), "grund": fehler[0][1], "job": fehler[0][2]} if fehler else None,
            "zustand": "rot" if rot else "ok",
            "knoten": gebunden,
            "messknoten": messknoten() or None,
            "messknoten_bezug": bezug,
            "aktionen": {"jetzt": {"moeglich": grund is None, "grund": grund,
                                   "hinweis": f"läuft nicht auf {', '.join(gesperrte)} (Fenster)" if gesperrte and grund is None else None}},
        }

    # ── cluster.ereignisse ─────────────────────────────────────────────────────────────────
    def ereignisse(self, args: dict[str, Any]) -> dict[str, Any]:
        ns = str(args.get("ns") or "").strip() or None
        stunden = max(1, min(int(args.get("stunden") or 24), 168))
        nur_warn = args.get("nur_warnungen", True) is not False
        feld = "type=Warning" if nur_warn else None
        items = self._cache.holen(f"ereignisse:{ns}:{feld}", 30, lambda: self.kube().liste("events", ns, feld=feld))
        grenze = time.time() - stunden * 3600
        gruppen: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        laerm: dict[str, dict[str, Any]] = {}
        for e in items:
            t = ereignis_zeit(e)
            if t is None or t < grenze:
                continue
            grund = str(e.get("reason") or "?")
            obj = e.get("involvedObject") or {}
            art, name = str(obj.get("kind") or "?"), str(obj.get("name") or "?")
            ens = "" if art == "Node" else str(obj.get("namespace") or _ns(e))
            anzahl = ereignis_anzahl(e)
            if grund in LAERM or (not nur_warn and grund in LAERM_NORMAL):
                lr = laerm.setdefault(grund, {"grund": grund, "anzahl": 0, "objekte": set()})
                lr["anzahl"] += anzahl
                lr["objekte"].add(f"{ens}/{name}")
                continue
            g = gruppen.setdefault((grund, art, ens, basisname(art, name)), {
                "grund": grund, "typ": e.get("type"), "art": art, "ns": ens, "objekt": basisname(art, name),
                "anzahl": 0, "objekte": set(), "zuletzt": t, "erstmals": t, "meldung": "", "knoten": set()})
            g["anzahl"] += anzahl
            g["objekte"].add(name)
            wirt = _g(e, "source", "host") or e.get("reportingInstance")
            if wirt:
                g["knoten"].add(str(wirt))
            if t >= g["zuletzt"] or not g["meldung"]:
                g["meldung"] = _kurz(e.get("message"), 240)
            g["zuletzt"] = max(g["zuletzt"], t)
            g["erstmals"] = min(g["erstmals"], t)
        liste = sorted(gruppen.values(), key=lambda g: -g["zuletzt"])
        for g in liste:
            g["objekte"] = len(g["objekte"])
            g["knoten"] = sorted(g["knoten"])
            g["zuletzt"], g["erstmals"] = _iso(g["zuletzt"]), _iso(g["erstmals"])
        return {
            "stunden": stunden,
            "nur_warnungen": nur_warn,
            "ns": ns,
            "gruppen": liste[:200],
            "gruppen_gesamt": len(liste),
            "laerm": [{"grund": lr["grund"], "anzahl": lr["anzahl"], "objekte": len(lr["objekte"])} for lr in laerm.values()],
            "ereignisse_gesamt": sum(g["anzahl"] for g in liste),
            "ts": _iso(time.time()),
        }

    # ── cluster.logs ───────────────────────────────────────────────────────────────────────
    def logs(self, args: dict[str, Any]) -> dict[str, Any]:
        ns = str(args.get("ns") or "").strip()
        pod_name = str(args.get("pod") or "").strip()
        deploy = str(args.get("deploy") or "").strip()
        if not ns:
            raise ValueError("ns fehlt")
        if bool(pod_name) == bool(deploy):
            raise ValueError("genau eins von pod oder deploy angeben")
        try:
            zeilen = max(1, min(int(args.get("zeilen") or LOG_ZEILEN), LOG_MAX_ZEILEN))
        except (TypeError, ValueError) as e:
            raise ValueError("zeilen muss eine Zahl sein") from e
        vorher = bool(args.get("vorher"))
        andere: list[dict[str, Any]] = []
        if deploy:
            dep = self._lesen("deployments", deploy, ns)
            kandidaten = self.kube().liste("pods", ns, selektor=_selektor(_g(dep, "spec", "selector")))
            if not kandidaten:
                return {"ns": ns, "deploy": deploy, "pod": None, "text": None, "pods": [],
                        "hinweis": "keine Pods — das Deployment steht auf 0 oder startet gerade"}
            # Der auffaelligste Pod zuerst: nicht bereit, dann meiste Neustarts, dann der juengste.
            kandidaten.sort(key=lambda p: (pod_bereit(p), -pod_neustarts(p), -(_ts(_g(p, "status", "startTime")) or 0)))
            pod = kandidaten[0]
            andere = [{"name": _name(p), "knoten": _g(p, "spec", "nodeName"), "bereit": pod_bereit(p),
                       "neustarts": pod_neustarts(p), "gesperrt": self._sperre(_g(p, "spec", "nodeName"))} for p in kandidaten]
            if self._sperre(_g(pod, "spec", "nodeName")):
                frei = [p for p in kandidaten if not self._sperre(_g(p, "spec", "nodeName"))]
                pod = frei[0] if frei else pod
        else:
            pod = self._lesen("pods", pod_name, ns)
        knoten = _g(pod, "spec", "nodeName")
        namen = [c.get("name") for c in _g(pod, "spec", "containers", vorgabe=[])]
        init = [c.get("name") for c in _g(pod, "spec", "initContainers", vorgabe=[])]
        container = str(args.get("container") or "").strip() or (namen[0] if namen else None)
        if container and container not in namen + init:
            raise ValueError(f"Container {container!r} gibt es in {_name(pod)} nicht — vorhanden: {', '.join(namen + init)}")
        out: dict[str, Any] = {"ns": ns, "pod": _name(pod), "deploy": deploy or None, "knoten": knoten,
                               "container": container, "container_liste": namen, "init_container": init,
                               "zeilen": zeilen, "vorher": vorher, "pods": andere, "text": None, "gekuerzt": False}
        sperre = self._sperre(knoten)
        if sperre:
            # Logs liefert das Kubelet auf dem Knoten selbst — im Fenster bleibt der Knoten in Ruhe.
            return {**out, "gesperrt": sperre}
        try:
            text = self.kube().logs(_name(pod), ns, container=container, zeilen=zeilen, vorher=vorher)
        except KubeFehler as e:
            if e.status == 400 and vorher:
                return {**out, "hinweis": "kein voriger Lauf dieses Containers"}
            if e.status == 403:
                return {**out, "recht_fehlt": recht_fehlt("pods/log get")}
            raise
        out["text"] = text
        out["gekuerzt"] = len(text.encode("utf-8", "replace")) >= LOG_MAX_BYTES
        out["zeilen_geliefert"] = len(text.splitlines())
        return out

    def _lesen(self, art: str, name: str, ns: str | None = None) -> dict:
        try:
            return self.kube().lesen(art, name, ns)
        except KubeFehler as e:
            if e.status == 404:
                ort = f"{ns}/{name}" if ns else name
                raise ValueError(f"{art[:-1] if art.endswith('s') else art} {ort} gibt es nicht") from e
            raise

    # ── cluster.flux_zustand ───────────────────────────────────────────────────────────────
    @staticmethod
    def _flux_eintrag(art: str, o: dict) -> dict[str, Any]:
        bereit = _bedingung(o, "Ready")
        st, spec = o.get("status") or {}, o.get("spec") or {}
        revision = None
        if art == "kustomizations":
            revision = st.get("lastAppliedRevision")
        elif art in ("gitrepositories", "helmrepositories", "ocirepositories"):
            revision = _g(st, "artifact", "revision")
        elif art == "helmreleases":
            revision = st.get("lastAttemptedRevision") or _g((st.get("history") or [{}])[0], "chartVersion")
        elif art == "imagepolicies":
            ref = st.get("latestRef") or {}
            revision = (f"{ref.get('tag') or ''}{'@' + str(ref['digest'])[7:19] if ref.get('digest') else ''}"
                        or st.get("latestImage"))
        elif art == "imagerepositories":
            scan = st.get("lastScanResult") or {}
            revision = f"{scan.get('tagCount')} Tags, gescannt {scan.get('scanTime')}" if scan else None
        elif art == "imageupdateautomations":
            revision = st.get("lastPushCommit") or st.get("lastAutomationRunTime")
        ref = spec.get("sourceRef") or {}
        return {
            "art": art,
            "ns": _ns(o),
            "name": _name(o),
            "bereit": bereit.get("status") or "Unknown",
            "grund": bereit.get("reason"),
            "meldung": _kurz(bereit.get("message"), 300),
            "seit": bereit.get("lastTransitionTime"),
            "revision": _kurz(revision, 120) if revision else None,
            "ausgesetzt": bool(spec.get("suspend")),
            "intervall": spec.get("interval"),
            "angestossen": st.get("lastHandledReconcileAt"),
            "quelle": f"{ref.get('kind')}/{ref.get('namespace') or _ns(o)}/{ref.get('name')}" if ref else None,
            "pfad": spec.get("path"),
        }

    def flux_zustand(self, args: dict[str, Any], parallel: bool = True) -> dict[str, Any]:
        nur_probleme = bool(args.get("nur_probleme"))
        voll = bool(args.get("voll"))
        auftraege = {a: (lambda a=a: self._liste(a, 20)) for a in _CRD}
        if parallel:
            r = self._parallel(auftraege, rechte={a: f"{a} list" for a in _CRD})
        else:
            # Aus cluster.lage heraus: dort laeuft das schon in einem Faden des Pools — nicht verschachteln.
            r = {}
            for a, f in auftraege.items():
                try:
                    r[a] = (f(), None)
                except KubeFehler as e:
                    r[a] = (None, recht_fehlt(f"{a} list") if e.status == 403 else _kurz(str(e), 240))
                except Exception as e:  # noqa: BLE001
                    r[a] = (None, _kurz(f"{type(e).__name__}: {e}", 240))
        arten: dict[str, Any] = {}
        probleme: list[dict[str, Any]] = []
        for art in _CRD:
            items, fehler = r[art]
            if fehler:
                arten[art] = {"ok": False, "fehler": fehler, "recht_fehlt": fehler.startswith("Recht fehlt")}
                continue
            eintraege = [self._flux_eintrag(art, o) for o in items or []]
            schlecht = [e for e in eintraege if e["bereit"] != "True" or e["ausgesetzt"]]
            probleme.extend(schlecht)
            # Knapp bleiben (86 ImageRepositories …): ohne `voll` alle Kustomizations (die braucht
            # das Abgleichen), sonst nur, was nicht stimmt. Bei Bereiten fehlt die Routine-Meldung.
            if nur_probleme or (art != "kustomizations" and not voll):
                zeigen = schlecht
            else:
                zeigen = sorted(eintraege, key=lambda e: (e["bereit"] == "True", e["ns"], e["name"]))
            if not voll:
                zeigen = [{**e, "meldung": None} if e["bereit"] == "True" and not e["ausgesetzt"] else e for e in zeigen]
            arten[art] = {
                "ok": True,
                "gesamt": len(eintraege),
                "bereit": sum(1 for e in eintraege if e["bereit"] == "True"),
                "ausgesetzt": sum(1 for e in eintraege if e["ausgesetzt"]),
                "eintraege": zeigen,
            }
        return {"arten": arten, "probleme": probleme, "anzahl_probleme": len(probleme), "ts": _iso(time.time())}

    # ── cluster.lage ───────────────────────────────────────────────────────────────────────
    def _alarme(self, ip_zu_name: dict[str, str]) -> list[dict[str, Any]]:
        """Feuernde Alarme aus Prometheus (ALERTS), ohne die Dauerlaeufer Watchdog/InfoInhibitor."""
        abfrager = ("monitoring-kube-state-metrics", "monitoring-prometheus-node-exporter", "dcgm-exporter")
        gruppen: dict[str, dict[str, Any]] = {}
        for r in self._abfrage('ALERTS{alertstate="firing"}'):
            m = r.get("metric") or {}
            name = str(m.get("alertname") or "?")
            if name in ("Watchdog", "InfoInhibitor"):
                continue
            g = gruppen.setdefault(name, {"name": name, "schwere": m.get("severity") or "?", "anzahl": 0, "ziele": []})
            g["anzahl"] += 1
            if m.get("job_name"):
                ziel = f"Job {m['job_name']}"
            elif m.get("pod") and not str(m["pod"]).startswith(abfrager):
                ziel = f"{m.get('namespace')}/{m['pod']}"
            elif m.get("node"):
                ziel = str(m["node"])
            elif m.get("instance"):
                ip = str(m["instance"]).rsplit(":", 1)[0]
                ziel = ip_zu_name.get(ip, str(m["instance"])) + (f" {m['device']}" if m.get("device") else "")
            else:
                ziel = name
            if ziel not in g["ziele"] and len(g["ziele"]) < 6:
                g["ziele"].append(ziel)
        rang = {"critical": 0, "warning": 1}
        return sorted(gruppen.values(), key=lambda g: (rang.get(str(g["schwere"]), 2), -g["anzahl"], g["name"]))

    def _zertifikate(self) -> list[dict[str, Any]]:
        uhren = self._lage_plugin().call("fristen", {}).get("uhren") or []
        return [u for u in uhren if u.get("art") == "zertifikat"]

    def lage(self, args: dict[str, Any]) -> dict[str, Any]:
        r = self._parallel({
            "pods": lambda: self._liste("pods"), "replicasets": lambda: self._liste("replicasets"),
            "jobs": lambda: self._liste("jobs"), "cronjobs": lambda: self._liste("cronjobs"),
            "nodes": lambda: self._liste("nodes"), "flux": lambda: self.flux_zustand({"nur_probleme": True}, parallel=False),
            "zertifikate": self._zertifikate,
        })
        jetzt = time.time()
        knoten = r["nodes"][0] or []
        ip_zu_name = {a.get("address"): _name(k) for k in knoten for a in _g(k, "status", "addresses", vorgabe=[])
                      if a.get("type") == "InternalIP"}
        alarme, alarm_fehler = self._parallel({"a": lambda: self._alarme(ip_zu_name)})["a"]
        gruppen: list[dict[str, Any]] = []

        def gruppe(art: str, titel: str, eintraege: list[dict[str, Any]]) -> None:
            if eintraege:
                schwere = "rot" if any(e.get("schwere") == "rot" for e in eintraege) else (
                    "gelb" if any(e.get("schwere") == "gelb" for e in eintraege) else "info")
                gruppen.append({"art": art, "titel": titel, "schwere": schwere, "anzahl": len(eintraege), "eintraege": eintraege})

        # Pods (ohne Job-Pods: die stehen bei den Jobs)
        rs_zu_dep = self._rs_zu_dep(r["replicasets"][0] or [])
        pod_gruppen: dict[tuple[str, str, str], dict[str, Any]] = {}
        for p in r["pods"][0] or []:
            b = besitzer(p, rs_zu_dep)
            if b and b[0] == "job" and _g(p, "status", "phase") in ("Failed", "Succeeded"):
                continue
            pr = pod_problem(p, jetzt)
            if not pr:
                continue
            wer = f"{b[0]}/{b[1]}" if b and b[0] not in ("rs", "job") else f"pod/{basisname('Pod', _name(p))}"
            g = pod_gruppen.setdefault((_ns(p), wer, pr[1]), {"ns": _ns(p), "name": wer, "grund": pr[1], "detail": pr[2],
                                                               "schwere": pr[0], "anzahl": 0, "knoten": [], "pods": []})
            g["anzahl"] += 1
            if _g(p, "spec", "nodeName") and _g(p, "spec", "nodeName") not in g["knoten"]:
                g["knoten"].append(_g(p, "spec", "nodeName"))
            g["pods"].append(_name(p))
        gruppe("pods", "Pods mit Problemen", sorted(pod_gruppen.values(), key=lambda g: (g["schwere"] != "rot", g["ns"], g["name"])))

        # Gescheiterte Jobs der letzten 24 h, je CronJob
        erfolg = {(_ns(c), _name(c)): _ts(_g(c, "status", "lastSuccessfulTime")) for c in r["cronjobs"][0] or []}
        job_gruppen: dict[tuple[str, str], dict[str, Any]] = {}
        for j in r["jobs"][0] or []:
            c = _bedingung(j, "Failed")
            t = _ts(c.get("lastTransitionTime"))
            if c.get("status") != "True" or not t or jetzt - t > 86400:
                continue
            ref = next((x for x in _g(j, "metadata", "ownerReferences", vorgabe=[]) if x.get("kind") == "CronJob"), None)
            name = str(ref.get("name")) if ref else basisname("Job", _name(j))
            g = job_gruppen.setdefault((_ns(j), name), {"ns": _ns(j), "name": name, "anzahl": 0, "gruende": [], "zuletzt": t})
            g["anzahl"] += 1
            if c.get("reason") and c.get("reason") not in g["gruende"]:
                g["gruende"].append(c.get("reason"))
            g["zuletzt"] = max(g["zuletzt"], t)
        jobs = []
        for (ns, name), g in job_gruppen.items():
            ok_seit = erfolg.get((ns, name))
            seither_ok = bool(ok_seit and ok_seit > g["zuletzt"])
            jobs.append({**g, "zuletzt": _iso(g["zuletzt"]), "seither_ok": seither_ok, "schwere": "gelb" if seither_ok else "rot",
                         "grund": ", ".join(g["gruende"]) or "Failed"})
        gruppe("jobs", "Gescheiterte Jobs (24 h)", sorted(jobs, key=lambda g: (g["seither_ok"], g["ns"], g["name"])))

        # Flux
        flux, flux_fehler = r["flux"]
        fl = []
        for e in (flux or {}).get("probleme") or []:
            schwere = "gelb" if e["ausgesetzt"] or e["bereit"] == "Unknown" else "rot"
            fl.append({"art": e["art"], "ns": e["ns"], "name": e["name"], "bereit": e["bereit"], "ausgesetzt": e["ausgesetzt"],
                       "grund": "ausgesetzt" if e["ausgesetzt"] else (e["grund"] or "nicht bereit"), "detail": e["meldung"],
                       "schwere": schwere})
        gruppe("flux", "Flux nicht bereit oder ausgesetzt", fl)

        # Ausgesetzte CronJobs mit Grund
        aus = []
        for c in r["cronjobs"][0] or []:
            if not _g(c, "spec", "suspend"):
                continue
            a = _annot(c)
            grund = a.get("fyos.io/ausgesetzt-grund") or a.get("fyos/ausgesetzt-grund") or suspend_grund_aus_repo(c, self.repo)
            aus.append({"ns": _ns(c), "name": _name(c), "grund": grund or "kein Grund hinterlegt",
                        "bis": a.get("fyos.io/ausgesetzt-bis"), "schwere": "info"})
        gruppe("cronjobs_aus", "Ausgesetzte CronJobs", aus)

        # Knoten
        kn = []
        for k in knoten:
            bereit = _bedingung(k, "Ready")
            if bereit.get("status") != "True":
                kn.append({"name": _name(k), "grund": f"nicht bereit ({bereit.get('reason') or bereit.get('status') or '?'})",
                           "seit": bereit.get("lastTransitionTime"), "schwere": "rot"})
            for c in _g(k, "status", "conditions", vorgabe=[]):
                if c.get("type") in ("MemoryPressure", "DiskPressure", "PIDPressure") and c.get("status") == "True":
                    kn.append({"name": _name(k), "grund": c.get("type"), "seit": c.get("lastTransitionTime"), "schwere": "rot"})
            if _g(k, "spec", "unschedulable"):
                kn.append({"name": _name(k), "grund": "abgesperrt (nimmt keine neuen Pods)", "schwere": "gelb"})
        gruppe("knoten", "Knoten", kn)

        # Alarme
        gruppe("alarme", "Alarme (Prometheus)", [
            {**a, "schwere": "rot" if a["schwere"] == "critical" else "gelb" if a["schwere"] == "warning" else "info"}
            for a in alarme or []])

        # Zertifikat (Befund 14: Warnung unter 21 Tagen)
        info: list[str] = []
        zert, zert_fehler = r["zertifikate"]
        zs = []
        for u in zert or []:
            tage = u.get("in_tagen")
            if tage is None:
                zs.append({"name": u.get("name"), "grund": u.get("text"), "schwere": "gelb"})
            elif tage < ZERT_GELB_TAGE:
                zs.append({"name": u.get("name"), "grund": f"läuft in {tage:.0f} Tagen ab", "schwere": "rot" if tage < ZERT_ROT_TAGE else "gelb"})
            else:
                info.append(f"Zertifikat {u.get('name')}: noch {tage:.0f} Tage")
        gruppe("zertifikate", "Zertifikate", zs)

        quellen = {k: {"ok": v[1] is None, "fehler": v[1]} for k, v in r.items()}
        quellen["alarme"] = {"ok": alarm_fehler is None, "fehler": alarm_fehler}
        if flux_fehler is None and flux:
            for art, a in (flux.get("arten") or {}).items():
                if not a.get("ok"):
                    quellen[f"flux:{art}"] = {"ok": False, "fehler": a.get("fehler")}
        zustand = "rot" if any(g["schwere"] == "rot" for g in gruppen) else (
            "gelb" if any(g["schwere"] == "gelb" for g in gruppen) else "gruen")
        teile = [f"{g['anzahl']} × {g['titel']}" for g in gruppen if g["schwere"] != "info"]
        satz = "Alles in Ordnung." if not teile else "Nicht in Ordnung: " + ", ".join(teile) + "."
        if any(not q["ok"] for q in quellen.values()):
            satz += " Nicht alle Quellen lesbar."
        return {"zustand": zustand, "satz": satz, "gruppen": gruppen, "info": info, "quellen": quellen, "ts": _iso(jetzt)}

    # ── cluster.knoten_raeumen (nur Probe) ─────────────────────────────────────────────────
    def knoten_raeumen(self, args: dict[str, Any]) -> dict[str, Any]:
        name = str(args.get("knoten") or "").strip()
        if not name:
            raise ValueError("knoten fehlt")
        r = self._parallel({"nodes": lambda: self._liste("nodes"), "pods": lambda: self._liste("pods"),
                            "replicasets": lambda: self._liste("replicasets"), "pvcs": lambda: self._liste("pvcs"),
                            "pdbs": lambda: self._liste("pdbs")}, rechte={"pdbs": "poddisruptionbudgets list"})
        knoten = r["nodes"][0] or []
        k = next((x for x in knoten if _name(x) == name), None)
        if k is None:
            raise ValueError(f"Knoten {name!r} gibt es nicht — vorhanden: {', '.join(sorted(_name(x) for x in knoten))}")
        andere = [x for x in knoten if _name(x) != name]
        rs_zu_dep = self._rs_zu_dep(r["replicasets"][0] or [])
        pvcs = {(_ns(p), _name(p)): p for p in r["pvcs"][0] or []}
        eintraege = []
        je_workload: dict[str, list[str]] = {}
        for p in r["pods"][0] or []:
            if _g(p, "spec", "nodeName") != name or _g(p, "status", "phase") in ("Succeeded", "Failed"):
                continue
            b = besitzer(p, rs_zu_dep)
            spec = dict(p.get("spec") or {})
            spec.pop("nodeName", None)
            if b and b[0] == "ds":
                zustand, text = "bleibt", "DaemonSet — läuft auf jedem Knoten weiter"
            elif (b and b[0] == "statisch") or "kubernetes.io/config.mirror" in _annot(p):
                zustand, text = "bleibt", "statischer Pod des Knotens"
            elif not b:
                zustand, text = "kann_nicht", "nackter Pod ohne Besitzer — würde gelöscht und käme nicht wieder"
            else:
                lokal = []
                leer = False
                for v in spec.get("volumes") or []:
                    if v.get("persistentVolumeClaim"):
                        pvc = pvcs.get((_ns(p), str(v["persistentVolumeClaim"].get("claimName"))))
                        if pvc and (_g(pvc, "spec", "storageClassName") == "local-path"
                                    or _annot(pvc).get("volume.kubernetes.io/selected-node") == name):
                            lokal.append(f"PVC {_name(pvc)}")
                    elif v.get("hostPath"):
                        lokal.append(f"hostPath {v['hostPath'].get('path')}")
                    elif "emptyDir" in v:
                        leer = True
                ziele = moegliche_knoten(spec, andere)
                if lokal:
                    zustand, text = "kann_nicht", "lokale Daten: " + ", ".join(lokal[:4])
                elif not ziele:
                    zustand, text = "kann_nicht", "kein anderer Knoten passt (nodeSelector, Affinität, Taints)"
                else:
                    zustand = "zieht_um"
                    text = "→ " + ", ".join(ziele) + (" · Inhalt von emptyDir geht verloren" if leer else "") + (
                        " · der Job-Lauf bricht ab" if b[0] == "job" else "")
            wl = f"{b[0]}/{b[1]}" if b else None
            if wl and b[0] in ("deploy", "sts"):
                je_workload.setdefault(f"{_ns(p)}/{wl}", []).append(zustand)
            eintraege.append({"ns": _ns(p), "pod": _name(p), "workload": wl, "zustand": zustand, "text": text})
        faellt_aus = sorted(w for w, z in je_workload.items() if all(x == "kann_nicht" for x in z))
        hinweise = []
        pdbs, pdb_fehler = r["pdbs"]
        if pdb_fehler:
            hinweise.append(f"PodDisruptionBudgets nicht geprüft: {pdb_fehler}")
        else:
            hier = [p for p in r["pods"][0] or [] if _g(p, "spec", "nodeName") == name]
            for pdb in pdbs or []:
                if _g(pdb, "status", "disruptionsAllowed") != 0:
                    continue
                sel = _g(pdb, "spec", "selector", "matchLabels") or {}
                betroffen = [p for p in hier if _ns(p) == _ns(pdb) and sel and all(_labels(p).get(a) == b for a, b in sel.items())]
                if betroffen:
                    hinweise.append(f"PDB {_ns(pdb)}/{_name(pdb)} erlaubt gerade keine Unterbrechung — {_name(betroffen[0])} müsste warten")
        steuerebene = any(x in _labels(k) for x in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"))
        gesperrt = None
        if steuerebene or name in nie_absperren():
            gesperrt = f"{name} wird nie geräumt (Steuerebene, lokale Daten)"
        elif self._sperre(name):
            gesperrt = self._sperre(name)
        reihen = {"zieht_um": 0, "kann_nicht": 1, "bleibt": 2}
        eintraege.sort(key=lambda e: (reihen[e["zustand"]], e["ns"], e["pod"]))
        return {
            "knoten": name,
            "nur_probe": True,
            "ausfuehren": "in dieser Runde nicht vorgesehen — nur die Probe",
            "gesperrt": gesperrt,
            "zaehlung": {z: sum(1 for e in eintraege if e["zustand"] == z) for z in reihen},
            "pods": eintraege,
            "faellt_aus": faellt_aus,
            "hinweise": hinweise,
            "ts": _iso(time.time()),
        }

    # ── cluster.altlasten ──────────────────────────────────────────────────────────────────
    def altlasten(self, args: dict[str, Any]) -> dict[str, Any]:
        r = self._parallel({"pvs": lambda: self._liste("pvs"), "pods": lambda: self._liste("pods"),
                            "services": lambda: self._liste("services"), "endpoints": lambda: self._liste("endpoints")},
                           rechte={"pvs": "persistentvolumes list"})
        released = []
        pvs, pv_fehler = r["pvs"]
        for pv in pvs or []:
            if _g(pv, "status", "phase") != "Released":
                continue
            claim = _g(pv, "spec", "claimRef") or {}
            sp = pv.get("spec") or {}
            if sp.get("nfs"):
                pfad = f"{sp['nfs'].get('server')}:{sp['nfs'].get('path')}"
            else:
                pfad = _g(sp, "hostPath", "path") or _g(sp, "local", "path") or _g(sp, "csi", "volumeHandle")
            released.append({"name": _name(pv), "groesse_gib": round(menge(_g(pv, "spec", "capacity", "storage")) / 2**30, 1),
                             "klasse": sp.get("storageClassName"), "pfad": pfad,
                             "war_fuer": f"{claim.get('namespace')}/{claim.get('name')}" if claim else None,
                             "rueckgabe": sp.get("persistentVolumeReclaimPolicy")})
        released.sort(key=lambda x: -x["groesse_gib"])
        nackt = []
        for p in r["pods"][0] or []:
            if _g(p, "metadata", "ownerReferences") or "kubernetes.io/config.mirror" in _annot(p):
                continue
            nackt.append({"ns": _ns(p), "name": _name(p), "phase": _g(p, "status", "phase"), "knoten": _g(p, "spec", "nodeName"),
                          "seit": _g(p, "status", "startTime"), "flux": flux_verwaltet(p)})
        mit_endpunkten = set()
        for ep in r["endpoints"][0] or []:
            if any(s.get("addresses") for s in ep.get("subsets") or []):
                mit_endpunkten.add((_ns(ep), _name(ep)))
        leer = []
        for s in r["services"][0] or []:
            if not _g(s, "spec", "selector") or _g(s, "spec", "type") == "ExternalName":
                continue
            if (_ns(s), _name(s)) not in mit_endpunkten:
                leer.append({"ns": _ns(s), "name": _name(s), "selektor": _g(s, "spec", "selector"), "flux": flux_verwaltet(s)})
        return {
            "released_pvs": released,
            "released_gib": round(sum(x["groesse_gib"] for x in released), 1),
            "released_fehler": pv_fehler,
            "nackte_pods": nackt,
            "dienste_ohne_endpunkte": leer,
            "skript": self._altlasten_skript(released, nackt, leer),
            "hinweis": "Löscht nie. Das Skript zeigt ohne Argument nur an; mit --ja löscht der Betreiber genau die Liste.",
            "ts": _iso(time.time()),
        }

    @staticmethod
    def _altlasten_skript(released: list[dict], nackt: list[dict], leer: list[dict]) -> str:
        z = [
            "#!/usr/bin/env bash",
            f"# Altlasten laut cluster.altlasten vom {_iso(time.time())} — der Betreiber fuehrt aus, das Steuerpult loescht nie.",
            "#   bash altlasten.sh        # Trockenlauf: zeigt, was weg wuerde",
            "#   bash altlasten.sh --ja   # loescht genau diese Objekte, wenn sie noch im selben Zustand sind",
            "set -euo pipefail",
            'JA=0; [ "${1:-}" = "--ja" ] && JA=1',
            'weg() { if [ "$JA" = 1 ]; then echo "+ $*"; "$@"; else echo "(trocken) $*"; fi; }',
            "",
            "# Released-PVs (Retain): `kubectl delete pv` entfernt nur das Objekt. Die Daten bleiben am",
            "# genannten Ort liegen — Platz wird erst frei, wenn der Betreiber den Ordner dort selbst loescht.",
        ]
        for pv in released:
            z.append(f'[ "$(kubectl get pv {pv["name"]} -o jsonpath={{.status.phase}} 2>/dev/null)" = Released ] '
                     f'&& weg kubectl delete pv {pv["name"]}   # {pv["groesse_gib"]} GiB, war {pv["war_fuer"]}, Daten: {pv.get("pfad") or "?"}')
        z.append("")
        z.append("# Nackte Pods ohne Besitzer — nur beendete; laufende bleiben stehen.")
        for p in nackt:
            if p["phase"] in ("Succeeded", "Failed") and not p["flux"]:
                z.append(f'case "$(kubectl -n {p["ns"]} get pod {p["name"]} -o jsonpath={{.status.phase}} 2>/dev/null)" in '
                         f'Succeeded|Failed) weg kubectl -n {p["ns"]} delete pod {p["name"]} ;; esac')
            else:
                z.append(f"# {p['ns']}/{p['name']}: {p['phase']}{' (Flux)' if p['flux'] else ''} — bleibt")
        z.append("")
        z.append("# Dienste ohne Endpunkte — Flux-verwaltete in Git entfernen, nicht hier.")
        for s in leer:
            if s["flux"]:
                z.append(f"# {s['ns']}/{s['name']}: Flux ({s['flux']}) — in Git entfernen")
            else:
                z.append(f'[ -z "$(kubectl -n {s["ns"]} get endpoints {s["name"]} -o jsonpath={{.subsets}} 2>/dev/null)" ] '
                         f'&& weg kubectl -n {s["ns"]} delete service {s["name"]}')
        z.append("exit 0")
        return "\n".join(z) + "\n"

    # ── schreibend: gemeinsame Regeln ──────────────────────────────────────────────────────
    @staticmethod
    def _start(args: dict[str, Any]) -> tuple[bool, str]:
        ausfuehren = args.get("ausfuehren") is True
        grund = " ".join(str(args.get("grund") or "").split())[:GRUND_MAX]
        if ausfuehren and not grund:
            raise ValueError(_GRUND_LEER)
        return ausfuehren, grund

    def _probe(self, fn: Callable[[], Any], recht: str) -> tuple[str | None, str | None]:
        """Trockenlauf am API-Server (dryRun=All): (Recht fehlt, sonstiger Einwand)."""
        try:
            fn()
            return None, None
        except KubeFehler as e:
            if e.status == 403:
                return recht_fehlt(recht), None
            return None, f"API-Server lehnt ab: {_kurz(str(e), 240)}"

    def _abschluss(self, werkzeug: str, erg: dict[str, Any], ausfuehren: bool, grund: str, ziel: str,
                   knoten: list[str], tun: Callable[[], dict[str, Any]], recht: str) -> dict[str, Any]:
        """Trockenlauf-Antwort fertig machen — oder, mit ausfuehren, ausfuehren und melden."""
        erg.setdefault("hinweise", [])
        erg["ausfuehrbar"] = erg.get("blockiert") is None and not erg.get("braucht")
        erg.update(trockenlauf=True, ausgefuehrt=False, grund=grund or None)
        if not ausfuehren:
            return erg
        if erg.get("blockiert"):
            return {**erg, "ok": False, "error": f"nicht ausgeführt — {erg['blockiert']}"}
        if erg.get("braucht"):
            return {**erg, "ok": False, "error": "nicht ausgeführt — bestätigen: " + " · ".join(b["text"] for b in erg["braucht"])}
        try:
            ergebnis = tun()
        except KubeFehler as e:
            text = recht_fehlt(recht) if e.status == 403 else _kurz(str(e), 300)
            self._melden(werkzeug, ziel, grund, f"fehler: {text}", knoten)
            return {**erg, "ok": False, "error": text}
        self._cache_leeren()
        self._melden(werkzeug, ziel, grund, "ok", knoten)
        return {**erg, "trockenlauf": False, "ausgefuehrt": True, "ergebnis": ergebnis}

    def _pflicht(self, args: dict[str, Any], *namen: str) -> list[str]:
        werte = []
        for n in namen:
            w = str(args.get(n) or "").strip()
            if not w:
                raise ValueError(f"{n} fehlt")
            werte.append(w)
        return werte

    def _pod_knoten(self, ns: str, selektor: dict | None) -> tuple[list[dict], list[str]]:
        pods = self.kube().liste("pods", ns, selektor=_selektor(selektor)) if selektor else []
        pods = [p for p in pods if _g(p, "status", "phase") not in ("Succeeded", "Failed")]
        return pods, sorted({str(_g(p, "spec", "nodeName")) for p in pods if _g(p, "spec", "nodeName")})

    def _erste_sperre(self, knoten: list[str]) -> str | None:
        return next((s for s in (self._sperre(k) for k in knoten) if s), None)

    def _neue_pods_hinweis(self, tpl: dict, knoten_jetzt: list[str]) -> str | None:
        """Neue Pods (Neustart, Hochskalieren) koennen auch auf einem gesperrten Knoten landen."""
        try:
            moegl = moegliche_knoten(tpl, self._liste("nodes"))
        except Exception:  # noqa: BLE001
            return None
        weitere = [k for k in moegl if k not in knoten_jetzt and self._sperre(k)]
        if weitere:
            return f"Neue Pods könnten auch auf {', '.join(weitere)} landen ({self._sperre(weitere[0])}) — der Scheduler entscheidet"
        return None

    # ── cluster.neustarten ─────────────────────────────────────────────────────────────────
    def neustarten(self, args: dict[str, Any]) -> dict[str, Any]:
        ausfuehren, grund = self._start(args)
        ns, art, name = self._pflicht(args, "ns", "art", "name")
        if art not in _PATCH_ART:
            raise ValueError("art: deploy, sts oder ds")
        o = self._lesen(_PATCH_ART[art], name, ns)
        spec = o.get("spec") or {}
        pods, knoten = self._pod_knoten(ns, spec.get("selector"))
        hinweise: list[str] = []
        braucht: list[dict[str, str]] = []
        blockiert = self._erste_sperre(knoten)
        if ns in SYSTEM_NS and args.get("trotzdem") is not True:
            braucht.append({"feld": "trotzdem", "text": f"{ns} ist Systembereich (Flux/Kubernetes) — nur bewusst neu starten"})
        fl = flux_verwaltet(o)
        if fl:
            hinweise.append(f"Flux verwaltet das Objekt ({fl}) — der Neustart bleibt trotzdem, Flux führt die Neustart-Marke nicht")
        if art == "deploy":
            strat = spec.get("strategy") or {}
            if strat.get("type") == "Recreate":
                wie = "alle Pods gleichzeitig weg, dann neu — kurzer Ausfall"
            else:
                ru = strat.get("rollingUpdate") or {}
                weg = ru.get("maxUnavailable", "25%")
                if str(weg) in ("0", "0%"):
                    wie = "erst der neue Pod, dann geht der alte — kein Ausfall"
                else:
                    wie = f"Pods nacheinander ersetzt, höchstens {weg} gleichzeitig weg"
        elif art == "sts":
            wie = "Pods nacheinander, von der höchsten Nummer abwärts"
        else:
            wie = "Knoten für Knoten"
        if art in ("sts", "ds") and _g(spec, "updateStrategy", "type") == "OnDelete":
            blockiert = blockiert or "updateStrategy OnDelete — die Neustart-Marke allein startet nichts neu"
        if art != "ds" and spec.get("replicas") == 0:
            blockiert = blockiert or f"nichts zu tun — {ns}/{name} steht auf 0 Replikas (neu starten wirkt erst beim Hochskalieren)"
        neu = self._neue_pods_hinweis(_g(spec, "template", "spec", vorgabe={}), knoten) if art != "ds" else None
        if neu:
            hinweise.append(neu)
        if (ns, name) == ("fyos", "mcp-hub"):
            hinweise.append("Das ist der Hub selbst — die Antwort kommt noch, danach die Seite kurz neu laden")
        marke = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        patch = {"spec": {"template": {"metadata": {"annotations": {"kubectl.kubernetes.io/restartedAt": marke}}}}}
        recht = _RECHT_ZUM_NEUSTART[art]
        fehlt, einwand = self._probe(lambda: self.kube().patchen(_PATCH_ART[art], name, ns, patch, trocken=True), recht)
        erg = {
            "werkzeug": "cluster.neustarten",
            "ziel": {"art": art, "ns": ns, "name": name},
            "plan": f"Startet {ns}/{name} neu: {len(pods)} Pod(s) auf {', '.join(knoten) or '—'}; {wie}.",
            "knoten": knoten,
            "hinweise": hinweise,
            "blockiert": blockiert or fehlt or einwand,
            "braucht": braucht,
        }
        return self._abschluss("cluster.neustarten", erg, ausfuehren, grund, f"{art} {ns}/{name}", knoten,
                               lambda: {"neustart_marke": marke, "antwort": _kurz(json.dumps(
                                   self.kube().patchen(_PATCH_ART[art], name, ns, patch, trocken=False).get("status") or {}), 200)},
                               recht)

    # ── cluster.skalieren ──────────────────────────────────────────────────────────────────
    def skalieren(self, args: dict[str, Any]) -> dict[str, Any]:
        ausfuehren, grund = self._start(args)
        ns, name = self._pflicht(args, "ns", "name")
        replikas = args.get("replikas")
        if not isinstance(replikas, int) or isinstance(replikas, bool) or not 0 <= replikas <= REPLIKAS_MAX:
            raise ValueError(f"replikas: ganze Zahl von 0 bis {REPLIKAS_MAX}")
        o = self._lesen("deployments", name, ns)
        spec = o.get("spec") or {}
        jetzt = int(spec.get("replicas") if spec.get("replicas") is not None else 1)
        pods, knoten = self._pod_knoten(ns, spec.get("selector"))
        hinweise: list[str] = []
        braucht: list[dict[str, str]] = []
        blockiert = self._erste_sperre(knoten) if replikas != jetzt else None
        if replikas == jetzt:
            blockiert = f"nichts zu tun — {ns}/{name} hat schon {jetzt} Replika(s)"
        verwalter = replikas_verwalter(o)
        if verwalter["flux"] and args.get("trotzdem") is not True:
            braucht.append({"feld": "trotzdem", "text": (
                f"Flux ({verwalter['manager']}, {verwalter['kustomization'] or verwalter['helmrelease']}) verwaltet "
                f"spec.replicas: wird beim nächsten Abgleich auf {jetzt} zurückgesetzt — dauerhaft nur per Git-Änderung")})
        elif verwalter["autoskalierer"] and args.get("trotzdem") is not True:
            braucht.append({"feld": "trotzdem", "text": "ein Autoscaler (HPA) steuert die Replikas und stellt sie gleich wieder"})
        if verwalter["flux"] and args.get("trotzdem") is True:
            hinweise.append(f"Flux setzt beim nächsten Abgleich wieder auf {jetzt} — dauerhaft nur per Git-Änderung")
        if replikas == 0 and args.get("null_bestaetigt") is not True:
            braucht.append({"feld": "null_bestaetigt", "text": f"0 Replikas: {ns}/{name} ist dann aus"})
        if replikas > jetzt:
            neu = self._neue_pods_hinweis(_g(spec, "template", "spec", vorgabe={}), knoten)
            if neu:
                hinweise.append(neu)
        fehlt, einwand = (None, None)
        if replikas != jetzt:
            fehlt, einwand = self._probe(lambda: self.kube().skalieren(name, ns, replikas, trocken=True), "deployments/scale patch")
        erg = {
            "werkzeug": "cluster.skalieren",
            "ziel": {"art": "deploy", "ns": ns, "name": name},
            "plan": f"Setzt {ns}/{name} von {jetzt} auf {replikas} Replika(s).",
            "vorher": jetzt,
            "nachher": replikas,
            "replikas_von": "flux" if verwalter["flux"] else "autoscaler" if verwalter["autoskalierer"] else "hand",
            "knoten": knoten,
            "hinweise": hinweise,
            "blockiert": blockiert or fehlt or einwand,
            "braucht": braucht,
        }

        def tun() -> dict[str, Any]:
            a = self.kube().skalieren(name, ns, replikas, trocken=False)
            return {"vorher": jetzt, "nachher": _g(a, "spec", "replicas", vorgabe=replikas)}

        return self._abschluss("cluster.skalieren", erg, ausfuehren, grund, f"deploy {ns}/{name} {jetzt}→{replikas}",
                               knoten, tun, "deployments/scale patch")

    # ── cluster.flux_abgleichen ────────────────────────────────────────────────────────────
    def flux_abgleichen(self, args: dict[str, Any]) -> dict[str, Any]:
        ausfuehren, grund = self._start(args)
        (name,) = self._pflicht(args, "kustomization")
        ns = str(args.get("ns") or "flux-system").strip()
        mit_quelle = args.get("mit_quelle", True) is not False
        ks = self._lesen("kustomizations", name, ns)
        ref = _g(ks, "spec", "sourceRef", vorgabe={})
        quelle_art = {"GitRepository": "gitrepositories", "OCIRepository": "ocirepositories",
                      "HelmRepository": "helmrepositories"}.get(str(ref.get("kind")))
        quelle = (quelle_art, str(ref.get("name")), str(ref.get("namespace") or ns)) if ref else None
        hinweise = ["Flux gleicht ohnehin im Takt ab — der Anstoß zieht ihn nur vor."]
        blockiert = None
        if _g(ks, "spec", "suspend"):
            blockiert = f"{ns}/{name} ist ausgesetzt — Flux ignoriert den Anstoß, bis sie fortgesetzt wird"
        if mit_quelle and ref and not quelle_art:
            hinweise.append(f"Quelle {ref.get('kind')} wird nicht angestoßen (nur Git/OCI/Helm)")
        marke = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        patch = {"metadata": {"annotations": {"reconcile.fluxcd.io/requestedAt": marke}}}
        ziele = ([quelle] if mit_quelle and quelle and quelle_art else []) + [("kustomizations", name, ns)]

        def alle(trocken: bool) -> list[str]:
            erledigt = []
            for art, n, n_ns in ziele:
                self.kube().patchen(art, n, n_ns, patch, trocken=trocken)
                erledigt.append(f"{_ART_NAME.get(art, art)} {n_ns}/{n}")
            return erledigt

        fehlt, einwand = self._probe(lambda: alle(True), "kustomizations patch")
        erg = {
            "werkzeug": "cluster.flux_abgleichen",
            "ziel": {"art": "kustomization", "ns": ns, "name": name},
            "plan": "Stößt an: " + " → ".join(f"{_ART_NAME.get(a, a)} {x_ns}/{n}" for a, n, x_ns in ziele) + " (reconcile.fluxcd.io/requestedAt).",
            "zuletzt_abgeglichen": _g(ks, "status", "lastHandledReconcileAt"),
            "revision": _g(ks, "status", "lastAppliedRevision"),
            "knoten": [],
            "hinweise": hinweise,
            "blockiert": blockiert or fehlt or einwand,
            "braucht": [],
        }
        return self._abschluss("cluster.flux_abgleichen", erg, ausfuehren, grund, f"kustomization {ns}/{name}", [],
                               lambda: {"angestossen": alle(False), "marke": marke}, "kustomizations patch")

    # ── cluster.cronjob_jetzt ──────────────────────────────────────────────────────────────
    def cronjob_jetzt(self, args: dict[str, Any]) -> dict[str, Any]:
        ausfuehren, grund = self._start(args)
        ns, name = self._pflicht(args, "ns", "name")
        cj = self._lesen("cronjobs", name, ns)
        knoten = self._liste("nodes")
        sperren = self._sperren(knoten)
        e = self._cronjob_eintrag(cj, [], sperren, self._rechte("jobs create"), knoten, time.time())
        vorlage = _g(cj, "spec", "jobTemplate", vorgabe={})
        pod_spec = _g(vorlage, "spec", "template", "spec", vorgabe={})
        moegl = moegliche_knoten(pod_spec, knoten)
        meiden = sorted(k for k in moegl if sperren.get(k))
        job_spec = json.loads(json.dumps(vorlage.get("spec") or {}))
        if meiden and len(meiden) < len(moegl):
            job_spec.setdefault("template", {})["spec"] = _meide_knoten(pod_spec, meiden)
        anhang = "".join(secrets.choice(_SICHER) for _ in range(5))
        job_name = f"{name[:52]}-hand-{anhang}"
        annot = {**(_g(vorlage, "metadata", "annotations", vorgabe={}) or {}), "cronjob.kubernetes.io/instantiate": "manual",
                 "fyos.io/ausgeloest-von": f"steuerpult:{self._wer()}"[:60]}
        if grund:
            annot["fyos.io/grund"] = grund
        koerper = {
            "apiVersion": "batch/v1", "kind": "Job",
            "metadata": {"name": job_name, "namespace": ns, "labels": _g(vorlage, "metadata", "labels", vorgabe={}) or {},
                         "annotations": annot,
                         "ownerReferences": [{"apiVersion": "batch/v1", "kind": "CronJob", "name": name,
                                              "uid": _g(cj, "metadata", "uid"), "controller": True}]},
            "spec": job_spec,
        }
        hinweise = []
        blockiert = e["aktionen"]["jetzt"]["grund"]
        if e["messknoten_bezug"]:
            hinweise.append(f"arbeitet gegen {e['messknoten']}: " + ", ".join(e["messknoten_bezug"][:3]))
        if meiden and len(meiden) < len(moegl) and not blockiert:
            hinweise.append(f"der Job meidet {', '.join(meiden)} (Fenster offen)")
        if e["grund_ausgesetzt"]:
            hinweise.append(f"ausgesetzt, weil: {e['grund_ausgesetzt']}")
        fehlt, einwand = self._probe(lambda: self.kube().job_anlegen(ns, koerper, trocken=True), "jobs create")
        erg = {
            "werkzeug": "cluster.cronjob_jetzt",
            "ziel": {"art": "cronjob", "ns": ns, "name": name},
            "plan": f"Startet {ns}/{name} jetzt einmal als Job {job_name} (Zeitplan sonst: {e['zeitplan']}).",
            "zuletzt_erfolgreich": e["zuletzt_erfolgreich"],
            "knoten": [k for k in moegl if k not in meiden],
            "hinweise": hinweise,
            "blockiert": blockiert or fehlt or einwand,
            "braucht": [],
        }
        return self._abschluss("cluster.cronjob_jetzt", erg, ausfuehren, grund, f"cronjob {ns}/{name}", erg["knoten"],
                               lambda: {"job": _name(self.kube().job_anlegen(ns, koerper, trocken=False)) or job_name},
                               "jobs create")

    # ── cluster.knoten_absperren ───────────────────────────────────────────────────────────
    def knoten_absperren(self, args: dict[str, Any]) -> dict[str, Any]:
        ausfuehren, grund = self._start(args)
        (name,) = self._pflicht(args, "knoten")
        an = args.get("an")
        if not isinstance(an, bool):
            raise ValueError("an: true (absperren) oder false (freigeben)")
        k = self._lesen("nodes", name)
        lab = _labels(k)
        steuerebene = any(x in lab for x in ("node-role.kubernetes.io/control-plane", "node-role.kubernetes.io/master"))
        ist = bool(_g(k, "spec", "unschedulable"))
        blockiert = None
        if an and (steuerebene or name in nie_absperren()):
            blockiert = f"{name} wird nie abgesperrt (Steuerebene, lokale Daten)"
        elif ist == an:
            blockiert = f"nichts zu tun — {name} ist schon {'abgesperrt' if an else 'frei'}"
        blockiert = blockiert or self._sperre(name)
        pods = [p for p in self._liste("pods") if _g(p, "spec", "nodeName") == name
                and _g(p, "status", "phase") not in ("Succeeded", "Failed")]
        patch = {"spec": {"unschedulable": an}}
        fehlt, einwand = self._probe(lambda: self.kube().patchen("nodes", name, None, patch, trocken=True), "nodes patch")
        erg = {
            "werkzeug": "cluster.knoten_absperren",
            "ziel": {"art": "knoten", "name": name, "an": an},
            "plan": (f"Sperrt {name} für neue Pods. Die {len(pods)} laufenden Pods bleiben (kein Räumen), DaemonSets laufen weiter."
                     if an else f"Gibt {name} wieder für neue Pods frei."),
            "knoten": [name],
            "hinweise": [],
            "blockiert": blockiert or fehlt or einwand,
            "braucht": [],
        }
        return self._abschluss("cluster.knoten_absperren", erg, ausfuehren, grund,
                               f"knoten {name} {'absperren' if an else 'freigeben'}", [name],
                               lambda: {"abgesperrt": bool(_g(self.kube().patchen("nodes", name, None, patch, trocken=False),
                                                              "spec", "unschedulable"))},
                               "nodes patch")


def _meide_knoten(pod_spec: dict, knoten: list[str]) -> dict:
    """Pod-Vorlage, die die genannten Knoten meidet (Pflicht-Affinitaet hostname NotIn …).
    Jeder vorhandene nodeSelectorTerm bekommt den Ausdruck dazu — Terme sind ODER-verknuepft."""
    spec = json.loads(json.dumps(pod_spec))
    na = spec.setdefault("affinity", {}).setdefault("nodeAffinity", {})
    pflicht = na.setdefault("requiredDuringSchedulingIgnoredDuringExecution", {})
    ausdruck = {"key": "kubernetes.io/hostname", "operator": "NotIn", "values": sorted(knoten)}
    terme = pflicht.get("nodeSelectorTerms") or [{}]
    pflicht["nodeSelectorTerms"] = [{**t, "matchExpressions": list(t.get("matchExpressions") or []) + [ausdruck]} for t in terme]
    return spec


# ── Schutz fuer k8s.restart_deployment / k8s.scale_deployment (kein ungeschuetzter Nebenweg) ──
def schutz_deployment(dep: Any, pods: Any, *, skalieren: bool, trotzdem: bool) -> str | None:
    """Grund, warum k8s_core ein Deployment NICHT anfassen darf — oder None.

    Dieselben Regeln wie im Steuerpult: Fenster-Sperre fuer die Knoten der Pods, beim Skalieren
    Flux/Autoscaler (nur mit trotzdem). Arbeitet mit den typisierten Objekten von k8s_core."""
    knoten = sorted({str(getattr(getattr(p, "spec", None), "node_name", None) or "") for p in (getattr(pods, "items", None) or [])} - {""})
    for k in knoten:
        s = lage.sperrgrund(k)
        if s:
            return f"gesperrt: {s}"
    if skalieren and not trotzdem:
        v = replikas_verwalter(dep)
        if v["flux"]:
            return (f"Flux ({v['manager']}, {v['kustomization'] or v['helmrelease']}) verwaltet spec.replicas — wird beim "
                    "nächsten Abgleich zurückgesetzt, dauerhaft nur per Git-Änderung. Mit trotzdem: true trotzdem skalieren.")
        if v["autoskalierer"]:
            return "ein Autoscaler (HPA) steuert die Replikas und stellt sie gleich wieder. Mit trotzdem: true trotzdem skalieren."
    return None
