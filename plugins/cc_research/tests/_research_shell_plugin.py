"""Render host-loaded recipes while doubling root selection in downstream workflow tests."""

from __future__ import annotations

import re
import shlex
import sys
from pathlib import Path

from render_plugin_root import render_document


def render_shell(body: str, root: Path, plugin: str) -> str:
    """Run the guarded bootstrap against a strict fixture root-selection boundary."""
    selected = shlex.quote(root.as_posix())
    resolver = shlex.quote((root / "bin/resolve_plugin_root.py").as_posix())
    owner = shlex.quote(plugin)
    renderer = shlex.quote((root / "bin/render_plugin_root.py").as_posix())
    render_code = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent / 'bin')!r}); "
        "from render_plugin_root import render_document; "
        "sys.stdout.write(render_document(Path(sys.argv[2]).read_text(encoding='utf-8'), sys.argv[1]))"
    )
    bootstrap = (
        'PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" '
        f"--plugin-name {plugin}) || exit $?; export PLUGIN_ROOT\n"
    )
    if "$PLUGIN_ROOT/bin/" in body and "PLUGIN_ROOT=$(" not in body:
        body = bootstrap + body
    body = re.sub(r"\bpython\s*\(\s*\)\s*\{", "workflow_python() {", body)
    boundary = (
        "python() {\n"
        f'  if [ "$1" = {resolver} ]; then\n'
        f'    [ "$#" -eq 3 ] && [ "$2" = --plugin-name ] && [ "$3" = {owner} ] || return 2\n'
        f"    printf '%s\\n' {selected}\n"
        f'  elif [ "$1" = {renderer} ]; then\n'
        '    [ "$#" -eq 2 ] || return 2\n'
        f'    {shlex.quote(Path(sys.executable).as_posix())} -c {shlex.quote(render_code)} {selected} "$2"\n'
        "  elif command -v workflow_python >/dev/null 2>&1; then\n"
        '    workflow_python "$@"\n'
        '  else\n    command python "$@"\n  fi\n}\n'
    )
    return boundary + render_document(body, root)
