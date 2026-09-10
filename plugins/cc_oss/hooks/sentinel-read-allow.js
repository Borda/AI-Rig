#!/usr/bin/env node
// sentinel-read-allow.js — PreToolUse hook (matcher: Bash)
//
// PURPOSE
//   Plugin skills persist cross-block state via TMPDIR sentinel files read with
//   `VAR=$(cat "${TMPDIR:-/tmp}/<name>-${CSID}")`. The `$(...)` command
//   substitution makes settings.json prefix allow-rules fail-closed, so every
//   such command — written verbatim in versioned plugin MD files — raises a
//   "Contains expansion" permission prompt in subagents. This hook auto-allows
//   exactly that pre-canned blueprint idiom; everything else passes through
//   (exit 0, no output) to normal permission + deny checking. Custom or
//   on-the-fly code therefore stays gated.
//
// SECURITY MODEL — why the allow is safe
//   Unlike rtk-rewrite.js we emit NO updatedInput: the ORIGINAL command string
//   is allowed unchanged, so settings.json deny rules keep matching it (deny is
//   evaluated regardless of a hook allow decision). The allow fires only when
//   ALL of the following are proven:
//
//     1. ≥1 blueprint ANCHOR is present: a substitution matching a blueprint
//        shape — the sentinel read (path starts with the literal
//        `${TMPDIR:-/tmp}/`) or the timestamp idiom `$(date [-u] +FORMAT)` — or
//        the substitution-free rewritten idiom `IFS= read -r VAR < "${TMPDIR:-
//        /tmp}/…"` (READ_FORM). The read form needs the hook too: prefix
//        allow-rules match on the first token, and its first token is the
//        `IFS=` assignment, so no allow entry can ever match it.
//        UNQUOTED paths/defaults use a strict filename
//        charset (FNCHAR) that excludes EVERY shell metacharacter, so an
//        unquoted span can never carry `;`/`>`/`&`/`|`/`<` that the shell would
//        act on (`$(cat ${TMPDIR:-/tmp}/x;rm y)` is rejected). Quoted paths keep
//        their bytes literal (content between `"` is data), and both forbid `$(`,
//        backticks, and backslashes and confine `${...}` to plain parameter
//        expansions — so no nested command substitution can hide in a span.
//     2. No `..` traversal anywhere — matched as a path COMPONENT (TRAVERSAL),
//        not as a bare substring, so `...` and `v1.2..v1.3` stop counting as
//        traversal while `../x`, `/../`, `a/..`, bare `..` and `${V:-..}` all
//        still reject. After removing safe spans, NO other `$(`, backtick,
//        `<(`, `>(`, or heredoc remains.
//     3. Quoted regions are masked by a state-machine scanner (handles \" and
//        '...'), so quote tricks cannot smuggle a separator past segmentation.
//     4. No loader/lookup-poisoning assignment (PATH, LD_PRELOAD, IFS≠empty, …).
//     5. The only WRITE-capable redirects reject: stderr-silence / stdout-to-null
//        (`2>/dev/null`, `2>&1`, `N>/dev/null`, `N>&M`) are stripped, then any
//        remaining `>` rejects. Input `<` is allowed — the `read < file` idiom
//        needs it and it is no more capable than a whitelisted token reading the
//        same path as an argument (`Bash(cat:*)` already reads any file).
//     6. Every segment (split on newline ; & | && ||), after skipping whole-line
//        comments and stripping leading VAR=... assignments, starts with a
//        strictly non-writing whitelisted token (see SAFE_TOKENS —
//        find/mkdir/touch/sort/jq/date deliberately excluded because segment
//        validation does not inspect arguments). Guarded CLIs (git, gh, rm,
//        curl, ...) are NOT whitelisted → passthrough. The comment skip cannot
//        hide a live command: segmentation splits on `;|&` as well as newline,
//        so `# note && rm x` still validates `rm x`, and a `#` inside quotes is
//        already masked to `Q` before this runs.
//
//   False negatives (odd-but-safe commands passing through to a prompt) are
//   acceptable; false positives (allowing a mutation, a write, a spawned
//   process, or lookup-path hijack) are not — every ambiguity resolves toward
//   passthrough. ACCEPTED residual: a whitelisted read-only token can disclose
//   an arbitrary readable file it is given as an operand (incl. via an unquoted
//   `${VAR}` that word-splits) — non-escalating, since `Bash(cat:*)` et al.
//   already read any file promptless. The TRAVERSAL guard is defence-in-depth
//   over that residual, not the control itself, and it only sees LITERAL text:
//   a `..` the shell materialises at runtime is invisible to it — `$'\056\056'`
//   (ANSI-C quoting), `.?` / `.[.]` (globs that match the `..` entry), `."."`
//   (quote-split), `D=.; $D$D`. All five are allowed, all five were equally
//   allowed by the previous `includes("..")` form (verified against HEAD), and
//   all five land inside the read-only residual above. Closing them needs shell
//   expansion semantics this hook deliberately does not implement.
//   Reviewed adversarially (Codex) 2026-07-22, two passes; all confirmed
//   bypasses closed. Re-reviewed 2026-08-18 for the comment-skip and TRAVERSAL
//   changes; the traversal PoCs found then (`cat {a,../etc/passwd}` et al.,
//   from an allow-list neighbour class) are closed and pinned by regression
//   tests.
//
// EXIT CODES
//   0  always — passthrough (no output) or allow (JSON to stdout)

"use strict";

// ── Sentinel-read shape ───────────────────────────────────────────────────────
// ${VAR} / ${VAR:-plain-default} — no nested `$(`/backtick/backslash/parens.
const PE = "\\$\\{[A-Za-z_]\\w*(?::-[^}$`()\\\\]*)?\\}";
// $VAR
const PV = "\\$[A-Za-z_]\\w*";
// Strict filename charset for UNQUOTED contexts — every shell metacharacter
// (; & | < > ( ) { } $ ` ' " \ space) is excluded so an unquoted path/default
// can NEVER carry a command separator, redirect, or substitution that the
// shell would execute. Only literal filename bytes + `${...}`/`$VAR` via the
// alternations below. (`..` traversal is rejected separately in isAllowable.)
const FNCHAR = "[A-Za-z0-9_./-]";
// Quoted sentinel path: "${TMPDIR:-/tmp}/..." — content between the double
// quotes is shell-LITERAL, so `;`/`>`/`&` inside are harmless data. Only `$`
// and backtick (which would re-enable expansion) are excluded; `${...}`/`$VAR`
// are re-admitted via the alternation. No injection is possible from here.
const QPATH = '"\\$\\{TMPDIR:-/tmp\\}/(?:[^"$`\\\\]|' + PE + "|" + PV + ')*"';
// Unquoted sentinel path: ${TMPDIR:-/tmp}/... — strict charset + `${...}` only.
// Bare `$VAR` (PV) is intentionally NOT admitted here: no blueprint unquoted
// path uses it (they use `${CSID}`/`${_CM_PROJ}` = PE), and an attacker-set
// `$X=" /etc/passwd"` would word-split into an arbitrary-read operand.
const UPATH = "\\$\\{TMPDIR:-/tmp\\}/(?:" + FNCHAR + "|" + PE + ")*";
// `|| echo <default>` — double-quoted (literal, `$`/backtick excluded),
// single-quoted (fully literal), $VAR/${VAR}, or a strict-charset bare word.
const DFLT = '(?:"(?:[^"$`\\\\]|' + PE + "|" + PV + ")*\"|'[^']*'|" + PE + "|" + PV + "|" + FNCHAR + "+)";
const SENTINEL_READ =
  "\\$\\(\\s*cat\\s+(?:" +
  QPATH +
  "|" +
  UPATH +
  ")" +
  "(?:\\s+2>/dev/null)?(?:\\s*\\|\\|\\s*echo\\s+" +
  DFLT +
  ")?\\s*\\)";
// Blueprint timestamp idiom: `$(date -u +%Y-%m-%dT%H-%M-%SZ)` / `$(date +%s)` — a lone
// +FORMAT argument only; any extra argument or separator falls outside the class → reject.
const DATE_STAMP = "\\$\\(\\s*date\\s+(?:-u\\s+)?\\+[%\\w:.+-]*\\s*\\)";
const SAFE_SUBST = new RegExp(SENTINEL_READ + "|" + DATE_STAMP, "g");
// Substitution-free rewritten sentinel idiom (see claude-config.md §TMPDIR
// Sentinel Scoping): `IFS= read -r VAR < "${TMPDIR:-/tmp}/…"`. Counts as a
// blueprint anchor only — it adds no capability (read < is already permitted
// in §5 and `read` is in SAFE_TOKENS); its sole job is to let a command with
// ZERO substitutions qualify, because the leading `IFS=` assignment means no
// prefix allow-rule can ever match the rewritten form.
const READ_FORM = new RegExp("(?:IFS=\\s+)?read\\s+-r\\s+[A-Za-z_]\\w*\\s*<\\s*(?:" + QPATH + "|" + UPATH + ")");

// Strictly non-writing, non-spawning first tokens. A whitelisted token must not
// be able to write a file, create a dir, spawn a process, or change host state
// with ANY flag or operand — because segment validation checks only the leading
// token, never its arguments. Deliberately EXCLUDED for that reason:
//   find   — `-exec`/`-execdir`/`-ok` spawn arbitrary commands, `-delete` removes,
//            `-fprintf`/`-fprint` write files
//   mkdir  — creates directories
//   touch  — creates/updates files
//   sort   — `-o FILE`/`--output` writes
//   date   — `--set` mutates the clock (and only ever appears inside `$(date +FMT)`,
//            handled by DATE_STAMP — never needed as a bare segment token)
//   jq     — complex; keep out of an allow path that skips argument inspection
// Also excluded (pass through to the real allow/deny matcher): git, gh, rm, mv,
// cp, curl, sed, awk, xargs, python, node, tee, dd, ...
const SAFE_TOKENS = new Set([
  "cat",
  "ls",
  "grep",
  "head",
  "tail",
  "wc",
  "echo",
  "printf",
  "basename",
  "dirname",
  "cut",
  "tr",
  // `uniq` deliberately EXCLUDED: its second positional operand is an OUTPUT
  // file — `uniq /dev/null victim` truncates `victim` to zero bytes. Same class
  // as `sort -o`, but positional rather than a flag, so it survived the original
  // sweep. Segment validation never inspects operands, so it cannot be admitted.
  "test",
  "[",
  "[[",
  "true",
  ":",
  "export",
  "read",
]);

/**
 * Mask quoted regions with 'Q' so shell metacharacters inside quotes cannot
 * confuse segmentation. Handles \x escapes outside and inside double quotes.
 * Assumes no `$(`/backtick remains (checked by caller) — quoted content is
 * then pure data + parameter expansion.
 */
function maskQuotes(s) {
  let out = "";
  let state = "plain"; // plain | single | double
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (state === "plain") {
      if (c === "\\") {
        // Escaped char = data, EXCEPT the newline, which must survive as a
        // newline. Eating it merged a `# comment \` line with the line after,
        // so the comment skip swallowed a live payload the shell still ran
        // (bash ends a comment at the physical newline; `\` does not continue
        // one). That was arbitrary command execution behind a `#`.
        out += s[i + 1] === "\n" ? "Q\n" : "QQ";
        i++;
        continue;
      }
      if (c === "'") {
        state = "single";
        out += "Q";
        continue;
      }
      if (c === '"') {
        state = "double";
        out += "Q";
        continue;
      }
      out += c;
    } else if (state === "single") {
      if (c === "'") {
        state = "plain";
      }
      out += "Q";
    } else {
      // double
      if (c === "\\") {
        out += s[i + 1] === "\n" ? "Q\n" : "QQ";
        i++;
        continue;
      }
      if (c === '"') {
        state = "plain";
      }
      out += "Q";
    }
  }
  // Unterminated quote = malformed command → force rejection downstream.
  return state === "plain" ? out : out + "$(";
}

/** True when every segment of `masked` starts with a whitelisted token. */
function segmentsAreReadOnly(masked) {
  const segments = masked.split(/[\n;|&]+/);
  for (const seg of segments) {
    const t = seg.trim();
    if (!t) continue;
    // Whole-line comment: inert, and every blueprint block carries `# timeout: N`.
    // Safe in both directions — quoted `#` is already masked to Q, and segments
    // split on `;|&` too, so `# x && rm y` still validates `rm y` as if it were live.
    if (t.startsWith("#")) continue;
    // Strip leading VAR=... assignments (covers `IFS= read`, `RUN_DIR=SREAD`).
    const rest = t.replace(/^(?:[A-Za-z_]\w*=\S*\s*)+/, "");
    if (!rest) continue; // pure assignment segment
    const first = rest.match(/^\S+/)[0];
    if (!SAFE_TOKENS.has(first)) return false;
  }
  return true;
}

// Assignments that could redirect which binary a later whitelisted token runs,
// or alter parsing/loader behaviour — reject even though the token itself is
// "safe" (e.g. `export PATH=/tmp/evil:$PATH; V=$(cat …)` would run a planted
// `cat`). `IFS=` is allowed ONLY when empty (the `IFS= read` idiom); a non-empty
// IFS assignment is rejected.
// `..` is traversal by DEFAULT; it is exempt when EITHER immediate neighbour is a
// word character or a dot — `file..txt`, `v1.2..v1.3`, `...` ellipsis, and also
// one-sided shapes like `a../x`. Either-side, not both-sides: requiring both would
// re-reject `...`. Written as a default-reject because
// the inverse — enumerating the separators a traversal may open at — silently
// misses every character left out of the class: `,` (brace expansion:
// `cat {a,../etc/passwd}`), `(`, `[` all escaped an earlier allow-list form.
const TRAVERSAL = /(?<![.\w])\.\.(?![.\w])/;

// `\+?=` catches the append form (`PATH+=:/evil`). Tested against BOTH the raw
// command and a quote/backslash-stripped copy, because bash removes those from an
// assignment word before the builtin sees it: `export "PATH"=/evil:$PATH` and
// `export \PATH=…` both really do set PATH, yet neither matched the raw pattern —
// a planted `cat` on the hijacked PATH then runs as a "read-only" whitelisted
// token. Stripping can only over-reject (a quoted literal `PATH=` inside a string),
// which is the safe direction.
// `BASH_FUNC` is deliberately absent: the real exported-function form is
// `BASH_FUNC_name%%=`, so the character after the prefix is `_`, never `=` — the
// entry could never match and only gave false coverage. `IFS` is handled by
// NONEMPTY_IFS instead, since the blueprint idiom itself opens with `IFS= read`.
const SENSITIVE_VARS =
  "PATH|LD_PRELOAD|LD_LIBRARY_PATH|LD_AUDIT|DYLD_INSERT_LIBRARIES|DYLD_LIBRARY_PATH|DYLD_FALLBACK_LIBRARY_PATH|DYLD_FRAMEWORK_PATH|DYLD_FALLBACK_FRAMEWORK_PATH|DYLD_VERSIONED_LIBRARY_PATH|BASH_ENV|ENV|SHELLOPTS|BASHOPTS|GLOBIGNORE|PS4|CDPATH|PROMPT_COMMAND";
const SENSITIVE_ASSIGN = new RegExp(
  // `(?:\+|:)?=` covers three operators, not one: plain `NAME=`, the append form
  // `NAME+=`, and bash's parameter-expansion assignment `${NAME:=word}` — the last
  // performs a real assignment (and keeps an existing export attribute, so a child
  // process inherits it) while never producing the literal `NAME=` the plain
  // pattern looks for.
  "(?:^|[\\s;|&(){])(?:export\\s+|declare\\s+\\S+\\s+|typeset\\s+\\S+\\s+)?(?:" + SENSITIVE_VARS + ")(?:\\+|:)?=",
);

// Assignment syntax is only ONE route to setting a variable, and gating it alone
// is not enough — bash offers three more that never produce a literal `NAME=` for
// the pattern above to see. Each was a confirmed PATH hijack to arbitrary code
// execution (a planted binary ran as a whitelisted "read-only" token):
//   printf -v PATH …        the printf builtin's -v flag writes a shell variable
//   read PATH < file        `read` writes its target name; input `<` is permitted
//   export PA${X}H=…        export expands its argument, so no literal PATH= exists
// The control therefore has to be "can this segment write a shell variable at all",
// not "does the text contain a sensitive assignment".
const PRINTF_ASSIGN = /(?:^|[\s;|&(){])printf\s[^\n;|&]*-v\b/;
const READ_TARGET = new RegExp("(?:^|[\\s;|&(){])read\\s+(?:-\\S+\\s+)*(?:" + SENSITIVE_VARS + "|IFS)(?:\\s|$)");
// An `export` whose NAME half carries an expansion cannot be read literally, so the
// name it actually assigns is unknowable here. Blueprint idioms always name the
// variable outright (`export CSID="${…}"` — expansion in the VALUE only).
const EXPORT_COMPUTED_NAME = /(?:^|[\s;|&(){])export\s+[^\s=]*[${`]/;
// `{` in the boundary class and `:?=` for the same reasons as SENSITIVE_ASSIGN —
// without them `${IFS=x}` and `${IFS:=x}` both slipped, the first because the
// character before `IFS` is `{`.
const NONEMPTY_IFS = /(?:^|[\s;|&({])(?:export\s+)?IFS:?=[^\s;|&]/;

/**
 * Return `cmd` with shell comments removed — an unquoted `#` in word-start
 * position through to end of line.
 *
 * Used ONLY to decide whether a blueprint anchor is present. Skipping comment
 * segments during validation (see segmentsAreReadOnly) otherwise lets a comment
 * SUPPLY the anchor: `# IFS= read -r X < "${TMPDIR:-/tmp}/y"` followed by a live
 * `cat /etc/passwd` would satisfy READ_FORM from text the shell never runs. Every
 * other check keeps operating on the full command, so a comment can only ever
 * cost an allow, never grant one.
 */
function stripComments(cmd) {
  const masked = maskQuotes(cmd);
  let out = "";
  let i = 0;
  while (i < cmd.length) {
    if (masked[i] === "#" && (i === 0 || /[\s;|&(]/.test(masked[i - 1]))) {
      while (i < cmd.length && masked[i] !== "\n") i++;
      continue;
    }
    out += cmd[i];
    i++;
  }
  return out;
}

/** Decide whether `cmd` is provably just blueprint sentinel-reads + read-only follow-ups. */
function isAllowable(cmd) {
  if (cmd.includes("`")) return false;
  // ANSI-C quoting desyncs maskQuotes: bash unescapes `\'` INSIDE `$'…'`, so the
  // string ends at a different quote than the masker thinks, and the toggle count
  // drifts. `echo $'\''; git push --force \'` masked the `;` and the push into one
  // `echo` segment while bash ran two commands. No blueprint idiom uses `$'…'`,
  // so fail closed rather than teach the masker a second quoting dialect.
  if (cmd.includes("$'")) return false;
  // Path traversal has no place in a blueprint sentinel path; reject anywhere.
  // Anchored to a real path COMPONENT, not any two dots: a bare `..` substring
  // also fires on `...` ellipsis and `v1.2..v1.3`, which are not traversal. `..`
  // must open at a separator/quote/start and close at one/end to count.
  // Also test a backslash-collapsed copy: `cat \.\./etc/passwd` carries no literal
  // `..` bytes, yet the shell strips the no-op escapes and resolves the parent dir.
  // Detection-only — the original string is what gets executed, so this can only
  // add rejections.
  if (TRAVERSAL.test(cmd) || TRAVERSAL.test(cmd.replace(/\\(.)/g, "$1"))) return false;
  // Loader / lookup-poisoning assignments defeat the read-only-token guarantee.
  // NONEMPTY_IFS stays on the raw text only — stripping quotes would turn the
  // legitimate empty `IFS=""` into a bare `IFS=` and stop rejecting the non-empty
  // forms it exists to catch.
  const unquoted = cmd.replace(/["'\\]/g, "");
  // `""`/`''` collapse to a space first, so a deliberately EMPTY assignment stays
  // empty in the stripped copy. Without that, the blueprint's own `IFS="" read`
  // would read as a non-empty IFS; with it, `export "IFS"=,` is still caught.
  const unquotedKeepEmpty = cmd.replace(/""|''/g, " ").replace(/["'\\]/g, "");
  if (SENSITIVE_ASSIGN.test(cmd) || SENSITIVE_ASSIGN.test(unquoted)) return false;
  if (NONEMPTY_IFS.test(cmd) || NONEMPTY_IFS.test(unquotedKeepEmpty)) return false;
  // Non-assignment routes to setting a variable — see the block comment above.
  if (PRINTF_ASSIGN.test(cmd) || READ_TARGET.test(cmd) || READ_TARGET.test(unquoted)) return false;
  if (EXPORT_COMPUTED_NAME.test(cmd)) return false;
  // Replace safe substitution spans (sentinel reads / date stamps); require at
  // least one blueprint anchor: a safe substitution OR the rewritten read-form
  // sentinel idiom. Without an anchor (e.g. plain `ls -la`), passthrough — this
  // hook only fronts for blueprint idioms the prefix matcher cannot express.
  const remaining = cmd.replace(SAFE_SUBST, () => "SREAD");
  // The anchor must come from text the shell actually runs — see stripComments.
  // Everything below still inspects the FULL command, so commented-out text can
  // only add rejections, never remove them.
  const live = stripComments(cmd);
  let liveSpans = 0;
  live.replace(SAFE_SUBST, () => {
    liveSpans++;
    return "SREAD";
  });
  if (liveSpans === 0 && !READ_FORM.test(live)) return false;
  // Any other substitution / process substitution / heredoc → not our idiom.
  if (remaining.includes("$(") || remaining.includes("<(") || remaining.includes(">(")) return false;
  if (remaining.includes("<<")) return false;
  const masked = maskQuotes(remaining);
  if (masked.includes("$(")) return false; // unterminated-quote marker
  // Bare parens, outside quotes and after safe substitutions became SREAD. A POSIX
  // function definition with a SUBSHELL body — `cat () ( touch x ); cat` — carries no
  // top-level separator inside the body, so segmentation sees one segment whose first
  // token is the safe name `cat` and never vets the body at all. The following `cat`
  // then runs it: arbitrary command execution. The `$(`/`<(`/`>(` guards above all
  // require a sigil and so miss bare parens entirely. No blueprint idiom uses them.
  // Tested on comment-stripped text: prose parens in a `# Reload X (Check 41)` line
  // are inert, and rejecting those alone cost 14 real blueprint blocks.
  const liveMasked = maskQuotes(stripComments(remaining));
  if (liveMasked.includes("(") || liveMasked.includes(")")) return false;
  // Strip stderr-silence / stdout-to-null forms, then reject any remaining WRITE
  // redirect (`>`, `>>`, `>|`, fd-dup `N>&M`). Input `<` is intentionally allowed:
  // the `read` blueprint form (`IFS= read -r VAR < "$F"`) needs it, and it grants
  // nothing beyond what a whitelisted read-only token already does with a path
  // argument (`Bash(cat:*)` etc. already read any file without a prompt).
  // Each stripped form must END at a token boundary. Without the lookaheads
  // `>/dev/nullpwned` had its `>/dev/null` prefix consumed, leaving no `>` for
  // the check below — so a real file write was allowed. Same class for `N>&M`.
  const noRedirects = masked.replace(/(?:\d*>\/dev\/null(?![^\s;|&])|\d*>&\d+(?![^\s;|&])|2>&1(?![^\s;|&]))/g, "");
  if (noRedirects.includes(">")) return false;
  return segmentsAreReadOnly(noRedirects);
}

/**
 * Build the hook's stdout payload for a raw stdin string, or null for passthrough.
 * Same checks in the same order the top-level driver used before this became a function: parse, Bash-only, trim,
 * empty-command early exit, `isAllowable`, and the fixed reason string. Returns; never calls `process.exit`.
 */
function decide(raw) {
  const cmd = commandOf(raw);
  return cmd !== null && isAllowable(cmd) ? allowPayload() : null;
}

/**
 * Return the trimmed Bash command carried by `raw`, or null when there is none to examine.
 * Collapses the four ways stdin can fail to name a command — unparsable, not an object, not a Bash call, and a
 * `command` that is absent, empty or not a string. The type test matters: a non-string `command` used to reach
 * `.trim()` and throw, which the dispatcher then recorded as `module-error`, making a malformed host payload
 * indistinguishable in the log from a module that actually broke.
 */
function commandOf(raw) {
  let data;
  try {
    data = JSON.parse(raw);
  } catch (_) {
    return null;
  }
  if (!data || data.tool_name !== "Bash") return null;
  const command = data.tool_input && data.tool_input.command;
  if (typeof command !== "string") return null;
  return command.trim() || null;
}

/**
 * Return this module's allow payload.
 * One builder shared by `decide` and `evaluate`, so the two cannot disagree on a byte of what the host is told.
 */
function allowPayload() {
  return {
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "allow",
      permissionDecisionReason:
        "plugin blueprint idiom — ${TMPDIR:-/tmp} sentinel read (subst or read-form) / date stamp only, all segments read-only",
    },
  };
}

/**
 * Classify what this module did with `raw`, for the audit record the dispatcher writes.
 *
 *   decision "allow"        the shape matched and an allow payload was produced;
 *   decision "passthrough"  the command was examined and the shape did not match;
 *   decision "none"         the module returned before examining any shape at all.
 *
 * `isAllowable` returns a bare boolean and gains no per-rejection reason here: deriving one would mean reaching into
 * the matcher, which stays untouched. Every decline is therefore `shape-mismatch`.
 *
 * No digest: normalization belongs to the blueprint module, and a second implementation of it here would be a second
 * definition of what a command IS. Never calls `process.exit`.
 *
 * @returns {{payload: object|null, decision: string, lane: string, rank: number, why?: string}}
 */
function evaluate(raw) {
  const lane = { lane: "shape", rank: 2 };
  const cmd = commandOf(raw);
  if (cmd === null) return { ...lane, payload: null, decision: "none", why: "not-applicable" };
  if (!isAllowable(cmd)) return { ...lane, payload: null, decision: "passthrough", why: "shape-mismatch" };
  return { ...lane, payload: allowPayload(), decision: "allow" };
}

// Requiring this module must attach no stdin listener and must not exit — the dispatcher requires it as a library,
// and `bin/audit_hook_coverage.py` still subprocesses it as a standalone hook. Both entry points stay supported.
if (require.main === module) {
  let raw = "";
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (d) => (raw += d));
  process.stdin.on("end", () => {
    try {
      const payload = decide(raw);
      if (payload) process.stdout.write(JSON.stringify(payload));
    } catch (_) {
      // Never crash or block Claude due to a hook bug
    }
    process.exit(0);
  });
}

module.exports = {
  isAllowable,
  decide,
  evaluate,
};
