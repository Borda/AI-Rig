# PR Review Checklist

Five-category checklist, reviewing PRs in Python/ML/CV/AI OSS projects.

## Correctness

```markdown
[ ] Logic is correct for the stated purpose
[ ] Edge cases handled (empty input, None, boundary values)
[ ] Error handling is appropriate and messages are actionable
[ ] No unintended behavior changes to existing functionality
```

## Code Quality

```markdown
[ ] Follows existing code style (ruff passes, mypy clean)
[ ] Type annotations on all public interfaces
[ ] No new global mutable state
[ ] No bare `except:` or overly broad exception handling
[ ] No `import *` or unused imports
```

## Tests

```markdown
[ ] New functionality has tests
[ ] Bug fixes have a regression test (test that would have caught the bug)
[ ] Tests are deterministic and parametrized for edge cases
[ ] Existing tests still pass
```

## Documentation

```markdown
[ ] Public API changes have updated docstrings
[ ] CHANGELOG updated (unless purely internal)
[ ] README updated if user-facing behavior changed
[ ] Deprecation notice added if replacing old API
```

## Compatibility

```markdown
[ ] No breakage of public API without deprecation cycle
[ ] New dependencies justified and license-compatible
[ ] Python version compatibility maintained
[ ] pyproject.toml updated if new optional dep added
```
