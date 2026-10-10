---
name: fyos-git-remotes
description: Use when im Repo gepusht wird, mehrere Remotes im Spiel sind oder Force-Pushes auftreten.
---

# Git-Remotes — Verteilung, Branch-Schutz und die Force-Push-Falle

Mehrere Remotes, eine Wahrheit, und Agenten, die anders schreiben als Menschen. Wer das Raster nicht kennt, jagt
Phantom-Bugs.

## Das Raster (Beispiel; die Namen und Adressen stehen in deinem Klon: `git remote -v`)

```
origin  = https://git.example.org/team/projekt.git   (Gitea, die GitOps-Quelle)
          + zweite push-URL: ein Spiegel (z. B. GitLab)
github  = https://github.com/team/projekt.git        (öffentliches Archiv, optional)
```

Flux (oder dein GitOps-Werkzeug) zieht nur vom Git-Server der Wahrheit. Spiegel sind Sicherungen und laufen zeitgesteuert.

## Die zwei regelmäßigen Missverständnisse

### 1. „Push origin ging durch" beweist nichts über den Git-Server

Trägt `origin` zwei push-URLs, bedient `git push origin main` beide. Lehnt der Server der Wahrheit den Push ab (Schutz),
kann git trotzdem „up-to-date" melden, weil die andere URL schon aktuell ist. **Den Stand des Git-Servers immer separat
lesen:**

```bash
git ls-remote https://git.example.org/team/projekt.git main   # die Wahrheit
# Push gezielt an den Git-Server, wenn er fehlt:
git push https://git.example.org/team/projekt.git main:main
```

### 2. Agenten, die über die Contents-API committen, schreiben anders

Ein Agent ohne Klon liest Datei und `sha` und schreibt später mit diesem `sha` zurück. Landet dazwischen ein fremder
Commit, baut der Server den neuen Commit auf dem alten Elter auf: der Branch-Ref springt zurück, jeder Fetch meldet
`+ … (forced update)`. Abhilfe: bei 409 mit frischem `sha` wiederholen.

## Rettungsmuster bei Force-Push-Verlust

```bash
git fetch origin                       # zeigt forced update
git merge origin/main -m "Merge origin/main (Rettung)"
git push origin main                   # Fast-Forward wiederherstellen
```

Merge, nie rebase, wenn mehrere Remotes und Agenten parallel schreiben. Lehnt der Schutz den eigenen Push ab: erst den
Stand des Servers mergen, dann pushen; ein non-fast-forward eigener Commits sieht für den Schutz wie ein Force-Push aus.

## Branch-Schutz auf main

Richtig: `enable_push: true` (Pushes frei), `enable_force_push: false` (Force serverseitig verboten). Falle beim Setzen:
per API-POST ohne `enable_push` ist die Push-Whitelist leer, dann darf niemand pushen. Ändern per PATCH:

```bash
curl -X PATCH "https://git.example.org/api/v1/repos/team/projekt/branch_protections/main" \
  -H "Authorization: token $TOKEN" -H "Content-Type: application/json" \
  -d '{"enable_push":true,"enable_force_push":false}'
```

Die API ist Gitea v1. Commit-Status: `/api/v1/repos/team/projekt/commits/<sha>/status`.

## Wichtig zu wissen

- Automatische Commits (Digest-Nachführung, Bildautomatik) entstehen von den Automatiken selbst; niemals von Hand
  „aufräumen".
- CI-Status „pending" kann Runner-Knappheit sein (ein Runner = eine sequentielle Queue): die Logs des Runners zeigen,
  was wirklich läuft, bevor man einen Build für hängend hält.
