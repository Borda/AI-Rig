"""Anonymizer regression for the coverage block's ``remedies`` records.

``remedies`` is a list of ``{reason, remedy}`` records added to every incomplete coverage block. The telemetry record
keeps the full query result and the anonymizer passes any key it does not list through unchanged, so the field needs an
explicit rule: each string in each record is scrubbed by the qualified-name rule, wherever the field appears. Reducer
slugs and plain remedy prose stay readable; a qualified name that a future template might pick up is pseudonymized.
"""

from __future__ import annotations

import anonymize
import pytest

_SALT = b"x" * 32

#: Every completeness reducer slug the coverage block can emit.
_REDUCER_SLUGS = [
    "collision",
    "degraded",
    "module_degraded",
    "root_mismatch",
    "stale",
    "stale_undetermined",
    "symbol_alias_ambiguous",
    "untracked",
]

#: Remedy records whose prose carries no qualified-name token, as the coverage block emits them.
_PLAIN_REMEDIES = [
    pytest.param({"reason": "stale", "remedy": "Re-run /codemap-py:scan-codebase to update."}, id="stale"),
    pytest.param({"reason": "untracked", "remedy": "re-scan or verify with grep."}, id="untracked"),
    pytest.param(
        {"reason": "root_mismatch", "remedy": "Re-scan the current root, or query with a matching --root."},
        id="root-mismatch",
    ),
    pytest.param(
        {
            "reason": "symbol_alias_ambiguous",
            "remedy": "see symbol_alias_limitations for the alias and reason, then verify that path with grep.",
        },
        id="alias-ambiguous",
    ),
]


def _record_with(remedies: list[dict[str, str]]) -> dict:
    """Wrap *remedies* in a CLI telemetry record shaped like a real incomplete ``rdeps`` result."""
    return {"cmd": "rdeps", "result": {"importers": [], "index": {"query_complete": False, "remedies": remedies}}}


class TestRemediesScrub:
    """Every ``remedies`` string passes through the qualified-name scrub at any depth."""

    def test_injected_qualified_name_is_pseudonymized(self):
        """A remedy that carried a module path would leave the export pseudonymized, not verbatim."""
        record = _record_with([{"reason": "collision", "remedy": "drop the copy of pkg.secret_mod, then re-scan"}])

        out = anonymize.anonymize_record(record, _SALT)

        remedy = out["result"]["index"]["remedies"][0]["remedy"]
        assert "pkg.secret_mod" not in remedy
        assert remedy == f"drop the copy of {anonymize._pseudo('pkg.secret_mod', _SALT)}, then re-scan"

    @pytest.mark.parametrize("reason", _REDUCER_SLUGS)
    def test_reducer_slug_survives(self, reason):
        """Reducer slugs carry no identity and stay readable so exported telemetry can still be aggregated."""
        record = _record_with([{"reason": reason, "remedy": "verify with grep."}])

        out = anonymize.anonymize_record(record, _SALT)

        assert out["result"]["index"]["remedies"][0]["reason"] == reason

    @pytest.mark.parametrize("entry", _PLAIN_REMEDIES)
    def test_plain_remedy_prose_survives(self, entry):
        """A remedy without a qualified-name token is exported verbatim."""
        out = anonymize.anonymize_record(_record_with([entry]), _SALT)

        assert out["result"]["index"]["remedies"] == [entry]

    def test_top_level_remedies_scrubbed(self):
        """The rule is key-based at any depth, like ``not_covered``, not gated on the ``result`` subtree."""
        record = {"remedies": [{"reason": "stale", "remedy": "see pkg.secret_mod"}]}

        out = anonymize.anonymize_record(record, _SALT)

        assert "pkg.secret_mod" not in out["remedies"][0]["remedy"]
