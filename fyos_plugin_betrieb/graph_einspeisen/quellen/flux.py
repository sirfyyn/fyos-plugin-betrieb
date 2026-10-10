"""Quelle flux — wer rollt was aus: Kustomizations, HelmReleases, Bildautomatik.

Ist Flux im Cluster nicht installiert (keine Kustomization-API), ist die Quelle aus, kein Fehler.

Knoten
    kustomization:<ns>/<name>   kustomization  (pfad, bereit, ausgesetzt)
    helmrelease:<ns>/<name>     helmrelease    (chart, bereit)
    imagepolicy:<ns>/<name>     imagepolicy    (repo, bereit)
Kanten
    dienst/cronjob/service/pvc  verwaltet_von  kustomization   (aus dem Bestandsverzeichnis `status.inventory`)
    kustomization               braucht        kustomization   (`dependsOn`)
    imagepolicy                 beschreibt     image:<repo>    (die Bildautomatik beobachtet dieses Bild)
Die Revision (Commit) steht bewusst nicht in den Attributen: sie wechselt mit jedem Commit und würde den Verlauf
zumüllen. Wer wissen will, was ein Dienst gerade fährt, liest die `nutzt_image`-Kante (Tag, Digest) der Quelle k8s.
"""

from __future__ import annotations

from typing import Any

from ...betriebsgraph import Kante, Knoten
from ..k8s_zugang import K8sFehler
from ..kontext import Ausgeschaltet

NAME = "flux"

#: Bestandsverzeichnis-Art → Präfix der Graph-ID. Alles andere (ConfigMaps, RBAC, …) ist für Abhängigkeiten ohne Wert.
ART_ZU_PRAEFIX = {
    "Deployment": "dienst",
    "StatefulSet": "dienst",
    "DaemonSet": "dienst",
    "CronJob": "cronjob",
    "Service": "service",
    "PersistentVolumeClaim": "pvc",
}


def _bereit(obj: dict[str, Any]) -> bool | None:
    for c in (obj.get("status") or {}).get("conditions") or []:
        if c.get("type") == "Ready":
            return c.get("status") == "True"
    return None


def inventar_id(eintrag: str) -> str | None:
    """`fyos_mcp-hub_apps_Deployment` → `dienst:fyos/mcp-hub`; unbekannte Arten und Cluster-Objekte → None."""
    teile = eintrag.split("_")
    if len(teile) != 4:
        return None
    ns, name, _gruppe, art = teile
    praefix = ART_ZU_PRAEFIX.get(art)
    if not praefix or not ns or not name:
        return None
    return f"{praefix}:{ns}/{name}"


def _fehlt(e: K8sFehler) -> bool:
    """Die Art gibt es im Cluster nicht (CRD nicht installiert)."""
    return e.status == 404


def sammeln(ctx: Any) -> tuple[list[Knoten], list[Kante]]:
    knoten: list[Knoten] = []
    kanten: list[Kante] = []
    try:
        ks = ctx.k8s_liste("kustomizations")
    except K8sFehler as e:
        if _fehlt(e):
            raise Ausgeschaltet("Flux ist im Cluster nicht installiert (keine Kustomization-API)") from e
        raise
    for k in ks:
        ns, name = k["metadata"]["namespace"], k["metadata"]["name"]
        kid = f"kustomization:{ns}/{name}"
        sp = k.get("spec") or {}
        knoten.append(Knoten(kid, "kustomization", name, {"pfad": sp.get("path"), "bereit": _bereit(k), "ausgesetzt": bool(sp.get("suspend"))}))
        for d in sp.get("dependsOn") or []:
            kanten.append(Kante(kid, f"kustomization:{d.get('namespace') or ns}/{d['name']}", "braucht", beleg="dependsOn"))
        gesehen: set[str] = set()
        for e in ((k.get("status") or {}).get("inventory") or {}).get("entries") or []:
            i = inventar_id(e.get("id", ""))
            if i and i not in gesehen:
                gesehen.add(i)
                kanten.append(Kante(i, kid, "verwaltet_von"))
    try:
        helm = ctx.k8s_liste("helmreleases")
    except K8sFehler as e:
        if not _fehlt(e):  # der Helm-Controller ist bei Flux optional
            raise
        helm = []
    for h in helm:
        ns, name = h["metadata"]["namespace"], h["metadata"]["name"]
        chart = (((h.get("spec") or {}).get("chart") or {}).get("spec") or {}).get("chart")
        knoten.append(Knoten(f"helmrelease:{ns}/{name}", "helmrelease", name, {"chart": chart, "bereit": _bereit(h)}))
    try:
        repos = {
            (r["metadata"]["namespace"], r["metadata"]["name"]): (r.get("spec") or {}).get("image")
            for r in ctx.k8s_liste("imagerepositories")
        }
        for p in ctx.k8s_liste("imagepolicies"):
            ns, name = p["metadata"]["namespace"], p["metadata"]["name"]
            ref = ((p.get("spec") or {}).get("imageRepositoryRef") or {}).get("name")
            repo = repos.get((ns, ref))
            pid = f"imagepolicy:{ns}/{name}"
            knoten.append(Knoten(pid, "imagepolicy", name, {"repo": repo, "bereit": _bereit(p)}))
            if repo:
                kanten.append(Kante(pid, f"image:{repo}", "beschreibt", beleg="Bildautomatik beobachtet das Bild"))
    except Exception:  # noqa: BLE001 — die Bildautomatik ist Zusatz
        pass
    return knoten, kanten
