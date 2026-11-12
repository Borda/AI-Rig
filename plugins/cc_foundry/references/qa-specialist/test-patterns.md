## Parametrized Tests

Apply **Parametrization**: one `pytest.param(..., id=...)` per case and a comment naming the edge it covers (all-zero input → zero, not nan; single element). Float results via `pytest.approx(expected, abs=...)`. For Test Structure style rules, run `cat ~/.claude/rules/foundry-python-testing.md` via Bash; if absent, run `cat "${CLAUDE_PLUGIN_ROOT}/rules/python-testing.md"`.

## Error Path Testing

Assert the exception type **and** a `match=` on the message (`pytest.raises(ValueError, match="must be positive")`). Deprecations: `pytest.warns(DeprecationWarning, match=...)`, then assert the old and new call return the same result.

## Doctest Patterns

| Situation | Solution |
| -- | -- |
| Optional dep missing | `# doctest: +REQUIRES(module:torch)` via pytest-doctestplus plugin (PyPI: pytest-doctestplus) |
| Abstraction not public yet | `__doctest_skip__ = ["ClassName.method"]` at module level |

```toml
# pyproject.toml
addopts = ["--doctest-modules", "--doctest-plus"]
```

## Integration Test with Real Dependencies

Integration tests cover full roundtrip (create, persist, retrieve) and verify side effects — not just happy-path return value.

## Fixture Design

Fixtures return minimal valid object needed for test scope — only fields test actually exercises, nothing more.
