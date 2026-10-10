"""Lesender Zugang des Einspeisers zum Kubernetes-API-Server (`graph_einspeisen/k8s_zugang.py`), ohne Cluster.

Die Antworten kommen von Attrappen in der Form, die der Client mit `_preload_content=False` liefert (`.data` mit dem
rohen JSON). Dass die Methodennamen im echten Client vorkommen, prüft ein eigener Test gegen die installierte Bibliothek."""

from __future__ import annotations

import json
from typing import Any

import pytest

from fyos_plugin_betrieb.graph_einspeisen import k8s_zugang
from fyos_plugin_betrieb.graph_einspeisen.k8s_zugang import ARTEN, CRD, K8sFehler, K8sZugang
from fyos_plugin_betrieb.graph_einspeisen.kontext import Kontext


class Daten:
    """Eine Antwort, deren `.data` in der Form `data` vorliegt (Bytes, Text, schon ein Objekt)."""

    def __init__(self, data: Any) -> None:
        self.data = data


class Roh(dict):
    """Ein Client, der die Antwort ohne `.data` gleich als Zuordnung gibt."""


def Antwort(daten: Any) -> Daten:  # noqa: N802 — wie die rohe Antwort des Clients: `.data` trägt die Bytes des JSON
    return Daten(json.dumps(daten).encode())


class Api:
    """Eine Api-Attrappe: jede Methode `list_…` zeichnet ihre Aufrufe auf und antwortet der Reihe nach aus `seiten`.

    Eine Zuordnung oder Liste wird als JSON-Antwort verpackt, `Daten` und `Roh` gehen unverändert hinaus, eine
    Ausnahme wird geworfen."""

    def __init__(self, *seiten: Any) -> None:
        self.seiten = list(seiten) or [{"items": []}]
        self.aufrufe: list[tuple[str, tuple, dict]] = []

    def __getattr__(self, name: str):
        if not name.startswith(("list_", "get_")):
            raise AttributeError(name)

        def abruf(*a: Any, **kw: Any) -> Any:
            self.aufrufe.append((name, a, kw))
            seite = self.seiten[min(len(self.aufrufe) - 1, len(self.seiten) - 1)]
            if isinstance(seite, Exception):
                raise seite
            return seite if isinstance(seite, (Daten, Roh)) else Antwort(seite)

        return abruf


class ApiFehler(Exception):
    """Wie `kubernetes.client.ApiException`: Status und Körper hängen am Fehler."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"({status})\nReason: x\nHTTP response body: {body}")
        self.status = status
        self.body = body


def zugang(**apis: Api) -> K8sZugang:
    return K8sZugang(seite=2, frist_s=7, apis=apis)


# ── Listen ───────────────────────────────────────────────────────────────────


def test_liste_blaettert_und_ergaenzt_kind() -> None:
    kern = Api(
        {"items": [{"metadata": {"name": "p1"}}, {"metadata": {"name": "p2"}}], "metadata": {"continue": "T1"}},
        {"items": [{"metadata": {"name": "p3"}}, {"kind": "Eigenes", "metadata": {"name": "p4"}}, "kein-objekt"], "metadata": {}},
    )
    pods = zugang(core=kern).liste("pods")
    assert [p["metadata"]["name"] for p in pods] == ["p1", "p2", "p3", "p4"]  # ein Eintrag, der kein Objekt ist, fällt weg
    assert [p["kind"] for p in pods] == ["Pod", "Pod", "Pod", "Eigenes"]  # die Listen tragen `kind` nicht, es wird ergänzt
    (n1, a1, k1), (n2, a2, k2) = kern.aufrufe
    assert n1 == n2 == "list_pod_for_all_namespaces" and a1 == a2 == ()
    assert k1 == {"limit": 2, "_request_timeout": 7, "_preload_content": False}
    assert k2 == {"limit": 2, "_request_timeout": 7, "_preload_content": False, "_continue": "T1"}


def test_liste_nimmt_je_art_die_richtige_methode_der_richtigen_api() -> None:
    erwartet = {
        "nodes": ("core", "list_node"),  # Knoten und Volumes gehören keinem Namensraum
        "pvs": ("core", "list_persistent_volume"),
        "pods": ("core", "list_pod_for_all_namespaces"),
        "services": ("core", "list_service_for_all_namespaces"),
        "pvcs": ("core", "list_persistent_volume_claim_for_all_namespaces"),
        "configmaps": ("core", "list_config_map_for_all_namespaces"),
        "deployments": ("apps", "list_deployment_for_all_namespaces"),
        "statefulsets": ("apps", "list_stateful_set_for_all_namespaces"),
        "daemonsets": ("apps", "list_daemon_set_for_all_namespaces"),
        "cronjobs": ("batch", "list_cron_job_for_all_namespaces"),
    }
    assert set(erwartet) == set(ARTEN)
    for art, (welche, methode) in erwartet.items():
        apis = {"core": Api(), "apps": Api(), "batch": Api()}
        assert K8sZugang(apis=apis).liste(art) == []
        gerufen = {n: [a[0] for a in api.aufrufe] for n, api in apis.items()}
        assert gerufen[welche] == [methode], (art, gerufen)
        assert sum(len(v) for v in gerufen.values()) == 1


def test_die_methodennamen_gibt_es_im_echten_client() -> None:
    client = pytest.importorskip("kubernetes.client")
    klassen = {"core": client.CoreV1Api, "apps": client.AppsV1Api, "batch": client.BatchV1Api}
    for art, (welche, einzahl, gebunden, _kind) in ARTEN.items():
        name = f"list_{einzahl}_for_all_namespaces" if gebunden else f"list_{einzahl}"
        assert hasattr(klassen[welche], name), (art, name)
    assert hasattr(client.CustomObjectsApi, "list_cluster_custom_object")
    assert hasattr(client.ApisApi, "get_api_versions")


def test_unbekannte_art_ist_ein_programmfehler() -> None:
    with pytest.raises(ValueError, match="unbekannte Art 'secrets'"):
        zugang().liste("secrets")  # Secrets liest der Einspeiser nie


def test_secrets_gibt_es_nicht_unter_den_arten() -> None:
    assert not any("secret" in a.lower() or "secret" in e[1].lower() for a, e in ARTEN.items())


# ── Flux (CRD) ───────────────────────────────────────────────────────────────

GRUPPEN = {
    "groups": [
        {"name": "apps", "preferredVersion": {"version": "v1"}},
        {"name": "kustomize.toolkit.fluxcd.io", "preferredVersion": {"version": "v1"}},
        {"name": "image.toolkit.fluxcd.io", "preferredVersion": {"version": "v1beta2"}},
    ]
}


def test_flux_objekte_in_der_version_die_der_cluster_bevorzugt() -> None:
    eigene = Api({"items": [{"metadata": {"name": "apps"}}]}, {"items": []})
    entdeckung = Api(GRUPPEN)
    z = zugang(custom=eigene, apis=entdeckung)
    ks = z.liste("kustomizations")
    assert ks == [{"metadata": {"name": "apps"}}]  # keine `kind`-Ergänzung: die CRD-Objekte tragen es selbst
    z.liste("imagepolicies")
    (n1, a1, _), (n2, a2, _) = eigene.aufrufe
    assert n1 == n2 == "list_cluster_custom_object"
    assert a1 == ("kustomize.toolkit.fluxcd.io", "v1", "kustomizations")
    assert a2 == ("image.toolkit.fluxcd.io", "v1beta2", "imagepolicies")
    assert len(entdeckung.aufrufe) == 1  # die Gruppen werden nur einmal erfragt
    assert entdeckung.aufrufe[0][2] == {"_request_timeout": 7, "_preload_content": False}


def test_flux_ist_nicht_installiert_wenn_die_gruppe_fehlt() -> None:
    z = zugang(custom=Api(), apis=Api({"groups": [{"name": "apps", "preferredVersion": {"version": "v1"}}]}))
    with pytest.raises(K8sFehler, match="helm.toolkit.fluxcd.io") as fehler:
        z.liste("helmreleases")
    assert fehler.value.status == 404  # daran erkennen die Quellen „gibt es nicht"


def test_alle_flux_arten_haben_gruppe_und_plural() -> None:
    assert set(CRD) == {"kustomizations", "helmreleases", "imagerepositories", "imagepolicies"}
    assert all(gruppe.endswith(".toolkit.fluxcd.io") and plural == art for art, (gruppe, plural) in CRD.items())


# ── Fehler ───────────────────────────────────────────────────────────────────


def test_api_fehler_werden_zu_k8s_fehler_mit_der_meldung_des_servers() -> None:
    body = json.dumps({"kind": "Status", "message": 'pods is forbidden: User "x" cannot list resource "pods"', "code": 403})
    z = zugang(core=Api(ApiFehler(403, body)))
    with pytest.raises(K8sFehler) as fehler:
        z.liste("pods")
    assert fehler.value.status == 403
    assert str(fehler.value) == 'HTTP 403: pods is forbidden: User "x" cannot list resource "pods"'
    assert isinstance(fehler.value.__cause__, ApiFehler)


def test_ein_koerper_ohne_json_wird_gekuerzt_und_ohne_zeilenumbruch_gemeldet() -> None:
    z = zugang(core=Api(ApiFehler(500, "Fehler\n" + "x" * 1000)))
    with pytest.raises(K8sFehler) as fehler:
        z.liste("nodes")
    text = str(fehler.value)
    assert text.startswith("HTTP 500: Fehler xxx") and "\n" not in text and len(text) <= len("HTTP 500: ") + 300 and text.endswith("…")


def test_netzfehler_haben_keinen_status() -> None:
    z = zugang(core=Api(OSError("Verbindung abgelehnt")))
    with pytest.raises(K8sFehler, match="Verbindung abgelehnt") as fehler:
        z.liste("nodes")
    assert fehler.value.status is None


@pytest.mark.parametrize(
    "roh,erwartet",
    [
        (Daten(b'{"items": [{"a": 1}]}'), [{"a": 1}]),
        (Daten('{"items": [{"a": 1}]}'), [{"a": 1}]),
        (Daten({"items": [{"a": 1}]}), [{"a": 1}]),
        (Roh({"items": [{"a": 1}]}), [{"a": 1}]),  # der Client ohne `.data`
        (Daten(b""), []),
        (Daten(b"[1, 2]"), []),
        (Daten(b'{"items": null}'), []),
    ],
)
def test_antwortformen(roh: Any, erwartet: list) -> None:
    assert zugang(core=Api(roh)).liste("nodes") == [{**e, "kind": "Node"} for e in erwartet]


# ── Zugang zum Cluster ───────────────────────────────────────────────────────


def test_zugang_im_pod_nimmt_den_serviceaccount_sonst_die_kubeconfig(monkeypatch: pytest.MonkeyPatch) -> None:
    config = pytest.importorskip("kubernetes.config")
    gesehen: list[Any] = []
    monkeypatch.setattr(config, "load_incluster_config", lambda: gesehen.append("serviceaccount"))
    monkeypatch.setattr(config, "load_kube_config", lambda config_file=None: gesehen.append(("kubeconfig", config_file)))
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "192.0.2.1")
    z = K8sZugang()
    assert z._api("core") is not None and z._api("apps") is not None  # geladen wird einmal
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST")
    monkeypatch.setenv("KUBECONFIG", "/pfad/zur/kubeconfig")
    K8sZugang()._api("batch")
    monkeypatch.delenv("KUBECONFIG")
    K8sZugang()._api("custom")
    assert gesehen == ["serviceaccount", ("kubeconfig", "/pfad/zur/kubeconfig"), ("kubeconfig", None)]


def test_der_kontext_nimmt_ohne_attrappe_den_echten_zugang_und_liest_jede_art_einmal(monkeypatch: pytest.MonkeyPatch) -> None:
    gelesen: list[str] = []

    class Zugang:
        def liste(self, art: str) -> list[dict[str, Any]]:
            gelesen.append(art)
            return [{"art": art}]

    monkeypatch.setattr(k8s_zugang, "K8sZugang", Zugang)
    ctx = Kontext(umgebung={})
    assert ctx.k8s_liste("pods") == [{"art": "pods"}] and ctx.k8s_liste("pods") == [{"art": "pods"}]
    ctx.k8s_liste("nodes")
    assert gelesen == ["pods", "nodes"]
