"""Inferred method-call edges: receivers typed by constructor, annotation, attribute, ``super()`` or class hierarchy.

Each case indexes a tiny project through the public ``scan`` entry point and reads the ``inferred`` edges of one caller,
so the per-file receiver facts and the whole-index resolution pass are exercised together. Every rule has a matching
never-guess case: a shadowed or rebound name, a conflicting rebind, an unindexed class, or an unindexed base ahead of
the defining class must leave the call without an inferred edge.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _scan_helpers import IGNORE_RUNNER_THREAD_FORK_WARNING
from codemap_py.graph import incremental_scan, scan

_ENGINE = (
    "class Engine:\n"
    "    def start(self):\n        return 1\n\n"
    "    @classmethod\n    def build(cls):\n        return cls()\n\n\n"
    "class Wheel:\n    def start(self):\n        return 2\n"
)


def _project(root: Path, consumer: str) -> Path:
    """Write ``pkg/engine.py`` (re-exported by ``pkg/__init__.py``) and ``pkg/use.py`` holding *consumer*."""
    pkg = root / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("from .engine import Engine\n", encoding="utf-8")
    (pkg / "engine.py").write_text(_ENGINE, encoding="utf-8")
    (pkg / "use.py").write_text(consumer, encoding="utf-8")
    return root


def _inferred(index: dict, caller: str) -> set[tuple[str, str]]:
    """Return ``(target, rule)`` of every inferred edge leaving ``module::symbol`` *caller*."""
    module, _, qualified = caller.partition("::")
    entry = next(m for m in index["modules"] if m["name"] == module)
    symbol = next(s for s in entry["symbols"] if s["qualified_name"] == qualified)
    return {(edge["target"], edge["inferred"]) for edge in symbol["calls"] if "inferred" in edge}


_HEADER = (
    "from typing import Optional\nfrom external_lib import Thing, ExternalBase\nfrom .engine import Engine, Wheel\n"
)


class TestInferredEdges:
    """A receiver whose class the source states yields an inferred edge to the defining method."""

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    @pytest.mark.parametrize(
        ("body", "caller", "expected"),
        [
            pytest.param(
                "def run():\n    e = Engine()\n    return e.start()\n",
                "pkg.use::run",
                {("pkg.engine::Engine.start", "constructor")},
                id="constructor-local",
            ),
            pytest.param(
                "def run():\n    return Wheel().start()\n",
                "pkg.use::run",
                {("pkg.engine::Wheel.start", "constructor")},
                id="constructor-direct",
            ),
            pytest.param(
                "def run(e: Optional[Engine]):\n    return e.start()\n",
                "pkg.use::run",
                {("pkg.engine::Engine.start", "annotation")},
                id="annotation-optional",
            ),
            pytest.param(
                "def run(e: 'Engine | None' = None):\n    e = e or make()\n    return e.start()\n",
                "pkg.use::run",
                {("pkg.engine::Engine.start", "annotation")},
                id="annotation-string-union-governs-rebind",
            ),
            pytest.param(
                "class Car:\n    def __init__(self, engine: Engine):\n        self.engine = engine\n"
                "    def go(self):\n        return self.engine.start()\n",
                "pkg.use::Car.go",
                {("pkg.engine::Engine.start", "attribute")},
                id="attribute-from-annotated-param",
            ),
            pytest.param(
                "class Turbo(Engine):\n    def start(self):\n        return super().start()\n",
                "pkg.use::Turbo.start",
                {("pkg.engine::Engine.start", "super")},
                id="super",
            ),
            pytest.param(
                "class Turbo(Engine):\n    def go(self):\n        return self.start()\n",
                "pkg.use::Turbo.go",
                {("pkg.engine::Engine.start", "base")},
                id="inherited-self",
            ),
            pytest.param(
                "def run():\n    return Engine.build()\n",
                "pkg.use::run",
                {("pkg.engine::Engine.build", "class")},
                id="class-qualified-imported",
            ),
            pytest.param(
                "import pkg\n\n\ndef run():\n    return pkg.Engine.build()\n",
                "pkg.use::run",
                {("pkg.engine::Engine.build", "class")},
                id="class-qualified-package-reexport",
            ),
        ],
    )
    def test_adds_edge_to_defining_method(
        self, tmp_path: Path, body: str, caller: str, expected: set[tuple[str, str]]
    ) -> None:
        """Each stated receiver class resolves to its indexed method, marked with the rule that inferred it.

        The scanner alone records these calls as unresolved or as a dangling member target, so ``fn-rdeps`` of the
        method missed them; the inferred edge is what makes the caller visible.
        """
        index = scan(_project(tmp_path, _HEADER + body))
        assert _inferred(index, caller) == expected


class TestNeverGuess:
    """A receiver whose class is not stated without ambiguity yields no inferred edge."""

    @IGNORE_RUNNER_THREAD_FORK_WARNING
    @pytest.mark.parametrize(
        ("body", "caller"),
        [
            pytest.param(
                "def run(Engine):\n    e = Engine()\n    return e.start()\n", "pkg.use::run", id="shadowed-class"
            ),
            pytest.param(
                "def run():\n    e = Engine()\n    e = make()\n    return e.start()\n", "pkg.use::run", id="reassigned"
            ),
            pytest.param(
                "def run(flag):\n    e = Engine()\n    if flag:\n        e = Wheel()\n    return e.start()\n",
                "pkg.use::run",
                id="conditional-rebind-other-class",
            ),
            pytest.param(
                "def run(items):\n    e = Engine()\n    for e in items:\n        e.start()\n",
                "pkg.use::run",
                id="loop-target-rebind",
            ),
            pytest.param("def run():\n    t = Thing()\n    return t.start()\n", "pkg.use::run", id="unindexed-class"),
            pytest.param("def run(t: Thing):\n    return t.start()\n", "pkg.use::run", id="unindexed-annotation"),
            pytest.param(
                "class Mixed(ExternalBase, Engine):\n    def go(self):\n        return self.start()\n",
                "pkg.use::Mixed.go",
                id="unindexed-base-ahead-of-definer",
            ),
            pytest.param(
                "class Car:\n    def __init__(self, flag):\n        self.engine = Engine()\n"
                "        if flag:\n            self.engine = Wheel()\n"
                "    def go(self):\n        return self.engine.start()\n",
                "pkg.use::Car.go",
                id="attribute-conflicting-classes",
            ),
            pytest.param(
                "class Solo:\n    def go(self):\n        return super().start()\n",
                "pkg.use::Solo.go",
                id="super-without-base",
            ),
            pytest.param(
                "class Turbo(Engine):\n    def go(self):\n        return super(Engine, self).start()\n",
                "pkg.use::Turbo.go",
                id="super-with-explicit-arguments",
            ),
            pytest.param(
                "def run(v):\n    e = Engine()\n    match v:\n        case [*e]:\n            pass\n    return e.start()\n",
                "pkg.use::run",
                id="match-star-capture-rebind",
            ),
            pytest.param(
                "def run(v):\n    e = Engine()\n    match v:\n        case {**e}:\n            pass\n    return e.start()\n",
                "pkg.use::run",
                id="match-mapping-rest-rebind",
            ),
            pytest.param("def run():\n    return Engine.missing()\n", "pkg.use::run", id="class-qualified-undefined"),
        ],
    )
    def test_leaves_call_without_inferred_edge(self, tmp_path: Path, body: str, caller: str) -> None:
        """Ambiguous, shadowed, rebound or unindexed receivers stay unresolved instead of being guessed.

        Each body differs from an inferred case by one binding or one unindexed name, so a rule that ignored the binding
        or the index would add a wrong edge here.
        """
        index = scan(_project(tmp_path, _HEADER + body))
        assert _inferred(index, caller) == set()


@IGNORE_RUNNER_THREAD_FORK_WARNING
def test_incremental_scan_drops_inferred_edge_of_a_carried_module(tmp_path: Path) -> None:
    """An unchanged caller loses its inferred edge when the method it pointed to is removed elsewhere.

    The consumer module is carried over unparsed by the incremental scan, so a pass that kept old inferred edges would
    leave a stale edge to a method that no longer exists.
    """
    root = _project(tmp_path, _HEADER + "def run():\n    return Engine().start()\n")
    old = scan(root)
    before = _inferred(old, "pkg.use::run")
    (root / "pkg" / "engine.py").write_text("class Engine:\n    pass\n\n\nclass Wheel:\n    pass\n", encoding="utf-8")
    new = incremental_scan(root, old)
    assert before == {("pkg.engine::Engine.start", "constructor")}
    assert _inferred(new, "pkg.use::run") == set()
