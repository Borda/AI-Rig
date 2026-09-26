#!/usr/bin/env python3
"""Check plugin versions and committed version continuity.

Purpose: Keep both host manifests aligned and prevent a shipped plugin version or serialized integer version from
skipping its committed HEAD baseline. Scope: Scan dual-host manifests below ``--scan-dir`` and tracked host manifests
still shipped; compare changed shipped JSON and Python filenames supplied by pre-commit for HEAD continuity, including
nested JSON version, schema, and protocol fields, Python dictionary literals with those field names, and module-level
integer ``*_VERSION``, ``_SCHEMA``, and ``PROTOCOL`` constants. Test and report files are outside the selection. Usage:
Run ``check_plugin_version_sync.py --scan-dir plugins [changed JSON/Python paths...]`` from the repository checkout.
Positional paths are optional for a host-sync-only run. Outputs: Print actionable findings and exit 1 on a mismatch,
skipped or downgraded version, or unreadable selected file. Otherwise print success and exit 0. No writes. Failure: A
missing Git HEAD, invalid selected file, or Git read failure fails closed. A file absent from HEAD starts each static
integer version family at 1. Used by: The repository pre-commit ``check-plugin-version-sync`` hook and adjacent
packaging tests.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


_SEMVER = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")


def _read_version(manifest: Path) -> str | None:
    """Return the ``version`` string from a plugin manifest, or None if unreadable.

    Args:
        manifest: Path to a ``plugin.json`` file.

    Returns:
        The ``version`` value when the file parses and carries one, else None.
    """
    try:
        value = json.loads(manifest.read_text(encoding="utf-8")).get("version")
    except (OSError, ValueError):
        return None
    return value if isinstance(value, str) else None


def find_desyncs(scan_dir: Path) -> list[str]:
    """Return one finding line per dual-manifest plugin whose versions disagree.

    Args:
        scan_dir: Root to scan; every ``.claude-plugin`` directory found below it
            whose parent also holds a ``.codex-plugin`` directory is checked.

    Returns:
        Human-readable finding strings; empty when all pairs agree.
    """
    findings: list[str] = []
    for claude_dir in sorted(scan_dir.rglob(".claude-plugin")):
        plugin_root = claude_dir.parent
        codex_manifest = plugin_root / ".codex-plugin" / "plugin.json"
        claude_manifest = claude_dir / "plugin.json"
        if not codex_manifest.parent.is_dir():
            continue  # single-host plugin — nothing to desync
        claude_version = _read_version(claude_manifest)
        codex_version = _read_version(codex_manifest)
        rel = plugin_root.as_posix()
        if claude_version is None or codex_version is None:
            missing = ".claude-plugin" if claude_version is None else ".codex-plugin"
            findings.append(f"{rel}: unreadable or missing version in {missing}/plugin.json")
        elif claude_version != codex_version:
            findings.append(f"{rel}: .claude-plugin {claude_version} != .codex-plugin {codex_version}")
    return findings


def find_removed_host_manifests(scan_dir: Path) -> list[str]:
    """Find tracked host manifests deleted from plugins that still ship files."""
    root_result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    if root_result.returncode != 0:
        return ["cannot locate the Git checkout for tracked host manifests"]
    root = Path(root_result.stdout.strip()).resolve()
    try:
        relative_scan = scan_dir.resolve().relative_to(root)
    except ValueError:
        return []  # A caller may scan an unrelated, untracked directory for host sync only.
    tracked = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD", "--", relative_scan.as_posix()],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if tracked.returncode != 0:
        return ["cannot inspect committed host manifests"]
    tracked_paths = {Path(line) for line in tracked.stdout.splitlines()}
    findings: list[str] = []
    for manifest in sorted(
        path
        for path in tracked_paths
        if path.parts[-2:] in {(".claude-plugin", "plugin.json"), (".codex-plugin", "plugin.json")}
    ):
        plugin_root = root / manifest.parent.parent
        if (
            plugin_root.is_dir()
            and any(path.is_file() for path in plugin_root.rglob("*"))
            and not (root / manifest).is_file()
        ):
            findings.append(f"{manifest.parent.parent.as_posix()}: tracked manifest missing: {manifest.as_posix()}")
    return findings


def _literal_integer(value: ast.expr | None) -> int | None:
    """Read a signed integer literal without treating booleans as versions."""
    if isinstance(value, ast.Constant) and isinstance(value.value, int) and not isinstance(value.value, bool):
        return value.value
    if (
        isinstance(value, ast.UnaryOp)
        and isinstance(value.op, ast.USub)
        and isinstance(value.operand, ast.Constant)
        and isinstance(value.operand.value, int)
        and not isinstance(value.operand.value, bool)
    ):
        return -value.operand.value
    return None


def _python_dict_version_fields(tree: ast.AST) -> dict[str, int]:
    """Identify literal dictionary versions by lexical scope and named assignment."""
    fields: dict[str, int] = {}
    counts: dict[tuple[tuple[str, ...], str], int] = {}

    def visit(node: ast.AST, scope: tuple[str, ...]) -> None:
        """Walk dictionary expressions while retaining their containing scope."""
        if isinstance(node, ast.ClassDef):
            scope = (*scope, f"class:{node.name}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = (*scope, f"function:{node.name}")
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            scope = (*scope, f"assignment:{node.targets[0].id}")
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            scope = (*scope, f"assignment:{node.target.id}")
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                    continue
                name = key.value
                if name not in {"version", "schema", "protocol"} and not name.endswith(
                    ("_version", "_schema", "_protocol")
                ):
                    continue
                identity = (scope, name)
                index = counts.get(identity, 0)
                counts[identity] = index + 1
                integer = _literal_integer(value)
                if integer is not None:
                    fields[f"dict.{'.'.join(scope)}.{name}[{index}]"] = integer
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, ("<module>",))
    return fields


def _python_version_fields(source: str) -> tuple[dict[str, int], set[str], set[str]]:
    """Read static Python version fields and identify unverifiable module mutations."""
    fields: dict[str, int] = {}
    augmented: set[str] = set()
    nonliteral: set[str] = set()
    tree = ast.parse(source)
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = [target for target in statement.targets if isinstance(target, ast.Name)]
        elif isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
            targets = [statement.target]
        elif isinstance(statement, ast.AugAssign) and isinstance(statement.target, ast.Name):
            if statement.target.id.endswith("_VERSION") or statement.target.id in {"_SCHEMA", "PROTOCOL"}:
                fields.pop(statement.target.id, None)
                augmented.add(statement.target.id)
            continue
        else:
            continue
        integer = _literal_integer(statement.value)
        for target in targets:
            if target.id.endswith("_VERSION") or target.id in {"_SCHEMA", "PROTOCOL"}:
                if integer is None:
                    fields.pop(target.id, None)
                    nonliteral.add(target.id)
                else:
                    fields[target.id] = integer

    fields.update(_python_dict_version_fields(tree))
    return fields, augmented, nonliteral


def _version_fields(value: Any, path: tuple[str | int, ...] = ()) -> dict[tuple[str | int, ...], Any]:
    """Find version-named fields at every depth of a JSON document."""
    fields: dict[tuple[str | int, ...], Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child_path = (*path, key)
            if key in {"version", "schema", "protocol"} or key.endswith(("_version", "_schema", "_protocol")):
                fields[child_path] = item
            fields.update(_version_fields(item, child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            fields.update(_version_fields(item, (*path, index)))
    return fields


def _semver_finding(path: str, current: Any, baseline: Any) -> str | None:
    """Describe an invalid one-step SemVer bump from its committed version."""
    current_match = _SEMVER.fullmatch(current) if isinstance(current, str) else None
    if current_match is None:
        return f"{path}: plugin version must be X.Y.Z SemVer, got {current!r}"
    if baseline is None:
        return None
    baseline_match = _SEMVER.fullmatch(baseline) if isinstance(baseline, str) else None
    if baseline_match is None:
        return f"{path}: HEAD plugin version is not X.Y.Z SemVer: {baseline!r}"
    old_major, old_minor, old_patch = map(int, baseline_match.groups())
    new_major, new_minor, new_patch = map(int, current_match.groups())
    if (new_major, new_minor, new_patch) in {
        (old_major, old_minor, old_patch),
        (old_major, old_minor, old_patch + 1),
        (old_major, old_minor + 1, 0),
        (old_major + 1, 0, 0),
    }:
        return None
    return f"{path}: plugin version {current} must equal HEAD {baseline} or be one patch/minor/major bump"


def _python_discontinuities(path: str, current: str, baseline: str | None) -> list[str]:
    """Report discontinuities in static Python constants and dictionary versions."""
    new_fields, augmented, nonliteral = _python_version_fields(current)
    old_fields = _python_version_fields(baseline)[0] if baseline is not None else {}
    findings: list[str] = []
    invalid = augmented | {name for name in nonliteral if name in old_fields or name in new_fields}
    for name in sorted(augmented):
        findings.append(f"{path}:{name}: augmented version mutation cannot be checked against HEAD")
    for name in sorted(invalid - augmented):
        findings.append(f"{path}:{name}: nonliteral version mutation cannot be checked against HEAD")

    old_dict: dict[str, list[int]] = {}
    new_dict: dict[str, list[int]] = {}
    for source, grouped in ((old_fields, old_dict), (new_fields, new_dict)):
        for name, value in source.items():
            if name.startswith("dict."):
                key = name.rsplit("[", 1)[0]
                grouped.setdefault(key, []).append(value)
    # Match field families, not source occurrence numbers: inserting an earlier
    # dictionary must neither hide a jump nor turn an unchanged producer into a downgrade.
    for key, old_values in old_dict.items():
        current_values = new_dict.get(key, [])
        if len(old_values) > 1 and current_values != old_values:
            findings.append(f"{path}:{key}: ambiguous changed inline version producers")
            continue
        if len(current_values) < len(old_values) or any(
            not any(value in {old, old + 1} for value in current_values) for old in old_values
        ):
            findings.append(f"{path}:{key}: literal version field removed from HEAD")
        if any(value > max(old_values) + 1 or value < 1 for value in current_values):
            findings.append(f"{path}:{key}: version exceeds the next HEAD value ({max(old_values) + 1})")
    # A new scope can reference a shipped field name; an unseen name starts a new family.
    old_dict_versions: dict[str, set[int]] = {}
    for key, values in old_dict.items():
        old_dict_versions.setdefault(key.rsplit(".", 1)[-1], set()).update(values)
    for key, values in new_dict.items():
        if key in old_dict:
            continue
        old_values = old_dict_versions.get(key.rsplit(".", 1)[-1], set())
        for value in values:
            if value == 1 or any(value in {old, old + 1} for old in old_values):
                continue
            if old_values:
                findings.append(f"{path}:{key}: version {value} exceeds the matching HEAD dictionary version")
            else:
                findings.append(f"{path}:{key}: new version family must start at 1, got {value}")
    for name, value in new_fields.items():
        if name.startswith("dict."):
            continue
        old_value = old_fields.get(name)
        if old_value is None and value != 1:
            findings.append(f"{path}:{name}: new version family must start at 1, got {value}")
        elif old_value is not None and value not in {old_value, old_value + 1}:
            findings.append(
                f"{path}:{name}: version {value} must equal HEAD {old_value} or be exactly next ({old_value + 1})"
            )
    for name in old_fields.keys() - new_fields.keys() - invalid:
        if name.startswith("dict."):
            continue
        findings.append(f"{path}:{name}: literal integer version removed from HEAD")
    return findings


def find_head_discontinuities(files: list[str]) -> list[str]:
    """Compare selected shipped JSON and Python versions with committed HEAD."""
    if not files:
        return []
    root_result = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    if root_result.returncode != 0:
        return ["cannot locate the Git checkout for version continuity"]
    root = Path(root_result.stdout.strip()).resolve()
    head_result = subprocess.run(["git", "cat-file", "-e", "HEAD^{commit}"], cwd=root, capture_output=True, check=False)
    if head_result.returncode != 0:
        return ["cannot read committed HEAD for version continuity"]

    findings: list[str] = []
    for filename in files:
        try:
            path = Path(filename).resolve()
            relative = path.relative_to(root)
        except ValueError:
            findings.append(f"{filename}: selected file is outside the Git checkout")
            continue
        if relative.suffix not in {".json", ".py"} or relative.parts[:1] != ("plugins",):
            continue
        if any(part in {"tests", "reports", ".reports"} for part in relative.parts[2:-1]):
            continue
        try:
            current_source = path.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:
            findings.append(f"{relative.as_posix()}: cannot read selected file: {exc}")
            continue
        tracked = subprocess.run(
            ["git", "ls-tree", "--name-only", "HEAD", "--", relative.as_posix()],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if tracked.returncode != 0:
            findings.append(f"{relative.as_posix()}: cannot inspect committed HEAD")
            continue
        prior = None
        if tracked.stdout.strip():
            prior = subprocess.run(
                ["git", "show", f"HEAD:{relative.as_posix()}"], cwd=root, capture_output=True, text=True, check=False
            )
            if prior.returncode != 0:
                findings.append(f"{relative.as_posix()}: cannot read committed HEAD file")
                continue
        if relative.suffix == ".py":
            try:
                findings.extend(
                    _python_discontinuities(
                        relative.as_posix(), current_source, prior.stdout if prior is not None else None
                    )
                )
            except SyntaxError as exc:
                findings.append(f"{relative.as_posix()}: cannot parse Python version constants: {exc}")
            continue
        try:
            current = json.loads(current_source)
            baseline = json.loads(prior.stdout) if prior is not None else None
        except ValueError as exc:
            findings.append(f"{relative.as_posix()}: cannot parse selected or HEAD JSON: {exc}")
            continue

        old_fields = _version_fields(baseline)
        current_fields = _version_fields(current)
        for field_path, value in current_fields.items():
            old_value = old_fields.get(field_path)
            if not isinstance(value, int) or isinstance(value, bool):
                if isinstance(old_value, int) and not isinstance(old_value, bool):
                    findings.append(
                        f"{relative.as_posix()}:{'.'.join(map(str, field_path))}: integer version became {value!r}"
                    )
                continue
            if old_value is not None and (not isinstance(old_value, int) or isinstance(old_value, bool)):
                findings.append(
                    f"{relative.as_posix()}:{'.'.join(map(str, field_path))}: HEAD version is not an integer"
                )
            elif old_value is None and value != 1:
                findings.append(
                    f"{relative.as_posix()}:{'.'.join(map(str, field_path))}: new version family must start at 1, got {value}"
                )
            elif old_value is not None and value not in {old_value, old_value + 1}:
                findings.append(
                    f"{relative.as_posix()}:{'.'.join(map(str, field_path))}: version {value} must equal HEAD {old_value} or be exactly next ({old_value + 1})"
                )
        for field_path in old_fields.keys() - current_fields.keys():
            if isinstance(old_fields[field_path], int) and not isinstance(old_fields[field_path], bool):
                findings.append(
                    f"{relative.as_posix()}:{'.'.join(map(str, field_path))}: version field removed from HEAD"
                )

        if relative.parts[-2:] == (".claude-plugin", "plugin.json") or relative.parts[-2:] == (
            ".codex-plugin",
            "plugin.json",
        ):
            if not isinstance(current, dict) or (baseline is not None and not isinstance(baseline, dict)):
                findings.append(f"{relative.as_posix()}: plugin manifest must be a JSON object")
                continue
            finding = _semver_finding(
                relative.as_posix(), current.get("version"), baseline.get("version") if baseline else None
            )
            if finding:
                findings.append(finding)
    return findings


def main(argv: list[str] | None = None) -> int:
    """Check host manifest agreement and selected file continuity from committed HEAD."""
    parser = argparse.ArgumentParser(description="Check plugin manifest sync and version continuity")
    parser.add_argument("--scan-dir", default="plugins", help="directory to scan (default: plugins)")
    parser.add_argument("files", nargs="*", help="changed shipped plugin JSON/Python paths to compare with HEAD")
    args = parser.parse_args(argv)

    desyncs = find_desyncs(Path(args.scan_dir)) + find_removed_host_manifests(Path(args.scan_dir))
    discontinuities = find_head_discontinuities(args.files)
    if desyncs or discontinuities:
        for finding in desyncs:
            print(f"VERSION-DESYNC: {finding}")
        for finding in discontinuities:
            print(f"VERSION-CONTINUITY: {finding}")
        return 1
    print("✓: plugin manifest sync and selected HEAD version continuity verified")
    return 0


if __name__ == "__main__":
    sys.exit(main())
