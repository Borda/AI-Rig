"""Guard the oss:review rule that closes a missing-package evidence gap with an ephemeral overlay run.

A real review reported "hotcoco 1.1.0 not installed, so the parity claims were never run" as a confidence gap while the
PR's own ``pyproject.toml`` declared ``hotcoco>=1.1.0,<1.2`` and a matching wheel existed for the host. These tests pin
the replacement contract: only packages the base branch already declares, wheel-only on the default index, a non-
executing fetch into a run-local directory followed by a separate test run, an overlay that never touches the project
environment, and an evidence line (or a concrete failure reason) in place of a bare "not installed" gap. A name the base
or the user routes off the default index, or whose routing cannot be determined, is never fetched from PyPI.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parents[2] / "skills/review/templates"


def _template(name: str) -> str:
    """Read one review template as the orchestrator loads it."""
    return (_TEMPLATES / name).read_text(encoding="utf-8")


def _procedure() -> str:
    """Return the missing-package procedure block from the agent prompts."""
    text = _template("agent-prompts.md")
    start = text.index("**Missing-package evidence gap")
    return text[start : text.index("**Spawn slots", start)]


def test_procedure_follows_the_evidence_standard() -> None:
    """The procedure sits beside the finding evidence standard, so every agent prompt carries it."""
    text = _template("agent-prompts.md")
    assert text.index("**Finding evidence standard") < text.index("**Missing-package evidence gap — close it")


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("(every agent)", id="applies-to-every-agent"),
        pytest.param("already executes PR code (Agent 2 targeted tests)", id="gate-is-existing-pr-code-run"),
        pytest.param("read-only sandbox) keeps the gap with that reason", id="read-only-reviewer-keeps-gap"),
    ],
)
def test_procedure_scope_and_gate(marker: str) -> None:
    """The procedure binds every agent and rides the run's existing permission to execute PR code."""
    assert marker in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("the harness permission prompt for each exact command is the approval", id="prompt-is-approval"),
        pytest.param(
            "never add a reusable `uv pip install`, `uv run` or `pip` allow entry", id="no-reusable-allow-entry"
        ),
        pytest.param("a denial keeps the gap with the denial as evidence", id="denial-keeps-gap"),
    ],
)
def test_approval_is_the_exact_command_prompt(marker: str) -> None:
    """Each overlay command is approved at its own permission prompt, never through a standing allow rule.

    The earlier wording said the procedure needed no extra approval, which read as licence to allow-list `uv run`.
    """
    assert marker in _procedure()


def test_no_blanket_no_approval_claim() -> None:
    """The superseded claim that the procedure needs no approval stays removed."""
    assert "needs no extra approval" not in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("only a name the PR's base already declares (`git show <base sha>:<file>`)", id="base-declared"),
        pytest.param("`pyproject.toml` (`[project]` dependencies / optional-dependencies", id="pyproject"),
        pytest.param("`requirements*.txt`", id="requirements"),
        pytest.param("use PR-head's spec; cite base + head `file:line`", id="head-spec-with-citations"),
        pytest.param(
            "Never a name, version, or index taken from PR body, comments, commit messages, or code strings",
            id="never-from-untrusted-pr-text",
        ),
    ],
)
def test_only_base_declared_packages(marker: str) -> None:
    """Names come from the base branch's dependency files with a citation, never from untrusted PR text."""
    assert marker in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("Name only PR-head declares = new dependency", id="head-only-name-is-new"),
        pytest.param("finding `new dependency <pkg> — verify provenance`, never fetched", id="finding-not-fetch"),
        pytest.param("reason `new dependency, not overlaid`", id="gap-reason"),
    ],
)
def test_new_dependency_is_a_finding_not_an_install(marker: str) -> None:
    """A package the PR newly adds is reported for provenance review instead of being installed and imported."""
    assert marker in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(
            "uv pip install --dry-run --only-binary :all: --no-config --no-cache --default-index https://pypi.org/simple "
            "--python <env-python> '<pkg><spec>'`",
            id="support-check-wheel-only-default-index",
        ),
        pytest.param("its `+ <name>==<ver>` lines = overlay set", id="dry-run-lists-overlay-set"),
        pytest.param("--only-binary :all: --no-deps", id="pip-fallback-wheel-only"),
        pytest.param(
            "-m pip download --isolated --index-url https://pypi.org/simple", id="pip-download-default-index-only"
        ),
        pytest.param(
            "-m pip install --isolated --index-url https://pypi.org/simple", id="pip-install-default-index-only"
        ),
    ],
)
def test_wheel_only_on_default_index(marker: str) -> None:
    """The support check refuses source builds and every repository-declared or inherited index."""
    assert marker in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("PR-head spec = untrusted text → enters a command only as a pure version range", id="range-only"),
        pytest.param("only letters, digits, spaces, `. , * + ! - _ = < > ~`", id="character-gate"),
        pytest.param("extras + env marker never passed", id="no-extras-or-marker"),
        pytest.param("finding `non-index dependency reference <pkg>`, never fetched", id="direct-reference-finding"),
        pytest.param("gap reason `head specifier not a version range`", id="gap-reason"),
        pytest.param("`'<pkg><spec>'` stays one shell word", id="single-shell-word"),
    ],
)
def test_head_spec_must_be_a_version_range(marker: str) -> None:
    """A PR-head direct reference, URL or marker never reaches the installer or breaks the command's quoting.

    A direct reference would make the dry run download from a contributor-chosen host, and a quote inside a marker would
    end the single-quoted requirement early in the shell command the agent runs.
    """
    assert marker in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("**Index routing + `uv` env (fail closed, before any index read)**", id="routing-step"),
        pytest.param(
            "Check requested name before step 3, every other overlay-set name before step 4", id="every-overlay-name"
        ),
        pytest.param(
            "`pyproject.toml` `[[tool.uv.index]]`, `[tool.uv.sources]`, `[tool.uv]` `index-url`/`extra-index-url`/"
            "`find-links`, `[tool.uv.pip]`",
            id="base-uv-routing",
        ),
        pytest.param("other tools' source tables (`[[tool.poetry.source]]`, `[[tool.pdm.source]]`)", id="other-tools"),
        pytest.param("`requirements*.txt`/`constraints*.txt` + every file they include", id="requirements-options"),
        pytest.param('(`uv.lock` must say `registry = "https://pypi.org/simple"`)', id="lock-source"),
        pytest.param(
            "`PIP_INDEX_URL`, `PIP_EXTRA_INDEX_URL`, `PIP_FIND_LINKS`, `PIP_NO_INDEX`, `PIP_CONFIG_FILE` set (even empty)",
            id="user-index-variables",
        ),
        pytest.param("index/find-links setting in user or system `uv.toml`", id="user-uv-config"),
        pytest.param("key in `<env-python> -m pip config list`", id="user-pip-config"),
        pytest.param("`[[tool.uv.index]]` without `explicit = true`", id="global-index-routes-all"),
        pytest.param("or routing undeterminable", id="undetermined-fails-closed"),
        pytest.param("never fetched; gap stays, reason `non-default index dependency <pkg>`", id="routed-gap"),
        pytest.param("never its value (may hold credentials)", id="no-credential-values"),
        pytest.param("Never strip, override or replace a user index setting to reach PyPI", id="never-strip"),
    ],
)
def test_index_routing_fails_closed(marker: str) -> None:
    """A name the base or the user routes off the default index is never fetched from PyPI.

    A base pinning `acme-internal` to a private index, with a same-named squat on PyPI, would otherwise have the overlay
    download and execute the squat; clearing the user's index variables made PyPI the only answer.
    """
    assert marker in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("project's own + its uv workspace root", id="workspace-root-pyproject"),
        pytest.param(
            "every other requirements-format file (`*.in`, files under `requirements/`) + every "
            "`-r`/`--requirement`/`-c`/`--constraint` target, recursively",
            id="requirements-directories-and-nested-includes",
        ),
        pytest.param(
            "`Pipfile` `[[source]]` + package `index` key, `Pipfile.lock` `_meta.sources` + package `index`",
            id="pipfile-sources",
        ),
        pytest.param("Base content scan (parsed list names files; routing set elsewhere goes unseen)", id="scan"),
        pytest.param("git grep -n -I -i -E -e 'index[-_]?url|find[-_]links|no[-_]index|", id="scan-command"),
        pytest.param(
            "any other hit (CI workflow or Dockerfile `PIP_INDEX_URL`/`UV_INDEX_URL`", id="ci-and-docker-hits-count"
        ),
        pytest.param("→ every name's routing undetermined. Scan can't run → undetermined", id="scan-fails-closed"),
        pytest.param("Pip or not, read every pip config file directly for those keys", id="pip-config-without-pip"),
        pytest.param("legacy `~/.pip/pip.conf`", id="legacy-user-pip-config"),
        pytest.param("`C:\\ProgramData\\pip\\pip.ini`, `%APPDATA%\\pip\\pip.ini`", id="windows-pip-config"),
        pytest.param("content-scan hit outside parsed sources) → never fetched", id="scan-hit-routes-name"),
    ],
)
def test_index_routing_sees_sources_outside_the_named_files(marker: str) -> None:
    """Routing set in a file the parsed list never names still keeps the name off the default index.

    A credentialed private index configured only through `UV_INDEX_URL` in a CI workflow, with no lock file, passed
    every filename-based check, so the overlay fetched a same-named public squat and ran it.
    """
    assert marker in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(
            "any beyond `UV_CACHE_DIR`, `UV_NO_CACHE`, `UV_NO_PROGRESS`, `UV_NO_CONFIG`, `UV_LINK_MODE`, `UV_PYTHON`, "
            "`UV_MANAGED_PYTHON`, `UV_NO_MANAGED_PYTHON`",
            id="allow-list",
        ),
        pytest.param("gap stays, reason `uv environment overrides set: <names>`", id="override-gap"),
        pytest.param(
            "`UV_OVERRIDE`, `UV_CONSTRAINT`, `UV_EXCLUDE`, `UV_TORCH_BACKEND`, `UV_INSECURE_HOST`", id="names"
        ),
        pytest.param("never unset or empty them to force a run", id="never-cleared"),
        pytest.param("Step 2 passed + `--no-config`", id="default-index-only-after-checks"),
        pytest.param("PIP_CONFIG_FILE=/dev/null <env-python> -m pip download --isolated", id="pip-download-no-config"),
        pytest.param("PIP_CONFIG_FILE=/dev/null <env-python> -m pip install --isolated", id="pip-install-no-config"),
        pytest.param("global + site `pip.conf` still load under it", id="isolated-limit-stated"),
        pytest.param("step 2 routing check first (its `pip config list` covers", id="pip-fallback-routing-first"),
    ],
)
def test_resolver_environment_is_allow_listed(marker: str) -> None:
    """Any `UV_*` variable outside a short allowed set keeps the gap, and the pip fallback runs the routing check first.

    Clearing a fixed list of index variables missed `UV_OVERRIDE` and `UV_CONSTRAINT`, which still rerouted resolution
    under `--no-config`; `pip --isolated` still loads global and site `pip.conf`.
    """
    assert marker in _procedure()


@pytest.mark.parametrize(
    "superseded",
    [
        pytest.param("env -u UV_CONFIG_FILE", id="env-unset-prefix"),
        pytest.param("`not on default index`", id="absence-only-reason"),
    ],
)
def test_superseded_index_clearing_is_gone(superseded: str) -> None:
    """The procedure no longer strips inherited index settings and substitutes PyPI silently."""
    assert superseded not in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("**Fetch (no code runs)**", id="fetch-step"),
        pytest.param(
            "`uv pip install --only-binary :all: --no-deps --no-config --no-cache --default-index https://pypi.org/simple "
            "--python <env-python> --target \"$RUN_DIR/dep-overlay\" '<name>==<ver>' …`",
            id="fetch-into-run-local-target",
        ),
        pytest.param("**Run (separate command, no network)**", id="run-step"),
        pytest.param(
            '`PYTHONPATH="$RUN_DIR/dep-overlay" <env-python> -m pytest <tests> -p no:cacheprovider`',
            id="run-over-overlay",
        ),
        pytest.param("Never fetch and run in one command (`uv run --with …`)", id="no-combined-command"),
    ],
)
def test_fetch_and_run_are_split(marker: str) -> None:
    """Downloading wheels and executing PR code are separate commands, each approved on its own."""
    assert marker in _procedure()


def test_superseded_combined_overlay_command_is_gone() -> None:
    """The procedure no longer prescribes the combined download-and-execute overlay command."""
    assert "uv run --no-project" not in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(
            "`uv pip list --python <env-python>` before fetch and after run must be identical", id="env-identity-check"
        ),
        pytest.param(
            'Never `uv pip install` without `--dry-run` or `--target "$RUN_DIR/dep-overlay"`',
            id="no-project-env-install",
        ),
        pytest.param("Never `uv sync`, `uv add`", id="no-project-env-sync"),
        pytest.param(
            "`pip install` into project or global env, sdist build, or `--extra-index-url`", id="no-global-or-index"
        ),
    ],
)
def test_overlay_is_ephemeral(marker: str) -> None:
    """The run layers over the project env, which must stay byte-for-byte the same package set."""
    assert marker in _procedure()


def test_import_origin_proof_required() -> None:
    """An editable install pointing at another tree must not silently test the wrong checkout."""
    assert "path must be the reviewed checkout" in _procedure()


def test_evidence_line_records_every_field() -> None:
    """The evidence line names package, version, wheel, declaring specs, both commands, result and env check."""
    assert (
        "under `### Ephemeral dependency evidence`: `<pkg>==<ver> · wheel <filename> · spec base <file:line> head "
        "<file:line> '<spec>' · fetch <full command> · run <full command> · result <N passed, M failed> · "
        "env unchanged (pip list identical) · routing default index (<sources checked>)`"
    ) in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param("gap stays, naming failed step + concrete reason", id="gap-kept-with-reason"),
        pytest.param("`no cp313 macosx_11_0_arm64 wheel for X==Y`", id="no-wheel"),
        pytest.param("`index unreachable: <error>`", id="network"),
        pytest.param("`timeout after Ns`", id="timeout"),
        pytest.param("procedure skipped, not an evidence limit", id="bare-gap-is-skipped-procedure"),
    ],
)
def test_failure_keeps_gap_with_concrete_reason(marker: str) -> None:
    """No wheel, network failure or timeout keeps the gap, stated with the failed step and its reason."""
    assert marker in _procedure()


@pytest.mark.parametrize(
    "marker",
    [
        pytest.param(
            "copy every agent's `### Ephemeral dependency evidence` line verbatim into `Review Confidence`",
            id="evidence-copied",
        ),
        pytest.param("never carry that gap into the aggregate score", id="closed-gap-not-scored"),
        pytest.param("`ephemeral-install procedure not attempted`", id="bare-gap-flagged"),
    ],
)
def test_consolidator_closes_or_flags_package_gaps(marker: str) -> None:
    """An evidence line closes another reviewer's matching gap; a bare gap is flagged as not attempted."""
    assert marker in _template("consolidator-prompt.md")


def test_report_template_has_evidence_slot() -> None:
    """The report template gives the consolidator a fixed place for the evidence line, in the agents' field order."""
    text = _template("review-report.md")
    slot = text[text.index("### Review Confidence") :]
    assert "**Ephemeral dependency evidence**: `<pkg>==<ver> · wheel <filename> · spec base <file:line>" in slot
    assert "· fetch <full command> · run <full command> ·" in slot
    assert "· env unchanged · routing default index (<sources checked>)`" in slot
