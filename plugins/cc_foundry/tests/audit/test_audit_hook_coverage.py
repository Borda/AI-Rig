"""Tests for audit_hook_coverage.py — transcript parsing, verdict attribution, filters.

The measurement this tool produces is only trustworthy if two properties hold: it unions every installed manifest
(probing one plugin's copy under-reports, because a block owned by one plugin is passed through by the others) and it
can exclude sessions that predate a hook. Both are pinned here, alongside the parsing that feeds them.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

import audit_hook_coverage as ahc


def _write_transcript(path: Path, records: list[dict]) -> Path:
    """Replace a transcript with UTF-8 JSONL records, preserving their order.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     path = _write_transcript(Path(directory) / "events.jsonl", [{"id": 1}, {"id": 2}])
    ...     [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    [{'id': 1}, {'id': 2}]
    """
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return path


def _bash_record(command: str) -> dict:
    """Wrap command text in the transcript's assistant tool-use content shape.

    >>> _bash_record("echo example")["message"]["content"]
    [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'echo example'}}]
    """
    return {"message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}


def _make_args(**overrides) -> argparse.Namespace:
    """Build collection arguments with optional overrides over the unfiltered defaults.

    >>> vars(_make_args(skills_only=True))
    {'since': None, 'project': None, 'skills_only': True}
    """
    values = {"since": None, "project": None, "skills_only": False}
    values.update(overrides)
    return argparse.Namespace(**values)


def _fake_cache(root: Path, plugins: dict[str, dict[str, list[str]]]) -> Path:
    """Create versioned plugin directories and schema-one manifests containing the supplied digests.

    >>> from tempfile import TemporaryDirectory
    >>> with TemporaryDirectory() as directory:
    ...     root = _fake_cache(Path(directory), {"example": {"1.0.0": ["abc"]}})
    ...     manifest = json.loads((root / "example/1.0.0/blueprint-manifest.json").read_text())
    ...     manifest["schema"], manifest["entries"]
    (1, {'abc': {'src': 'x.md:1', 'kind': 'command'}})
    """
    for plugin, versions in plugins.items():
        for version, digests in versions.items():
            plugin_dir = root / plugin / version
            plugin_dir.mkdir(parents=True)
            entries = {digest: {"src": "x.md:1", "kind": "command"} for digest in digests}
            (plugin_dir / "blueprint-manifest.json").write_text(
                json.dumps({"schema": 1, "plugin": plugin, "entries": entries}), encoding="utf-8"
            )
    return root


class TestVerdictOf:
    """Mechanism attribution for a single command."""

    @pytest.mark.parametrize(
        ("owner", "shape", "expected"),
        [
            pytest.param("oss", False, "blueprint:oss", id="manifest-hit"),
            pytest.param(None, True, "shape", id="shape-hit"),
            pytest.param(None, False, "none", id="no-hit"),
            pytest.param("foundry", True, "blueprint:foundry", id="blueprint-wins-over-shape"),
        ],
    )
    def test_labels_by_mechanism(self, owner, shape, expected):
        """Each (manifest, shape) combination maps to one stable label.

        The last case is the one that matters for the split the report prints: when both
        mechanisms would allow, the call must be attributed to blueprint, or the shape
        hook absorbs credit for exact-match coverage and the two look interchangeable.
        """
        assert ahc.verdict_of(owner, shape) == expected


class TestRate:
    """Share formatting, including the empty-denominator case."""

    def test_formats_percentage(self):
        """A share renders with a single decimal."""
        assert ahc.rate(72, 1140) == "6.3%"

    def test_empty_denominator_is_not_an_error(self):
        """Zero examined calls yields `n/a` rather than raising.

        Filters can legitimately match no transcript at all (a project substring that hits nothing); the summary must
        still print instead of dying on ZeroDivision.
        """
        assert ahc.rate(0, 0) == "n/a"


class TestParseSince:
    """ISO date to timestamp conversion."""

    def test_rejects_non_iso_text(self):
        """A malformed date fails loudly instead of silently disabling the filter.

        Silently ignoring it would include pre-hook sessions and quietly deflate the measured rate — the exact
        contamination ``--since`` exists to remove.
        """
        with pytest.raises(ValueError):
            ahc.parse_since("last tuesday")


class TestReadTranscript:
    """Command and skill extraction from a session log."""

    def test_extracts_bash_commands_in_order(self, tmp_path):
        """Bash tool_use commands come back in execution order."""
        path = _write_transcript(tmp_path / "s.jsonl", [_bash_record("ls"), _bash_record("pwd")])
        commands, _ = ahc.read_transcript(path)
        assert commands == ["ls", "pwd"]

    def test_ignores_other_tools(self, tmp_path):
        """Only Bash calls count toward the denominator.

        Read/Edit/Grep calls never face the Bash permission gate, so counting them would inflate the denominator with
        calls no hook could ever allow.
        """
        other = {"message": {"content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "a"}}]}}
        path = _write_transcript(tmp_path / "s.jsonl", [other, _bash_record("ls")])
        commands, _ = ahc.read_transcript(path)
        assert commands == ["ls"]

    def test_survives_malformed_lines(self, tmp_path):
        """A truncated or non-JSON line is skipped, not fatal.

        Transcripts of live sessions are appended to while being read, so the final line is routinely a partial write.
        """
        path = tmp_path / "s.jsonl"
        path.write_text(json.dumps(_bash_record("ls")) + "\n{not json\n", encoding="utf-8")
        commands, _ = ahc.read_transcript(path)
        assert commands == ["ls"]

    def test_detects_invoked_skills(self, tmp_path):
        """Skill invocations are collected as `plugin:skill` names."""
        path = _write_transcript(tmp_path / "s.jsonl", [{"content": "<command-name>/oss:review</command-name>"}])
        _, skills = ahc.read_transcript(path)
        assert skills == {"oss:review"}


class TestLoadDigests:
    """Manifest union across installed plugins."""

    def test_unions_every_plugin(self, tmp_path, monkeypatch):
        """Digests from all installed plugins are merged into one lookup.

        A block committed in `foundry` is allowed by foundry's hook copy and passed through by every other plugin's
        copy; since all copies run on each Bash call, the effective set is the union. Probing one plugin under-reports
        coverage.
        """
        monkeypatch.setattr(
            ahc, "PLUGIN_CACHE", _fake_cache(tmp_path, {"oss": {"1.0.0": ["aa"]}, "foundry": {"1.0.0": ["bb"]}})
        )
        assert ahc.load_digests() == {"aa": "oss", "bb": "foundry"}

    def test_uses_newest_version_only(self, tmp_path, monkeypatch):
        """Only the newest installed version of a plugin contributes digests.

        Stale versions linger in the cache; counting them would credit coverage to manifest text that is no longer what
        runs.
        """
        monkeypatch.setattr(ahc, "PLUGIN_CACHE", _fake_cache(tmp_path, {"oss": {"0.9.0": ["old"], "0.10.0": ["new"]}}))
        assert ahc.load_digests() == {"new": "oss"}

    def test_version_selection_holds_for_nested_paths(self, tmp_path, monkeypatch):
        """Version ranking uses the directory under the plugin, not the file's parent.

        The shape hook sits at `<plugin>/<version>/hooks/<file>`, so keying on the immediate parent would compare the
        literal string `hooks` for every candidate and fall back to glob order.
        """
        for version in ("0.9.0", "0.10.0"):
            hooks = tmp_path / "foundry" / version / "hooks"
            hooks.mkdir(parents=True)
            (hooks / "sentinel-read-allow.js").write_text("", encoding="utf-8")
        monkeypatch.setattr(ahc, "PLUGIN_CACHE", tmp_path)
        picked = ahc.newest_per_plugin("hooks/sentinel-read-allow.js")
        assert [path.parent.parent.name for path in picked] == ["0.10.0"]


class TestClassifier:
    """Verdict assignment and memoization."""

    def test_manifest_hit_skips_the_shape_probe(self, monkeypatch):
        """A manifest match never spawns the shape hook subprocess.

        Every distinct command otherwise costs a node process; short-circuiting on the cheaper in-process check is what
        keeps a full-history scan tractable.
        """
        probed: list[str] = []
        classifier = ahc.Classifier({ahc.sha256_text(ahc.normalize("ls -la")): "oss"}, Path("unused.js"))
        monkeypatch.setattr(classifier, "_shape_allows", lambda command: probed.append(command) or False)
        assert classifier.verdict("ls -la") == "blueprint:oss"
        assert probed == []

    def test_memoizes_by_exact_text(self, monkeypatch):
        """Repeat commands are classified once.

        Sessions re-run identical commands constantly; without the cache the scan cost scales with call count rather
        than distinct-command count.
        """
        calls: list[str] = []
        classifier = ahc.Classifier({}, Path("unused.js"))
        monkeypatch.setattr(classifier, "_shape_allows", lambda command: bool(calls.append(command)))
        classifier.verdict("pwd")
        classifier.verdict("pwd")
        assert calls == ["pwd"]

    def test_missing_shape_hook_degrades_to_manifest_only(self):
        """With no installed shape hook, commands fall through to `none`.

        A partial install must still produce a manifest-only number instead of crashing on a missing path.
        """
        assert ahc.Classifier({}, None).verdict("pwd") == "none"

    def test_shape_hook_receives_one_node_argv_and_payload(self, monkeypatch):
        """A shape miss invokes the installed hook once with the Bash command payload."""
        observed: dict[str, object] = {}

        def _fake_run(*args, **kwargs):
            """Capture the shape-hook subprocess invocation and allow it."""
            observed["args"] = args
            observed["kwargs"] = kwargs
            return type("Result", (), {"stdout": '{"permissionDecision":"allow"}'})()

        monkeypatch.setattr(ahc.subprocess, "run", _fake_run)
        classifier = ahc.Classifier({}, Path("shape-hook.js"))

        assert classifier.verdict("pwd") == "shape"
        assert observed == {
            "args": (["node", "shape-hook.js"],),
            "kwargs": {
                "input": '{"tool_name": "Bash", "tool_input": {"command": "pwd"}}',
                "capture_output": True,
                "text": True,
                "check": False,
            },
        }


class TestCollect:
    """Transcript selection filters."""

    @pytest.fixture(name="transcripts")
    def _transcripts(self, tmp_path, monkeypatch):
        """Two projects, one session each; one invokes a skill."""
        root = tmp_path / "projects"
        plain = root / "proj-alpha"
        skilled = root / "proj-beta"
        plain.mkdir(parents=True)
        skilled.mkdir(parents=True)
        _write_transcript(plain / "a.jsonl", [_bash_record("ls")])
        _write_transcript(
            skilled / "b.jsonl", [{"content": "<command-name>/oss:review</command-name>"}, _bash_record("pwd")]
        )
        monkeypatch.setattr(ahc, "TRANSCRIPT_ROOT", root)
        return root

    def test_project_filter_selects_by_substring(self, transcripts):
        """Narrow to matching project directories."""
        _, sessions = ahc.collect(_make_args(project="beta"), ahc.Classifier({}, None))
        assert [row["project"] for row in sessions] == ["proj-beta"]

    def test_skills_only_drops_ad_hoc_sessions(self, transcripts):
        """Keep sessions that invoked a plugin skill.

        Ad-hoc engineering sessions run almost no blueprint text, so mixing them in dilutes the rate toward zero and
        hides what skill runs actually achieve.
        """
        _, sessions = ahc.collect(_make_args(skills_only=True), ahc.Classifier({}, None))
        assert [row["session"] for row in sessions] == ["b"]

    def test_since_excludes_older_transcripts(self, transcripts):
        """Exclude transcripts last modified before the requested cutoff."""
        _, sessions = ahc.collect(_make_args(since="2099-01-01"), ahc.Classifier({}, None))
        assert sessions == []

    def test_vanished_transcript_is_skipped(self, transcripts, monkeypatch):
        """A transcript deleted between glob and read does not abort the scan.

        Live sessions rotate their logs while the scan walks the tree; an unhandled FileNotFoundError there loses every
        result gathered so far.
        """
        original = ahc.read_transcript

        def _exploding(path: Path):
            """Raise only for the transcript that vanishes during this scan."""
            if path.name == "a.jsonl":
                raise FileNotFoundError(path)
            return original(path)

        monkeypatch.setattr(ahc, "read_transcript", _exploding)
        tally, sessions = ahc.collect(_make_args(), ahc.Classifier({}, None))
        assert [row["session"] for row in sessions] == ["b"]
        assert tally["none"] == 1
