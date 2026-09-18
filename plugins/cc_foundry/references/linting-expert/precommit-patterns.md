<!-- Loaded by foundry:linting-expert (haiku + medium) -->

# pre-commit Configuration & Versioning (foundry:linting-expert specialized guidance)

Read only for tasks explicitly creating, editing, or auditing `.pre-commit-config.yaml`. Skip ruff-only or mypy-only tasks.

## pre-commit — enforce at commit time

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: <CURRENT> # run `pre-commit autoupdate` to set; verify version at pypi.org/project/ruff
    hooks:
      - id: ruff
        args: [--fix]
      - id: ruff-format

  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: <CURRENT> # run `pre-commit autoupdate` to set; verify version at pypi.org/project/mypy
    hooks:
      - id: mypy
        additional_dependencies: [types-requests, types-PyYAML]  # Update versions when upgrading ruff/mypy — these are pinned for reproducibility

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: <CURRENT> # run `pre-commit autoupdate` to set
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-toml
      - id: check-merge-conflict
      - id: debug-statements
      - id: check-added-large-files
        args: [--maxkb=1000]
```

```bash
pre-commit install
pre-commit run --all-files
pre-commit autoupdate      # run regularly
```

> **Tip**: pre-commit.ci auto-runs + auto-fixes hooks per PR, no local setup needed.

## Version Pinning

Apply the matching context:

**Live project config** (`.pre-commit-config.yaml` exists + in use):

- Run `pre-commit autoupdate` — fetches latest release tag for every hook
- Don't manually look up versions or use `pip install --upgrade` to determine rev
- Commit result of `pre-commit autoupdate` directly; don't modify revs it sets

**Template / starter file** (creating new config for others to copy):

- Use `<CURRENT>` as rev placeholder — NEVER real version string like `v0.5.0`
- Add autoupdate comment on same line:
  ```yaml
  rev: <CURRENT>  # run `pre-commit autoupdate` to set; verify release at the hook's repo
  ```

**New live project config** (creating `.pre-commit-config.yaml` for first time for actual use):

- Create minimal config with placeholder revs; immediately run `pre-commit autoupdate` to populate real versions
- Don't manually write version strings; autoupdate sets them correctly from start
- To update single hook: `pre-commit autoupdate --repo <repo-url>`

Run `pre-commit autoupdate` with regular dependency updates (monthly, or when upgrading other deps).

## Version Verification

After `pre-commit autoupdate`, cross-check ruff/mypy revs against pypi.org, pre-commit-hooks revs against its GitHub releases. Don't rely only on GitHub releases for ruff/mypy — pypi.org reflects published package versions. Use WebFetch when `pre-commit autoupdate` output is ambiguous (e.g. rev updated before pypi metadata).

Cache version lookups: store in session variable, reuse; avoid re-fetching same URL.

## Prohibited Patterns

- `rev: latest` — not a valid git ref; autoupdate never writes it; treat as placeholder mistake
