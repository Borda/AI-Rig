"""Protect remote names and URL ports at the local Git identity boundary."""

import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from _modules import load_module_registered

SELECTOR = load_module_registered(
    "remote_identity_safety", Path(__file__).resolve().parents[2] / "shared/select-git-remote.py"
)


def test_remote_name_is_separated_from_get_url_options(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Even a config-provided option-shaped name occupies the remote argument position."""
    runner = Mock(
        spec=subprocess.run,
        side_effect=[
            subprocess.CompletedProcess([], 0, "--push\n"),
            subprocess.CompletedProcess([], 0, "https://host/a/b\n"),
        ],
    )
    monkeypatch.setattr(SELECTOR.subprocess, "run", runner)

    assert SELECTOR.read_remotes(tmp_path) == {"--push": ["https://host/a/b"]}
    assert runner.call_args.args[0] == ["git", "remote", "get-url", "--all", "--", "--push"]


@pytest.mark.parametrize("port", [pytest.param(443, id="explicit-default-port"), pytest.param(8443, id="other-port")])
def test_explicit_port_cannot_match_portless_repository(port: int) -> None:
    """Repository selection preserves explicit ports rather than equating another endpoint."""
    with pytest.raises(ValueError, match="no-matching-remote"):
        SELECTOR.select_remote("https://github.com/a/b", {"origin": [f"https://github.com:{port}/a/b"]})
