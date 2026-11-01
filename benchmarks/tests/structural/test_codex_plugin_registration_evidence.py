"""Failure-evidence tests for the Codex plugin registration gates.

The structural runner admits a Codex arm home only if its plugin roster is exactly what the arm should carry: the
treatment pair for C, nothing the home registered itself for A. These tests pin that admission and, above all, the
evidence a rejection carries, since the disposable home is deleted while the launcher fails. The Codex command runner is
replaced by a stub, so no Codex process starts.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

FROZEN_REGISTRATION = (
    '[plugins."codemap-py@borda-ai-rig-frozen"]\nenabled = true\n\n'
    '[plugins."codex-rig@borda-ai-rig-frozen"]\nenabled = true\n'
)


@pytest.fixture(name="make_treatment_home")
def _make_treatment_home(script_run_codex: Any, tmp_path: Path) -> Callable[[str], Any]:
    """Return a factory for a C arm home whose ``config.toml`` holds the supplied registration text."""

    def _make(config: str) -> Any:
        """Create the home directory with the given configuration and wrap it as a C_strict arm home."""
        home_path = tmp_path / "codex-home"
        home_path.mkdir()
        (home_path / "config.toml").write_text(config, encoding="utf-8")
        return script_run_codex.ArmHome("C_strict", home_path, {}, True, True)

    return _make


@pytest.fixture(name="make_plain_home")
def _make_plain_home(script_run_codex: Any, tmp_path: Path) -> Callable[[str | None], Any]:
    """Return a factory for an A arm home; ``None`` leaves the home without any ``config.toml``."""

    def _make(config: str | None = None) -> Any:
        """Create the home directory, optionally with a configuration file, and wrap it as an A_plain arm home."""
        home_path = tmp_path / "plain-home"
        home_path.mkdir()
        if config is not None:
            (home_path / "config.toml").write_text(config, encoding="utf-8")
        return script_run_codex.ArmHome("A_plain", home_path, {"PATH": str(tmp_path / "empty-bin")}, False)

    return _make


@pytest.fixture(name="registered_home")
def _registered_home(make_treatment_home: Callable[[str], Any]) -> Any:
    """Return a C arm home whose configuration still registers both treatment plugins."""
    return make_treatment_home(FROZEN_REGISTRATION)


def _runner(stdout: str, *, returncode: int = 0) -> Callable[..., SimpleNamespace]:
    """Build a command runner stub that always reports the given exit status and standard output.

    >>> _runner("{}", returncode=2)(["codex"]).returncode
    2
    """

    def _command_runner(_command: list[str], **_kwargs: Any) -> SimpleNamespace:
        """Return the canned Codex command result."""
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    return _command_runner


def _roster(*names: str) -> str:
    """Serialize a Codex plugin listing in which every named plugin is installed and enabled.

    >>> json.loads(_roster("gmail"))
    {'installed': [{'name': 'gmail', 'enabled': True}]}
    """
    return json.dumps({"installed": [{"name": name, "enabled": True} for name in names]})


class TestInstalledPair:
    """Admission of the C arm's installed plugin roster."""

    def test_failure_reports_status_roster_and_surviving_registration(
        self, script_run_codex: Any, registered_home: Any
    ) -> None:
        """A rejected C roster names the exit status, the parsed roster, and the surviving registration.

        The paid launcher deletes the disposable home while failing, so a message carrying only the child's standard
        error cannot separate a lost registration from a Codex listing that returned nothing while ``config.toml`` still
        held both plugin tables. A negative status is the shape a signal-killed Codex process produces.
        """
        runner = _runner('{"installed": [], "available": []}', returncode=-9)

        with pytest.raises(RuntimeError) as failure:
            script_run_codex._verify_installed_plugin_pair(registered_home, command_runner=runner)

        message = str(failure.value)
        assert "rc=-9" in message
        assert "enabled=[]" in message
        assert "codemap-py@borda-ai-rig-frozen" in message
        assert "codex-rig@borda-ai-rig-frozen" in message

    def test_admits_codex_first_party_plugins_this_home_never_registered(
        self, script_run_codex: Any, registered_home: Any
    ) -> None:
        """First-party Codex plugins outside this home's registration are admitted and recorded.

        Codex 0.153 lists its own connector plugins for an authenticated home, and a disposable home can neither
        register nor remove them. They are present in every arm, so admission turns on what the home installed for
        itself while the remaining names are kept as host-tooling evidence.
        """
        runner = _runner(_roster("codemap-py", "codex-rig", "gmail", "slack"))

        script_run_codex._verify_installed_plugin_pair(registered_home, command_runner=runner)

        assert registered_home.host_plugin_names == ("gmail", "slack")

    def test_rejects_a_third_plugin_registered_by_this_home(
        self, script_run_codex: Any, make_treatment_home: Callable[[str], Any]
    ) -> None:
        """A home that registered a plugin beyond the reviewed pair is rejected.

        Host tooling is tolerated because no arm controls it; a third registration in the home's own ``config.toml`` is
        exactly the contamination the C admission exists to catch, so it must still fail closed.
        """
        home = make_treatment_home(
            FROZEN_REGISTRATION + '\n[plugins."extra-tool@borda-ai-rig-frozen"]\nenabled = true\n'
        )
        runner = _runner(_roster("codemap-py", "codex-rig", "extra-tool"))

        with pytest.raises(RuntimeError, match="plugin registration is invalid"):
            script_run_codex._verify_installed_plugin_pair(home, command_runner=runner)


class TestPlainAbsenceProbe:
    """Admission of the A arm's plugin-free home."""

    def test_admits_first_party_plugins_and_records_them(
        self, script_run_codex: Any, make_plain_home: Callable[[str | None], Any]
    ) -> None:
        """The control arm tolerates host plugins, rejects any treatment plugin, and records what it saw.

        Recording the host roster on the control home is what makes the tolerated names auditable: parity holds only
        while the same first-party tooling is present in every arm.
        """
        home = make_plain_home("")

        script_run_codex._verify_plain_plugin_absent(home, command_runner=_runner(_roster("gmail")))

        assert home.host_plugin_names == ("gmail",)

    def test_rejects_a_home_that_registered_a_plugin(
        self, script_run_codex: Any, make_plain_home: Callable[[str | None], Any]
    ) -> None:
        """A control home whose configuration registers any plugin is rejected.

        A registration that lists as disabled would otherwise pass the enabled-name check while leaving the control arm
        carrying an installed treatment plugin.
        """
        home = make_plain_home('[plugins."codemap-py@borda-ai-rig"]\nenabled = false\n')

        with pytest.raises(RuntimeError, match="carries a treatment plugin"):
            script_run_codex._verify_plain_plugin_absent(home, command_runner=_runner(_roster()))

    def test_failure_reports_status_and_roster(
        self, script_run_codex: Any, make_plain_home: Callable[[str | None], Any]
    ) -> None:
        """A failed A absence probe names the exit status and the roster Codex reported.

        The control arm proves the treatment plugin is absent, so a probe that cannot run must say how it failed rather
        than emit an empty diagnosis when Codex writes nothing to standard error.
        """
        home = make_plain_home()

        with pytest.raises(RuntimeError, match=r"absence probe failed: rc=2"):
            script_run_codex._verify_plain_plugin_absent(home, command_runner=_runner("", returncode=2))


class TestRegistrationParsing:
    """The helpers that read plugin registrations from a Codex home and its listing."""

    def test_registered_tables_read_every_registered_identifier(
        self, script_run_codex: Any, registered_home: Any
    ) -> None:
        """Every ``[plugins."<id>"]`` table in a Codex home is reported in sorted order.

        The evidence helper reads the same registration the permission composition rewrites, so it must recover both
        marketplace-qualified identifiers rather than the bare plugin names.
        """
        tables = script_run_codex._registered_plugin_tables(registered_home.path / "config.toml")

        assert tables == ["codemap-py@borda-ai-rig-frozen", "codex-rig@borda-ai-rig-frozen"]

    def test_registered_tables_are_empty_without_configuration(self, script_run_codex: Any, tmp_path: Path) -> None:
        """A home without a readable ``config.toml`` registers no plugin tables.

        The helper runs while an arm home is being torn down, so an unreadable configuration must degrade to an empty
        roster instead of replacing the original failure with its own.
        """
        assert script_run_codex._registered_plugin_tables(tmp_path / "config.toml") == []

    def test_enabled_names_are_empty_for_a_non_list_payload(self, script_run_codex: Any) -> None:
        """A roster payload that is not a list parses to an empty set of enabled names.

        A Codex build that reports plugins as an object would otherwise make the parser return ``False``, which compares
        unequal to every expected roster and reports a registration defect that never happened.
        """
        assert script_run_codex._enabled_plugin_names('{"plugins": {"codemap-py": {"enabled": true}}}') == set()
