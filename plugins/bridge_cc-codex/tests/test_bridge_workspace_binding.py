"""Exercise process-local Bridge workspace authority through real native stdio forms.

The binding tests drive an actual ``bridge_mcp.py --stdio`` server process, so they stay real; the artifact-boundary
tests exercise ``bridge_call`` directly or through its CLI.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import threading
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import pytest
from _bridge_link_probes import directory_symlinks_supported, hardlinks_supported

SERVER = Path(__file__).resolve().parents[1] / "bin/bridge_mcp.py"
if str(SERVER.parent) not in sys.path:
    sys.path.insert(0, str(SERVER.parent))
import bridge_call  # noqa: E402
import bridge_mcp  # noqa: E402

BIND_ANSWER = {"action": "accept", "content": {"answer": "Bind this folder"}}
_each_executable_tool = pytest.mark.parametrize("tool", ["bridge_advise", "bridge_review", "bridge_implement"])


class Client:
    """Exchange newline-delimited JSON with an actual server process with bounded waits."""

    def __init__(self, server: Path = SERVER, env: dict[str, str] | None = None) -> None:
        """Start the server, optionally with a replaced environment, and drain stdout into a portable message queue."""
        self.process = subprocess.Popen(
            [sys.executable, str(server), "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            env=env,
        )
        self.messages: queue.Queue[str] = queue.Queue()
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.reader.start()

    def _read(self) -> None:
        """Drain stdout without platform-specific file descriptor polling."""
        assert self.process.stdout is not None
        for line in self.process.stdout:
            self.messages.put(line)

    def send(self, message: dict[str, Any] | str) -> None:
        """Send one client message or a deliberately malformed raw JSON line."""
        assert self.process.stdin is not None
        self.process.stdin.write((message if isinstance(message, str) else json.dumps(message)) + "\n")
        self.process.stdin.flush()

    def receive(self) -> dict[str, Any]:
        """Read the next protocol message or fail with a bounded timeout."""
        return json.loads(self.messages.get(timeout=5))

    def request(self, request_id: str | int, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a client JSON-RPC request with an explicit correlation identifier."""
        self.send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}})

    def initialize(self, protocol: str = "2025-11-25", capability: Any = None) -> dict[str, Any]:
        """Negotiate the protocol and client form support, then notify initialization."""
        self.request(
            1,
            "initialize",
            {"protocolVersion": protocol, "capabilities": {"elicitation": {} if capability is None else capability}},
        )
        result = self.receive()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def call(self, name: str, request_id: int = 3, arguments: dict[str, Any] | None = None) -> None:
        """Call one public Bridge tool with visible arguments and correlation."""
        self.request(request_id, "tools/call", {"name": name, "arguments": arguments or {}})

    def answer(self, request_id: Any, result: Any) -> None:
        """Return a client elicitation result to a server request identifier."""
        self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    def close(self) -> None:
        """Close input, collect process exit, and release pipe handles even on failure."""
        assert self.process.stdin is not None
        if not self.process.stdin.closed:
            self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
        self.reader.join(timeout=5)
        for stream in (self.process.stdout, self.process.stderr):
            if stream is not None:
                stream.close()


@contextmanager
def client(server: Path = SERVER, env: dict[str, str] | None = None) -> Iterator[Client]:
    """Own the server process lifetime for one independent protocol scenario."""
    instance = Client(server, env)
    try:
        yield instance
    finally:
        instance.close()


def bind(connection: Client, folder: Path, request_id: int = 3) -> dict[str, Any]:
    """Select a human path and separately confirm its exact canonical target."""
    connection.call("bridge_bind_workspace", request_id)
    selection = connection.receive()
    assert selection["method"] == "elicitation/create"
    connection.answer(selection["id"], {"action": "accept", "content": {"answer": str(folder)}})
    confirmation = connection.receive()
    assert confirmation["method"] == "elicitation/create"
    assert confirmation["id"] != selection["id"]
    assert confirmation["params"]["message"] == (
        f"Bind Bridge calls in this process to exactly this canonical project folder: {folder.resolve().as_posix()}"
    )
    assert confirmation["params"]["requestedSchema"]["properties"]["answer"]["enum"] == ["Bind this folder", "Cancel"]
    connection.answer(confirmation["id"], {**BIND_ANSWER, "_meta": None})
    return connection.receive()


def status(connection: Client, request_id: int = 50) -> dict[str, Any]:
    """Read binding status without invoking any peer."""
    connection.call("bridge_status", request_id)
    return json.loads(connection.receive()["result"]["content"][0]["text"])


def _open_proposed_confirmation(connection: Client, folder: Path) -> dict[str, Any]:
    """Propose ``folder`` and return the native confirmation request the server opens for it."""
    connection.call("bridge_bind_workspace", arguments={"proposed_workspace": str(folder)})
    confirmation = connection.receive()
    assert confirmation["method"] == "elicitation/create"
    return confirmation


def _open_typed_confirmation(connection: Client, folder: Path) -> dict[str, Any]:
    """Type ``folder`` into the selection form and return the confirmation request that follows."""
    connection.call("bridge_bind_workspace")
    selection = connection.receive()
    connection.answer(selection["id"], {"action": "accept", "content": {"answer": str(folder)}})
    confirmation = connection.receive()
    assert confirmation["method"] == "elicitation/create"
    return confirmation


def _propose_outcome(connection: Client, folder: Path) -> tuple[int | None, str]:
    """Propose ``folder`` and return the error code the server answers with and the binding status afterwards."""
    connection.call("bridge_bind_workspace", arguments={"proposed_workspace": str(folder)})
    error_code = connection.receive()["error"]["code"]
    return error_code, status(connection)["binding_status"]


def _typed_outcome(connection: Client, folder: Path) -> tuple[int | None, str]:
    """Type ``folder`` into the selection form and return no error code plus the resulting binding status."""
    connection.call("bridge_bind_workspace")
    form = connection.receive()
    connection.answer(form["id"], {"action": "accept", "content": {"answer": str(folder)}})
    return None, connection.receive()["result"]["structuredContent"]["binding_status"]


_each_confirmation_flow = pytest.mark.parametrize(
    "open_confirmation",
    [pytest.param(_open_proposed_confirmation, id="proposed"), pytest.param(_open_typed_confirmation, id="typed")],
)
_each_refusal_flow = pytest.mark.parametrize(
    ("attempt", "expected"),
    [
        pytest.param(_propose_outcome, (-32602, "unbound"), id="proposed"),
        pytest.param(_typed_outcome, (None, "unbound"), id="typed"),
    ],
)


_skip_no_directory_symlinks = pytest.mark.skipif(
    not directory_symlinks_supported(), reason="directory symlinks unavailable"
)
_skip_no_hardlinks = pytest.mark.skipif(not hardlinks_supported(), reason="hard links unavailable")


def _queued_record(job_id: str) -> bytes:
    """Serialize the minimal queued job record the lifecycle commands read."""
    return json.dumps({"job_id": job_id, "status": "queued", "pid": None}).encode()


def _run_bridge_call(command: str, workspace: Path, job_id: str) -> subprocess.CompletedProcess[str]:
    """Run one ``bridge_call.py`` lifecycle command in a real subprocess and capture its output."""
    return subprocess.run(
        [
            sys.executable,
            str(SERVER.with_name("bridge_call.py")),
            command,
            "--workspace",
            str(workspace),
            "--job-id",
            job_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _write_transcript(paths: Any, job_path: Path) -> None:
    """Write a transcript through the cached artifact paths."""
    bridge_call._write_transcript(paths, "bounded output", "")


def _write_atomic_job(paths: Any, job_path: Path) -> None:
    """Write a job record atomically through the cached artifact paths."""
    bridge_call._write_json(job_path, {"status": "queued"}, paths=paths)


def _append_health(paths: Any, job_path: Path) -> None:
    """Append one health record through the cached artifact paths."""
    bridge_call._append_health(
        paths,
        {
            "run_id": "contained",
            "verb": "review",
            "direction": "codex_to_claude",
            "model": "host-default",
            "effort": "medium",
            "cost": None,
            "tokens": {},
            "duration_seconds": 0.0,
            "status": "refused",
            "depth": 1,
        },
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Empty project folder ``tmp_path/project`` to bind or hand to the CLI."""
    folder = tmp_path / "project"
    folder.mkdir()
    return folder


@pytest.fixture
def git_workspace(workspace: Path) -> Path:
    """Project folder carrying a ``.git`` marker, which ``bridge_implement`` requires."""
    (workspace / ".git").mkdir()
    return workspace


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    """Directory beside the project that no artifact write may ever reach."""
    folder = tmp_path / "outside"
    folder.mkdir()
    return folder


@pytest.fixture
def job_id() -> str:
    """Fresh job identifier for lifecycle commands."""
    return str(uuid.uuid4())


@pytest.fixture
def jobs_dir(workspace: Path) -> Path:
    """Real job store ``project/.temp/bridge/jobs``."""
    jobs = workspace / ".temp/bridge/jobs"
    jobs.mkdir(parents=True)
    return jobs


@pytest.fixture
def isolated_home(tmp_path: Path) -> SimpleNamespace:
    """Build a throwaway user home with both agent configuration homes, two variable-named homes and a project.

    The server runs with ``HOME``/``USERPROFILE`` and both configuration variables pointed here, so the probe never
    depends on, or touches, the real ``~/.claude`` or ``~/.codex``.
    """
    home = tmp_path / "home"
    folders = {
        "claude-home": home / ".claude",
        "codex-home": home / ".codex",
        "descendant": home / ".claude" / "projects" / "example",
        "codex-home-env": tmp_path / "codex-env-home",
        "claude-config-env": tmp_path / "claude-env-config",
        "env-descendant": tmp_path / "codex-env-home" / "sessions",
        "project": home / "work" / "project",
    }
    for folder in folders.values():
        folder.mkdir(parents=True, exist_ok=True)
    folders["home-ancestor"] = tmp_path
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "CODEX_HOME": str(folders["codex-home-env"]),
        "CLAUDE_CONFIG_DIR": str(folders["claude-config-env"]),
    }
    return SimpleNamespace(env=env, folders=folders)


@pytest.fixture
def isolated_home_env(isolated_home: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Point this process's home and configuration variables at the isolated home for in-process server calls."""
    for name in ("HOME", "USERPROFILE", "CODEX_HOME", "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(name, isolated_home.env[name])
    return isolated_home


@pytest.fixture
def user_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Make ``tmp_path`` the user home seen by the in-process workspace guards."""
    monkeypatch.setattr(bridge_mcp, "_user_home", lambda: tmp_path.resolve())
    return tmp_path


class TestProposedBinding:
    """A folder proposed by the host avoids typing but conveys no authority before native acceptance."""

    @pytest.mark.integration
    @_each_executable_tool
    def test_requires_confirmation_before_dispatch(self, tool: str, git_workspace: Path) -> None:
        """No tool runs until the proposed folder is confirmed, after which it binds to exactly that folder.

        The confirmation form names Codex as the folder's source, so a user cannot mistake an unprompted proposal for a
        path they typed themselves; the typed-path form used by ``bind`` carries no such origin line.
        """
        with client() as connection:
            connection.initialize()
            confirmation = _open_proposed_confirmation(connection, git_workspace)
            assert confirmation["params"]["message"] == (
                "Codex proposed this folder; you did not type it. Bind Bridge calls in this process to exactly this "
                f"canonical project folder: {git_workspace.resolve().as_posix()}"
            )
            assert confirmation["params"]["requestedSchema"]["properties"]["answer"]["enum"] == [
                "Bind this folder",
                "Cancel",
            ]
            connection.call(tool, 4, {"task": "No peer before acceptance."})
            assert connection.receive()["error"]["code"] == -32002
            assert not (git_workspace / ".temp").exists()
            connection.answer(confirmation["id"], BIND_ANSWER)
            binding = connection.receive()["result"]["structuredContent"]
            assert binding["workspace"] == git_workspace.resolve().as_posix()
            connection.call(tool, 5, {"task": "Refuse before peer.", "depth": 1, "binding_id": binding["binding_id"]})
            envelope = json.loads(connection.receive()["result"]["content"][0]["text"])
            assert envelope["status"] == "refused"
            assert envelope["blockers"] == ["recursion-depth"]

    @pytest.mark.integration
    @pytest.mark.parametrize("proposal", [None, "", "relative", 1, True])
    def test_invalid_proposal_clears_previous_authority(self, proposal: Any, tmp_path: Path) -> None:
        """A malformed proposal is an invalid-params error that cannot keep an older binding alive.

        A client bound to a folder re-proposes garbage; the earlier authority must be dropped, not silently retained.
        """
        with client() as connection:
            connection.initialize()
            bind(connection, tmp_path)
            connection.call("bridge_bind_workspace", 7, {"proposed_workspace": proposal})
            assert connection.receive()["error"]["code"] == -32602
            assert status(connection)["binding_status"] == "unbound"

    @pytest.mark.integration
    @pytest.mark.parametrize("action", ["decline", "cancel"])
    def test_second_proposal_is_rejected_and_unconfirmed_first_stays_unbound(self, action: str, tmp_path: Path) -> None:
        """A second proposal cannot replace the target already displayed, and declining the first binds nothing.

        The first form is still pending when another folder is proposed; the server answers busy, and the first form's
        decline or cancel leaves the process unbound.
        """
        other = tmp_path / "other"
        other.mkdir()
        with client() as connection:
            connection.initialize()
            confirmation = _open_proposed_confirmation(connection, tmp_path)
            connection.call("bridge_bind_workspace", 4, {"proposed_workspace": str(other)})
            assert connection.receive()["error"]["code"] == -32001
            connection.answer(confirmation["id"], {"action": action})
            assert connection.receive()["result"]["structuredContent"]["binding_status"] == "unbound"

    @pytest.mark.integration
    def test_second_proposal_cannot_replace_the_confirmed_target(self, tmp_path: Path) -> None:
        """Acceptance of the first form binds the first folder even after a rejected later proposal.

        The rejected second proposal must not leak into the pending form: accepting binds exactly what was displayed.
        """
        other = tmp_path / "other"
        other.mkdir()
        with client() as connection:
            connection.initialize()
            confirmation = _open_proposed_confirmation(connection, tmp_path)
            connection.call("bridge_bind_workspace", 4, {"proposed_workspace": str(other)})
            assert connection.receive()["error"]["code"] == -32001
            connection.answer(confirmation["id"], BIND_ANSWER)
            assert connection.receive()["result"]["structuredContent"]["workspace"] == tmp_path.resolve().as_posix()

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "answer",
        [
            pytest.param({"action": "accept", "content": {"answer": "Cancel"}}, id="explicit-cancel"),
            pytest.param({"action": "accept", "content": {"answer": "unexpected"}}, id="invalid-choice"),
            pytest.param({"action": "accept", "content": {}}, id="missing-answer"),
        ],
    )
    def test_unaccepted_confirmation_has_no_authority(self, answer: dict[str, Any], tmp_path: Path) -> None:
        """A cancelling or malformed native confirmation cannot authorize the proposal or revive an old binding.

        Opening a new proposal already revokes the previous binding id; the bad answer must leave it revoked.
        """
        with client() as connection:
            connection.initialize()
            old = bind(connection, tmp_path)["result"]["structuredContent"]["binding_id"]
            confirmation = _open_proposed_confirmation(connection, tmp_path)
            assert status(connection)["binding_status"] == "unbound"
            connection.call("bridge_advise", 5, {"task": "Old ID must fail.", "binding_id": old})
            assert connection.receive()["error"]["code"] == -32002
            connection.answer(confirmation["id"], answer)
            assert connection.receive()["result"]["structuredContent"]["binding_status"] == "unbound"


class TestBindingFlow:
    """The two-form native selection and confirmation flow gates every executable tool."""

    @pytest.mark.integration
    def test_discovers_binding_tool_and_reports_unbound_native_protocol(self) -> None:
        """A fresh server lists the binding tool and reports no binding, id or workspace.

        The prerequisite must be advertised honestly before any user authority exists.
        """
        with client() as connection:
            initialized = connection.initialize()
            connection.request(2, "tools/list")
            tools = {tool["name"] for tool in connection.receive()["result"]["tools"]}
            assert "bridge_bind_workspace" in tools
            payload = status(connection)
            assert payload["binding_status"] == "unbound"
            assert payload["binding_id"] is None
            assert payload["workspace"] is None
            assert payload["protocol_version"] == initialized["result"]["protocolVersion"]

    @pytest.mark.integration
    @_each_executable_tool
    def test_calls_require_confirmed_binding_before_any_provider_artifacts(
        self, tool: str, git_workspace: Path
    ) -> None:
        """Every executable tool is refused, and creates nothing, until both native forms complete.

        After binding, a call with recursion depth 1 is refused by the supervisor before any peer launches, which proves
        the bound path reached artifact setup (``.temp/bridge``) without a provider.
        """
        with client() as connection:
            connection.initialize()
            assert status(connection)["binding_status"] == "unbound"
            connection.call(tool, 2, {"task": "No provider execution before binding."})
            assert connection.receive()["error"]["code"] == -32002
            assert not (git_workspace / ".temp").exists()
            receipt = bind(connection, git_workspace)
            assert receipt["id"] == 3
            binding = receipt["result"]["structuredContent"]
            assert binding["status"] == "bound"
            assert binding["binding_status"] == "bound"
            assert binding["workspace"] == git_workspace.resolve().as_posix()
            assert isinstance(binding["binding_id"], str)
            assert status(connection)["workspace"] == git_workspace.resolve().as_posix()
            arguments = {"task": "Refuse before launching a peer.", "depth": 1, "run_id": "bound-proof"}
            connection.call(tool, 4, {**arguments, "binding_id": binding["binding_id"]})
            envelope = json.loads(connection.receive()["result"]["content"][0]["text"])
            assert envelope["status"] == "refused"
            assert envelope["blockers"] == ["recursion-depth"]
            assert (git_workspace / ".temp/bridge").is_dir()

    @pytest.mark.integration
    @pytest.mark.parametrize("action", ["decline", "cancel"])
    def test_cancelled_rebind_invalidates_previous_workspace(self, action: str, tmp_path: Path) -> None:
        """Starting and then cancelling a rebind leaves no execution authority behind.

        The previous binding is revoked when the new form opens, so calls are refused while it is pending and after it
        is declined or cancelled.
        """
        with client() as connection:
            connection.initialize()
            bind(connection, tmp_path)
            connection.call("bridge_bind_workspace", 7)
            form = connection.receive()
            assert status(connection)["binding_status"] == "unbound"
            connection.call("bridge_advise", 9, {"task": "Blocked while binding."})
            assert connection.receive()["error"]["code"] == -32002
            connection.answer(form["id"], {"action": action})
            assert connection.receive()["result"]["structuredContent"]["binding_status"] == "unbound"
            assert status(connection)["workspace"] is None

    @pytest.mark.integration
    def test_ignores_stale_ids_and_requires_second_exact_confirmation(self, tmp_path: Path) -> None:
        """A matched selection cannot authorize work through a stale answer id or a wrong confirmation.

        An answer to an unknown id and a confirmation sent against the selection id are both ignored; answering the real
        confirmation with something other than the bind choice cancels.
        """
        with client() as connection:
            connection.initialize()
            connection.call("bridge_bind_workspace")
            selection = connection.receive()
            connection.answer("stale-id", {"action": "accept", "content": {"answer": str(tmp_path)}})
            assert status(connection)["binding_status"] == "unbound"
            connection.answer(selection["id"], {"action": "accept", "content": {"answer": str(tmp_path)}})
            confirmation = connection.receive()
            connection.answer(selection["id"], BIND_ANSWER)
            assert status(connection)["binding_status"] == "unbound"
            connection.answer(confirmation["id"], {"action": "accept", "content": {"answer": "Approve"}})
            assert connection.receive()["result"]["structuredContent"]["status"] == "cancelled"
            assert status(connection)["binding_status"] == "unbound"

    @pytest.mark.integration
    def test_missing_form_support_and_model_workspace_override_fail_closed(self, tmp_path: Path) -> None:
        """Neither a model-supplied path nor a client without form support can create authority.

        A ``workspace`` argument is invalid params; a client that only advertises URL elicitation cannot be asked.
        """
        with client() as connection:
            connection.initialize(capability={"url": {}})
            connection.call("bridge_bind_workspace", arguments={"workspace": str(tmp_path)})
            assert connection.receive()["error"]["code"] == -32602
            connection.call("bridge_bind_workspace", 4)
            assert connection.receive()["error"]["code"] == -32002
            assert status(connection)["binding_status"] == "unbound"

    @pytest.mark.integration
    @_each_confirmation_flow
    def test_eof_cancels_unconfirmed_binding(
        self, open_confirmation: Callable[[Client, Path], dict[str, Any]], tmp_path: Path
    ) -> None:
        """Closing the transport while a confirmation is pending reports a cancel and never binds the folder.

        Both a proposed and a typed folder reach the confirmation form; losing the client then must not leave a half-
        confirmed binding that a later reader could treat as authority.
        """
        with client() as connection:
            connection.initialize()
            open_confirmation(connection, tmp_path)
            connection.process.stdin.close()
            receipt = connection.receive()["result"]["structuredContent"]
            assert receipt == {
                "status": "cancelled",
                "binding_status": "unbound",
                "binding_id": None,
                "workspace": None,
            }

    @pytest.mark.integration
    @_each_confirmation_flow
    def test_directory_replaced_before_confirmation_does_not_bind(
        self, open_confirmation: Callable[[Client, Path], dict[str, Any]], workspace: Path, tmp_path: Path
    ) -> None:
        """Confirmation grants authority only to the unchanged directory identity that was displayed.

        The canonical path text stays valid after the folder is renamed away and recreated, so identity, not text, must
        decide; this holds for a proposed and for a typed folder alike.
        """
        with client() as connection:
            connection.initialize()
            confirmation = open_confirmation(connection, workspace)
            workspace.rename(tmp_path / "old-project")
            workspace.mkdir()
            connection.answer(confirmation["id"], BIND_ANSWER)
            assert connection.receive()["result"]["structuredContent"]["binding_status"] == "unbound"

    @pytest.mark.integration
    def test_duplicate_pending_rpc_id_cancels_binding_once(self, tmp_path: Path) -> None:
        """Reusing a pending request id cancels the form once and later answers grant nothing.

        Transport reuse must not leave an orphaned form that could still produce authority when answered.
        """
        with client() as connection:
            connection.initialize()
            connection.call("bridge_bind_workspace", 3)
            selection = connection.receive()
            connection.request(3, "ping")
            receipt = connection.receive()
            assert receipt["id"] == 3
            assert receipt["result"]["structuredContent"]["status"] == "cancelled"
            assert connection.receive() == {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": selection["id"]},
            }
            connection.answer(selection["id"], {"action": "accept", "content": {"answer": str(tmp_path)}})
            assert status(connection)["binding_status"] == "unbound"

    @pytest.mark.integration
    @pytest.mark.parametrize("folder_kind", ["relative", "root", "home", "payload", "missing", "file"])
    @_each_refusal_flow
    def test_refuses_invalid_or_protected_folders(
        self,
        attempt: Callable[[Client, Path], tuple[int | None, str]],
        expected: tuple[int | None, str],
        folder_kind: str,
        tmp_path: Path,
    ) -> None:
        """Only existing project directories outside broad or installed payload roots may bind.

        A proposed folder is rejected as invalid params; a typed folder is cancelled by the confirmation step. Either
        way the process stays unbound.
        """
        file = tmp_path / "file"
        file.write_text("not a directory")
        folder = {
            "relative": Path("relative"),
            "root": Path(tmp_path.anchor),
            "home": Path.home(),
            "payload": SERVER.parents[1],
            "missing": tmp_path / "missing",
            "file": file,
        }[folder_kind]
        with client() as connection:
            connection.initialize()
            assert attempt(connection, folder) == expected

    @pytest.mark.integration
    @pytest.mark.parametrize(
        "folder_kind",
        [
            "claude-home",
            "codex-home",
            "descendant",
            "codex-home-env",
            "claude-config-env",
            "env-descendant",
            "home-ancestor",
        ],
    )
    @_each_refusal_flow
    def test_refuses_agent_config_homes_and_home_ancestors(
        self,
        attempt: Callable[[Client, Path], tuple[int | None, str]],
        expected: tuple[int | None, str],
        folder_kind: str,
        isolated_home: SimpleNamespace,
    ) -> None:
        """A folder that would let a bound run edit agent configuration, or that spans the whole home, never binds.

        A model steered by ingested content could propose ``~/.claude`` or ``~/.codex``; one confirmation click would
        then root an ``implement`` run where it can rewrite the host's own allow lists or approval policy.
        """
        with client(env=isolated_home.env) as connection:
            connection.initialize()
            assert attempt(connection, isolated_home.folders[folder_kind]) == expected

    @pytest.mark.integration
    def test_project_under_isolated_home_still_binds(self, isolated_home: SimpleNamespace) -> None:
        """The widened refusal set keeps an ordinary project folder below the home bindable.

        Without this, the config-home and home-ancestor refusals could grow until nothing was bindable.
        """
        with client(env=isolated_home.env) as connection:
            connection.initialize()
            receipt = bind(connection, isolated_home.folders["project"])
            assert receipt["result"]["structuredContent"]["binding_status"] == "bound"

    @_skip_no_directory_symlinks
    @pytest.mark.integration
    @_each_executable_tool
    def test_symlink_retarget_invalidates_each_executable_sibling(self, tool: str, tmp_path: Path) -> None:
        """Retargeting the symlink that was bound revokes authority for every kind of provider call.

        The originally selected path is revalidated before each call, so a link swapped to a second folder is refused.
        """
        first = tmp_path / "first"
        second = tmp_path / "second"
        first.mkdir()
        second.mkdir()
        link = tmp_path / "selected"
        link.symlink_to(first, target_is_directory=True)
        with client() as connection:
            connection.initialize()
            binding = bind(connection, link)["result"]["structuredContent"]
            link.unlink()
            link.symlink_to(second, target_is_directory=True)
            connection.call(tool, 4, {"task": "Must not dispatch after retarget.", "binding_id": binding["binding_id"]})
            assert connection.receive()["error"]["code"] == -32002
            assert status(connection)["binding_status"] == "unbound"
            assert not (first / ".temp").exists()
            assert not (second / ".temp").exists()

    @pytest.mark.integration
    @_each_executable_tool
    @pytest.mark.parametrize(
        "arguments",
        [
            pytest.param(lambda old: {"task": "Missing expectation."}, id="missing-expectation"),
            pytest.param(lambda old: {"task": "Stale expectation.", "binding_id": old}, id="stale-expectation"),
        ],
    )
    def test_rebind_same_folder_invalidates_previous_call_identity(
        self, tool: str, arguments: Callable[[str], dict[str, Any]], tmp_path: Path
    ) -> None:
        """After a rebind to the same folder, a call without the new id or with the old id is refused.

        A stale or omitted caller expectation must not silently use the newer process binding.
        """
        with client() as connection:
            connection.initialize()
            old = bind(connection, tmp_path)["result"]["structuredContent"]["binding_id"]
            current = bind(connection, tmp_path, 4)["result"]["structuredContent"]["binding_id"]
            assert current != old
            assert status(connection)["binding_id"] == current
            connection.call(tool, 5, arguments(old))
            assert connection.receive()["error"]["code"] == -32002
            assert not (tmp_path / ".temp").exists()


class TestWorkspaceRefusal:
    """In-process guards refuse workspaces that are unsafe to launch a provider in."""

    @pytest.mark.parametrize(
        "folder_kind",
        [pytest.param("claude-home", id="config-home"), pytest.param("home-ancestor", id="home-ancestor")],
    )
    def test_implement_refuses_config_home_launch_workspace(
        self, folder_kind: str, isolated_home_env: SimpleNamespace
    ) -> None:
        """A write-capable call refuses a host-launched workspace that is a configuration home or spans the home.

        The host launches the server in that folder without any binding form, so the guard must apply to the trusted
        workspace itself and answer with the project-workspace message.
        """
        response = bridge_mcp.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "bridge_implement", "arguments": {"task": "Edit something."}},
            },
            trusted_workspace=isolated_home_env.folders[folder_kind],
        )

        assert response is not None
        assert response["error"]["code"] == -32602
        assert "write-capable bridge calls need a project workspace" in response["error"]["message"]

    @pytest.mark.parametrize(
        ("candidate", "refused"),
        [
            pytest.param(r"d:\Users\operator\.codex\plugins\cache\other-market\sibling\1.0", True, id="cache-sibling"),
            pytest.param(r"D:\Users\operator\project", False, id="home-project"),
            pytest.param(r"D:\Users\operator\.codex\plugins\cache-extra\project", False, id="prefix-sibling"),
        ],
    )
    def test_cache_guards_use_windows_path_ancestry_on_every_host(self, candidate: str, refused: bool) -> None:
        """Windows drive and case semantics reject real cache descendants without prefix guessing.

        The protected roots come from a Windows plugin path built with ``PureWindowsPath``, so the check runs the same
        on every host; ``cache-extra`` shares a prefix with ``cache`` and must stay allowed.
        """
        plugin = PureWindowsPath(r"D:\Users\operator\.codex\plugins\cache\market\bridge\0.6.0")

        protected = bridge_mcp._protected_payload_roots(plugin)

        assert any(PureWindowsPath(candidate).is_relative_to(root) for root in protected) is refused

    @pytest.mark.integration
    def test_installed_cache_sibling_is_not_a_project_binding(self, tmp_path: Path) -> None:
        """The actual cache ancestor protects sibling plugins, not only this payload.

        The server runs from a copied install under a ``plugins/cache`` tree; typing a sibling plugin's folder must be
        cancelled at confirmation.
        """
        cache = tmp_path / "plugins" / "cache"
        installed = cache / "market" / "bridge" / "0.6.0"
        shutil.copytree(
            SERVER.parents[1], installed, ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache")
        )
        sibling = cache / "other-market" / "other-plugin" / "1.0"
        sibling.mkdir(parents=True)
        with client(installed / "bin/bridge_mcp.py") as connection:
            connection.initialize()
            assert _typed_outcome(connection, sibling) == (None, "unbound")

    @pytest.mark.parametrize(
        ("relative", "refused"),
        [
            pytest.param(".ssh", True, id="ssh-home"),
            pytest.param(".aws/profiles", True, id="aws-subtree"),
            pytest.param("projects/app", False, id="project-below-home"),
        ],
    )
    def test_credential_directories_cannot_root_a_workspace(
        self, user_home: Path, monkeypatch: pytest.MonkeyPatch, relative: str, refused: bool
    ) -> None:
        """Credential directories under the home are refused as workspaces while ordinary project folders stay allowed.

        A write-capable run rooted in ~/.ssh or ~/.aws could read or rewrite keys; a project folder below the home is
        the normal case and must keep binding.
        """
        monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
        monkeypatch.delenv("CODEX_HOME", raising=False)
        folder = user_home / relative
        folder.mkdir(parents=True)

        reason = bridge_mcp._workspace_refusal(folder.resolve())

        assert (reason == "a credential or system configuration directory such as ~/.ssh or /etc") is refused

    @pytest.mark.parametrize(
        ("marker", "relative", "refused"),
        [
            pytest.param("repo/README.md", "repo", True, id="no-marker"),
            pytest.param("repo/.git", "repo", False, id="git-file-of-a-linked-worktree"),
            pytest.param("repo/pyproject.toml", "repo", False, id="pyproject-file"),
            pytest.param("repo/.git", "repo/src/pkg", False, id="marker-in-ancestor"),
        ],
    )
    def test_implement_requires_a_project_marker(
        self, user_home: Path, marker: str, relative: str, refused: bool
    ) -> None:
        """A write-capable call needs project evidence in the bound folder or an ancestor below the home.

        The denylist cannot name every sensitive folder, so an acceptEdits run is limited to folders that look like a
        project; a subfolder of a repository qualifies through the repository root, and a README alone does not.
        """
        folder = user_home / relative
        folder.mkdir(parents=True)
        (user_home / marker).write_text("", encoding="utf-8")

        reason = bridge_mcp._project_marker_refusal(folder.resolve())

        assert (reason is not None) is refused

    def test_implement_marker_in_the_home_itself_does_not_qualify(self, user_home: Path) -> None:
        """A dotfiles repository in the home does not turn every folder below the home into a project.

        The home's own ``.git`` is excluded from the ancestor search, so ``~/Documents`` still lacks a project marker.
        """
        (user_home / ".git").mkdir()
        folder = user_home / "Documents"
        folder.mkdir()

        reason = bridge_mcp._project_marker_refusal(folder.resolve())

        assert reason == "a folder with no project marker such as .git or pyproject.toml in it or its parents"

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            pytest.param("model", "-c", id="model-option-shaped"),
            pytest.param("run_id", "run id", id="run-id-free-text"),
        ],
    )
    def test_mcp_rejects_non_identifier_model_and_run_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, value: str
    ) -> None:
        """MCP arguments obey the CLI's identifier rule before a request reaches the Claude child.

        A dash-led model would be parsed as a child CLI option, and free text would reach the child prompt and the logs.
        """
        monkeypatch.setattr(
            bridge_mcp, "run_request", lambda *args, **kwargs: pytest.fail("invalid identifier dispatched")
        )

        response = bridge_mcp.handle_message(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "bridge_advise", "arguments": {"task": "Check.", name: value}},
            },
            trusted_workspace=tmp_path,
        )

        assert response is not None
        assert response["error"]["code"] == -32602
        assert response["error"]["message"].startswith(f"{name} must match")


class TestArtifactContainment:
    """Bridge artifact writers and lifecycle commands stay inside the workspace's own ``.temp/bridge`` store."""

    @_skip_no_directory_symlinks
    @pytest.mark.integration
    def test_bound_stdio_preserves_artifact_containment(self, workspace: Path, outside: Path) -> None:
        """Native workspace binding does not bypass the supervisor's artifact protection.

        ``.temp`` is a symlink to a folder outside the project; a bound call must fail with the generic execution error,
        reveal no outside path and create nothing there.
        """
        (workspace / ".temp").symlink_to(outside, target_is_directory=True)
        with client() as connection:
            connection.initialize()
            binding = bind(connection, workspace)["result"]["structuredContent"]["binding_id"]
            connection.call(
                "bridge_advise", 4, {"task": "Reject artifact escape before provider work.", "binding_id": binding}
            )
            error = connection.receive()["error"]
            assert error == {"code": -32603, "message": "bridge execution failed"}
            assert str(outside) not in error["message"]
            assert not (outside / "bridge").exists()

    @_skip_no_directory_symlinks
    @pytest.mark.integration
    @_each_executable_tool
    def test_bound_stdio_rejects_nested_incident_escape(self, tool: str, git_workspace: Path, outside: Path) -> None:
        """Every correctly bound executable tool is rejected before a linked incident directory can write outside.

        ``.temp/bridge/incidents`` points outside the project; the call fails with the generic error, the outside folder
        stays empty, and the binding itself remains valid.
        """
        root = git_workspace / ".temp/bridge"
        root.mkdir(parents=True)
        (root / "incidents").symlink_to(outside, target_is_directory=True)
        with client() as connection:
            connection.initialize()
            binding = bind(connection, git_workspace)["result"]["structuredContent"]["binding_id"]
            connection.call(
                tool, 4, {"task": "No provider needed for depth refusal.", "binding_id": binding, "depth": 1}
            )
            reply = connection.receive()
            assert reply.get("error") == {"code": -32603, "message": "bridge execution failed"}
            assert list(outside.iterdir()) == []
            assert status(connection)["binding_id"] == binding

    @_skip_no_directory_symlinks
    @pytest.mark.integration
    @pytest.mark.parametrize("command", ["status", "result", "cancel"])
    def test_cli_lifecycle_rejects_nested_job_store_escape(
        self, command: str, workspace: Path, outside: Path, job_id: str
    ) -> None:
        """Lifecycle commands neither read an external queued record nor write a cancel marker through a linked store.

        ``.temp/bridge/jobs`` is a symlink to a folder holding a queued record; the CLI must exit 2 with an artifact
        error, empty stderr, and leave the record byte-identical with no marker beside it.
        """
        root = workspace / ".temp/bridge"
        root.mkdir(parents=True)
        record = _queued_record(job_id)
        (outside / f"{job_id}.json").write_bytes(record)
        (root / "jobs").symlink_to(outside, target_is_directory=True)

        completed = _run_bridge_call(command, workspace, job_id)

        assert completed.returncode == 2
        assert "artifact" in json.loads(completed.stdout)["error"]
        assert completed.stderr == ""
        assert [path.name for path in outside.iterdir()] == [f"{job_id}.json"]
        assert (outside / f"{job_id}.json").read_bytes() == record

    @_skip_no_directory_symlinks
    @pytest.mark.parametrize(
        "write",
        [
            pytest.param(_write_transcript, id="transcript"),
            pytest.param(_write_atomic_job, id="atomic-job"),
            pytest.param(_append_health, id="health"),
        ],
    )
    def test_cached_artifact_paths_reject_parent_replacement(
        self, write: Callable[[Any, Path], None], workspace: Path, outside: Path
    ) -> None:
        """Each writer rechecks its cached authority before following a later replacement of the artifact root.

        The bridge root is renamed away and replaced by a symlink to an outside folder after ``prepare``; the
        transcript, atomic job and health writers must all refuse and leave the outside folder empty.
        """
        paths = bridge_call.BridgePaths(workspace)
        paths.prepare()
        job_path = paths.jobs / f"{uuid.uuid4()}.json"
        paths.root.rename(workspace / "prior-bridge")
        paths.root.symlink_to(outside, target_is_directory=True)

        with pytest.raises(bridge_call.ArtifactBoundaryError, match="artifact member"):
            write(paths, job_path)

        assert list(outside.iterdir()) == []

    @_skip_no_directory_symlinks
    @pytest.mark.integration
    def test_cli_cancel_preserves_contained_directory_alias(self, workspace: Path, job_id: str) -> None:
        """A legitimate in-store alias of the jobs directory still works through the public lifecycle consumer.

        ``jobs`` is a symlink to a sibling folder inside ``.temp/bridge``; cancelling a queued job must request the
        cancel and write its marker into that contained target.
        """
        root = workspace / ".temp/bridge"
        target = root / "job-data"
        target.mkdir(parents=True)
        (root / "jobs").symlink_to(target, target_is_directory=True)
        (target / f"{job_id}.json").write_bytes(_queued_record(job_id))

        completed = _run_bridge_call("cancel", workspace, job_id)

        assert completed.returncode == 0, completed.stdout
        assert json.loads(completed.stdout)["status"] == "cancel_requested"
        assert json.loads((target / f"{job_id}.cancel.json").read_text())["job_id"] == job_id

    @_skip_no_directory_symlinks
    @pytest.mark.integration
    @pytest.mark.parametrize("target", ["external", "self-loop"])
    def test_cli_cancel_rejects_linked_job_leaf_before_read_or_marker_write(
        self, target: str, jobs_dir: Path, workspace: Path, tmp_path: Path, job_id: str
    ) -> None:
        """A job record that is itself a symlink is refused, whether it leads outside or loops onto itself.

        Being inside a contained directory is not enough: the leaf member must not be followed, no marker may be
        written, and the external record must stay untouched.
        """
        external = tmp_path / "outside.json"
        original = _queued_record(job_id)
        external.write_bytes(original)
        member = jobs_dir / f"{job_id}.json"
        member.symlink_to({"external": external, "self-loop": member}[target])

        completed = _run_bridge_call("cancel", workspace, job_id)

        assert completed.returncode == 2
        assert "artifact member" in json.loads(completed.stdout)["error"]
        assert completed.stderr == ""
        assert external.read_bytes() == original
        assert not (jobs_dir / f"{job_id}.cancel.json").exists()

    @_skip_no_hardlinks
    @pytest.mark.integration
    def test_cli_cancel_rejects_hardlinked_job_record(
        self, jobs_dir: Path, workspace: Path, tmp_path: Path, job_id: str
    ) -> None:
        """An external record inode hard-linked into the store is refused before its task or state is consumed.

        A second link count marks the record as shared with a file outside the store, so cancel must exit 2, keep the
        external bytes and write no marker.
        """
        external = tmp_path / "outside.json"
        original = _queued_record(job_id)
        external.write_bytes(original)
        os.link(external, jobs_dir / f"{job_id}.json")

        completed = _run_bridge_call("cancel", workspace, job_id)

        assert completed.returncode == 2
        assert "artifact member" in json.loads(completed.stdout)["error"]
        assert external.read_bytes() == original
        assert not (jobs_dir / f"{job_id}.cancel.json").exists()

    def test_member_boundary_rejects_windows_file_reparse_metadata(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Windows reparse-point file metadata is rejected on every host without faking a different pathlib platform.

        ``lstat`` is patched to report a regular file carrying the reparse attribute for one member; the boundary check
        must refuse it and leave the file unchanged.
        """
        paths = bridge_call.BridgePaths(tmp_path)
        paths.prepare()
        member = paths.jobs / f"{uuid.uuid4()}.json"
        member.write_bytes(b"{}")
        original_lstat = Path.lstat
        monkeypatch.setattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400, raising=False)

        def reparse_lstat(path: Path) -> os.stat_result | SimpleNamespace:
            """Supply the Windows-only metadata explicitly for the selected regular file."""
            if path == member:
                return SimpleNamespace(st_mode=stat.S_IFREG, st_nlink=1, st_file_attributes=0x400)
            return original_lstat(path)

        monkeypatch.setattr(Path, "lstat", reparse_lstat)

        with pytest.raises(bridge_call.ArtifactBoundaryError, match="artifact member"):
            paths._member(member)

        assert member.read_bytes() == b"{}"

    def test_atomic_write_collision_preserves_existing_temporary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When exclusive temporary creation fails, another writer's temporary file is left intact.

        The generated temporary name is forced to collide with an existing file; the write must raise
        ``FileExistsError``, keep that file's bytes, and not create the target.
        """
        paths = bridge_call.BridgePaths(tmp_path)
        paths.prepare()
        target = paths.jobs / f"{uuid.uuid4()}.json"
        generated = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
        temporary = target.with_name(f".{target.name}.{generated.hex}.tmp")
        original = b"another writer owns these bytes"
        temporary.write_bytes(original)
        monkeypatch.setattr(bridge_call.uuid, "uuid4", lambda: generated)

        with pytest.raises(FileExistsError):
            bridge_call._write_json(target, {"status": "queued"}, paths=paths)

        assert temporary.read_bytes() == original
        assert not target.exists()
