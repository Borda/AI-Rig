<!-- file: premise-grounding.md — consumers: feature/SKILL.md, fix/SKILL.md, refactor/SKILL.md, debug/SKILL.md -->

## Premise Grounding Gate

Every assumption, hypothesis, constraint claim, or stated fact used as **pillar for next step must be grounded in evidence** read now — from source. Gate fires before any design, implementation, plan generation, hypothesis presentation.

**Scan [SCAN_SOURCE] for ungrounded premises** — any of:

- Technical constraint claims: "X cannot do Y", "not supported by", "requires workaround because", "guard needed for"
- Feasibility assumptions: "this approach will work because", "X is fast/slow/reliable enough"
- Recalled facts: anything stated from memory, training knowledge, prior experience, or analogy — "I know that X does Y", "typically Z", "this library usually"
- Behavioral assumptions: "this function returns", "this API expects", "this version changed"

For each premise found:

1. List: `PREMISE: <claim>`
2. Classify source needed — see Evidence Authority below
3. Read that source now; confirm claim matches
4. If unverified: STOP — invoke `AskUserQuestion`: "Premise `<claim>` has no verified source. Options:"
   - (a) Provide authoritative source (link or file path)
   - (b) Premise is false — revise design/hypothesis to remove it
   - (c) Run experimental validation (write minimal test/script that proves or refutes it)
   - (d) Accept as unverified risk and proceed

Never build on a premise failing step 3. Memory or training knowledge never evidence — [CONSEQUENCE] if premise false.

## Evidence Authority

Not all sources equal. Weak sources need corroboration or experimental confirmation before a premise based on them is treated as fact.

**Tier 1 — Authoritative (sufficient alone)**

- Official documentation (versioned, from project/library/standard itself)
- Source code read from disk (actual file, actual build config)
- Official release notes, changelogs, migration guides
- Specification or RFC from governing body
- Test suite output — empirical, reproducible, observed this session

**Tier 2 — Weak (requires ≥3 independent sources OR experimental validation)**

- Blog posts, tutorials, personal sites
- Stack Overflow answers, forum posts
- Social media posts (tweets, Mastodon, Reddit threads)
- Third-party summaries or comparisons
- Training knowledge / memory ("I recall that…", "I learned that…", "typically…")
- Any source not signed or maintained by project/standard author

When only Tier 2 sources available: find ≥3 genuinely independent corroborating sources, OR write minimal experiment (script, test, REPL invocation) empirically confirming or refuting premise. Document which path taken.

**Independence requirement**: sources independent only when derived from different authors, different primary research. N posts all referencing same blog post = 1 source, not N. Count distinct origin nodes, not surface citations.

**Citation tracing — mandatory before counting sources**:

1. For each Tier 2 source: follow its citations one level deep
2. Map: `source → cites → origin`
3. Singleton detection: sources sharing an origin → merge into one; count distinct origins only
4. Tier upgrade: tracing reveals a Tier 1 source (official doc, spec, changelog) cited by a Tier 2 source but not found directly → read it; confirms claim → premise becomes Tier 1 verified (sufficient alone)
5. After tracing, distinct-origin count < 3 and no Tier 1 found → require experimental validation

### Skill contexts (substitute when calling this protocol)

**feature**:

- `[SCAN_SOURCE]` = scope analysis, feature description, and any API/library assumptions in proposed approach
- `[CONSEQUENCE]` = feature ships built on wrong design

**fix**:

- `[SCAN_SOURCE]` = root cause analysis, proposed fix approach, and any behavioral assumptions about failing code path
- `[CONSEQUENCE]` = fix ships wrong code

**refactor**:

- `[SCAN_SOURCE]` = goal statement, sw-engineer analysis output, and any assumptions about current code behavior or caller impact
- `[CONSEQUENCE]` = unnecessary or wrong structural change lands in codebase

**debug**:

- `[SCAN_SOURCE]` = root cause hypothesis and all supporting evidence claims
- `[CONSEQUENCE]` = fix addresses wrong mechanism, symptom returns
