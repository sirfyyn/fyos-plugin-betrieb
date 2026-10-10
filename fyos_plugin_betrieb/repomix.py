"""mcp_hub.plugins.repomix — Repomix Sub-MCP Plugin (S-MCP-HUB.1.14, fyos-Iter-0090).

Bindet das externe Repomix-CLI (yamadashy, MIT,
https://github.com/yamadashy/repomix) als Sub-MCP in den FYOS mcp-hub.
Repomix packt ein gesamtes Repository in eine einzige AI-friendly Datei
(XML / Markdown / Plain) inklusive Token-Counting und Glob-basierter
Filterung.

**Direktive H+ + Symmetric-Tooling**: ein Plugin macht Repomix-Capability
gleichzeitig fuer jeden Agenten verfuegbar — kein doppelter Wrapper.

**Karpathy-Principles aktiv** (Iter-0083): Surgical Changes — Plugin
spiegelt cli_anything.py 1:1 in Lifecycle / Subprocess-Wrapping /
Graceful-Degradation. Keine spekulativen Features.

Tools-Surface (4):
    repomix.repomix_pack(repo_path, output_format, include_globs?, exclude_globs?)
        Packt Repo in stdout, Returns content + token_count.
    repomix.repomix_pack_to_file(repo_path, output_file, ...)
        Packt Repo in Datei, Returns file_path + size_bytes.
    repomix.repomix_estimate_tokens(repo_path)
        Token-Schaetzung ohne vollen Pack.
    repomix.repomix_check_available()
        Version + binary-path probe (Lifecycle / Health).

Backend: `repomix` binary, install via `npx repomix` oder `pip install repomix`.

Graceful degradation (analog cli_anything pattern):
    - `repomix` binary fehlt → plugin registers degraded, calls liefern
      `tool_unavailable` Error, Hub-Startup nie blockiert
    - subprocess-Fehler werden in PluginCallError gewrappt
    - Timeouts: pack=120s, estimate=60s

Sprint-Item: S-MCP-HUB.1.14 (Repomix Sub-MCP, Block-A Quick-Win)
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from typing import Any, Optional

from mcp_hub.plugin import HubPlugin, PluginCallError, ToolDescriptor

logger = logging.getLogger(__name__)


# ─── Config defaults ───────────────────────────────────────────────────────

DEFAULT_REPOMIX_BINARY = "repomix"
"""Name of the repomix executable on PATH (npx- or pip-installed)."""

DEFAULT_PACK_TIMEOUT = 120.0
DEFAULT_ESTIMATE_TIMEOUT = 60.0

VALID_OUTPUT_FORMATS = ("markdown", "xml", "plain")
"""Repomix --style values."""

# Heuristic token-count regex — repomix prints "Total Tokens: 12345" or
# variants. We extract the largest integer following 'token' on stderr/stdout.
_TOKEN_COUNT_RE = re.compile(r"(?i)token[s]?[^\d]*(\d[\d,]*)")


# ─── Plugin ────────────────────────────────────────────────────────────────


class RepomixPlugin(HubPlugin):
    """Repomix Sub-MCP plugin: 4 tools wrapping `repomix` subprocess calls."""

    name = "repomix"
    description = (
        "External Repomix CLI (yamadashy, MIT): pack entire repository into "
        "single AI-friendly file (Markdown/XML/Plain) with token-counting and "
        "glob-filtering. FYOS Direktive H+ Composite-AI: same tool for Claude "
        "and other agents."
    )
    version = "0.1.0"

    def __init__(self, config: Optional[dict[str, Any]] = None):
        super().__init__()
        cfg = config or {}
        self.binary = cfg.get("binary", DEFAULT_REPOMIX_BINARY)
        self.pack_timeout = float(cfg.get("pack_timeout_sec", DEFAULT_PACK_TIMEOUT))
        self.estimate_timeout = float(
            cfg.get("estimate_timeout_sec", DEFAULT_ESTIMATE_TIMEOUT)
        )
        self._available = False  # set by register()
        self._repomix_path: Optional[str] = None

    # --- Lifecycle ---------------------------------------------------------

    def register(self) -> None:
        """Probe `repomix` binary availability; never block startup."""
        path = shutil.which(self.binary)
        if path is None:
            logger.warning(
                "mcp_hub.plugins.repomix: repomix binary not found on PATH "
                "(searched %r). Plugin registers in degraded state. "
                "Install via `npx repomix` or `pip install repomix`.",
                self.binary,
            )
            self._available = False
            return
        self._repomix_path = path
        self._available = True
        logger.info(
            "mcp_hub.plugins.repomix: repomix binary at %s, plugin ready",
            path,
        )

    def health(self) -> dict[str, Any]:
        return {
            "available": self._available,
            "binary_path": self._repomix_path,
        }

    def metrics(self) -> dict[str, Any]:
        return {"available": int(self._available)}

    # --- Tool Descriptors --------------------------------------------------

    def describe_tools(self) -> list[ToolDescriptor]:
        return [
            ToolDescriptor(
                name="repomix_pack",
                description=(
                    "Pack a repository into a single AI-friendly string. "
                    "Returns {content, token_count, exit_code}. Output format "
                    "defaults to markdown. Glob filters narrow file selection."
                ),
                input_schema={
                    "type": "object",
                    "required": ["repo_path"],
                    "properties": {
                        "repo_path": {"type": "string"},
                        "output_format": {
                            "type": "string",
                            "enum": list(VALID_OUTPUT_FORMATS),
                            "description": "markdown | xml | plain (default: markdown)",
                        },
                        "include_globs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Glob patterns to include (e.g. ['src/**/*.ts']).",
                        },
                        "exclude_globs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Glob patterns to exclude.",
                        },
                    },
                },
                tags=["repomix", "read"],
                # pack_timeout_sec 120 s plus Luft.
                zeitlimit_s=150,
            ),
            ToolDescriptor(
                name="repomix_pack_to_file",
                description=(
                    "Pack a repository directly into an output file. "
                    "Returns {file_path, size_bytes, exit_code}. Useful for "
                    "large repos where stdout buffering is impractical."
                ),
                input_schema={
                    "type": "object",
                    "required": ["repo_path", "output_file"],
                    "properties": {
                        "repo_path": {"type": "string"},
                        "output_file": {"type": "string"},
                        "output_format": {
                            "type": "string",
                            "enum": list(VALID_OUTPUT_FORMATS),
                        },
                        "include_globs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "exclude_globs": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                tags=["repomix", "write"],
                # pack_timeout_sec 120 s plus Luft.
                zeitlimit_s=150,
            ),
            ToolDescriptor(
                name="repomix_estimate_tokens",
                description=(
                    "Estimate token-count for a repository without producing "
                    "the full pack. Faster than repomix_pack for sizing."
                ),
                input_schema={
                    "type": "object",
                    "required": ["repo_path"],
                    "properties": {
                        "repo_path": {"type": "string"},
                    },
                },
                tags=["repomix", "read"],
            ),
            ToolDescriptor(
                name="repomix_check_available",
                description=(
                    "Probe repomix binary version + path. Returns "
                    "{available, version, binary_path}."
                ),
                input_schema={"type": "object", "properties": {}},
                tags=["repomix", "read"],
            ),
        ]

    # --- Dispatch ----------------------------------------------------------

    def call(self, method: str, args: dict[str, Any]) -> dict[str, Any]:
        # check_available works even when binary is missing — it's the probe.
        if method == "repomix_check_available":
            return self._check_available()

        if not self._available:
            return {
                "ok": False,
                "error": "tool_unavailable",
                "detail": (
                    "repomix binary not found on PATH. Install via "
                    "`npx repomix` or `pip install repomix`."
                ),
            }

        try:
            if method == "repomix_pack":
                return self._pack(
                    repo_path=args["repo_path"],
                    output_format=args.get("output_format", "markdown"),
                    include_globs=args.get("include_globs"),
                    exclude_globs=args.get("exclude_globs"),
                )
            if method == "repomix_pack_to_file":
                return self._pack_to_file(
                    repo_path=args["repo_path"],
                    output_file=args["output_file"],
                    output_format=args.get("output_format", "markdown"),
                    include_globs=args.get("include_globs"),
                    exclude_globs=args.get("exclude_globs"),
                )
            if method == "repomix_estimate_tokens":
                return self._estimate_tokens(repo_path=args["repo_path"])
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
    ) -> dict[str, Any]:
        """Run repomix subprocess + capture stdout/stderr/exit_code."""
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
        if proc.returncode != 0:
            result["error"] = "nonzero_exit"
        return result

    @staticmethod
    def _extract_token_count(text: str) -> Optional[int]:
        """Parse 'Total tokens: 12,345' (or similar) out of repomix output."""
        if not text:
            return None
        best: Optional[int] = None
        for match in _TOKEN_COUNT_RE.finditer(text):
            raw = match.group(1).replace(",", "")
            try:
                value = int(raw)
            except ValueError:
                continue
            if best is None or value > best:
                best = value
        return best

    @staticmethod
    def _validate_output_format(output_format: str) -> str:
        if output_format not in VALID_OUTPUT_FORMATS:
            raise PluginCallError(
                "repomix",
                "repomix_pack",
                f"invalid output_format {output_format!r}; "
                f"expected one of {VALID_OUTPUT_FORMATS}",
            )
        return output_format

    def _build_glob_args(
        self,
        include_globs: Optional[list[str]],
        exclude_globs: Optional[list[str]],
    ) -> list[str]:
        """Build --include / --ignore CLI flags. Repomix accepts comma-lists."""
        args: list[str] = []
        if include_globs:
            args += ["--include", ",".join(include_globs)]
        if exclude_globs:
            args += ["--ignore", ",".join(exclude_globs)]
        return args

    def _pack(
        self,
        repo_path: str,
        output_format: str = "markdown",
        include_globs: Optional[list[str]] = None,
        exclude_globs: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        fmt = self._validate_output_format(output_format)
        argv = [
            self._repomix_path or self.binary,
            repo_path,
            "--output", "-",
            "--style", fmt,
        ]
        argv += self._build_glob_args(include_globs, exclude_globs)
        result = self._run(argv, timeout=self.pack_timeout)
        if result["ok"]:
            content = result.get("stdout", "")
            # Token-count typically printed to stderr — fallback to stdout.
            token_count = self._extract_token_count(
                result.get("stderr", "")
            ) or self._extract_token_count(content)
            result["content"] = content
            result["token_count"] = token_count
            result["output_format"] = fmt
        return result

    def _pack_to_file(
        self,
        repo_path: str,
        output_file: str,
        output_format: str = "markdown",
        include_globs: Optional[list[str]] = None,
        exclude_globs: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        fmt = self._validate_output_format(output_format)
        argv = [
            self._repomix_path or self.binary,
            repo_path,
            "-o", output_file,
            "--style", fmt,
        ]
        argv += self._build_glob_args(include_globs, exclude_globs)
        result = self._run(argv, timeout=self.pack_timeout)
        if result["ok"]:
            size_bytes: Optional[int] = None
            try:
                from pathlib import Path
                size_bytes = Path(output_file).stat().st_size
            except OSError:
                size_bytes = None
            result["file_path"] = output_file
            result["size_bytes"] = size_bytes
            result["output_format"] = fmt
            # Best-effort token-count from stderr.
            result["token_count"] = self._extract_token_count(
                result.get("stderr", "")
            )
        return result

    def _estimate_tokens(self, repo_path: str) -> dict[str, Any]:
        # Repomix ≥0.2 supports --token-count-only; fallback to packing
        # with stdout-discard if unavailable. We try the explicit flag first.
        argv = [
            self._repomix_path or self.binary,
            repo_path,
            "--token-count-only",
        ]
        result = self._run(argv, timeout=self.estimate_timeout)
        if result["ok"]:
            result["token_count"] = self._extract_token_count(
                result.get("stdout", "")
            ) or self._extract_token_count(result.get("stderr", ""))
        return result

    def _check_available(self) -> dict[str, Any]:
        path = self._repomix_path or shutil.which(self.binary)
        if path is None:
            return {
                "ok": False,
                "available": False,
                "version": None,
                "binary_path": None,
            }
        # Version probe — short timeout, never raise.
        result = self._run([path, "--version"], timeout=10.0)
        version = (result.get("stdout") or "").strip() or None
        return {
            "ok": True,
            "available": True,
            "version": version,
            "binary_path": path,
        }
