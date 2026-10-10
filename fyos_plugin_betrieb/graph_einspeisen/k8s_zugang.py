"""Lesender Zugang zum Kubernetes-API-Server für den Einspeiser, ohne `kubectl`.

Im Cluster mit dem ServiceAccount des Pods (`KUBERNETES_SERVICE_HOST`), sonst mit der kubeconfig (`KUBECONFIG` oder
Vorgabe des Clients). Es wird nur gelesen (`list`), nie ein Secret. Antworten kommen roh als JSON, wie `kubectl -o json`:
das ist schnell (kein Modellbau für Hunderte Pods), und die Quellen und ihre Tests arbeiten mit demselben Rohstand.

Die Listen einzelner Objekte tragen von sich aus kein `kind`; es wird ergänzt, weil die Quellen daran Deployment,
StatefulSet und DaemonSet unterscheiden. Flux-Objekte (CRDs) werden in der Version gelesen, die der Cluster als
bevorzugt meldet (Discovery), weil sich die Version mit Flux-Ständen ändert.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

#: art → (API, Einzahl für den Methodennamen, namensraumgebunden, kind)
ARTEN: dict[str, tuple[str, str, bool, str]] = {
    "nodes": ("core", "node", False, "Node"),
    "pods": ("core", "pod", True, "Pod"),
    "services": ("core", "service", True, "Service"),
    "pvcs": ("core", "persistent_volume_claim", True, "PersistentVolumeClaim"),
    "pvs": ("core", "persistent_volume", False, "PersistentVolume"),
    "configmaps": ("core", "config_map", True, "ConfigMap"),
    "deployments": ("apps", "deployment", True, "Deployment"),
    "statefulsets": ("apps", "stateful_set", True, "StatefulSet"),
    "daemonsets": ("apps", "daemon_set", True, "DaemonSet"),
    "cronjobs": ("batch", "cron_job", True, "CronJob"),
}
#: Flux-Objekte: art → (API-Gruppe, Plural)
CRD: dict[str, tuple[str, str]] = {
    "kustomizations": ("kustomize.toolkit.fluxcd.io", "kustomizations"),
    "helmreleases": ("helm.toolkit.fluxcd.io", "helmreleases"),
    "imagerepositories": ("image.toolkit.fluxcd.io", "imagerepositories"),
    "imagepolicies": ("image.toolkit.fluxcd.io", "imagepolicies"),
}


class K8sFehler(RuntimeError):
    """Der API-Server antwortet nicht, verweigert das Lesen oder kennt die Art nicht (`status` 404)."""

    def __init__(self, text: str, status: int | None = None) -> None:
        super().__init__(text)
        self.status = status


def _kurz(text: Any, n: int = 300) -> str:
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


class K8sZugang:
    """Liest Listen vom API-Server. `liste(art)` mit einer Art aus `ARTEN` oder `CRD`."""

    def __init__(self, *, frist_s: int = 60, seite: int = 500, apis: dict[str, Any] | None = None) -> None:
        self.frist_s = frist_s
        self.seite = seite
        self._apis = apis
        self._gruppen: dict[str, str] | None = None

    def _api(self, welche: str) -> Any:
        if self._apis is None:
            from kubernetes import client, config  # Abhängigkeit des Pakets (kubernetes>=30)

            if os.getenv("KUBERNETES_SERVICE_HOST"):
                config.load_incluster_config()
            else:
                config.load_kube_config(config_file=os.getenv("KUBECONFIG") or None)
            basis = client.ApiClient()
            self._apis = {
                "core": client.CoreV1Api(basis),
                "apps": client.AppsV1Api(basis),
                "batch": client.BatchV1Api(basis),
                "custom": client.CustomObjectsApi(basis),
                "apis": client.ApisApi(basis),
            }
        return self._apis[welche]

    @staticmethod
    def _json(fn: Callable[..., Any], *a: Any, **kw: Any) -> dict[str, Any]:
        try:
            antwort = fn(*a, _preload_content=False, **kw)
        except Exception as e:  # noqa: BLE001 — ApiException und Netzfehler einheitlich
            status = getattr(e, "status", None)
            text = getattr(e, "body", None) or str(e)
            try:
                text = json.loads(text).get("message") or text
            except (TypeError, ValueError, AttributeError):
                pass
            raise K8sFehler(f"HTTP {status}: {_kurz(text)}" if status else _kurz(text), status) from e
        daten = getattr(antwort, "data", antwort)
        if isinstance(daten, (bytes, bytearray)):
            daten = daten.decode("utf-8", "replace")
        if isinstance(daten, str):
            daten = json.loads(daten) if daten else {}
        return daten if isinstance(daten, dict) else {}

    def _version(self, gruppe: str) -> str:
        if self._gruppen is None:
            d = self._json(self._api("apis").get_api_versions, _request_timeout=self.frist_s)
            self._gruppen = {
                str(g.get("name")): str((g.get("preferredVersion") or {}).get("version") or "")
                for g in d.get("groups") or []
                if isinstance(g, dict)
            }
        version = self._gruppen.get(gruppe)
        if not version:
            raise K8sFehler(f"die API-Gruppe {gruppe} gibt es im Cluster nicht", 404)
        return version

    def liste(self, art: str) -> list[dict[str, Any]]:
        """Alle Objekte einer Art, über alle Namensräume."""
        kind = None
        if art in CRD:
            gruppe, plural = CRD[art]
            version = self._version(gruppe)
            api = self._api("custom")

            def abruf(**kw: Any) -> Any:
                return api.list_cluster_custom_object(gruppe, version, plural, **kw)

        elif art in ARTEN:
            welche, einzahl, gebunden, kind = ARTEN[art]
            api = self._api(welche)
            abruf = getattr(api, f"list_{einzahl}_for_all_namespaces" if gebunden else f"list_{einzahl}")
        else:
            raise ValueError(f"unbekannte Art {art!r}; bekannt: {sorted([*ARTEN, *CRD])}")
        items: list[dict[str, Any]] = []
        weiter = ""
        while True:
            kw: dict[str, Any] = {"limit": self.seite, "_request_timeout": self.frist_s}
            if weiter:
                kw["_continue"] = weiter
            d = self._json(abruf, **kw)
            items += [i for i in d.get("items") or [] if isinstance(i, dict)]
            weiter = str((d.get("metadata") or {}).get("continue") or "")
            if not weiter:
                break
        if kind:
            for i in items:
                i.setdefault("kind", kind)
        return items
