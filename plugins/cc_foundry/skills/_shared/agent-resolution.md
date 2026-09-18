## Agent Resolution — Cross-Plugin Dependencies

> **Plugin check**: `find ~/.claude/plugins/cache -name "<plugin>" -type d 2>/dev/null | head -1` (non-empty = installed). Uncertain: proceed as installed — common case; fall back only if agent dispatch explicitly fails.

Availability vars — set once before cross-plugin dispatch; pass via env or re-check inline:

```bash
OSS_AVAILABLE=$(find ~/.claude/plugins/cache -name "oss" -type d 2>/dev/null | head -1)  # timeout: 5000
RESEARCH_AVAILABLE=$(find ~/.claude/plugins/cache -name "research" -type d 2>/dev/null | head -1)  # timeout: 5000
CODEMAP_AVAILABLE=$(find ~/.claude/plugins/cache -name "codemap-py" -type d 2>/dev/null | head -1)  # timeout: 5000
DEVELOP_AVAILABLE=$(find ~/.claude/plugins/cache -name "develop" -type d 2>/dev/null | head -1)  # timeout: 5000
```

### OSS Plugin Absent

oss not installed → sub `oss:X` with `general-purpose`, prepend role description + `model: <model>` to spawn call:

| oss agent | Fallback | Model | Role description prefix |
| -- | -- | -- | -- |
| `oss:cicd-steward` | `general-purpose` | `sonnet` | `You are a CI/CD specialist for GitHub Actions. Diagnose failing workflows, reduce build times, pin action SHAs, configure test matrices and caching, design quality gates. NOT for ruff/mypy rule selection (use linting-expert) or PyPI release management (use shepherd).` |
| `oss:shepherd` | `general-purpose` | `opus` | `You are an OSS project shepherd. Write release notes, triage GitHub issues, prepare CHANGELOG entries, make SemVer decisions, mentor contributors. NOT for inline docstrings or README content — those belong to doc-scribe.` |

### Research Plugin Absent

research not installed → sub `research:X` with `general-purpose`, prepend role description + `model: <model>` to spawn call:

| research agent | Fallback | Model | Role description prefix |
| -- | -- | -- | -- |
| `research:scientist` | `general-purpose` | `opus` | `You are an ML research scientist. Implement methods from papers, design experiments, validate hypotheses against codebase constraints. NOT for general code refactoring or data pipeline correctness.` |
| `research:data-steward` | `general-purpose` | `sonnet` | `You are a data pipeline specialist. Detect label leakage, validate train/val/test splits, audit augmentation pipelines, manage data provenance and completeness. NOT for model training or inference code.` |

### Codemap and Develop Plugins Absent

Codemap and develop expose skills only — no agent-level fallback. Absent → skip ops requiring their skills, log:

```
"codemap plugin not installed — skipping <codemap-py:skill>"
"develop plugin not installed — skipping <develop:skill>"
```

Skills with `--team` mode: omit unavailable cross-plugin agents from roster, log per-agent skip note. Team runs with agents available.
