"""Send one authorized PR branch update through an isolated Git transport.

## Purpose

Bind the actual receiver advertisement to the approved original head, and keep mutable source-worktree routing, config
and hooks out of publication.

## Scope

Use an approved run's offer, reservation and existing checkout. The transport uses the checkout's Git directory with
temporary common configuration, a copied index and a temporary object directory that reads the checkout's objects. The
only remote write is one non-force update to the admitted GitHub HTTPS branch.

## Usage

Issue a one-time runtime approval for this helper with the run path and the exact SHA-256 of its offer. User-question
verification and worktree delivery are independently owned by Code Remediate. For a push requested by the user and
approved after local promotion, pass the fixed publication-request directory inside the promoted run. Its attempt and
outcome stay separate from local delivery.

## Outputs

Publish one local, exclusive execution marker, flushed to disk, before sending bytes and emit one status: ``pushed`` on
success, ``not-sent`` for a refusal while no execution marker exists in the run, ``failed`` once the marker exists and
Git's answer is known, and ``unverified`` for a started push whose effect is not established, since the remote may hold
the update: one that timed out or ended without Git's verdict on the ref (``publication-push-outcome-unknown``), or that
Git reported as successful without the single approved fast-forward (``publication-update-unverified``). GitHub readback
is a separate workflow step.

## Failure

Invalid offers, reused attempts, unavailable credentials and changed remote tips fail closed. A call that names no
credential helper is refused (``publication-credential-helper-missing``), so nothing is sent without a sign-in; an
unsupported helper name is a usage error. A refusal found while checking (dirty or unverifiable source, a source history
that does not reach the approved tip such as a shallow clone, hook present, a moved tip, a run closed by a terminal
record, or an authenticated dry run of the exact push that Git refuses) happens before the marker, spends nothing and
reports ``not-sent``. This helper leaves the approved attempt unspent then, but whether it may run again is the
publication gate's decision: the gate allows one rerun only after ``publication-push-precheck-failed``. Once the marker
exists, nothing runs again: a failed, interrupted or delayed run never retries publication. Removing the private
transport directory never changes the reported result of a push that already happened.

## Used by

The optional Code Remediate PR linked-worktree publication checkpoint.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import mmap
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from pathlib import Path

OFFER_FILE = "publication-offer.json"
ATTEMPT_FILE = "publication-attempt.json"
EXECUTED_FILE = "publication-executed"
RESULT_FILE = "publication-execution-result.json"
REQUEST_DIRECTORY = "publication-request"
REPORT_LIMIT = 16 * 1024 * 1024  # Same output bound as the PR collector.
# Either record closes the run: the gate's terminal publication disposition, or the promoted local result.
TERMINAL_RECORDS = ("publication.json", "result.json")
OFFER_FIELDS = {"checkout", "branch", "common_dir", "url", "ref", "source_oid", "expected_oid"}
GITHUB_URL = re.compile(r"https://github\.com/[A-Za-z0-9-]+/[A-Za-z0-9_.-]+\.git")
GIT_OID = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")
CREDENTIAL_HELPERS = ("osxkeychain", "libsecret", "manager", "manager-core")
#: Reason for a started push whose result Git never reported, so the remote may or may not hold the update.
OUTCOME_UNKNOWN = "publication-push-outcome-unknown"
#: Started-push reasons whose remote effect is not established: Git reported no verdict, or it exited successfully
#: without reporting exactly the approved fast-forward. Both report ``unverified``; any other started refusal is known.
UNVERIFIED_REASONS = frozenset({OUTCOME_UNKNOWN, "publication-update-unverified"})

# Git passes exactly one update per stdin line. The advertised old OID is the
# one Git subsequently sends to receive-pack for its server-side ref check.
PRE_PUSH = """#!/bin/sh
reject() { echo 'codex-publication-advertisement-mismatch' >&2; exit 1; }
[ "$2" = "$RIG_PUBLICATION_URL" ] || reject
IFS=' ' read -r local_ref local_oid remote_ref remote_oid extra || reject
[ "$local_oid" = "$RIG_PUBLICATION_SOURCE" ] || reject
[ "$remote_ref" = "$RIG_PUBLICATION_REF" ] || reject
[ "$remote_oid" = "$RIG_PUBLICATION_EXPECTED" ] || reject
[ -z "$extra" ] || reject
if IFS= read -r extra; then reject; fi
"""


def _read_record(run: Path, name: str, *, limit: int = 16_384) -> bytes:
    """Read a regular, non-symlink record, bounding transport records separately from complete remediation reports."""
    path = run / name
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"publication-record-missing:{name}")
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"publication-record-too-large:{name}")
    return data


def read_offer(run: Path, digest: str) -> dict[str, str]:
    """Bind all transport inputs to the runtime-approved offer bytes."""
    raw = _read_record(run, OFFER_FILE)
    if not DIGEST.fullmatch(digest) or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("publication-offer-digest-mismatch")
    offer = json.loads(raw)
    if (
        not isinstance(offer, dict)
        or offer.keys() != OFFER_FIELDS
        or any(not isinstance(value, str) or not value for value in offer.values())
    ):
        raise ValueError("publication-offer-invalid")
    if (
        not Path(offer["checkout"]).is_absolute()
        or not Path(offer["common_dir"]).is_absolute()
        or not GITHUB_URL.fullmatch(offer["url"])
    ):
        raise ValueError("publication-checkout-or-url-invalid")
    source, expected = offer["source_oid"], offer["expected_oid"]
    if (
        not GIT_OID.fullmatch(source)
        or not GIT_OID.fullmatch(expected)
        or "0" * 40 in (source, expected)
        or source == expected
        or not offer["ref"].startswith("refs/heads/")
    ):
        raise ValueError("publication-ref-or-object-invalid")
    return offer


def _git(executable: str, cwd: Path, env: dict[str, str], *args: str) -> str:
    """Call one fixed Git operation without retaining output or credentials on failure."""
    result = subprocess.run(  # noqa: S603 - pinned executable, argv, sanitized environment
        [executable, "-C", str(cwd), *args],
        env=env,
        capture_output=True,
        check=True,
        timeout=120,
    )
    return os.fsdecode(result.stdout).rstrip("\r\n")


def _credential_config(helpers: Sequence[str]) -> tuple[str, ...]:
    """Return one ``-c credential.helper=<name>`` pair per helper, keeping the given order.

    Git treats ``credential.helper`` as a list: it asks each configured helper in turn until one supplies a
    credential, so repeating the option reproduces a configured chain of several helpers instead of choosing one.

    Example:
        >>> _credential_config(("osxkeychain", "manager"))
        ('-c', 'credential.helper=osxkeychain', '-c', 'credential.helper=manager')
    """
    return tuple(part for helper in helpers for part in ("-c", f"credential.helper={helper}"))


def _source_identity(git: str, checkout: Path, env: dict[str, str], branch: str, common_dir: Path, source: str) -> None:
    """Recheck the approved attached branch, repository and commit without invoking source hooks."""
    actual_branch = _git(git, checkout, env, "symbolic-ref", "-q", "HEAD")
    actual_common = _git(git, checkout, env, "rev-parse", "--path-format=absolute", "--git-common-dir")
    actual_source = _git(git, checkout, env, "rev-parse", "--verify", "HEAD")
    if (
        actual_branch != f"refs/heads/{branch}"
        or Path(actual_common).resolve(strict=True) != common_dir.resolve(strict=True)
        or actual_source != source
    ):
        raise ValueError("publication-source-changed")


def _require_ancestor(git: str, scratch: Path, env: dict[str, str], expected: str, source: str) -> None:
    """Require the approved remote tip to be an ancestor of the delivered commit using the checkout's objects.

    The temporary common directory excludes the checkout's shallow boundary, so in a shallow clone the
    ancestry walk reaches a parent that is not on disk and Git fails (exit 128) instead of answering. That is the named
    refusal ``publication-source-history-incomplete``. A definite "not an ancestor" answer (exit 1) is left as Git's
    own error: the offer was required to prove ancestry before it was written.
    """
    try:
        _git(git, scratch, env, "merge-base", "--is-ancestor", expected, source)
    except subprocess.CalledProcessError as error:
        if error.returncode == 1:
            raise
        raise ValueError("publication-source-history-incomplete") from error


def publication_decision_id(run: Path, offer_digest: str) -> str:
    """Distinguish a new post-delivery question from the original question for identical offer bytes."""
    prefix = "pr-worktree-publication-request" if run.name == REQUEST_DIRECTORY else "pr-worktree-publication"
    return f"{prefix}-{offer_digest}"


def _verify_requested_pr(original: Path, offer: dict[str, str]) -> None:
    """Corroborate the prepared branch against the collected PR identity and its normalized GitHub repository."""
    receipt_name = "remediation-branch-recovered.json"
    if not os.path.lexists(original / receipt_name):
        receipt_name = "remediation-branch.json"
    receipt = json.loads(_read_record(original, receipt_name))
    if (original / "pr").is_symlink():
        raise ValueError("publication-pr-receipt-mismatch")
    pr = json.loads(_read_record(original / "pr", "pr.json", limit=REPORT_LIMIT))
    routing = json.loads(_read_record(original / "pr", "pr-routing.json", limit=REPORT_LIMIT))
    try:
        number, url = pr["number"], pr["url"]
        # The collector normalizes both GitHub representations. Cross-check its original repository response here
        # without loading the network-capable collector or any of its dependencies into the approved transport.
        head_repo = pr["headRepository"]
        if isinstance(head_repo, dict):
            owner = head_repo.get("owner", pr.get("headRepositoryOwner"))
            if isinstance(owner, dict):
                owner = owner.get("login") or owner.get("name")
            head_repo = (
                head_repo.get("nameWithOwner")
                or head_repo.get("name_with_owner")
                or (f"{owner}/{head_repo['name']}" if isinstance(owner, str) else None)
            )
        matches = (
            type(number) is int
            and number > 0
            and isinstance(url, str)
            and re.fullmatch(rf"https://github[.]com/[a-zA-Z0-9-]+/[a-zA-Z0-9_.-]+/pull/{number}", url)
            and receipt["schema_version"] == 2
            and receipt["status"] == "prepared"
            and receipt["pr_number"] == routing["pr_number"] == number
            and receipt["pr_url"] == routing["pr_url"] == url
            and receipt["initial_head"] == routing["head_oid"] == pr["headRefOid"] == offer["expected_oid"]
            and receipt["branch"] == offer["branch"]
            and receipt["head_ref"] == routing["head_ref"] == pr["headRefName"]
            and offer["ref"] == f"refs/heads/{receipt['head_ref']}"
            and receipt["head_repository"] == routing["head_repo"] == head_repo
            and offer["url"] == f"https://github.com/{routing['head_repo']}.git"
        )
    except (TypeError, KeyError) as error:
        raise ValueError("publication-pr-receipt-mismatch") from error
    if not matches:
        raise ValueError("publication-pr-receipt-mismatch")


def _verify_recorded_destination(original: Path, offer: dict[str, str], artifacts: list[dict]) -> None:
    """Match the delivered tuple, allowing the original source worktree to differ after return delivery."""
    checkout, branch, head, common_dir = (offer[key] for key in ("checkout", "branch", "source_oid", "common_dir"))
    if not any(
        isinstance(item, dict)
        and item.get("path") == checkout
        and not str(item.get("label", "")).startswith("Source checkout:")
        for item in artifacts
    ):
        raise ValueError("publication-delivery-mismatch")
    if os.path.lexists(original / "worktree-delivery.json"):
        recorded = json.loads(_read_record(original, "worktree-delivery.json", limit=REPORT_LIMIT))
        if (
            isinstance(recorded, dict)
            and recorded.get("schema_version") == 1
            and (
                recorded.get("status") in ("delivered", "worktree-only")
                and (recorded.get("checkout"), recorded.get("branch"), recorded.get("head"), recorded.get("common_dir"))
                == (checkout, branch, head, common_dir)
            )
        ):
            return
        raise ValueError("publication-delivery-mismatch")

    # Legacy deliveries record a labelled destination tuple or separate destination fields. An OID in a note, the
    # historical source row or an invoking-checkout comparison cannot stand in for the delivered HEAD.
    note = _read_record(original, "worktree-delivery.md", limit=REPORT_LIMIT).decode("utf-8")
    fields: dict[str, list[str]] = {}
    for line in note.splitlines():
        label, sep, value = line.strip().removeprefix("- ").partition(": ")
        if sep:
            fields.setdefault(label.strip("*").casefold(), []).append(value.rstrip(".").strip("`"))
    statuses = fields.get("status", [])
    if len(statuses) != 1 or not re.match(r"(delivered|worktree-only)\b", statuses[0], re.I):
        raise ValueError("publication-delivery-mismatch")
    combined = fields.get("local destination")
    if combined is not None:
        matches = combined == [f"{checkout}, branch {branch}, head {head}"]
    else:
        matches = (
            fields.get("checkout", fields.get("destination checkout")) == [checkout]
            and fields.get("branch", fields.get("destination branch")) == [branch]
            and fields.get("final authorized head", fields.get("final head")) == [head]
        )
    if not matches or fields.get("git common directory", [common_dir]) != [common_dir]:
        raise ValueError("publication-delivery-mismatch")


def requested_delivery(run: Path, offer: dict[str, str]) -> dict[str, str]:
    """Bind a fresh follow-up offer to the retained PR, promoted handoff and delivered commit.

    Only the original run owns the local delivery. An unanswered or unoffered publication can be requested later. A
    denial, consumed attempt or started transport cannot be replayed in a child checkpoint.
    """
    original = run.parent
    if any(
        os.path.lexists(original / name)
        for name in (ATTEMPT_FILE, EXECUTED_FILE, RESULT_FILE, "publication-readback.json")
    ):
        raise ValueError("publication-prior-decision")
    if os.path.lexists(original / "publication-answer.json"):
        earlier_answer = json.loads(_read_record(original, "publication-answer.json"))
        if not isinstance(earlier_answer, dict) or earlier_answer.get("answer") != "Approve":
            raise ValueError("publication-prior-decision")
    if os.path.lexists(original / "publication.json"):
        previous = json.loads(_read_record(original, "publication.json"))
        if not isinstance(previous, dict) or previous.get("status") != "skipped":
            raise ValueError("publication-prior-decision")

    result = json.loads(_read_record(original, "result.json", limit=REPORT_LIMIT))
    handoff_bytes = _read_record(original, "final-handoff.json", limit=REPORT_LIMIT)
    handoff = json.loads(handoff_bytes)
    try:
        committed = (
            result["status"] == "pass"
            and result["metadata"]["mode"] == "pr"
            and result["metadata"]["final_handoff"]["handoff_sha256"] == hashlib.sha256(handoff_bytes).hexdigest()
            and handoff["skill"] == "code-remediate"
            and handoff["commit_disposition"]["status"] == "committed"
        )
    except (KeyError, TypeError) as error:
        raise ValueError("publication-promoted-delivery-required") from error
    if not committed:
        raise ValueError("publication-promoted-delivery-required")
    prior_binding = result["metadata"].get("worktree_publication")
    if prior_binding is not None and (
        not isinstance(prior_binding, dict)
        or prior_binding.get("status") != "skipped"
        or not os.path.lexists(original / "publication.json")
    ):
        raise ValueError("publication-prior-decision")

    _verify_requested_pr(original, offer)
    _verify_recorded_destination(original, offer, handoff.get("artifacts") or [])
    locators = [
        name for name in ("remediation-worktree.json", "recovery-worktree.json") if os.path.lexists(original / name)
    ]
    if len(locators) > 1:
        raise ValueError("publication-delivery-mismatch")
    if locators:
        locator = json.loads(_read_record(original, locators[0]))
        if not isinstance(locator, dict) or (
            locator.get("schema_version") != 1
            or locator.get("worktree") != offer["checkout"]
            or locator.get("git_common_dir") != offer["common_dir"]
        ):
            raise ValueError("publication-delivery-mismatch")
    return {
        "checkout": offer["checkout"],
        "branch": offer["branch"],
        "common_dir": offer["common_dir"],
        "head": offer["source_oid"],
    }


def _require_open_run(run: Path, offer: dict[str, str], attempt: dict[str, str]) -> None:
    """Require an open run eligible for publication, including at the final execution checkpoint."""
    if run.name == REQUEST_DIRECTORY:
        requested_delivery(run, offer)
        if json.loads(_read_record(run, "publication-answer.json")) != attempt:
            raise ValueError("publication-answer-mismatch")
    if os.path.lexists(run / "recovery-worktree.json"):
        raise ValueError("publication-ineligible-recovery-worktree")
    if any(os.path.lexists(run / name) for name in TERMINAL_RECORDS):
        raise ValueError("publication-run-already-closed")


def _ignore_rules(git: str, checkout: Path, common_dir: Path, user_env: dict[str, str]) -> bytes:
    """Return the user's own ignore rules (global excludes file, then the repository's info/exclude).

    The isolated status cannot read the user's Git configuration, so a file the user's Git hides through
    ``core.excludesFile`` would otherwise read as dirty. Only the named ignore files are read, never run.
    """
    rules: list[bytes] = []
    try:
        configured = _git(git, checkout, user_env, "config", "--path", "--get", "core.excludesFile")
    except subprocess.CalledProcessError:
        configured = ""
    home = user_env.get("HOME")  # Git for Windows prefers HOME over USERPROFILE, which Path.home() reads
    xdg = user_env.get("XDG_CONFIG_HOME") or str((Path(home) if home else Path.home()) / ".config")
    for candidate in (
        Path(configured) if configured else Path(xdg) / "git" / "ignore",
        common_dir / "info" / "exclude",
    ):
        # Dotfile managers symlink these files; reading the target is as safe as reading the file, which is never run.
        if candidate.is_absolute() and candidate.resolve().is_file():
            with candidate.resolve().open("rb") as stream:
                data = stream.read(1_048_577)
            if len(data) > 1_048_576:
                raise ValueError("publication-ignore-rules-too-large")
            rules.append(data if data.endswith(b"\n") or not data else data + b"\n")
    return b"".join(rules)


def _require_self_contained_index(index: Path) -> None:
    """Refuse a copied index that makes Git open and refresh a shared index inside the source Git directory."""
    if index.stat().st_size < 32:
        raise ValueError("publication-source-index-invalid")
    with index.open("rb") as stream, mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
        # gitformat-index puts sized extensions before the final SHA-1 checksum. Check the whole suffix so a
        # tracked path containing "link" or a leftover sharedindex file doesn't imply an active split index.
        end = len(data) - 20
        for link in re.finditer(b"link", data):
            start = link.start()
            size = int.from_bytes(data[start + 4 : start + 8], "big")
            if size < 20 or not any(data[start + 8 : start + 28]):
                continue
            cursor = start + 8 + size
            while cursor + 8 <= end:
                cursor += 8 + int.from_bytes(data[cursor + 4 : cursor + 8], "big")
            if cursor == end:
                raise ValueError("publication-source-index-unverifiable")


def _verify_clean_delivery(
    git: str,
    scratch: Path,
    env: dict[str, str],
    source: str,
) -> None:
    """Compare the copied index and working tree to the approved commit without following mutable refs."""
    git_entries = _git(git, scratch, env, "ls-files", "--stage", "-v", "-z").split("\0")
    if any(entry[:1] in {"S", "s"} or entry[:1].islower() or entry[2:9] == "160000 " for entry in git_entries):
        raise ValueError("publication-source-index-unverifiable")
    # Check the index and working tree separately; a staged edit can be undone only in the working tree.
    if (
        _git(git, scratch, env, "diff", "--cached", "--name-only", "-z", source, "--")
        or _git(git, scratch, env, "diff", "--name-only", "-z", "--")
        or _git(git, scratch, env, "ls-files", "--others", "--exclude-standard", "-z")
    ):
        raise ValueError("publication-source-dirty")


def _isolated_push(
    git: str,
    checkout: Path,
    url: str,
    ref: str,
    source: str,
    expected: str,
    credential_helpers: Sequence[str],
    *,
    branch: str,
    common_dir: Path,
    protocol: str = "https",
    before_push: Callable[[], None] = lambda: None,
) -> None:
    """Fast-forward only the approved advertised tip, using fresh local Git configuration.

    ``before_push`` runs after every check and immediately before the first byte is sent, so a refusal found while
    checking never uses up the single attempt. The checks end with an authenticated ``git push --dry-run`` of the
    exact update: a remote read or rehearsal that the remote refuses or that times out (for example for missing or
    expired credentials, which a private repository already refuses at the read) fails as
    ``publication-push-precheck-failed``, and a moved tip as ``publication-remote-tip-changed``, before the callback.
    A successful rehearsal does not prove the receiver will accept the update: rules a server applies on receipt can
    still refuse the real push.

    ``credential_helpers`` is the ordered helper chain for every remote command; Git asks the helpers in that order,
    as it does for a configured list. The command line requires at least one; an empty chain sends no credentials and
    serves only local test transports that ask for none.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith("GIT_") and name.upper() not in {"SSH_ASKPASS", "SSH_ASKPASS_REQUIRE"}
    }
    user_env = dict(env)
    # Honor configured project push hooks by declining to replace them. This
    # read-only Git query never uses their code in the isolated transport.
    configured_hook = _git(git, checkout, env, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push")
    if not Path(configured_hook).is_absolute() or os.path.lexists(configured_hook):
        raise ValueError("publication-source-pre-push-hook-present")
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_TERMINAL_PROMPT="0",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_ALLOW_PROTOCOL=protocol,
        RIG_PUBLICATION_URL=url,
        RIG_PUBLICATION_REF=ref,
        RIG_PUBLICATION_SOURCE=source,
        RIG_PUBLICATION_EXPECTED=expected,
    )
    _git(git, checkout, env, "check-ref-format", ref)
    _source_identity(git, checkout, env, branch, common_dir, source)
    objects = Path(_git(git, checkout, env, "rev-parse", "--path-format=absolute", "--git-path", "objects"))
    if not objects.is_absolute() or not objects.is_dir() or "\n" in str(objects) or "\r" in str(objects):
        raise ValueError("publication-object-store-invalid")
    git_dir = Path(_git(git, checkout, env, "rev-parse", "--absolute-git-dir")).resolve(strict=True)
    index = Path(_git(git, checkout, env, "rev-parse", "--path-format=absolute", "--git-path", "index"))
    if not index.is_absolute() or not index.is_file() or index.is_symlink():
        raise ValueError("publication-source-index-invalid")
    # Removing the scratch directory runs after the real push; a failure there (a busy or locked file) must not turn
    # a push that happened into a reported failure, so cleanup errors are ignored and the push result is checked
    # inside the block.
    with tempfile.TemporaryDirectory(prefix="codex-pr-publication-", ignore_cleanup_errors=True) as directory:
        scratch = Path(directory)
        # GIT_DIR stays attached to the existing checkout. Git requires a refs directory under GIT_COMMON_DIR;
        # leave it empty and use the approved object IDs directly. All transport writes use temporary files.
        (scratch / "refs").mkdir()
        (scratch / "objects" / "info").mkdir(parents=True)
        (scratch / "objects" / "info" / "alternates").write_bytes(os.fsencode(objects) + b"\n")
        shutil.copyfile(index, scratch / "index")
        _require_self_contained_index(scratch / "index")
        (scratch / "info").mkdir()
        (scratch / "info" / "exclude").write_bytes(_ignore_rules(git, checkout, common_dir, user_env))
        (scratch / "hooks").mkdir()
        hook = scratch / "hooks" / "pre-push"
        hook.write_text(PRE_PUSH, encoding="utf-8", newline="\n")
        hook.chmod(0o700)
        transport_env = dict(
            env,
            GIT_DIR=str(git_dir),
            GIT_COMMON_DIR=str(scratch),
            GIT_WORK_TREE=str(scratch),
            GIT_INDEX_FILE=str(scratch / "index"),
            GIT_OBJECT_DIRECTORY=str(scratch / "objects"),
            GIT_OPTIONAL_LOCKS="0",
        )
        if _git(git, scratch, transport_env, "config", "--list", "-z"):
            raise ValueError("publication-transport-config-unverifiable")
        _require_ancestor(git, scratch, transport_env, expected, source)
        _verify_clean_delivery(git, scratch, {**transport_env, "GIT_WORK_TREE": str(checkout)}, source)
        _source_identity(git, checkout, env, branch, common_dir, source)
        credential_config = _credential_config(credential_helpers)
        # A tip the PR author moved since the offer is the common refusal, so find it before the attempt is spent; the
        # pre-push hook stays the final check against a move between this read and the push.
        # A private repository refuses this read itself when sign-in is missing or expired, so a refused or timed-out
        # read is the same recoverable sign-in refusal as a refused rehearsal, not an unexplained Git error.
        try:
            advertised = _git(
                git,
                scratch,
                transport_env,
                "-c",
                "http.followRedirects=false",
                *credential_config,
                "-c",
                "credential.interactive=false",
                "ls-remote",
                "--refs",
                "--",
                url,
                ref,
            )
        except subprocess.SubprocessError as error:
            raise ValueError("publication-push-precheck-failed") from error
        # ``ls-remote`` matches the pattern against each ref name's tail, so a branch such as ``x/refs/heads/<name>``
        # is listed too; only the row naming exactly the approved ref is the destination.
        destination = [line for line in advertised.split("\n") if line.partition("\t")[2] == ref]
        if destination != [f"{expected}\t{ref}"]:
            raise ValueError("publication-remote-tip-changed")
        # One argument list for the rehearsal and the push, so their transport, credential and hook handling cannot
        # drift apart; only ``--dry-run`` differs.
        push = (
            "-c",
            "http.followRedirects=false",
            *credential_config,
            "-c",
            "credential.interactive=false",
            "push",
            "--porcelain",
            "--no-force",
            "--no-follow-tags",
            "--recurse-submodules=no",
            "--no-signed",
        )
        target = ("--", url, f"{source}:{ref}")
        # The read above can succeed anonymously, so it proves neither credentials nor push rights. A dry run asks for
        # the push side of the transport with the same credentials and runs the same trusted pre-push check, but sends
        # no objects and updates no ref. Whatever the receiving side refuses at that stage (an expired sign-in, for
        # example) fails here, before the attempt is spent; which push rights a host checks there is the host's choice.
        try:
            rehearsal = _git(git, scratch, transport_env, *push, "--dry-run", *target)
        except subprocess.SubprocessError as error:
            # A timeout carries no stderr; it is a refusal to retry, never a moved tip.
            stderr = getattr(error, "stderr", None) or b""
            if b"codex-publication-advertisement-mismatch" in stderr.splitlines():
                raise ValueError("publication-remote-tip-changed") from error
            raise ValueError("publication-push-precheck-failed") from error
        _require_single_update(rehearsal, source, ref, "publication-push-precheck-failed")
        before_push()
        try:
            output = _git(git, scratch, transport_env, *push, *target)
        except subprocess.TimeoutExpired as error:
            # The objects and the ref update may already have reached the receiver when the wait ran out.
            raise ValueError(OUTCOME_UNKNOWN) from error
        except subprocess.CalledProcessError as error:
            if b"codex-publication-advertisement-mismatch" in (error.stderr or b"").splitlines():
                raise ValueError("publication-advertised-ref-mismatch") from error
            if _reports_rejection(os.fsdecode(error.stdout or b""), source, ref):
                raise ValueError("publication-push-rejected") from error
            # No per-ref verdict (a dropped connection, for example): the receiver may have applied the update.
            raise ValueError(OUTCOME_UNKNOWN) from error
        _require_single_update(output, source, ref, "publication-update-unverified")


def _reports_rejection(output: str, source: str, ref: str) -> bool:
    """Return whether Git's porcelain output says the approved update was refused, so the remote kept its old tip.

    Git flags every ref it did not update with ``!``, but only two summaries are a refusal: ``[rejected]`` (Git never
    sent the ref) and ``[remote rejected]`` (the receiver declined it). ``[remote failure]`` shares the flag yet means
    the receiver never reported the result, so the update may have been applied. Split on newline only, as
    ``_require_single_update`` does.
    """
    for line in output.split("\n"):
        fields = line.split("\t", 2)
        if fields[:2] == ["!", f"{source}:{ref}"] and len(fields) == 3:
            return fields[2].startswith(("[rejected]", "[remote rejected]"))
    return False


def _require_single_update(output: str, source: str, ref: str, failure: str) -> None:
    """Require porcelain output reporting exactly one fast-forward of the approved commit onto the approved ref.

    A no-op is not a successful publication by this invocation, and a rehearsal that would do anything else is not the
    approved update. Split on newline only: a Git ref name may hold Unicode line separators that ``str.splitlines``
    would cut apart.
    """
    updates = [line.split("\t", 2) for line in output.split("\n") if "\t" in line]
    if len(updates) != 1 or updates[0][:2] != [" ", f"{source}:{ref}"]:
        raise ValueError(failure)


def _failure_status(run: Path | None, reason: str) -> str:
    """Name a refusal by what is on disk afterwards, not by where in this process it happened.

    The marker is created before the first byte is sent, so a run directory with nothing at the marker path proves no
    invocation sent anything and the approved attempt is still unspent: ``not-sent``. Any entry at that path, even a
    symlink or a partial write, counts as a started attempt, so a lost race, an earlier invocation's marker or a
    damaged marker never reads as retryable. A run directory that could not be resolved held no marker. A started
    attempt is ``failed`` when Git's answer is known, and ``unverified`` for :data:`UNVERIFIED_REASONS`, where the
    remote may hold the update.

    Example:
        >>> _failure_status(None, "publication-offer-invalid")
        'not-sent'
    """
    if run is None or not os.path.lexists(run / EXECUTED_FILE):
        return "not-sent"
    return "unverified" if reason in UNVERIFIED_REASONS else "failed"


def _fsync_directory(directory: Path) -> None:
    """Flush a directory's entries on POSIX hosts, so a file just created inside it survives a power loss.

    Best effort: native Windows cannot open a directory for flushing, so it is skipped there, and a POSIX file system
    that refuses to flush a directory is tolerated, because the file itself has already been flushed and is visible.
    """
    if os.name != "posix":
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        with contextlib.suppress(OSError):
            os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv: list[str] | None = None) -> int:
    """Consume one exact publication reservation under a separate runtime approval."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--offer-digest", required=True)
    # Repeated in order, like Git's own configured helper list; an unsupported name is an argparse usage error.
    parser.add_argument(
        "--credential-helper", action="append", choices=CREDENTIAL_HELPERS, default=[], dest="credential_helpers"
    )
    args = parser.parse_args(argv)
    # Kept separately from ``run`` so a failure before the directory resolves still reports a status.
    run_dir: Path | None = None
    try:
        if args.run.is_symlink():
            raise ValueError("publication-checkpoint-symlink")
        run = args.run.resolve(strict=True)
        run_dir = run
        # Without a helper the isolated transport has no credentials at all; refuse instead of trying anonymously.
        if not args.credential_helpers:
            raise ValueError("publication-credential-helper-missing")
        offer = read_offer(run, args.offer_digest)
        attempt = json.loads(_read_record(run, ATTEMPT_FILE))
        if attempt != {
            "offer_digest": args.offer_digest,
            "decision_id": publication_decision_id(run, args.offer_digest),
            "answer": "Approve",
        }:
            raise ValueError("publication-attempt-invalid")
        git = shutil.which("git")
        if git is None or not Path(git).is_absolute():
            raise ValueError("publication-git-unavailable")
        _require_open_run(run, offer, attempt)
        if os.path.lexists(run / EXECUTED_FILE):
            raise ValueError("publication-attempt-already-started")

        def mark_executed() -> None:
            """Spend the single attempt exclusively, right before bytes are sent.

            The remote checks can take minutes, so the run is checked for a terminal record again here: one written
            meanwhile still refuses before the marker, and the refusal stays ``not-sent``. The marker and then the run
            directory holding its name are flushed to disk before returning: a marker lost to a power cut after a real
            push would let the run resolve as "nothing was sent" although the remote moved.
            """
            _require_open_run(run, offer, attempt)
            try:
                with (run / EXECUTED_FILE).open("xb") as stream:
                    stream.write((args.offer_digest + "\n").encode("ascii"))
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError as error:
                raise ValueError("publication-attempt-already-started") from error
            _fsync_directory(run)

        _isolated_push(
            git,
            Path(offer["checkout"]),
            offer["url"],
            offer["ref"],
            offer["source_oid"],
            offer["expected_oid"],
            tuple(args.credential_helpers),
            branch=offer["branch"],
            common_dir=Path(offer["common_dir"]),
            before_push=mark_executed,
        )
        result = {"status": "pushed", "offer_digest": args.offer_digest}
        try:
            with (run / RESULT_FILE).open("x", encoding="utf-8", newline="\n") as stream:
                json.dump(result, stream)
                stream.write("\n")
        except OSError:
            # The push already happened: report it as pushed, and let the missing receipt leave the run unverified.
            print(json.dumps({**result, "receipt": "not-written"}))
            return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        reason = type(error).__name__
        if isinstance(error, ValueError) and str(error).startswith("publication-"):
            reason = str(error)
        print(json.dumps({"status": _failure_status(run_dir, reason), "reason": reason}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
