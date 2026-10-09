// shell-git.js — find every git invocation in a Bash tool command (library, not a hook)
//
// PURPOSE
//   commit-guard.js gates `git push`; rule-inject.js injects the commit rule on commit-creating git
//   calls. Both must see the invocation however the shell spells it: grouped (`{ git push; }`,
//   `if x; then git push; fi`), behind a prefix (`time`, `sudo -u u`, `nohup`, `xargs`, `env -u V`),
//   with a quoted argument (`git -C "dir with space" push`), inside a substitution, or as shell
//   source handed to another program (`bash -c "git push -f"`, `eval '…'`, `env -S '…'`).
//   One implementation for both hooks, so the guard and the injector never disagree on a spelling.
//   Neither hook is propagated to another plugin, so this module ships with foundry alone.
//
// HOW IT WORKS
//   1. Precise pass. A quote-aware lexer splits the command into simple commands (word lists) at
//      unquoted operators, removes quoting, applies brace expansion to unquoted braces as bash
//      does (`{git,} pu{s..s}h {-f,x}` is `git push -f x`), lexes `$(…)`, `<(…)`, `>(…)` and
//      backtick bodies as commands of their own (also inside double quotes and unquoted-delimiter
//      heredocs), skips comments, and keeps heredoc bodies apart.
//      Every word naming the git program starts an invocation at any position, so no wrapper list
//      or wrapper option grammar is needed (`sudo -u git git push` yields both readings). The name
//      is the basename with case folded and `.exe` dropped — macOS and Windows file systems run
//      `GIT` and `Git.exe` as git; the same folding applies to SOURCE_RUNNERS names (`BASH -c`).
//      Words after a SOURCE_RUNNERS word are lexed as commands, and so are the heredoc bodies of a
//      command containing one (`bash <<'EOF'`, `cat <<'EOF' | sh`). The arguments of an
//      INTERPRETERS word (`python3 -c`, `perl -e`, `ruby -e`, `node -e`) get a raw-text scan
//      instead: a standalone `git` plus a push-family word yields a push reading carrying every
//      token of that code, so a force token anywhere in it counts.
//   2. Inline aliases. `-c alias.<name>=<value>` and `--config-env alias.<name>=<VAR>` between git
//      and its subcommand define an alias; used as the subcommand (name case ignored, as git does)
//      it is expanded — global options, nested aliases and `!` shell aliases included — and read
//      beside the literal subcommand, since git runs a builtin over an alias of the same name. An
//      alias whose value only the environment holds (`--config-env`, `-c alias.p="$V"`), or a
//      chain deeper than MAX_ALIAS_DEPTH, reads as `push --force`.
//   3. Coarse pass, always. The original detector — raw operator split, quotes ignored, argv[0]
//      after an environment prefix — runs too, so the lexer can only add detections, never lose
//      one the earlier hooks made.
//   4. Fail closed. When the lexer cannot follow the quoting (unbalanced quote, unterminated
//      substitution, heredoc without a delimiter) or a brace expansion yields more than
//      MAX_BRACE_WORDS words, any `git` word in any raw segment, quote and brace characters
//      stripped, counts as an invocation.
//   Unknown git global options are read both ways — taking a value and not — so neither
//   `git --opt v push` nor `git --opt push` hides the push.
//
// LIMITS (accepted)
//   Text the shell or git builds at run time is beyond any string inspection: a variable used as a
//   word (`$GIT push`, `git push "$REMOTE"`, `${X:-git}`), configuration read from a file (`git ci`
//   aliased in ~/.gitconfig, `include.path`), a shell alias or function defined in an rc file, a
//   script on disk, and an interpreter assembling the git words from fragments. Aliases are not
//   resolved through `git config`: running git inside the hook on every Bash call is not worth a
//   rare spelling. These string-visible spellings are not followed either, by decision — the
//   finder stays scoped to the review findings it fixes:
//     * ANSI-C and locale strings are not decoded: `$'\x67it' push`, `$"git" push`.
//     * an assigned value is not read as commands: `X='git push -f'; $X`, `GIT_SSH_COMMAND=…`,
//       `GIT_PAGER=…`, `EDITOR=…`.
//     * commands git itself runs from configuration or options: `-c core.pager=…`,
//       `-c core.sshCommand=…`, `rebase --exec`, `submodule foreach`, `difftool --extcmd`,
//       `filter-branch` filters.
//     * Windows shells as source runners: `cmd /c "git push -f"`, `pwsh -c '…'`, `powershell`
//       (an unquoted `cmd /c git push -f` is still found as a git word).
//     * globs naming the program or the subcommand: `/usr/bin/gi? push`, `git pu?h` (after
//       `touch push`); zsh's `=git push`.
//     * aliases defined through the environment: GIT_CONFIG_COUNT/GIT_CONFIG_KEY_n/
//       GIT_CONFIG_VALUE_n and GIT_CONFIG_PARAMETERS.
//     * push behaviour set by inline configuration: `-c remote.<name>.mirror=true push`,
//       `-c remote.<name>.push=+<refspec> push` force with no flag on the command line.
//     * dashed git programs: `…/git-core/git-push --force`, and a case variant of one, which git
//       runs as plain git (`…/git-core/GIT-STATUS push -f`).
//     * interpreter code arriving on stdin or in a heredoc (`python3 <<'EOF'`), and interpreters
//       outside INTERPRETERS (`php -r`, `osascript -e`, `awk 'BEGIN{system(…)}'`, `lua -e`).
//   Unquoted prose naming git in another command's arguments (`echo run git push -f`) is detected
//   as an invocation — a false positive preferred to a bypass. `case … in pat)` inside `$(…)` ends
//   the substitution early in the lexer; the coarse pass still sees the commands after it.

"use strict";

// Programs that run a later argument as shell source (`bash -c SRC`, `eval SRC`, `env -S SRC`,
// `su -c SRC`, `watch SRC`, `ssh host SRC`). Every later argument holding whitespace or shell syntax
// is lexed as a command; a plain argument (`bash script.sh "a b"`) costs only a harmless extra parse.
const SOURCE_RUNNERS = new Set([
  "sh",
  "bash",
  "zsh",
  "dash",
  "ksh",
  "mksh",
  "ash",
  "fish",
  "busybox",
  "eval",
  "env",
  "su",
  "runuser",
  "watch",
  "script",
  "parallel",
  "ssh",
]);
// Interpreters whose `-c`/`-e` code can run git through a library call (`os.system`, `system`,
// `child_process`). Their syntax is not shell syntax, so the code is scanned as raw text rather than
// lexed.
const INTERPRETERS = /^(?:python|pypy|perl|ruby|node|nodejs)[\d.]*$/;
// Standalone words in interpreter code: not part of `shell-git.js`, `.git` or `github`, and not a
// `.push(` method call.
const CODE_GIT = /(?<![\w.-])git(?:\.exe)?(?![\w.])/i;
const CODE_PUSH = /(?<![\w.])(?:push|send-pack|http-push)(?!\w)/i;
const CODE_SEPARATORS = /[\s'"`,;()[\]{}]+/;
// Shell source nested in shell source (`bash -c "sh -c '…'"`) is followed this deep, then ignored.
const MAX_NESTING = 4;
const SHELL_SYNTAX = /[\s;&|`$()<>]/;
// Value-taking git global options must consume their argument, or the value would read as the
// subcommand. Options in neither set are read both ways.
const GIT_GLOBAL_FLAGS_WITH_VALUE = new Set([
  "-C",
  "-c",
  "--git-dir",
  "--work-tree",
  "--namespace",
  "--exec-path",
  "--config-env",
  "--attr-source",
]);
const GIT_GLOBAL_FLAGS_NO_VALUE = new Set([
  "-p",
  "--paginate",
  "-P",
  "--no-pager",
  "--no-replace-objects",
  "--no-lazy-fetch",
  "--no-optional-locks",
  "--no-advice",
  "--bare",
  "--literal-pathspecs",
  "--glob-pathspecs",
  "--noglob-pathspecs",
  "--icase-pathspecs",
  "--html-path",
  "--man-path",
  "--info-path",
  "-v",
  "--version",
  "-h",
  "--help",
]);
const ENVIRONMENT_ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*=/;
// Unquoted `{` `,` `}` stand in a lexed word as these private-use characters until brace expansion,
// so quoted and escaped ones stay literal.
const BRACE_OPEN = "\uE000";
const BRACE_COMMA = "\uE001";
const BRACE_CLOSE = "\uE002";
const BRACE_MARKS = { "{": BRACE_OPEN, ",": BRACE_COMMA, "}": BRACE_CLOSE };
const BRACE_TEXT = { [BRACE_OPEN]: "{", [BRACE_COMMA]: ",", [BRACE_CLOSE]: "}" };
// More words than this from one brace expansion and the lexer gives up: the command fails closed.
const MAX_BRACE_WORDS = 256;
// Inline aliases expanding into further aliases are followed this deep; git itself stops a loop.
const MAX_ALIAS_DEPTH = 8;
// An alias value only the environment holds (`--config-env`, `-c alias.p="$V"`).
const RUNTIME_VALUE = null;
// The reading of a git command whose meaning cannot be resolved: the strongest one, so a guard
// fails closed.
const UNRESOLVED_READING = ["push", "--force"];

// --- lexer ---

/**
 * Lex `src` from `start`, pushing each simple command's words onto `acc.commands` and each heredoc
 * body onto `acc.heredocs`. `inSubst` = the body of `$(`, `<(` or `>(`: stop after its closing `)`.
 * Returns the index after the lexed text, or -1 when quoting or a substitution is unterminated.
 */
function lexFrom(src, start, inSubst, acc) {
  let words = [];
  let word = null;
  let depth = 0;
  let parameter = 0; // open `${…}` in this word: its braces and commas are not brace expansion
  const pending = [];
  const append = (text) => {
    word = (word === null ? "" : word) + text;
  };
  const appendUnquoted = (c) => {
    if (c === "{" && word !== null && word.endsWith("$")) parameter += 1;
    else if (c === "}" && parameter > 0) parameter -= 1;
    else if (parameter === 0 && BRACE_MARKS[c]) c = BRACE_MARKS[c];
    append(c);
  };
  const endWord = () => {
    if (word !== null) {
      const expanded = expandBraces(word);
      if (expanded === null) acc.overflow = true;
      else words.push(...expanded);
    }
    word = null;
    parameter = 0;
  };
  const endCommand = () => {
    endWord();
    if (words.length) acc.commands.push(words);
    words = [];
  };
  let i = start;
  while (i < src.length) {
    const c = src[i];
    const next = src[i + 1];
    if (c === "\\") {
      if (next !== "\n") append(next === undefined ? "" : next); // backslash-newline joins lines
      i += 2;
    } else if (c === "'") {
      const end = src.indexOf("'", i + 1);
      if (end < 0) return -1;
      append(src.slice(i + 1, end));
      i = end + 1;
    } else if (c === "$" && next === "'") {
      const read = readAnsiC(src, i + 2);
      if (read === null) return -1;
      append(read.text);
      i = read.end;
    } else if (c === '"') {
      const read = readExpanding(src, i + 1, '"', acc);
      if (read === null) return -1;
      append(read.text);
      i = read.end;
    } else if ((c === "$" || c === "<" || c === ">") && next === "(") {
      const end = lexFrom(src, i + 2, true, acc);
      if (end < 0) return -1;
      append(src.slice(i, end));
      i = end;
    } else if (c === "`") {
      const end = readBackticks(src, i + 1, acc);
      if (end < 0) return -1;
      append(src.slice(i, end));
      i = end;
    } else if (c === "#" && word === null) {
      while (i < src.length && src[i] !== "\n") i += 1;
    } else if (c === "<" && next === "<" && src[i + 2] !== "<") {
      endWord();
      const read = readHeredocStart(src, i + 2);
      if (read === null) return -1;
      pending.push(read.doc);
      i = read.end;
    } else if (c === "&" && (next === ">" || (word !== null && /[<>]$/.test(word)))) {
      append(c); // `&>file`, `2>&1`: a redirection, not a background operator
      i += 1;
    } else if (c === "\n") {
      endCommand();
      i = readHeredocBodies(src, i + 1, pending.splice(0), acc);
      if (i < 0) return -1;
    } else if (c === "(") {
      endCommand();
      depth += 1;
      i += 1;
    } else if (c === ")") {
      endCommand();
      if (inSubst && depth === 0) return i + 1;
      depth = Math.max(0, depth - 1);
      i += 1;
    } else if (c === ";" || c === "&" || c === "|") {
      endCommand();
      i += 1;
    } else if (c === " " || c === "\t") {
      endWord();
      i += 1;
    } else {
      appendUnquoted(c);
      i += 1;
    }
  }
  if (inSubst) return -1;
  endCommand();
  return i;
}

/**
 * Text of a double-quoted string (`terminator` '"') or of a heredoc body (`terminator` null), with
 * every `$(…)` and backtick body inside lexed as commands. Null when unterminated.
 */
function readExpanding(src, start, terminator, acc) {
  let text = "";
  let i = start;
  while (i < src.length) {
    const c = src[i];
    const next = src[i + 1];
    if (c === "\\" && next !== undefined && '"\\$`\n'.includes(next)) {
      if (next !== "\n") text += next;
      i += 2;
    } else if (c === terminator) {
      return { text, end: i + 1 };
    } else if (c === "$" && next === "(") {
      const end = lexFrom(src, i + 2, true, acc);
      if (end < 0) return null;
      text += src.slice(i, end);
      i = end;
    } else if (c === "`") {
      const end = readBackticks(src, i + 1, acc);
      if (end < 0) return null;
      text += src.slice(i, end);
      i = end;
    } else {
      text += c;
      i += 1;
    }
  }
  return terminator === null ? { text, end: i } : null;
}

/** Lex a backtick substitution's body; returns the index after the closing backtick, or -1. */
function readBackticks(src, start, acc) {
  let body = "";
  let i = start;
  while (i < src.length && src[i] !== "`") {
    if (src[i] === "\\" && "`\\$".includes(src[i + 1])) {
      body += src[i + 1];
      i += 2;
    } else {
      body += src[i];
      i += 1;
    }
  }
  if (i >= src.length) return -1;
  return lexFrom(body, 0, false, acc) < 0 ? -1 : i + 1;
}

/** `$'…'` ANSI-C quoting: a backslash escapes the next character, a quote included. */
function readAnsiC(src, start) {
  let text = "";
  let i = start;
  while (i < src.length) {
    if (src[i] === "\\") {
      text += src[i + 1] === undefined ? "" : src[i + 1];
      i += 2;
    } else if (src[i] === "'") {
      return { text, end: i + 1 };
    } else {
      text += src[i];
      i += 1;
    }
  }
  return null;
}

/** Parse `[-]DELIM` after `<<`. Quoting anywhere in DELIM keeps the body unexpanded. */
function readHeredocStart(src, start) {
  let i = start;
  const strip = src[i] === "-";
  if (strip) i += 1;
  while (src[i] === " " || src[i] === "\t") i += 1;
  let delim = "";
  let quoted = false;
  while (i < src.length && !/[\s;&|<>()]/.test(src[i])) {
    const c = src[i];
    if (c === "'" || c === '"') {
      const end = src.indexOf(c, i + 1);
      if (end < 0) return null;
      delim += src.slice(i + 1, end);
      quoted = true;
      i = end + 1;
    } else if (c === "\\") {
      delim += src[i + 1] === undefined ? "" : src[i + 1];
      quoted = true;
      i += 2;
    } else {
      delim += c;
      i += 1;
    }
  }
  return delim ? { doc: { delim, strip, quoted }, end: i } : null;
}

/** Consume the bodies of `docs`, in order, from `start` (the line after the `<<` line). */
function readHeredocBodies(src, start, docs, acc) {
  let i = start;
  for (const doc of docs) {
    let body = "";
    while (i < src.length) {
      const newline = src.indexOf("\n", i);
      const lineEnd = newline < 0 ? src.length : newline;
      const line = src.slice(i, lineEnd);
      i = lineEnd + 1;
      if ((doc.strip ? line.replace(/^\t+/, "") : line) === doc.delim) break;
      body += `${line}\n`;
    }
    acc.heredocs.push(body);
    // An unquoted delimiter leaves `$(…)` and backticks in the body live.
    if (!doc.quoted && readExpanding(body, 0, null, acc) === null) return -1;
  }
  return Math.min(i, src.length);
}

/** Simple commands and heredoc bodies of `src`, or null when the lexer cannot follow its quoting. */
function lexShell(src) {
  const acc = { commands: [], heredocs: [], overflow: false };
  return lexFrom(src, 0, false, acc) < 0 || acc.overflow ? null : acc;
}

// --- brace expansion ---

/** The words a lexed word expands to, as bash expands unquoted braces; null past MAX_BRACE_WORDS. */
function expandBraces(word) {
  if (!word.includes(BRACE_OPEN)) return [unmarkBraces(word)];
  const out = [];
  if (!expandBracesInto(word, out)) return null;
  // bash drops a word brace expansion leaves empty: `{git,} push` is `git push`.
  return out.map(unmarkBraces).filter((expanded) => expanded !== "");
}

/** Expand the leftmost valid brace expression of `word` into `out`, recursively; false on overflow. */
function expandBracesInto(word, out) {
  for (let open = word.indexOf(BRACE_OPEN); open >= 0; open = word.indexOf(BRACE_OPEN, open + 1)) {
    const close = matchingBrace(word, open);
    const items = close < 0 ? null : braceItems(word.slice(open + 1, close));
    if (items !== null) {
      const head = word.slice(0, open);
      const tail = word.slice(close + 1);
      return items.every((item) => expandBracesInto(head + item + tail, out));
    }
  }
  out.push(word);
  return out.length <= MAX_BRACE_WORDS;
}

function matchingBrace(word, open) {
  let depth = 0;
  for (let i = open; i < word.length; i += 1) {
    if (word[i] === BRACE_OPEN) depth += 1;
    else if (word[i] === BRACE_CLOSE && --depth === 0) return i;
  }
  return -1;
}

/** Alternatives of a brace body: its top-level comma list, else a `{x..y[..step]}` sequence, else null. */
function braceItems(body) {
  const items = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < body.length; i += 1) {
    if (body[i] === BRACE_OPEN) depth += 1;
    else if (body[i] === BRACE_CLOSE) depth -= 1;
    else if (body[i] === BRACE_COMMA && depth === 0) {
      items.push(body.slice(start, i));
      start = i + 1;
    }
  }
  return items.length ? [...items, body.slice(start)] : braceSequence(body);
}

/** `1..3`, `a..e`, `9..1..2` as their words, at most MAX_BRACE_WORDS + 1 of them; null otherwise. */
function braceSequence(body) {
  const match = /^(?:(-?\d+)\.\.(-?\d+)|([A-Za-z])\.\.([A-Za-z]))(?:\.\.(-?\d+))?$/.exec(body);
  if (!match) return null;
  const numeric = match[1] !== undefined;
  const first = numeric ? Number(match[1]) : match[3].charCodeAt(0);
  const last = numeric ? Number(match[2]) : match[4].charCodeAt(0);
  const step = (Math.abs(Number(match[5] || 1)) || 1) * (first <= last ? 1 : -1);
  const items = [];
  for (let value = first; step > 0 ? value <= last : value >= last; value += step) {
    items.push(numeric ? String(value) : String.fromCharCode(value));
    if (items.length > MAX_BRACE_WORDS) break;
  }
  return items;
}

function unmarkBraces(word) {
  return word.replace(/[\uE000-\uE002]/g, (mark) => BRACE_TEXT[mark]);
}

// --- coarse pass: the pre-lexer detector, kept so the lexer can only add detections ---

/** Raw segments split at operator characters, quotes ignored: an operator inside quotes only adds a segment. */
function coarseSegments(command) {
  return command.split(/(?:\|\||&&|[;&|\n`()])/);
}

/** A coarse segment's tokens from argv[0] on, after an environment/`env` prefix. */
function coarseCommandTokens(segment) {
  const tokens = segment.trim().split(/\s+/).filter(Boolean);
  let i = 0;
  let sawEnv = false;
  while (i < tokens.length) {
    const t = tokens[i];
    if (ENVIRONMENT_ASSIGNMENT.test(t) || (sawEnv && t.startsWith("-"))) {
      i += 1;
    } else if (t === "env" || t.endsWith("/env")) {
      sawEnv = true;
      i += 1;
    } else {
      break;
    }
  }
  return tokens.slice(i);
}

// --- invocation finding ---

function basename(word) {
  return word.split(/[/\\]/).pop();
}

/** The program a word runs, case folded with `.exe` dropped: macOS and Windows run `GIT` as git. */
function programName(word) {
  return basename(word)
    .toLowerCase()
    .replace(/\.exe$/, "");
}

/**
 * Push one invocation per `git` word in `words`, plus those in shell source a runner word receives and
 * a push an interpreter's code names. Returns true when `words` contains a runner (its heredoc bodies
 * are then shell source too).
 */
function collectWords(words, found, depth) {
  let runner = -1;
  let interpreter = -1;
  words.forEach((word, j) => {
    const name = programName(word);
    if (name === "git") found.push(words.slice(j));
    else if (runner < 0 && SOURCE_RUNNERS.has(name)) runner = j;
    else if (interpreter < 0 && INTERPRETERS.test(name)) interpreter = j;
  });
  if (interpreter >= 0) scanCode(words.slice(interpreter + 1).join(" "), found);
  if (runner < 0) return false;
  if (depth < MAX_NESTING) {
    for (const arg of words.slice(runner + 1)) {
      if (SHELL_SYNTAX.test(arg)) collect(arg, found, depth + 1);
    }
  }
  return true;
}

/** A push invocation for interpreter code naming git and a push-family command, carrying all its tokens. */
function scanCode(code, found) {
  if (CODE_GIT.test(code) && CODE_PUSH.test(code)) {
    found.push(["git", "push", ...code.split(CODE_SEPARATORS).filter(Boolean)]);
  }
}

function collect(command, found, depth) {
  const lexed = lexShell(command);
  if (lexed === null) {
    for (const segment of coarseSegments(command)) {
      const words = segment
        .replace(/["'\\{},]/g, " ")
        .trim()
        .split(/\s+/)
        .filter(Boolean);
      collectWords(words, found, MAX_NESTING);
    }
    return;
  }
  let runsSource = false;
  for (const words of lexed.commands) {
    if (collectWords(words, found, depth)) runsSource = true;
  }
  if (runsSource && depth < MAX_NESTING) {
    for (const body of lexed.heredocs) collect(body, found, depth + 1);
  }
}

// --- readings: subcommand and inline aliases ---

/** A `-c`/`--config-env` entry; a bare key is `true`, a value the environment holds RUNTIME_VALUE. */
function configEntry(spec, fromEnvironment) {
  const eq = spec.indexOf("=");
  if (eq < 0) return { key: spec.toLowerCase(), value: fromEnvironment ? RUNTIME_VALUE : true };
  const value = spec.slice(eq + 1);
  const runtime = fromEnvironment || /[$`]/.test(value);
  return { key: spec.slice(0, eq).toLowerCase(), value: runtime ? RUNTIME_VALUE : value };
}

/** Tokens from git's subcommand on, recording `-c`/`--config-env` entries into `config`. */
function stripGitGlobalFlags(tokens, unknownTakesValue, config) {
  let i = 1;
  while (i < tokens.length && tokens[i].startsWith("-")) {
    const t = tokens[i];
    const unknown = !GIT_GLOBAL_FLAGS_WITH_VALUE.has(t) && !GIT_GLOBAL_FLAGS_NO_VALUE.has(t);
    const takesValue = !t.includes("=") && (GIT_GLOBAL_FLAGS_WITH_VALUE.has(t) || (unknown && unknownTakesValue));
    if (t === "-c" || t === "--config-env") config.push(configEntry(tokens[i + 1] || "", t !== "-c"));
    else if (t.startsWith("--config-env=")) config.push(configEntry(t.slice("--config-env=".length), true));
    i += takesValue ? 2 : 1;
  }
  return tokens.slice(i);
}

/** The last inline definition of alias `subcommand` (names ignore case), or undefined. */
function aliasValue(config, subcommand) {
  const key = `alias.${subcommand.toLowerCase()}`;
  let value;
  for (const entry of config) {
    if (entry.key === key) value = entry.value;
  }
  return value;
}

/**
 * Append every reading of one git invocation to `readings`: its own subcommand and arguments, and
 * what an inline alias of that subcommand expands to.
 */
function readInvocation(tokens, unknownTakesValue, inherited, depth, readings) {
  const config = [...inherited];
  const args = stripGitGlobalFlags(tokens, unknownTakesValue, config);
  readings.push(args);
  const alias = args.length ? aliasValue(config, args[0]) : undefined;
  if (typeof alias !== "string" && alias !== RUNTIME_VALUE) return;
  if (alias === RUNTIME_VALUE || depth >= MAX_ALIAS_DEPTH) {
    readings.push([...UNRESOLVED_READING]);
  } else if (alias.startsWith("!")) {
    // A shell alias runs its text through the shell with the remaining arguments appended.
    const nested = [];
    collect(`${alias.slice(1)} ${args.slice(1).join(" ")}`, nested, 0);
    for (const nestedTokens of nested) readInvocation(nestedTokens, unknownTakesValue, config, depth + 1, readings);
  } else {
    const lexed = lexShell(alias);
    if (lexed === null) readings.push([...UNRESOLVED_READING]);
    else
      readInvocation(
        [tokens[0], ...lexed.commands.flat(), ...args.slice(1)],
        unknownTakesValue,
        config,
        depth + 1,
        readings,
      );
  }
}

/**
 * Arguments of every git invocation in a Bash command, from the subcommand on
 * (`git -C x push -f` → `["push", "-f"]`). An invocation appears once per reading of its unknown
 * global options and once per expansion of an inline alias, and may repeat across passes; callers
 * test with `some`/`filter`, never count.
 */
function gitSubcommandArgs(command) {
  if (typeof command !== "string") return [];
  const invocations = [];
  collect(command, invocations, 0);
  for (const segment of coarseSegments(command)) {
    const tokens = coarseCommandTokens(segment);
    if (tokens.length && programName(tokens[0]) === "git") invocations.push(tokens);
  }
  const readings = [];
  for (const tokens of invocations) {
    readInvocation(tokens, false, [], 0, readings);
    readInvocation(tokens, true, [], 0, readings);
  }
  return readings;
}

module.exports = { gitSubcommandArgs };
