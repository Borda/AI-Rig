"""Readers for skill text and shipped scripts shared by the research plugin tests.

The contract tests compare marker phrases against skill Markdown with its wrapping ignored. The bin tests load a script
by path under a unique module name, because hyphenated script names are not importable and several plugins ship a script
with the same file name. One definition of each lives here.

The module name is plugin-specific because ``--import-mode=importlib`` tests share one ``sys.modules``: a bare
``_skill_text`` would collide with the Codex Rig helper of that name in a full-repository run. The develop plugin keeps
its own copy of these helpers on purpose, since ``installed_plugin`` tests run without the repository.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def flat(text: str) -> str:
    """Collapse every whitespace run to one space so a phrase matches wherever the source wraps its prose."""
    return " ".join(text.split())


def load_module(path: Path, name: str, *, register: bool = False) -> ModuleType:
    """Load the script at ``path`` as a module called ``name``, so sibling plugins cannot shadow each other.

    Args:
        path: Script file to execute.
        name: Unique module name to load it under.
        register: Also store the module in ``sys.modules`` before it runs, which a script defining a dataclass needs
            because dataclass resolution looks the defining module up by name.

    Returns:
        The executed module.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    if register:
        sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
