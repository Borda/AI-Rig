"""Anonymizer regression for the ``dynamic_sites`` coverage record and the ``via`` key of dynamic importers.

``dynamic_sites`` is a new coverage-block record (``count``, ``modules``, ``scope``, ``check``) and resolved dynamic
importers gained a ``via`` slug. The telemetry record keeps the full query result and the anonymizer passes keys it does
not list through unchanged, so both must carry no identity by construction: counts, fixed slugs and one fixed sentence.
These cases prove the record survives export verbatim while the identifiers beside it are still scrubbed.
"""

from __future__ import annotations

import anonymize
from codemap_py.query.coverage import _DYNAMIC_SITES_CHECK, _dynamic_sites_payload

_SALT = b"x" * 32
_SECRET_MODULES = ("acme_secret.vault", "acme_secret.loader")


def _record() -> dict:
    """Return a telemetry record of an ``rdeps`` answer carrying a dynamic importer and a ``dynamic_sites`` record."""
    index = {
        "modules": [
            {"name": _SECRET_MODULES[0], "dynamic_sites": 1},
            {"name": _SECRET_MODULES[1], "dynamic_sites": 2},
        ]
    }
    coverage = {"query_complete": True, **_dynamic_sites_payload(index, command="rdeps", module_name=None)}
    importer = {
        "importer": _SECRET_MODULES[1],
        "path": "acme_secret/loader.py",
        "line": 3,
        "literal": _SECRET_MODULES[0],
        "via": "constant",
    }
    return {
        "command": "rdeps",
        "result": {"module": _SECRET_MODULES[0], "dynamic_imported_by": [importer], "index": coverage},
    }


class TestDynamicSitesExport:
    """Exported telemetry keeps ``dynamic_sites`` readable and identity-free."""

    def test_record_survives_verbatim(self):
        """The counts, scope slug and fixed check text are exported unchanged, so telemetry can aggregate them.

        The record is built by the coverage code itself from modules with secret names, then anonymized.
        """
        out = anonymize.anonymize_record(_record(), _SALT)

        assert out["result"]["index"]["dynamic_sites"] == {
            "count": 3,
            "modules": 2,
            "scope": "index",
            "check": _DYNAMIC_SITES_CHECK,
        }

    def test_no_identifier_reaches_the_export(self):
        """No module name of the project appears anywhere in the exported record, the new fields included.

        The importer row's ``via`` slug stays readable while its module names are pseudonymized.
        """
        out = anonymize.anonymize_record(_record(), _SALT)

        exported = repr(out)
        assert not any(secret in exported for secret in (*_SECRET_MODULES, "acme_secret"))
        assert out["result"]["dynamic_imported_by"][0]["via"] == "constant"

    def test_check_text_carries_no_dotted_token(self):
        """The fixed check text holds no dotted name the anonymizer would pseudonymize into an unreadable token.

        Text scrubbing is the fallback if a future rule ever scrubs the record; the sentence must survive it unchanged.
        """
        assert anonymize._anonymize_text(_DYNAMIC_SITES_CHECK, _SALT) == _DYNAMIC_SITES_CHECK
