"""mcp_hub.tests.test_cli_anything_plugin — S-MCP-HUB.1.13 (fyos-Iter-0080).

Covers the CliAnythingPlugin contract:
  - Plugin-Protocol compliance (name/version/description/8 ToolDescriptors)
  - Lifecycle: register() degraded when binary missing
  - Subprocess dispatch via mock subprocess.run
  - Happy-paths: list / search / info / install / uninstall / update / launch
  - SKILL.md read: present / missing / read-error
  - Error cases: timeout / nonzero-exit / exec_error
  - Tool-unavailable when binary absent
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import pytest

from mcp_hub.plugin import ToolDescriptor
from fyos_plugin_betrieb.cli_anything import CliAnythingPlugin


# ─── Helpers ────────────────────────────────────────────────────────────────


class FakeProc:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = ""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_plugin(binary_available: bool = True, **cfg) -> CliAnythingPlugin:
    plugin = CliAnythingPlugin(config=cfg)
    if binary_available:
        plugin._available = True
        plugin._cli_hub_path = "/usr/local/bin/cli-hub"
    return plugin


# ─── Lifecycle / Protocol ───────────────────────────────────────────────────


def test_plugin_metadata():
    p = CliAnythingPlugin()
    assert p.name == "cli_anything"
    assert p.version == "0.1.0"
    assert "CLI-Anything" in p.description


def test_describe_tools_count_and_names():
    p = CliAnythingPlugin()
    tools = p.describe_tools()
    assert len(tools) == 8
    expected = {
        "list_available", "search", "info", "install",
        "uninstall", "update", "launch", "read_skill_md",
    }
    assert {t.name for t in tools} == expected


def test_describe_tools_have_input_schema():
    p = CliAnythingPlugin()
    tools = p.describe_tools()
    for t in tools:
        assert isinstance(t, ToolDescriptor)
        assert isinstance(t.input_schema, dict)
        assert t.input_schema.get("type") == "object"


def test_register_degraded_when_binary_missing(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: None)
    p = CliAnythingPlugin()
    p.register()
    assert p._available is False


def test_register_available_when_binary_found(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda _: "/usr/local/bin/cli-hub")
    p = CliAnythingPlugin()
    p.register()
    assert p._available is True
    assert p._cli_hub_path == "/usr/local/bin/cli-hub"


def test_health_reports_state():
    p = _make_plugin(binary_available=True)
    h = p.health()
    assert h["available"] is True
    assert h["binary_path"] == "/usr/local/bin/cli-hub"


# ─── Tool unavailable when binary absent ─────────────────────────────────────


def test_call_tool_unavailable_returns_structured_error():
    p = _make_plugin(binary_available=False)
    p._available = False
    result = p.call("list_available", {})
    assert result["ok"] is False
    assert result["error"] == "tool_unavailable"
    assert "cli-anything-hub" in result["detail"]


# ─── list_available ──────────────────────────────────────────────────────────


def test_list_available_parses_json_output():
    p = _make_plugin()
    fake_clis = [
        {"name": "obsidian-cli", "version": "0.4.1", "source": "harness"},
        {"name": "n8n-cli", "version": "0.2.0", "source": "harness"},
    ]
    fake_proc = FakeProc(0, stdout=json.dumps(fake_clis))
    with patch("subprocess.run", return_value=fake_proc) as m:
        result = p.call("list_available", {})
    assert result["ok"] is True
    assert result["clis"] == fake_clis
    args = m.call_args.args[0]
    assert args[0] == "/usr/local/bin/cli-hub"
    assert "list" in args
    assert "--json" in args


def test_list_available_with_filters():
    p = _make_plugin()
    fake_proc = FakeProc(0, stdout="[]")
    with patch("subprocess.run", return_value=fake_proc) as m:
        p.call("list_available", {"category": "creative", "source": "harness"})
    args = m.call_args.args[0]
    assert "--category" in args and "creative" in args
    assert "--source" in args and "harness" in args


def test_list_available_handles_invalid_json():
    p = _make_plugin()
    fake_proc = FakeProc(0, stdout="not-json")
    with patch("subprocess.run", return_value=fake_proc):
        result = p.call("list_available", {})
    assert result["ok"] is True
    assert result["clis"] == []
    assert "parse_error" in result


# ─── search ──────────────────────────────────────────────────────────────────


def test_search_passes_query_and_parses_matches():
    p = _make_plugin()
    fake_matches = [{"name": "blender-cli", "score": 0.92}]
    fake_proc = FakeProc(0, stdout=json.dumps(fake_matches))
    with patch("subprocess.run", return_value=fake_proc) as m:
        result = p.call("search", {"query": "3d modeling"})
    assert result["matches"] == fake_matches
    args = m.call_args.args[0]
    assert "search" in args
    assert "3d modeling" in args


def test_search_missing_query_raises():
    from mcp_hub.plugin import PluginCallError
    p = _make_plugin()
    with pytest.raises(PluginCallError, match="missing required arg"):
        p.call("search", {})


# ─── info / install / uninstall / update ─────────────────────────────────────


def test_info_returns_subprocess_result():
    p = _make_plugin()
    fake_proc = FakeProc(0, stdout="cli: obsidian-cli\nversion: 0.4.1\n")
    with patch("subprocess.run", return_value=fake_proc):
        result = p.call("info", {"name": "obsidian-cli"})
    assert result["ok"] is True
    assert "obsidian-cli" in result["stdout"]


def test_install_uses_install_timeout():
    p = _make_plugin(install_timeout_sec=5.0)
    fake_proc = FakeProc(0, stdout="installed!")
    with patch("subprocess.run", return_value=fake_proc) as m:
        p.call("install", {"name": "n8n-cli"})
    kwargs = m.call_args.kwargs
    assert kwargs["timeout"] == 5.0


def test_install_propagates_nonzero_exit():
    p = _make_plugin()
    fake_proc = FakeProc(1, stdout="", stderr="ERR not found")
    with patch("subprocess.run", return_value=fake_proc):
        result = p.call("install", {"name": "no-such-cli"})
    assert result["ok"] is False
    assert result["exit_code"] == 1
    assert "not found" in result["stderr"]


def test_uninstall_smoke():
    p = _make_plugin()
    with patch("subprocess.run", return_value=FakeProc(0, stdout="removed")) as m:
        result = p.call("uninstall", {"name": "obsidian-cli"})
    assert result["ok"] is True
    assert "uninstall" in m.call_args.args[0]


def test_update_smoke():
    p = _make_plugin()
    with patch("subprocess.run", return_value=FakeProc(0, stdout="updated")) as m:
        result = p.call("update", {"name": "obsidian-cli"})
    assert result["ok"] is True
    assert "update" in m.call_args.args[0]


# ─── launch ─────────────────────────────────────────────────────────────────


def test_launch_passes_extra_args():
    p = _make_plugin()
    fake_proc = FakeProc(0, stdout="hello world")
    with patch("subprocess.run", return_value=fake_proc) as m:
        result = p.call(
            "launch",
            {"name": "obsidian-cli", "args": ["list-vaults", "--json"]},
        )
    assert result["ok"] is True
    args = m.call_args.args[0]
    assert "launch" in args
    assert "obsidian-cli" in args
    assert "list-vaults" in args
    assert "--json" in args


def test_launch_no_extra_args_works():
    p = _make_plugin()
    fake_proc = FakeProc(0, stdout="default ran")
    with patch("subprocess.run", return_value=fake_proc):
        result = p.call("launch", {"name": "obsidian-cli"})
    assert result["ok"] is True


# ─── timeout / exec_error handling ───────────────────────────────────────────


def test_subprocess_timeout_returns_structured_error():
    p = _make_plugin()
    err = subprocess.TimeoutExpired(cmd="cli-hub", timeout=15)
    err.stdout = b""
    err.stderr = b""
    with patch("subprocess.run", side_effect=err):
        result = p.call("list_available", {})
    assert result["ok"] is False
    assert result["error"] == "timeout"


def test_oserror_returns_exec_error():
    p = _make_plugin()
    with patch("subprocess.run", side_effect=OSError("bin missing")):
        result = p.call("list_available", {})
    assert result["ok"] is False
    assert result["error"] == "exec_error"


# ─── read_skill_md ───────────────────────────────────────────────────────────


def test_read_skill_md_install_dir_missing(tmp_path):
    p = _make_plugin(install_dir=str(tmp_path / "does-not-exist"))
    result = p.call("read_skill_md", {"name": "obsidian-cli"})
    assert result["ok"] is False
    assert result["error"] == "install_dir_missing"


def test_read_skill_md_finds_top_level(tmp_path):
    cli_dir = tmp_path / "obsidian-cli"
    cli_dir.mkdir()
    (cli_dir / "SKILL.md").write_text("# Obsidian CLI Skill\nuse-it", encoding="utf-8")
    p = _make_plugin(install_dir=str(tmp_path))
    result = p.call("read_skill_md", {"name": "obsidian-cli"})
    assert result["ok"] is True
    assert "Obsidian CLI Skill" in result["content"]
    assert result["length"] > 0


def test_read_skill_md_finds_in_skills_subdir(tmp_path):
    cli_dir = tmp_path / "n8n-cli" / "skills" / "n8n-cli"
    cli_dir.mkdir(parents=True)
    (cli_dir / "SKILL.md").write_text("# n8n", encoding="utf-8")
    p = _make_plugin(install_dir=str(tmp_path))
    result = p.call("read_skill_md", {"name": "n8n-cli"})
    assert result["ok"] is True
    assert "n8n" in result["content"]


def test_read_skill_md_not_found_lists_candidates(tmp_path):
    cli_dir = tmp_path / "cli-without-skill"
    cli_dir.mkdir()
    p = _make_plugin(install_dir=str(tmp_path))
    result = p.call("read_skill_md", {"name": "cli-without-skill"})
    assert result["ok"] is False
    assert result["error"] == "skill_md_not_found"
    assert isinstance(result["checked"], list)
    assert len(result["checked"]) == 3


# ─── Unknown method ──────────────────────────────────────────────────────────


def test_unknown_method_raises():
    from mcp_hub.plugin import PluginCallError
    p = _make_plugin()
    with pytest.raises(PluginCallError, match="unknown method"):
        p.call("does_not_exist", {})


# ─── Metrics ─────────────────────────────────────────────────────────────────


def test_metrics_counts_installed_dirs(tmp_path):
    (tmp_path / "obsidian-cli").mkdir()
    (tmp_path / "n8n-cli").mkdir()
    (tmp_path / "not-a-dir.txt").write_text("x")
    p = _make_plugin(install_dir=str(tmp_path))
    m = p.metrics()
    assert m["n_installed_clis"] == 2
    assert m["available"] == 1
