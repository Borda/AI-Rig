"""The blueprint-normalization contract vectors shared by the Python and JS blueprint test suites.

Cross-language contract vectors: ``audit/blueprint/test_build_blueprint_manifest.py`` runs them against
``bin/build_blueprint_manifest.py``; ``hooks/allow/test_blueprint_allow_js.py`` and
``hooks/allow/test_sentinel_read_allow_js.py`` run them against the JS port in ``hooks/blueprint-allow.js``. A vector
passing on only one side means the two hash spaces have diverged.

Deliberately a uniquely-named module rather than ``conftest``, for the reason ``_hook_env.py`` gives.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

VECTORS = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "blueprint_normalization_vectors.json").read_text(encoding="utf-8")
)


def vector_params(group: str) -> list:
    """Return the shared fixture's vectors for ``group`` as one ``pytest.param`` each."""
    return [pytest.param(vector, id=vector["id"]) for vector in VECTORS[group]]
