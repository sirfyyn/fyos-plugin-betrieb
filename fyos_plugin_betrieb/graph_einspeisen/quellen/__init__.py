"""Quellen des Betriebsgraphen. Jedes Modul hier ist eine Quelle (siehe `graph_einspeisen.einspeisen`).

Eine Quelle hat

    NAME = "k8s"
    def sammeln(ctx: Kontext) -> tuple[list[Knoten], list[Kante]]       # liest, schreibt nichts
    def nachlauf(ctx: Kontext, graph: Graph) -> dict | None             # optional, nach dem Abgleich

und wirft `Ausgeschaltet(grund)`, wenn sie nicht eingerichtet ist (dann wird sie übersprungen, kein Fehler).
Module, deren Name mit `_` beginnt, sind Hilfen, keine Quellen.
"""
