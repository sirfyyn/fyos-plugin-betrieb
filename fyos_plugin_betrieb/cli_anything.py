"""mcp_hub.plugins.cli_anything — CLI-Anything Sub-MCP Plugin (S-MCP-HUB.1.13, fyos-Iter-0080).

Bindet das externe CLI-Anything-Framework (HKUDS, Apache-2.0,
https://github.com/HKUDS/CLI-Anything) als Sub-MCP in den FYOS mcp-hub.
Damit hat jeder Agent ueber mcp-hub direkten Zugriff
auf 30+ agent-native CLI-Harnesses (Obsidian, n8n, Blender, FreeCAD,
Godot, Safari, Inkscape, MuseScore, Krita, etc).

**Direktive H+ + Symmetric-Tooling**: ein einziges Plugin macht alle
CLI-Anything-Harnesses fuer alle FYOS-Agents verfuegbar — keine
Per-Service-Wrapper-Plugins mehr.

**Apache-2.0 kompatibel** mit FYOS-Default-Lizenz (Direktive H4).

Tools-Surface:
    cli_anything.list_available()         — alle CLIs im Hub-Registry
    cli_anything.search(query)            — Hub-Suche
    cli_anything.info(name)               — Detail-Info zu einer CLI
    cli_anything.install(name)            — `cli-hub install <name>`
    cli_anything.uninstall(name)          — `cli-hub uninstall <name>`
    cli_anything.update(name)             — `cli-hub update <name>`
    cli_anything.launch(name, args)       — `cli-hub launch <name> <args>`
    cli_anything.read_skill_md(name)      — SKILL.md fuer Agent-Discovery

Backend: `cli-hub` binary aus `cli-anything-hub` PyPI-package.
Im mcp-hub-Container muss `pip install cli-anything-hub` einmal laufen.

Graceful degradation (analog argocd-plugin pattern):
    - `cli-hub` binary fehlt → plugin registers degraded, calls liefern
      `tool_unavailable` Error, Hub-startup nicht blockiert
    - subprocess-Fehler werden in PluginCallError gewrappt
    - Timeouts fuer install/launch (langlaufend) sind grosszuegig (300s)

Sprint-Item: S-MCP-HUB.1.13 (CLI-Anything Sub-MCP)
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from mcp_hub.plugin import HubPlugin, PluginCallError, ToolDescriptor

logger = logging.getLogger(__name__)


# ─── Config defaults ───────────────────────────────────────────────────────

DEFAULT_CLI_HUB_BINARY = "cli-hub"
"""Name of the cli-hub executable on PATH."""

DEFAULT_INSTALL_DIR_HINT = "~/.cli-anything"
"""Default install location used by cli-hub for SKILL.md lookup. May be
overridden via plugin-config.install_dir."""

DEFAULT_LIST_TIMEOUT = 15.0
DEFAULT_INSTALL_TIMEOUT = 300.0   # cli installs can pull npm/pip deps
DEFAULT_LAUNCH_TIMEOUT = 600.0    # generic launches may run long


# ─── Plugin ────────────────────────────────────────────────────────────────


class CliAnythingPlugin(HubPlugin):
    """CLI-Anything Sub-MCP plugin: 8 tools wrapping cli-hub subprocess calls."""

    name = "cli_anything"
    description = (
        "External CLI-Anything framework (HKUDS, Apache-2.0): 30+ "
        "agent-native CLI harnesses (Obsidian, n8n, Blender, FreeCAD, "
        "Godot, etc.) accessible via cli-hub. FYOS Direktive H+ "
        "Composite-AI-Layer: same tools for every agent."
    )
    version = "0.1.0"

    def __init__(self, config: Optional[dict[str, Any]] = None):
        super().__init__()
        cfg = config or {}
        self.binary = cfg.get("binary", DEFAULT_CLI_HUB_BINARY)
        self.install_dir = Path(
            cfg.get("install_dir", DEFAULT_INSTALL_DIR_HINT)
        ).expanduser()
        self.list_timeout = float(cfg.get("list_timeout_sec", DEFAULT_LIST_TIMEOUT))
        self.install_timeout = float(
            cfg.get("install_timeout_sec", DEFAULT_INSTALL_TIMEOUT)
        )
        self.launch_timeout = float(
            cfg.get("launch_timeout_sec", DEFAULT_LAUNCH_TIMEOUT)
        )
        self._available = False   # set by register()
        self._cli_hub_path: Optional[str] = None

    # --- Lifecycle ---------------------------------------------------------

    def register(self) -> None:
        """Probe `cli-hub` binary availability; never block startup."""
        path = shutil.which(self.binary)
        if path is None:
            logger.warning(
                "mcp_hub.plugins.cli_anything: cli-hub binary not found on PATH "
                "(searched %r). Plugin registers in degraded state. "
                "Run `pip install cli-anything-hub` to enable.",
                self.binary,
            )
            self._available = False
            return
        self._cli_hub_path = path
        self._available = True
        logger.info(
            "mcp_hub.plugins.cli_anything: cli-hub binary at %s, plugin ready",
            path,
        )

    def health(self) -> dict[str, Any]:
        return {
            "available": self._available,
            "binary_path": self._cli_hub_path,
            "install_dir": str(self.install_dir),
        }

    def metrics(self) -> dict[str, Any]:
        n_installed = 0
        if self.install_dir.exists():
            n_installed = sum(1 for _ in self.install_dir.iterdir() if _.is_dir())
        return {
            "n_installed_clis": n_installed,
            "available": int(self._available),
        }

    # --- Tool Descriptors --------------------------------------------------

    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="list_available",
                description=(
                    "List all CLIs in the cli-hub registry "
                    "(harnesses + public CLIs). Returns array of "
                    "{name, version, source, category, description}."
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "category": {
                            "type": "string",
                            "description": "Filter by category (optional).",
                        },
                        "source": {
                            "type": "string",
                            "enum": ["harness", "public", "all"],
                            "description": "harness=CLI-Anything-built, public=external (pip/npm/brew). Default all.",
                        },
                    },
                },
                tags=["cli-anything", "read"],
            ),
            ToolDescriptor(
                name="search",
                description="Search the cli-hub registry by free-text query.",
                input_schema={
                    "type": "object",
                    "required": ["query"],
                    "properties": {
                        "query": {"type": "string"},
                    },
                },
                tags=["cli-anything", "read"],
            ),
            ToolDescriptor(
                name="info",
                description="Detail-info for a single CLI by name.",
                input_schema={
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                tags=["cli-anything", "read"],
            ),
            ToolDescriptor(
                name="install",
                description=(
                    "Install a CLI by name. Equivalent of `cli-hub install <name>`."
                ),
                input_schema={
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                tags=["cli-anything", "write"],
                # install_timeout_sec 300 s plus Luft.
                zeitlimit_s=330,
            ),
            ToolDescriptor(
                name="uninstall",
                description="Uninstall a CLI by name.",
                input_schema={
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                # Entfernt installierte Software — die einzige Nicht-Umkehrbarkeit
                # in diesem Plugin.
                tags=["cli-anything", "destructive"],
            ),
            ToolDescriptor(
                name="update",
                description="Update a CLI to the latest version.",
                input_schema={
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                tags=["cli-anything", "write"],
                # install_timeout_sec 300 s plus Luft.
                zeitlimit_s=330,
            ),
            ToolDescriptor(
                name="launch",
                description=(
                    "Launch a CLI with optional arguments. Returns "
                    "stdout, stderr, exit_code."
                ),
                input_schema={
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "args": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Argument list passed to the CLI.",
                        },
                    },
                },
                # `exec`: fuehrt ein beliebiges installiertes Programm mit beliebigen
                # Argumenten aus (`cli-hub launch <name> <args>`). Was dabei passiert,
                # bestimmt das aufgerufene Programm, nicht dieses Plugin — die Karte
                # darf das nicht als `read` ausweisen.
                tags=["cli-anything", "exec", "dangerous"],
                # launch_timeout_sec 600 s plus Luft.
                zeitlimit_s=630,
            ),
            ToolDescriptor(
                name="read_skill_md",
                description=(
                    "Read the SKILL.md file of an installed CLI. "
                    "SKILL.md is the agent-discovery manifest with "
                    "supported commands and usage hints."
                ),
                input_schema={
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                },
                tags=["cli-anything", "read"],
            ),
        ]

    # --- Dispatch ----------------------------------------------------------

    def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        if not self._available:
            return {
                "ok": False,
                "error": "tool_unavailable",
                "detail": (
                    "cli-hub binary not found on PATH. "
                    "Install with `pip install cli-anything-hub`."
                ),
            }

        try:
            if method == "list_available":
                return self._list_available(
                    category=args.get("category"),
                    source=args.get("source"),
                )
            if method == "search":
                return self._search(query=args["query"])
            if method == "info":
                return self._info(name=args["name"])
            if method == "install":
                return self._install(name=args["name"])
            if method == "uninstall":
                return self._uninstall(name=args["name"])
            if method == "update":
                return self._update(name=args["name"])
            if method == "launch":
                return self._launch(
                    name=args["name"], extra_args=args.get("args") or []
                )
            if method == "read_skill_md":
                return self._read_skill_md(name=args["name"])
            raise PluginCallError(
                self.name, method, f"unknown method {method!r}"
            )
        except KeyError as e:
            raise PluginCallError(
                self.name, method, f"missing required arg: {e}"
            ) from e

    # --- Private subprocess wrappers ---------------------------------------

    def _run(
        self,
        argv: list[str],
        timeout: float,
        check: bool = False,
    ) -> dict[str, Any]:
        """Run cli-hub subprocess + capture stdout/stderr/exit_code."""
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            return {
                "ok": False,
                "error": "timeout",
                "detail": f"subprocess timed out after {timeout}s",
                "stdout": e.stdout or "",
                "stderr": e.stderr or "",
            }
        except OSError as e:
            return {"ok": False, "error": "exec_error", "detail": str(e)}

        result = {
            "ok": proc.returncode == 0,
            "exit_code": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
        if check and proc.returncode != 0:
            result["error"] = "nonzero_exit"
        return result

    def _list_available(
        self,
        category: Optional[str] = None,
        source: Optional[str] = None,
    ) -> dict[str, Any]:
        argv = [self._cli_hub_path or self.binary, "list", "--json"]
        if category:
            argv += ["--category", category]
        if source and source != "all":
            argv += ["--source", source]
        result = self._run(argv, timeout=self.list_timeout)
        if not result["ok"]:
            return result
        try:
            result["clis"] = json.loads(result["stdout"])
        except json.JSONDecodeError as e:
            result["clis"] = []
            result["parse_error"] = str(e)
        return result

    def _search(self, query: str) -> dict[str, Any]:
        argv = [self._cli_hub_path or self.binary, "search", query, "--json"]
        result = self._run(argv, timeout=self.list_timeout)
        if result["ok"]:
            try:
                result["matches"] = json.loads(result["stdout"])
            except json.JSONDecodeError:
                result["matches"] = []
        return result

    def _info(self, name: str) -> dict[str, Any]:
        argv = [self._cli_hub_path or self.binary, "info", name]
        return self._run(argv, timeout=self.list_timeout)

    def _install(self, name: str) -> dict[str, Any]:
        argv = [self._cli_hub_path or self.binary, "install", name]
        return self._run(argv, timeout=self.install_timeout)

    def _uninstall(self, name: str) -> dict[str, Any]:
        argv = [self._cli_hub_path or self.binary, "uninstall", name]
        return self._run(argv, timeout=self.list_timeout)

    def _update(self, name: str) -> dict[str, Any]:
        argv = [self._cli_hub_path or self.binary, "update", name]
        return self._run(argv, timeout=self.install_timeout)

    def _launch(self, name: str, extra_args: list[str]) -> dict[str, Any]:
        argv = [self._cli_hub_path or self.binary, "launch", name] + list(extra_args)
        return self._run(argv, timeout=self.launch_timeout)

    def _read_skill_md(self, name: str) -> dict[str, Any]:
        """Read SKILL.md for an installed CLI.

        Search-Pfad (Reihenfolge):
          1. {install_dir}/{name}/SKILL.md
          2. {install_dir}/{name}/skills/SKILL.md
          3. {install_dir}/{name}/skills/{name}/SKILL.md
        """
        if not self.install_dir.exists():
            return {
                "ok": False,
                "error": "install_dir_missing",
                "detail": str(self.install_dir),
            }

        cli_dir = self.install_dir / name
        candidates = [
            cli_dir / "SKILL.md",
            cli_dir / "skills" / "SKILL.md",
            cli_dir / "skills" / name / "SKILL.md",
        ]
        for path in candidates:
            if path.exists() and path.is_file():
                try:
                    content = path.read_text(encoding="utf-8", errors="replace")
                except OSError as e:
                    return {"ok": False, "error": "read_error", "detail": str(e)}
                return {
                    "ok": True,
                    "path": str(path),
                    "content": content,
                    "length": len(content),
                }

        return {
            "ok": False,
            "error": "skill_md_not_found",
            "checked": [str(p) for p in candidates],
            "hint": (
                "CLI may not be installed yet, or the CLI doesn't expose "
                "a SKILL.md. Run cli_anything.install(name) first."
            ),
        }
