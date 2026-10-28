"""Tests for ``bin/merge_settings_permissions.py``.

The script replaces the per-plugin ``jq --slurpfile … | unique`` permission merges in every ``/setup``, minus their re-
sort: user rules keep their order and new shipped rules are appended. The equivalence tests run the replaced jq filters
with ``unique`` swapped for an order-preserving ``reduce`` against the same fixtures and require byte-identical output,
so a drift in ordering, de-duplication, key placement or formatting fails here rather than in a user's settings.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import merge_settings_permissions as msp
import pytest

_HAS_JQ = shutil.which("jq") is not None
_requires_jq = pytest.mark.skipif(not _HAS_JQ, reason="jq not installed on this host")

#: Order-preserving jq de-duplication standing in for the replaced filters' ``unique`` (first occurrence wins).
_DEDUPE = "reduce .[] as $x ([]; if index([$x]) then . else . + [$x] end)"
#: The replaced foundry allow filter (foundry setup Step 5, drops one obsolete rule).
_FOUNDRY_ALLOW = (
    '.permissions.allow = (((.permissions.allow // []) | map(select(. != "Bash(gh api repos/*:*)"))) + $perms[0]'
    f" | {_DEDUPE})"
)
#: The replaced oss allow filter (drops two retired rules).
_OSS_ALLOW = (
    '.permissions.allow = (((.permissions.allow // []) | map(select(. != "Bash(gh api repos/*:*)"'
    f' and . != "Bash(uv pip install:*)"))) + $perms[0] | {_DEDUPE})'
)
#: The replaced foundry deny filter (removes the retired list first).
_FOUNDRY_DENY = f".permissions.deny = ((((.permissions.deny // []) - $retired[0]) + $deny[0]) | {_DEDUPE})"
#: The replaced develop/research deny filter (additive only).
_ADDITIVE_DENY = f".permissions.deny = ((.permissions.deny // []) + $deny[0] | {_DEDUPE})"

_OLD_RULE = "Bash(gh api repos/*:*)"
_OVERLAY_RULE = "Bash(uv pip install:*)"
_SETTINGS_FIXTURES = [
    pytest.param({}, id="first-install"),
    pytest.param({"permissions": {"allow": [_OLD_RULE, _OLD_RULE, "Read"]}}, id="duplicate-stale-rules"),
    pytest.param({"permissions": None, "model": "x"}, id="null-permissions"),
    pytest.param({"permissions": {"allow": False, "deny": None}}, id="false-and-null-lists"),
    pytest.param(
        {
            "env": {"CUSTOM": "préservé ✓"},
            "permissions": {
                "defaultMode": "default",
                "allow": ["Bash(zzz:*)", _OVERLAY_RULE, "Bash(custom:*)"],
                "deny": ["Bash(gh pr merge:*)", "Bash(custom-deny:*)", "Bash(gh pr merge:*)"],
            },
            "statusLine": {"type": "command"},
        },
        id="preserve-user-settings",
    ),
]


@pytest.fixture(name="shipped")
def _shipped(tmp_path: Path) -> dict[str, Path]:
    """Shipped allow/deny/retired lists, deliberately unsorted and with duplicates."""
    files = {
        "allow": ["Bash(gh api repos/*)", "Read", "Bash(gh api repos/*)", "Bash(ls:*)"],
        "deny": ["Bash(git push -f:*)", "Bash(git push --force:*)"],
        "retired": ["Bash(gh pr merge:*)", "Bash(git branch -D:*)"],
    }
    paths = {name: tmp_path / f"{name}.json" for name in files}
    for name, rules in files.items():
        paths[name].write_text(json.dumps(rules), encoding="utf-8")
    return paths


@pytest.fixture(name="user_settings")
def _user_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Provide a ``~/.claude/settings.json`` under a temporary home (HOME on POSIX, USERPROFILE on Windows)."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return home / ".claude" / "settings.json"


def _run_jq(settings_text: str, filters: list[tuple[str, list[str]]]) -> str:
    """Apply each replaced jq filter in sequence, exactly as the setup blocks chained them."""
    text = settings_text
    for jq_filter, extras in filters:
        text = subprocess.run(
            ["jq", *extras, jq_filter], input=text, capture_output=True, text=True, check=True, encoding="utf-8"
        ).stdout
    return text


def _can_symlink() -> bool:
    """Probe whether this host lets an unprivileged process create a file symlink (native Windows often does not)."""
    probe = Path(tempfile.mkdtemp())
    try:
        (probe / "link").symlink_to(probe / "target")
    except OSError:
        return False
    finally:
        shutil.rmtree(probe, ignore_errors=True)
    return True


_requires_symlink = pytest.mark.skipif(not _can_symlink(), reason="host cannot create symlinks")


@_requires_jq
class TestMatchesReplacedJq:
    """The script's output is byte-identical to the replaced jq filters with their re-sort swapped for in-order
    dedupe."""

    @pytest.mark.parametrize("settings", _SETTINGS_FIXTURES)
    def test_foundry_allow_and_retiring_deny(
        self, settings: dict[str, object], shipped: dict[str, Path], user_settings: Path
    ) -> None:
        """Foundry merge: drop the obsolete allow rule, retire stale denies, append shipped rules, dedupe in order.

        The setup ran the allow filter, then the deny filter, on the same file; one script call must land the same
        bytes, including untouched sibling keys and non-ASCII values.
        """
        user_settings.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        expected = _run_jq(
            user_settings.read_text(encoding="utf-8"),
            [
                (_FOUNDRY_ALLOW, ["--slurpfile", "perms", str(shipped["allow"])]),
                (
                    _FOUNDRY_DENY,
                    ["--slurpfile", "deny", str(shipped["deny"]), "--slurpfile", "retired", str(shipped["retired"])],
                ),
            ],
        )
        rc = msp.main(
            [
                *("--allow", str(shipped["allow"]), "--drop-allow", _OLD_RULE),
                *("--deny", str(shipped["deny"]), "--retire-deny-file", str(shipped["retired"])),
            ]
        )
        assert rc == 0
        assert user_settings.read_text(encoding="utf-8") == expected

    @pytest.mark.parametrize("settings", _SETTINGS_FIXTURES)
    def test_two_dropped_allows_and_additive_deny(
        self, settings: dict[str, object], shipped: dict[str, Path], user_settings: Path
    ) -> None:
        """Sibling merge shape: two retired allow rules dropped, denies merged additively (stale denies kept).

        Covers the oss allow filter (two exact-match drops) and the develop/research additive deny filter, which must
        leave stale gh denies in place for foundry setup to scrub.
        """
        user_settings.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
        expected = _run_jq(
            user_settings.read_text(encoding="utf-8"),
            [
                (_OSS_ALLOW, ["--slurpfile", "perms", str(shipped["allow"])]),
                (_ADDITIVE_DENY, ["--slurpfile", "deny", str(shipped["deny"])]),
            ],
        )
        rc = msp.main(
            [
                *("--allow", str(shipped["allow"]), "--drop-allow", _OLD_RULE, "--drop-allow", _OVERLAY_RULE),
                *("--deny", str(shipped["deny"])),
            ]
        )
        assert rc == 0
        assert user_settings.read_text(encoding="utf-8") == expected


class TestMergeContract:
    """Reporting, idempotence and refusal paths."""

    def test_drop_allow_file_and_flag_both_drop(
        self,
        shipped: dict[str, Path],
        user_settings: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Rules named in ``--drop-allow-file`` and on ``--drop-allow`` are both removed, user rules stay.

        Setup passes the retired list as a file because the gh-write-guard hook blocks a ``python`` call that names ``gh
        api`` among its arguments; the file form must drop exactly what the flag form drops.
        """
        retired = tmp_path / "retired.json"
        retired.write_text(json.dumps([_OLD_RULE, "Bash(find:*)"]), encoding="utf-8")
        user_settings.write_text(
            json.dumps({"permissions": {"allow": [_OLD_RULE, "Bash(find:*)", "Bash(sed:*)", "Read"]}}),
            encoding="utf-8",
        )
        rc = msp.main(
            [
                *("--allow", str(shipped["allow"])),
                *("--drop-allow-file", str(retired)),
                *("--drop-allow", "Bash(sed:*)"),
            ]
        )
        assert rc == 0
        allow = json.loads(user_settings.read_text(encoding="utf-8"))["permissions"]["allow"]
        assert [rule for rule in allow if rule in {_OLD_RULE, "Bash(find:*)", "Bash(sed:*)"}] == []
        assert "Read" in allow
        assert capsys.readouterr().out.splitlines()[0].endswith("dropped 3")

    @pytest.mark.parametrize(
        ("retired_text", "pair"),
        [
            pytest.param("null", "allow", id="allow-null"),
            pytest.param('["Bash(find:*)", 7]', "allow", id="allow-non-string-entry"),
            pytest.param('{"rule": "Bash(find:*)"}', "allow", id="allow-object"),
            pytest.param("null", "deny", id="deny-null"),
            pytest.param('["Bash(gh pr merge:*)", null]', "deny", id="deny-non-string-entry"),
        ],
    )
    def test_damaged_retirement_list_stops_the_merge(
        self, retired_text: str, pair: str, shipped: dict[str, Path], user_settings: Path, tmp_path: Path
    ) -> None:
        """A retirement list that cannot name the rules to remove exits 1 and leaves settings byte-unchanged.

        A truncated retired file read as "retire nothing" would report success while the broad grants it exists to
        remove stay in place. An empty array is the legitimate "nothing retired" state and is not refused.
        """
        retired = tmp_path / "retired.json"
        retired.write_text(retired_text, encoding="utf-8")
        original = json.dumps({"permissions": {"allow": ["Read", "Bash(find:*)"], "deny": ["Bash(gh pr merge:*)"]}})
        user_settings.write_text(original, encoding="utf-8")
        flag = "--drop-allow-file" if pair == "allow" else "--retire-deny-file"
        rc = msp.main([*(f"--{pair}", str(shipped[pair])), *(flag, str(retired))])
        assert rc == 1
        assert user_settings.read_text(encoding="utf-8") == original

    @pytest.mark.parametrize(
        ("pair", "flag", "counted"),
        [
            pytest.param("allow", "--drop-allow-file", "dropped 0", id="allow-empty-array"),
            pytest.param("deny", "--retire-deny-file", "retired 0", id="deny-empty-array"),
        ],
    )
    def test_empty_retirement_array_retires_nothing(
        self,
        pair: str,
        flag: str,
        counted: str,
        shipped: dict[str, Path],
        user_settings: Path,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An empty retirement array is the valid "nothing retired" state: the merge succeeds and removes no rule.

        Strictness against a damaged retirement file must not turn the legitimate empty list into a failure, or a plugin
        that has retired nothing could never run its setup.
        """
        retired = tmp_path / "retired.json"
        retired.write_text("[]", encoding="utf-8")
        user_settings.write_text(json.dumps({"permissions": {pair: ["Bash(find:*)"]}}), encoding="utf-8")
        rc = msp.main([*(f"--{pair}", str(shipped[pair])), *(flag, str(retired))])
        assert rc == 0
        assert capsys.readouterr().out.splitlines()[0].endswith(counted)

    @pytest.mark.parametrize(
        ("base", "empty_flag", "label"),
        [
            pytest.param("allow", "--drop-allow-file", "retired allow file", id="drop-allow-file"),
            pytest.param("deny", "--retire-deny-file", "retired deny file", id="retire-deny-file"),
            pytest.param("deny", "--allow", "allow file", id="allow"),
            pytest.param("allow", "--deny", "deny file", id="deny"),
        ],
    )
    def test_empty_option_path_stops_the_merge(
        self,
        base: str,
        empty_flag: str,
        label: str,
        shipped: dict[str, Path],
        user_settings: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """An option given an empty path stops the merge and says the path is empty, not that a directory is unreadable.

        An unset shell variable expanding to ``--drop-allow-file ""`` must not skip the retirement and still report
        success, so settings stay byte-unchanged and the call exits 1. ``Path("")`` is the current directory, so without
        the explicit check the refusal would name ``.`` instead of the operand the caller passed.
        """
        original = json.dumps({"permissions": {"allow": ["Bash(find:*)"], "deny": ["Bash(gh pr merge:*)"]}})
        user_settings.write_text(original, encoding="utf-8")
        rc = msp.main([*(f"--{base}", str(shipped[base])), *(empty_flag, "")])
        assert rc == 1
        assert f"{label} path is empty" in capsys.readouterr().err
        assert user_settings.read_text(encoding="utf-8") == original

    def test_empty_settings_path_is_refused_by_name(
        self, shipped: dict[str, Path], capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--settings ""`` exits 4 and says the settings path is empty instead of printing the working directory.

        The write-root check resolves an empty operand to the current directory, so its refusal used to name that
        directory rather than the empty value the caller passed.
        """
        rc = msp.main([*("--allow", str(shipped["allow"])), *("--settings", "")])
        assert rc == 4
        assert "settings path is empty" in capsys.readouterr().err

    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param(["--drop-allow", "Bash(find:*)"], id="drop-allow-flag"),
            pytest.param(["--drop-allow-file", "retired.json"], id="drop-allow-file"),
            pytest.param(["--drop-allow-file", ""], id="drop-allow-file-empty"),
        ],
    )
    def test_drop_option_without_allow_is_refused(
        self, extra: list[str], shipped: dict[str, Path], user_settings: Path
    ) -> None:
        """A drop option given without ``--allow`` exits 2 instead of being silently ignored.

        With only ``--deny`` the allow merge never runs, so a dropped rule would stay in settings while the call
        reported success.
        """
        original = json.dumps({"permissions": {"allow": ["Bash(find:*)"]}})
        user_settings.write_text(original, encoding="utf-8")
        with pytest.raises(SystemExit) as exit_info:
            msp.main([*("--deny", str(shipped["deny"])), *extra])
        assert exit_info.value.code == 2
        assert user_settings.read_text(encoding="utf-8") == original

    def test_retire_deny_file_empty_without_deny_is_refused(
        self, shipped: dict[str, Path], user_settings: Path
    ) -> None:
        """An empty ``--retire-deny-file`` path without ``--deny`` still exits 2 instead of reading as option absent.

        A guard that tested the option by truthiness would let ``""`` through, skip the deny merge and exit 0 with the
        retired entries left in settings.
        """
        original = json.dumps({"permissions": {"deny": ["Bash(gh pr merge:*)"]}})
        user_settings.write_text(original, encoding="utf-8")
        with pytest.raises(SystemExit) as exit_info:
            msp.main([*("--allow", str(shipped["allow"])), *("--retire-deny-file", "")])
        assert exit_info.value.code == 2
        assert user_settings.read_text(encoding="utf-8") == original

    def test_retire_deny_file_without_deny_is_refused(self, shipped: dict[str, Path], user_settings: Path) -> None:
        """``--retire-deny-file`` given without ``--deny`` exits 2 instead of being silently ignored."""
        original = json.dumps({"permissions": {"deny": ["Bash(gh pr merge:*)"]}})
        user_settings.write_text(original, encoding="utf-8")
        with pytest.raises(SystemExit) as exit_info:
            msp.main([*("--allow", str(shipped["allow"])), *("--retire-deny-file", str(shipped["retired"]))])
        assert exit_info.value.code == 2
        assert user_settings.read_text(encoding="utf-8") == original

    def test_reports_counts(
        self, shipped: dict[str, Path], user_settings: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Stdout carries the added / already-present / removed counts the setup report quotes.

        One shipped allow rule (``Read``) is already present and the stale ``gh api`` rule is dropped; one retired deny
        is present and removed.
        """
        user_settings.write_text(
            json.dumps({"permissions": {"allow": [_OLD_RULE, "Read"], "deny": ["Bash(gh pr merge:*)"]}}),
            encoding="utf-8",
        )
        rc = msp.main(
            [
                *("--allow", str(shipped["allow"]), "--drop-allow", _OLD_RULE),
                *("--deny", str(shipped["deny"]), "--retire-deny-file", str(shipped["retired"])),
            ]
        )
        assert rc == 0
        assert capsys.readouterr().out.splitlines() == [
            "permissions.allow: added 2, already present 1, dropped 1",
            "permissions.deny: added 2, already present 0, retired 1",
        ]

    def test_keeps_user_rule_order(self, shipped: dict[str, Path], user_settings: Path) -> None:
        """User rules keep their hand-written order; new shipped rules follow in shipped-file order, each once.

        jq ``unique`` re-sorted the whole array on every setup run, so ``Bash(zzz:*)`` written first by the user moved
        to the end. The merge now only appends.
        """
        user_settings.write_text(
            json.dumps({"permissions": {"allow": ["Bash(zzz:*)", "Read", "Bash(aaa:*)"], "deny": ["Bash(zz:*)"]}}),
            encoding="utf-8",
        )
        rc = msp.main(["--allow", str(shipped["allow"]), "--deny", str(shipped["deny"])])
        assert rc == 0
        assert json.loads(user_settings.read_text(encoding="utf-8"))["permissions"] == {
            "allow": ["Bash(zzz:*)", "Read", "Bash(aaa:*)", "Bash(gh api repos/*)", "Bash(ls:*)"],
            "deny": ["Bash(zz:*)", "Bash(git push -f:*)", "Bash(git push --force:*)"],
        }

    @_requires_symlink
    def test_writes_through_symlinked_settings(
        self, shipped: dict[str, Path], user_settings: Path, tmp_path: Path
    ) -> None:
        """A ``settings.json`` symlinked into a dotfiles tree stays a link; the merge lands in the link's target.

        The replaced shell ``mv`` swapped the link for a regular file, silently detaching the user's dotfiles copy.
        """
        dotfiles = tmp_path / "dotfiles" / "settings.json"
        dotfiles.parent.mkdir()
        dotfiles.write_text("{}", encoding="utf-8")
        user_settings.symlink_to(dotfiles)
        rc = msp.main(["--allow", str(shipped["allow"])])
        assert rc == 0
        assert user_settings.is_symlink()
        assert json.loads(dotfiles.read_text(encoding="utf-8"))["permissions"]["allow"] == [
            "Bash(gh api repos/*)",
            "Read",
            "Bash(ls:*)",
        ]

    def test_second_run_is_idempotent(self, shipped: dict[str, Path], user_settings: Path) -> None:
        """Re-running setup leaves the merged file byte-identical."""
        user_settings.write_text(json.dumps({"permissions": {"allow": ["Bash(zzz:*)"]}}), encoding="utf-8")
        argv = ["--allow", str(shipped["allow"]), "--deny", str(shipped["deny"])]
        msp.main(argv)
        first = user_settings.read_bytes()
        rc = msp.main(argv)
        assert rc == 0
        assert user_settings.read_bytes() == first

    def test_keeps_file_mode(self, shipped: dict[str, Path], user_settings: Path) -> None:
        """The atomic replace keeps the settings file's permission bits instead of the temp file's 0600."""
        user_settings.write_text("{}", encoding="utf-8")
        os.chmod(user_settings, 0o640)
        before = stat.S_IMODE(user_settings.stat().st_mode)
        rc = msp.main(["--allow", str(shipped["allow"])])
        assert rc == 0
        assert stat.S_IMODE(user_settings.stat().st_mode) == before

    def test_home_env_wins_over_path_home(
        self, shipped: dict[str, Path], user_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default ``~/.claude/settings.json`` is the one under ``$HOME``, the file the setup bash backed up.

        On macOS and Linux ``Path.home()`` already reads ``HOME``, so the order is only provable by patching it; on
        native Windows it reads ``USERPROFILE``, which can name a different directory than ``$HOME``.
        """
        decoy = tmp_path / "decoy"
        (decoy / ".claude").mkdir(parents=True)
        decoy_settings = decoy / ".claude" / "settings.json"
        decoy_settings.write_text("{}", encoding="utf-8")
        user_settings.write_text("{}", encoding="utf-8")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: decoy))
        rc = msp.main(["--allow", str(shipped["allow"])])
        assert rc == 0
        assert "permissions" in json.loads(user_settings.read_text(encoding="utf-8"))
        assert decoy_settings.read_text(encoding="utf-8") == "{}"

    def test_path_home_is_the_fallback_without_home_env(
        self, shipped: dict[str, Path], user_settings: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With ``HOME`` unset the platform home directory is used, so a bare environment still resolves ``~``."""
        decoy = tmp_path / "decoy"
        (decoy / ".claude").mkdir(parents=True)
        decoy_settings = decoy / ".claude" / "settings.json"
        decoy_settings.write_text("{}", encoding="utf-8")
        monkeypatch.delenv("HOME")
        monkeypatch.setattr(Path, "home", classmethod(lambda cls: decoy))
        rc = msp.main(["--allow", str(shipped["allow"])])
        assert rc == 0
        assert "permissions" in json.loads(decoy_settings.read_text(encoding="utf-8"))

    @pytest.mark.parametrize(
        "relative",
        [
            pytest.param("outside/settings.json", id="outside-home-claude"),
            pytest.param("home/.claude/plugins/cache/x/settings.json", id="plugin-cache-below-claude"),
            pytest.param("home/.claude/settings.txt", id="not-json"),
        ],
    )
    def test_refuses_paths_outside_write_root(
        self, relative: str, tmp_path: Path, shipped: dict[str, Path], user_settings: Path
    ) -> None:
        """Only a ``.json`` file directly in ``~/.claude`` may be written; the plugin cache below it is refused."""
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("{}", encoding="utf-8")
        rc = msp.main(["--settings", str(target), "--allow", str(shipped["allow"])])
        assert rc == 4
        assert target.read_text(encoding="utf-8") == "{}"

    @pytest.mark.parametrize(
        ("settings_text", "deny_name"),
        [
            pytest.param('{"permissions": {"allow": "Read"}}', "deny", id="allow-not-an-array"),
            pytest.param("[1, 2]", "deny", id="settings-not-an-object"),
            pytest.param("{not json", "deny", id="settings-invalid-json"),
            pytest.param("{}", "absent", id="deny-file-missing"),
        ],
    )
    def test_bad_input_leaves_settings_unchanged(
        self, settings_text: str, deny_name: str, shipped: dict[str, Path], user_settings: Path, tmp_path: Path
    ) -> None:
        """Unmergeable input exits 1 and never writes — neither half of the allow/deny pair lands."""
        user_settings.write_text(settings_text, encoding="utf-8")
        rc = msp.main(["--allow", str(shipped["allow"]), "--deny", str(tmp_path / f"{deny_name}.json")])
        assert rc == 1
        assert user_settings.read_text(encoding="utf-8") == settings_text


def test_foundry_setup_passes_the_retirement_flags() -> None:
    """Foundry setup Step 5 drops exactly the obsolete allow rules and passes its retired-deny list.

    Pins the call site that replaced the two jq filters, so a lost ``--drop-allow-file`` or ``--retire-deny-file`` flag
    fails here instead of silently keeping stale rules in users' settings. The broad find/node/claude/sed allows were
    narrowed out of every plugin's allow list; setup must retire them from installed settings too, since the merge only
    adds. The rules come from a shipped file, never the command line: the gh-write-guard hook blocks a ``python`` call
    whose arguments name ``gh api``, so ``--drop-allow 'Bash(gh api repos/*:*)'`` would stop setup.
    """
    plugin_root = Path(__file__).resolve().parents[2]
    skill = (plugin_root / "skills" / "setup" / "SKILL.md").read_text(encoding="utf-8")
    calls = [line for line in skill.splitlines() if "bin/merge_settings_permissions.py" in line]
    assert len(calls) == 1
    assert "--drop-allow " not in calls[0]
    assert "gh api" not in calls[0]
    assert '--drop-allow-file "$PLUGIN_ROOT/.claude-plugin/permissions-allow-retired.json"' in calls[0]
    retired = json.loads(
        (plugin_root / ".claude-plugin" / "permissions-allow-retired.json").read_text(encoding="utf-8")
    )
    assert retired == [
        _OLD_RULE,
        "Bash(claude:*)",
        "Bash(find:*)",
        "Bash(node:*)",
        "Bash(sed:*)",
        "Bash(eval:*)",
    ]
    assert '--retire-deny-file "$PLUGIN_ROOT/.claude-plugin/permissions-deny-retired.json"' in calls[0]
