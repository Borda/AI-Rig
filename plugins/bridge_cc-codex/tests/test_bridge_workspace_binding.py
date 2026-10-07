"""Exercise process-local Bridge workspace authority through real native stdio forms."""

from __future__ import annotations

import json
import os
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any

import pytest

SERVER = Path(__file__).resolve().parents[1] / "bin/bridge_mcp.py"
if str(SERVER.parent) not in sys.path:
    sys.path.insert(0, str(SERVER.parent))
import bridge_call  # noqa: E402
import bridge_mcp  # noqa: E402


class Client:
    """Exchange newline-delimited JSON with an actual server process with bounded waits."""

    def __init__(self, server: Path = SERVER) -> None:
        """Start the server and drain its stdout into a portable message queue."""
        self.process = subprocess.Popen(
            [sys.executable, str(server), "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
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
def client(server: Path = SERVER) -> Iterator[Client]:
    """Own the server process lifetime for one independent protocol scenario."""
    instance = Client(server)
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
    assert folder.resolve().as_posix() in confirmation["params"]["message"]
    assert confirmation["params"]["requestedSchema"]["properties"]["answer"]["enum"] == ["Bind this folder", "Cancel"]
    connection.answer(
        confirmation["id"], {"action": "accept", "content": {"answer": "Bind this folder"}, "_meta": None}
    )
    return connection.receive()


def status(connection: Client, request_id: int = 50) -> dict[str, Any]:
    """Read binding status without invoking any peer."""
    connection.call("bridge_status", request_id)
    return json.loads(connection.receive()["result"]["content"][0]["text"])


@pytest.mark.integration
def test_stdio_discovers_binding_tool_and_reports_unbound_native_protocol() -> None:
    """Expose the binding prerequisite honestly before any user authority exists."""
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
@pytest.mark.parametrize("tool", ["bridge_advise", "bridge_review", "bridge_implement"])
def test_stdio_calls_require_confirmed_binding_before_any_provider_artifacts(tool: str, tmp_path: Path) -> None:
    """Keep every executable sibling blocked until both native user forms complete."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    with client() as connection:
        connection.initialize()
        assert status(connection)["binding_status"] == "unbound"
        connection.call(tool, 2, {"task": "No provider execution before binding."})
        assert connection.receive()["error"]["code"] == -32002
        assert not (workspace / ".temp").exists()
        receipt = bind(connection, workspace)
        assert receipt["id"] == 3
        binding = receipt["result"]["structuredContent"]
        assert binding["status"] == "bound"
        assert binding["binding_status"] == "bound"
        assert binding["workspace"] == workspace.resolve().as_posix()
        assert isinstance(binding["binding_id"], str)
        assert status(connection)["workspace"] == workspace.resolve().as_posix()
        connection.call(
            tool,
            4,
            {
                "task": "Refuse before launching a peer.",
                "depth": 1,
                "run_id": "bound-proof",
                "binding_id": binding["binding_id"],
            },
        )
        envelope = json.loads(connection.receive()["result"]["content"][0]["text"])
        assert envelope["status"] == "refused"
        assert envelope["blockers"] == ["recursion-depth"]
        assert (workspace / ".temp/bridge").is_dir()


@pytest.mark.integration
@pytest.mark.parametrize("action", ["decline", "cancel"])
def test_rebind_cancellation_invalidates_previous_workspace(action: str, tmp_path: Path) -> None:
    """Cancelled rebind must not silently retain previous execution authority."""
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
def test_binding_ignores_stale_ids_and_requires_second_exact_confirmation(tmp_path: Path) -> None:
    """A matched selection cannot authorize work through stale or malformed confirmation."""
    with client() as connection:
        connection.initialize()
        connection.call("bridge_bind_workspace")
        selection = connection.receive()
        connection.answer("stale-id", {"action": "accept", "content": {"answer": str(tmp_path)}})
        assert status(connection)["binding_status"] == "unbound"
        connection.answer(selection["id"], {"action": "accept", "content": {"answer": str(tmp_path)}})
        confirmation = connection.receive()
        connection.answer(selection["id"], {"action": "accept", "content": {"answer": "Bind this folder"}})
        assert status(connection)["binding_status"] == "unbound"
        connection.answer(confirmation["id"], {"action": "accept", "content": {"answer": "Approve"}})
        assert connection.receive()["result"]["structuredContent"]["status"] == "cancelled"
        assert status(connection)["binding_status"] == "unbound"


@pytest.mark.integration
def test_missing_form_support_and_model_workspace_override_fail_closed(tmp_path: Path) -> None:
    """Neither a model path nor an unsupported client can create authority."""
    with client() as connection:
        connection.initialize(capability={"url": {}})
        connection.call("bridge_bind_workspace", arguments={"workspace": str(tmp_path)})
        assert connection.receive()["error"]["code"] == -32602
        connection.call("bridge_bind_workspace", 4)
        assert connection.receive()["error"]["code"] == -32002
        assert status(connection)["binding_status"] == "unbound"


@pytest.mark.integration
def test_eof_cancels_unconfirmed_binding(tmp_path: Path) -> None:
    """Closing the transport after selection never binds the pending canonical folder."""
    with client() as connection:
        connection.initialize()
        connection.call("bridge_bind_workspace")
        selection = connection.receive()
        connection.answer(selection["id"], {"action": "accept", "content": {"answer": str(tmp_path)}})
        connection.receive()
        connection.process.stdin.close()
        receipt = connection.receive()["result"]["structuredContent"]
        assert receipt == {"status": "cancelled", "binding_status": "unbound", "binding_id": None, "workspace": None}


@pytest.mark.integration
@pytest.mark.parametrize("folder_kind", ["relative", "root", "home", "payload", "missing", "file"])
def test_binding_refuses_invalid_or_protected_folders(folder_kind: str, tmp_path: Path) -> None:
    """Only existing project directories outside broad or installed payload roots may bind."""
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
        connection.call("bridge_bind_workspace")
        form = connection.receive()
        connection.answer(form["id"], {"action": "accept", "content": {"answer": str(folder)}})
        assert connection.receive()["result"]["structuredContent"]["binding_status"] == "unbound"


def _directory_symlinks_available() -> bool:
    """Probe the capability at collection time without assuming Windows privileges."""
    with tempfile.TemporaryDirectory() as value:
        target = Path(value) / "target"
        target.mkdir()
        try:
            (Path(value) / "link").symlink_to(target, target_is_directory=True)
        except OSError:
            return False
        return True


_skip_no_directory_symlinks = pytest.mark.skipif(
    not _directory_symlinks_available(), reason="directory symlinks unavailable"
)


@_skip_no_directory_symlinks
@pytest.mark.integration
@pytest.mark.parametrize("tool", ["bridge_advise", "bridge_review", "bridge_implement"])
def test_symlink_retarget_invalidates_each_executable_sibling(tool: str, tmp_path: Path) -> None:
    """Revalidate the originally selected path before every kind of provider call."""
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
@pytest.mark.parametrize("tool", ["bridge_advise", "bridge_review", "bridge_implement"])
def test_rebind_same_folder_invalidates_previous_call_identity(tool: str, tmp_path: Path) -> None:
    """A stale or omitted caller expectation cannot silently use a newer process binding."""
    with client() as connection:
        connection.initialize()
        old = bind(connection, tmp_path)["result"]["structuredContent"]["binding_id"]
        current = bind(connection, tmp_path, 4)["result"]["structuredContent"]["binding_id"]
        assert current != old
        assert status(connection)["binding_id"] == current
        for request_id, arguments in [
            (5, {"task": "Missing expectation."}),
            (6, {"task": "Stale expectation.", "binding_id": old}),
        ]:
            connection.call(tool, request_id, arguments)
            assert connection.receive()["error"]["code"] == -32002
        assert not (tmp_path / ".temp").exists()


@_skip_no_directory_symlinks
@pytest.mark.integration
def test_bound_stdio_preserves_artifact_containment(tmp_path: Path) -> None:
    """Native workspace binding must not bypass existing supervisor artifact protection."""
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
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


@pytest.mark.parametrize(
    ("candidate", "refused"),
    [
        pytest.param(r"d:\Users\operator\.codex\plugins\cache\other-market\sibling\1.0", True, id="cache-sibling"),
        pytest.param(r"D:\Users\operator\project", False, id="home-project"),
        pytest.param(r"D:\Users\operator\.codex\plugins\cache-extra\project", False, id="prefix-sibling"),
    ],
)
def test_cache_guards_use_windows_path_ancestry_on_every_host(candidate: str, refused: bool) -> None:
    """Windows drive/case semantics reject actual cache descendants without prefix guessing."""
    plugin = PureWindowsPath(r"D:\Users\operator\.codex\plugins\cache\market\bridge\0.6.0")
    protected = bridge_mcp._protected_payload_roots(plugin)
    assert any(PureWindowsPath(candidate).is_relative_to(root) for root in protected) is refused


@pytest.mark.integration
def test_installed_cache_sibling_is_not_a_project_binding(tmp_path: Path) -> None:
    """The actual cache ancestor protects sibling plugins, not only this payload."""
    cache = tmp_path / "plugins" / "cache"
    installed = cache / "market" / "bridge" / "0.6.0"
    shutil.copytree(
        SERVER.parents[1], installed, ignore=shutil.ignore_patterns("tests", "__pycache__", ".pytest_cache")
    )
    sibling = cache / "other-market" / "other-plugin" / "1.0"
    sibling.mkdir(parents=True)
    with client(installed / "bin/bridge_mcp.py") as connection:
        connection.initialize()
        connection.call("bridge_bind_workspace")
        selection = connection.receive()
        connection.answer(selection["id"], {"action": "accept", "content": {"answer": str(sibling)}})
        assert connection.receive()["result"]["structuredContent"]["binding_status"] == "unbound"


@pytest.mark.integration
def test_directory_replacement_before_confirmation_does_not_bind(tmp_path: Path) -> None:
    """Canonical path text alone cannot authorize a replaced directory identity."""
    project = tmp_path / "project"
    project.mkdir()
    with client() as connection:
        connection.initialize()
        connection.call("bridge_bind_workspace")
        selection = connection.receive()
        connection.answer(selection["id"], {"action": "accept", "content": {"answer": str(project)}})
        confirmation = connection.receive()
        project.rename(tmp_path / "old-project")
        project.mkdir()
        connection.answer(confirmation["id"], {"action": "accept", "content": {"answer": "Bind this folder"}})
        assert connection.receive()["result"]["structuredContent"]["binding_status"] == "unbound"


@pytest.mark.integration
def test_duplicate_pending_rpc_id_cancels_binding_once(tmp_path: Path) -> None:
    """Transport reuse cannot leave an orphaned form that later grants authority."""
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


@_skip_no_directory_symlinks
@pytest.mark.integration
@pytest.mark.parametrize("tool", ["bridge_advise", "bridge_review", "bridge_implement"])
def test_bound_stdio_rejects_nested_incident_escape(tool: str, tmp_path: Path) -> None:
    """Reject every correctly bound executable sibling before a linked incident directory can write outside."""
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    root = workspace / ".temp/bridge"
    root.mkdir(parents=True)
    outside.mkdir()
    (root / "incidents").symlink_to(outside, target_is_directory=True)
    with client() as connection:
        connection.initialize()
        binding = bind(connection, workspace)["result"]["structuredContent"]["binding_id"]
        connection.call(tool, 4, {"task": "No provider needed for depth refusal.", "binding_id": binding, "depth": 1})
        reply = connection.receive()
        assert reply.get("error") == {"code": -32603, "message": "bridge execution failed"}
        assert list(outside.iterdir()) == []
        assert status(connection)["binding_id"] == binding


@_skip_no_directory_symlinks
@pytest.mark.integration
@pytest.mark.parametrize("command", ["status", "result", "cancel"])
def test_cli_lifecycle_rejects_nested_job_store_escape(command: str, tmp_path: Path) -> None:
    """Do not read an external queued record or write its cancellation marker through a linked job store."""
    workspace = tmp_path / "project"
    outside = tmp_path / "outside"
    root = workspace / ".temp/bridge"
    root.mkdir(parents=True)
    outside.mkdir()
    job_id = str(uuid.uuid4())
    record = json.dumps({"job_id": job_id, "status": "queued", "pid": None}).encode()
    (outside / f"{job_id}.json").write_bytes(record)
    (root / "jobs").symlink_to(outside, target_is_directory=True)
    completed = subprocess.run(
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
    assert completed.returncode == 2
    assert "artifact" in json.loads(completed.stdout)["error"]
    assert completed.stderr == ""
    assert [path.name for path in outside.iterdir()] == [f"{job_id}.json"]
    assert (outside / f"{job_id}.json").read_bytes() == record


@_skip_no_directory_symlinks
@pytest.mark.parametrize("seam", ["transcript", "atomic-job", "health"])
def test_cached_artifact_paths_reject_parent_replacement(seam: str, tmp_path: Path) -> None:
    """Recheck each writer's cached authority before following a later root replacement."""
    workspace = tmp_path / "project"
    workspace.mkdir()
    paths = bridge_call.BridgePaths(workspace)
    paths.prepare()
    root = paths.root
    job_path = paths.jobs / f"{uuid.uuid4()}.json"
    outside = tmp_path / "outside"
    outside.mkdir()
    root.rename(workspace / "prior-bridge")
    root.symlink_to(outside, target_is_directory=True)

    def _run_expected_failure() -> None:
        """Run the statements expected to fail as one callable."""
        if seam == "transcript":
            bridge_call._write_transcript(paths, "bounded output", "")
        elif seam == "atomic-job":
            bridge_call._write_json(job_path, {"status": "queued"}, paths=paths)
        else:
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

    with pytest.raises(bridge_call.ArtifactBoundaryError, match="artifact member"):
        _run_expected_failure()
    assert list(outside.iterdir()) == []


@_skip_no_directory_symlinks
@pytest.mark.integration
def test_cli_cancel_preserves_contained_directory_alias(tmp_path: Path) -> None:
    """Keep a legitimate in-store job directory alias usable through the public lifecycle consumer."""
    workspace = tmp_path / "project"
    root = workspace / ".temp/bridge"
    target = root / "job-data"
    target.mkdir(parents=True)
    (root / "jobs").symlink_to(target, target_is_directory=True)
    job_id = str(uuid.uuid4())
    (target / f"{job_id}.json").write_text(
        json.dumps({"job_id": job_id, "status": "queued", "pid": None}), encoding="utf-8", newline="\n"
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(SERVER.with_name("bridge_call.py")),
            "cancel",
            "--workspace",
            str(workspace),
            "--job-id",
            job_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout
    assert json.loads(completed.stdout)["status"] == "cancel_requested"
    assert json.loads((target / f"{job_id}.cancel.json").read_text())["job_id"] == job_id


@_skip_no_directory_symlinks
@pytest.mark.integration
@pytest.mark.parametrize("target", ["external", "self-loop"])
def test_cli_cancel_rejects_linked_job_leaf_before_read_or_marker_write(tmp_path: Path, target: str) -> None:
    """Do not consume an external record merely because its immediate directory is contained."""
    workspace = tmp_path / "project"
    jobs = workspace / ".temp/bridge/jobs"
    jobs.mkdir(parents=True)
    job_id = str(uuid.uuid4())
    external = tmp_path / "outside.json"
    original = json.dumps({"job_id": job_id, "status": "queued", "pid": None}).encode()
    external.write_bytes(original)
    member = jobs / f"{job_id}.json"
    member.symlink_to(external if target == "external" else member)
    completed = subprocess.run(
        [
            sys.executable,
            str(SERVER.with_name("bridge_call.py")),
            "cancel",
            "--workspace",
            str(workspace),
            "--job-id",
            job_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "artifact member" in json.loads(completed.stdout)["error"]
    assert completed.stderr == ""
    assert external.read_bytes() == original
    assert not (jobs / f"{job_id}.cancel.json").exists()


def _hardlinks_available() -> bool:
    """Probe hard-link creation without assuming host privilege or filesystem support."""
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "source"
        source.write_bytes(b"probe")
        try:
            os.link(source, Path(directory) / "linked")
        except OSError:
            return False
        return True


_skip_no_hardlinks = pytest.mark.skipif(not _hardlinks_available(), reason="hard links unavailable")


@_skip_no_hardlinks
@pytest.mark.integration
def test_cli_cancel_rejects_hardlinked_job_record(tmp_path: Path) -> None:
    """Reject an external record inode shared into the store before consuming its task or cancellation state."""
    workspace = tmp_path / "project"
    jobs = workspace / ".temp/bridge/jobs"
    jobs.mkdir(parents=True)
    job_id = str(uuid.uuid4())
    external = tmp_path / "outside.json"
    original = json.dumps({"job_id": job_id, "status": "queued", "pid": None}).encode()
    external.write_bytes(original)
    os.link(external, jobs / f"{job_id}.json")
    completed = subprocess.run(
        [
            sys.executable,
            str(SERVER.with_name("bridge_call.py")),
            "cancel",
            "--workspace",
            str(workspace),
            "--job-id",
            job_id,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "artifact member" in json.loads(completed.stdout)["error"]
    assert external.read_bytes() == original
    assert not (jobs / f"{job_id}.cancel.json").exists()


def test_member_boundary_rejects_windows_file_reparse_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Apply Windows reparse-file rejection on every host without simulating a different pathlib platform."""
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


def test_atomic_artifact_collision_preserves_existing_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never remove another writer's artifact when exclusive temporary creation fails."""
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
