// shell-git.js — find every invocation of a program (git, gh) in a Bash tool command (library, not a hook)
//
// PURPOSE
//   commit-guard.js gates `git push`; rule-inject.js injects the commit rule on commit-creating git
//   calls; gh-write-guard.js blocks GitHub writes made through `gh`. Each must see the invocation
//   however the shell spells it: grouped (`{ git push; }`, `if x; then gh pr create; fi`), behind a
//   prefix (`time`, `sudo -u u`, `nohup`, `xargs`, `env -u V`), with a quoted argument
//   (`git -C "dir with space" push`), inside a substitution, or as shell source handed to another
//   program (`bash -c "git push -f"`, `eval '…'`, `env -S '…'`). One lexer for every hook, so the
//   guards and the injector never disagree on a spelling. The name is historical: the lexer is
//   program-agnostic, and only the readings (`gitSubcommandArgs`) are git-specific.
//   gh-write-guard.js ships in foundry, oss, develop and research, so this module is propagated
//   with it (bin/propagate_shared.py); commit-guard.js and rule-inject.js stay foundry-only.
//
// EXPORTS
//   gitSubcommandArgs(command)        every reading of every git invocation, from the subcommand on
//   programInvocations(command, name, info)
//                                     every invocation of program `name`, from the program word on;
//                                     null for any program but git when no exact argv exists (step 5);
//                                     `info.unreadable` / `info.unreadableReason` name unreadable syntax
//
// HOW IT WORKS
//   1. Precise pass. A quote-aware lexer splits the command into simple commands (word lists) at
//      unquoted operators, removes quoting (`$'\x67it'` decodes to `git` with the escapes bash and
//      zsh read alike; see step 5 for the rest), applies brace expansion to unquoted braces as bash
//      does (`{git,} pu{s..s}h {-f,x}` is `git push -f x`), lexes `$(…)`, `<(…)`, `>(…)` and
//      backtick bodies as commands of their own (also inside double quotes and unquoted-delimiter
//      heredocs), skips comments, and keeps heredoc bodies and here-string words (`<<< word`)
//      apart. Unquoted redirections (`>`, `>>`, `>|`, `<`, `<>`, `>&`, `<&`, `&>`, `&>>`) are
//      dropped with their target and an unquoted fd number (`2>`, `{fd}>`), decided while quoting
//      is known: a quoted `'>'` stays an argument and `'a>'&` still ends a command. A plain git
//      alias is not shell, so `<` and `>` stay arguments there.
//      Every word naming the program starts an invocation at any position, so no wrapper list
//      or wrapper option grammar is needed (`sudo -u git git push` yields both readings). The name
//      is the basename with case folded and `.exe` dropped — macOS and Windows file systems run
//      `GIT` and `Git.exe` as git; the same folding applies to SOURCE_RUNNERS names (`BASH -c`).
//      Words after a SOURCE_RUNNERS word are lexed as commands, and so are the heredoc bodies and
//      here-string words of a command containing one (`bash <<'EOF'`, `cat <<'EOF' | sh`,
//      `bash <<< 'git push -f'`; `trap '…' EXIT`, `source /dev/stdin <<< '…'`). A shell reading its
//      commands from stdin (`sh`, `bash -s`, `xargs sh -c` with the string left to xargs,
//      `source <(…)`) runs what the other commands on the line print, so their joined arguments are
//      lexed as commands too (`echo 'gh pr create' | sh`); printed text holding a backslash may be
//      decoded first (`printf`, `echo -e`), so it fails closed (step 5). An invocation run by xargs
//      gains the words xargs supplies as XARGS_TAIL, which reads as run-time text: each replacement
//      string (`-I {}`) and an appended tail (`xargs gh` hides the subcommand; git under xargs with a
//      hidden or push-family subcommand also reads `push --force`). The `[…]` subscript of an argument
//      to a builtin that evaluates it (SUBSCRIPT_BUILTINS: `printf -v 'a[$(…)]'`, `read`, `let`,
//      `declare`, `test -v`) is lexed as commands. The arguments of an INTERPRETERS word (`python3 -c`, `perl -e`,
//      `ruby -e`, `node -e`) get a raw-text scan instead. For git, a standalone `git` plus a
//      push-family word yields a push reading carrying every token of that code, so a force token
//      anywhere in it counts; for any other program, a standalone word naming it means no exact
//      argv (step 5): code tokens split quoted values (`'a b'`), so they cannot stand in for one.
//   2. Inline git configuration. `-c alias.<name>=<value>` and `--config-env alias.<name>=<VAR>`
//      between git and its subcommand define an alias; used as the subcommand (name case ignored,
//      as git does) it is expanded — global options, nested aliases and `!` shell aliases
//      included — and read beside the literal subcommand, since git runs a builtin over an alias
//      of the same name. An alias whose value only the environment holds (`--config-env`,
//      `-c alias.p="$V"`), or a chain deeper than MAX_ALIAS_DEPTH, reads as `push --force`. An
//      inline `help.autocorrect` that runs git's guess for a mistyped subcommand (any value but
//      AUTOCORRECT_OFF, a run-time value included) adds a `push --force` reading to every
//      subcommand: `git -c help.autocorrect=1 pus -f` runs `push -f`.
//   3. Unquoted globs. An unquoted `?`, `*` or `[` in a git argument may match a planted file
//      named like a flag or refspec (`touch ./-f; git push origin -?`), so beside the literal
//      reading a second one reads each such argument as `--force`. Readings stay plain strings.
//   4. Coarse pass, git only. The original detector — raw operator split, quotes ignored, argv[0]
//      after an environment prefix — runs too, so the lexer can only add git detections, never
//      lose one the earlier hooks made. Other programs have no earlier detector to preserve, and
//      quotes-ignored tokens would split their quoted arguments. One exception: the body of a
//      quoted-delimiter heredoc (`<<'EOF'`, `<<"EOF"`, `<<\EOF`) is left out of the coarse text when
//      the lexer read the whole command, nothing was unreadable, every simple command in it,
//      substitutions included, is a data command (DATA_PROGRAMS, `git commit|tag|notes`, `gh
//      pr|issue|release|gist|api`), and the rest is a plain skeleton (plainSkeleton: no `$`,
//      backtick, parenthesis, bracket, brace, backslash, `#` or CR outside quotes, but the
//      commit-message `"$(cat <<'EOF'…)"`): no shell expands that body, none of those commands runs
//      it, and with no `$[…]`, `${…}`, `((…))` or CRLF the lexer places the body exactly where the
//      shells do, so a body line `git push origin main` in release notes or a commit message is no
//      push. A body a shell, interpreter, pipe consumer, prefix program or later script may run
//      keeps its reading, and so does an unquoted-delimiter body.
//   5. Fail closed. When the lexer cannot follow the quoting (unbalanced quote, unterminated
//      substitution, heredoc without a delimiter) or a brace expansion yields more than
//      MAX_BRACE_WORDS words, git reads any git word in any raw segment, quote and brace
//      characters stripped, as an invocation, every glob character in it as unquoted: extra git
//      readings only add force detections. Quoting the shells read differently counts as quoting
//      the lexer cannot follow — the Bash tool runs the user's shell, often zsh, and the shells
//      disagree on `$"…"` (bash `git`, zsh `$git`), on a `$'…'` escape other than the named, octal
//      and `\xHH` ones (`\u`, `\U`, `\c`, `\x{…}`, an unknown letter, a NUL), on a heredoc
//      delimiter written with either, on two or more `$` before a quote, `{` or `(` (`$$'…'`:
//      bash `$$` then a plain quote, zsh `$` then `$'…'`; `"$${…}"`: bash opens `${` on the
//      second `$`, zsh reads `$$` first) and on a single quote inside a double-quoted `${…}` (a
//      quote to bash, text to zsh; nested double quotes and `\X` pairs there are read as every
//      shell reads them). So does `${` followed by a space, tab, newline or `|` — unquoted, in
//      double quotes or in a heredoc body: bash 5.3 runs `${ cmd; }` and `${|cmd;}` as commands,
//      older bash and zsh reject them. zsh syntax that runs code bash never reads is unreadable
//      too: a `${(flags)…}` expansion (`(e)` evaluates the value) anywhere, and a `(…)` closing an
//      unquoted word that holds a code-running glob qualifier (`.(e:'cmd':)`, `*(+name)`) — zsh is
//      the Bash tool's shell on macOS. A backslash-newline right after `$`, after `${`, inside a
//      heredoc delimiter, right after an unquoted `<` or `>`, or ending a line of an unquoted-delimiter
//      heredoc body is unreadable too: every shell splices the line before reading the opener or
//      comparing the line to the delimiter (`$\⏎'…'` is `$'…'`, `<\⏎<` is `<<`, `EO\⏎F` ends the
//      body), where the lexer decides on the raw text. A comment does not continue
//      (`# x \⏎git push` runs the push), so nothing is spliced ahead of lexing. Text printed to a
//      shell reading stdin that holds a backslash is unreadable as well (step 1). Since such quoting may hide
//      the program word itself, git then also reads `push --force`, and programInvocations sets
//      `info.unreadable` for the caller and `info.unreadableReason` to the first construct found
//      (UNREADABLE_SYNTAX), so a block message can name the real cause, not a push or gh write the
//      command may not hold. Any other program gets null from programInvocations
//      instead, and so does interpreter code naming it: quote-stripped words shift positionals
//      (`--title 'a b' create` reads `b` where gh reads `create`), so a guessed argv is not a
//      superset and the caller must fail closed on its own terms.
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
//     * shell syntax only zsh has (`=git push`, `${~x}`, a glob qualifier that runs no code): the
//       lexer reads bash. Code-running zsh syntax is unreadable instead (step 5).
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
//     * third-party `git-<name>` programs on PATH that push under their own subcommand
//       (`git town sync`, `git machete traverse`): an unbounded set.
//     * GitHub API force updates (`gh repo sync --force`, `gh api -X PATCH …/git/refs/…`) run no git
//       push; gh-write-guard.js denies them as gh writes.
//     * interpreter code arriving on stdin or in a heredoc (`python3 <<'EOF'`), and interpreters
//       outside INTERPRETERS (`php -r`, `osascript -e`, `awk 'BEGIN{system(…)}'`, `lua -e`).
//     * a `!` shell alias re-lexes the arguments appended to it, so a quoted glob character there
//       reads as unquoted: a false force reading only, never a missed one.
//   Unquoted prose naming git in another command's arguments (`echo run git push -f`) is detected
//   as an invocation — a false positive preferred to a bypass. `case … in pat)` inside `$(…)` ends
//   the substitution early in the lexer; the lexer then reads the rest at the outer level, and for
//   git the coarse pass still sees the commands after it.

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
  "trap",
  "source",
  ".",
]);
// Words a builtin such as `.` can follow in its own simple command: shell keywords and the bash and
// zsh precommand modifiers (an option of `time` or `command` and an assignment may follow them too).
// `.` elsewhere is data (`jq -e .`, `git add .`), so it runs nothing.
const BUILTIN_PREFIXES = new Set([
  "!",
  "{",
  "if",
  "then",
  "else",
  "elif",
  "do",
  "while",
  "until",
  "time",
  "coproc",
  "builtin",
  "command",
  "exec",
  "noglob",
  "nocorrect",
  "-",
]);
// Shells that run commands read from stdin when given no script and no `-c` string (`echo '…' | sh`,
// `sh < <(…)`, `bash -s`), or whose `-c` string is the last word, so xargs supplies it.
const STDIN_SHELLS = new Set(["sh", "bash", "zsh", "dash", "ksh", "mksh", "ash", "fish"]);
// Shell options that take the next word as their value (`bash -o pipefail`).
const SHELL_VALUE_OPTIONS = new Set(["-o", "+o", "-O", "+O"]);
// `source`/`.` arguments that read stdin or a process substitution (`source <(…)`).
const STDIN_FILE = /^(?:-|\/dev\/stdin|\/dev\/fd\/\d+|\/proc\/self\/fd\/\d+|<\()/;
// Builtins that evaluate an argument's `[…]` subscript as arithmetic, running any `$(…)` or backtick
// in it (`printf -v 'a[$(cmd)]' x`, `read`, `let`, `declare`, `test -v`).
const SUBSCRIPT_BUILTINS = new Set([
  "printf",
  "read",
  "let",
  "declare",
  "typeset",
  "local",
  "export",
  "readonly",
  "unset",
  "test",
  "[",
  "[[",
]);
// Stands for the words xargs appends at run time; reads as run-time text (`$`), never as a literal.
const XARGS_TAIL = "$(xargs)";
// A git subcommand that may push once xargs appends its arguments (`xargs git push` + `-f`).
const PUSH_FAMILY = /^(?:push|send-pack|http-push|subtree)$/i;
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
// An unquoted word right before `<` or `>` naming the redirected file descriptor (`2>`, `{fd}>`): part of
// the operator, never an argument. A quoted or escaped one (`"2">x`, `\2>x`) stays a word, as in bash.
const FD_PREFIX = /^(?:\d+|[A-Za-z_][A-Za-z0-9_]*)$/;
// Unquoted `?` `*` `[` stand in a lexed word as these private-use characters, so a reading can tell
// a pathname-expanding glob from a quoted or escaped literal one.
const GLOB_ANY_CHAR = "\uE003";
const GLOB_ANY_TEXT = "\uE004";
const GLOB_BRACKET = "\uE005";
const GLOB_MARKS = { "?": GLOB_ANY_CHAR, "*": GLOB_ANY_TEXT, "[": GLOB_BRACKET };
const GLOB_TEXT = { [GLOB_ANY_CHAR]: "?", [GLOB_ANY_TEXT]: "*", [GLOB_BRACKET]: "[" };
const GLOB_MARKED = /[\uE003-\uE005]/;
// The second reading of a glob argument: a planted file can make it expand to any flag or refspec.
const GLOB_READING = "--force";
// Inline aliases expanding into further aliases are followed this deep; git itself stops a loop.
const MAX_ALIAS_DEPTH = 8;
// An alias value only the environment holds (`--config-env`, `-c alias.p="$V"`).
const RUNTIME_VALUE = null;
// help.autocorrect values that never run git's guess for a mistyped subcommand (case ignored): off
// as a boolean, or suggest only — `prompt` asks on a terminal, and a hook-run Bash call has none.
const AUTOCORRECT_OFF = new Set(["0", "false", "off", "no", "", "never", "show", "prompt"]);
// The reading of a git command whose meaning cannot be resolved: the strongest one, so a guard
// fails closed.
const UNRESOLVED_READING = ["push", "--force"];

// --- lexer ---

/**
 * Lex `src` from `start`, pushing each simple command's words onto `acc.commands` and each heredoc
 * body onto `acc.heredocs`. `inSubst` = the body of `$(`, `<(` or `>(`: stop after its closing `)`.
 * Unquoted redirections (`2>/dev/null`, `> out.json`, `2>&1`, `&>log`) are dropped with their target
 * when `acc.redirections` is set; otherwise `<` and `>` are ordinary characters.
 * Returns the index after the lexed text, or -1 when quoting or a substitution is unterminated.
 */
function lexFrom(src, start, inSubst, acc) {
  let words = [];
  let word = null;
  let quoted = false; // part of the current word came from quoting or an escape
  let depth = 0;
  let parameter = 0; // open `${…}` in this word: its braces, commas and globs are not expansions
  let hereString = false; // the next word follows `<<<`: stdin text, not an argument
  let redirectTarget = false; // the next word follows a redirection operator: a file, not an argument
  const pending = [];
  const append = (text) => {
    word = (word === null ? "" : word) + text;
  };
  const appendQuoted = (text) => {
    append(text);
    quoted = true;
  };
  const appendUnquoted = (c) => {
    const afterDollar = word !== null && word.endsWith("$"); // `${`, `$?`, `$*`, `$[`: parameters
    if (c === "{" && afterDollar) parameter += 1;
    else if (c === "}" && parameter > 0) parameter -= 1;
    else if (parameter === 0 && BRACE_MARKS[c]) c = BRACE_MARKS[c];
    else if (parameter === 0 && GLOB_MARKS[c] && !afterDollar) c = GLOB_MARKS[c];
    append(c);
  };
  const endWord = () => {
    if (word !== null && redirectTarget) {
      redirectTarget = false; // the redirection's file: no argument of the command
    } else if (word !== null && hereString) {
      // bash applies neither brace nor pathname expansion to a here-string word
      acc.heredocs.push(unmarkGlobs(unmarkBraces(word)));
      hereString = false;
    } else if (word !== null) {
      const expanded = expandBraces(word);
      if (expanded === null) acc.overflow = true;
      else words.push(...expanded);
    }
    word = null;
    quoted = false;
    parameter = 0;
  };
  // Before `<` or `>`: an unquoted fd number belongs to the operator; any other word ends there.
  const endWordBeforeRedirection = () => {
    if (word === null || quoted || !FD_PREFIX.test(word)) return endWord();
    word = null;
    parameter = 0;
  };
  const endCommand = () => {
    endWord();
    hereString = false;
    redirectTarget = false;
    if (words.length) acc.commands.push(words);
    words = [];
  };
  let i = start;
  while (i < src.length) {
    const c = src[i];
    const next = src[i + 1];
    if (c === "\\") {
      if (next !== "\n") appendQuoted(next === undefined ? "" : next); // backslash-newline joins lines
      i += 2;
    } else if (c === "'") {
      const end = src.indexOf("'", i + 1);
      if (end < 0) return -1;
      appendQuoted(src.slice(i + 1, end));
      i = end + 1;
    } else if (c === "$" && next === "$" && DOLLARS_BEFORE_OPENER.test(src.slice(i))) {
      // `$$'…'`: bash reads `$$` then a plain quote, zsh `$` then `$'…'`
      return unreadable(acc, UNREADABLE_SYNTAX.dollars);
    } else if (c === "$" && unreadableExpansion(src, i)) {
      return unreadable(acc, unreadableExpansion(src, i));
    } else if (c === "$" && next === "'") {
      const read = readAnsiC(src, i + 2);
      if (read === null) return -1;
      if (read.unreadable) return unreadable(acc, UNREADABLE_SYNTAX.ansiC);
      appendQuoted(read.text);
      i = read.end;
    } else if (c === "$" && next === '"') {
      // A locale string: bash reads `$"git"` as `git`, zsh as `$git`.
      return unreadable(acc, UNREADABLE_SYNTAX.locale);
    } else if (c === '"') {
      const read = readExpanding(src, i + 1, '"', acc);
      if (read === null) return -1;
      appendQuoted(read.text);
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
    } else if (acc.redirections && (c === "<" || c === ">") && next === "\\" && src[i + 2] === "\n") {
      // `<\⏎<EOF`: the shells splice the line first and read `<<`, the lexer would read two `<`
      return unreadable(acc, UNREADABLE_SYNTAX.continued);
    } else if (c === "<" && next === "<" && src[i + 2] === "<") {
      endWordBeforeRedirection(); // `<<<` is one redirection token: a here-string, never a heredoc start
      hereString = true;
      redirectTarget = false;
      i += 3;
    } else if (c === "<" && next === "<") {
      endWordBeforeRedirection();
      const read = readHeredocStart(src, i + 2);
      if (read === null) return -1;
      if (read.unreadable) return unreadable(acc, UNREADABLE_SYNTAX.heredocDelimiter);
      pending.push(read.doc);
      i = read.end;
    } else if (acc.redirections && parameter === 0 && (c === "<" || c === ">" || (c === "&" && next === ">"))) {
      // Decided here, where quoting is known: a quoted `>` or `'a>'&` never reaches this branch.
      if (c === "&")
        endWord(); // `&>` takes no fd number: `2&>x` passes `2` as an argument
      else endWordBeforeRedirection();
      i = redirectionEnd(src, i);
      hereString = false;
      redirectTarget = true;
    } else if (c === "\n") {
      endCommand();
      i = readHeredocBodies(src, i + 1, pending.splice(0), acc, inSubst);
      if (i < 0) return -1;
    } else if (c === "(" && word !== null && !word.endsWith("=") && runsZshQualifier(src, i)) {
      // zsh reads `(…)` closing a word as glob qualifiers, and `e…` or `+name` there runs code
      // (`.(e:'cmd':)`); bash rejects the word. An array assignment `a=(…)` is no qualifier.
      return unreadable(acc, UNREADABLE_SYNTAX.zshQualifier);
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

/** Index after the unquoted redirection operator at `i`: `>` `>>` `>|` `>&` `<` `<>` `<&` `&>` `&>>`. */
function redirectionEnd(src, i) {
  const next = src[i + 1];
  if (src[i] === "&") return src[i + 2] === ">" ? i + 3 : i + 2;
  if (src[i] === ">" && (next === ">" || next === "|" || next === "&")) return i + 2;
  if (src[i] === "<" && (next === ">" || next === "&")) return i + 2;
  return i + 1;
}

/**
 * Text of a double-quoted string (`terminator` '"') or of a heredoc body (`terminator` null), with
 * every `$(…)` and backtick body inside lexed as commands. Null when unterminated.
 */
function readExpanding(src, start, terminator, acc) {
  let text = "";
  let parameter = 0; // `${…}` depth inside a double-quoted string
  let i = start;
  while (i < src.length) {
    const c = src[i];
    const next = src[i + 1];
    if (c === "\\" && next !== undefined && '"\\$`\n'.includes(next)) {
      if (next !== "\n") text += next;
      i += 2;
    } else if (parameter > 0 && c === "\\" && next !== undefined) {
      // inside `${…}` every shell keeps `\}` (any `\X`) in the expansion: it closes nothing
      text += c + next;
      i += 2;
    } else if (terminator === null && c === "$" && next === "$") {
      // a heredoc body pairs `$$` left to right in every shell: `$$(x)` is the PID then text
      text += "$$";
      i += 2;
    } else if (c === "$" && unreadableExpansion(src, i)) {
      markUnreadable(acc, unreadableExpansion(src, i));
      return null;
    } else if (terminator === '"' && c === "$" && next === "$" && DOLLARS_BEFORE_EXPANSION.test(src.slice(i))) {
      // `"$${…}"`, `"$$(…)"`: bash opens the expansion on the second `$`, zsh reads `$$` first
      markUnreadable(acc, UNREADABLE_SYNTAX.dollars);
      return null;
    } else if (parameter > 0 && c === "'") {
      // `"${x:-'…'}"`: bash reads the single quotes as quotes, zsh as text — no one reading is right
      markUnreadable(acc, UNREADABLE_SYNTAX.quoteInExpansion);
      return null;
    } else if (parameter > 0 && c === '"') {
      // `"${x%"$y"}"`: both shells nest the inner double quotes inside the expansion
      const read = readExpanding(src, i + 1, '"', acc);
      if (read === null) return null;
      text += src.slice(i, read.end);
      i = read.end;
    } else if (c === terminator) {
      return { text, end: i + 1 };
    } else if (terminator === '"' && c === "$" && next === "{") {
      parameter += 1;
      text += "${";
      i += 2;
    } else if (c === "}" && parameter > 0) {
      parameter -= 1;
      text += c;
      i += 1;
    } else if (c === "$" && next === "(") {
      const end = lexFrom(src, i + 2, true, acc);
      if (end < 0) return null;
      text += src.slice(i, end);
      i = end;
    } else if (c === "`") {
      const end = readBackticks(src, i + 1, acc, terminator === '"');
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

/**
 * True when the `(` at `open`, attached to a word, closes that word with a group zsh reads as glob
 * qualifiers holding one that runs code. Quotes inside the group are skipped whole.
 */
function runsZshQualifier(src, open) {
  let depth = 0;
  for (let j = open; j < src.length; j += 1) {
    const c = src[j];
    if (c === "\\") {
      j += 1;
    } else if (c === "'" || c === '"') {
      const end = src.indexOf(c, j + 1);
      if (end < 0) return false;
      j = end;
    } else if (c === "(") {
      depth += 1;
    } else if (c === ")" && --depth === 0) {
      const after = src[j + 1];
      const closesWord = after === undefined || /[\s;&|<>)]/.test(after);
      return closesWord && ZSH_CODE_QUALIFIER.test(src.slice(open + 1, j));
    }
  }
  return false;
}

/**
 * Lex a backtick substitution's body; returns the index after the closing backtick, or -1. Inside
 * double quotes the shells also unescape `\"` in the body.
 */
function readBackticks(src, start, acc, inDoubleQuotes = false) {
  const escapable = inDoubleQuotes ? '`\\$"' : "`\\$";
  let body = "";
  let i = start;
  while (i < src.length && src[i] !== "`") {
    if (src[i] === "\\" && escapable.includes(src[i + 1])) {
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

// `$'…'` escapes that bash (3.2 on) and zsh decode to the same character. Every other escape — `\u`,
// `\U`, `\c`, `\x{…}`, an unknown letter, a NUL — decodes differently between the shells or bash
// versions (zsh drops the backslash of `\q`, bash keeps it; bash 3.2 has no `\u`), so a string
// holding one is unreadable and the lexer fails closed.
const ANSI_C_ESCAPES = { a: "\x07", b: "\b", e: "\x1b", E: "\x1b", f: "\f", n: "\n", r: "\r", t: "\t", v: "\v" };
ANSI_C_ESCAPES["\\"] = "\\";
ANSI_C_ESCAPES["'"] = "'";
ANSI_C_ESCAPES['"'] = '"';
ANSI_C_ESCAPES["?"] = "?";
// Two or more `$` right before a quote, `{` or `(`: bash pairs the last `$` with what follows
// (`$'…'`, `${…}`, `$(…)`) where zsh reads `$$` first, so the shells end the word in different places.
const DOLLARS_BEFORE_OPENER = /^\${2,}['"{(]/;
// The same inside double quotes, where a quote after `$$` is literal text or the closing quote.
const DOLLARS_BEFORE_EXPANSION = /^\${2,}[{(]/;
// `${ cmd; }` and `${|cmd;}`: bash 5.3 runs the body as commands, older bash and zsh reject it.
// A backslash-newline after `${` is spliced away first, so it may hide either opener.
const COMMAND_BRACE = /^\$\{(?:[ \t\n|]|\\\n)/;
// `$` then a backslash-newline: the shells splice the line before reading what follows the `$`
// (`$\⏎'…'` is `$'…'`, `$\⏎(` is `$(`), which the lexer, deciding on the raw next character, misses.
const CONTINUED_DOLLAR = /^\$\\\n/;
// `${(flags)…}`: zsh parameter flags, `(e)` and `(P)` among them, evaluate the value as code or a
// name; bash rejects the form, so no bash reading of it is right.
const ZSH_FLAGS = /^\$\{\(/;
// What `info.unreadableReason` names for each kind of unreadable syntax, so a guard blocking the command
// can name the real cause instead of the push or gh write the command may not hold.
const UNREADABLE_SYNTAX = Object.freeze({
  dollars: "`$$` right before a quote, brace or parenthesis",
  commandBrace: "bash 5.3's `${ cmd; }` or `${|cmd;}`",
  continued: "a backslash-newline the shells splice before reading what follows it",
  zshFlags: "zsh `${(flags)…}` parameter flags",
  ansiC: "a `$'…'` escape bash and zsh decode differently",
  locale: 'a `$"…"` locale string',
  heredocDelimiter: "a heredoc delimiter written with `$'…'`, `$\"…\"` or a backslash-newline",
  zshQualifier: "a zsh glob qualifier that runs code (`(e:…:)`, `(+name)`)",
  quoteInExpansion: "a single quote inside a double-quoted `${…}`",
  printedBackslash: "text with a backslash printed to a shell reading stdin",
  heredocInSubstitution:
    "a heredoc inside `$(…)` whose body could close the substitution early in bash 3.2 (an unbalanced `)` such " +
    "as a `1)` list item, a backtick or `$'`); pass the text by file instead (`git commit -F <file>`, `--body-file`)",
});

/** The unreadable syntax a `$` at `i` opens (`${ cmd; }`, `$\⏎`, `${(flags)…}`), or null. */
function unreadableExpansion(src, i) {
  if (COMMAND_BRACE.test(src.slice(i, i + 4))) return UNREADABLE_SYNTAX.commandBrace;
  if (CONTINUED_DOLLAR.test(src.slice(i, i + 3))) return UNREADABLE_SYNTAX.continued;
  if (ZSH_FLAGS.test(src.slice(i, i + 3))) return UNREADABLE_SYNTAX.zshFlags;
  return null;
}
// A zsh glob qualifier list holding one that runs code: `e` and any delimiter (`e:…:`, `e'…'`), or
// `+name`. A qualifier list has no unquoted whitespace, so the qualifier comes before the first one.
const ZSH_CODE_QUALIFIER = /^\S*?(?:e[^\w\s]|\+[A-Za-z_])/;

/**
 * One escape inside a `$'…'` body at `i` (the character after the backslash): its text and the index
 * after it, or null when the shells read it differently.
 */
function readAnsiCEscape(body, i) {
  const c = body[i];
  if (ANSI_C_ESCAPES[c] !== undefined) return { text: ANSI_C_ESCAPES[c], end: i + 1 };
  const octal = /^[0-7]{1,3}/.exec(body.slice(i, i + 3));
  const hex = c === "x" && body[i + 1] !== "{" ? /^[0-9a-fA-F]{1,2}/.exec(body.slice(i + 1, i + 3)) : null;
  if (!octal && !hex) return null;
  const code = octal ? parseInt(octal[0], 8) & 0xff : parseInt(hex[0], 16);
  if (code === 0) return null; // bash ends the string at a NUL, zsh keeps going
  return { text: String.fromCharCode(code), end: octal ? i + octal[0].length : i + 1 + hex[0].length };
}

/**
 * `$'…'` ANSI-C quoting. The closing quote is found first, as the shell finds it (a backslash skips
 * the next character), then the body is decoded: `$'\x67it'` is `git`. `unreadable` when the body
 * holds an escape the shells decode differently; null when the quote never closes.
 */
function readAnsiC(src, start) {
  let end = start;
  while (end < src.length && src[end] !== "'") end += src[end] === "\\" ? 2 : 1;
  if (end >= src.length) return null;
  const body = src.slice(start, end);
  let text = "";
  for (let i = 0; i < body.length;) {
    if (body[i] !== "\\") {
      text += body[i];
      i += 1;
      continue;
    }
    const read = readAnsiCEscape(body, i + 1);
    if (read === null) return { unreadable: true, end: end + 1 };
    text += read.text;
    i = read.end;
  }
  return { text, end: end + 1 };
}

/** Parse `[-]DELIM` after `<<`. Quoting anywhere in DELIM keeps the body unexpanded. */
function readHeredocStart(src, start) {
  let i = start;
  const strip = src[i] === "-";
  if (strip) i += 1;
  while (src[i] === " " || src[i] === "\t") i += 1;
  let delim = "";
  let quoted = false;
  // The word ends only at a shell metacharacter: a CR (CRLF line ends) or any other space stays in it, as
  // in bash and zsh, which then end the body at the line `EOF\r`.
  while (i < src.length && !/[ \t\n;&|<>()]/.test(src[i])) {
    const c = src[i];
    // `<<$'EOF'`: which line ends the body depends on how the shell decodes the delimiter
    if (c === "$" && (src[i + 1] === "'" || src[i + 1] === '"')) return { unreadable: true };
    // `<<EO\⏎F`: the shells splice the line before reading the delimiter word, so it is `EOF`
    if (c === "\\" && src[i + 1] === "\n") return { unreadable: true };
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

/**
 * Consume the bodies of `docs`, in order, from `start` (the line after the `<<` line). A quoted-delimiter
 * body is recorded in `acc.quotedBodies` as its span of `src`, delimiter line included.
 */
function readHeredocBodies(src, start, docs, acc, inSubst = false) {
  let i = start;
  for (const doc of docs) {
    const bodyStart = i;
    let body = "";
    while (i < src.length) {
      const newline = src.indexOf("\n", i);
      const lineEnd = newline < 0 ? src.length : newline;
      const line = src.slice(i, lineEnd);
      i = lineEnd + 1;
      // An unquoted delimiter's body joins a line ending in an odd backslash run with the next one
      // before the shells compare it to the delimiter (`EO\⏎F` ends the body, `x\⏎EOF` does not).
      if (!doc.quoted && /(?:^|[^\\])(?:\\\\)*\\$/.test(line)) return unreadable(acc, UNREADABLE_SYNTAX.continued);
      if ((doc.strip ? line.replace(/^\t+/, "") : line) === doc.delim) break;
      body += `${line}\n`;
    }
    // bash 3.2 (macOS /bin/bash) ends a `$(` by scanning for its `)` before it reads the heredoc, so a body
    // that closes the substitution early runs its tail as commands there, while zsh and bash ≥ 4 read data.
    if (inSubst && closesSubstitution(body)) return unreadable(acc, UNREADABLE_SYNTAX.heredocInSubstitution);
    acc.heredocs.push(body);
    if (doc.quoted) acc.quotedBodies.push({ src, start: bodyStart, end: Math.min(i, src.length) });
    // An unquoted delimiter leaves `$(…)` and backticks in the body live.
    if (!doc.quoted && readExpanding(body, 0, null, acc) === null) return -1;
  }
  return Math.min(i, src.length);
}

/**
 * True when a heredoc `body` read inside `$(` could end the substitution early in bash 3.2, which scans the
 * body text for the `)` before it reads the heredoc: a backtick or `$'` (quoting that scan models differently),
 * or a `)` that takes paren depth below zero either counting every paren or skipping quoted ones (single and
 * double quotes, backslash escapes). Only the body is scanned, delimiter line excluded; balanced `fix(x)` and
 * an apostrophe stay readable, a list item `1)` does not (pass such text by file).
 */
function closesSubstitution(body) {
  if (/`|\$'/.test(body)) return true;
  let plain = 0;
  let quoted = 0;
  let quote = "";
  for (let i = 0; i < body.length; i++) {
    const c = body[i];
    if (c === "(") plain += 1;
    else if (c === ")" && --plain < 0) return true;
    if (c === "\\" && quote !== "'") i += 1;
    else if (quote) quote = c === quote ? "" : quote;
    else if (c === "'" || c === '"') quote = c;
    else if (c === "(") quoted += 1;
    else if (c === ")" && --quoted < 0) return true;
  }
  return false;
}

/**
 * Simple commands and heredoc bodies of `src`, or null when the lexer cannot follow its quoting.
 * `redirections` false reads `<` and `>` as ordinary characters, for text that is not shell source.
 */
function lexShell(src, redirections = true, failure = {}) {
  const acc = {
    commands: [],
    heredocs: [],
    quotedBodies: [],
    overflow: false,
    unreadable: false,
    unreadableReason: "",
    redirections,
  };
  if (lexFrom(src, 0, false, acc) >= 0 && !acc.overflow) return acc;
  failure.unreadable = acc.unreadable;
  failure.reason = acc.unreadableReason;
  return null;
}

/** Record in `acc` that it holds `reason`, syntax the shells read differently; the first reason found is kept. */
function markUnreadable(acc, reason) {
  acc.unreadable = true;
  if (!acc.unreadableReason) acc.unreadableReason = reason;
}

/** Mark `acc` as holding quoting the shells read differently (`reason`); the lexer then fails closed (-1). */
function unreadable(acc, reason) {
  markUnreadable(acc, reason);
  return -1;
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

// --- pathname expansion ---

function unmarkGlobs(word) {
  return word.replace(/[\uE003-\uE005]/g, (mark) => GLOB_TEXT[mark]);
}

/** Every glob character of `word` marked as unquoted: the fail-closed path knows no quoting. */
function markGlobs(word) {
  return word.replace(/[?*[]/g, (c) => GLOB_MARKS[c]);
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
 * Push one invocation per word naming `target.name` in `words`, plus those in shell source a runner
 * word receives and those an interpreter's code names. Returns true when `words` contains a runner
 * (its heredoc bodies and here-string words are then shell source too).
 */
function collectWords(words, found, depth, target) {
  let runner = -1;
  let interpreter = -1;
  let subscript = -1;
  words.forEach((word, j) => {
    const name = programName(word);
    if (name === target.name) found.push(withXargsTail(words, j));
    else if (runner < 0 && SOURCE_RUNNERS.has(name) && (name !== "." || j === commandStart(words))) runner = j;
    else if (interpreter < 0 && INTERPRETERS.test(name)) interpreter = j;
    else if (subscript < 0 && SUBSCRIPT_BUILTINS.has(unmarkGlobs(name))) subscript = j;
  });
  if (interpreter >= 0) target.scanCode(words.slice(interpreter + 1).join(" "), found);
  if (subscript >= 0 && depth < MAX_NESTING) {
    for (const arg of words.slice(subscript + 1).map(unmarkGlobs)) {
      if (arg.includes("[") && /[$`]/.test(arg)) collect(arg, found, depth + 1, target);
    }
  }
  if (runner < 0) return false;
  if (depth < MAX_NESTING) {
    for (const arg of words.slice(runner + 1)) {
      if (SHELL_SYNTAX.test(arg)) collect(arg, found, depth + 1, target);
    }
  }
  return true;
}

/**
 * The invocation starting at `words[j]`. Run by xargs (an earlier `xargs` word), its argv gains words
 * the command text does not show: each replacement string (`-I {}`) and an appended tail read as
 * XARGS_TAIL, so `xargs gh` and `xargs -I{} gh pr {}` hide their subcommand.
 */
function withXargsTail(words, j) {
  const x = words.findIndex((word) => programName(word) === "xargs");
  if (x < 0 || x > j) return words.slice(j);
  const replace = ["{}"];
  for (let k = x + 1; k < j; k += 1) {
    const word = words[k];
    if (word === "-I" || word === "-J") replace.push(words[(k += 1)] || "{}");
    else if (/^-[IJ]./.test(word)) replace.push(word.slice(2));
    else if (/^-i./.test(word) || word.startsWith("--replace=")) replace.push(word.replace(/^-i|^--replace=/, ""));
  }
  const shown = words.slice(j).map((word) => (replace.some((r) => r && word.includes(r)) ? XARGS_TAIL : word));
  return [...shown, XARGS_TAIL];
}

/** Index of the word a simple command runs, past keywords, precommand modifiers, their options and assignments. */
function commandStart(words) {
  let s = 0;
  while (
    s < words.length &&
    (BUILTIN_PREFIXES.has(words[s]) || ENVIRONMENT_ASSIGNMENT.test(words[s]) || (s > 0 && words[s].startsWith("-")))
  ) {
    s += 1;
  }
  return s;
}

/**
 * True when `words` runs a shell that takes its commands from stdin: `sh` or `bash -s` with no script,
 * `xargs sh -c` with the string left to xargs, or `source`/`.` reading stdin or `<(…)`.
 */
function readsStdinSource(words) {
  const s = commandStart(words);
  if ((words[s] === "source" || words[s] === ".") && words.length > s + 1) return STDIN_FILE.test(words[s + 1]);
  const r = words.findIndex((word) => STDIN_SHELLS.has(programName(word)));
  if (r < 0) return false;
  const args = words.slice(r + 1);
  for (let k = 0; k < args.length; k += 1) {
    const arg = args[k];
    if (SHELL_VALUE_OPTIONS.has(arg)) k += 1;
    else if (/^-[A-Za-z]*c[A-Za-z]*$/.test(arg)) return k === args.length - 1;
    else if (/^-[A-Za-z]*s/.test(arg)) return true;
    else if (!/^[-+]/.test(arg)) return false; // a script file
  }
  return true;
}

/** A push invocation for interpreter code naming git and a push-family command, carrying all its tokens. */
function scanGitCode(code, found) {
  if (CODE_GIT.test(code) && CODE_PUSH.test(code)) {
    found.push(["git", "push", ...code.split(CODE_SEPARATORS).filter(Boolean)]);
  }
}

/**
 * git's reading of text the lexer cannot follow: every raw segment's words, quoting stripped, globs
 * unquoted. Quoting the shells read differently (`unreadable`) may hide the program or subcommand
 * itself (`$'\x{67}it'`), so it also adds a `push --force` invocation.
 */
function scanRawSegments(command, found, unreadable) {
  for (const segment of coarseSegments(command)) {
    const words = segment
      .replace(/["'\\{},]/g, " ")
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .map(markGlobs);
    collectWords(words, found, MAX_NESTING, GIT);
  }
  if (unreadable) found.push(["git", "push", "--force"]);
}

// git keeps its own interpreter-code reading, its raw-segment fallback and the coarse pass of the
// pre-lexer hooks.
const GIT = { name: "git", scanCode: scanGitCode, unlexable: scanRawSegments, coarse: true };

/**
 * The target for any other program. Its words exist only where the lexer follows the quoting: text
 * it cannot lex, and interpreter code naming the program, set `approximate` instead of yielding a
 * guessed argv. Quote-stripped or code-split words shift positionals (`--title 'a b' create` reads
 * `b` where gh reads `create`), so a guess is not a superset and cannot fail closed.
 */
function programTarget(name) {
  const target = { name, coarse: false, approximate: false };
  target.scanCode = (code) => {
    if (code.split(CODE_SEPARATORS).some((token) => programName(token) === name)) target.approximate = true;
  };
  target.unlexable = () => {
    target.approximate = true;
  };
  return target;
}

/** Record on `target` that the command holds unreadable syntax (`reason`); the first reason found is kept. */
function markTargetUnreadable(target, reason) {
  target.unreadable = true;
  if (!target.unreadableReason) target.unreadableReason = reason || "";
}

/**
 * Push every invocation of `target` in `command` onto `found`. Returns the lexed command when the lexer read all
 * of it, or null when it fell back to the raw scan.
 */
function collect(command, found, depth, target) {
  const failure = {};
  const lexed = lexShell(command, true, failure);
  if (lexed === null) {
    if (failure.unreadable) markTargetUnreadable(target, failure.reason);
    target.unlexable(command, found, failure.unreadable);
    return null;
  }
  let runsSource = false;
  for (const words of lexed.commands) {
    if (collectWords(words, found, depth, target)) runsSource = true;
  }
  if (runsSource && depth < MAX_NESTING) {
    for (const body of lexed.heredocs) collect(body, found, depth + 1, target);
  }
  if (depth < MAX_NESTING && lexed.commands.some(readsStdinSource)) {
    // A shell reading stdin runs what the other commands print (`echo 'gh pr create' | sh`): their
    // arguments are shell source. printf and `echo -e` decode backslash escapes first, so text holding
    // one has no exact reading and fails closed.
    const printed = lexed.commands.map((words) => words.slice(1).map(unmarkGlobs).join(" "));
    if (printed.some((text) => text.includes("\\"))) {
      markTargetUnreadable(target, UNREADABLE_SYNTAX.printedBackslash);
      target.unlexable(command, found, true);
      return null;
    }
    for (const text of printed) collect(text, found, depth + 1, target);
  }
  return lexed;
}

// --- coarse pass: quoted heredoc bodies that are data ---

// Programs that only store or print what they are given: none runs its stdin or an argument as code,
// evaluates an argument's subscript (SUBSCRIPT_BUILTINS) or starts another program.
const DATA_PROGRAMS = new Set(["cat", "tee", "mkdir", "echo"]);
// git subcommands that read a message from stdin (`commit -F -`, `tag -F -`, `notes add -F -`) and run
// no program the command line names. Global options (`-c alias.x='!sh'`) leave the command unknown.
const GIT_DATA_SUBCOMMANDS = new Set(["commit", "tag", "notes"]);
// gh groups whose commands read stdin as a body (`--body-file -`, `--notes-file -`, `--input -`) and run
// no program; gh-write-guard.js gates their writes.
const GH_DATA_GROUPS = new Set(["pr", "issue", "release", "gist", "api"]);

/** True when a lexed simple command only stores, prints or sends the heredoc body it is given. */
function isDataCommand(words) {
  const name = programName(words[0]);
  if (DATA_PROGRAMS.has(name)) return true;
  if (name === "git") return GIT_DATA_SUBCOMMANDS.has(words[1]);
  if (name === "gh") return GH_DATA_GROUPS.has(words[1]);
  return false;
}

// A quoted heredoc operator as it opens in the coarse text: `<<'EOF'`, `<<-"EOF"`, `<<\EOF`.
const QUOTED_HEREDOC = /<<-?[ \t]*(?:'[^'\n]*'|"[^"\n]*"|\\[^ \t\n;&|<>()]+)/y;
// The commit-message idiom once its body is cut out: a double-quoted substitution holding only `cat` and a
// quoted heredoc (`git commit -m "$(cat <<'EOF'⏎…⏎EOF⏎)"`).
const MESSAGE_SUBSTITUTION = /"\$\([ \t]*cat[ \t]+<<-?[ \t]*(?:'[^'\n]*'|"[^"\n]*"|\\[^ \t\n;&|<>()"]+)\n[ \t]*\)"/y;
// A double-quoted string holding no expansion, escape or substitution.
const PLAIN_DOUBLE_QUOTED = /"[^"$`\\]*"/y;
// Unquoted characters opening a context where the lexer may read a heredoc the shells do not, or place its
// body elsewhere: `$[…]`, `${…}`, `$((…))`, `((…))`, `[[…]]` (where `<<` is a shift or text), backticks, a
// comment, an escape, a CR (the shells keep it in the delimiter word), a vertical tab, form feed or NUL.
const SKELETON_FORBIDDEN = /[$`()[\]{}\\#\r\v\f\0]/;

/** True when the sticky `pattern` matches `text` at `at`: the index after the match, or -1. */
function matchAt(pattern, text, at) {
  pattern.lastIndex = at;
  return pattern.test(text) ? pattern.lastIndex : -1;
}

/**
 * True when `skeleton` (a command with its quoted heredoc bodies cut out) holds only plain words, quotes,
 * operators and quoted heredoc operators, so every `<<` in it is a heredoc the shells read where the lexer does:
 * single-quoted text, double-quoted text without `$`, backtick or backslash (or the commit-message substitution),
 * a here-string `<<<`, and no SKELETON_FORBIDDEN character outside quotes. An unquoted-delimiter heredoc fails.
 */
function plainSkeleton(skeleton) {
  let i = 0;
  while (i >= 0 && i < skeleton.length) {
    const c = skeleton[i];
    if (c === "'") {
      const end = skeleton.indexOf("'", i + 1);
      i = end < 0 ? -1 : end + 1;
    } else if (c === '"') {
      const end = matchAt(MESSAGE_SUBSTITUTION, skeleton, i);
      i = end >= 0 ? end : matchAt(PLAIN_DOUBLE_QUOTED, skeleton, i);
    } else if (skeleton.startsWith("<<<", i)) {
      i += 3;
    } else if (skeleton.startsWith("<<", i)) {
      i = matchAt(QUOTED_HEREDOC, skeleton, i);
    } else {
      i = SKELETON_FORBIDDEN.test(c) ? -1 : i + 1;
    }
  }
  return i >= 0;
}

/**
 * The text the coarse pass reads: `command` without its quoted-delimiter heredoc bodies when the lexer read the
 * whole command, every simple command in it, substitutions included, is a data command, and what remains is a
 * plain skeleton — such a body is data to every shell (`cat > notes.md <<'EOF'` with a line `git push origin
 * main`). Otherwise `command` unchanged: a body a shell, an interpreter, a pipe consumer or a later script may
 * run, or one the lexer may have placed differently from the shells, keeps its raw reading.
 */
function coarseText(command, lexed) {
  if (lexed === null || !lexed.commands.every(isDataCommand)) return command;
  // Only spans of the command text itself: a backtick body is lexed as a string of its own.
  const bodies = lexed.quotedBodies.filter((body) => body.src === command).sort((a, b) => a.start - b.start);
  let skeleton = "";
  let at = 0;
  for (const body of bodies) {
    if (body.start < at) continue;
    skeleton += command.slice(at, body.start);
    at = body.end;
  }
  skeleton += command.slice(at);
  return plainSkeleton(skeleton) ? skeleton : command;
}

/**
 * Every invocation of `target` in `command`, from the program word on; unquoted globs still marked.
 * `info.unreadable` is set when the command holds quoting bash and zsh read differently, and
 * `info.unreadableReason` names that syntax (empty otherwise).
 */
function findInvocations(command, target, info = {}) {
  info.unreadable = false;
  info.unreadableReason = "";
  if (typeof command !== "string") return [];
  const found = [];
  const run = Object.create(target); // per-call state: GIT is shared across calls
  run.unreadable = false;
  run.unreadableReason = "";
  const lexed = collect(command, found, 0, run);
  info.unreadable = run.unreadable;
  info.unreadableReason = run.unreadableReason;
  if (target.coarse) {
    for (const segment of coarseSegments(run.unreadable ? command : coarseText(command, lexed))) {
      const tokens = coarseCommandTokens(segment);
      if (tokens.length && programName(tokens[0]) === target.name) found.push(tokens);
    }
  }
  return found;
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

/** The last inline value of configuration `key` (keys are lower-cased), or undefined. */
function configValue(config, key) {
  let value;
  for (const entry of config) {
    if (entry.key === key) value = entry.value;
  }
  return value;
}

/** True when inline configuration makes git run its guess for a mistyped subcommand. */
function autocorrects(config) {
  const value = configValue(config, "help.autocorrect");
  if (value === undefined) return false;
  return typeof value !== "string" || !AUTOCORRECT_OFF.has(value.toLowerCase());
}

/**
 * Push the literal reading of `args` and, when an argument after the subcommand holds an unquoted
 * glob, a second reading with each such argument read as GLOB_READING.
 */
function readArguments(args, readings) {
  readings.push(args.map(unmarkGlobs));
  if (args.slice(1).some((arg) => GLOB_MARKED.test(arg))) {
    readings.push(args.map((arg, k) => (k > 0 && GLOB_MARKED.test(arg) ? GLOB_READING : unmarkGlobs(arg))));
  }
}

/**
 * Append every reading of one git invocation to `readings`: its own subcommand and arguments, what
 * an inline alias of that subcommand expands to, and `push --force` under an enabling inline
 * help.autocorrect.
 */
function readInvocation(tokens, unknownTakesValue, inherited, depth, readings) {
  const config = [...inherited];
  const args = stripGitGlobalFlags(tokens, unknownTakesValue, config);
  readArguments(args, readings);
  // xargs appends words the text does not show: a hidden or push-family subcommand may force-push.
  if (tokens.includes(XARGS_TAIL) && (!args.length || args[0] === XARGS_TAIL || PUSH_FAMILY.test(args[0]))) {
    readings.push([...UNRESOLVED_READING]);
  }
  // git runs its guess for a mistyped subcommand, so any subcommand may run as push (`pus -f`).
  if (args.length && autocorrects(config)) readings.push([...UNRESOLVED_READING]);
  const alias = args.length ? configValue(config, `alias.${args[0].toLowerCase()}`) : undefined;
  if (typeof alias !== "string" && alias !== RUNTIME_VALUE) return;
  if (alias === RUNTIME_VALUE || depth >= MAX_ALIAS_DEPTH) {
    readings.push([...UNRESOLVED_READING]);
  } else if (alias.startsWith("!")) {
    // A shell alias runs its text through the shell with the remaining arguments appended.
    const nested = [];
    collect(`${alias.slice(1)} ${args.slice(1).join(" ")}`, nested, 0, GIT);
    for (const nestedTokens of nested) readInvocation(nestedTokens, unknownTakesValue, config, depth + 1, readings);
  } else {
    // git splits a plain alias itself and never expands a glob in it; `<` and `>` there are
    // arguments, not redirections (`alias.p='push > -f'` runs `push -f` with a `>` remote).
    const lexed = lexShell(alias, false);
    if (lexed === null) readings.push([...UNRESOLVED_READING]);
    else
      readInvocation(
        [tokens[0], ...lexed.commands.flat().map(unmarkGlobs), ...args.slice(1)],
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
 * global options, once per expansion of an inline alias and once more per glob reading, and may
 * repeat across passes; callers test with `some`/`filter`, never count.
 */
function gitSubcommandArgs(command) {
  const readings = [];
  for (const tokens of findInvocations(command, GIT)) {
    readInvocation(tokens, false, [], 0, readings);
    readInvocation(tokens, true, [], 0, readings);
  }
  return readings;
}

/**
 * Every invocation of program `name` (case and `.exe` ignored) in a Bash command, as its words from
 * the program word on, quoting and redirections removed (`gh -R "o/r" pr view 2>/dev/null` →
 * `["gh", "-R", "o/r", "pr", "view"]`). git invocations include the coarse pass and the raw-segment
 * fallback; aliases and global options are left to the caller. For any other program, null when the
 * command holds text the lexer cannot follow (unbalanced quoting, an unterminated substitution, an
 * oversized brace expansion, quoting bash and zsh read differently) or interpreter code naming the
 * program: no exact argv exists, so the caller fails closed. `info.unreadable` (optional object) is
 * set when quoting bash and zsh read differently was found, for any program: such quoting may hide
 * the program word itself; `info.unreadableReason` names that syntax ("" when there is none). May
 * repeat an invocation; callers test with `some`/`filter`, never count.
 */
function programInvocations(command, name, info = {}) {
  const program = String(name).toLowerCase();
  const target = program === GIT.name ? GIT : programTarget(program);
  const found = findInvocations(command, target, info);
  return target.approximate ? null : found.map((tokens) => tokens.map(unmarkGlobs));
}

module.exports = { gitSubcommandArgs, programInvocations, XARGS_TAIL };
