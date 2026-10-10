"""graph_einspeisen — der Einspeiser des Betriebsgraphen (Konsolenskript `fyos-graph-einspeisen`).

Er liest die Quellen (Cluster, Flux, Caddy, Hub, Bestandsbuch, eigene Rechner, Experimente, Vorgänge) und gleicht sie mit
der Graph-Datenbank ab; das Hub-Plugin `graph` (`fyos_plugin_betrieb.graph`) liest sie. Aufruf und Quellen:
`einspeisen.py`, Einrichtung der Datenbank: `db_einrichten.py`, Quellen: Unterpaket `quellen`.

Dieses Paket importiert beim Laden nichts Schweres (kein Kubernetes-Client, kein Postgres-Treiber); beides kommt erst
beim ersten Zugriff.
"""
