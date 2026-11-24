"""Prepare disposable Git worktree and publication evidence for lifecycle tests."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

SHARED = Path(__file__).resolve().parents[1] / "shared"


def command(cwd: Path, *args: str) -> str:
    """Run fixture commands with no inherited user or system Git configuration."""
    return subprocess.run(
        args,
        cwd=cwd,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"},
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def write_json(path: Path, obj: dict) -> bytes:
    """Persist exact fixture bytes used by digest-bound receipts."""
    raw = json.dumps(obj).encode()
    path.write_bytes(raw)
    return raw


def record_pr_target(parent: Path, offer: dict[str, str], *, captured: str | None = None) -> None:
    """Retain the original contributor repository, ref and branch receipt used to bind the proposed update."""
    (parent / "pr").mkdir(exist_ok=True)
    receipt = {
        "schema_version": 2,
        "status": "prepared",
        "worktree": offer["checkout"],
        "branch": offer["branch"],
        "head_repository": offer["url"].removeprefix("https://github.com/").removesuffix(".git"),
        "head_ref": offer["ref"].removeprefix("refs/heads/"),
        "pr_number": 123,
        "pr_url": "https://github.com/upstream/widgets/pull/123",
        "initial_head": offer["expected_oid"],
    }
    (parent / "remediation-branch.json").write_text(json.dumps(receipt))
    captured = captured or offer["expected_oid"]
    pr = {
        "url": receipt["pr_url"],
        "number": 123,
        "headRefOid": offer["expected_oid"],
        "baseRefName": "main",
        "baseRefOid": captured,
        "headRefName": receipt["head_ref"],
        "headRepository": {"nameWithOwner": receipt["head_repository"]},
    }
    (parent / "pr" / "pr.json").write_text(json.dumps(pr))
    routing = {
        "pr_url": pr["url"],
        "pr_number": 123,
        "head_oid": offer["expected_oid"],
        "head_ref": offer["ref"].removeprefix("refs/heads/"),
        "head_repo": receipt["head_repository"],
        "base_repo": "upstream/widgets",
        "base_ref": "main",
        "base_oid": captured,
    }
    (parent / "pr" / "pr-routing.json").write_text(json.dumps(routing))
    target = {
        "status": "fetched",
        "base_ref": "main",
        "expected_base_oid": captured,
        "expected_base_is_ancestor": True,
        "local_head": captured,
        "remote_ref": captured,
    }
    (parent / "pr" / "target-branch.json").write_text(json.dumps(target))


def record_destination(original: Path, offer: dict[str, str], *, result_name: str = "result.json") -> None:
    """Retain the published destination separately from the historical source checkout."""
    original.joinpath("worktree-delivery.md").write_text(
        f"Status: delivered.\n- Local destination: {offer['checkout']}, branch {offer['branch']}, head {offer['source_oid']}.\n"
        f"- Git common directory: {offer['common_dir']}.\n"
    )
    handoff = json.dumps(
        {
            "skill": "code-remediate",
            "commit_disposition": {"status": "committed"},
            "artifacts": [{"label": "Delivery checkout", "path": offer["checkout"]}],
        }
    ).encode()
    (original / "final-handoff.json").write_bytes(handoff)
    metadata = {"mode": "pr", "final_handoff": {"handoff_sha256": hashlib.sha256(handoff).hexdigest()}}
    (original / result_name).write_text(json.dumps({"status": "pass", "metadata": metadata}))
    checks = [{"id": gate, "status": "pass", "exit_code": 0} for gate in ("lint", "format", "types", "tests", "review")]
    checks[3].update(status="not-applicable", reason="documentation-only change")
    (original / "gates.json").write_text(
        json.dumps({"status": "pass", "checks_failed": [], "checks": checks}), encoding="utf-8"
    )


def published_worktree(
    tmp_path: Path, *, followup: bool = False, combined: bool = True
) -> tuple[Path, Path, Path, Path, str]:
    """Allocate a real worktree and record the push-bound receipt chain at the chosen checkpoint."""
    caller = (tmp_path / "caller").resolve()
    caller.mkdir()
    command(caller, "git", "init", "-b", "main")
    command(caller, "git", "config", "user.name", "Fixture")
    command(caller, "git", "config", "user.email", "fixture@example.invalid")
    # The fixture writes LF bytes; a user or system `core.autocrlf=true` (Git for Windows default) would make
    # `checkout-index` emit CRLF and the byte-for-byte cleanup comparison would retain every worktree.
    command(caller, "git", "config", "core.autocrlf", "false")
    (caller / "tracked").write_bytes(b"initial\n")
    (caller / ".gitignore").write_bytes(b"ignored\n")
    command(caller, "git", "add", ".")
    command(caller, "git", "commit", "-m", "Initial commit")
    expected = command(caller, "git", "rev-parse", "HEAD")
    fresh = tmp_path.resolve() / "reports" / "fresh"
    fresh.mkdir(parents=True)
    allocation = json.loads(
        command(
            caller,
            sys.executable,
            str(SHARED / "remediation_branch.py"),
            "worktree",
            "--receipt",
            str(fresh / "remediation-worktree.json"),
        )
    )
    checkout = Path(allocation["worktree"])
    run = fresh.parent / "pr-123" / "run-001"
    run.parent.mkdir()
    fresh.rename(run)
    command(checkout, "git", "switch", "-c", "fix")
    (checkout / "tracked").write_bytes(b"delivered\n")
    command(checkout, "git", "commit", "-am", "Delivered fix")
    head = command(checkout, "git", "rev-parse", "HEAD")
    offer = {
        "checkout": checkout.as_posix(),
        "branch": "fix",
        "source_oid": head,
        "expected_oid": expected,
        "common_dir": allocation["git_common_dir"],
        "ref": "refs/heads/fix",
        "url": "https://github.com/contributor/widgets.git",
        "after_push": "remove-owned-worktree",
    }
    if not combined:
        del offer["after_push"]
    record_pr_target(run, offer)
    record_destination(run, offer)
    write_json(
        run / "pr/local-checkout.json", {"worktree": checkout.as_posix(), "local_branch": "fix", "local_head": expected}
    )
    gates = json.loads((run / "gates.json").read_bytes())
    write_json(run / "gates.json", {**gates, "execution_cwd": checkout.as_posix()})
    delivery = {
        "schema_version": 1,
        "status": "worktree-only",
        "checkout": offer["checkout"],
        "branch": "fix",
        "head": head,
        "common_dir": offer["common_dir"],
    }
    write_json(run / "worktree-delivery.json", delivery)
    checkpoint = run
    if followup:
        checkpoint = run / "publication-request"
        checkpoint.mkdir()
    raw = write_json(checkpoint / "publication-offer.json", offer)
    digest = hashlib.sha256(raw).hexdigest()
    prefix = "pr-worktree-publication-request" if followup else "pr-worktree-publication"
    answer = {"offer_digest": digest, "decision_id": f"{prefix}-{digest}", "answer": "Approve"}
    write_json(checkpoint / "publication-answer.json", answer)
    write_json(checkpoint / "publication-attempt.json", answer)
    (checkpoint / "publication-executed").write_bytes(f"{digest}\n".encode())
    write_json(checkpoint / "publication-execution-result.json", {"status": "pushed", "offer_digest": digest})
    readback = write_json(
        checkpoint / "publication-readback.json",
        {
            "ref": offer["ref"],
            "url": "https://api.github.com/repos/contributor/widgets/git/refs/heads/fix",
            "object": {"type": "commit", "sha": head},
        },
    )
    terminal = {"schema_version": 1, "status": "verified", "reason": "Confirmed push and ref.", "observed_oid": head}
    raw = write_json(checkpoint / "publication.json", terminal)
    if not followup:
        result = json.loads((run / "result.json").read_bytes())
        result["metadata"]["worktree_publication"] = {
            **terminal,
            "evidence": "publication.json",
            "evidence_sha256": hashlib.sha256(raw).hexdigest(),
            "readback_sha256": hashlib.sha256(readback).hexdigest(),
        }
        write_json(run / "result.json", result)
    owner = "owner1"
    claim = command(
        caller,
        sys.executable,
        str(SHARED / "review_checkout_slot.py"),
        "acquire",
        "--checkout",
        str(caller),
        "--target",
        "https://github.com/upstream/widgets/pull/123",
        "--owner",
        owner,
    )
    return caller, checkout, run, Path(json.loads(claim)["slot"]), owner
