---
name: create
description: 'Interactive outline co-creation for developer advocacy content — collects format, audience profile, story arc (Problem→Journey→Insight→Action), and voice/tone; detects out-of-scope requests (FAQs, comparison tables); surfaces conflicts between user brief and audience needs. Writes approved outline to .plans/content/<slug>-outline.md for foundry:creator to execute. Use when starting a blog post, Marp slide deck, social thread, talk abstract, lightning talk, animated SVG, or short video (HyperFrames HTML composition; render stays user-run). SKIP: idea generation before an outline exists (use foundry:brainstorm — brainstorm explores directions into a tree; create co-creates the outline, foundry:creator writes the finished artifact).'
argument-hint: '[topic]'
disable-model-invocation: true
allowed-tools: Read, Write, Bash, TaskCreate, TaskUpdate, TaskList, AskUserQuestion, Agent, Skill
effort: medium
---

<objective>

Story arc four-beat: Problem → Journey → Insight → Action.

NOT for: implementation, code gen, README writing (use `foundry:doc-scribe`), structured ref docs (FAQs, comparison tables — use `foundry:doc-scribe`), media processing of existing files (download, transcode, trim, transcribe — user runs `ffmpeg` / Whisper / `yt-dlp`).

</objective>

<inputs>

- **$ARGUMENTS**: optional — topic or goal, any form; one sentence enough. Format hints accepted ("a blog post about…", "talk abstract for…").

</inputs>

<workflow>

**Task hygiene** — task tools may be deferred; load before first use: `ToolSearch(query="select:TaskList,TaskCreate,TaskUpdate,TaskGet", max_results=4)`. Call `TaskList` first and triage each task it returns: `completed` if work clearly done, `deleted` if orphaned, keep `in_progress` only if genuinely continuing. Never spend a turn on bookkeeping alone — every `TaskCreate`/`TaskUpdate` ships in the same response as the next substantive tool call; one exception, `TaskUpdate(completed)` immediately before a long output block (`rules/task-lifecycle.md`).

**Task tracking**: TaskCreate all steps in the same response as the first real tool call — never a response of bookkeeping alone.

## Step 1 — Parse topic and out-of-scope detection

- $ARGUMENTS provided: extract topic; note embedded format hint.
- No $ARGUMENTS: AskUserQuestion — "What are you trying to write about, and for whom?" (free text). After answer, re-check against out-of-scope conditions: answer describes FAQs, comparison tables, feature matrices, README content, or docstrings: stop. Respond: "This format doesn't fit a narrative arc — use `foundry:doc-scribe` for structured reference content." No further steps.
- Out-of-scope gate (when $ARGUMENTS provided): brief describes FAQs, comparison tables, feature matrices, or ref docs: stop. Respond: "This format doesn't fit a narrative arc — use `foundry:doc-scribe` for structured reference content." No further steps.
- Media-processing gate (both inputs — $ARGUMENTS and the opener answer): brief only asks to download, transcode, trim, compress, caption or transcribe existing media, no new narrative artifact: stop. Respond: "This is media processing, not a narrative artifact — run `ffmpeg` / Whisper / `yt-dlp` directly; `/foundry:create` authors new content only." No further steps. Source media for a new artifact (own talk recording, demo clip) stays in scope: record its path in outline `## Constraints` as context.

## Step 2 — Format and audience (1 AskUserQuestion call — 2 questions; +1 follow-up call when Q1 = (d))

**Format + audience** (single AskUserQuestion call with 2 questions; AskUserQuestion allows at most 4 options per question, so the four short talk and visual formats share slot (d)):

- Q1 "What content format?": (a) blog post · (b) conference / meetup talk with Marp slide deck ★ · (c) social thread (X/LinkedIn) · (d) short talk or visual — talk abstract, lightning talk, animated SVG, short video
- Q2 "Who is the audience?": (a) beginners — new to problem space ★ · (b) intermediate — familiar with basics, seeking depth · (c) expert — know landscape, want novel insight · (d) describe your own profile

Q1 = (d) → one follow-up AskUserQuestion call, 1 question "Which format?": (a) talk abstract (CFP submission) · (b) lightning talk (5–10 min) · (c) animated SVG (looping explainer graphic, embeddable via `<img>`) · (d) short video (≤60 s, HyperFrames HTML composition). No ★ — pure user choice. All seven formats stay reachable.

After answer: restate one sentence covering format + audience ("Got it — a [format] for [audience description].").

## Step 3 — Arc construction and conflict check

Propose four-beat arc from topic + audience:

- **Problem**: concrete opening hook — specific pain or question, not generic
- **Journey**: 3–5 key points (what tried, what failed, what arc covers)
- **Insight**: core "aha" framed for stated audience level — name directly
- **Action**: specific next step for audience
- **Visual formats** (animated SVG, short video): beats map to timed segments (SVG) or scenes (video); Journey 1–3 points max; Action = closing frame/card

**Out-of-scope re-check (re-apply the Step 1 test to the constructed arc)**: the Step 1 gate tests the brief; this one tests what the arc turned out to be. If the Journey beat resolves mainly to a feature-by-feature comparison, an options matrix, a question-and-answer list, or reference material with no narrative through-line — i.e. the beats only hold together as a table or list — stop here, before writing any outline. Respond: "The arc for this topic resolves to structured reference content rather than a narrative — use `foundry:doc-scribe` instead." Do not write the outline file and do not offer the Step 4 generation gate. A topic that merely *mentions* alternatives still passes: the test is whether the Insight beat names a single transferable idea, or just summarises the comparison.

**Editorial conflict check**: brief implies expert audience but topic introductory, or vice versa: surface before continuing:

> "Your brief suggests [X] but audience profile is [Y] — recommend adjusting [Z]. Proceed as-is or adjust?"

**Arc approval + voice** (single AskUserQuestion call): derive a topic slug (kebab-case, max 5 words, per Step 4), Read `.temp/create/<slug>-arc.md` first if it exists, then Write the proposed arc (Hook · Journey · Insight · Action) there, keeping each beat to one or two lines. The full arc is printed once, rendered as normal Markdown, as the last reply text before the question call. The question text carries the arc headline, four-beat count and `→ .temp/create/<slug>-arc.md`, then asks the voice choice — option (d) redirects to arc adjustment. Keep the question text to at most 6 plain lines (target 600 characters, hard cap 1,500), with no table, no pipe layout and no code fence. Options carry labels and one-line action descriptions, no `preview`; place the recommended option second without changing labels or answer mapping.

Options:

- (a) Approve arc — neutral developer advocate (balanced, educational) ★
- (b) Approve arc — opinionated / direct first-person, no hedging
- (c) Approve arc — conversational / approachable, informal
- (d) Adjust the arc first (free text — describe what to change)

On (d): Read `.temp/create/<slug>-arc.md`, revise arc, rewrite that file, print the revised arc once immediately before re-invoking this question and refresh its plain summary. After (a)/(b)/(c): restate confirmed arc and voice in two sentences.

## Step 4 — Write outline file

- Derive slug from topic: kebab-case, max 5 words (e.g. `tracing-python-services-otel`).

- Write creates `.plans/content/` if absent — no separate mkdir needed.

- **Anti-overwrite check before writing outline**: substitute the derived slug in the guarded Bash block below, then list existing files matching `.plans/content/<slug>-outline*.md`. If `.plans/content/<slug>-outline.md` already exists, append smallest available counter suffix (`-2`, `-3`, …) per quality-gates.md output routing convention. Resulting path becomes new `<outline-path>`; use in Write call AND in Step 4 gate spawn prompt below. Print resolved path before writing.

  ```bash
  SLUG='<slug>'
  case "$SLUG" in *'<'*|*'>'*|""|*[!a-z0-9-]*) echo "! BLOCKED — slug not substituted or invalid"; exit 1;; esac
  ls -1 .plans/content/"$SLUG"-outline*.md 2>/dev/null || true
  ```

- Write `<outline-path>` with this structure:

```md
---
topic: <topic from brief>
created: YYYY-MM-DD
---

## Audience
[who they are, experience level, what they've likely seen, what they need]

## Format
[blog post | conference talk (N min) | social thread (x|linkedin) | talk abstract | lightning talk (N min) | animated svg (N s, loop|once, WxH) | short video (N s, landscape|portrait|square, fps 30|60, audio: none|music|voiceover)]

## Voice
[tone brief: e.g., "direct and opinionated, first-person, no hedging"]

## Arc

### Problem
[concrete opening hook — the pain or question]

### Journey
[key points to explore: what was tried, what failed, what the arc covers]

### Insight
[the core "aha" — what was learned or built; name it directly]

### Action
[call to action — specific, what audience should do next]

## Constraints
[length target, things to avoid, format-specific constraints; visual: palette, fonts, assets/source media paths, captions yes/no]
```

- Confirm file path to user.

- Artifact output path `<output-path>` per Step 2 format — substitute it literally into the spawn prompt before invoking `Agent()`, never pass literal `<ext>` (path on disk is never `.plans/content/<slug>.<ext>`):

  - text formats (blog post, Marp deck, social thread, talk abstract, lightning talk) → `md`: `.plans/content/<slug>.md`
  - animated SVG → `svg`: `.plans/content/<slug>.svg`
  - short video → project directory `.plans/content/<slug>-video/` (creator writes `index.html` + `compositions/*.html` inside). Skill resolves the anti-overwrite counter using the guarded listing below before scaffolding (taken → smallest free `<slug>-video-2/`, `-3/`, …); resolved path = `<video-dir>`.
  - A future format with a different extension must add its own mapping here.

- **Short video directory listing**: substitute the derived slug before running:

  ```bash
  SLUG='<slug>'
  case "$SLUG" in *'<'*|*'>'*|""|*[!a-z0-9-]*) echo "! BLOCKED — slug not substituted or invalid"; exit 1;; esac
  ls -d .plans/content/"$SLUG"-video*/ 2>/dev/null || true
  ```

- **Short video prerequisites**: Node ≥22 with npm and npx installed by the user, plus network access to the npm registry and scaffold dependency sources. HyperFrames uses deliberate latest-tracking (`npx hyperframes@latest init`) so new projects receive the current scaffold; repeat runs may use a different CLI version. Before the generation gate, disclose that generation will download and execute the latest npm package and install project dependencies under `<video-dir>`; include this effect in option (a)'s description. If network access is unavailable, retain the outline and print the resume commands; never retry with broader permissions. Existing project scripts retain the CLI version selected at scaffold time.

- **Short video prerequisite probe** (short video only, main thread, before the generation gate; read-only), one block each:

  ```bash
  node --version  # timeout: 5000
  ```

  ```bash
  npm --version  # timeout: 5000
  ```

  ```bash
  npx --version  # timeout: 5000
  ```

  ```bash
  ffmpeg -version  # timeout: 5000
  ```

  - Node missing or major < 22 → print `**! MISSING — short video needs Node ≥22**`, keep the outline, print the resume lines below, end at (b) **Stop here** — no question asked.
  - npm or npx missing → print `**! MISSING — short video needs npm and npx**`, keep the outline, print the resume lines below, end at (b) **Stop here** — no question asked; the user installs prerequisites.
  - ffmpeg missing → warn only (`⚠ ffmpeg missing — needed later for the user-run render`); authoring proceeds.
  - **Resume lines** (a prerequisite missing, or user picks (b)): print a standalone fenced Bash block containing the scaffold commands below, wrapped in a subshell `( ... )`, including the `npx hyperframes@latest init` line, telemetry opt-out and vendor-instruction renames, with `<video-dir>` and resolution substituted with their resolved values. Tell the user the subshell keeps a failed command from exiting their interactive shell; run that block after installing prerequisites, then print `@foundry:creator <outline-path>` with the actual outline path substituted — creator defaults to `<video-dir>` when it holds a scaffolded `index.html`. Do not execute the resume commands.

- **Short video scaffold** — runs only after the user picks (a) at the generation gate (never before consent), as its own response; on success, spawn creator in the next response; non-zero exit → stop, report, keep outline, no spawn. Substitute `<video-dir>` and the outline's preset word. Telemetry off via env opt-out; `--skip-skills` (never install skills, never sign in). Vendor `CLAUDE.md`/`AGENTS.md` that `init` copies into the project are renamed to non-instruction `.txt` files so no agent loads them as instructions:

  ```bash
  (
  VIDEO_DIR='<video-dir>'; RES='<resolution>'
  case "$VIDEO_DIR" in *'<'*'>'*|"") echo "! BLOCKED — <video-dir> not substituted"; exit 1;; .plans/content/*-video*) ;; *) echo "! BLOCKED — video dir must be under .plans/content/"; exit 1;; esac
  case "$RES" in landscape|portrait|square) ;; *) echo "! BLOCKED — resolution must be landscape|portrait|square"; exit 1;; esac
  HYPERFRAMES_NO_TELEMETRY=1 DO_NOT_TRACK=1 npx hyperframes@latest init "$VIDEO_DIR" --skip-skills --non-interactive --resolution "$RES" || exit 1  # timeout: 180000
  for F in CLAUDE.md AGENTS.md; do [ -f "$VIDEO_DIR/$F" ] && mv "$VIDEO_DIR/$F" "$VIDEO_DIR/HYPERFRAMES-$F.txt"; done; true
  )
  ```

> Creator spawn prompt must require an envelope even on exhaustion — `partial: true` plus what was finished. Extend the envelope contract in every creator spawn with `notes` (Confidence justification, gaps, assumptions and arc divergence), `reason` for blocked/unsupported outcomes, and `needs_decision`, `question`, `options` when user input is required. `file` and `confidence` apply only when an artifact and an assessed score exist; never fabricate them on a stop.

- End with `AskUserQuestion` gate, two options: (a) **Generate the full artifact now** — for short video, download and execute the latest npm package and install project dependencies under `<video-dir>`, then spawn `foundry:creator` via `Agent(subagent_type='foundry:creator', name='creator-<slug>', description='write <format> artifact', prompt='Read <outline-path> and generate the complete <format> artifact. Output file path: <output-path>. Return ONLY a JSON envelope: status, file (path actually written, counter suffix included; omit when none), partial (true on exhaustion, plus what was finished), confidence (your assessed Confidence score, when available), notes (Confidence justification, gaps, assumptions and arc divergence). For needs_decision, include needs_decision: true, question and options; do not write the artifact until the caller supplies the answer. For blocked or unsupported, include reason and any owner/redirect, with no fabricated file or score.')` where `<outline-path>` is resolved path from anti-overwrite step above, and `<slug>`, `<format>`, `<output-path>` substituted from generated outline (`<output-path>` per the output path mapping above — file, or `<video-dir>` for short video) before the call — never pass literal angle-bracket placeholders to spawned agent. Short video: append to the prompt "Project scaffolded by `hyperframes init` at <video-dir>; Read its index.html first, keep its head/script tags and take width/height from it. Envelope also carries `assets`: list of {dest: assets/<name>, src: <source path>} for every media file the composition references." (b) **Stop here** — I'll invoke `foundry:creator` manually when ready (short video: print the resume lines above).

  **Substitution verification before issuing Agent call (mandatory)**:

  1. Construct final prompt, `name` and `description` strings with all placeholders replaced
  2. Scan all three constructed strings for remaining `<` or `>` characters; either present means substitution incomplete — resolve missing value(s) before spawning
  3. Confirm outline file path in prompt matches `<outline-path>` exactly (resolved path including any counter suffix, not a guess)

  Short video exception: on (a), run the scaffold block first as its own response; the Agent() call goes in the next response, only after the scaffold succeeded.

  If user selects (a), issue Agent() call in same response turn AFTER verification above passes. Do not narrate intent — call the tool. Same response: Read `.temp/create/agent-watch-creator.tsv` first if it exists, then Write it = `creator-<slug>\t-\t900` (`_shared/agent-spawn-protocol.md` §Deadlines) — envelope-only row: creator's anti-overwrite may write `<slug>-2.md`, so a fixed deliverable path could match a stale prior artifact and mark `done` at once. End the turn — never `ScheduleWakeup`, `ListAgents`, `Monitor`, a filler call, a "waiting" line, or a sleep. On its notification run `python "${CLAUDE_PLUGIN_ROOT}/bin/agent_watch.py" --state-dir ".temp/create"` once; notification carried the envelope → apply Creator outcome routing below; no envelope → report the artifact as ⏱ `timed_out`.

- **Creator outcome routing** — after the notification and deadline check, inspect the envelope status before reporting success. `needs_decision`: invoke `AskUserQuestion` with its `question` and `options`, then re-spawn the creator with the same resolved paths, the returned decision and the original brief; re-arm its 900-second envelope-only watch and await its notification. Never infer an answer or repeat a completed video scaffold. `blocked` or `unsupported`: preserve and report its `reason` and owner/redirect when supplied, then stop generation without success next steps. `partial: true`: report the finished work and remaining scope, preserve its file path when supplied, and skip humanizer and success next steps. A malformed/missing envelope remains unverified or timed out; a notification alone never proves artifact completion.

- **Optional prose polish** — after a `done` envelope with `partial: false`, an existing prose artifact and no unresolved decision, offer an optional `/foundry:humanizer <file>` pass alongside the format next steps. Invoke `Skill(skill="foundry:humanizer", args="<file>")` only when the user chooses that pass; substitute the envelope's actual counter-suffixed file path. Eligible artifacts are Markdown blog posts, Marp decks, social threads, talk abstracts and lightning talks; preserve ordinary frontmatter, Marp separators/directives and code fences. Skip SVG, short-video HTML, machine envelopes, `.reports/` files and report-schema frontmatter. If the humanizer skill is unavailable, report the optional pass as unavailable and retain the completed artifact.

- After a complete `done` envelope, print user-run next steps for the format — never run them:

  - animated SVG: open the file in a browser to preview; embed via `<img src>` (SMIL plays; scripts never run).
  - short video, from inside `<video-dir>`, POSIX shell (PowerShell: set both env vars first): one `mkdir -p assets && cp <src> <dest>` line per envelope `assets` entry → `HYPERFRAMES_NO_TELEMETRY=1 DO_NOT_TRACK=1 npm run check` (fix every error) → `HYPERFRAMES_NO_TELEMETRY=1 DO_NOT_TRACK=1 npm run dev` (preview) → `HYPERFRAMES_NO_TELEMETRY=1 DO_NOT_TRACK=1 npm run render -- --quality delivery --fps <fps> -o <slug>.mp4`. npm scripts pin the CLI version `init` used, so check and render match. Optional ffmpeg post-steps, one example each: social GIF `ffmpeg -i <slug>.mp4 -vf "fps=12,scale=640:-1" <slug>.gif`; WebM `ffmpeg -i <slug>.mp4 -c:v libvpx-vp9 -crf 34 -b:v 0 <slug>.webm`; compress `ffmpeg -i <slug>.mp4 -c:v libx264 -crf 28 -preset slow <slug>-small.mp4`. Never print or run `npx hyperframes events`, `feedback`, `skills`, `npm run publish`, or cloud render.

- End with `## Confidence` block per quality-gates.md protocol, score based on outline coverage of topic, arc, audience.

</workflow>

<notes>

- **Execution model**: `disable-model-invocation: true` — Claude itself follows this SKILL.md as workflow template directly in main context (no autonomous sub-agent dispatch during outline phase). Step 4 gate selects (a): exactly one sub-agent spawned, `foundry:creator` (executes outline, writes full artifact). No other sub-agent invocations by this skill — short video included: still exactly one sub-agent; the scaffold runs in main thread.

- Question budget: **3 `AskUserQuestion` calls / 4 questions** when a topic is supplied (step 2 = 1 call carrying 2 questions; steps 3 and 4 = 1 question each). Step 2 format follow-up (Q1 = (d)) adds 1 call / 1 question. Short video with Node missing skips the step 4 gate → 1 call fewer. Topic absent adds step 1's opener → 4 calls / 5 questions. Each arc-conflict round in step 3 re-invokes that call → up to 7 calls / 8 questions worst case.

- Each AskUserQuestion uses lettered options with one ★ recommended default, except the step 2 format follow-up (pure user choice, no ★).

- After each answer, restate understanding in 1–2 sentences before proceeding.

- Never silently adjust arc to match audience — always surface conflicts explicitly (Step 3).

- Refuse FAQs / comparison tables / ref docs at Step 1 gate; name `foundry:doc-scribe` as redirect.

- Write outline exactly once after approval — no second draft unless user requests.

- `foundry:creator` reads output outline file, generates full artifact autonomously.

- Outline spec files written to `.plans/content/` — `.plans/` is manual lifecycle, never auto-deleted (`artifact-lifecycle.md`).

- Media ingest (Whisper transcript of own talk, `yt-dlp` for own or licensed source video, `ffmpeg` cuts) = user-run prep; outputs referenced as paths in outline `## Constraints`. Respect source rights and ToS. Never upload user media.

- Render, preview, check and cloud features stay user-run; skill runs only the `hyperframes init` scaffold, after generation consent, with `--skip-skills` and telemetry disabled by env opt-out (`HYPERFRAMES_NO_TELEMETRY=1 DO_NOT_TRACK=1`), and renames the vendor `CLAUDE.md`/`AGENTS.md` it copies. Never `events`, `feedback`, skills install, sign-in, `publish` or cloud render.

</notes>
