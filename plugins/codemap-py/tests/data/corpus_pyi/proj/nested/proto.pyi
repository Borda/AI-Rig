# Scenario: stub-only module in a nested authoritative package.
# Expected: stub_only=true; dotted name "nested.proto"; import of pkg.stub_only
# resolves as an import edge, no call edges.
from typing import Protocol

from pkg.stub_only import Marker

class Handler(Protocol):
    marker: Marker

    def handle(self) -> None: ...
