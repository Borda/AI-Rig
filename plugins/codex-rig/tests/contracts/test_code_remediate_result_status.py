"""Keep remediation result authors able to report every shared gate outcome."""

import json
from pathlib import Path


def test_remediation_template_admits_pass_failure_and_timeout() -> None:
    """A timed-out remediation must remain expressible without changing its outcome to failure."""
    template_path = Path(__file__).resolve().parents[2] / "skills/code-remediate/result-template.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))

    assert set(template["status"].split("|")) == {"pass", "fail", "timeout"}
