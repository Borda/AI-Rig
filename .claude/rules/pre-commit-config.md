---
description: pre-commit configuration version pinning rules
paths:
  - .pre-commit-config.yaml
---

## Version Pinning

- Always run `pre-commit autoupdate` before pinning any hook revision
- Check current versions at pypi.org for ruff, mypy, and pre-commit-hooks before committing

## Hook Rev Placeholders

In templates, use `<CURRENT>` as a placeholder and add a comment:

```yaml
rev: <CURRENT>  # run `pre-commit autoupdate` to set; verify at https://pypi.org/project/ruff
```

Never hardcode a specific version without first verifying it is the latest.
