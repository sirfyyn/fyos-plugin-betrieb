"""mcp_hub.plugins.git_unified — Multi-Remote Git Plugin (S-MCP-HUB.1.8).

Koordiniert 3 Remotes (Namen wie im Repo-Klon konfiguriert, Vorgabe gitea, origin, github):

    primary:  gitea  (z. B. https://git.example.org/team/projekt.git)
    mirror-1: origin (z. B. ein GitLab-Spiegel)
    mirror-2: github (z. B. ein öffentliches Archiv)

Ohne Repo-Klon (FYOS_REPO_PATH oder `repo_path` in der Konfiguration) ist das Plugin nicht eingerichtet: lesende Werkzeuge
liefern einen Zustand mit dem Weg zum Einrichten, schreibende `ok: false`.

Five MVP tools (subprocess-based, kein External-HTTP):

    git.status_consensus()       — current-HEAD + unpushed counts per remote
    git.push_all(branch, dryRun) — push branch to all 3 remotes (parallel if possible)
    git.log_unified(since, limit)— combined git log with remote-sync-markers
    git.tag_broadcast(tag, message) — create tag + push to all 3
    git.sync_repair(strategy)    — re-sync out-of-date mirror from primary

Credentials: re-used from existing `.git/config` (gitea-token embedded, github
GPT embedded, gitlab SSH-agent). Hub-Plugin subprocess runs in repo cwd.

Error Surface:
    - plugin_error   — subprocess exit!=0
    - not_found      — unknown tool
"""

from __future__ import annotations

import logging
import os
import subprocess
from typing import Any

from . import _umgebung
from mcp_hub.plugin import (
    HubPlugin,
    ToolDescriptor,
    build_dry_run_result,
    is_dry_run,
)

logger = logging.getLogger(__name__)

DEFAULT_REMOTES = ["gitea", "origin", "github"]  # origin = gitlab per FYOS-convention
DEFAULT_TIMEOUT = 60.0
EINRICHTEN = (
    "Setze FYOS_REPO_PATH (Pfad des Repo-Klons, den das Plugin ansprechen soll) oder `repo_path` in der Konfiguration "
    "des Plugins."
)


class GitUnifiedPlugin(HubPlugin):
    name = "git"
    description = (
        "Git plugin over the repo clone FYOS_REPO_PATH (a flat clone taken at pod start can lag behind main). "
        "status/log/push/tag/sync operations; not configured without a repo path."
    )
    version = "0.1.0"

    config: dict[str, Any]
    repo_path: str = ""
    schreibbar: bool = False
    remotes: list[str]
    timeout: float

    def register(self) -> None:
        cfg = getattr(self, "config", {}) or {}
        self.config = cfg
        self.repo_path = str(cfg.get("repo_path") or _umgebung.lesen("FYOS_REPO_PATH"))
        self.remotes = list(cfg.get("remotes") or DEFAULT_REMOTES)
        self.timeout = float(cfg.get("timeout_sec") or DEFAULT_TIMEOUT)
        # Audit 2026-10-02: Im Hub-Pod kann der Klon read-only eingehaengt sein (flacher Klon beim
        # Pod-Start). push_all/tag_broadcast/sync_repair scheiterten dort still bzw. versprachen,
        # was sie nicht koennen. Schreibbar heisst: das Repo-Verzeichnis ist beschreibbar
        # (access(2) meldet EROFS auf einem read-only-Mount).
        if "schreibbar" in cfg:
            self.schreibbar = bool(cfg["schreibbar"])
        else:
            self.schreibbar = bool(self.repo_path) and os.access(self.repo_path, os.W_OK)

    def _schreib_ablehnung(self, werkzeug: str) -> dict[str, Any] | None:
        """Klare Ablehnung statt stillem Scheitern, wenn die Arbeitskopie nicht schreibbar ist."""
        if self.schreibbar:
            return None
        return {
            "ok": False,
            "error": (
                f"git.{werkzeug} ist hier nicht verfuegbar: die Arbeitskopie {self.repo_path} ist "
                "schreibgeschuetzt (flacher Klon beim Pod-Start, read-only eingehaengt). Pushen, "
                "Taggen und Spiegel-Reparatur laufen ueber den Mac bzw. die CI."
            ),
            "kind": "nicht_verfuegbar",
        }

    def init(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def _run_git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        """Subprocess git command wrapper."""
        cmd = ["git", "-C", self.repo_path, *args]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=self.timeout,
            check=check,
        )

    # --- tool descriptors --------------------------------------------------

    def describe_tools(self) -> list[ToolDescriptor]:
        nur_lesen = (
            ""
            if getattr(self, "schreibbar", True)
            else "NICHT VERFUEGBAR hier: Arbeitskopie schreibgeschuetzt — lehnt mit Begruendung ab. "
        )
        return [
            ToolDescriptor(
                name="status_consensus",
                description=(
                    "Vergleicht HEAD der Arbeitskopie mit dem ECHTEN main jedes Remotes "
                    "(ls-remote, kein veralteter Tracking-Ref). Remotes, die nicht erreichbar "
                    "oder nicht eingerichtet sind, stehen als bekannt=false da; all_synced ist "
                    "dann null statt geraten."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                tags=["git", "read"],
            ),
            ToolDescriptor(
                name="push_all",
                description=nur_lesen
                + "Push branch (default=main) to all configured remotes. Sequential with "
                "per-remote success-status. Supports dryRun.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "branch": {"type": "string", "default": "main"},
                        "dryRun": {"type": "boolean", "default": False},
                    },
                    "additionalProperties": False,
                },
                tags=["git", "write"],
            ),
            ToolDescriptor(
                name="log_unified",
                description=(
                    "Letzte `limit` Commits (Vorgabe 10) der Arbeitskopie mit SHA, Autor, Betreff. "
                    "Im Hub-Pod flacher Klon (--depth 1): nur der Stand beim Pod-Start."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "default": 10},
                    },
                    "additionalProperties": False,
                },
                tags=["git", "read"],
            ),
            ToolDescriptor(
                name="tag_broadcast",
                description=nur_lesen + "Create annotated tag + push to all configured remotes.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "tag": {"type": "string"},
                        "message": {"type": "string"},
                        "dryRun": {"type": "boolean", "default": False},
                    },
                    "required": ["tag", "message"],
                    "additionalProperties": False,
                },
                tags=["git", "write"],
            ),
            ToolDescriptor(
                name="sync_repair",
                description=nur_lesen + "Re-sync an out-of-date mirror from primary. Strategy: "
                "'force-from-primary' fast-forwards mirror to primary's HEAD.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "target_remote": {"type": "string", "description": "Mirror to repair"},
                        "strategy": {
                            "type": "string",
                            "enum": ["force-from-primary"],
                            "default": "force-from-primary",
                        },
                        "dryRun": {"type": "boolean", "default": True},
                    },
                    "required": ["target_remote"],
                    "additionalProperties": False,
                },
                tags=["git", "write", "destructive"],
            ),
        ]

    # --- call dispatch -----------------------------------------------------

    def call(self, method: str, args: dict[str, Any]) -> Any:
        if not self.repo_path:  # kein Repo-Klon: nicht eingerichtet, kein Fehler
            if method in ("push_all", "tag_broadcast", "sync_repair"):
                return _umgebung.abgelehnt("Das Git-Plugin", EINRICHTEN)
            return _umgebung.zustand("Das Git-Plugin", EINRICHTEN)
        # Hub-level dry-run sentinel (Pattern H19 / Mythos P17). The plugin
        # already exposes a tool-internal `dryRun` parameter for git itself
        # (forwarded to `git push --dry-run`); the `__dry_run__` envelope is
        # a different layer — Hub-side pre-action plan with side-effect listing,
        # no subprocess invocation at all.
        if method == "status_consensus":
            return self._tool_status_consensus()
        if method in ("push_all", "tag_broadcast", "sync_repair"):
            ablehnung = self._schreib_ablehnung(method)
            if ablehnung is not None:
                return ablehnung
        if method == "push_all":
            if is_dry_run(args):
                return self._dry_run_push_all(args)
            return self._tool_push_all(args.get("branch", "main"), bool(args.get("dryRun", False)))
        if method == "log_unified":
            return self._tool_log_unified(int(args.get("limit", 10)))
        if method == "tag_broadcast":
            if is_dry_run(args):
                return self._dry_run_tag_broadcast(args)
            return self._tool_tag_broadcast(
                args["tag"], args["message"], bool(args.get("dryRun", False))
            )
        if method == "sync_repair":
            if is_dry_run(args):
                return self._dry_run_sync_repair(args)
            return self._tool_sync_repair(
                args["target_remote"],
                args.get("strategy", "force-from-primary"),
                bool(args.get("dryRun", True)),
            )
        raise ValueError(f"unknown tool: {method}")

    # --- dry-run plan-builders (Pattern H19 / Mythos P17) ------------------

    def _dry_run_push_all(self, args: dict[str, Any]) -> dict[str, Any]:
        """Plan a git.push_all without invoking subprocess."""
        branch = str(args.get("branch", "main"))
        return build_dry_run_result(
            would_execute=(
                f"push branch {branch!r} sequentially to all "
                f"{len(self.remotes)} remotes ({', '.join(self.remotes)})"
            ),
            expected_side_effects=[
                f"git-remote/{remote}: push refs/heads/{branch}" for remote in self.remotes
            ],
            estimated_cost=len(self.remotes),
            plugin=self.name,
            method="push_all",
            args_preview={"branch": branch, "remotes": list(self.remotes)},
        )

    def _dry_run_tag_broadcast(self, args: dict[str, Any]) -> dict[str, Any]:
        """Plan a git.tag_broadcast without invoking subprocess."""
        tag = str(args.get("tag", ""))
        message = str(args.get("message", ""))
        if not tag:
            raise ValueError("git.tag_broadcast dry-run requires non-empty 'tag'")
        side_effects: list[str] = [f"local-repo: create annotated tag {tag!r}"]
        for remote in self.remotes:
            side_effects.append(f"git-remote/{remote}: push tag {tag!r}")
        return build_dry_run_result(
            would_execute=(
                f"create annotated tag {tag!r} (message: "
                f"{(message[:60] + '...') if len(message) > 60 else message!r}) "
                f"and push to all {len(self.remotes)} remotes"
            ),
            expected_side_effects=side_effects,
            estimated_cost=1 + len(self.remotes),  # 1 create + N pushes
            plugin=self.name,
            method="tag_broadcast",
            args_preview={"tag": tag, "remotes": list(self.remotes)},
        )

    def _dry_run_sync_repair(self, args: dict[str, Any]) -> dict[str, Any]:
        """Plan a git.sync_repair without invoking subprocess."""
        target_remote = str(args.get("target_remote", ""))
        if not target_remote:
            raise ValueError("git.sync_repair dry-run requires 'target_remote'")
        if target_remote not in self.remotes:
            raise ValueError(f"unknown remote: {target_remote}")
        strategy = str(args.get("strategy", "force-from-primary"))
        return build_dry_run_result(
            would_execute=(
                f"force-sync mirror {target_remote!r} from primary HEAD "
                f"(strategy={strategy}, --force-with-lease)"
            ),
            expected_side_effects=[
                f"git-remote/{target_remote}: force-with-lease push main "
                "(rewrites remote history if mirror has unseen commits would "
                "be aborted by lease check)",
            ],
            estimated_cost=1,
            plugin=self.name,
            method="sync_repair",
            args_preview={"target_remote": target_remote, "strategy": strategy},
        )

    # --- tool implementations ---------------------------------------------

    def _tool_status_consensus(self) -> dict[str, Any]:
        """HEAD gegen den echten main der Remotes (Audit 2026-10-02).

        Vorher: `log <remote>/main..HEAD` gegen den Tracking-Ref des Klons — im Hub-Pod
        derselbe Stand wie HEAD (flacher Klon), also immer „synchron", auch wenn main laengst
        weiter war. Jetzt `ls-remote` (nur lesend, braucht keinen Schreibzugriff).
        """
        head = self._run_git("rev-parse", "HEAD").stdout.strip()
        try:
            vorhanden = set(self._run_git("remote", check=False).stdout.split())
        except Exception:  # noqa: BLE001
            vorhanden = set()
        remotes: dict[str, Any] = {}
        for remote in self.remotes:
            if remote not in vorhanden:
                remotes[remote] = {
                    "bekannt": False,
                    "grund": "Remote in der Arbeitskopie nicht eingerichtet",
                }
                continue
            try:
                res = self._run_git("ls-remote", remote, "refs/heads/main", check=False)
            except Exception as exc:  # noqa: BLE001
                remotes[remote] = {"bekannt": False, "grund": f"{type(exc).__name__}: {exc}"[:200]}
                continue
            zeile = (res.stdout or "").strip().split()
            if res.returncode != 0 or not zeile:
                grund = (res.stderr or "").strip()[-200:] or "kein refs/heads/main"
                remotes[remote] = {"bekannt": False, "grund": grund}
                continue
            entfernt = zeile[0]
            remotes[remote] = {
                "bekannt": True,
                "main": entfernt,
                "gleich_wie_head": entfernt == head,
            }
        unbekannt = sorted(r for r, v in remotes.items() if not v.get("bekannt"))
        bekannte = [v for v in remotes.values() if v.get("bekannt")]
        all_synced: bool | None
        if not bekannte:
            all_synced = None
        elif any(not v["gleich_wie_head"] for v in bekannte):
            all_synced = False
        else:
            all_synced = None if unbekannt else True
        out: dict[str, Any] = {
            "head": head,
            "remotes": remotes,
            "all_synced": all_synced,
            "unbekannt": unbekannt,
        }
        if all_synced is False:
            out["hinweis"] = (
                "HEAD weicht vom main des Remotes ab. Im Hub-Pod heisst das meist: der flache Klon "
                "vom Pod-Start hinkt hinterher (Abstand ohne Historie nicht zaehlbar)."
            )
        return out

    def _tool_push_all(self, branch: str, dry_run: bool) -> dict[str, Any]:
        results: dict[str, Any] = {}
        for remote in self.remotes:
            args = ["push", remote, branch]
            if dry_run:
                args.append("--dry-run")
            try:
                res = self._run_git(*args, check=False)
                results[remote] = {
                    "returncode": res.returncode,
                    "stdout": res.stdout.strip()[-200:],
                    "stderr": res.stderr.strip()[-200:],
                    "ok": res.returncode == 0,
                }
            except Exception as exc:  # noqa: BLE001
                results[remote] = {"error": str(exc), "ok": False}
        successes = sum(1 for r in results.values() if isinstance(r, dict) and r.get("ok"))
        return {
            "branch": branch,
            "dryRun": dry_run,
            "results": results,
            "success_count": successes,
            "total_remotes": len(self.remotes),
        }

    def _tool_log_unified(self, limit: int) -> list[dict[str, Any]]:
        res = self._run_git("log", f"-{limit}", "--format=%H%x09%an%x09%s")
        entries: list[dict[str, Any]] = []
        for line in res.stdout.splitlines():
            parts = line.split("\t")
            if len(parts) >= 3:
                entries.append(
                    {
                        "sha": parts[0],
                        "author": parts[1],
                        "message": parts[2],
                    }
                )
        return entries

    def _tool_tag_broadcast(self, tag: str, message: str, dry_run: bool) -> dict[str, Any]:
        if dry_run:
            return {
                "tag": tag,
                "dryRun": True,
                "would_create": True,
                "would_push_to": self.remotes,
            }
        # Create annotated tag
        create_res = self._run_git("tag", "-a", tag, "-m", message, check=False)
        if create_res.returncode != 0:
            return {
                "tag": tag,
                "ok": False,
                "error": f"tag-create failed: {create_res.stderr.strip()[-200:]}",
            }
        push_results: dict[str, Any] = {}
        for remote in self.remotes:
            try:
                res = self._run_git("push", remote, tag, check=False)
                push_results[remote] = {
                    "ok": res.returncode == 0,
                    "stderr": res.stderr.strip()[-100:],
                }
            except Exception as exc:  # noqa: BLE001
                push_results[remote] = {"ok": False, "error": str(exc)}
        return {"tag": tag, "created": True, "push_results": push_results}

    def _tool_sync_repair(self, target_remote: str, strategy: str, dry_run: bool) -> dict[str, Any]:
        if target_remote not in self.remotes:
            raise ValueError(f"unknown remote: {target_remote}")
        if strategy != "force-from-primary":
            raise ValueError(f"unsupported strategy: {strategy}")
        if dry_run:
            return {
                "target_remote": target_remote,
                "strategy": strategy,
                "dryRun": True,
                "would_run": f"git push --force-with-lease {target_remote} main",
                "safety_note": "force-with-lease prevents over-write if mirror has unseen commits",
            }
        res = self._run_git("push", "--force-with-lease", target_remote, "main", check=False)
        return {
            "target_remote": target_remote,
            "ok": res.returncode == 0,
            "stdout": res.stdout.strip()[-200:],
            "stderr": res.stderr.strip()[-200:],
        }

    # --- health ------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        if not self.repo_path:
            return {"ok": True, "status": _umgebung.NICHT_EINGERICHTET, "einrichten": EINRICHTEN}
        try:
            head = self._run_git("rev-parse", "HEAD").stdout.strip()
            return {"status": "ok", "head": head, "remotes_configured": self.remotes}
        except Exception as exc:  # noqa: BLE001
            return {"status": "unreachable", "error": str(exc)}

    def metrics(self) -> dict[str, Any]:
        return {"plugin": self.name, "version": self.version, "remotes": self.remotes}
