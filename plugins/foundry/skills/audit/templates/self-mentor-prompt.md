**Re: Compress natural language in checklist to caveman format**

Each invocation ask self-mentor to check:

- **Purpose and logical coherence**: role clearly defined? Scope right — not too broad, not too narrow? New user know when to reach for it vs similar one?
- **Structural completeness**: required sections present, tags balanced, step numbering sequential
- **Cross-reference validity**: every agent/skill name mentioned must exist on disk. Cross-reference against Step 2 inventory. Any name not in Step 2 inventory = **broken cross-reference** (critical). No conditional language ("if X doesn't exist") — by Step 3, inventory known. If inventory not collected (e.g., running in isolation), flag: "unverified reference — requires disk inventory check." **Antipattern to flag**: writing "potentially missing" / "likely doesn't exist" / "if this agent doesn't exist" / "pending verification" / "should be checked against inventory" when Step 2 ran. These phrases = agent not using inventory. Name in workflow, absent from Step 2 list = confirmed broken cross-reference — report critical, not conditional. Conditional language only acceptable when Step 2 genuinely not run.
- **Verbosity and duplication**: bloated steps, repeated instructions, copy-paste between files
- **Content freshness**: outdated model names, stale version pins, deprecated API references
- **Hardcoded user paths**: any `$HOME/` or `/home/<name>/` absolute path — must be `.claude/`, `~/`, or derived from `git rev-parse --show-toplevel`. Flag every occurrence regardless of context — paths in "negative example" notes, "do not do this" callouts, or instructional text not exempt. Literal path string appears = flag at medium severity.
- **Infinite loops**: file A follow-up chain reference file B which references A = cycle? (flag, don't auto-fix)
- **Example value vs. token cost**: for each inline example (code block or `## Example` section), judge whether earns tokens — demonstrates non-obvious pattern or nuanced judgment call prose alone cannot convey? Flag examples that restate surrounding prose in code, illustrate obvious/trivial cases, or better served by project-local `AGENTS.md`. Note: if project has own `AGENTS.md` or `CONTRIBUTING.md`, generic examples in agent files less justified.

**Scope constraint**: report only findings within above checklist. No out-of-scope findings (e.g., "no error handling described," "missing inputs section for a skill") unless that specific check in list. Extra findings = noise — dilute precision, distract from confirmed issues.
