"""Quellen des Betriebsgraphen (fyos_plugin_betrieb/graph_einspeisen/quellen/*): jede wird mit kleinen Attrappen gelesen,
ohne Cluster, ohne Netz und ohne Datenbank. Geprüft wird, dass aus dem Rohstand die richtigen Knoten und Kanten
entstehen (Pins, Adressen, Textfunde, Kommentar) und dass eine nicht eingerichtete Quelle ausgeschaltet ist, statt zu
scheitern; nicht die Zahlen eines echten Clusters."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
import yaml
from mcp_hub import vorgaenge
from mcp_hub.vorgaenge import Dienst

import fyos_plugin_betrieb
from fyos_plugin_betrieb.graph_einspeisen.k8s_zugang import K8sFehler
from fyos_plugin_betrieb.graph_einspeisen.kontext import Ausgeschaltet, Kontext
from fyos_plugin_betrieb.graph_einspeisen.quellen import _text, bestand, caddy, experimente, flux, hub, itsm, k8s, rechner

BEISPIEL = Path(fyos_plugin_betrieb.__file__).resolve().parent / "daten" / "rechner.beispiel.yaml"


def ids(liste):
    return {x.id for x in liste}


def kanten_set(liste):
    return {(e.von, e.nach, e.art) for e in liste}


def cluster(antworten: dict[str, list[dict[str, Any]]]):
    """Attrappe für `Kontext.k8s`: Art → Objekte; eine fehlende Art gibt es im Cluster nicht (404)."""

    def liste(art: str) -> list[dict[str, Any]]:
        if art not in antworten:
            raise K8sFehler(f"die Art {art} gibt es nicht", 404)
        return antworten[art]

    return liste


def ohne_umgebung(**einstellungen: str) -> dict[str, str]:
    return dict(einstellungen)


# ── k8s ──────────────────────────────────────────────────────────────────────


def test_bild_zerlegen_und_groessen() -> None:
    assert k8s.bild_zerlegen("git.example/a/b:main") == ("git.example/a/b", "main", None)
    assert k8s.bild_zerlegen("host:5000/a/b:1.2@sha256:abcdef") == ("host:5000/a/b", "1.2", "sha256:abcdef")
    assert k8s.bild_zerlegen("qdrant/qdrant") == ("qdrant/qdrant", None, None)
    assert k8s.groesse_text("2048Mi") == "2Gi"
    assert k8s.groesse_text("512Mi") == "512Mi"


def _knoten(name: str, **labels):
    return {
        "metadata": {"name": name, "labels": {"kubernetes.io/arch": "amd64", "kubernetes.io/hostname": name, **labels}},
        "spec": {},
        "status": {"allocatable": {}},
    }


def test_kandidaten_beachtet_selektor_taint_und_gpu() -> None:
    schlicht = _knoten("node-a")
    gpu_knoten = _knoten("node-b")
    gpu_knoten["spec"]["taints"] = [{"key": "gpu", "effect": "NoSchedule"}]
    gpu_knoten["status"]["allocatable"] = {"nvidia.com/gpu": "2"}
    alle = [schlicht, gpu_knoten]
    assert k8s.kandidaten({}, alle) == ["node-a"]  # node-b hat einen Taint
    assert k8s.kandidaten({"tolerations": [{"key": "gpu", "operator": "Exists"}]}, alle) == ["node-a", "node-b"]
    assert k8s.kandidaten({"nodeSelector": {"kubernetes.io/arch": "arm64"}}, alle) == []
    gpu = {"tolerations": [{"operator": "Exists"}], "containers": [{"resources": {"limits": {"nvidia.com/gpu": 1}}}]}
    assert k8s.kandidaten(gpu, alle) == ["node-b"]  # node-a hat keine GPU
    affin = {
        "affinity": {"nodeAffinity": {"requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [
            {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "NotIn", "values": ["node-b"]}]}]}}}
    }
    assert k8s.kandidaten({**affin, "tolerations": [{"operator": "Exists"}]}, alle) == ["node-a"]


def test_adressen_nur_bekannte_services() -> None:
    bekannt = {("fyos", "qdrant"), ("fyos", "redis")}
    t = "http://qdrant.fyos.svc.cluster.local:6333 und redis://redis:6379/0 und http://unbekannt:80"
    assert k8s.adressen(t, "fyos", bekannt, kurz=True) == {("fyos", "qdrant"), ("fyos", "redis")}
    # ohne `kurz` zählt nur die volle Form
    assert k8s.adressen(t, "fyos", bekannt, kurz=False) == {("fyos", "qdrant")}


def _cluster_klein() -> dict[str, list[dict[str, Any]]]:
    """Ein kleiner Cluster in der Form der API-Listen: einzelne Objekte OHNE `kind`, wie der API-Server sie liefert."""
    knoten = [_knoten("node-a"), _knoten("node-b")]
    knoten[0]["status"]["addresses"] = [{"type": "InternalIP", "address": "192.0.2.1"}]

    def vorlage(name, *, selector=None, env=None, volumes=None, image="img/x:1", labels=None):
        spec = {"containers": [{"name": "c", "image": image, "env": env or []}], "volumes": volumes or []}
        if selector:
            spec["nodeSelector"] = selector
        return {"metadata": {"labels": labels or {"app": name}}, "spec": spec}

    def dep(name, *, replicas=1, **kw):
        return {
            "metadata": {"name": name, "namespace": "fyos"},
            "spec": {"replicas": replicas, "template": vorlage(name, **kw)},
            "status": {"readyReplicas": replicas},
        }

    sts = {
        "metadata": {"name": "db", "namespace": "fyos"},
        "spec": {"replicas": 1, "template": vorlage("db"), "volumeClaimTemplates": [{"metadata": {"name": "data"}}]},
        "status": {"readyReplicas": 1},
    }
    ds = {
        "metadata": {"name": "agent", "namespace": "kube-system"},
        "spec": {"template": vorlage("agent")},
        "status": {"desiredNumberScheduled": 2, "numberReady": 1},
    }
    cron = {
        "metadata": {"name": "sicherung", "namespace": "fyos"},
        "spec": {
            "schedule": "0 3 * * *",
            "suspend": True,
            "jobTemplate": {"spec": {"template": vorlage("sicherung", selector={"kubernetes.io/hostname": "node-b"})}},
        },
    }
    webapp_pod = {
        "metadata": {"namespace": "fyos", "name": "webapp-5d9c7b8f6-abcde", "ownerReferences": [{"kind": "ReplicaSet", "name": "webapp-5d9c7b8f6"}]},
        "spec": {"nodeName": "node-b"},
        "status": {"phase": "Running"},
    }
    return {
        "nodes": knoten,
        "deployments": [
            dep("qdrant", selector={"kubernetes.io/hostname": "node-a"}, volumes=[{"name": "d", "persistentVolumeClaim": {"claimName": "qdrant-data"}}]),
            dep("webapp", env=[{"name": "QDRANT_URL", "value": "http://qdrant.fyos.svc.cluster.local:6333"}]),
        ],
        "statefulsets": [sts],
        "daemonsets": [ds],
        "cronjobs": [cron],
        "services": [
            {"metadata": {"name": "qdrant", "namespace": "fyos"}, "spec": {"selector": {"app": "qdrant"}, "ports": [{"port": 6333}]}},
            {"metadata": {"name": "kube-dns", "namespace": "kube-node-lease"}, "spec": {"selector": {"app": "x"}}},
        ],
        "pods": [webapp_pod],
        "pvcs": [
            {"metadata": {"name": "qdrant-data", "namespace": "fyos"}, "spec": {"volumeName": "pv-q", "resources": {"requests": {"storage": "10Gi"}}}, "status": {"phase": "Bound"}},
            {"metadata": {"name": "data-db-0", "namespace": "fyos"}, "spec": {}, "status": {"phase": "Bound"}},
        ],
        "pvs": [
            {
                "metadata": {"name": "pv-q"},
                "spec": {"capacity": {"storage": "10Gi"}, "nodeAffinity": {"required": {"nodeSelectorTerms": [
                    {"matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In", "values": ["node-a"]}]}]}}},
                "status": {"phase": "Bound"},
            }
        ],
        "configmaps": [],
    }


def test_k8s_sammeln_pins_adressen_volumes() -> None:
    ctx = Kontext(umgebung={}, trocken=True, k8s=cluster(_cluster_klein()))
    knoten, kanten = k8s.sammeln(ctx)
    assert {"knoten:node-a", "knoten:node-b", "dienst:fyos/qdrant", "dienst:fyos/webapp", "service:fyos/qdrant", "pvc:fyos/qdrant-data", "pv:pv-q"} <= ids(knoten)
    ks = kanten_set(kanten)
    assert ("dienst:fyos/qdrant", "knoten:node-a", "laeuft_auf") in ks
    # nodeSelector auf genau einen Knoten = harter Pin; webapp hat zwei Kandidaten = frei, dorthin, wo der Pod läuft
    pins = {(e.von, e.nach): e.attribute.get("pin") for e in kanten if e.art == "laeuft_auf"}
    assert pins[("dienst:fyos/qdrant", "knoten:node-a")] == "hart"
    assert pins[("dienst:fyos/webapp", "knoten:node-b")] == "frei"
    assert ("service:fyos/qdrant", "dienst:fyos/qdrant", "waehlt") in ks
    assert ("dienst:fyos/webapp", "service:fyos/qdrant", "nutzt") in ks
    assert ("dienst:fyos/qdrant", "pvc:fyos/qdrant-data", "mountet") in ks
    assert ("pvc:fyos/qdrant-data", "pv:pv-q", "bindet") in ks
    assert ("pv:pv-q", "knoten:node-a", "liegt_auf") in ks
    nutzt = next(e for e in kanten if e.art == "nutzt" and e.von == "dienst:fyos/webapp")
    assert "QDRANT_URL" in nutzt.beleg and "6333" not in nutzt.beleg  # der Beleg nennt den Ort, nie den Wert
    assert ("dienst:fyos/qdrant", "image:img/x", "nutzt_image") in ks


def test_k8s_sammeln_arten_und_ausgelassene_namensraeume() -> None:
    ctx = Kontext(umgebung={}, trocken=True, k8s=cluster(_cluster_klein()))
    knoten, kanten = k8s.sammeln(ctx)
    je_id = {k.id: k for k in knoten}
    # StatefulSet, DaemonSet und CronJob erscheinen, obwohl die API-Listen kein `kind` tragen
    assert je_id["dienst:fyos/db"].attribute["art"] == "StatefulSet"
    assert je_id["dienst:kube-system/agent"].attribute["art"] == "DaemonSet" and je_id["dienst:kube-system/agent"].attribute["bereit"] is False
    assert je_id["cronjob:fyos/sicherung"].attribute == {"ns": "fyos", "zeitplan": "0 3 * * *", "ausgesetzt": True}
    ks = kanten_set(kanten)
    assert ("cronjob:fyos/sicherung", "knoten:node-b", "laeuft_auf") in ks
    assert ("dienst:fyos/db", "pvc:fyos/data-db-0", "mountet") in ks  # volumeClaimTemplate
    assert not any("kube-node-lease" in k.id for k in knoten)  # k3s-Rauschen bleibt draußen


def test_k8s_ohne_configmaps_geht_weiter() -> None:
    antworten = _cluster_klein()
    del antworten["configmaps"]
    knoten, _ = k8s.sammeln(Kontext(umgebung={}, trocken=True, k8s=cluster(antworten)))
    assert "dienst:fyos/qdrant" in ids(knoten)


def test_k8s_nutzt_aus_configmap_nennt_nur_den_ort() -> None:
    antworten = _cluster_klein()
    web = antworten["deployments"][1]
    web["spec"]["template"]["spec"]["volumes"] = [{"name": "k", "configMap": {"name": "web-config"}}]
    antworten["configmaps"] = [
        {"metadata": {"name": "web-config", "namespace": "fyos"}, "data": {"app.ini": "suche = http://qdrant.fyos.svc.cluster.local:6333"}}
    ]
    _, kanten = k8s.sammeln(Kontext(umgebung={}, trocken=True, k8s=cluster(antworten)))
    nutzt = [e for e in kanten if e.art == "nutzt" and e.von == "dienst:fyos/webapp"]
    # die ConfigMap findet den Dienst, den die Umgebung schon nannte, nicht doppelt
    assert len(nutzt) == 1


def test_k8s_kontext_liest_jede_art_nur_einmal() -> None:
    aufrufe: list[str] = []
    antworten = _cluster_klein()

    def liste(art: str):
        aufrufe.append(art)
        return antworten[art]

    ctx = Kontext(umgebung={}, trocken=True, k8s=liste)
    k8s.sammeln(ctx)
    ctx.k8s_liste("configmaps")  # eine zweite Quelle (caddy, hub) fragt dasselbe
    assert aufrufe.count("configmaps") == 1 and aufrufe.count("nodes") == 1


# ── flux ─────────────────────────────────────────────────────────────────────


def test_flux_inventar_id() -> None:
    assert flux.inventar_id("fyos_mcp-hub_apps_Deployment") == "dienst:fyos/mcp-hub"
    assert flux.inventar_id("fyos_graph-einspeisen_batch_CronJob") == "cronjob:fyos/graph-einspeisen"
    assert flux.inventar_id("_ns__Namespace") is None
    assert flux.inventar_id("fyos_x_y_ConfigMap") is None


def _flux_antworten() -> dict[str, list[dict[str, Any]]]:
    return {
        "kustomizations": [
            {
                "metadata": {"name": "apps", "namespace": "flux-system"},
                "spec": {"path": "./apps", "dependsOn": [{"name": "kern"}]},
                "status": {"conditions": [{"type": "Ready", "status": "True"}], "inventory": {"entries": [{"id": "fyos_graph-einspeisen_batch_CronJob", "v": "v1"}]}},
            }
        ],
        "helmreleases": [{"metadata": {"name": "monitoring", "namespace": "monitoring"}, "spec": {"chart": {"spec": {"chart": "kube-prometheus-stack"}}}}],
        "imagerepositories": [{"metadata": {"name": "app", "namespace": "flux-system"}, "spec": {"image": "registry.example/org/app"}}],
        "imagepolicies": [{"metadata": {"name": "app", "namespace": "flux-system"}, "spec": {"imageRepositoryRef": {"name": "app"}}}],
    }


def test_flux_sammeln() -> None:
    knoten, kanten = flux.sammeln(Kontext(umgebung={}, trocken=True, k8s=cluster(_flux_antworten())))
    assert {"kustomization:flux-system/apps", "helmrelease:monitoring/monitoring", "imagepolicy:flux-system/app"} <= ids(knoten)
    ks = kanten_set(kanten)
    assert ("cronjob:fyos/graph-einspeisen", "kustomization:flux-system/apps", "verwaltet_von") in ks
    assert ("kustomization:flux-system/apps", "kustomization:flux-system/kern", "braucht") in ks
    assert ("imagepolicy:flux-system/app", "image:registry.example/org/app", "beschreibt") in ks


def test_flux_ohne_flux_ist_aus_und_ohne_helm_geht_weiter() -> None:
    with pytest.raises(Ausgeschaltet, match="Flux"):
        flux.sammeln(Kontext(umgebung={}, trocken=True, k8s=cluster({})))
    antworten = _flux_antworten()
    del antworten["helmreleases"]  # Der Helm-Controller ist bei Flux optional
    knoten, _ = flux.sammeln(Kontext(umgebung={}, trocken=True, k8s=cluster(antworten)))
    assert "kustomization:flux-system/apps" in ids(knoten) and not any(i.startswith("helmrelease:") for i in ids(knoten))
    # ein anderer Fehler (kein Recht) bleibt ein Fehler
    def verboten(art: str):
        raise K8sFehler("HTTP 403: verboten", 403)

    with pytest.raises(K8sFehler):
        flux.sammeln(Kontext(umgebung={}, trocken=True, k8s=verboten))


# ── caddy ────────────────────────────────────────────────────────────────────

CADDYFILE = """
(anmeldung) {
    forward_auth auth.sso.svc.cluster.local:9000 {
        uri /outpost
    }
}
a.example.org {
    import anmeldung
    reverse_proxy http://qdrant.fyos.svc.cluster.local:6333
}
b.example.org {
    handle /api/* {
        reverse_proxy http://webapp.fyos.svc.cluster.local:8000
    }
    handle {
        reverse_proxy http://192.0.2.10:8080
    }
}
c.example.org {
    import authentik
    reverse_proxy http://192.0.2.50:8123
}
d.example.org {
    reverse_proxy http://192.0.2.10:9999
}
"""


def test_caddy_parse_faltet_snippets_und_pfade() -> None:
    seiten = {s["hosts"][0]: s for s in caddy.parse(CADDYFILE) if s["hosts"]}
    assert seiten["a.example.org"]["auth"] is True and seiten["b.example.org"]["auth"] is False
    ups = {u for u, _ in seiten["b.example.org"]["upstreams"]}
    assert "http://webapp.fyos.svc.cluster.local:8000" in ups
    pfad = dict(seiten["b.example.org"]["upstreams"])["http://webapp.fyos.svc.cluster.local:8000"]
    assert pfad.startswith("handle /api")
    # der Name des Anmelde-Snippets lässt sich ändern: ohne Eintrag zählt `import authentik` nicht mehr
    assert next(s for s in caddy.parse(CADDYFILE, ("sso",)) if s["hosts"] == ["c.example.org"])["auth"] is False


def test_caddy_upstream_ziel() -> None:
    ad = {"192.0.2.10": "knoten:werkbank"}
    ports = {("192.0.2.10", 8080): "rechner:modellserver"}
    ext = {"192.0.2.50": {"id": "extern:haussteuerung"}}
    assert caddy.upstream_ziel("http://qdrant.fyos.svc.cluster.local:6333", ad, ports, ext) == ("service:fyos/qdrant", "service")
    assert caddy.upstream_ziel("192.0.2.10:8080", ad, ports, ext) == ("rechner:modellserver", "rechner")
    assert caddy.upstream_ziel("http://192.0.2.10:9999", ad, ports, ext) == ("knoten:werkbank", "knoten")
    assert caddy.upstream_ziel("http://192.0.2.50:8123", ad, ports, ext) == ("extern:haussteuerung", "extern")
    assert caddy.upstream_ziel("{env.X}", ad, ports, ext) is None
    assert caddy.upstream_ziel("http://unbekannt:80", ad, ports, ext) is None


def test_caddy_ohne_caddyfile_ist_aus() -> None:
    with pytest.raises(Ausgeschaltet, match="GRAPH_CADDYFILE"):
        caddy.sammeln(Kontext(umgebung={}, trocken=True))


def test_caddy_sammeln_aus_datei_mit_rechner_datei(tmp_path: Path) -> None:
    manifest = tmp_path / "caddy.yaml"  # ein ConfigMap-Manifest geht wie eine Rohdatei
    manifest.write_text(yaml.safe_dump({"data": {"Caddyfile": CADDYFILE}}), encoding="utf-8")
    umgebung = ohne_umgebung(
        GRAPH_CADDYFILE=str(manifest), GRAPH_RECHNER_DATEN=str(BEISPIEL),
        GRAPH_CADDY_DIENST="dienst:ingress/caddy", GRAPH_CADDY_AUTH="service:sso/auth",
    )
    knoten, kanten = caddy.sammeln(Kontext(umgebung=umgebung, trocken=True))
    ks = kanten_set(kanten)
    assert {"route:a.example.org", "route:b.example.org", "route:c.example.org", "extern:haussteuerung"} <= ids(knoten)
    assert ("route:a.example.org", "service:fyos/qdrant", "routet_zu") in ks
    assert ("route:a.example.org", "service:sso/auth", "braucht") in ks
    assert ("route:c.example.org", "service:sso/auth", "braucht") in ks  # `import authentik` (Vorgabe-Snippet)
    assert ("route:b.example.org", "service:sso/auth", "braucht") not in ks
    assert ("route:b.example.org", "service:fyos/webapp", "routet_zu") in ks
    assert ("route:b.example.org", "dienst:ingress/caddy", "braucht") in ks
    # Adresse und Port eines Dienstes aus der Rechner-Datei; eine fremde Portnummer bleibt beim Rechner
    assert ("route:b.example.org", "rechner:modellserver", "routet_zu") in ks
    assert ("route:d.example.org", "knoten:werkbank", "routet_zu") in ks
    assert ("route:c.example.org", "extern:haussteuerung", "routet_zu") in ks
    assert next(k for k in knoten if k.id == "extern:haussteuerung").name == "Haussteuerung"
    assert next(e for e in kanten if e.nach == "rechner:modellserver").beleg == str(manifest)


def test_caddy_ohne_angaben_zum_dienst_keine_braucht_kanten(tmp_path: Path) -> None:
    datei = tmp_path / "Caddyfile"
    datei.write_text(CADDYFILE, encoding="utf-8")
    _, kanten = caddy.sammeln(Kontext(umgebung=ohne_umgebung(GRAPH_CADDYFILE=str(datei)), trocken=True))
    assert not any(e.art == "braucht" for e in kanten)  # weder Caddy-Dienst noch Anmeldung sind benannt
    assert ("route:a.example.org", "service:fyos/qdrant", "routet_zu") in kanten_set(kanten)


def test_caddy_aus_configmap_im_cluster() -> None:
    antworten = {"configmaps": [{"metadata": {"namespace": "ingress", "name": "caddy"}, "data": {"Caddyfile": CADDYFILE}}]}
    ctx = Kontext(umgebung=ohne_umgebung(GRAPH_CADDY_CONFIGMAP="ingress/caddy"), trocken=True, k8s=cluster(antworten))
    knoten, kanten = caddy.sammeln(ctx)
    assert "route:a.example.org" in ids(knoten)
    # ohne eigene Angabe heißt der Caddy-Dienst wie die ConfigMap
    assert ("route:a.example.org", "dienst:ingress/caddy", "braucht") in kanten_set(kanten)
    assert next(e for e in kanten if e.art == "routet_zu").beleg == "configmap ingress/caddy"
    with pytest.raises(RuntimeError, match="nicht gefunden"):
        caddy.sammeln(Kontext(umgebung=ohne_umgebung(GRAPH_CADDY_CONFIGMAP="ingress/gibtsnicht"), trocken=True, k8s=cluster(antworten)))
    with pytest.raises(RuntimeError, match="namespace"):
        caddy.sammeln(Kontext(umgebung=ohne_umgebung(GRAPH_CADDY_CONFIGMAP="ohneschrägstrich"), trocken=True, k8s=cluster(antworten)))


def test_caddy_unlesbare_datei_scheitert_laut(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="nicht lesbar"):
        caddy.sammeln(Kontext(umgebung=ohne_umgebung(GRAPH_CADDYFILE=str(tmp_path / "gibtsnicht")), trocken=True))
    leer = tmp_path / "leer.yaml"
    leer.write_text("data: {}\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="kein Caddyfile"):
        caddy.sammeln(Kontext(umgebung=ohne_umgebung(GRAPH_CADDYFILE=str(leer)), trocken=True))


# ── hub ──────────────────────────────────────────────────────────────────────


def test_hub_adressen_und_konfiguration() -> None:
    text = "# http://alt.fyos.svc.cluster.local ist Kommentar\nURL = 'http://qdrant.fyos.svc.cluster.local:6333'\n"
    assert hub.adressen_in(text) == [(2, "fyos", "qdrant")]
    konfig = {"plugins": [{"name": "audit", "module": "pakete.plugins.audit_log", "enabled": False, "config": {"url": "http://qdrant.fyos.svc.cluster.local:6333"}}, {"name": "lage"}, {"kein": "name"}]}
    p = hub.plugins_aus_config(yaml.safe_dump(konfig))
    assert p["audit"]["modul"] == "audit_log" and p["audit"]["voll"] == "pakete.plugins.audit_log" and p["audit"]["aktiv"] is False
    assert p["lage"]["aktiv"] is True and p["lage"]["voll"] == "" and set(p) == {"audit", "lage"}
    # dasselbe als ConfigMap-Manifest (die Konfiguration liegt unter data.<Schlüssel>)
    manifest = {"data": {"config.yaml": yaml.safe_dump(konfig), "anderes": "x"}}
    assert set(hub.plugins_aus_config(yaml.safe_dump(manifest))) == {"audit", "lage"}
    assert hub.plugins_aus_config("") == {}


def test_hub_entry_points_ohne_laden() -> None:
    eps = [("graph", "fyos_plugin_betrieb.graph"), ("lage", "fyos_plugin_betrieb.lage"), ("graph", "doppelt.graph")]
    gefunden = hub.plugins_aus_entry_points(eps, schon={"lage"})
    assert set(gefunden) == {"graph"} and gefunden["graph"]["voll"] == "fyos_plugin_betrieb.graph" and gefunden["graph"]["aktiv"] is True
    # die echten Entry-Points dieses Pakets lassen sich lesen, ohne die Plugins zu laden
    echte = dict(hub.entry_points_lesen())
    if "lage" not in echte:
        pytest.skip("Paket nicht installiert")
    assert echte.get("graph") == "fyos_plugin_betrieb.graph"


def test_hub_modul_text_liest_ohne_auszufuehren() -> None:
    assert "class GraphPlugin" in (hub.modul_text("fyos_plugin_betrieb.graph") or "")
    assert hub.modul_text("gibt.es.nicht") is None and hub.modul_text("") is None


def test_hub_sammeln(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hub, "entry_points_lesen", lambda: [("eigen", "paket.eigen"), ("stumm", "paket.stumm"), ("aus", "paket.aus")])
    quellen = {"paket.eigen": "# Kommentar http://nie.fyos.svc.cluster.local\nURL = 'http://qdrant.fyos.svc.cluster.local:6333'\n", "paket.stumm": "x = 1\n"}
    monkeypatch.setattr(hub, "modul_text", lambda modul: quellen.get(modul))
    konfig = tmp_path / "hub.yaml"
    konfig.write_text(yaml.safe_dump({"plugins": [
        {"name": "aus", "enabled": False},
        {"name": "stumm", "config": {"url": "http://redis.fyos.svc.cluster.local:6379"}},
    ]}), encoding="utf-8")
    umgebung = ohne_umgebung(GRAPH_HUB_CONFIG=str(konfig), GRAPH_HUB_DIENST="dienst:fyos/hub")
    knoten, kanten = hub.sammeln(Kontext(umgebung=umgebung, trocken=True))
    je_id = {k.id: k for k in knoten}
    assert set(je_id) == {"plugin:eigen", "plugin:stumm", "plugin:aus"}
    assert je_id["plugin:aus"].attribute == {"modul": "aus", "aktiv": False}
    assert je_id["plugin:eigen"].attribute == {"modul": "eigen", "aktiv": True}
    ks = kanten_set(kanten)
    assert {(p, "dienst:fyos/hub", "braucht") for p in je_id} <= ks
    nutzt = {(e.von, e.nach): e for e in kanten if e.art == "nutzt"}
    assert nutzt[("plugin:eigen", "service:fyos/qdrant")].beleg == "paket.eigen:2" and nutzt[("plugin:eigen", "service:fyos/qdrant")].vertrauen == 0.8
    assert ("plugin:eigen", "service:fyos/nie") not in nutzt  # Kommentarzeilen zählen nicht
    # die Adresse aus der Konfiguration eines Plugins; der Beleg nennt den Ort, nicht den Wert
    assert nutzt[("plugin:stumm", "service:fyos/redis")].beleg.startswith("Hub-Konfiguration (plugin stumm)")
    # ohne Hub-Dienst gibt es keine braucht-Kanten, die Plugins bleiben
    knoten2, kanten2 = hub.sammeln(Kontext(umgebung=ohne_umgebung(GRAPH_HUB_CONFIG=str(konfig)), trocken=True))
    assert len(knoten2) == 3 and not any(e.art == "braucht" for e in kanten2)


def test_hub_nutzt_nur_bekannte_services(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub, "entry_points_lesen", lambda: [("eigen", "paket.eigen")])
    monkeypatch.setattr(hub, "modul_text", lambda modul: "A = 'http://qdrant.fyos.svc.cluster.local'\nB = 'http://fremd.fyos.svc.cluster.local'\n")
    _, kanten = hub.sammeln(Kontext(umgebung={}, trocken=True, bekannt={"service:fyos/qdrant"}))
    assert {e.nach for e in kanten} == {"service:fyos/qdrant"}


def test_hub_konfiguration_aus_configmap_und_fehler(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hub, "entry_points_lesen", lambda: [])
    antworten = {"configmaps": [{"metadata": {"namespace": "hub", "name": "konfig"}, "data": {"config.yaml": yaml.safe_dump({"plugins": [{"name": "x", "module": "a.b"}]})}}]}
    monkeypatch.setattr(hub, "modul_text", lambda modul: None)
    knoten, _ = hub.sammeln(Kontext(umgebung=ohne_umgebung(GRAPH_HUB_CONFIGMAP="hub/konfig"), trocken=True, k8s=cluster(antworten)))
    assert ids(knoten) == {"plugin:x"}
    with pytest.raises(RuntimeError, match="nicht gefunden"):
        hub.sammeln(Kontext(umgebung=ohne_umgebung(GRAPH_HUB_CONFIGMAP="hub/nix"), trocken=True, k8s=cluster(antworten)))
    with pytest.raises(RuntimeError, match="nicht lesbar"):
        hub.sammeln(Kontext(umgebung=ohne_umgebung(GRAPH_HUB_CONFIG=str(tmp_path / "gibtsnicht")), trocken=True))


# ── bestand ──────────────────────────────────────────────────────────────────


def test_bestand_sammeln() -> None:
    stand = {
        "eintraege": [
            {"id": "werkzeug:graph.abhaengig", "art": "werkzeug", "name": "graph.abhaengig", "zweck": "x", "abhaengig_von": ["plugin:graph"]},
            {"id": "skript:ops/x.py", "art": "skript", "name": "x.py", "abhaengig_von": ["dienst:fyos/webapp", "dienst:fyos/gibtsnicht"]},
            {"id": "dienst:fyos/webapp", "art": "dienst", "name": "webapp"},
        ]
    }
    ctx = Kontext(umgebung={}, trocken=True, bestand_stand=stand, bekannt={"plugin:graph", "dienst:fyos/webapp"})
    knoten, kanten = bestand.sammeln(ctx)
    assert ids(knoten) == {"werkzeug:graph.abhaengig", "skript:ops/x.py"}  # Dienste gehören k8s
    ks = kanten_set(kanten)
    assert ("werkzeug:graph.abhaengig", "plugin:graph", "gehoert_zu") in ks
    assert ("skript:ops/x.py", "dienst:fyos/webapp", "nutzt") in ks
    assert not any(e.nach == "dienst:fyos/gibtsnicht" for e in kanten)
    with pytest.raises(RuntimeError):
        bestand.sammeln(Kontext(umgebung={}, trocken=True, bestand_stand={"eintraege": []}))


def test_bestand_ohne_redis_ist_aus_mit_redis_laut(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(Ausgeschaltet, match="GRAPH_BESTAND_REDIS_URL"):
        bestand.sammeln(Kontext(umgebung={}, trocken=True))
    gelesen: dict[str, Any] = {}

    class Redis:
        def __init__(self, url: str) -> None:
            gelesen["url"] = url

        def get(self, schluessel: str):
            gelesen["schluessel"] = schluessel
            return gelesen.get("antwort")

    import redis

    monkeypatch.setattr(redis.Redis, "from_url", staticmethod(lambda url, **kw: Redis(url)))
    ctx = Kontext(umgebung=ohne_umgebung(GRAPH_BESTAND_REDIS_URL="redis://beispiel:6379", GRAPH_BESTAND_SCHLUESSEL="mein:stand"), trocken=True)
    with pytest.raises(RuntimeError, match="mein:stand ist leer"):  # eingerichtet, aber kein Stand: laut
        bestand.sammeln(ctx)
    gelesen["antwort"] = json.dumps({"eintraege": [{"id": "geraet:lampe", "art": "geraet", "name": "Lampe"}]})
    knoten, _ = bestand.sammeln(ctx)
    assert ids(knoten) == {"geraet:lampe"} and gelesen["url"] == "redis://beispiel:6379"


# ── rechner ──────────────────────────────────────────────────────────────────


def _rechner_ctx(**einstellungen: str) -> Kontext:
    return Kontext(umgebung=einstellungen, trocken=True)


def test_rechner_ohne_datei_ist_aus() -> None:
    with pytest.raises(Ausgeschaltet, match="GRAPH_RECHNER_DATEN"):
        rechner.sammeln(_rechner_ctx())


def test_rechner_beispieldatei_ist_in_sich_stimmig() -> None:
    knoten, kanten = rechner.sammeln(_rechner_ctx(GRAPH_RECHNER_DATEN=str(BEISPIEL)))
    je_id = {k.id: k for k in knoten}
    assert {"rechner:k8s-agent", "rechner:modellserver", "rechner:modelltor", "speicher:werkbank/modelle", "artefakt:basis-q4", "profil:standard"} <= set(je_id)
    assert je_id["rechner:modellserver"].typ == "rechner" and je_id["rechner:modellserver"].attribute["port"] == 8080
    ks = kanten_set(kanten)
    # Dienste laufen auf dem Standard-Rechner, mit hartem Pin; ein eigener `rechner` gewinnt
    assert ("rechner:modellserver", "knoten:werkbank", "laeuft_auf") in ks
    assert ("speicher:speicherbox/messungen", "knoten:speicherbox", "liegt_auf") in ks
    assert ("rechner:modelltor", "rechner:modellserver", "braucht") in ks
    assert ("knoten:werkbank", "rechner:k8s-agent", "braucht") in ks  # der Agent trägt den Knoten
    assert ("artefakt:basis-q4", "speicher:werkbank/modelle", "liegt_in") in ks
    assert ("profil:standard", "artefakt:basis-q4", "laedt") in ks and ("profil:standard", "rechner:modellserver", "beschreibt") in ks
    assert all(e.attribute.get("pin") == "hart" for e in kanten if e.art in ("laeuft_auf", "liegt_auf"))
    # Die Datei ist Wissen mit Beleg: nichts ohne Quelle
    assert all(e.beleg for e in kanten)
    # Die Querverbindung entsteht nur, wenn `von` im Graphen bekannt ist
    assert ("plugin:modell", "rechner:modelltor", "nutzt") in ks  # ohne bekannte IDs lässt `gibt` alles durch
    _, mit_bekannten = rechner.sammeln(Kontext(umgebung=ohne_umgebung(GRAPH_RECHNER_DATEN=str(BEISPIEL)), trocken=True, bekannt={"dienst:fyos/x"}))
    assert ("plugin:modell", "rechner:modelltor", "nutzt") not in kanten_set(mit_bekannten)


def test_rechner_beispieldatei_kommt_mit_dem_paket() -> None:
    assert BEISPIEL.is_file()
    d = yaml.safe_load(BEISPIEL.read_text(encoding="utf-8"))
    assert d["version"] == 1 and {"dienste", "speicher", "artefakte", "profile", "querverbindungen", "adressen", "extern"} <= set(d)


def test_rechner_fehler_nennen_den_eintrag(tmp_path: Path) -> None:
    def lade(inhalt: str) -> Any:
        datei = tmp_path / "r.yaml"
        datei.write_text(inhalt, encoding="utf-8")
        return rechner.sammeln(_rechner_ctx(GRAPH_RECHNER_DATEN=str(datei)))

    with pytest.raises(RuntimeError, match=r"dienste Nr\. 2: name fehlt"):
        lade("dienste:\n  - {id: 'rechner:a', name: a}\n  - {id: 'rechner:b'}\n")
    with pytest.raises(RuntimeError, match="muss eine Liste sein"):
        lade("dienste: x\n")
    with pytest.raises(RuntimeError, match="Kantenart 'frisst'"):
        lade("querverbindungen:\n  - {von: 'a:1', nach: 'b:2', art: frisst}\n")
    with pytest.raises(RuntimeError, match="kein gültiges YAML"):
        lade("dienste: [unvollständig\n")
    with pytest.raises(RuntimeError, match="oberste Ebene"):
        lade("- eins\n- zwei\n")
    with pytest.raises(RuntimeError, match="nicht lesbar"):
        rechner.sammeln(_rechner_ctx(GRAPH_RECHNER_DATEN=str(tmp_path / "gibtsnicht.yaml")))
    knoten, kanten = lade("")  # eine leere Datei ist erlaubt: nichts zu übernehmen
    assert knoten == [] and kanten == []


def test_rechner_ohne_standard_rechner_keine_platzierung(tmp_path: Path) -> None:
    datei = tmp_path / "r.yaml"
    datei.write_text("dienste:\n  - {id: 'rechner:a', name: a, beleg: x}\n  - {id: 'rechner:b', name: b, rechner: 'knoten:n1', beleg: x}\n", encoding="utf-8")
    _, kanten = rechner.sammeln(_rechner_ctx(GRAPH_RECHNER_DATEN=str(datei)))
    assert kanten_set(kanten) == {("rechner:b", "knoten:n1", "laeuft_auf")}


# ── experimente ──────────────────────────────────────────────────────────────


def test_experimente_ohne_verzeichnis_ist_aus() -> None:
    with pytest.raises(Ausgeschaltet, match="EXPERIMENTE_DIR"):
        experimente.sammeln(Kontext(umgebung={}, trocken=True))


def test_experimente_sammeln(tmp_path: Path) -> None:
    verz = tmp_path / "experimente"
    verz.mkdir()
    (verz / "e1.json").write_text(
        json.dumps({"id": "e1", "titel": "basis-q4 gegen basis-q8 mit Profil standard", "frage": "mail-relay antwortet langsam?"}), encoding="utf-8"
    )
    (verz / "kaputt.json").write_text("{nicht json", encoding="utf-8")
    (verz / "liste.json").write_text("[1, 2]", encoding="utf-8")
    umgebung = ohne_umgebung(EXPERIMENTE_DIR=str(verz), GRAPH_RECHNER_DATEN=str(BEISPIEL))
    ctx = Kontext(umgebung=umgebung, trocken=True, bekannt={"dienst:fyos/mail-relay"})
    knoten, kanten = experimente.sammeln(ctx)
    assert ids(knoten) == {"experiment:e1"}
    ks = kanten_set(kanten)
    assert ("experiment:e1", "artefakt:basis-q4", "misst") in ks and ("experiment:e1", "artefakt:basis-q8", "misst") in ks
    assert ("experiment:e1", "profil:standard", "misst") in ks
    assert ("experiment:e1", "dienst:fyos/mail-relay", "betrifft") in ks
    # ohne Rechner-Datei gibt es nur die Textfunde
    _, ohne = experimente.sammeln(Kontext(umgebung=ohne_umgebung(EXPERIMENTE_DIR=str(verz)), trocken=True, bekannt={"dienst:fyos/mail-relay"}))
    assert kanten_set(ohne) == {("experiment:e1", "dienst:fyos/mail-relay", "betrifft")}


def test_experimente_eingerichtet_aber_nicht_lesbar_ist_laut(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="nicht lesbar"):
        experimente.sammeln(Kontext(umgebung=ohne_umgebung(EXPERIMENTE_DIR=str(tmp_path / "gibtsnicht")), trocken=True))
    leer = tmp_path / "leer"
    leer.mkdir()
    with pytest.raises(RuntimeError, match="keine Experimente"):
        experimente.sammeln(Kontext(umgebung=ohne_umgebung(EXPERIMENTE_DIR=str(leer)), trocken=True))


def test_experimente_artefakt_muster_kurzname_und_ausnahme() -> None:
    muster = experimente.artefakt_muster([
        {"id": "artefakt:q4"}, {"id": "artefakt:x"}, {"id": "artefakt:geheim", "experimente": False}, {"id": "artefakt:basis-q8"},
    ])
    assert set(muster) == {"artefakt:q4", "artefakt:basis-q8"}  # ein Zeichen ist zu kurz, `experimente: false` nimmt heraus
    assert muster["artefakt:q4"].search("Lauf Q4 heute") and not muster["artefakt:q4"].search("q40 und a-q4")
    assert muster["artefakt:basis-q8"].search("basis-q8 mit")


# ── Textfunde ────────────────────────────────────────────────────────────────


def test_textfunde_ganze_woerter_und_sperrliste() -> None:
    idx = _text.Index({"dienst:fyos/qdrant", "dienst:fyos/webapp", "dienst:fyos/mail-relay", "dienst:fyos/tempo", "cronjob:fyos/pg-dump-audit", "dienst:fyos/db", "rechner:modellserver", "knoten:nas"})
    assert idx.finde("Qdrant antwortet nicht") == {"dienst:fyos/qdrant"}
    assert idx.finde("mail-relay hängt") == {"dienst:fyos/mail-relay"}  # Bindestrich gehört zum Wort: `mail` allein wäre zu kurz
    assert idx.finde("das Tempo der Messung, db und nas") == set()  # gewöhnliche Wörter / zu kurz
    assert idx.finde("qdrant-data ist voll") == set()
    assert idx.finde("modellserver.service startet nicht") == {"rechner:modellserver"}
    assert idx.finde("") == set()
    # Eigene Sperrwörter (GRAPH_TEXT_SPERRE): ein Name, der in jedem Text vorkommt
    assert _text.Index({"dienst:fyos/webapp"}).finde("webapp läuft") == {"dienst:fyos/webapp"}
    assert _text.Index({"dienst:fyos/webapp"}, sperre=["WebApp"]).finde("webapp läuft") == set()


def test_text_sperre_aus_der_umgebung() -> None:
    assert Kontext(umgebung={"GRAPH_TEXT_SPERRE": " WebApp, db ,,"}).text_sperre() == frozenset({"webapp", "db"})
    assert Kontext(umgebung={}).text_sperre() == frozenset()


# ── itsm ─────────────────────────────────────────────────────────────────────


class Vorgaenge:
    """Attrappe der Vorgangs-Quelle (Form von `mcp_hub.vorgaenge.Quelle`)."""

    eingerichtet = True

    def __init__(self, tickets, beschreibungen=None, verlauf=None) -> None:
        self.tickets = tickets
        self.beschreibungen = beschreibungen or {}
        self.verlauf_je = verlauf or {}
        self.kommentare: list[tuple[str, str, str]] = []

    def liste(self, status=None, grenze=50):
        return self.tickets[:grenze]

    def offen(self, grenze=200):
        return [t for t in self.tickets if t.get("status", "new") in vorgaenge.OFFEN][:grenze]

    def holen(self, vid):
        return {"id": vid, "description": self.beschreibungen.get(vid, "")}

    def verlauf(self, vid, grenze=20):
        return self.verlauf_je.get(vid, [])

    def kommentieren(self, vid, text, actor=""):
        self.kommentare.append((vid, text, actor))


def itsm_ctx(quelle, **kw) -> Kontext:
    return Kontext(umgebung=kw.pop("umgebung", {}), trocken=True, vorgaenge_quelle=lambda: quelle, **kw)


def test_itsm_sammeln_titel_und_text() -> None:
    tickets = [
        {"id": "INC-1", "type": "INC", "status": "new", "title": "qdrant antwortet nicht", "severity": "high"},
        {"id": "PRB-2", "type": "PRB", "status": "triage", "title": "Allgemeines"},
        {"id": "CHG-3", "type": "CHG", "status": "resolved", "title": "qdrant erledigt"},  # nicht offen
    ]
    quelle = Vorgaenge(tickets, {"PRB-2": "Ursache liegt bei mail-relay und audit.recall"})
    ctx = itsm_ctx(quelle, bekannt={"dienst:fyos/qdrant", "dienst:fyos/mail-relay", "plugin:audit"})
    knoten, kanten = itsm.sammeln(ctx)
    assert ids(knoten) == {"vorgang:INC-1", "vorgang:PRB-2"}
    je_id = {k.id: k for k in knoten}
    assert je_id["vorgang:INC-1"].attribute == {"art": "INC", "status": "new", "schwere": "high", "titel": "qdrant antwortet nicht", "zustaendig": None}
    by = {(e.von, e.nach): e for e in kanten}
    assert by[("vorgang:INC-1", "dienst:fyos/qdrant")].vertrauen == itsm.FUND_TITEL
    assert by[("vorgang:PRB-2", "dienst:fyos/mail-relay")].vertrauen == itsm.FUND_TEXT
    assert ("vorgang:PRB-2", "plugin:audit") in by
    assert all(e.art == "betrifft" for e in kanten)


def test_itsm_beschreibung_aus_der_liste_wird_nicht_nachgeholt() -> None:
    class Zaehler(Vorgaenge):
        geholt = 0

        def holen(self, vid):
            Zaehler.geholt += 1
            return super().holen(vid)

    tickets = [{"id": "WB-1", "status": "new", "title": "x", "description": "mail-relay"}, {"id": "WB-2", "status": "new", "title": "y"}]
    knoten, kanten = itsm.sammeln(itsm_ctx(Zaehler(tickets), bekannt={"dienst:fyos/mail-relay"}))
    assert Zaehler.geholt == 1  # nur für den Vorgang, dessen Liste keine Beschreibung trägt
    assert kanten_set(kanten) == {("vorgang:WB-1", "dienst:fyos/mail-relay", "betrifft")}
    assert {k.id: k.attribute["art"] for k in knoten} == {"vorgang:WB-1": "WB", "vorgang:WB-2": "WB"}  # ohne `type` zählt die ID


def test_itsm_keine_offenen_vorgaenge() -> None:
    # Die Quelle antwortet, aber nichts ist offen: übernommen, die Knoten enden (nichts bekannt = nichts zu schließen)
    assert itsm.sammeln(itsm_ctx(Vorgaenge([{"id": "INC-9", "status": "closed", "title": "x"}]))) == ([], [])
    # Der Graph kennt offene Vorgänge, die Quelle aber überhaupt keinen Vorgang mehr: eher ein Lesefehler
    with pytest.raises(RuntimeError, match="keinen einzigen Vorgang"):
        itsm.sammeln(itsm_ctx(Vorgaenge([]), bekannt={"vorgang:INC-1"}))
    # Alle Vorgänge sind erledigt, die Quelle kennt sie noch: die Knoten dürfen enden
    assert itsm.sammeln(itsm_ctx(Vorgaenge([{"id": "INC-1", "status": "closed", "title": "x"}]), bekannt={"vorgang:INC-1"})) == ([], [])


def test_itsm_ohne_quelle_ist_aus_kein_fehler(ohne_vorgangsquelle) -> None:
    with pytest.raises(Ausgeschaltet, match="Vorgänge"):
        itsm.sammeln(Kontext(umgebung={}, trocken=True))
    assert itsm.nachlauf(Kontext(umgebung={"GRAPH_ITSM_KOMMENTARE": "0"}, trocken=True), None) == {"kommentare": "aus"}


class Workboard:
    """Ablage in der Form des Hub-Plugins openclaw (Grundstufe ohne Ticketdienst), mit allem, was die Quelle braucht."""

    def __init__(self) -> None:
        self.karten = [
            {"id": "WB-1a2b3c4d", "title": "mail-relay neu starten", "description": "", "status": "in_progress", "priority": "normal"},
            {"id": "INC-77", "title": "qdrant antwortet nicht", "description": "", "status": "new", "priority": "high", "created_at": (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=1)).timestamp() * 1000},
            {"id": "WB-9", "title": "erledigt", "description": "", "status": "closed", "priority": "normal"},
        ]
        self.kommentare: list[tuple[str, dict]] = []

    def ablage_liste(self, status="", grenze=50):
        return [k for k in self.karten if not status or k["status"] == status][:grenze]

    def ablage_holen(self, vid):
        return next((k for k in self.karten if k["id"] == vid), None)

    def ablage_verlauf(self, vid, grenze=20):
        return []

    def ablage_kommentieren(self, vid, koerper):
        self.kommentare.append((vid, koerper))
        return {"ok": True, "id": vid}


def test_itsm_im_workboard(ohne_vorgangsquelle) -> None:
    vorgaenge.ablage_anmelden(Workboard())
    ctx = Kontext(umgebung={}, trocken=True, bekannt={"dienst:fyos/mail-relay", "dienst:fyos/qdrant"})
    knoten, kanten = itsm.sammeln(ctx)
    assert ids(knoten) == {"vorgang:WB-1a2b3c4d", "vorgang:INC-77"}  # offene Karten; die geschlossene fehlt
    je_id = {k.id: k for k in knoten}
    assert je_id["vorgang:INC-77"].attribute["art"] == "INC" and je_id["vorgang:INC-77"].attribute["schwere"] == "high"  # priority statt severity
    ks = kanten_set(kanten)
    assert ("vorgang:WB-1a2b3c4d", "dienst:fyos/mail-relay", "betrifft") in ks and ("vorgang:INC-77", "dienst:fyos/qdrant", "betrifft") in ks


def test_itsm_im_vorgangs_dienst() -> None:
    gesehen: list[str] = []

    def aufruf(methode: str, pfad: str, koerper: Any, frist: float):
        gesehen.append(f"{methode} {pfad}")
        if pfad.startswith("/tickets?status=new"):
            return 200, {"tickets": [{"id": "INC-5", "type": "INC", "status": "new", "title": "mail-relay hängt"}]}
        if pfad.startswith("/tickets?"):
            return 200, {"tickets": []}
        if pfad == "/tickets/INC-5":
            return 200, {"ticket": {"id": "INC-5", "description": "auch qdrant"}}
        return 404, {"detail": "nein"}

    dienst = Dienst("http://tickets.example", aufruf=aufruf)
    ctx = itsm_ctx(dienst, bekannt={"dienst:fyos/mail-relay", "dienst:fyos/qdrant"})
    knoten, kanten = itsm.sammeln(ctx)
    assert ids(knoten) == {"vorgang:INC-5"}
    by = {(e.von, e.nach): e.vertrauen for e in kanten}
    assert by == {("vorgang:INC-5", "dienst:fyos/mail-relay"): itsm.FUND_TITEL, ("vorgang:INC-5", "dienst:fyos/qdrant"): itsm.FUND_TEXT}
    assert "GET /tickets/INC-5" in gesehen  # die Beschreibung kommt vom Einzelabruf


def test_itsm_dienst_aus_der_umgebung(ohne_vorgangsquelle, monkeypatch: pytest.MonkeyPatch) -> None:
    quelle = Kontext(umgebung={"FYOS_VORGAENGE_URL": "http://tickets.example/", "FYOS_VORGAENGE_KOPF": "X-Key: abc"}).vorgangs_quelle()
    assert quelle.art == "dienst" and quelle.url == "http://tickets.example" and quelle.kopf == {"X-Key": "abc"}


# ── itsm: Nachlauf ───────────────────────────────────────────────────────────


class GraphKommentar:
    """Antwortet für kommentar_text/nachlauf; `kanten` = Textfunde je Vorgang."""

    def __init__(self, kanten):
        self.kanten = kanten

    def _ausfuehren(self, sql, params=None):
        return [(nach, v) for nach, v in self.kanten.get(params[0], [])]

    def knoten_info(self, ids_):
        return {i: {"id": i, "typ": i.split(":")[0], "name": i.split("/")[-1]} for i in ids_ if i.startswith(("dienst:", "plugin:", "rechner:"))}

    def auswirkung(self, start):
        return {
            "ausgefallen": [
                {"id": "plugin:audit", "typ": "plugin", "name": "audit"},
                {"id": "rechner:modellserver", "typ": "rechner", "name": "modellserver"},
                {"id": "service:fyos/qdrant", "typ": "service", "name": "qdrant"},
            ],
            "beeintraechtigt": [{"id": "dienst:fyos/webapp", "typ": "dienst", "name": "webapp"}],
            "umgeplant": [],
            "eingeschraenkt": [],
        }


def test_itsm_kommentar_text() -> None:
    g = GraphKommentar({})
    text = itsm.kommentar_text(g, "vorgang:INC-1", [("dienst:fyos/qdrant", 0.8)])
    assert text.startswith(itsm.MARKE) and "Fällt mit aus (2): audit, modellserver" in text and "webapp" in text and "graph.auswirkung" in text
    assert "service" not in text.split("Fällt mit aus")[1].split("\n")[0]  # Services zählen nicht als „Dienst fällt aus"
    assert itsm.kommentar_text(g, "vorgang:INC-1", [("vorgang:X", 0.5)]) is None  # nichts Bekanntes


def _inc_tickets():
    jetzt = dt.datetime.now(dt.timezone.utc)
    neu = (jetzt - dt.timedelta(hours=2)).isoformat()
    alt = (jetzt - dt.timedelta(days=10)).isoformat()
    return [
        {"id": "INC-1", "type": "INC", "status": "new", "title": "x", "created_at": neu},
        {"id": "INC-2", "type": "INC", "status": "new", "title": "x", "created_at": alt},
        {"id": "INC-3", "type": "INC", "status": "new", "title": "x", "created_at": neu},
        {"id": "PRB-4", "type": "PRB", "status": "new", "title": "x", "created_at": neu},
        {"id": "INC-5", "type": "INC", "status": "new", "title": "x", "created_at": "kein Datum"},
    ]


def test_itsm_nachlauf_ist_standardmaessig_aus() -> None:
    quelle = Vorgaenge(_inc_tickets())
    graph = GraphKommentar({"vorgang:INC-1": [("dienst:fyos/qdrant", 0.8)]})
    assert itsm.nachlauf(itsm_ctx(quelle), graph) == {"kommentare": "aus"}
    assert itsm.nachlauf(itsm_ctx(quelle, umgebung={"GRAPH_ITSM_KOMMENTARE": "0"}), graph) == {"kommentare": "aus"}
    assert quelle.kommentare == []


def test_itsm_nachlauf_postet_einmal_und_nur_junge_incs() -> None:
    verlauf = {"INC-3": [{"detail": f"{itsm.MARKE} Betroffen ..."}]}
    kante = [("dienst:fyos/qdrant", 0.8)]
    graph = GraphKommentar({f"vorgang:INC-{n}": kante for n in (1, 2, 3, 5)})
    quelle = Vorgaenge(_inc_tickets(), verlauf=verlauf)
    r = itsm.nachlauf(itsm_ctx(quelle, umgebung={"GRAPH_ITSM_KOMMENTARE": "1", "GRAPH_ITSM_AKTEUR": "graph"}), graph)
    assert r == {"kommentare": ["INC-1"], "schon_kommentiert": 1}
    (vid, text, akteur), = quelle.kommentare
    assert vid == "INC-1" and akteur == "graph" and text.startswith(itsm.MARKE)
    # ohne eigenen Akteur gilt `api`
    quelle2 = Vorgaenge(_inc_tickets())
    itsm.nachlauf(itsm_ctx(quelle2, umgebung={"GRAPH_ITSM_KOMMENTARE": "ja"}), graph)
    assert {a for _, _, a in quelle2.kommentare} == {"api"}


def test_itsm_nachlauf_hoechstens_fuenf_je_lauf() -> None:
    jetzt = dt.datetime.now(dt.timezone.utc).isoformat()
    tickets = [{"id": f"INC-{n}", "type": "INC", "status": "new", "title": "x", "created_at": jetzt} for n in range(1, 9)]
    graph = GraphKommentar({f"vorgang:INC-{n}": [("dienst:fyos/qdrant", 0.8)] for n in range(1, 9)})
    quelle = Vorgaenge(tickets)
    r = itsm.nachlauf(itsm_ctx(quelle, umgebung={"GRAPH_ITSM_KOMMENTARE": "1"}), graph)
    assert len(r["kommentare"]) == itsm.KOMMENTAR_MAX == 5


def test_itsm_nachlauf_im_workboard_kommentiert_die_karte(ohne_vorgangsquelle) -> None:
    wb = Workboard()
    vorgaenge.ablage_anmelden(wb)
    graph = GraphKommentar({"vorgang:INC-77": [("dienst:fyos/qdrant", 0.8)], "vorgang:WB-1a2b3c4d": [("dienst:fyos/mail-relay", 0.8)]})
    r = itsm.nachlauf(Kontext(umgebung={"GRAPH_ITSM_KOMMENTARE": "1"}, trocken=True), graph)
    assert r["kommentare"] == ["INC-77"]  # WB-… ist kein INC
    (vid, koerper), = wb.kommentare
    assert vid == "INC-77" and koerper["actor"] == "api" and koerper["text"].startswith(itsm.MARKE)


@pytest.mark.parametrize(
    "wert,erwartet",
    [
        ("2026-10-09T08:00:00Z", dt.datetime(2026, 10, 9, 8, 0, tzinfo=dt.timezone.utc)),
        ("2026-10-09T08:00:00", dt.datetime(2026, 10, 9, 8, 0, tzinfo=dt.timezone.utc)),
        (1_760_000_000, dt.datetime.fromtimestamp(1_760_000_000, dt.timezone.utc)),
        (1_760_000_000_000, dt.datetime.fromtimestamp(1_760_000_000, dt.timezone.utc)),
        (None, None),
        ("gestern", None),
        (True, None),
    ],
)
def test_itsm_zeit_liest_text_und_epoche(wert, erwartet) -> None:
    assert itsm._zeit(wert) == erwartet
