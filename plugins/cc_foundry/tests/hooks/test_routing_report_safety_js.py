"""Behavioral regressions for routing and report-hook trust boundaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _hook_env import _symlinks_supported

HOOKS = Path(__file__).resolve().parents[2] / "hooks"
_node_available = pytest.mark.skipif(shutil.which("node") is None, reason="requires node")
_symlink_available = pytest.mark.skipif(not _symlinks_supported(), reason="symlink capability unavailable")


def _node(script: str, *args: str, timeout: float = 10) -> object:
    """Run the real helper with controlled external boundaries and decode its JSON output."""
    result = subprocess.run(["node", "-e", script, *args], capture_output=True, text=True, timeout=timeout, check=True)
    return json.loads(result.stdout)


def _report_call(name: str, *args: object, timeout: float = 10) -> object:
    """Invoke the report helper's exported contract in a real Node process."""
    return _node(
        "const h=require(process.argv[1]);process.stdout.write(JSON.stringify(h[process.argv[2]](...JSON.parse(process.argv[3]))));",
        str(HOOKS / "report-header-table.js"),
        name,
        json.dumps(args),
        timeout=timeout,
    )


@_node_available
def test_rerouting_preserves_input_and_host_permission(tmp_path: Path) -> None:
    """An unknown agent rewrite retains host approval and every unrelated tool parameter."""
    payload = {
        "hook_event_name": "PreToolUse",
        "tool_name": "Agent",
        "session_id": "safety-rewrite",
        "tool_input": {"subagent_type": "absent:agent", "prompt": "p", "model": "sonnet", "run_in_background": True},
    }
    result = subprocess.run(
        ["node", str(HOOKS / "agent-router.js")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
        env={
            **os.environ,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "OPENAI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
        },
        cwd=tmp_path,
    )
    output = json.loads(result.stdout)["hookSpecificOutput"]
    assert output.get("permissionDecision") != "allow"
    assert output["updatedInput"]["model"] == "sonnet"
    assert output["updatedInput"]["run_in_background"] is True
    assert output["updatedInput"]["subagent_type"] == "general-purpose"


@_node_available
@pytest.mark.parametrize(
    ("provider", "event", "opt_in"),
    [
        pytest.param("OPENAI_API_KEY", "SessionStart", False, id="embedding-default-local"),
        pytest.param("OPENAI_API_KEY", "SessionStart", True, id="embedding-opted-in"),
        pytest.param("ANTHROPIC_API_KEY", "PreToolUse", False, id="llm-default-local"),
        pytest.param("ANTHROPIC_API_KEY", "PreToolUse", True, id="llm-opted-in"),
    ],
)
def test_external_routing_requires_opt_in(tmp_path: Path, provider: str, event: str, opt_in: bool) -> None:
    """Configured provider credentials do not opt local agent descriptions into external routing."""
    agents = tmp_path / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "local.md").write_text("---\ndescription: private project description\n---\n", encoding="utf-8")
    preload = tmp_path / "preload.js"
    requests = tmp_path / "requests"
    preload.write_text(
        "require('https').request=()=>{require('fs').appendFileSync(process.env.REQUESTS,'called');throw Error('blocked');};",
        encoding="utf-8",
    )
    env = {key: value for key, value in os.environ.items() if key != "AGENT_ROUTER_ALLOW_EXTERNAL"}
    result = subprocess.run(
        ["node", "--require", str(preload), str(HOOKS / "agent-router.js")],
        input=json.dumps(
            {
                "hook_event_name": event,
                "session_id": "api-opt-in",
                "tool_name": "Agent",
                "tool_input": {"subagent_type": "missing:agent", "prompt": "private request"},
            }
        ),
        capture_output=True,
        text=True,
        check=True,
        cwd=tmp_path,
        env={
            **env,
            "HOME": str(tmp_path),
            "USERPROFILE": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "REQUESTS": str(requests),
            "OPENAI_API_KEY": "",
            "ANTHROPIC_API_KEY": "",
            provider: "test-key",
            "AGENT_ROUTER_ALLOW_EXTERNAL": "1" if opt_in else "",
        },
    )
    assert result.returncode == 0
    assert requests.exists() is opt_in


@_node_available
@_symlink_available
def test_model_cache_rejects_preplanted_symlink(tmp_path: Path) -> None:
    """A symlinked model cache cannot supply a forged model or overwrite its destination."""
    got = _node(
        "const fs=require('fs'),h=require(process.argv[1]);const env={TMPDIR:process.argv[2]};"
        "const p=h.haikuModelCachePath(env);fs.mkdirSync(require('path').dirname(p),{recursive:true,mode:0o700});"
        "const target=process.argv[2]+'/victim';fs.writeFileSync(target,'claude-attacker');fs.symlinkSync(target,p);"
        "h.resolveHaikuModel({env,apiKey:'k',fetchModels:async()=>({statusCode:200,raw:JSON.stringify({data:[{id:'claude-safe',line:'haiku'}]})}),log:()=>{}})"
        ".then(r=>process.stdout.write(JSON.stringify({model:r.model,victim:fs.readFileSync(target,'utf8')})));",
        str(HOOKS / "agent-router.js"),
        str(tmp_path),
    )
    assert got == {"model": "claude-safe", "victim": "claude-attacker"}


@_node_available
@_symlink_available
@pytest.mark.parametrize("surface", ["sentinel", "marker"])
def test_stop_rejects_symlinked_state(tmp_path: Path, surface: str) -> None:
    """Stop never trusts a symlink sentinel or follows a marker symlink to write another file."""
    sentinel = tmp_path / "workflow-session"
    report = tmp_path / "report.md"
    report.write_text("---\nTitle: Review\nOutcome: PASS\nPath: report.md\n---\n", encoding="utf-8")
    sentinel.write_text(str(tmp_path), encoding="utf-8")
    marker = tmp_path / "report-delivered-workflow-session"
    victim = tmp_path / "victim"
    victim.write_text("untouched", encoding="utf-8")
    state = sentinel if surface == "sentinel" else marker
    state.unlink(missing_ok=True)
    state.symlink_to(victim)
    reason = _report_call("stopBlockReason", str(sentinel), str(report), {}, "review")
    assert reason is None
    assert victim.read_text(encoding="utf-8") == "untouched"
    assert state.is_symlink()


@_node_available
def test_report_reasons_do_not_repeat_untrusted_values(tmp_path: Path) -> None:
    """A mismatching report is rejected without copying report instructions into hook control text."""
    report = tmp_path / "report.md"
    report.write_text("---\nTitle: SECRET_INSTRUCTION\nOutcome: PASS\nPath: report.md\n---\n", encoding="utf-8")
    table = "| Field | Value |\n| --- | --- |\n| Title | wrong |\n| Outcome | PASS |\n| Path | report.md |"
    reason = _report_call("deliveryProblem", str(report), None, table)
    question_reason = _report_call("questionDeliveryProblem", str(report), {"questions": [{"question": "What next?"}]})
    assert reason is not None
    assert question_reason is not None
    assert "SECRET_INSTRUCTION" not in reason
    assert "SECRET_INSTRUCTION" not in question_reason


@_node_available
def test_blank_line_reply_is_processed_with_bounded_runtime(tmp_path: Path) -> None:
    """A long blank reply cannot trigger quadratic table-regex scanning."""
    report = tmp_path / "report.md"
    report.write_text("---\nTitle: Review\nOutcome: PASS\nPath: report.md\n---\n", encoding="utf-8")
    got = _node(
        "const h=require(process.argv[1]);const started=Date.now();"
        "const problem=h.deliveryProblem(process.argv[2],null,'\\n'.repeat(200000)+'|x');"
        "process.stdout.write(JSON.stringify({denied:typeof problem==='string',elapsed:Date.now()-started}));",
        str(HOOKS / "report-header-table.js"),
        str(report),
        timeout=3,
    )
    assert got["denied"] is True
    assert got["elapsed"] < 1000


@_node_available
def test_stop_rejects_foreign_owned_sentinel(tmp_path: Path) -> None:
    """A sentinel owned by a different effective uid cannot activate Stop or create its marker."""
    sentinel = tmp_path / "workflow-session"
    sentinel.write_text(str(tmp_path), encoding="utf-8")
    report = tmp_path / "report.md"
    report.write_text("---\nTitle: Review\nOutcome: PASS\nPath: report.md\n---\n", encoding="utf-8")
    got = _node(
        "const fs=require('fs'),h=require(process.argv[1]);"
        "process.getuid=()=>fs.statSync(process.argv[2]).uid+1;"
        "const reason=h.stopBlockReason(process.argv[2],process.argv[3],{},'review');"
        "process.stdout.write(JSON.stringify({reason,marker:fs.existsSync(h.deliveredMarkerPath(process.argv[2]))}));",
        str(HOOKS / "report-header-table.js"),
        str(sentinel),
        str(report),
    )
    assert got == {"reason": None, "marker": False}


@_node_available
@_symlink_available
def test_routing_cache_rejects_symlink_directory(tmp_path: Path) -> None:
    """Neither routing index nor model cache can write through a preplanted private-root symlink."""
    got = _node(
        "const fs=require('fs'),h=require(process.argv[1]),env={TMPDIR:process.argv[2]};"
        "const root=h.privateCacheDir(env),victim=process.argv[2]+'/victim';fs.mkdirSync(victim,{mode:0o700});"
        "fs.symlinkSync(victim,root,'dir');"
        "h.resolveHaikuModel({env,apiKey:'k',fetchModels:async()=>({statusCode:200,raw:JSON.stringify({data:[{id:'safe',line:'haiku'}]})}),log:()=>{}})"
        ".then(r=>process.stdout.write(JSON.stringify({model:r.model,files:fs.readdirSync(victim)})));",
        str(HOOKS / "agent-router.js"),
        str(tmp_path),
    )
    assert got == {"model": "safe", "files": []}
