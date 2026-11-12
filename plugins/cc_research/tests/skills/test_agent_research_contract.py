"""Guard data acquisition and scientist review instructions against incomplete evidence."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

_PLUGIN = Path(__file__).resolve().parents[2]


def _lines(agent: str) -> list[str]:
    """Read an agent contract as individual Markdown lines."""
    return (_PLUGIN / "agents" / f"{agent}.md").read_text(encoding="utf-8").splitlines()


def test_api_acquisition_retains_raw_pagination_evidence() -> None:
    """API completeness must use raw responses rather than a WebFetch summary."""
    lines = _lines("data-steward")
    fetch = next(line for line in lines if line.startswith("2. **Fetch with completeness enforcement**"))
    assert all(term in fetch for term in ("Bash", "curl", "raw", "disk", "Link", "cursor"))
    assert "WebFetch with pagination" not in fetch
    assert any("WebFetch" in line and "HTML discovery only" in line for line in lines)
    assert any("unverified" in line and "pagination" in line for line in lines)
    fallback = next(line for line in lines if line.startswith("| `foundry:web-explorer`"))
    assert "Bash for raw source downloads" in fallback
    assert "URL discovery and scraping" not in fallback


def test_post_fetch_reuses_full_record_validation() -> None:
    """A scrape cannot pass with required fields checked only in a five-record sample."""
    lines = _lines("data-steward")
    post_fetch = next(line for line in lines if line.startswith("**Post-fetch validation**"))
    assert "Completeness verification" in post_fetch
    assert "Encoding" in post_fetch
    assert "first 5" not in post_fetch
    assert "spot-check" not in post_fetch
    assert any(line.startswith("[ ] Schema:") and "every record" in line for line in lines)
    assert any(line.startswith("[ ] Split Integrity Rules") for line in lines)
    provenance = next(line for line in lines if line.startswith("4. **Document provenance**"))
    assert "Source documentation" in provenance


def test_shuffled_evaluation_severity_requires_metric_effect() -> None:
    """Shuffle alone is Info while omitted samples or batch weighting justify Medium."""
    lines = _lines("data-steward")
    severity = next(line for line in lines if line.startswith("- **shuffle=True"))
    assert all(term in severity for term in ("[Info]", "[Medium]", "drop_last=True", "per-batch"))
    hygiene = next(line for line in lines if line.startswith("- **Engineering-hygiene severity tier**"))
    assert "shuffle-only" in hygiene
    medium = next(line for line in lines if line.startswith("[Medium]"))
    assert "shuffle=True" not in medium
    assert "order-dependent" in medium


def test_authenticated_acquisition_protects_secrets() -> None:
    """Acquisition cannot expose tokens through commands, artifacts, or error diagnostics."""
    line = next(line for line in _lines("data-steward") if line.startswith("**Authenticated sources**"))
    assert all(term in line for term in ("existing", "never", "tokens", "cookies", "logs", "dataset_card.yaml"))
    assert "redact" in line
    assert "set -x" in line


@pytest.mark.integration
@pytest.mark.parametrize("fragment_present", [True, False])
def test_scientist_load_command_emits_reference_or_warning(tmp_path: Path, fragment_present: bool) -> None:
    """The actual scientist recipe resolves the plugin path and exposes missing-fragment degradation."""
    line = next(
        line.strip() for line in _lines("scientist") if line.strip().startswith('python "${CLAUDE_PLUGIN_ROOT}/bin/')
    )
    argv = shlex.split(line.removesuffix(" || exit 1"))
    assert argv[1] == "${CLAUDE_PLUGIN_ROOT}/bin/load-agent-reference.py"
    root = tmp_path / "plugin with spaces"
    sidecar = root / "references/scientist"
    sidecar.mkdir(parents=True)
    fragment = sidecar / ("ml-concepts.md" if fragment_present else "other.md")
    fragment.write_text("reference evidence", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, str(_PLUGIN / "bin/load-agent-reference.py"), *argv[2:]],
        env={**os.environ, "CLAUDE_PLUGIN_ROOT": str(root), "PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ("reference evidence" if fragment_present else argv[4])
    assert "⚠" in argv[4]
    assert "degraded" in argv[4]
    assert line.endswith(" || exit 1")


def test_experiment_workflow_uses_canonical_design_process() -> None:
    """Design and implementation use one seed-averaging and falsification contract."""
    lines = _lines("scientist")
    design = next(line for line in lines if line.startswith("4. Experiment design:"))
    implementation = next(line for line in lines if line.startswith("5. Implement and validate:"))
    assert "Experiment Design Process" in design
    assert "Experiment Design Process" in implementation
    start = lines.index("## Experiment Design Process")
    end = lines.index("## Evaluating Results")
    process = lines[start:end]
    assert any("≥3" in line and "mean ± std" in line and "baseline" in line for line in process)
    assert any("falsif" in line for line in process)
    assert any("one variable" in line for line in process)


def test_hypothesis_prediction_is_optional_without_losing_falsifiability() -> None:
    """Scientist follows the queue schema while retaining a concrete refutation criterion."""
    line = next(line for line in _lines("scientist") if line.startswith("When emitting hypotheses to JSONL"))
    assert "optional" in line
    assert "falsifiable_prediction" in line
    assert "rationale" in line
    assert "each line must include a `falsifiable_prediction`" not in line


def test_paper_equations_require_a_local_pdf_and_page_reads() -> None:
    """A summarized web response cannot substitute for paper bytes used to quote equations."""
    line = next(line for line in _lines("scientist") if line.startswith("2. Literature search:"))
    assert all(term in line for term in ("Bash", "curl", "run directory", "Read", "pages"))
    assert "WebFetch" in line
    assert "HTML" in line
    assert "Use WebFetch to download" not in line


def test_skill_review_cannot_modify_sources_or_load_prior_memory() -> None:
    """Skill reviews preserve source files and state while allowing only requested report artifacts."""
    lines = _lines("scientist")
    review = next(line for line in lines if line.startswith("- **Skill-dispatched reviews**"))
    assert all(term in review for term in ("read-only", "source", "state", "run directory", "artifacts", "Bash"))
    assert "enforced" in review
    assert "tool sandbox" in review
    assert not any(line.startswith("memory:") for line in lines)
    assert any("prior-run memory" in line and "independent" in line for line in lines)
