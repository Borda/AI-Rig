"""Tests for the shared evolution contract module: versions, identities, store I/O and path containment."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from _evolution_common import (
    StorePathError,
    append_jsonl,
    atomic_write_json,
    entry_ids,
    last_by_key,
    normalise_entry_text,
    parse_remote_url,
    parse_version,
    python_symbols_changed,
    read_jsonl,
    repair_tail,
    resolve_store_root,
)
from _evolution_repos import RepoBuilder


@pytest.mark.parametrize(
    ("tag", "canonical", "pre"),
    [
        pytest.param("v1.2.0", "1.2.0", False, id="v-prefix"),
        pytest.param("1.2.0rc1", "1.2.0rc1", True, id="pep440-rc"),
        pytest.param("1.2.0-rc.1", "1.2.0rc1", True, id="semver-rc"),
        pytest.param("1.2.0.post1", "1.2.0.post1", False, id="post"),
        pytest.param("1.2.0.dev3", "1.2.0.dev3", True, id="dev"),
        pytest.param("2024.01.15", "2024.1.15", False, id="calver"),
        pytest.param("1.0.0+build.5", "1.0.0", False, id="local-version"),
    ],
)
def test_parse_version_accepts_pep440_and_semver(tag: str, canonical: str, pre: bool) -> None:
    """Tags in PEP 440 or SemVer form parse to their canonical text and pre-release flag."""
    version = parse_version(tag)
    assert version is not None
    assert (version.canonical, version.is_prerelease) == (canonical, pre)


@pytest.mark.parametrize("tag", [pytest.param("devtools-1.0", id="dev-substring"), pytest.param("latest", id="word")])
def test_parse_version_rejects_non_versions(tag: str) -> None:
    """Tag names that only contain version-like substrings are not versions."""
    assert parse_version(tag) is None


def test_version_sort_key_follows_pep440_order() -> None:
    """Dev, alpha, beta, rc, final and post releases sort in PEP 440 order."""
    ordered = ["1.0.0.dev1", "1.0.0a1", "1.0.0b2", "1.0.0rc1", "1.0.0", "1.0.0.post1", "1.0.1", "1.10.0"]
    shuffled = list(reversed(ordered))
    assert sorted(shuffled, key=lambda tag: parse_version(tag).sort_key()) == ordered


def test_entry_ids_are_stable_and_suffix_repeats() -> None:
    """Reference edits keep an entry's ID, and identical texts in one section get occurrence suffixes."""
    first = entry_ids("1.0.0", ["Fix crash in loader", "Fix crash in loader (#12)", "Other"])
    assert first[1] == f"{first[0]}~2"
    assert entry_ids("1.0.0", ["Fix crash in loader (#99)"])[0] == first[0]
    assert normalise_entry_text("Fix [docs](https://x) **now** (abc1234)") == "fix docs now"


def test_read_jsonl_drops_torn_tail_and_keeps_last_record(tmp_path: Path) -> None:
    """A torn last line is ignored on read and truncated before the next append."""
    path = tmp_path / "rows.jsonl"
    append_jsonl(path, [{"fix_id": "a", "v": 1}, {"fix_id": "a", "v": 2}])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"fix_id": "b"')
    records, dropped = read_jsonl(path)
    assert dropped == 1
    assert last_by_key(records, "fix_id")["a"]["v"] == 2
    assert repair_tail(path) > 0
    append_jsonl(path, [{"fix_id": "c"}])
    records, dropped = read_jsonl(path)
    assert dropped == 0
    assert [record["fix_id"] for record in records] == ["a", "a", "c"]


def test_atomic_write_json_replaces_file(tmp_path: Path) -> None:
    """The manifest writer leaves a complete JSON file and no temp file behind."""
    path = tmp_path / "store" / "manifest.json"
    atomic_write_json(path, {"schema_version": 1})
    atomic_write_json(path, {"schema_version": 1, "scope": "all"})
    assert json.loads(path.read_text(encoding="utf-8"))["scope"] == "all"
    assert [p.name for p in path.parent.iterdir()] == ["manifest.json"]


@pytest.mark.parametrize(
    ("url", "parts"),
    [
        pytest.param("https://github.com/Owner/Repo.git", ["github.com", "Owner", "Repo"], id="https"),
        pytest.param("git@github.com:o/r.git", ["github.com", "o", "r"], id="scp"),
        pytest.param("ssh://git@gitlab.com:22/g/s/p.git", ["gitlab.com", "g", "s", "p"], id="ssh-nested"),
        pytest.param("https://github.com/../etc", None, id="dotdot"),
    ],
)
def test_parse_remote_url(url: str, parts: list[str] | None) -> None:
    """Remote URLs split into safe store path components; unsafe ones are refused."""
    assert parse_remote_url(url) == parts


def test_resolve_store_root_rejects_unsafe_components(tmp_path: Path) -> None:
    """Dot-dot and separator components never reach the filesystem."""
    evolution = tmp_path / ".evolution"
    with pytest.raises(StorePathError):
        resolve_store_root(evolution, ["github.com", "..", "x"])
    with pytest.raises(StorePathError):
        resolve_store_root(evolution, ["github.com", "a/b", "x"])
    paths = resolve_store_root(evolution, ["github.com", "o", "r"])
    assert paths.manifest == evolution / "github.com" / "o" / "r" / "store" / "manifest.json"


def test_python_symbols_changed_reports_added_and_modified(evo_repo: RepoBuilder) -> None:
    """A commit that edits one function and adds another reports both; ``added_only`` keeps only the new one.

    Symbol overlap is a scoring signal, so a modified method must be reported by its qualified class path.
    """
    evo_repo.write("pkg/m.py", "def a():\n    return 1\n\n\nclass K:\n    def m(self):\n        return 2\n")
    evo_repo.commit("base", day=0)
    evo_repo.write(
        "pkg/m.py",
        "def a():\n    return 1\n\n\nclass K:\n    def m(self):\n        return 3\n\n    def n(self):\n        return 4\n",
    )
    sha = evo_repo.commit("change", day=1)
    assert python_symbols_changed(evo_repo.path, sha) == ["pkg/m.py:K", "pkg/m.py:K.m", "pkg/m.py:K.n"]
    assert python_symbols_changed(evo_repo.path, sha, added_only=True) == ["pkg/m.py:K.n"]


@pytest.mark.skipif(os.name == "nt", reason="creating symlinks needs elevated rights on native Windows")
def test_resolve_store_root_rejects_symlinked_component(tmp_path: Path) -> None:
    """A symlinked component below ``.evolution/`` is refused before any read or write."""
    evolution = tmp_path / ".evolution"
    (evolution / "github.com").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (evolution / "github.com" / "o").symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorePathError):
        resolve_store_root(evolution, ["github.com", "o", "r"])
