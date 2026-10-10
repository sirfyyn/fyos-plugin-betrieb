"""betroffen — Impact-Analyse fuer betrieb.betroffen (2026-09-30), FastAPI- und Cluster-frei.

Portiert aus der abgebauten früheren Route `/api/fyos/betroffen`
(Logik und Infrastruktur der früheren Route). Beide Ebenen sind reine Datenarbeit; das Laden (Prozesskarte,
Cluster-Objekte) macht das Plugin `betrieb`. So bleibt die Logik ohne Cluster testbar.

    betroffen_bfs     Ebene „automatik": BFS ueber Prozesskarten-Kanten (lanes[].edges)
    knoten_treffer    welche Prozesskarten-Knoten meint die Frage „von=…"
    infra_betroffen   Ebene „infra": Node -> Workload -> Pod (Pins, Ausweich-Nodes)
"""

from __future__ import annotations

import re
from typing import Any


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower().split("(")[0])


def knoten_treffer(karte: dict[str, Any], von: str) -> list[str]:
    """Knoten der Karte, die `von` meint: exakt oder als Teilstring (normalisiert).

    „guardian" trifft alle Guardians — so war es früher, und so fragt man vor einem Change.
    Wirft ValueError mit aehnlichen Namen, wenn nichts passt.
    """
    ziel = _norm(von)
    if not ziel:
        raise ValueError("betroffen: `von` ist leer")
    nodes = karte.get("nodes", {})
    treffer = sorted(nid for nid in nodes if _norm(nid) == ziel or ziel in _norm(nid))
    if not treffer:
        bekannt = sorted(nodes)
        aehnlich = [n for n in bekannt if ziel[:4] and ziel[:4] in _norm(n)][:5]
        raise ValueError(
            f"betroffen: '{von}' nicht in der Prozesskarte ({len(bekannt)} Knoten). Aehnliche: {aehnlich}"
        )
    return treffer


def betroffen_bfs(karte: dict[str, Any], start_knoten: list[str]) -> dict[str, Any]:
    """Downstream-/Upstream-BFS ueber Prozesskarten-Kanten.

    start_knoten: eine oder mehrere Quellen (die Route matcht ggf. mehrere
    Knoten per Teilstring, z.B. "guardian" -> alle Guardians).
    Rueckgabe: dict mit abhaengig (was bricht), speist (wer liefert ein),
    lanes_betroffen. Je Eintrag steht 'ueber' — der Rueckweg ueber die Kante,
    damit man sieht, WARUM jemand betroffen ist.
    """
    abhaengig: dict[str, list[str]] = {}
    speiset_tmp: dict[str, list[str]] = {}
    betroffene_lanes: set[str] = set()

    def _downstream(start: str) -> None:
        seen: set[str] = {start}
        queue: list[str] = [start]
        while queue:
            cur = queue.pop(0)
            for lane in karte.get("lanes", []):
                for a, b in lane.get("edges", []):
                    if a == cur and b not in seen:
                        seen.add(b)
                        abhaengig.setdefault(b, []).append(cur)
                        betroffene_lanes.add(lane.get("titel", lane.get("id", "?")))
                        queue.append(b)

    def _upstream(start: str) -> None:
        seen: set[str] = {start}
        queue: list[str] = [start]
        while queue:
            cur = queue.pop(0)
            for lane in karte.get("lanes", []):
                for a, b in lane.get("edges", []):
                    if b == cur and a not in seen:
                        seen.add(a)
                        speiset_tmp.setdefault(a, []).append(cur)
                        queue.append(a)

    for start in start_knoten:
        _downstream(start)
        _upstream(start)

    node_lane = {nid: n.get("lane") for nid, n in karte.get("nodes", {}).items()}

    def _anreichern(d: dict[str, list[str]]) -> list[dict[str, Any]]:
        outl = []
        for name, ursachen in sorted(d.items()):
            nd = karte.get("nodes", {}).get(name, {})
            outl.append(
                {
                    "name": name,
                    "lane": node_lane.get(name, "?"),
                    "kurz": (nd.get("kurz") or "")[:110],
                    "ueber": ursachen,
                }
            )
        return outl

    return {
        "abhaengig": _anreichern(abhaengig),
        "speist": _anreichern(speiset_tmp),
        "lanes_betroffen": sorted(betroffene_lanes),
    }


def _selector_ziel(spec: dict[str, Any], tier_nodes: dict[str, list[str]]) -> dict[str, Any]:
    """Liefert {'hart': node} | {'weich': node} | {'frei': None} fuer einen PodSpec."""
    if spec.get("nodeName"):
        return {"art": "hart", "node": spec["nodeName"]}
    sel = spec.get("nodeSelector") or {}
    host = sel.get("kubernetes.io/hostname")
    if host:
        return {"art": "hart", "node": host}
    tier = sel.get("fyos-tier")
    if tier:
        nodes = tier_nodes.get(tier, [])
        if len(nodes) == 1:
            return {"art": "hart", "node": nodes[0]}
        if len(nodes) > 1:
            return {"art": "weich", "nodes": nodes}
    return {"art": "frei"}


def infra_betroffen(
    nodes_json: dict[str, Any],
    deployments_json: dict[str, Any],
    cronjobs_json: dict[str, Any] | None = None,
    pods_json: dict[str, Any] | None = None,
    von: str = "",
) -> dict[str, Any]:
    """Impact-Analyse auf Infra-Ebene. von=<node-name> (oder Dienst-Name)."""
    cronjobs_json = cronjobs_json or {}
    pods_json = pods_json or {}

    nodes = {}
    for it in nodes_json.get("items", []):
        meta = it["metadata"]
        labels = meta.get("labels") or {}
        conds = {c.get("type"): c.get("status") for c in it.get("status", {}).get("conditions", [])}
        nodes[meta["name"]] = {
            "tier": labels.get("fyos-tier", "?"),
            "ready": conds.get("Ready") == "True",
        }
    tier_nodes: dict[str, list[str]] = {}
    for n, info in nodes.items():
        tier_nodes.setdefault(info["tier"], []).append(n)

    # Dienst-Name? -> auf welchem Node laeuft er (nur) — umgekehrte Frage
    if von and von not in nodes:
        treffer: dict[str, Any] = {"von": von, "ist_node": False, "laeuft_auf": []}
        for it in deployments_json.get("items", []):
            if it["metadata"]["name"] != von:
                continue
            ziel = _selector_ziel(it["spec"]["template"]["spec"], tier_nodes)
            treffer["laeuft_auf"].append({"dienst": von, **ziel})
        return treffer

    # Node-Frage
    out: dict[str, Any] = {
        "von": von,
        "ist_node": True,
        "node_ready": nodes.get(von, {}).get("ready"),
        "gepinnte_workloads": [],
        "weiche_treffer": [],
        "laufende_pods": [],
        "ketten_hinweis": None,
    }

    for it in deployments_json.get("items", []):
        name = it["metadata"]["name"]
        ziel = _selector_ziel(it["spec"]["template"]["spec"], tier_nodes)
        if ziel.get("art") == "hart" and ziel.get("node") == von:
            out["gepinnte_workloads"].append(
                {
                    "name": name,
                    "art": "Deployment",
                    "grund": "harter Pin (nodeName/Selector/Tier-eindeutig)",
                }
            )
        elif ziel.get("art") == "weich" and von in ziel.get("nodes", []):
            ausweich = [n for n in ziel["nodes"] if n != von]
            out["weiche_treffer"].append(
                {"name": name, "art": "Deployment", "ausweich_nodes": ausweich}
            )

    for it in cronjobs_json.get("items", []):
        name = it["metadata"]["name"]
        ziel = _selector_ziel(it["spec"]["jobTemplate"]["spec"]["template"]["spec"], tier_nodes)
        if ziel.get("art") == "hart" and ziel.get("node") == von:
            out["gepinnte_workloads"].append(
                {"name": name, "art": "CronJob", "grund": "harter Pin"}
            )
        elif ziel.get("art") == "weich" and von in ziel.get("nodes", []):
            ausweich = [n for n in ziel["nodes"] if n != von]
            out["weiche_treffer"].append(
                {"name": name, "art": "CronJob", "ausweich_nodes": ausweich}
            )

    for it in pods_json.get("items", []):
        meta = it["metadata"]
        spec = it["spec"]
        if spec.get("nodeName") != von:
            continue
        besitzer = "?"
        for own in meta.get("ownerReferences", []):
            besitzer = own.get("name", "?")
            if own.get("kind") == "ReplicaSet":  # RS -> Deployment kuerzen
                besitzer = "-".join(own["name"].split("-")[:-1]) or own["name"]
        out["laufende_pods"].append(
            {
                "name": meta["name"],
                "besitzer": besitzer,
                "phase": it.get("status", {}).get("phase", "?"),
            }
        )

    if out["gepinnte_workloads"]:
        out["ketten_hinweis"] = (
            f"{len(out['gepinnte_workloads'])} Workloads sind HART auf '{von}' gepinnt und "
            "haben keinen Ausweich-Node — sie stehen, bis der Node zurueck ist. "
            "Alles, was diese Dienste ruft, faellt mit (z.B. CI, Inference)."
        )
    return out
