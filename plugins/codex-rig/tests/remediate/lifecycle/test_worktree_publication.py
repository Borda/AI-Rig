"""Exercise the one-attempt publication transport against disposable Git repositories."""

import errno
import hashlib
import json
import os
import shutil
import stat
import subprocess
import tempfile
from pathlib import Path

import pytest
from _modules import load_module
from _platform import FILE_SYMLINKS_AVAILABLE
from _remediation_cleanup import record_destination as _record_destination
from _remediation_cleanup import record_pr_target as _record_pr_target

PLUGIN_ROOT = Path(__file__).resolve().parents[3]
PUBLICATION = load_module("worktree_publication", PLUGIN_ROOT / "shared" / "worktree_publication.py")
BRANCH = "refs/heads/change"
TARGET = "refs/heads/main"
#: The credential option every CLI call carries, as the publication gate always passes at least one helper.
HELPER_ARGS = ["--credential-helper", "osxkeychain"]


def _git(repo: Path, *args: str, input_text: str | None = None) -> str:
    """Run Git for a private fixture repository without inheriting user configuration."""
    # Bytes in and out: text-mode stdin turns every line feed into CRLF on Windows and Git rejects the corrupt object.
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        input=None if input_text is None else input_text.encode(),
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


@pytest.fixture(autouse=True)
def private_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every home-directory variable at an empty directory so no test reads the real user's Git files."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    return home


@pytest.fixture
def publication_repositories(tmp_path: Path) -> tuple[Path, Path, str, str, str]:
    """Build a source with three commits and an isolated destination on their approved parent."""
    source, remote = tmp_path / "source", tmp_path / "remote"
    source.mkdir()
    remote.mkdir()
    _git(source, "init", "--initial-branch=source")
    _git(remote, "init", "--bare", "--template=")
    _git(source, "config", "user.name", "Fixture")
    _git(source, "config", "user.email", "fixture@example.invalid")
    _git(source, "config", "core.hooksPath", str(source / "empty-hooks"))
    (source / "tracked.txt").write_text("committed content\n", encoding="utf-8")
    _git(source, "add", "--", "tracked.txt")
    tree = _git(source, "write-tree")
    first = _git(source, "commit-tree", tree, input_text="Initial fixture\n")
    old = _git(source, "commit-tree", tree, "-p", first, input_text="Approved tip\n")
    new = _git(source, "commit-tree", tree, "-p", old, input_text="Chosen tip\n")
    _git(source, "update-ref", "HEAD", new)
    _git(source, "read-tree", "HEAD")
    (remote / "objects" / "info" / "alternates").write_bytes(os.fsencode(source / ".git" / "objects") + b"\n")
    _git(remote, "update-ref", BRANCH, old)
    _git(remote, "update-ref", TARGET, first)
    return source, remote, first, old, new


def _push(
    source: Path,
    remote: Path,
    new: str,
    old: str,
    common_dir: Path | None = None,
    branch: str = "source",
    **options: object,
) -> None:
    """Run the guarded transport on the fixture's approved branch and local endpoint.

    A ``file://`` endpoint asks no credential helper, so the helper chain is empty unless a test passes one.
    """
    PUBLICATION._isolated_push(
        shutil.which("git"),
        source,
        remote.as_uri(),
        options.pop("ref", BRANCH),
        new,
        old,
        options.pop("credential_helpers", ()),
        branch=branch,
        common_dir=common_dir or source / ".git",
        base=(remote.as_uri(), TARGET, _git(remote, "rev-parse", TARGET)),
        protocol="file",
        **options,
    )


@pytest.mark.integration
@pytest.mark.parametrize("topology", ["same-repository", "fork"])
@pytest.mark.parametrize(
    ("base_change", "expected"),
    [
        pytest.param("unseen", "publication-target-not-integrated", id="advanced-outside-source-history"),
        pytest.param("diverged", "publication-target-not-integrated", id="known-but-unmerged-target"),
        pytest.param("included", "pushed", id="advanced-to-included-commit"),
        pytest.param("rewound", "publication-target-diverged", id="rewound-to-included-ancestor"),
        pytest.param("deleted", "publication-target-unverifiable", id="target-disappeared"),
    ],
)
def test_push_checks_live_base_after_rehearsal(
    publication_repositories,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    topology: str,
    base_change: str,
    expected: str,
) -> None:
    """Check the real base after rehearsal, including when a fork carries a stale branch of that name."""
    source, destination, first, old, new = publication_repositories
    upstream = destination
    if topology == "fork":
        upstream = tmp_path / "upstream.git"
        _git(tmp_path, "clone", "--bare", "--no-local", destination.as_uri(), str(upstream))
    head_repository = "upstream/widgets" if topology == "same-repository" else "contributor/widgets"
    head_url, base_url = f"https://github.com/{head_repository}.git", "https://github.com/upstream/widgets.git"
    offer = {
        "checkout": source.as_posix(),
        "common_dir": (source / ".git").as_posix(),
        "branch": "source",
        "source_oid": new,
        "expected_oid": old,
        "ref": BRANCH,
        "url": head_url,
    }
    run = tmp_path / "run"
    run.mkdir()
    captured = old if base_change == "rewound" else first
    if base_change == "rewound":
        _git(upstream, "update-ref", TARGET, old, first)
    digest = _open_attempt(run, offer, captured=captured)
    # Move the host's base only AFTER Git successfully rehearsed publication of this unchanged PR head.
    tree = _git(upstream, "rev-parse", f"{first}^{{tree}}")
    unseen = _git(
        upstream,
        "-c",
        "user.name=Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit-tree",
        tree,
        "-p",
        first,
        "-m",
        "Concurrent target update",
    )
    if base_change == "diverged":
        # A known local object still has to be an ancestor of the approved commit.
        commit = _git(upstream, "cat-file", "commit", unseen) + "\n"
        assert _git(source, "hash-object", "-w", "-t", "commit", "--stdin", input_text=commit) == unseen
    if base_change == "deleted":
        # ls-remote's pattern still returns this unrelated ref after the true target disappears.
        _git(upstream, "update-ref", f"refs/heads/shadow/{TARGET}", first)
    refs = {head_url: destination.as_uri(), base_url: upstream.as_uri()}
    git = PUBLICATION._git

    def local_transport(executable: str, cwd: Path, env: dict[str, str], *args: str) -> str:
        """Route only the hosted transports to isolated bare repositories while running their actual Git commands."""
        if "ls-remote" not in args and "push" not in args:
            return git(executable, cwd, env, *args)
        endpoint = args[args.index("--") + 1]
        env = {**env, "GIT_ALLOW_PROTOCOL": "file", "RIG_PUBLICATION_URL": destination.as_uri()}
        translated = tuple(refs[value] if value == endpoint else value for value in args)
        output = git(executable, cwd, env, *translated)
        if "--dry-run" in args:
            if base_change == "deleted":
                _git(upstream, "update-ref", "-d", TARGET, first)
            elif base_change == "rewound":
                _git(upstream, "update-ref", TARGET, first, old)
            else:
                _git(upstream, "update-ref", TARGET, old if base_change == "included" else unseen, first)
        return output

    monkeypatch.setattr(PUBLICATION, "_git", local_transport)
    code = PUBLICATION.main(["--run", str(run), "--offer-digest", digest, *HELPER_ARGS])
    result = json.loads(capsys.readouterr().out)
    assert (code, result["status"], result.get("reason")) == (
        (0, "pushed", None) if expected == "pushed" else (1, "not-sent", expected)
    )
    assert (run / PUBLICATION.EXECUTED_FILE).exists() is (expected == "pushed")
    assert _git(destination, "rev-parse", BRANCH) == (new if expected == "pushed" else old)


@pytest.mark.parametrize(
    ("record", "field", "replacement"),
    [
        pytest.param("pr-routing", "base_repo", "different/project", id="unrelated-read-destination"),
        pytest.param("pr-routing", "base_ref", "other", id="wrong-target-branch"),
        pytest.param("pr-routing", "base_ref", None, id="missing-target"),
        pytest.param("target-branch", "base_ref", "other", id="wrong-fetched-target-branch"),
        pytest.param("target-branch", "remote_ref", None, id="missing-fetched-commit"),
        pytest.param("target-branch", "expected_base_oid", "d" * 40, id="capture-from-different-base"),
    ],
)
def test_refuses_conflicting_target_before_remote_transport(
    tmp_path: Path,
    record: str,
    field: str,
    replacement: str | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An uncorroborated routing record cannot redirect a check or open a different network endpoint."""
    digest = _open_attempt(tmp_path, _placeholder_offer(tmp_path))
    path = tmp_path / "pr" / f"{record}.json"
    receipt = json.loads(path.read_bytes())
    receipt[field] = replacement
    path.write_text(json.dumps(receipt), encoding="utf-8")
    monkeypatch.setattr(PUBLICATION, "_isolated_push", lambda *a, **kw: pytest.fail("transport reached"))
    assert PUBLICATION.main(["--run", str(tmp_path), "--offer-digest", digest, *HELPER_ARGS]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "not-sent", "reason": "publication-target-unverifiable"}
    assert not (tmp_path / PUBLICATION.EXECUTED_FILE).exists()


@pytest.mark.integration
def test_transport_moves_exact_ref_without_inheriting_url_rewrites(
    publication_repositories, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ignore source repository routing and leave a rewrite's alternate target untouched."""
    source, remote, _, old, new = publication_repositories
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    _git(alternate, "init", "--bare", "--template=")
    _git(source, "config", f"url.{alternate.as_uri()}.insteadOf", remote.as_uri())
    assert _git(source, "ls-remote", "--get-url", remote.as_uri()) == alternate.as_uri()
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{alternate.as_uri()}.pushInsteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", remote.as_uri())

    _push(source, remote, new, old)

    assert _git(remote, "rev-parse", BRANCH) == new
    assert _git(alternate, "for-each-ref") == ""


@pytest.mark.integration
def test_transport_refuses_to_create_a_deleted_destination(publication_repositories) -> None:
    """An existing approved head is required during the push itself."""
    source, remote, first, old, new = publication_repositories
    _git(remote, "update-ref", "-d", BRANCH, old)

    with pytest.raises(ValueError, match="publication-remote-tip-changed"):
        _push(source, remote, new, old)

    assert _git(remote, "for-each-ref", "--format=%(objectname) %(refname)") == f"{first} {TARGET}"


@pytest.mark.integration
def test_transport_refuses_a_changed_ancestor(publication_repositories) -> None:
    """An ordinary fast-forward can still be outside the approval when the advertised tip changed."""
    source, remote, first, old, new = publication_repositories
    _git(remote, "update-ref", BRANCH, first, old)
    spent = []

    with pytest.raises(ValueError, match="publication-remote-tip-changed"):
        _push(source, remote, new, old, before_push=lambda: spent.append(True))

    assert spent == []
    assert _git(remote, "rev-parse", BRANCH) == first


@pytest.mark.integration
def test_a_tip_that_moves_after_the_check_is_still_refused_by_the_hook(publication_repositories) -> None:
    """The read before the attempt narrows the race; the pre-push comparison remains the final check."""
    source, remote, first, old, new = publication_repositories

    with pytest.raises(ValueError, match="publication-advertised-ref-mismatch"):
        _push(source, remote, new, old, before_push=lambda: _git(remote, "update-ref", BRANCH, first, old))

    assert _git(remote, "rev-parse", BRANCH) == first


@pytest.mark.integration
def test_receiver_ref_change_after_advertisement_stops_update(publication_repositories) -> None:
    """The receiver compares the advertised old OID even if it changes after pre-push, and Git reports the rejection."""
    source, remote, first, old, new = publication_repositories
    hooks = remote / "hooks"
    hooks.mkdir()
    hook = hooks / "pre-receive"
    hook.write_text(
        "#!/bin/sh\nunset GIT_QUARANTINE_PATH GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES\n"
        f"git update-ref {BRANCH} {first} {old}\n",
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o700)

    with pytest.raises(ValueError, match=r"^publication-push-rejected$"):
        _push(source, remote, new, old)

    assert _git(remote, "rev-parse", BRANCH) == first


@pytest.mark.integration
def test_transport_does_not_bypass_source_pre_push_hook(publication_repositories) -> None:
    """A checkout's configured push policy prevents the isolated route from sending anything."""
    source, remote, _, old, new = publication_repositories
    hooks = source / "custom-hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    _git(source, "config", "core.hooksPath", str(hooks))

    with pytest.raises(ValueError, match="publication-source-pre-push-hook-present"):
        _push(source, remote, new, old)

    assert _git(remote, "rev-parse", BRANCH) == old


@pytest.mark.integration
def test_transport_accepts_clean_linked_worktree(publication_repositories, tmp_path: Path) -> None:
    """Use the linked checkout's index and the approved shared Git directory."""
    source, remote, _, old, new = publication_repositories
    linked = tmp_path / "linked"
    _git(source, "worktree", "add", "-b", "delivery", str(linked), new)

    _push(linked, remote, new, old, common_dir=source / ".git", branch="delivery")

    assert _git(remote, "rev-parse", BRANCH) == new


@pytest.mark.integration
def test_publication_uses_existing_repository_without_changing_its_metadata(
    publication_repositories, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The temporary transport files cannot serve as a repository, and the source Git files stay byte-identical."""
    source, remote, _, old, new = publication_repositories
    common = source / ".git"
    before = {path: path.read_bytes() for path in common.rglob("*") if path.is_file()}
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def inspect_transport() -> None:
        """Ask Git itself whether the helper prepared another repository."""
        (scratch,) = tmp_path.glob("codex-pr-publication-*")
        with pytest.raises(subprocess.CalledProcessError):
            _git(scratch, "--git-dir", str(scratch), "rev-parse", "--absolute-git-dir")

    _push(source, remote, new, old, before_push=inspect_transport)

    assert {path: path.read_bytes() for path in common.rglob("*") if path.is_file()} == before
    assert _git(remote, "rev-parse", BRANCH) == new


@pytest.mark.integration
def test_publication_refuses_to_refresh_the_sources_shared_index(publication_repositories) -> None:
    """Git normally touches the source split index even on reads; a refused publication must leave it intact."""
    source, remote, _, old, new = publication_repositories
    _git(source, "update-index", "--split-index")
    (shared_index,) = (source / ".git").glob("sharedindex.*")
    before = shared_index.stat().st_mtime_ns

    with pytest.raises(ValueError, match=r"^publication-source-index-unverifiable$"):
        _push(source, remote, new, old)

    assert shared_index.stat().st_mtime_ns == before
    assert _git(remote, "rev-parse", BRANCH) == old


@pytest.mark.integration
def test_publication_accepts_full_index_with_leftover_shared_index(publication_repositories) -> None:
    """Disabling split index leaves a companion behind; publication can still use the self-contained index."""
    source, remote, _, old, new = publication_repositories
    _git(source, "update-index", "--split-index")
    _git(source, "update-index", "--no-split-index")
    assert list((source / ".git").glob("sharedindex.*"))

    _push(source, remote, new, old)

    assert _git(remote, "rev-parse", BRANCH) == new


@pytest.mark.integration
def test_transport_refuses_new_branch_at_same_commit(publication_repositories) -> None:
    """The offered SHA alone does not prove the delivered branch is still attached."""
    source, remote, _, old, new = publication_repositories
    _git(source, "switch", "-c", "another-source", new)

    with pytest.raises(ValueError, match="publication-source-changed"):
        _push(source, remote, new, old)

    assert _git(remote, "rev-parse", BRANCH) == old


@pytest.mark.integration
def test_transport_refuses_changed_common_dir(publication_repositories) -> None:
    """The same branch name and SHA in a different repository do not qualify."""
    source, remote, _, old, new = publication_repositories

    with pytest.raises(ValueError, match="publication-source-changed"):
        _push(source, remote, new, old, common_dir=remote)

    assert _git(remote, "rev-parse", BRANCH) == old


@pytest.mark.integration
def test_transport_refuses_dirty_source(publication_repositories) -> None:
    """Checking the approved commit also requires an unchanged delivered index and worktree."""
    source, remote, _, old, new = publication_repositories
    (source / "tracked.txt").write_text("edited during approval", encoding="utf-8")

    with pytest.raises(ValueError, match="publication-source-dirty"):
        _push(source, remote, new, old)

    assert _git(remote, "rev-parse", BRANCH) == old


@pytest.mark.integration
def test_transport_accepts_a_repository_without_an_info_directory(publication_repositories) -> None:
    """A repository created with an empty template has no info directory; the status check still runs."""
    source, remote, _, old, new = publication_repositories
    shutil.rmtree(source / ".git" / "info")

    _push(source, remote, new, old)

    assert _git(remote, "rev-parse", BRANCH) == new


@pytest.mark.integration
def test_transport_honors_the_users_excludes_file(publication_repositories, tmp_path: Path) -> None:
    """A file the user's own Git hides through core.excludesFile does not make a clean source read as dirty."""
    source, remote, _, old, new = publication_repositories
    excludes = tmp_path / "global-ignore"
    excludes.write_text("scratch-notes.txt", encoding="utf-8")
    _git(source, "config", "core.excludesFile", str(excludes))
    (source / "scratch-notes.txt").write_text("not part of the delivery\n", encoding="utf-8")

    _push(source, remote, new, old)

    assert _git(remote, "rev-parse", BRANCH) == new


@pytest.mark.integration
@pytest.mark.parametrize(
    "location",
    [
        pytest.param(".config/git/ignore", id="home-default"),
        pytest.param("xdg/git/ignore", id="xdg-config-home"),
    ],
)
def test_transport_honors_the_default_global_ignore_file(
    publication_repositories, private_home: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    """With no core.excludesFile, the ignore file under the user's own home or XDG directory hides the file."""
    source, remote, _, old, new = publication_repositories
    if location.startswith("xdg"):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(private_home / "xdg"))
    ignore = private_home / location
    ignore.parent.mkdir(parents=True)
    ignore.write_text("scratch-notes.txt\n", encoding="utf-8")
    (source / "scratch-notes.txt").write_text("not part of the delivery\n", encoding="utf-8")

    _push(source, remote, new, old)

    assert _git(remote, "rev-parse", BRANCH) == new


@pytest.mark.integration
def test_a_file_the_users_ignore_rules_do_not_cover_still_reads_as_dirty(publication_repositories) -> None:
    """Without a matching rule an untracked file is real drift, so the home-directory tests prove the rule did the
    work."""
    source, remote, _, old, new = publication_repositories
    (source / "scratch-notes.txt").write_text("not part of the delivery\n", encoding="utf-8")

    with pytest.raises(ValueError, match="publication-source-dirty"):
        _push(source, remote, new, old)


@pytest.mark.integration
@pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="file symlinks unavailable")
def test_transport_follows_a_symlinked_excludes_file(publication_repositories, tmp_path: Path) -> None:
    """Dotfile managers symlink the excludes file; the rules behind the link still hide the user's files."""
    source, remote, _, old, new = publication_repositories
    target = tmp_path / "managed" / "ignore"
    target.parent.mkdir()
    target.write_text("scratch-notes.txt\n", encoding="utf-8")
    link = tmp_path / "global-ignore"
    link.symlink_to(target)
    _git(source, "config", "core.excludesFile", str(link))
    (source / "scratch-notes.txt").write_text("not part of the delivery\n", encoding="utf-8")

    _push(source, remote, new, old)

    assert _git(remote, "rev-parse", BRANCH) == new


@pytest.mark.integration
def test_the_attempt_is_spent_only_right_before_bytes_are_sent(publication_repositories) -> None:
    """The callback runs once after every check, while the destination still holds the approved tip."""
    source, remote, _, old, new = publication_repositories
    seen = []

    _push(source, remote, new, old, before_push=lambda: seen.append(_git(remote, "rev-parse", BRANCH)))

    assert seen == [old]
    assert _git(remote, "rev-parse", BRANCH) == new


@pytest.mark.integration
def test_a_shallow_source_history_is_refused_with_a_named_reason(publication_repositories, tmp_path: Path) -> None:
    """A shallow checkout holds the approved tip but not its parents; the isolated ancestry walk cannot finish.

    The transport reads the checkout's objects without its shallow boundary, so Git fails instead of answering. That is
    a named, unspent refusal, not an unexplained Git error.
    """
    source, remote, _, old, new = publication_repositories
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "--template=", "--depth=2", source.as_uri(), str(shallow))
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    spent = []

    with pytest.raises(ValueError, match=r"^publication-source-history-incomplete$"):
        _push(shallow, remote, new, old, common_dir=shallow / ".git", before_push=lambda: spent.append(True))

    assert spent == []
    assert _git(remote, "rev-parse", BRANCH) == old

    # The remedy the gate names: deepening the history lets the same checkout publish.
    _git(shallow, "fetch", "--unshallow")
    _push(shallow, remote, new, old, common_dir=shallow / ".git", before_push=lambda: spent.append(True))

    assert spent == [True]
    assert _git(remote, "rev-parse", BRANCH) == new


@pytest.mark.integration
def test_a_refusal_found_while_checking_spends_no_attempt(publication_repositories) -> None:
    """A dirty source, a hook or a moved branch fails before the callback, so the approval stays usable."""
    source, remote, _, old, new = publication_repositories
    (source / "tracked.txt").write_text("edited during approval", encoding="utf-8")
    spent = []

    with pytest.raises(ValueError, match="publication-source-dirty"):
        _push(source, remote, new, old, before_push=lambda: spent.append(True))

    assert spent == []
    assert _git(remote, "rev-parse", BRANCH) == old


def _recording_git(calls: list[tuple[str, ...]], refuse_dry_run: bool = False):
    """Wrap the helper's Git runner so a test sees every push argv, optionally refusing the dry run like a 403."""
    real = PUBLICATION._git

    def run(executable: str, cwd: Path, env: dict[str, str], *args: str) -> str:
        if "push" in args:
            calls.append(args)
            if refuse_dry_run and "--dry-run" in args:
                raise subprocess.CalledProcessError(128, ["git", *args], b"", b"fatal: Authentication failed\n")
        return real(executable, cwd, env, *args)

    return run


@pytest.mark.integration
def test_an_authenticated_dry_run_rehearses_the_exact_push_before_the_attempt(
    publication_repositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rehearsal uses the real push's argument list plus ``--dry-run``, runs first and moves no remote ref."""
    source, remote, _, old, new = publication_repositories
    events: list[object] = []
    monkeypatch.setattr(PUBLICATION, "_git", _recording_git(events))

    _push(source, remote, new, old, before_push=lambda: events.append(("spend", _git(remote, "rev-parse", BRANCH))))

    rehearsal, spend, real = events
    assert spend == ("spend", old)
    separator = real.index("--")
    assert rehearsal == (*real[:separator], "--dry-run", *real[separator:])
    assert "--dry-run" not in real
    assert _git(remote, "rev-parse", BRANCH) == new


@pytest.mark.integration
def test_a_refused_dry_run_spends_no_attempt(publication_repositories, monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing credentials or push rights refuse the rehearsal, so the single attempt is never used up."""
    source, remote, _, old, new = publication_repositories
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(PUBLICATION, "_git", _recording_git(calls, refuse_dry_run=True))
    spent = []

    with pytest.raises(ValueError, match=r"^publication-push-precheck-failed$"):
        _push(source, remote, new, old, before_push=lambda: spent.append(True))

    assert spent == []
    assert [("--dry-run" in call) for call in calls] == [True]
    assert _git(remote, "rev-parse", BRANCH) == old


def _refusing_git(command: str, error: subprocess.SubprocessError):
    """Wrap the helper's Git runner so one remote command fails the way an expired sign-in or a stalled network does."""
    real = PUBLICATION._git

    def run(executable: str, cwd: Path, env: dict[str, str], *args: str) -> str:
        if command in args and (command != "push" or "--dry-run" in args):
            raise error
        return real(executable, cwd, env, *args)

    return run


@pytest.mark.integration
@pytest.mark.parametrize(
    ("command", "error"),
    [
        pytest.param(
            "ls-remote",
            subprocess.CalledProcessError(128, ["git"], b"", b"fatal: Authentication failed\n"),
            id="private-repository-read-refused",
        ),
        pytest.param("ls-remote", subprocess.TimeoutExpired(["git"], 120), id="read-timed-out"),
        pytest.param("push", subprocess.TimeoutExpired(["git"], 120), id="rehearsal-timed-out"),
    ],
)
def test_a_refused_or_stalled_remote_check_is_the_retryable_sign_in_refusal(
    publication_repositories, monkeypatch: pytest.MonkeyPatch, command: str, error: subprocess.SubprocessError
) -> None:
    """A private repository refuses the read itself without sign-in; that and a timeout keep the attempt unspent."""
    source, remote, _, old, new = publication_repositories
    monkeypatch.setattr(PUBLICATION, "_git", _refusing_git(command, error))
    spent = []

    with pytest.raises(ValueError, match=r"^publication-push-precheck-failed$"):
        _push(source, remote, new, old, before_push=lambda: spent.append(True))

    assert spent == []
    assert _git(remote, "rev-parse", BRANCH) == old


@pytest.mark.integration
def test_a_destination_hidden_from_push_is_refused_by_the_rehearsal(publication_repositories) -> None:
    """A ref the read side shows but the push side does not advertise fails the rehearsal before the attempt."""
    source, remote, _, old, new = publication_repositories
    _git(remote, "config", "receive.hideRefs", BRANCH)
    assert _git(source, "ls-remote", "--refs", remote.as_uri(), BRANCH) == f"{old}\t{BRANCH}"
    spent = []

    with pytest.raises(ValueError, match=r"^publication-remote-tip-changed$"):
        _push(source, remote, new, old, before_push=lambda: spent.append(True))

    assert spent == []
    assert _git(remote, "rev-parse", BRANCH) == old


@pytest.mark.integration
def test_cli_keeps_the_attempt_after_a_refused_rehearsal(
    publication_repositories, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refused rehearsal reports ``not-sent`` and writes no marker, so the same approval can run once later.

    After the real push the marker exists, so a further call is refused as ``failed`` and never sends again.
    """
    source, remote, _, old, new = publication_repositories
    run = tmp_path / "run"
    run.mkdir()
    offer = {
        "checkout": str(source),
        "branch": "source",
        "common_dir": str(source / ".git"),
        "url": "https://github.com/example/project.git",
        "ref": BRANCH,
        "source_oid": new,
        "expected_oid": old,
    }
    digest = _open_attempt(run, offer, captured=_git(remote, "rev-parse", TARGET))
    real_push = PUBLICATION._isolated_push

    def local_endpoint(git: str, checkout: Path, url: str, *args: object, **kwargs: object) -> None:
        """Send the offer's transport to the fixture remote instead of GitHub."""
        assert url == offer["url"]
        assert kwargs["base"] == ("https://github.com/upstream/widgets.git", TARGET, _git(remote, "rev-parse", TARGET))
        kwargs["base"] = (remote.as_uri(), *kwargs["base"][1:])
        real_push(git, checkout, remote.as_uri(), *args, protocol="file", **kwargs)

    monkeypatch.setattr(PUBLICATION, "_isolated_push", local_endpoint)
    _git(remote, "config", "receive.hideRefs", BRANCH)
    argv = ["--run", str(run), "--offer-digest", digest, *HELPER_ARGS]

    assert PUBLICATION.main(argv) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "not-sent", "reason": "publication-remote-tip-changed"}
    assert not (run / PUBLICATION.EXECUTED_FILE).exists()
    assert _git(remote, "rev-parse", BRANCH) == old

    _git(remote, "config", "--unset", "receive.hideRefs")
    assert PUBLICATION.main(argv) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "pushed", "offer_digest": digest}
    assert (run / PUBLICATION.EXECUTED_FILE).read_text(encoding="ascii") == f"{digest}\n"
    assert _git(remote, "rev-parse", BRANCH) == new

    assert PUBLICATION.main(argv) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "reason": "publication-attempt-already-started"}


def _open_attempt(run: Path, offer: dict[str, str], *, captured: str | None = None) -> str:
    """Write an offer and its approved attempt record into ``run`` and return the offer digest."""
    raw = json.dumps(offer).encode()
    digest = hashlib.sha256(raw).hexdigest()
    (run / PUBLICATION.OFFER_FILE).write_bytes(raw)
    prefix = "pr-worktree-publication-request" if run.name == "publication-request" else "pr-worktree-publication"
    (run / PUBLICATION.ATTEMPT_FILE).write_text(
        json.dumps({"offer_digest": digest, "decision_id": f"{prefix}-{digest}", "answer": "Approve"}),
        encoding="utf-8",
    )
    (run / "publication-answer.json").write_bytes((run / PUBLICATION.ATTEMPT_FILE).read_bytes())
    if run.name != "publication-request":
        _record_destination(run, offer, result_name="result.candidate.json")
        _record_pr_target(run, offer, captured=captured)
        (run / "remediation-worktree.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "worktree": offer["checkout"],
                    "git_common_dir": offer["common_dir"],
                    "invoking_checkout": str(run.parent),
                }
            )
        )
        _attest_candidate(run)
    return digest


def _attest_candidate(run: Path) -> None:
    """Freeze the candidate and gate bytes the standalone transport must see at both execution checkpoints."""
    (run / "publication-candidate-validation.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_sha256": hashlib.sha256((run / "result.candidate.json").read_bytes()).hexdigest(),
                "gates_sha256": hashlib.sha256((run / "gates.json").read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("entry", ["initial", "follow-up"])
@pytest.mark.parametrize("defect", ["missing", "denied", "wrong-digest", "wrong-decision"])
def test_transport_refuses_attempt_without_matching_native_answer(
    delivered_request,
    entry: str,
    defect: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An attempt record cannot override a missing, negative, or differently scoped user answer."""
    checkpoint, offer, _ = delivered_request
    run = tmp_path if entry == "initial" else checkpoint
    digest = _open_attempt(run, offer)
    answer = run / "publication-answer.json"
    changes = {
        "denied": {"answer": "Deny"},
        "wrong-digest": {"offer_digest": "0" * 64},
        "wrong-decision": {"decision_id": "previous-offer"},
    }
    if defect == "missing":
        answer.unlink()
    else:
        answer.write_text(json.dumps({**json.loads(answer.read_bytes()), **changes[defect]}), encoding="utf-8")
    monkeypatch.setattr(PUBLICATION, "_isolated_push", lambda *a, **kw: pytest.fail("transport reached"))
    assert PUBLICATION.main(["--run", str(run), "--offer-digest", digest, *HELPER_ARGS]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "not-sent"
    assert not (run / PUBLICATION.EXECUTED_FILE).exists()


@pytest.mark.parametrize(
    ("name", "content"),
    [
        pytest.param("result.candidate.json", None, id="candidate-missing"),
        pytest.param("result.candidate.json", b'{"status":"pass"}', id="different-candidate"),
        pytest.param("publication-candidate-validation.json", None, id="validation-incomplete"),
        pytest.param("gates.json", b'{"status":"fail"}', id="local-evidence-changed"),
        pytest.param("final-handoff.json", b"{}", id="committed-handoff-changed"),
    ],
)
def test_initial_send_rechecks_validated_candidate_before_execution_marker(
    tmp_path: Path,
    name: str,
    content: bytes | None,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Changing evidence during the transport preflight cannot consume the attempt or send the commit."""
    offer = _placeholder_offer(tmp_path)
    digest = _open_attempt(tmp_path, offer)

    def change_evidence(*args: object, **kwargs: object) -> None:
        """Change a single retained input after the first eligibility check and before its final recheck."""
        file = tmp_path / name
        if content is None:
            file.unlink()
        else:
            file.write_bytes(content)
        kwargs["before_push"]()
        pytest.fail("Send was authorized after evidence changed")

    monkeypatch.setattr(PUBLICATION, "_isolated_push", change_evidence)
    assert PUBLICATION.main(["--run", str(tmp_path), "--offer-digest", digest, *HELPER_ARGS]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "not-sent"
    assert not (tmp_path / PUBLICATION.EXECUTED_FILE).exists()


def test_pass_claim_cannot_override_a_failed_local_gate_on_initial_send(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A claimed passing candidate and matching receipt cannot replace the local check on the actual gates."""
    digest = _open_attempt(tmp_path, _placeholder_offer(tmp_path))
    path = tmp_path / "gates.json"
    gates = json.loads(path.read_bytes())
    gates["checks"][0].update(status="fail", exit_code=1)
    path.write_text(json.dumps(gates), encoding="utf-8")
    _attest_candidate(tmp_path)
    monkeypatch.setattr(PUBLICATION, "_isolated_push", lambda *a, **kw: pytest.fail("transport reached"))
    assert PUBLICATION.main(["--run", str(tmp_path), "--offer-digest", digest, *HELPER_ARGS]) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "not-sent",
        "reason": "publication-validated-candidate-required",
    }
    assert not (tmp_path / PUBLICATION.EXECUTED_FILE).exists()


@pytest.mark.integration
def test_a_scratch_cleanup_failure_after_the_push_still_reports_pushed(
    publication_repositories, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Removing the private transport directory can fail after the real push; the report follows the push.

    The directory removal goes through the standard library's own cleanup, so the failure is injected where that
    cleanup reports it: the final removal of the scratch directory itself.
    """
    source, remote, _, old, new = publication_repositories
    run = tmp_path / "run"
    run.mkdir()
    url = "https://github.com/example/project.git"
    offer = {
        "checkout": str(source),
        "branch": "source",
        "common_dir": str(source / ".git"),
        "url": url,
        "ref": BRANCH,
        "source_oid": new,
        "expected_oid": old,
    }
    digest = _open_attempt(run, offer, captured=_git(remote, "rev-parse", TARGET))
    real_push = PUBLICATION._isolated_push

    def local_endpoint(git: str, checkout: Path, offer_url: str, *args: object, **kwargs: object) -> None:
        """Send the offer's transport to the fixture remote instead of GitHub."""
        assert offer_url == url
        assert kwargs["base"] == ("https://github.com/upstream/widgets.git", TARGET, _git(remote, "rev-parse", TARGET))
        kwargs["base"] = (remote.as_uri(), *kwargs["base"][1:])
        real_push(git, checkout, remote.as_uri(), *args, protocol="file", **kwargs)

    scratch_parent = tmp_path / "scratch-parent"
    scratch_parent.mkdir()
    real_rmdir = os.rmdir

    def stuck_rmdir(path: object, *args: object, **kwargs: object) -> None:
        """Refuse to remove only the transport's own scratch directory, as a busy or locked directory does."""
        if Path(os.fsdecode(path)).name.startswith("codex-pr-publication-"):
            raise OSError(errno.EBUSY, "Device or resource busy", os.fsdecode(path))
        real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(PUBLICATION, "_isolated_push", local_endpoint)
    monkeypatch.setattr(tempfile, "tempdir", str(scratch_parent))
    monkeypatch.setattr(os, "rmdir", stuck_rmdir)

    assert PUBLICATION.main(["--run", str(run), "--offer-digest", digest, *HELPER_ARGS]) == 0

    assert json.loads(capsys.readouterr().out) == {"status": "pushed", "offer_digest": digest}
    assert _git(remote, "rev-parse", BRANCH) == new
    assert json.loads((run / PUBLICATION.RESULT_FILE).read_text(encoding="utf-8"))["status"] == "pushed"


@pytest.mark.parametrize(
    "terminal_record",
    [
        pytest.param("publication.json", id="terminal-publication"),
        pytest.param("result.json", id="promoted-local-result"),
    ],
)
def test_a_run_closed_during_the_checks_keeps_the_attempt_unspent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], terminal_record: str
) -> None:
    """A terminal record written while the remote checks run stops the attempt before its marker or any byte."""
    offer = {
        "checkout": str(tmp_path),
        "branch": "source",
        "common_dir": str(tmp_path / ".git"),
        "url": "https://github.com/example/project.git",
        "ref": BRANCH,
        "source_oid": "a" * 40,
        "expected_oid": "b" * 40,
    }
    digest = _open_attempt(tmp_path, offer)
    sent = []

    def close_then_send(*args: object, **kwargs: object) -> None:
        """Close the run during the checks, then reach the point right before the first byte would be sent."""
        (tmp_path / terminal_record).write_text("{}", encoding="utf-8")
        kwargs["before_push"]()
        sent.append(True)

    monkeypatch.setattr(PUBLICATION, "_isolated_push", close_then_send)

    assert PUBLICATION.main(["--run", str(tmp_path), "--offer-digest", digest, *HELPER_ARGS]) == 1

    assert json.loads(capsys.readouterr().out) == {"status": "not-sent", "reason": "publication-run-already-closed"}
    assert sent == []
    assert not os.path.lexists(tmp_path / PUBLICATION.EXECUTED_FILE)
    assert not os.path.lexists(tmp_path / PUBLICATION.RESULT_FILE)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        pytest.param(None, "not-sent", id="no-marker"),
        pytest.param("file", "failed", id="marker"),
        pytest.param("empty", "failed", id="partial-marker"),
        pytest.param(
            "symlink",
            "failed",
            id="dangling-symlink-marker",
            marks=pytest.mark.skipif(not FILE_SYMLINKS_AVAILABLE, reason="file symlinks unavailable"),
        ),
    ],
)
def test_a_refusal_is_retryable_only_while_no_marker_entry_exists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    entry: str | None,
    expected: str,
) -> None:
    """Any entry at the marker path, even a damaged one, means the attempt started and the refusal is final."""
    offer = {
        "checkout": str(tmp_path),
        "branch": "source",
        "common_dir": str(tmp_path / ".git"),
        "url": "https://github.com/example/project.git",
        "ref": BRANCH,
        "source_oid": "a" * 40,
        "expected_oid": "b" * 40,
    }
    digest = _open_attempt(tmp_path, offer)
    marker = tmp_path / PUBLICATION.EXECUTED_FILE
    if entry == "file":
        marker.write_text(f"{digest}\n", encoding="ascii")
    elif entry == "empty":
        marker.touch()
    elif entry == "symlink":
        marker.symlink_to(tmp_path / "missing-target")

    def refuse(*args: object, **kwargs: object) -> None:
        """Refuse like a sign-in the receiving side rejects during the rehearsal."""
        raise ValueError("publication-push-precheck-failed")

    monkeypatch.setattr(PUBLICATION, "_isolated_push", refuse)
    argv = ["--run", str(tmp_path), "--offer-digest", digest, *HELPER_ARGS]

    assert PUBLICATION.main(argv) == 1
    reported = json.loads(capsys.readouterr().out)
    assert reported["status"] == expected
    assert os.path.lexists(marker) is (entry is not None)


def test_a_push_that_starts_and_then_fails_is_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once this invocation writes the marker, a refused real push reports ``failed`` and never runs again."""
    offer = {
        "checkout": str(tmp_path),
        "branch": "source",
        "common_dir": str(tmp_path / ".git"),
        "url": "https://github.com/example/project.git",
        "ref": BRANCH,
        "source_oid": "a" * 40,
        "expected_oid": "b" * 40,
    }
    digest = _open_attempt(tmp_path, offer)
    launches = []

    def start_then_fail(*args: object, **kwargs: object) -> None:
        """Spend the attempt, then fail like a receiver rule that refuses the update on receipt."""
        launches.append(True)
        kwargs["before_push"]()
        raise ValueError("publication-push-rejected")

    monkeypatch.setattr(PUBLICATION, "_isolated_push", start_then_fail)
    argv = ["--run", str(tmp_path), "--offer-digest", digest, *HELPER_ARGS]

    assert PUBLICATION.main(argv) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "reason": "publication-push-rejected"}
    assert (tmp_path / PUBLICATION.EXECUTED_FILE).read_text(encoding="ascii") == f"{digest}\n"

    assert PUBLICATION.main(argv) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "reason": "publication-attempt-already-started"}
    assert launches == [True]


def test_a_run_directory_that_does_not_exist_reports_nothing_sent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A refusal before the run directory resolves can have sent nothing."""
    assert PUBLICATION.main(["--run", str(tmp_path / "missing"), "--offer-digest", "0" * 64, *HELPER_ARGS]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "not-sent", "reason": "FileNotFoundError"}


@pytest.mark.integration
def test_a_ref_name_with_a_unicode_line_separator_still_verifies(publication_repositories) -> None:
    """Git may print U+2028 inside a ref name; only a newline ends a porcelain line."""
    source, remote, _, old, new = publication_repositories
    ref = "refs/heads/line\u2028separator"
    _git(remote, "update-ref", ref, old)

    _push(source, remote, new, old, ref=ref)

    assert _git(remote, "rev-parse", ref) == new


@pytest.mark.parametrize(
    ("terminal_record", "first_exit", "launches"),
    [
        pytest.param("", 0, 1, id="reserved-attempt"),
        pytest.param("publication.json", 1, 0, id="terminal-publication"),
        pytest.param("result.json", 1, 0, id="promoted-local-result"),
        pytest.param("recovery-worktree.json", 1, 0, id="independent-recovery-without-worktree-option"),
    ],
)
def test_cli_consumes_one_matching_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, terminal_record: str, first_exit: int, launches: int
) -> None:
    """Only an open, reserved attempt can launch once, even if a prior response was lost."""
    offer = {
        "checkout": str(tmp_path),
        "branch": "source",
        "common_dir": str(tmp_path / ".git"),
        "url": "https://github.com/example/project.git",
        "ref": BRANCH,
        "source_oid": "a" * 40,
        "expected_oid": "b" * 40,
    }
    digest = _open_attempt(tmp_path, offer)
    observed = []
    monkeypatch.setattr(
        PUBLICATION,
        "_isolated_push",
        lambda *args, **kwargs: (kwargs["before_push"](), observed.append((args, kwargs))),
    )
    argv = ["--run", str(tmp_path), "--offer-digest", digest, *HELPER_ARGS]
    if terminal_record:
        (tmp_path / terminal_record).write_text("{}", encoding="utf-8")

    assert PUBLICATION.main(argv) == first_exit
    assert PUBLICATION.main(argv) == 1
    assert len(observed) == launches
    result = tmp_path / PUBLICATION.RESULT_FILE
    assert (json.loads(result.read_bytes())["offer_digest"] if result.exists() else None) == (
        digest if launches else None
    )


def test_a_receipt_that_cannot_be_written_still_reports_the_completed_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The push already happened, so a leftover receipt file must not print a failure; the run stays unverified."""
    offer = {
        "checkout": str(tmp_path),
        "branch": "source",
        "common_dir": str(tmp_path / ".git"),
        "url": "https://github.com/example/project.git",
        "ref": BRANCH,
        "source_oid": "a" * 40,
        "expected_oid": "b" * 40,
    }
    digest = _open_attempt(tmp_path, offer)
    (tmp_path / PUBLICATION.RESULT_FILE).write_text("leftover", encoding="utf-8")
    monkeypatch.setattr(PUBLICATION, "_isolated_push", lambda *args, **kwargs: kwargs["before_push"]())

    assert PUBLICATION.main(["--run", str(tmp_path), "--offer-digest", digest, *HELPER_ARGS]) == 0

    reported = json.loads(capsys.readouterr().out)
    assert reported == {"status": "pushed", "offer_digest": digest, "receipt": "not-written"}
    assert (tmp_path / PUBLICATION.RESULT_FILE).read_text(encoding="utf-8") == "leftover"


def _placeholder_offer(directory: Path) -> dict[str, str]:
    """Return a well-formed offer whose transport a test replaces, so only the command-line handling runs."""
    return {
        "checkout": str(directory),
        "branch": "source",
        "common_dir": str(directory / ".git"),
        "url": "https://github.com/example/project.git",
        "ref": BRANCH,
        "source_oid": "a" * 40,
        "expected_oid": "b" * 40,
    }


def test_native_offer_distinguishes_push_from_push_and_cleanup(tmp_path: Path) -> None:
    """Adding removal to a historical push offer requires a different scoped approval."""
    offer = _placeholder_offer(tmp_path)
    path = tmp_path / PUBLICATION.OFFER_FILE
    path.write_bytes(json.dumps(offer).encode())
    push_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    assert PUBLICATION.read_offer(tmp_path, push_digest) == offer

    combined = {**offer, "after_push": "remove-owned-worktree"}
    path.write_bytes(json.dumps(combined).encode())
    combined_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match="publication-offer-digest-mismatch"):
        PUBLICATION.read_offer(tmp_path, push_digest)
    assert PUBLICATION.read_offer(tmp_path, combined_digest) == combined
    assert PUBLICATION.publication_decision_id(tmp_path, combined_digest) != PUBLICATION.publication_decision_id(
        tmp_path, push_digest
    )

    for invalid in ({**combined, "after_push": "remove-branch"}, {**combined, "path": str(tmp_path.parent)}):
        path.write_bytes(json.dumps(invalid).encode())
        with pytest.raises(ValueError, match="publication-offer-invalid"):
            PUBLICATION.read_offer(tmp_path, hashlib.sha256(path.read_bytes()).hexdigest())


@pytest.mark.integration
def test_every_remote_command_hands_git_the_whole_helper_chain_in_order(
    publication_repositories, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The remote read, the rehearsal and the push each carry every passed helper, in the order given."""
    source, remote, _, old, new = publication_repositories
    calls: list[tuple[str, ...]] = []
    real = PUBLICATION._git

    def record(executable: str, cwd: Path, env: dict[str, str], *args: str) -> str:
        if "ls-remote" in args or "push" in args:
            calls.append(args)
        return real(executable, cwd, env, *args)

    monkeypatch.setattr(PUBLICATION, "_git", record)

    _push(source, remote, new, old, credential_helpers=("libsecret", "manager"))

    assert ["ls-remote" if "ls-remote" in args else "push" for args in calls] == [
        "ls-remote",
        "push",
        "ls-remote",
        "push",
    ]
    for args in calls:
        command = args.index("ls-remote") if "ls-remote" in args else args.index("push")
        options = args[:command]
        helpers = [index for index, value in enumerate(options) if value.startswith("credential.helper=")]
        assert [options[index] for index in helpers] == ["credential.helper=libsecret", "credential.helper=manager"]
        assert all(options[index - 1] == "-c" for index in helpers)
    assert _git(remote, "rev-parse", BRANCH) == new


@pytest.mark.integration
@pytest.mark.parametrize(
    ("answering", "expected_calls"),
    [
        pytest.param("manager", ["libsecret", "manager"], id="second-helper-holds-the-sign-in"),
        pytest.param("libsecret", ["libsecret"], id="first-helper-holds-the-sign-in"),
    ],
)
def test_git_tries_the_passed_helpers_in_order_until_one_answers(
    tmp_path: Path, answering: str, expected_calls: list[str]
) -> None:
    """Git asks the helpers in the order the transport passes them and stops at the first that has a sign-in.

    Stub helpers in a private exec path shadow any real helper of the same name, so no real credential store is read.
    """
    stubs, log = tmp_path / "exec-path", tmp_path / "calls.log"
    stubs.mkdir()
    log.write_text("", encoding="utf-8")
    for name in PUBLICATION.CREDENTIAL_HELPERS:
        reply = f"printf 'username=user-{name}\\npassword=fixture\\n'" if name == answering else ":"
        stub = stubs / f"git-credential-{name}"
        stub.write_text(
            f"#!/bin/sh\n[ \"$1\" = get ] && echo {name} >> '{log.as_posix()}'\ncat >/dev/null\n{reply}\n",
            encoding="utf-8",
            newline="\n",
        )
        stub.chmod(0o700)
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("GIT_") and name.upper() not in {"SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"}
    }
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0")

    result = subprocess.run(
        [
            "git",
            f"--exec-path={stubs}",
            *PUBLICATION._credential_config(("libsecret", "manager")),
            "-c",
            "credential.interactive=false",
            "credential",
            "fill",
        ],
        input="protocol=https\nhost=github.com\npath=example/project.git\n\n",
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )

    assert log.read_text(encoding="utf-8").split() == expected_calls
    assert f"username=user-{answering}" in result.stdout.splitlines()


def test_cli_refuses_a_call_without_a_credential_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A call naming no helper would push without a sign-in, so it is refused before the marker and spends nothing."""
    digest = _open_attempt(tmp_path, _placeholder_offer(tmp_path))
    launches = []
    monkeypatch.setattr(PUBLICATION, "_isolated_push", lambda *args, **kwargs: launches.append(args))

    assert PUBLICATION.main(["--run", str(tmp_path), "--offer-digest", digest]) == 1

    assert json.loads(capsys.readouterr().out) == {
        "status": "not-sent",
        "reason": "publication-credential-helper-missing",
    }
    assert launches == []
    assert not os.path.lexists(tmp_path / PUBLICATION.EXECUTED_FILE)


@pytest.mark.parametrize(
    "helpers",
    [
        pytest.param(["store"], id="plain-text-store"),
        pytest.param(["!gh auth git-credential"], id="shell-command"),
        pytest.param(["/usr/local/share/gcm-core/git-credential-manager"], id="absolute-path"),
        pytest.param(["osxkeychain", "cache"], id="one-unsupported-in-the-chain"),
    ],
)
def test_cli_refuses_an_unsupported_credential_helper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, helpers: list[str]
) -> None:
    """Only the supported helper names reach Git; any other value stops the call before anything runs."""
    digest = _open_attempt(tmp_path, _placeholder_offer(tmp_path))
    launches = []
    monkeypatch.setattr(PUBLICATION, "_isolated_push", lambda *args, **kwargs: launches.append(args))
    options = [part for helper in helpers for part in ("--credential-helper", helper)]

    with pytest.raises(SystemExit) as refusal:
        PUBLICATION.main(["--run", str(tmp_path), "--offer-digest", digest, *options])

    assert refusal.value.code == 2
    assert launches == []
    assert not os.path.lexists(tmp_path / PUBLICATION.EXECUTED_FILE)


def test_cli_passes_every_credential_helper_in_the_given_order(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Repeated options become one ordered helper chain, the same order Git uses for a configured list."""
    digest = _open_attempt(tmp_path, _placeholder_offer(tmp_path))
    chains = []

    def capture(*args: object, **kwargs: object) -> None:
        """Record the helper chain the command line produced, then stop like a refused rehearsal."""
        chains.append(args[6])
        raise ValueError("publication-push-precheck-failed")

    monkeypatch.setattr(PUBLICATION, "_isolated_push", capture)
    options = [
        "--credential-helper",
        "libsecret",
        "--credential-helper",
        "manager",
        "--credential-helper",
        "osxkeychain",
    ]

    assert PUBLICATION.main(["--run", str(tmp_path), "--offer-digest", digest, *options]) == 1

    assert chains == [("libsecret", "manager", "osxkeychain")]


@pytest.mark.integration
def test_a_branch_whose_name_ends_with_the_destination_is_not_a_moved_tip(publication_repositories) -> None:
    """Git matches a remote ref pattern against the name's tail, so an unrelated ``x/refs/heads/<name>`` is ignored."""
    source, remote, first, old, new = publication_repositories
    collision = f"refs/heads/x/{BRANCH}"
    _git(remote, "update-ref", collision, first)
    assert _git(source, "ls-remote", "--refs", remote.as_uri(), BRANCH).split("\n") == [
        f"{old}\t{BRANCH}",
        f"{first}\t{collision}",
    ]

    _push(source, remote, new, old)

    assert _git(remote, "rev-parse", BRANCH) == new
    assert _git(remote, "rev-parse", collision) == first


def test_the_marker_reaches_the_disk_before_any_byte_is_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A power loss after a real push must not erase the proof that the attempt started.

    The marker file is flushed first, then, on POSIX hosts, the run directory that holds its name; native Windows cannot
    open a directory for flushing. Both happen before the transport is allowed to send.
    """
    digest = _open_attempt(tmp_path, _placeholder_offer(tmp_path))
    events: list[tuple[str, bool]] = []
    real_fsync = os.fsync

    def record_fsync(descriptor: int) -> None:
        """Note whether each flush is of a directory, then flush for real."""
        events.append(("fsync", stat.S_ISDIR(os.fstat(descriptor).st_mode)))
        real_fsync(descriptor)

    def push_after_marker(*args: object, **kwargs: object) -> None:
        """Spend the attempt, then record that the transport would now send."""
        kwargs["before_push"]()
        events.append(("send", os.path.lexists(tmp_path / PUBLICATION.EXECUTED_FILE)))

    monkeypatch.setattr(os, "fsync", record_fsync)
    monkeypatch.setattr(PUBLICATION, "_isolated_push", push_after_marker)

    assert PUBLICATION.main(["--run", str(tmp_path), "--offer-digest", digest, *HELPER_ARGS]) == 0

    flushed = [("fsync", False), *([("fsync", True)] if os.name == "posix" else [])]
    assert events == [*flushed, ("send", True)]


def _failing_real_push(error: subprocess.SubprocessError):
    """Wrap the helper's Git runner so only the real push, never its rehearsal, fails with ``error``."""
    real = PUBLICATION._git

    def run(executable: str, cwd: Path, env: dict[str, str], *args: str) -> str:
        if "push" in args and "--dry-run" not in args:
            raise error
        return real(executable, cwd, env, *args)

    return run


@pytest.mark.integration
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(subprocess.TimeoutExpired(["git"], 120), id="push-timed-out"),
        pytest.param(
            subprocess.CalledProcessError(128, ["git"], b"", b"fatal: the remote end hung up unexpectedly\n"),
            id="connection-lost-before-a-verdict",
        ),
    ],
)
def test_a_push_without_a_verdict_from_git_has_an_unknown_outcome(
    publication_repositories, monkeypatch: pytest.MonkeyPatch, error: subprocess.SubprocessError
) -> None:
    """After the marker, a push that timed out or stopped without a per-ref verdict may still have moved the remote."""
    source, remote, _, old, new = publication_repositories
    monkeypatch.setattr(PUBLICATION, "_git", _failing_real_push(error))
    spent = []

    with pytest.raises(ValueError, match=r"^publication-push-outcome-unknown$"):
        _push(source, remote, new, old, before_push=lambda: spent.append(True))

    assert spent == [True]


@pytest.mark.integration
@pytest.mark.parametrize(
    ("summary", "reason"),
    [
        pytest.param(
            "[remote failure] (remote failed to report status)", "publication-push-outcome-unknown", id="unknown"
        ),
        pytest.param("[remote rejected] (protected branch hook declined)", "publication-push-rejected", id="refused"),
        pytest.param("[rejected] (fetch first)", "publication-push-rejected", id="never-sent"),
    ],
)
def test_only_a_refusal_verdict_proves_the_remote_kept_its_tip(
    publication_repositories, monkeypatch: pytest.MonkeyPatch, summary: str, reason: str
) -> None:
    """Git flags every ref it did not update with ``!``; ``[remote failure]`` means the result was never reported."""
    source, remote, _, old, new = publication_repositories
    verdict = f"To {remote.as_uri()}\n!\t{new}:{BRANCH}\t{summary}\nDone\n"
    error = subprocess.CalledProcessError(1, ["git"], verdict.encode(), b"error: failed to push some refs\n")
    monkeypatch.setattr(PUBLICATION, "_git", _failing_real_push(error))

    with pytest.raises(ValueError, match=f"^{reason}$"):
        _push(source, remote, new, old)


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        pytest.param("publication-push-outcome-unknown", "unverified", id="no-verdict"),
        pytest.param("publication-update-unverified", "unverified", id="success-without-the-approved-update"),
        pytest.param("publication-push-rejected", "failed", id="known-refusal"),
        pytest.param("publication-advertised-ref-mismatch", "failed", id="hook-stopped-the-send"),
    ],
)
def test_a_started_push_is_unverified_unless_its_outcome_is_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], reason: str, status: str
) -> None:
    """Once the marker exists, only an outcome Git established reads ``failed``; the rest may have moved the remote."""
    digest = _open_attempt(tmp_path, _placeholder_offer(tmp_path))

    def start_then_end(*args: object, **kwargs: object) -> None:
        """Spend the attempt, then end the push with ``reason``."""
        kwargs["before_push"]()
        raise ValueError(reason)

    monkeypatch.setattr(PUBLICATION, "_isolated_push", start_then_end)

    assert PUBLICATION.main(["--run", str(tmp_path), "--offer-digest", digest, *HELPER_ARGS]) == 1

    assert json.loads(capsys.readouterr().out) == {"status": status, "reason": reason}


@pytest.mark.integration
def test_a_push_the_receiver_rejects_is_a_known_refusal(publication_repositories) -> None:
    """A receiver hook that declines the update gives Git's own rejection verdict, so the outcome is known."""
    source, remote, _, old, new = publication_repositories
    hooks = remote / "hooks"
    hooks.mkdir()
    (hooks / "pre-receive").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8", newline="\n")
    (hooks / "pre-receive").chmod(0o700)
    spent = []

    with pytest.raises(ValueError, match=r"^publication-push-rejected$"):
        _push(source, remote, new, old, before_push=lambda: spent.append(True))

    assert spent == [True]
    assert _git(remote, "rev-parse", BRANCH) == old


def test_a_push_with_an_unknown_outcome_is_reported_unverified_and_never_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The remote may hold the update, so the report is ``unverified``, not ``failed``, and the attempt stays spent."""
    digest = _open_attempt(tmp_path, _placeholder_offer(tmp_path))

    def start_then_lose_the_verdict(*args: object, **kwargs: object) -> None:
        """Spend the attempt, then end like a push that timed out after sending."""
        kwargs["before_push"]()
        raise ValueError("publication-push-outcome-unknown")

    monkeypatch.setattr(PUBLICATION, "_isolated_push", start_then_lose_the_verdict)
    argv = ["--run", str(tmp_path), "--offer-digest", digest, *HELPER_ARGS]

    assert PUBLICATION.main(argv) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "unverified", "reason": "publication-push-outcome-unknown"}

    assert PUBLICATION.main(argv) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "failed", "reason": "publication-attempt-already-started"}


@pytest.fixture
def delivered_request(publication_repositories, tmp_path: Path) -> tuple[Path, dict[str, str], Path]:
    """Retain a completed in-place PR remediation with its original fork identity and an empty follow-up checkpoint."""
    source, remote, _, old, new = publication_repositories
    parent = tmp_path / "completed-run"
    request = parent / "publication-request"
    request.mkdir(parents=True)
    offer = {
        "checkout": source.as_posix(),
        "branch": "source",
        "common_dir": (source / ".git").as_posix(),
        "url": "https://github.com/contributor/widgets.git",
        "ref": BRANCH,
        "source_oid": new,
        "expected_oid": old,
    }
    _record_pr_target(parent, offer, captured=_git(remote, "rev-parse", TARGET))
    _record_destination(parent, offer)
    return request, offer, remote


def _record_hosted_ci_result(original: Path) -> None:
    """Retain a failed PR result whose only open selected item needs a hosted run on the delivered docs commit."""
    path = original / "result.json"
    result = json.loads(path.read_bytes())
    result.update(status="fail", checks_failed=[], findings={"critical": 0})
    result["metadata"].update(
        resolution_scope={"selected_indexes": [1, 2]},
        final_resolution_table={
            "items": [
                {"item_type": "docs", "selectable": True, "resolution_status": "implemented", "severity": "medium"},
                {"item_type": "ci", "selectable": True, "resolution_status": "unresolved", "severity": "medium"},
            ]
        },
        unresolved_summary={
            "selected_items_total": 2,
            "selected_items_unresolved": 1,
            "local_actionable_items_unresolved": 1,
            "unresolved_reason_groups": [{"reason": "external-ci", "count": 1}],
        },
    )
    path.write_text(json.dumps(result), encoding="utf-8")


@pytest.mark.parametrize(
    ("row_type", "group_reason", "gate_state", "failed_checks"),
    [
        pytest.param("code", "external-ci", "pass", [], id="open-source-row"),
        pytest.param("ci", "environment-blocked", "pass", [], id="unrelated-blocker"),
        pytest.param("ci", "external-ci", "fail", [], id="failed-local-gate"),
        pytest.param("ci", "external-ci", "not-applicable", [], id="unapproved-test-skip"),
        pytest.param("ci", "external-ci", "pass", ["source-integrity"], id="failed-nongate-check"),
    ],
)
def test_requested_publication_refuses_unverified_local_work(
    delivered_request, row_type, group_reason, gate_state, failed_checks
) -> None:
    """Reject misleading summaries, incomplete local checks and failed supplemental verification before transport."""
    request, offer, _ = delivered_request
    run = request.parent
    _record_hosted_ci_result(run)
    result = json.loads((run / "result.json").read_bytes())
    result["checks_failed"] = failed_checks
    result["metadata"]["final_resolution_table"]["items"][1]["item_type"] = row_type
    result["metadata"]["unresolved_summary"]["unresolved_reason_groups"][0]["reason"] = group_reason
    (run / "result.json").write_text(json.dumps(result), encoding="utf-8")
    gates = json.loads((run / "gates.json").read_bytes())
    gates["checks"][3].update(status=gate_state, reason="Tests are unavailable.")
    (run / "gates.json").write_text(json.dumps(gates), encoding="utf-8")
    with pytest.raises(ValueError, match="publication-promoted-delivery-required"):
        PUBLICATION.requested_delivery(request, offer)


@pytest.fixture(params=["in-place", "independent", "returned-to-invoking", "previously-skipped", "awaiting-hosted-ci"])
def offered_delivery(delivered_request, request: pytest.FixtureRequest) -> tuple[Path, dict[str, str], Path]:
    """Bind the offer to the actual destination retained from each supported delivery mode."""
    checkpoint, offer, remote = delivered_request
    original = checkpoint.parent
    if request.param == "awaiting-hosted-ci":
        _record_hosted_ci_result(original)
    if request.param in {"previously-skipped", "awaiting-hosted-ci"}:
        skipped = {"schema_version": 1, "status": "skipped", "reason": "Question unavailable.", "observed_oid": None}
        (original / "publication.json").write_text(json.dumps(skipped))
    if request.param == "independent":
        control = Path(offer["checkout"])
        linked = control.parent / "independent linked checkout"
        _git(control, "worktree", "add", "-b", "fork-change", str(linked), offer["source_oid"])
        offer.update(checkout=linked.as_posix(), branch="fork-change")
        receipt = json.loads((original / "remediation-branch.json").read_bytes())
        (original / "remediation-branch.json").write_text(
            json.dumps({**receipt, "worktree": offer["checkout"], "branch": offer["branch"]})
        )
        locator = {
            "schema_version": 1,
            "invoking_checkout": control.as_posix(),
            "worktree": linked.as_posix(),
            "git_common_dir": offer["common_dir"],
        }
        (original / "recovery-worktree.json").write_text(json.dumps(locator))
        _record_destination(original, offer)
    if request.param == "returned-to-invoking":
        invoking = Path(offer["checkout"])
        historical_source = invoking.parent / "prior linked source"
        _git(invoking, "worktree", "add", "--detach", str(historical_source), offer["source_oid"])
        receipt = json.loads((original / "remediation-branch.json").read_bytes())
        (original / "remediation-branch.json").write_text(
            json.dumps({**receipt, "worktree": historical_source.as_posix()})
        )
    return checkpoint, offer, remote


@pytest.mark.integration
def test_later_request_publishes_delivered_branch_once(
    offered_delivery, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A fresh offer publishes committed delivery once, preserving the already promoted result."""
    request, offer, remote = offered_delivery
    parent = request.parent
    digest = _open_attempt(request, offer)
    (request / "publication-answer.json").write_bytes((request / PUBLICATION.ATTEMPT_FILE).read_bytes())
    original = (parent / "result.json").read_bytes()
    real_push = PUBLICATION._isolated_push

    def local_endpoint(git: str, checkout: Path, url: str, *args: object, **kwargs: object) -> None:
        """Exercise real Git against the fixture remote only after the exact approved destination has matched."""
        assert url == offer["url"]
        assert kwargs["base"] == ("https://github.com/upstream/widgets.git", TARGET, _git(remote, "rev-parse", TARGET))
        kwargs["base"] = (remote.as_uri(), *kwargs["base"][1:])
        real_push(git, checkout, remote.as_uri(), *args, protocol="file", **kwargs)

    monkeypatch.setattr(PUBLICATION, "_isolated_push", local_endpoint)
    args = ["--run", str(request), "--offer-digest", digest, *HELPER_ARGS]
    assert PUBLICATION.main(args) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "pushed"
    assert _git(remote, "rev-parse", BRANCH) == offer["source_oid"]
    assert PUBLICATION.main(args) == 1
    assert json.loads(capsys.readouterr().out)["reason"] == "publication-attempt-already-started"
    assert (parent / "result.json").read_bytes() == original
    assert not (parent / "remediation-worktree.json").exists()
    assert not (parent / "worktree-delivery.json").exists()

    finalize = load_module("requested_publication_finalize", PLUGIN_ROOT / "shared" / "remediation_finalize.py")
    readback = {
        "ref": BRANCH,
        "object": {"type": "commit", "sha": offer["source_oid"]},
        "url": "https://api.github.com/repos/contributor/widgets/git/refs/heads/change",
    }
    (request / "publication-readback.json").write_text(json.dumps(readback))
    assert finalize.main(["publication-resume", "--run", str(request)]) == 0
    assert json.loads(capsys.readouterr().out)["publication"]["status"] == "verified"


@pytest.mark.parametrize(
    ("record", "content", "reason"),
    [
        pytest.param("publication.json", '{"status":"declined"}', "publication-prior-decision", id="prior-denial"),
        pytest.param("publication-attempt.json", "{}", "publication-prior-decision", id="prior-consumed"),
        pytest.param("worktree-delivery.md", "Pending.", "publication-delivery-mismatch", id="unverified-commit"),
        pytest.param("recovery-worktree.json", "{}", "publication-delivery-mismatch", id="different-repository"),
    ],
)
def test_followup_refuses_conflicting_completed_evidence(
    delivered_request, record, content, reason, monkeypatch, capsys
) -> None:
    """A follow-up cannot bypass a prior decision or substitute unrelated delivery coordinates."""
    request, offer, _ = delivered_request
    (request.parent / record).write_text(content)
    digest = _open_attempt(request, offer)
    (request / "publication-answer.json").write_bytes((request / PUBLICATION.ATTEMPT_FILE).read_bytes())
    monkeypatch.setattr(PUBLICATION, "_isolated_push", lambda *a, **kw: pytest.fail("transport reached"))
    assert PUBLICATION.main(["--run", str(request), "--offer-digest", digest, *HELPER_ARGS]) == 1
    assert json.loads(capsys.readouterr().out) == {"status": "not-sent", "reason": reason}


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        pytest.param("url", "https://github.com/upstream/widgets.git", id="base-repo-substitution"),
        pytest.param("ref", "refs/heads/another", id="wrong-destination-ref"),
        pytest.param("expected_oid", "f" * 40, id="wrong-remote-tip"),
        pytest.param("source_oid", "f" * 40, id="undelivered-commit"),
    ],
)
def test_later_offer_must_match_the_original_pr(delivered_request, field, replacement, monkeypatch, capsys) -> None:
    """Runtime approval cannot redirect an offer away from the original PR or its delivered commit."""
    request, offer, _ = delivered_request
    digest = _open_attempt(request, {**offer, field: replacement})
    (request / "publication-answer.json").write_bytes((request / PUBLICATION.ATTEMPT_FILE).read_bytes())
    monkeypatch.setattr(PUBLICATION, "_isolated_push", lambda *a, **kw: pytest.fail("transport reached"))
    assert PUBLICATION.main(["--run", str(request), "--offer-digest", digest, *HELPER_ARGS]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "not-sent"


def test_followup_deny_is_retained_after_interruption(delivered_request, capsys) -> None:
    """An interrupted later request can report its recorded denial without contacting the remote or sending again."""
    request, offer, _ = delivered_request
    digest = _open_attempt(request, offer)
    (request / PUBLICATION.ATTEMPT_FILE).unlink()
    deny = {"offer_digest": digest, "decision_id": f"pr-worktree-publication-request-{digest}", "answer": "Deny"}
    (request / "publication-answer.json").write_text(json.dumps(deny))
    finalize = load_module("requested_publication_finalize", PLUGIN_ROOT / "shared" / "remediation_finalize.py")
    assert finalize.main(["publication-resume", "--run", str(request)]) == 0
    disposition = json.loads(capsys.readouterr().out)
    assert disposition["publication"]["status"] == "declined"
    assert disposition["needs_terminal_record"] is True
    assert not (request / PUBLICATION.EXECUTED_FILE).exists()


def test_followup_rejects_another_oid_mentioned_in_the_delivery_note(delivered_request) -> None:
    """Prior or invoking heads mentioned beside the actual delivery do not become publishable."""
    request, offer, _ = delivered_request
    note = request.parent / "worktree-delivery.md"
    another = "c" * 40
    note.write_text(note.read_text() + f"- Invoking checkout previously held {another}.\n")
    with pytest.raises(ValueError, match="publication-delivery-mismatch"):
        PUBLICATION.requested_delivery(request, {**offer, "source_oid": another})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        pytest.param("pr_number", 456, id="different-pr-number"),
        pytest.param("pr_url", "https://github.com/other/repo/pull/123", id="different-base-repository"),
    ],
)
def test_followup_rejects_receipt_for_different_pr(delivered_request, field, value) -> None:
    """A fork's branch can be proposed in multiple PRs, so repository and ref alone do not identify the PR."""
    request, offer, _ = delivered_request
    path = request.parent / "remediation-branch.json"
    receipt = json.loads(path.read_bytes())
    path.write_text(json.dumps({**receipt, field: value}))
    with pytest.raises(ValueError, match="publication-pr-receipt-mismatch"):
        PUBLICATION.requested_delivery(request, offer)


def test_unoffered_interrupted_request_remains_closed_without_source_or_remote_reads(delivered_request, capsys) -> None:
    """An interruption after reserving the exclusive directory has no spending authority and a reportable outcome."""
    request, _, _ = delivered_request
    finalize = load_module("requested_publication_finalize", PLUGIN_ROOT / "shared" / "remediation_finalize.py")
    assert finalize.main(["publication-resume", "--run", str(request)]) == 0
    disposition = json.loads(capsys.readouterr().out)
    assert disposition["publication"]["status"] == "skipped"
    assert disposition["needs_terminal_record"] is True
    assert list(request.iterdir()) == []
