"""Exercise the shipped pagination and cleanup examples and check deserialization guidance."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from itertools import takewhile
from pathlib import Path

import pytest
from _hook_env import _bash_runs_posix_script

_RULES = Path(__file__).resolve().parents[2] / "rules"
_BASH = pytest.mark.skipif(not _bash_runs_posix_script(), reason="requires Bash executing POSIX scripts")
_JQ = pytest.mark.skipif(shutil.which("jq") is None, reason="pagination example requires jq")
_FAKE_CURL = """
first_page=$1 second_page=$2 failure=$3 next_query=$4
curl() {
    case "${@: -1}" in
        */items) printf '%s' "$first_page" ;;
        *"?$next_query")
            if [ "$failure" = transport ]; then return 7; fi
            if [ "$failure" = http ] && [[ "$1" == -*f* ]]; then return 22; fi
            if [ "$failure" = nul ]; then printf '%b' "$second_page"; return; fi
            printf '%s' "$second_page"
            if [ "$failure" = interrupted ]; then return 18; fi ;;
        *) return 64 ;;
    esac
}
"""


def _section(name: str, heading: str) -> list[str]:
    """Return the lines under the named level-two heading."""
    lines = (_RULES / name).read_text(encoding="utf-8").splitlines()
    return list(takewhile(lambda line: not line.startswith("## "), lines[lines.index(heading) + 1 :]))


def _bash_example(name: str, heading: str) -> str:
    """Extract the Bash example without altering the commands under test."""
    lines = _section(name, heading)
    start = lines.index("```bash") + 1
    return "\n".join(lines[start : lines.index("```", start)])


def _paginate(
    cwd: Path, first_page: str, second_page: str = "", failure: str = "", next_query: str = "pageToken=next"
) -> subprocess.CompletedProcess[str]:
    """Run the example with local responses and print items only after the loop completes."""
    example = _bash_example("_full/external-data.md", "## Cloud / Google-style APIs (next_page_token)")
    publish = 'printf "COMPLETE\\n"\nfor item in "${all_items[@]}"; do printf "%s\\n" "$item"; done'
    return subprocess.run(
        ["bash", "-c", f"{_FAKE_CURL}\n{example}\n{publish}", "bash", first_page, second_page, failure, next_query],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        timeout=15,
    )


def test_pickle_is_prohibited_for_untrusted_data() -> None:
    """Require the trust boundary for all documented pickle deserialization entry points."""
    lines = _section("python-code.md", "## Security")
    for api in ("pickle.load", "pickle.loads", "pickle.Unpickler"):
        assert any(f"`{api}`" in line and "Never" in line and "untrusted" in line for line in lines)
    assert all("weights_only" not in line for line in lines if "pickle" in line)


def test_torch_uses_the_checkpoint_safeguard() -> None:
    """Require PyTorch's supported weights-only option for external checkpoints."""
    lines = _section("python-code.md", "## Security")
    assert any("`torch.load`" in line and "`weights_only=True`" in line and "external" in line for line in lines)


@_BASH
@_JQ
@pytest.mark.parametrize(
    ("failure", "second_page"),
    [
        pytest.param("transport", "", id="transport-failure"),
        pytest.param("interrupted", '{"items": [2]}', id="read-failure-with-valid-json-prefix"),
        pytest.param("http", '{"items": [], "message": "denied"}', id="http-failure-with-json-body"),
        pytest.param("", "", id="empty-response"),
        pytest.param("", '{"items": [', id="malformed-json"),
        pytest.param("nul", r'{"items":[]}\0', id="embedded-nul-byte"),
        pytest.param("", '{"items": []}\n{"items": []}', id="multiple-json-documents"),
        pytest.param("", "[]", id="non-object-page"),
        pytest.param("", "{}", id="missing-items"),
        pytest.param("", '{"items": {}}', id="non-array-items"),
        pytest.param("", '{"items": [], "nextPageToken": false}', id="non-string-token"),
    ],
)
def test_pagination_failure_cannot_publish_prior_pages(tmp_path: Path, failure: str, second_page: str) -> None:
    """Abort on second-page failure before a consumer sees first-page data."""
    result = _paginate(tmp_path, '{"items": [1], "nextPageToken": "next"}', second_page, failure)

    assert (result.returncode, result.stdout) == (1, "")


@_BASH
@_JQ
@pytest.mark.parametrize(
    "page", ['{"items": []}', '{"items": [], "nextPageToken": null}', '{"items": [], "nextPageToken": ""}']
)
def test_valid_empty_page_can_complete(tmp_path: Path, page: str) -> None:
    """Complete on each valid empty-token shape without inserting a phantom item."""
    result = _paginate(tmp_path, page)

    assert (result.returncode, result.stdout) == (0, "COMPLETE\n"), result.stderr


@_BASH
@_JQ
def test_pagination_keeps_each_item_as_one_json_value(tmp_path: Path) -> None:
    """Keep strings, globs, structured items and scalars intact across page boundaries."""
    (tmp_path / "match.txt").touch()
    first_items = ["two words", "*.txt", "line\nbreak\t\\end", {"name": "two words"}]
    last_items = [[1, "two words"], "", False, None, 42]
    result = _paginate(
        tmp_path, json.dumps({"items": first_items, "nextPageToken": "next"}), json.dumps({"items": last_items})
    )

    assert result.returncode == 0, result.stderr
    completion, *lines = result.stdout.splitlines()
    assert completion == "COMPLETE"
    assert [json.loads(line) for line in lines] == first_items + last_items


@_BASH
@_JQ
def test_pagination_encodes_the_opaque_token(tmp_path: Path) -> None:
    """Follow a token containing query delimiters and trailing whitespace through an empty first page."""
    page = json.dumps({"items": [], "nextPageToken": "a&b+ c/\n"})
    result = _paginate(tmp_path, page, '{"items": [2]}', next_query="pageToken=a%26b%2B%20c%2F%0A")

    assert (result.returncode, result.stdout) == (0, "COMPLETE\n2\n"), result.stderr


@pytest.fixture
def cleanup_tree(tmp_path: Path) -> Path:
    """Create the roots expected by the cleanup recipe and an unrelated root-level path."""
    for root in (
        ".reports/calibrate",
        ".reports/resolve",
        ".reports/audit",
        ".reports/analyse",
        ".experiments",
        ".developments",
        ".reports/review",
        ".plans/blueprint",
        ".cache",
        ".temp",
    ):
        (tmp_path / root).mkdir(parents=True)
    (tmp_path / "tail").touch()
    return tmp_path


@_BASH
@pytest.mark.parametrize(
    ("root", "prefix", "marker"),
    [
        pytest.param(".reports/calibrate", "job", "result.jsonl", id="completed-run"),
        pytest.param(".reports/review", "review", None, id="flat-review"),
        pytest.param(".reports/review/pr-7", "run-008", None, id="pr-review"),
        pytest.param(".cache", "entry", "", id="old-file"),
    ],
)
def test_cleanup_preserves_path_boundaries(cleanup_tree: Path, root: str, prefix: str, marker: str | None) -> None:
    """Delete old selected paths with spaces or apostrophes and preserve fresh and unrelated neighbors."""
    parent = cleanup_tree / root
    parent.mkdir(parents=True, exist_ok=True)
    old = time.time() - 40 * 86400
    selected = [parent / f"{prefix} tail", parent / f"{prefix} 'archive"]
    neighbors = [parent / prefix, parent / f"{prefix} fresh"]
    for target in [*selected, *neighbors]:
        if marker == "":
            target.touch()
        else:
            target.mkdir()
            if marker:
                (target / marker).touch()
        if target in selected:
            os.utime(target / marker if marker else target, (old, old))
    example = _bash_example("foundry-config.md", "## Cleanup Hook (SessionEnd)")
    result = subprocess.run(
        ["bash", "-c", example], cwd=cleanup_tree, capture_output=True, encoding="utf-8", timeout=15
    )

    assert result.returncode == 0, result.stderr
    assert all(path.exists() for path in [cleanup_tree / "tail", *neighbors])
    assert not any(path.exists() for path in selected)


@_BASH
@pytest.mark.parametrize(
    ("relative", "is_dir"),
    [
        pytest.param(".reports/calibrate/result.jsonl", False, id="marker-outside-run"),
        pytest.param(".reports/calibrate/job/result.jsonl", True, id="marker-is-directory"),
        pytest.param(".reports/review/legacy/run-old", True, id="nested-run-outside-pr"),
    ],
)
def test_cleanup_preserves_paths_outside_selected_layouts(cleanup_tree: Path, relative: str, is_dir: bool) -> None:
    """Require both the selected path's expected location and its type before deletion."""
    target = cleanup_tree / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    if is_dir:
        target.mkdir()
    else:
        target.touch()
    old = time.time() - 40 * 86400
    os.utime(target, (old, old))
    example = _bash_example("foundry-config.md", "## Cleanup Hook (SessionEnd)")
    result = subprocess.run(
        ["bash", "-c", example], cwd=cleanup_tree, capture_output=True, encoding="utf-8", timeout=15
    )

    assert result.returncode == 0, result.stderr
    assert target.exists()
