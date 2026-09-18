# Shepherd Voice — Tone and Contributor-Facing Output Templates

Scope: GitHub issue/PR comments, release notes, CHANGELOG entries, contributor-facing replies. Other agents producing such text route through here. Out of scope: inline docstrings (foundry:doc-scribe), commit messages (see `git-commit.md` — same `#N`/`@name` scoping rule below, enforced there for commit bodies), internal notes.

### GitHub Reference Scoping — mandatory before any draft is shown as ready-to-post

<!-- policy-sibling: plugins/CLAUDE.md (canonical), plugins/cc_foundry/rules/git-commit.md, plugins/cc_foundry/rules/_full/git-commit.md — same GH #/@ scoping policy restated for this consumer's context. Edit canonical first, then grep "policy-sibling" repo-wide to update every copy in lockstep. -->

`#N` in this draft = **only** a real GitHub issue/PR/discussion number already confirmed in context (e.g. the PR under review, an issue actually linked from the thread). `@name` = **only** a real GitHub username actually party to this thread (author, reviewer, mentioned contributor).

Before finalizing any draft under this file's scope: scan for bare `#N` / `@name`, fix each.

- `#N` referring to a local ordinal (comment index, item number, list position, rank), not a real GH issue/PR → drop the `#`, or use `No.N` / an ordinal word (`1st`, `item 3`)
- `#N` referring to an issue/PR/discussion in a **different repo** than the one this draft posts to → bare `#N` resolves against the target repo only; use the full URL (`https://github.com/<owner>/<repo>/issues/N`) instead, never `#N` or `owner/repo#N`
- `@name` that is not a real GitHub handle in this thread (a role label, a tool/package name, a decorator-like token) → drop the `@`
- `@handle` that IS a real GitHub user but intent is genuinely uncertain (naming them in passing inside an internal note not addressed to them, e.g. "as suggested by X" in an analysis report) → drop the `@`, write the handle bare — never backtick-wrap it, backticks are for code-shaped tokens only, not for disguising a person's handle
- Genuine same-repo GH issue/PR refs, and genuine contributor `@handle` mentions where a live ping is actually intended, stay as-is — correct, intended use. Release-note and CHANGELOG contributor credit is standard, *deliberate* ping-intent (crediting them is the point) — leave it live, don't neutralize it

Reason: this draft is markdown headed straight for a live GitHub comment/issue/PR/release — `#N` and `@name` render as clickable links/notifications there. A false one cross-links the wrong issue (or the wrong repo), or pings someone who wasn't meant to be notified.

### Humanizer Pass — companion, not a substitute

Before any draft under this file's scope is shown as ready-to-post: run a `foundry:humanizer` pass (requires `foundry` plugin) after applying Shared Voice below, not instead of it. Shepherd Voice sets tone and structure; humanizer strips statistical AI-writing tells (banned vocabulary, formatting tics) voice rules alone don't catch. `foundry` plugin absent → skip the pass, post with Shared Voice alone — don't reconstruct the checklist from memory; an unverified imitation is exactly the training-knowledge-as-evidence substitution `quality-gates.md` §Evidence Grounding forbids.

### Shared Voice

Tone: dev talking to dev — peer-to-peer, polite, warm, constructive. Not gatekeeper judging submissions; collaborator helping get work across line. Warm but direct. Prefers enabling over doing.

- **Acknowledge before critiquing**: open with genuine specific observation — `nice approach here` / `solid fix` — not performative (`thanks for your contribution!`); then move to feedback
- **"I" not "you"**: `I find this hard to follow` not `you wrote confusing code` — feedback on code, not person
- **Terse**: short phrases, no preamble, jump straight to point
- **Suggest, don't command**: frame alternatives as options anchored to known-good pattern — `see sklearn`, `similar to X above` — not directives
- **Questions for intent**: `is line break really needed?` / `thoughts?` — interrogative when uncertain, imperative for obvious fixes (`put it on a new line`)
- **Why in one sentence**: `introducing one more for loop instead of triple commands would make this much more readable`
- **PR as mentoring**: beyond immediate fix, briefly name broader principle or pattern — `we generally avoid this because...` / `the convention here is X — helps with Y`. Light overlap into adjacent code fine when same pattern recurs nearby; stop there, don't expand into separate review
- **Declining — four steps**: (1) acknowledge effort genuinely, (2) explain why, (3) point to alternatives if any, (4) close decisively — `thanks for this; it adds complexity outside our core scope, so I'm closing — could work well as a standalone plugin though`
- **Length**: inline comment = 1-2 sentences; issue reply = 2-4 sentences; release note item = 1 line
- **Emoji sparingly**: 😺 🐰 🚩 — occasional, never performative

**Phrases to avoid:**

| Avoid | Use instead |
| -- | -- |
| "Thank you for your contribution!" (generic) | name specific thing: `good approach here` / `solid fix` |
| "Could you please provide a reproduction?" | "can you paste the traceback?" / "what does your setup look like?" / "which version?" |
| "It would be great if you could..." | state directly: `can you add X?` |
| "This may potentially cause issues." | "this breaks X when Y" |
| "You need to fix X, Y, and Z before this can be merged." | "N things need sorting before I merge" + prose per item |
| Closing without explaining resolution | say what was fixed and how: `fixed in #123 by doing X — can you check if it works for you?` |

Use contractions. Short sentences. State opinions directly.

**Apology for late reaction optional** — measure time since last activity: skip if < 1 week; judgment call at 1–3 weeks (omit for active threads); include if ≥ 4 weeks.

When included, vary phrasing: "apologies for not getting back sooner" / "apologies for the delayed follow-up" / "apologies for the slow response" / "apologies for letting this PR sit without review".

**`[blocking]`/`[suggestion]`/`[nit]` annotation prefixes for internal review reports only** — never in contributor-facing output. Severity communicated through structure (ordering, scope line count), not labels.

> Scope: annotation prefixes apply to PR review checklists and internal analysis only. See `<antipatterns-to-flag>` for enforcement.

### PR Replies — structural divergences

*Shared voice applies. Format and mandatory elements only.*

Two parts. Part 1 = Reply summary — always present, always information-complete on its own. Part 2 = Inline suggestions — optional, adds location-specific detail.

**PART 1 — Reply summary** (always present, always complete, honest on its own):

1. **Acknowledgement + Praise** — `@handle` + warm specific opening; name what's genuinely good: technique, structural decision, test strategy, API choice — concrete, not generic ("great PR!"). 1–3 observations.
2. **Areas needing improvement** — thematic, no counts, no itemisation, no "see below". Name concern areas concretely enough contributor knows what to look at without needing Part 2 (e.g. "error handling in `_run_tracker_on_detections` needs guard against empty detection files, and direct unit tests for that function are missing"). Omit entirely only when verdict is true LGTM.
3. **Optional intro sentence** — only when Part 2 follows: e.g. `"I've left inline suggestions with specifics."` — omit if no Part 2.

**PART 2 — Inline suggestions** (MANDATORY whenever ≥1 finding references a specific file:line; omit ONLY for true LGTM. Post as individual diff comments or follow-up block):

One unified table — all findings in single place, no separate prose:

```markdown
| Importance | Confidence | File | Line | Comment |
|------------|------------|------|------|---------|
| high | 0.95 | `src/foo/bar.py` | 42 | what's wrong and concrete fix — 1-2 sentences for high items since there is no prose paragraph |
| medium | 0.80 | `src/foo/bar.py` | 87 | one-sentence observation + suggestion |
| low | 0.70 | `src/foo/bar.py` | 101 | nit or minor style note |
```

- **Importance** values: `high`, `medium`, `low`
- **Confidence** (0.0–1.0): certainty of finding based on evidence in diff
- **Column order**: Importance and Confidence are two leftmost columns, most decision-relevant
- **Row ordering**: high → medium → low importance; within same tier, sort by Confidence descending
- **Comment length**: 1-2 sentences per row; high-importance rows may use 2 sentences since no separate prose paragraph
- **Use full GitHub Markdown** throughout: code spans, fenced blocks, `> blockquotes` for cited excerpts, inline links where helpful

**When to produce both parts**: any request to write contributor reply, review summary for contributor, or `--reply` output from `/oss:review`. Produce Reply summary (Part 1) alone ONLY when no specific line-level issues (e.g., simple "LGTM"). Otherwise Part 2 table mandatory: any finding naming file:line MUST be table row, never embedded in Part 1 prose (Part 1 stays thematic per line 50).

### Issue Replies — structural divergences

*Shared voice applies. Format and mandatory elements only.*

One comment, no inline table.

**Comment structure** (5 parts, 20–90 words total; go longer only when issue has multiple root causes, affects several commenters, or needs migration path explained — every extra sentence earns its place):

```markdown
1. GREETING + @MENTION          "Hi @username —"
2. APOLOGY (optional)            See threshold below — omit for recent activity
3. CONTEXT (1–2 sentences)      What you found, what changed, or what you understand
4. ACTION(S) (1–2 sentences)    One directive or a short sequence — keep sequences high-level, not step-by-step
5. ENDING (scenario-dependent)  See variants below
```

Optional inserts between 4 and 5: tag bystanders (@mention others who reported same), thank contributors by name, redirect to another repo, note relabel.

**Step 5 ending variants:**

| Scenario | Ending |
| -- | -- |
| Closing (fixed / stale / external / superseded) | "Closing — please reopen if [specific condition]." |
| Needs more info (keep open) | No explicit close — ask in step 4 is ending; thread stays open |
| PR guidance (keep open) | "Fix those N and you're good to merge." / "LGTM once CI is green." |
| Triaging / relabeling (keep open) | "Labeling as [label]." / "Relabeling as enhancement — contributions welcome!" |
| Answering a question — fully resolved | "Closing — feel free to reopen if you have follow-up questions." |
| Answering a question — discussion expected | "Let me know if that helps." (leave open) |

**Close-scenario archetypes (A–G):**

- **A. Fixed in release** — Hi @user — apologies for not closing this out sooner. Fixed in #NNN (vX.Y.Z). Please upgrade (`pip install pkg --upgrade`). Closing as fixed.

- **B. Fixed on develop** — Hi @user — apologies for delayed follow-up. Root cause — [brief explanation] — fixed on `develop` (#NNN), ships in next release. Can install from `develop` to test. Closing — please reopen if it persists on next release.

- **C. Superseded by architecture change** — Hi @user — apologies for slow response. [OldThing] replaced by [NewThing] in vX.Y.Z with rewritten [subsystem]. Please upgrade and use [NewAPI]. Closing — please reopen if issues on current version.

- **D. External / wrong repo** — acknowledge, redirect to [other-repo], close with reopen offer if library-side issue surfaces.

- **E. Self-resolved / stale** — confirm root cause in one clause, note related improvement in vX.Y.Z, close as self-resolved, thank helpers by @mention.

- **F. Keep open + relabel** — acknowledge problem is real, note vX.Y.Z partial improvement, relabel as enhancement, invite contributions.

- **G. Superseded PR** — name replacement approach (#NNN) and explain subsystem was rewritten, thank contributor by @handle.

**Non-close replies** — intent-based structure:

- **Needs info**: confirm what you understand in one sentence → name single most important gap → ask one question needed. Don't pile multiple questions.
- **Confirmed / triaged**: state diagnosis in one sentence → set expectation (label, milestone, or "fixing in X") → close with next action.
- **Answering a question**: direct answer first, context second, 2–4 sentences max.

Use code spans/blocks for tracebacks, commands, config snippets. Avoid headers in short replies, prose reads faster than structured sections.

### Discussion Replies — structural divergences

*Shared voice applies. Format and mandatory elements only.*

One comment, conversational tone, no inline table. Discussions = design-space conversations, reply is position, not verdict.

1. Engage with specific point raised (quote sparingly with `>` if thread is long)
2. State position or answer directly, don't hedge before giving it
3. Add context, caveats, or trade-offs only if they change picture
4. Close with invitation for follow-up if genuinely open (`thoughts?` / `does that address your concern?`) — omit if answer is clear-cut

Can be longer than issue replies when topic warrants (3–5 sentences or short bullet list for multi-part questions). Use fenced code blocks for design sketches or API examples.
