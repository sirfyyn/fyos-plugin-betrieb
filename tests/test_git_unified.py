"""mcp_hub.tests.test_git_unified — S-MCP-HUB.1.8 subprocess-based tests."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from fyos_plugin_betrieb.git_unified import GitUnifiedPlugin


@pytest.fixture
def plugin(tmp_path) -> GitUnifiedPlugin:
    p = GitUnifiedPlugin()
    p.config = {"repo_path": str(tmp_path), "remotes": ["gitea", "origin", "github"]}
    p.register()
    return p


def _make_mock_result(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    m = MagicMock(spec=subprocess.CompletedProcess)
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


def test_plugin_metadata():
    p = GitUnifiedPlugin()
    assert p.name == "git"
    assert p.version == "0.1.0"


def test_describe_tools_has_5(plugin: GitUnifiedPlugin):
    tools = plugin.describe_tools()
    assert len(tools) == 5
    names = {t.name for t in tools}
    assert names == {"status_consensus", "push_all", "log_unified", "tag_broadcast", "sync_repair"}


def _ls(sha: str) -> MagicMock:
    return _make_mock_result(0, f"{sha}\trefs/heads/main\n")


def test_status_consensus_all_synced(plugin: GitUnifiedPlugin):
    with patch("subprocess.run") as mock_run:
        # rev-parse HEAD, remote-Liste, 3x ls-remote (echter main der Remotes)
        mock_run.side_effect = [
            _make_mock_result(0, "abc123def456\n"),
            _make_mock_result(0, "gitea\norigin\ngithub\n"),
            _ls("abc123def456"),
            _ls("abc123def456"),
            _ls("abc123def456"),
        ]
        result = plugin.call("status_consensus", {})
    assert result["head"] == "abc123def456"
    assert result["all_synced"] is True
    assert result["remotes"]["gitea"] == {
        "bekannt": True,
        "main": "abc123def456",
        "gleich_wie_head": True,
    }
    assert any("ls-remote" in c.args[0] for c in mock_run.call_args_list)


def test_status_consensus_with_drift(plugin: GitUnifiedPlugin):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _make_mock_result(0, "xyz\n"),
            _make_mock_result(0, "gitea\norigin\ngithub\n"),
            _ls("neuer"),  # main ist weiter als der Klon
            _ls("xyz"),
            _ls("xyz"),
        ]
        result = plugin.call("status_consensus", {})
    assert result["all_synced"] is False
    assert result["remotes"]["gitea"]["gleich_wie_head"] is False
    assert "hinweis" in result


def test_status_consensus_sagt_unbekannt_statt_zu_raten(plugin: GitUnifiedPlugin):
    """Audit 2026-10-02: im Pod gibt es nur `origin`; fehlende/unerreichbare Remotes sind unbekannt."""
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _make_mock_result(0, "abc\n"),
            _make_mock_result(0, "origin\n"),
            _ls("abc"),
        ]
        result = plugin.call("status_consensus", {})
    assert result["all_synced"] is None
    assert result["unbekannt"] == ["gitea", "github"]
    assert result["remotes"]["origin"]["gleich_wie_head"] is True


def test_schreibwerkzeuge_lehnen_bei_schreibschutz_ab(tmp_path):
    p = GitUnifiedPlugin()
    p.config = {"repo_path": str(tmp_path), "schreibbar": False}
    p.register()
    beschreibung = {t.name: t.description for t in p.describe_tools()}
    with patch("subprocess.run") as mock_run:
        for name, args in (
            ("push_all", {}),
            ("tag_broadcast", {"tag": "v1", "message": "m"}),
            ("sync_repair", {"target_remote": "origin"}),
        ):
            assert beschreibung[name].startswith("NICHT VERFUEGBAR")
            res = p.call(name, args)
            assert res["ok"] is False and "schreibgeschuetzt" in res["error"]
    mock_run.assert_not_called()


def test_push_all_dry_run(plugin: GitUnifiedPlugin):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _make_mock_result(0, "", "Everything up-to-date"),
            _make_mock_result(0, "", "Everything up-to-date"),
            _make_mock_result(0, "", "Everything up-to-date"),
        ]
        result = plugin.call("push_all", {"dryRun": True})
    assert result["dryRun"] is True
    assert result["success_count"] == 3
    assert result["total_remotes"] == 3
    # Verify --dry-run flag in call
    for call in mock_run.call_args_list:
        args = call[0][0]
        assert "--dry-run" in args


def test_push_all_partial_fail(plugin: GitUnifiedPlugin):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _make_mock_result(0, "ok", ""),
            _make_mock_result(1, "", "authentication failed"),
            _make_mock_result(0, "ok", ""),
        ]
        result = plugin.call("push_all", {"branch": "main", "dryRun": False})
    assert result["success_count"] == 2
    assert result["results"]["origin"]["ok"] is False


def test_log_unified_parses(plugin: GitUnifiedPlugin):
    fake_log = "abc\tAlex\tfix: something\ndef\tClaude\tfeat: something else\n"
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = _make_mock_result(0, fake_log)
        result = plugin.call("log_unified", {"limit": 2})
    assert len(result) == 2
    assert result[0]["sha"] == "abc"
    assert result[0]["author"] == "Alex"
    assert result[1]["message"] == "feat: something else"


def test_tag_broadcast_dry_run(plugin: GitUnifiedPlugin):
    result = plugin.call("tag_broadcast", {"tag": "v1.0", "message": "release", "dryRun": True})
    assert result["dryRun"] is True
    assert result["would_create"] is True
    assert "gitea" in result["would_push_to"]


def test_tag_broadcast_actual(plugin: GitUnifiedPlugin):
    with patch("subprocess.run") as mock_run:
        mock_run.side_effect = [
            _make_mock_result(0, ""),  # tag-create OK
            _make_mock_result(0, "", ""),  # push gitea
            _make_mock_result(0, "", ""),  # push origin
            _make_mock_result(0, "", ""),  # push github
        ]
        result = plugin.call("tag_broadcast", {"tag": "v1.1", "message": "test", "dryRun": False})
    assert result["created"] is True
    assert result["push_results"]["gitea"]["ok"] is True


def test_sync_repair_dry_run(plugin: GitUnifiedPlugin):
    result = plugin.call(
        "sync_repair",
        {"target_remote": "github", "strategy": "force-from-primary", "dryRun": True},
    )
    assert result["dryRun"] is True
    assert "force-with-lease" in result["would_run"]


def test_sync_repair_unknown_remote(plugin: GitUnifiedPlugin):
    with pytest.raises(ValueError, match="unknown remote"):
        plugin.call("sync_repair", {"target_remote": "sourcehut", "dryRun": True})


def test_sync_repair_invalid_strategy(plugin: GitUnifiedPlugin):
    with pytest.raises(ValueError, match="unsupported strategy"):
        plugin.call("sync_repair", {"target_remote": "github", "strategy": "yolo", "dryRun": True})


def test_unknown_tool(plugin: GitUnifiedPlugin):
    with pytest.raises(ValueError, match="unknown tool"):
        plugin.call("rebase_everything", {})
