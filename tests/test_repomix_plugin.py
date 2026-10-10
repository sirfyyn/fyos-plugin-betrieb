"""mcp_hub.tests.test_repomix_plugin — S-MCP-HUB.1.14 (fyos-Iter-0090).

Covers RepomixPlugin contract (analog test_cli_anything_plugin):
  - Plugin-Protocol compliance (name/version/description/4 ToolDescriptors)
  - Lifecycle: register() degraded when binary missing
  - Subprocess dispatch via mock subprocess.run
  - Tool happy-paths: pack / pack_to_file / estimate_tokens / check_available
  - Error cases: timeout / nonzero-exit / exec_error
  - Tool-unavailable when binary absent (except check_available)
  - Token-count extraction from output text
  - Glob-args (include/exclude) routing
  - output_format validation
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from mcp_hub.plugin import PluginCallError, ToolDescriptor
from fyos_plugin_betrieb.repomix import RepomixPlugin


# ─── Helpers ────────────────────────────────────────────────────────────────


class FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_plugin(binary_available: bool = True, **cfg) -> RepomixPlugin:
    plugin = RepomixPlugin(config=cfg)
    if binary_available:
        plugin._available = True
        plugin._repomix_path = "/usr/local/bin/repomix"
    return plugin


# ─── Lifecycle / Protocol ───────────────────────────────────────────────────


def test_plugin_metadata():
    p = RepomixPlugin()
    assert p.name == "repomix"
    assert p.version == "0.1.0"
    assert "Repomix" in p.description


def test_describe_tools_count_and_names():
    p = RepomixPlugin()
    tools = p.describe_tools()
    assert len(tools) == 4
    expected = {
        "repomix_pack",
        "repomix_pack_to_file",
        "repomix_estimate_tokens",
        "repomix_check_available",
    }
    assert {t.name for t in tools} == expected


def test_describe_tools_have_input_schema():
    p = RepomixPlugin()
    for t in p.describe_tools():
        assert isinstance(t, ToolDescriptor)
        assert isinstance(t.input_schema, dict)
        assert t.input_schema.get("type") == "object"


def test_register_degraded_when_binary_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    p = RepomixPlugin()
    p.register()
    assert p._available is False
    assert p._repomix_path is None


def test_register_available_when_binary_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/repomix")
    p = RepomixPlugin()
    p.register()
    assert p._available is True
    assert p._repomix_path == "/usr/local/bin/repomix"


def test_health_reports_state():
    p = _make_plugin(binary_available=True)
    h = p.health()
    assert h["available"] is True
    assert h["binary_path"] == "/usr/local/bin/repomix"


def test_metrics_reports_availability():
    p = _make_plugin(binary_available=True)
    assert p.metrics()["available"] == 1
    p2 = _make_plugin(binary_available=False)
    p2._available = False
    assert p2.metrics()["available"] == 0


# ─── Tool unavailable when binary absent ─────────────────────────────────────


def test_call_tool_unavailable_returns_structured_error():
    p = _make_plugin(binary_available=False)
    p._available = False
    result = p.call("repomix_pack", {"repo_path": "/tmp/x"})
    assert result["ok"] is False
    assert result["error"] == "tool_unavailable"
    assert "repomix" in result["detail"].lower()


def test_check_available_works_even_when_binary_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    p = _make_plugin(binary_available=False)
    p._available = False
    p._repomix_path = None
    result = p.call("repomix_check_available", {})
    assert result["available"] is False
    assert result["version"] is None
    assert result["binary_path"] is None


# ─── repomix_pack ────────────────────────────────────────────────────────────


def test_pack_passes_repo_path_and_format():
    p = _make_plugin()
    fake_proc = FakeProc(0, stdout="# Repo Content\n", stderr="Total tokens: 1234")
    with patch("subprocess.run", return_value=fake_proc) as m:
        result = p.call(
            "repomix_pack",
            {"repo_path": "/srv/myrepo", "output_format": "markdown"},
        )
    assert result["ok"] is True
    assert result["content"] == "# Repo Content\n"
    assert result["token_count"] == 1234
    assert result["output_format"] == "markdown"
    args = m.call_args.args[0]
    assert args[0] == "/usr/local/bin/repomix"
    assert "/srv/myrepo" in args
    assert "--output" in args and "-" in args
    assert "--style" in args and "markdown" in args


def test_pack_default_format_is_markdown():
    p = _make_plugin()
    fake_proc = FakeProc(0, stdout="content")
    with patch("subprocess.run", return_value=fake_proc) as m:
        p.call("repomix_pack", {"repo_path": "/r"})
    args = m.call_args.args[0]
    assert "markdown" in args


def test_pack_invalid_format_raises():
    p = _make_plugin()
    with pytest.raises(PluginCallError, match="invalid output_format"):
        p.call(
            "repomix_pack",
            {"repo_path": "/r", "output_format": "yaml"},
        )


def test_pack_with_include_and_exclude_globs():
    p = _make_plugin()
    fake_proc = FakeProc(0, stdout="x")
    with patch("subprocess.run", return_value=fake_proc) as m:
        p.call(
            "repomix_pack",
            {
                "repo_path": "/r",
                "include_globs": ["src/**/*.ts", "lib/**/*.py"],
                "exclude_globs": ["**/*.test.ts"],
            },
        )
    args = m.call_args.args[0]
    assert "--include" in args
    include_idx = args.index("--include")
    assert args[include_idx + 1] == "src/**/*.ts,lib/**/*.py"
    assert "--ignore" in args
    ignore_idx = args.index("--ignore")
    assert args[ignore_idx + 1] == "**/*.test.ts"


def test_pack_missing_repo_path_raises():
    p = _make_plugin()
    with pytest.raises(PluginCallError, match="missing required arg"):
        p.call("repomix_pack", {})


def test_pack_propagates_nonzero_exit():
    p = _make_plugin()
    fake_proc = FakeProc(1, stdout="", stderr="ERR: repo not found")
    with patch("subprocess.run", return_value=fake_proc):
        result = p.call("repomix_pack", {"repo_path": "/nope"})
    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert result["error"] == "nonzero_exit"


def test_pack_uses_pack_timeout():
    p = _make_plugin(pack_timeout_sec=42.0)
    with patch("subprocess.run", return_value=FakeProc(0)) as m:
        p.call("repomix_pack", {"repo_path": "/r"})
    assert m.call_args.kwargs["timeout"] == 42.0


# ─── repomix_pack_to_file ────────────────────────────────────────────────────


def test_pack_to_file_passes_output_arg(tmp_path):
    p = _make_plugin()
    out = tmp_path / "out.md"
    out.write_bytes(b"x" * 17)  # pretend repomix wrote 17 bytes
    fake_proc = FakeProc(0, stdout="", stderr="Total tokens: 99")
    with patch("subprocess.run", return_value=fake_proc) as m:
        result = p.call(
            "repomix_pack_to_file",
            {"repo_path": "/r", "output_file": str(out)},
        )
    assert result["ok"] is True
    assert result["file_path"] == str(out)
    assert result["size_bytes"] == 17
    assert result["token_count"] == 99
    args = m.call_args.args[0]
    assert "-o" in args
    o_idx = args.index("-o")
    assert args[o_idx + 1] == str(out)


def test_pack_to_file_size_none_when_file_missing():
    p = _make_plugin()
    fake_proc = FakeProc(0)
    with patch("subprocess.run", return_value=fake_proc):
        result = p.call(
            "repomix_pack_to_file",
            {"repo_path": "/r", "output_file": "/nonexistent/out.md"},
        )
    assert result["ok"] is True
    assert result["size_bytes"] is None


def test_pack_to_file_missing_output_file_raises():
    p = _make_plugin()
    with pytest.raises(PluginCallError, match="missing required arg"):
        p.call("repomix_pack_to_file", {"repo_path": "/r"})


# ─── repomix_estimate_tokens ─────────────────────────────────────────────────


def test_estimate_tokens_parses_count():
    p = _make_plugin()
    fake_proc = FakeProc(0, stdout="Total tokens: 5,432\n")
    with patch("subprocess.run", return_value=fake_proc) as m:
        result = p.call("repomix_estimate_tokens", {"repo_path": "/r"})
    assert result["ok"] is True
    assert result["token_count"] == 5432
    args = m.call_args.args[0]
    assert "--token-count-only" in args


def test_estimate_tokens_uses_estimate_timeout():
    p = _make_plugin(estimate_timeout_sec=7.5)
    with patch("subprocess.run", return_value=FakeProc(0)) as m:
        p.call("repomix_estimate_tokens", {"repo_path": "/r"})
    assert m.call_args.kwargs["timeout"] == 7.5


# ─── repomix_check_available ─────────────────────────────────────────────────


def test_check_available_reports_version():
    p = _make_plugin()
    fake_proc = FakeProc(0, stdout="0.3.1\n")
    with patch("subprocess.run", return_value=fake_proc):
        result = p.call("repomix_check_available", {})
    assert result["ok"] is True
    assert result["available"] is True
    assert result["version"] == "0.3.1"
    assert result["binary_path"] == "/usr/local/bin/repomix"


# ─── Error handling: timeout / exec_error ────────────────────────────────────


def test_subprocess_timeout_returns_structured_error():
    p = _make_plugin()
    err = subprocess.TimeoutExpired(cmd="repomix", timeout=120)
    err.stdout = b""
    err.stderr = b""
    with patch("subprocess.run", side_effect=err):
        result = p.call("repomix_pack", {"repo_path": "/r"})
    assert result["ok"] is False
    assert result["error"] == "timeout"


def test_oserror_returns_exec_error():
    p = _make_plugin()
    with patch("subprocess.run", side_effect=OSError("bin missing")):
        result = p.call("repomix_pack", {"repo_path": "/r"})
    assert result["ok"] is False
    assert result["error"] == "exec_error"


# ─── Token-count extraction edge cases ───────────────────────────────────────


def test_extract_token_count_handles_comma():
    assert RepomixPlugin._extract_token_count("Total Tokens: 12,345") == 12345


def test_extract_token_count_picks_largest():
    text = "tokens 5\nfile tokens: 100\ngrand total tokens 9999"
    assert RepomixPlugin._extract_token_count(text) == 9999


def test_extract_token_count_returns_none_on_no_match():
    assert RepomixPlugin._extract_token_count("no numbers here") is None
    assert RepomixPlugin._extract_token_count("") is None


# ─── Unknown method ──────────────────────────────────────────────────────────


def test_unknown_method_raises():
    p = _make_plugin()
    with pytest.raises(PluginCallError, match="unknown method"):
        p.call("does_not_exist", {})
