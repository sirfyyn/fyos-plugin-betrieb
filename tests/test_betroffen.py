"""betroffen — Impact-Analyse (portiert aus der früheren Route, 2026-09-30).

Die Tests stammen aus app/routes/test_betroffen_logik.py (CHG-2817 Takt 2)
und test_betroffen_infra.py (Takt 2b, INC-2890); geaendert ist nur der Import. Dazu kommt
die Knotenwahl, die in der früheren Route lag.
"""

from __future__ import annotations

import pytest

from fyos_plugin_betrieb.betroffen import betroffen_bfs, infra_betroffen, knoten_treffer


def _karte():
    """Minimaler Ausschnitt der echten Backup-Lane (CHG-2811-Form)."""
    return {
        "lanes": [
            {
                "titel": "Backup-Pyramide",
                "edges": [
                    ["pg-dump-gitea", "backup-pruefer"],
                    ["pg-dump-immich", "backup-pruefer"],
                    ["qdrant-snapshot", "backup-pruefer"],
                    ["backup-pruefer", "backup-rueckholprobe"],
                    ["qdrant-snapshot", "offsite-backup"],
                ],
            },
            {
                "titel": "Guardians",
                "edges": [["knoten-waechter-beta", "_meldung"]],
            },
        ],
        "nodes": {
            "pg-dump-gitea": {"lane": "backup", "kurz": "Gitea-DB dumpen"},
            "backup-pruefer": {"lane": "backup", "kurz": "Backups pruefen"},
            "backup-rueckholprobe": {"lane": "backup", "kurz": "Restore testen"},
            "offsite-backup": {"lane": "backup", "kurz": "Offsite spiegeln"},
            "_meldung": {"lane": "guardian", "kurz": "Sink"},
        },
    }


def test_downstream_transitiv():
    """pg-dump-gitea ausfallen -> pruefer UND rueckholprobe betroffen (transitiv)."""
    erg = betroffen_bfs(_karte(), ["pg-dump-gitea"])
    namen = [x["name"] for x in erg["abhaengig"]]
    assert "backup-pruefer" in namen
    assert "backup-rueckholprobe" in namen  # transitive Kante, nicht direkt


def test_downstream_mit_rueckweg():
    """Je betroffenem Knoten steht UEBER — der Rueckweg ueber die Kante."""
    erg = betroffen_bfs(_karte(), ["pg-dump-gitea"])
    pruefer = next(x for x in erg["abhaengig"] if x["name"] == "backup-pruefer")
    assert "pg-dump-gitea" in pruefer["ueber"]
    assert pruefer["lane"] == "backup"


def test_upstream_liefert_speiser():
    """backup-pruefer fragen: wer speist mich? Beide Dumps."""
    erg = betroffen_bfs(_karte(), ["backup-pruefer"])
    speiser = [x["name"] for x in erg["speist"]]
    assert set(speiser) == {"pg-dump-gitea", "pg-dump-immich", "qdrant-snapshot"}


def test_kein_selfloop_keine_doppelten():
    """BFS terminiert und liefert jeden Knoten genau einmal (Seen-Set)."""
    erg = betroffen_bfs(_karte(), ["qdrant-snapshot"])
    namen = [x["name"] for x in erg["abhaengig"]]
    assert len(namen) == len(set(namen))
    assert "qdrant-snapshot" not in namen


def test_lanes_betroffen_angenreichert():
    erg = betroffen_bfs(_karte(), ["knoten-waechter-beta"])
    assert erg["lanes_betroffen"] == ["Guardians"]


def test_knoten_treffer_exakt_und_teilstring():
    assert knoten_treffer(_karte(), "Backup-Pruefer") == ["backup-pruefer"]
    assert knoten_treffer(_karte(), "backup") == ["backup-pruefer", "backup-rueckholprobe", "offsite-backup"]


def test_knoten_treffer_unbekannt_nennt_aehnliche():
    with pytest.raises(ValueError, match="nicht in der Prozesskarte.*backup-pruefer"):
        knoten_treffer(_karte(), "backupx-gibtsnicht")


# ── Infra-Ebene ──────────────────────────────────────────────────────────────
def _nodes():
    return {
        "items": [
            {
                "metadata": {"name": "beta", "labels": {"fyos-tier": "t1-gpu"}},
                "status": {"conditions": [{"type": "Ready", "status": "False"}]},
            },
            {
                "metadata": {"name": "alpha", "labels": {"fyos-tier": "t2-state"}},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
            {
                "metadata": {"name": "gamma", "labels": {"fyos-tier": "t3-edge"}},
                "status": {"conditions": [{"type": "Ready", "status": "True"}]},
            },
        ]
    }


def _deploys():
    def d(name, spec):
        return {"metadata": {"name": name}, "spec": {"template": {"spec": spec}}}

    return {
        "items": [
            d("gitlab-runner", {"nodeSelector": {"kubernetes.io/hostname": "beta"}}),
            d("gitea-act-runner", {"nodeSelector": {"kubernetes.io/hostname": "beta"}}),
            d("tickets", {"nodeSelector": {"fyos-tier": "t2-state"}}),
            d("castor", {"nodeName": "alpha"}),
            d("frigate", {"nodeSelector": {"kubernetes.io/hostname": "gamma"}}),
        ]
    }


def test_beta_ausfall_zeigt_harte_pins():
    """Die INC-2890-Kernfrage: was steht, wenn beta weg ist?"""
    erg = infra_betroffen(_nodes(), _deploys(), von="beta")
    gepinnt = [w["name"] for w in erg["gepinnte_workloads"]]
    assert "gitlab-runner" in gepinnt
    assert "gitea-act-runner" in gepinnt
    assert erg["node_ready"] is False
    assert erg["ketten_hinweis"] is not None and "keinen Ausweich" in erg["ketten_hinweis"]


def test_tier_mit_mehreren_nodes_ist_weich():
    """Tier-Selector mit mehreren Nodes: Umzug moeglich, kein harter Ausfall.

    Im echten Cluster ist t2-state = nur alpha — dann ist alles hart. Der
    weiche Fall braucht ein Tier mit >=2 Nodes (Fixture: delta dazu)."""
    nodes = _nodes()
    nodes["items"].append(
        {
            "metadata": {"name": "delta", "labels": {"fyos-tier": "t2-state"}},
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }
    )
    erg = infra_betroffen(nodes, _deploys(), von="alpha")
    hart = [w["name"] for w in erg["gepinnte_workloads"]]
    weich = [w["name"] for w in erg["weiche_treffer"]]
    assert "castor" in hart  # nodeName = hart
    assert "tickets" in weich  # Tier passt jetzt auf 2 Nodes
    assert "gitlab-runner" not in hart  # nicht gepinnt auf alpha


def test_dienst_frage_liefert_node():
    """Umgekehrte Frage: wo laeuft der gitlab-runner (nur)?"""
    erg = infra_betroffen(_nodes(), _deploys(), von="gitlab-runner")
    assert erg["ist_node"] is False
    ziel = erg["laeuft_auf"][0]
    assert ziel["art"] == "hart" and ziel["node"] == "beta"


def test_cronjob_pins_erkannt():
    """CronJob-Pins werden genauso erkannt wie Deployment-Pins.

    Echter Cluster: graph-rebuild hat fyos-tier=t2-state = nur alpha
    -> hart gepinnt (INC-2890-Klasse)."""
    cj = {
        "items": [
            {
                "metadata": {"name": "graph-rebuild"},
                "spec": {
                    "jobTemplate": {
                        "spec": {"template": {"spec": {"nodeSelector": {"fyos-tier": "t2-state"}}}}
                    }
                },
            }
        ]
    }
    erg = infra_betroffen(_nodes(), _deploys(), cronjobs_json=cj, von="alpha")
    assert any(
        w["name"] == "graph-rebuild" and w["art"] == "CronJob" for w in erg["gepinnte_workloads"]
    )


def test_lauffaehige_pods_je_node():
    pods = {
        "items": [
            {
                "metadata": {
                    "name": "tickets-abc",
                    "ownerReferences": [{"kind": "ReplicaSet", "name": "tickets-7967d76f57"}],
                },
                "spec": {"nodeName": "alpha"},
                "status": {"phase": "Running"},
            },
            {
                "metadata": {
                    "name": "frigate-xyz",
                    "ownerReferences": [{"kind": "DaemonSet", "name": "frigate"}],
                },
                "spec": {"nodeName": "gamma"},
                "status": {"phase": "Running"},
            },
        ]
    }
    erg = infra_betroffen(_nodes(), _deploys(), pods_json=pods, von="alpha")
    assert erg["laufende_pods"][0]["besitzer"] == "tickets"
    assert erg["laufende_pods"][0]["phase"] == "Running"
