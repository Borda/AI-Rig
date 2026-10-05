# Scenario: stub-only module (no stub_only.py sibling).
# Expected: indexed once with stub_only=true; contributes declarations + imports
# (Marker, describe, PROTOCOL_VERSION) but NO executable body / call edges.

from typing import Protocol

class Marker(Protocol):
    name: str

    def describe(self) -> str: ...

def describe(marker: Marker) -> str: ...

PROTOCOL_VERSION: int
