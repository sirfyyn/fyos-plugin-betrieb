---
name: fyos-rollout-verifikation
description: Use wenn nach Bauwellen "ist es live?" gefragt ist (Rollout beweisen statt behaupten).
---

# Rollout-Verifikation — beweisen statt behaupten

Ein Dev-Server ist Bau. Bis jeder Schritt bewiesen ist, ist nichts live.

## Ablauf (Reihenfolge fest, je Schritt ein Beweis)

1. **CI-Queue zuerst**: die Git-API (`/commits/<sha>/statuses`) je Head-Commit abfragen. `pending` heißt: nicht live,
   egal wie alt der Commit ist. Ein einzelner Runner arbeitet sequenziell; bei einem Runner-Wechsel können Dutzende Jobs
   anstehen oder ein Job geht verloren („Build lief nie").
2. **Deploy-Stand**: das Image-Digest des Deployments mit dem letzten Push der Bildautomatik vergleichen. Ist das Digest
   nicht der Build des Head-Commits, hinkt der Rollout nach: nie „live" sagen. Die Startzeit des Pods zeigt den letzten
   Roll.
3. **Bedientes Artefakt**: den Browser gegen die echte Adresse (nicht den Dev-Server) richten. Ohne Sitzung kommt eine
   Weiterleitung zur Anmeldung: das ist korrekt, kein Fehler.
4. **Je Feature ein DOM-Beweis**: ein konkreter Selektor mit erwartetem Inhalt. Fehlt er, ist es nicht deployt: Ursache
   suchen statt behaupten.

## Bewiesene Stolpersteine dieser Kette

- **Eine Flux-Kustomization blockiert gesamt**, wenn einem YAML-Dokument die `apiVersion` fehlt. Vorher mit
  `yaml.safe_load_all` prüfen: „ohne apiVersion" muss 0 sein. Symptom: die Kustomization ist `Ready=False`.
- **NetworkPolicy default-deny**: ein neuer Daemon braucht eine eigene Egress-Policy, sonst „Connection refused" im
  Pod-Log.
- **Client-Timeout kürzer als der Long-Poll**: hält eine Gegenstelle eine Anfrage 25 s offen, muss der Client-Deckel
  darüber liegen, sonst endlose Timeouts.
- **Die Bildautomatik pusht den alten Digest**, wenn der Build noch lief: der nächste Zyklus holt nach. Nicht eingreifen,
  nur beobachten.

## Nach der Bauwelle

Eine Landkarte für den Betreiber: ein Satz je Neuerung und wo im UI sie zu finden ist. Neue Flächen immer in der
Registry der Oberfläche eintragen, sonst sieht niemand sie live.
