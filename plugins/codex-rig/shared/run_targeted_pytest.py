"""Run a project's targeted pytest command with bytecode disabled and run-local pytest cache and coverage data.

## Purpose

Keep ordinary imports and pytest's bookkeeping from changing the checkout fingerprint during PR remediation.
The settings apply before the project runner starts and are inherited by Python and pytest subprocesses.

## Scope

The caller selects the existing project runner, test arguments, verified checkout and trusted run directory.
This helper does not select tests, change sandbox permissions, take an integrity snapshot or verify the result.

## Usage

From the plugin directory run the portable Python launcher with this script, --checkout <checkout>,
<run-directory>, -- and the complete project pytest command as separate arguments.

## Outputs

The child inherits the terminal streams. Pytest writes its selection cache, raw coverage data and requested file
reports underneath the run directory.

## Failure

Propagate the child's exit status, including expected regression failures. Invalid arguments or launch errors
fail the invocation before a test result is available.

## Used by

Code Remediate uses this entry point for direct sandboxed PR pytest runs before and after edits.
"""

from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def protected_pytest_environment(environment: dict[str, str], output: Path, run_directory: Path) -> dict[str, str]:
    """Expose only the artifact plugin on the import path and preserve the project's runner and options."""
    plugin_dir = str(Path(__file__).resolve().with_name("pytest_plugins"))
    python_path = environment.get("PYTHONPATH", "")
    return {
        **environment,
        "COVERAGE_FILE": str(output / ".coverage"),
        "CODEX_RIG_PYTEST_ARTIFACTS": str(run_directory),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": os.pathsep.join((python_path, plugin_dir)) if python_path else plugin_dir,
        "PYTEST_ADDOPTS": f"{environment.get('PYTEST_ADDOPTS', '')} -p codex_rig_pytest_artifacts".strip(),
    }


def main() -> int:
    """Launch the selected pytest invocation in its checkout and preserve its result."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.command:
        parser.error("supply the project pytest command after --")

    run_directory = args.run_directory.resolve()
    environment = protected_pytest_environment(dict(os.environ), run_directory, run_directory)
    # Windows resolves a relative executable against the launching process even with a child cwd.
    executable = args.command[0]
    if os.path.dirname(executable) and not os.path.isabs(executable):
        args.command[0] = str(args.checkout.resolve() / executable)
    try:
        result = subprocess.run(args.command, cwd=args.checkout, env=environment, check=False)  # noqa: S603 - caller-selected argv, no shell.
    except OSError as error:
        parser.exit(2, f"could not start project pytest command: {error}\n")
    return 128 - result.returncode if result.returncode < 0 else result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
