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
//   gh-write-guard.js ships in Foundry only, so this module stays local
//   with it; commit-guard.js and rule-inject.js also stay Foundry-only.
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
//      dropped with their target and an unquoted fd number (`2>`), decided while quoting is known:
//      a quoted `'>'` stays an argument and `'a>'&` still ends a command. A `{name}>` descriptor
//      variable is unreadable (step 5). A plain git alias is not shell, so `<` and `>` stay
//      arguments there.
//      Every word naming the program starts an invocation at any position, so no wrapper list
//      or wrapper option grammar is needed (`sudo -u git git push` yields both readings). The name
//      is the basename with case folded and `.exe` dropped — macOS and Windows file systems run
//      `GIT` and `Git.exe` as git; the same folding applies to SOURCE_RUNNERS names (`BASH -c`).
//      Only executable argument positions of SOURCE_RUNNERS are re-read: a shell's `-c` value, joined eval
//      operands, and env's split-string command (including its remaining argv). A script file's arguments and
//      another interpreter's code are data. A `-c` string is the first operand after `-c` and any options that
//      follow it (`bash -c -e SRC`). Heredoc bodies and here-string words are re-read when consumed by a shell or
//      a pipeline feeding one, or when the file the heredoc writes is a script the input runs through a shell or
//      `source` (`cat > w.sh <<'EOF' … bash w.sh`, matched by normalized literal path) (`bash <<'EOF'`, `cat <<'EOF' | sh`,
//      `bash <<< 'git push -f'`; `trap '…' EXIT`, `source /dev/stdin <<< '…'`). A shell reading its
//      commands from stdin (`sh`, `bash -s`, `xargs sh -c` with the string left to xargs,
//      `source <(…)`) runs what commands in that pipeline print only when its command receives input from a pipe
//      or input redirection. Only the command word, past modeled exec wrappers, selects a consumer; an argument
//      such as `bash` in `grep bash` is data. Literal printf / print -f `%s`, `%-Ns`, `%%` and `\n`, `\t`, `\\`
//      formats are decoded first. Echo permits only -n; print permits -n/-r. Their operands permit no backslash.
//      Runtime arguments and everything outside this closed grammar fail closed when printed as source (step 5).
//      Script paths discovered in nested source are shared until a bounded fixed point. An invocation run by xargs
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
//      older bash and zsh reject them. A `$` is read only in the forms the lexer models (DOLLAR_READ,
//      PARAMETER_READ: a name, digits, a special parameter, `$(`, `$((`, and a `${…}` that reads a
//      parameter with `:-` `:+` `-` `+` `#` `%` `/`, an offset, error-if-unset `:?`/`?`, bash case
//      modification `^` `^^` `,` `,,`, or the zsh split sigil `$=x`/`${=x}`, which splits a run-time
//      value as bash splits `$x`); any other form — zsh `$~x` `$^x` `${~x}`, indirect
//      `${!x}`, `$[…]`, an assigning `${x:=…}` or `${x::=…}`, the transform `${x@Q}`, a computed
//      subscript — is unreadable, unquoted, in double quotes or in a heredoc
//      body. So is a `{name}>` descriptor variable (bash 3.2 reads the word as an argument, bash
//      4.1+ and zsh assign the descriptor number to the name), a command that switches a shell
//      option outside SAFE_SHELL_OPTIONS and READING_NEUTRAL_OPTIONS (`setopt`, `unsetopt`, `shopt`,
//      `set -o`, `emulate`) before another command, a shell started with such an option that runs
//      a `-c` script or, as the command word, text fed to it (`zsh -o rcquotes -c …`, `echo … |
//      bash --posix`), and an `options[…]=`, `SHELLOPTS=` or `BASHOPTS=` assignment: the lexer
//      reads every later command, and that shell's script, with options at their defaults.
//      zsh syntax that runs code bash never reads is unreadable
//      too: a `${(flags)…}` expansion (`(e)` evaluates the value) anywhere, and a `(…)` closing an
//      unquoted word that holds a code-running glob qualifier (`.(e:'cmd':)`, `*(+name)`) — zsh is
//      the Bash tool's shell on macOS. A backslash-newline right after `$`, after `${`, inside a
//      heredoc delimiter, right after an unquoted `<` or `>`, or ending a line of an unquoted-delimiter
//      heredoc body is unreadable too: every shell splices the line before reading the opener or
//      comparing the line to the delimiter (`$\⏎'…'` is `$'…'`, `<\⏎<` is `<<`, `EO\⏎F` ends the
//      body), where the lexer decides on the raw text. A comment does not continue
//      (`# x \⏎git push` runs the push), so nothing is spliced ahead of lexing. Text printed to a
//      shell outside the closed printer grammar is unreadable as well (step 1). Since such quoting may hide
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
//   pre-existing script on disk, and an interpreter assembling the git words from fragments. Code consumers
//   fail closed on unknown generated output reaching them, matching modeled file writes, and modeled commands
//   shadowed by functions or aliases. Unknown programs are not inferred to write files. Literal print output and
//   literal cat/tee input written to a script are followed. Aliases are not
//   resolved through `git config`: running git inside the hook on every Bash call is not worth a
//   rare spelling. These string-visible spellings are not followed either, by decision — the
//   finder stays scoped to the review findings it fixes:
//     * zsh SH_WORD_SPLIT and `$=x`/`${=x}`/`${==x}` change only field splitting of expanded values
//       (zshoptions SH_WORD_SPLIT; zshexpn `${=spec}`), not which literal words execute. They are admitted
//       in either sense under the same run-time-value residual as plain `$x`; GLOB_SUBST and `${~x}` remain
//       unreadable because generated glob qualifiers can execute code.
//     * compound groups and substitutions in a pipeline share a conservative output scan. Commands in separate
//       top-level lists do not share it. The coarse git scan retains its existing script-file and heredoc readings.
//     * shell syntax only zsh has (`=git push`, a glob qualifier that runs no code): the lexer
//       reads bash. Code-running zsh syntax and every `$` form outside the admit set are
//       unreadable instead (step 5).
//     * a `${…}` operand holding a blank (`${X:-git push -f}`) is split at the blank where the
//       shells keep one expansion: its words are read as text, like any run-time value.
//     * shell options set before the command runs (an rc file, an exported SHELLOPTS): read as off.
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

const path = require("path");

// Programs that run a later argument as shell source (`bash -c SRC`, `eval SRC`, `env -S SRC`,
// `su -c SRC`, `watch SRC`, `ssh host SRC`). sourceArguments selects executable text; a shell's
// script-file operands and an ordinary env invocation's argv are data, never shell source.
const SOURCE_RUNNERS = new Set([
  "sh",
  "bash",
  "zsh",
  "dash",
  "ksh",
  "mksh",
  "ash",
  "fish",
  "nu",
  "pwsh",
  "csh",
  "tcsh",
  "xonsh",
  "elvish",
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
const STDIN_SHELLS = new Set([
  "sh",
  "bash",
  "zsh",
  "dash",
  "ksh",
  "mksh",
  "ash",
  "fish",
  "nu",
  "pwsh",
  "csh",
  "tcsh",
  "xonsh",
  "elvish",
]);
// Wrapper flags whose operand positions are modeled below. An unknown option keeps every later shell candidate:
// guessing whether it consumes the next word could hide the real stdin consumer. Ordinary program arguments
// never select a consumer.
const EXEC_WRAPPERS = {
  env: {
    flags: /^(?:-[i0v]+|--ignore-environment|--null|--debug)$/,
    values: /^(?:-[uCaS]|--unset|--chdir|--argv0|--split-string)$/,
  },
  sudo: {
    flags: /^(?:-[AbEHknPSsVv]+|--askpass|--background|--preserve-env|--set-home|--non-interactive|--stdin)$/,
    values: /^(?:-[CDgprtuU]|--close-from|--chdir|--group|--prompt|--chroot|--command-timeout|--user|--other-user)$/,
  },
  xargs: {
    flags: /^(?:-[0ortx]+|--null|--no-run-if-empty|--open-tty|--verbose|--exit)$/,
    values: /^(?:-[adEIJLnPs]|--arg-file|--delimiter|--eof|--replace|--max-lines|--max-args|--max-procs|--max-chars)$/,
  },
  time: { flags: /^(?:-[apqv]+|--append|--portability|--quiet|--verbose)$/, values: /^(?:-[fo]|--format|--output)$/ },
  nohup: { flags: /^$/, values: /^$/ },
  command: { flags: /^-p+$/, values: /^$/ },
  exec: { flags: /^-[cl]+$/, values: /^-a$/ },
  nice: { flags: /^-\d+$/, values: /^(?:-n|--adjustment)$/ },
  timeout: {
    flags: /^(?:-[v]+|--foreground|--preserve-status|--verbose)$/,
    values: /^(?:-[ks]|--kill-after|--signal)$/,
  },
  stdbuf: { flags: /^$/, values: /^(?:-[ioe]|--input|--output|--error)$/ },
  doas: { flags: /^-[Lns]+$/, values: /^-[Cu]$/ },
  setsid: { flags: /^(?:-[cfw]+|--ctty|--fork|--wait)$/, values: /^$/ },
  flock: {
    flags: /^(?:-[suxnoFv]+|--shared|--exclusive|--unlock|--nonblock|--nb|--close|--no-fork|--verbose)$/,
    values: /^(?:-[wE]|--wait|--timeout|--conflict-exit-code)$/,
  },
  caffeinate: { flags: /^-[dimsu]+$/, values: /^-[tw]$/ },
  ionice: { flags: /^(?:-t|--ignore)$/, values: /^(?:-[cnpPu]|--class|--classdata|--pid|--pgid|--uid)$/ },
  unbuffer: { flags: /^-p$/, values: /^$/ },
  busybox: { flags: /^$/, values: /^$/ },
  ssh: { flags: /^-[1246AaCfGgKkMNnqsTtVvXxYy]+$/, values: /^-[BbcDEeFIiJLlmOopQRSWw]$/ },
};
EXEC_WRAPPERS.parallel = EXEC_WRAPPERS.xargs;
// Shell startup options share the value grammar, including letters embedded in clusters (`bash -co pipefail`).
const SHELL_OPTIONS = {
  flags: /^[-+][A-Za-z]+$/,
  values: /^(?:[-+][oO]|--rcfile|--init-file)$/,
};
const FISH_OPTIONS = {
  flags: SHELL_OPTIONS.flags,
  values: /^(?:-[cC]|--command|--init-command)$/,
};
const PRINT_PROGRAMS = new Set(["echo", "printf", "print"]);
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
// Builtins whose first operand (the count) zsh evaluates as an arithmetic expression, which re-expands a command
// substitution stored in it by quoting (`exit '$(gh pr merge 1)'` runs gh). Other arithmetic-operand positions
// (printf numeric conversions, substring offsets, array-element keys, integer-parameter assignments) are a run-time
// re-expansion residual the lexer does not follow — see LIMITS; the blueprint and sentinel lanes refuse them.
const ARITHMETIC_COUNT_BUILTINS = new Set(["shift", "break", "continue", "return", "exit", "repeat"]);
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
// Shell source nested in shell source (`bash -c "sh -c '…'"`) is followed this deep, then refused.
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
// An unquoted number right before `<` or `>` naming the redirected file descriptor (`2>`): part of the
// operator, never an argument. A quoted or escaped one (`"2">x`, `\2>x`) stays a word, as in bash.
const FD_PREFIX = /^\d+$/;
// An unquoted brace group right before a redirection (`{fd}>`, `{IFS}<`), its braces still marked: bash 4.1+ and
// zsh allocate a descriptor and assign its number to the name, bash 3.2 reads the word as an argument. No one
// reading is right, so the command is unreadable.
const FD_VARIABLE = /^\uE000[^]*\uE002$/;
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

// An assignment word (`a=1`, `a[0]=1`, `a+=1`), a bare name, the builtins that take `NAME=(…)` arrays as
// operands, and the reserved words that may open a command before its assignments.
const ASSIGNMENT_WORD = /^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=/;
const NAME_WORD = /^[A-Za-z_][A-Za-z0-9_]*$/;
const ARRAY_DECLARERS = new Set(["typeset", "declare", "local", "export", "readonly"]);
// `time` is a reserved word in bash and zsh (`time [ pipeline ]`), so a `(` after it opens a subshell (`time (…)`).
const COMMAND_OPENERS = new Set(["if", "then", "elif", "else", "while", "until", "do", "!", "{", "time"]);
const COMPOUND_ENDS = { if: "fi", while: "done", until: "done", for: "done", select: "done", case: "esac" };
// Per-word facts the array-assignment test needs, kept beside each word: its `NAME=` prefix is unquoted (an
// assignment), and the word is written exactly as the shell reads it, with no quoting and no brace expansion (a
// reserved word or builtin name as written: `{if,if}` expands to two `if` words, neither of them reserved).
const WORD_ASSIGNS = 1;
const WORD_BARE = 2;
const WORD_LITERAL = 4;

/**
 * True when a `(` opening a word (none in progress) groups: at command position (no word yet, or only reserved
 * openers such as `if`, `!`, `{`), inside an open `[[ … ]]`, as a `case` pattern right after `case WORD in`, or as
 * the `()` of a function definition. Anywhere else, after a command word, a redirection operator or a group just
 * closed, zsh reads `(` as the start of a glob word, whose glued second group (`(*)(e:…:)`) runs code.
 */
function groupingPosition(words, facts, next) {
  if (next === ")") return true;
  const plain = words.map(unmarkGlobs);
  const bare = (k) => (facts[k] & WORD_BARE) !== 0;
  return (
    plain.every((w, k) => bare(k) && COMMAND_OPENERS.has(w)) ||
    (plain.at(-1) === "-p" &&
      bare(plain.length - 1) &&
      plain.slice(0, -1).every((w, k) => bare(k) && COMMAND_OPENERS.has(w)) &&
      plain.includes("time")) ||
    conditionOpen(plain, facts) ||
    plain.some(
      (word, k) =>
        word === "case" &&
        bare(k) &&
        plain.length === k + 3 &&
        plain[k + 2] === "in" &&
        bare(k + 2) &&
        plain.slice(0, k).every((prefix, j) => bare(j) && COMMAND_OPENERS.has(prefix)),
    )
  );
}

/**
 * True when `words` (globs unmarked) is a `[[` command, written as the reserved word, not yet closed by `]]`. Reserved
 * openers before it are passed over: `do [[ … ]]`, `if [[ … ]]`, `! [[ … ]]` open the test in the same simple command.
 */
function conditionOpen(plain, facts) {
  let s = 0;
  while (s < plain.length && (facts[s] & WORD_BARE) !== 0 && COMMAND_OPENERS.has(plain[s])) s += 1;
  return (
    s < plain.length &&
    (facts[s] & WORD_BARE) !== 0 &&
    plain[s] === "[[" &&
    !plain.some((w, k) => k > s && (facts[k] & WORD_BARE) !== 0 && w === "]]")
  );
}

/**
 * True when a `NAME=(` word starts an array assignment: before the command word (every earlier word, past
 * reserved words, is an assignment) or as an operand of a declaration builtin. Anywhere else zsh reads the
 * `(…)` as glob qualifiers. Judged on the words as written: a quoted `'X=1'` is the command word.
 */
function arrayAssignmentPosition(words, facts) {
  const plain = words.map(unmarkGlobs);
  let i = 0;
  while (i < plain.length && facts[i] & WORD_BARE && COMMAND_OPENERS.has(plain[i])) i += 1;
  while (i < plain.length && facts[i] & WORD_ASSIGNS) i += 1;
  if (i === plain.length) return true;
  if (!(facts[i] & WORD_BARE) || !ARRAY_DECLARERS.has(plain[i])) return false;
  return plain
    .slice(i + 1)
    .every(
      (w, k) =>
        facts[i + 1 + k] & WORD_ASSIGNS || (facts[i + 1 + k] & WORD_BARE && (w.startsWith("-") || NAME_WORD.test(w))),
    );
}

/**
 * Lex `src` from `start`, pushing each simple command's words onto `acc.commands` and each heredoc
 * body onto `acc.heredocs`. `inSubst` = the body of `$(`, `<(` or `>(`: stop after its closing `)`.
 * Unquoted redirections (`2>/dev/null`, `> out.json`, `2>&1`, `&>log`) are dropped with their target
 * when `acc.redirections` is set; otherwise `<` and `>` are ordinary characters.
 * Returns the index after the lexed text, or -1 when quoting or a substitution is unterminated.
 */
function lexFrom(src, start, inSubst, acc, parentPipeline = null, parentFed = false) {
  let words = [];
  let facts = []; // WORD_ASSIGNS / WORD_BARE per entry of `words`
  let texts = []; // original word spellings, before quote removal
  let wordStart = start;
  let wordSubstitutions = []; // commands whose stdout expands into the current argument
  let word = null;
  let quoted = false; // part of the current word came from quoting or an escape
  let literal = true; // printf output is exact only when no word depends on expansion
  let quotedFrom = -1; // length of the current word when its first quoted or escaped text arrived
  let depth = 0;
  let parameter = 0; // open `${…}` in this word: its braces, commas and globs are not expansions
  let hereString = false; // the next word follows `<<<`: stdin text, not an argument
  let redirectTarget = false; // the next word follows a redirection operator: a file, not an argument
  let redirectWrite = false;
  let redirectFd = "0";
  let redirectOperator = "";
  let redirectAt = start;
  let functionBodyPending = false;
  let groupClosed = false; // a `)` closed a grouping and no separator followed yet: a `(` now opens a glob word
  let functionParenAt = -1; // only a function signature's empty `()` leaves the following body at command position
  // index of the last grouping `(`, and of the last `|`: a `#` right after either is unsettled. Both start before the
  // input (-2, not -1), so a `#` opening the input is a comment, not a `#` after a `(` at index -1 (`# note\nls`).
  let groupOpenAt = -2;
  let pipeAt = -2;
  let broken = ""; // the unreadable syntax a word ended on (a `{fd}>` descriptor variable): the lexer stops there
  let braceDepth = 0;
  let pipeline = parentPipeline || { commands: [], piped: false, fed: new Set(), outputs: new Set() };
  if (!parentPipeline) acc.pipelines.push(pipeline);
  let feedsStdin = parentFed;
  let awaitingPipeCommand = false; // a newline immediately after `|` continues the same pipeline
  const inputGroups = []; // { fed, start, end }: inherited stdin and first command in each compound
  let closedGroup = []; // a trailing input redirection also feeds every command in the just-closed group
  const feedInput = () => {
    pipeline.fed.add(words);
    for (const command of closedGroup) pipeline.fed.add(command);
  };
  const closeInputGroup = () => {
    const group = inputGroups.pop();
    feedsStdin = group ? group.fed : parentFed;
    closedGroup = group ? pipeline.commands.slice(group.start) : [];
  };
  const endPipeline = () => {
    // Compound commands and substitution output can feed an enclosing pipe. Keep their commands together;
    // independent top-level lists get distinct inputs, so an unrelated later shell cannot execute earlier data.
    if (parentPipeline || inputGroups.length > 0) return;
    pipeline = { commands: [], piped: false, fed: new Set(), outputs: new Set() };
    acc.pipelines.push(pipeline);
  };
  const pending = [];
  const append = (text) => {
    word = (word === null ? "" : word) + text;
  };
  const appendQuoted = (text) => {
    if (quotedFrom < 0) quotedFrom = word === null ? 0 : word.length;
    append(text);
    quoted = true;
  };
  const appendUnquoted = (c) => {
    if (c === "$" || (c === "~" && (word === null || /[=:]$/.test(word)))) literal = false;
    const afterDollar = word !== null && word.endsWith("$"); // `${`, `$?`, `$*`, `$[`: parameters
    if (c === "{" && afterDollar) parameter += 1;
    else if (c === "}" && parameter > 0) parameter -= 1;
    else if (parameter === 0 && BRACE_MARKS[c]) c = BRACE_MARKS[c];
    else if (parameter === 0 && GLOB_MARKS[c] && !afterDollar) c = GLOB_MARKS[c];
    append(c);
  };
  const endWord = () => {
    for (const nested of wordSubstitutions) acc.substitutionOutputs.add(nested);
    if (word !== null && redirectTarget) {
      redirectTarget = false; // the redirection's file: no argument of the command
      acc.redirectFiles.push({
        words,
        file: unmarkGlobs(unmarkBraces(word)),
        text: src.slice(wordStart, i),
        literal: literal && !GLOB_MARKED.test(word),
        write: redirectWrite,
        fd: redirectFd,
        operator: redirectOperator,
        at: redirectAt,
        group: closedGroup,
      });
    } else if (word !== null && hereString) {
      // bash applies neither brace nor pathname expansion to a here-string word
      acc.heredocs.push({
        body: unmarkGlobs(unmarkBraces(word)),
        words,
        pipeline,
        group: closedGroup,
        literal,
        expands: true,
        fd: redirectFd,
        at: redirectAt,
      });
      if (redirectFd === "0") feedInput();
      hereString = false;
    } else if (word !== null) {
      const expanded = expandBraces(word);
      if (expanded === null) acc.overflow = true;
      else {
        const prefix = ASSIGNMENT_WORD.exec(unmarkGlobs(word));
        const fact =
          (prefix !== null && (quotedFrom < 0 || prefix[0].length <= quotedFrom) ? WORD_ASSIGNS : 0) |
          (quoted || expanded.length !== 1 ? 0 : WORD_BARE) |
          (literal && !GLOB_MARKED.test(word) ? WORD_LITERAL : 0);
        words.push(...expanded);
        facts.push(...expanded.map(() => fact));
        texts.push(...expanded.map(() => src.slice(wordStart, i)));
        const caseGroup = inputGroups.at(-1);
        const caseWord = caseGroup?.end === "esac" && caseGroup.phase !== "body";
        const caseEnd =
          caseWord &&
          !caseGroup.parenthesized &&
          caseGroup.phase === "pattern" &&
          expanded[0] === "esac" &&
          words.length === 1;
        if (caseWord && !caseEnd) {
          acc.caseData.add(words);
          // The subject and patterns are data, even when spelled like reserved compound openers.
          if (caseGroup.phase === "subject") caseGroup.phase = "in";
          else if (caseGroup.phase === "in" && expanded[0] === "in") caseGroup.phase = "pattern";
        } else if (fact & WORD_BARE && expanded.length === 1) {
          const atStart = words.slice(0, -1).every((w, k) => facts[k] & WORD_BARE && COMMAND_OPENERS.has(w));
          const declaration = commandStart(words);
          if (
            expanded[0] === "{" &&
            words.length === declaration + 3 &&
            words[declaration] === "function" &&
            NAME_WORD.test(words[declaration + 1])
          ) {
            acc.functions.add(words[declaration + 1]);
            words.splice(declaration, 2);
            facts.splice(declaration, 2);
            texts.splice(declaration, 2);
            functionBodyPending = true;
          }
          if (expanded[0] === "{" && commandStart(words) === words.length) {
            braceDepth += 1;
            inputGroups.push({
              fed: feedsStdin,
              start: pipeline.commands.length,
              end: "}",
              functionBody: functionBodyPending,
            });
            functionBodyPending = false;
          } else if (expanded[0] === "}" && braceDepth > 0 && commandStart(words) === words.length - 1) {
            braceDepth -= 1;
            closeInputGroup();
          } else if (atStart && Object.hasOwn(COMPOUND_ENDS, expanded[0])) {
            // A pipe into a keyword compound feeds its conditions and every body command, across separators.
            inputGroups.push({
              fed: feedsStdin,
              start: pipeline.commands.length,
              end: COMPOUND_ENDS[expanded[0]],
              phase: expanded[0] === "case" ? "subject" : "body",
            });
          } else if (atStart && inputGroups.at(-1)?.end === expanded[0]) {
            closeInputGroup();
          }
        }
      }
    }
    word = null;
    wordSubstitutions = [];
    quoted = false;
    literal = true;
    quotedFrom = -1;
    parameter = 0;
  };
  // Before a redirection: an unquoted fd number belongs to the operator; any other word ends there. An unquoted
  // brace group (`{fd}>`) is a descriptor variable the shells read differently, so the lexer stops there.
  const endWordBeforeRedirection = () => {
    const fd = word !== null && !quoted && FD_PREFIX.test(word) ? word : "";
    if (word !== null && !quoted && FD_VARIABLE.test(word)) {
      broken = UNREADABLE_SYNTAX.fdVariable;
      return;
    }
    if (!fd) {
      endWord();
      return "";
    }
    word = null;
    quotedFrom = -1;
    parameter = 0;
    return fd;
  };
  const endCommand = () => {
    endWord();
    hereString = false;
    redirectTarget = false;
    groupClosed = false;
    if (words.length) {
      acc.commands.push(words);
      if (inputGroups.some((group) => group.functionBody)) acc.functionCommands.add(words);
      acc.wordFacts.set(words, facts);
      acc.wordTexts.set(words, texts);
      pipeline.commands.push(words);
      if (feedsStdin) pipeline.fed.add(words);
      awaitingPipeCommand = false;
    }
    words = [];
    facts = [];
    texts = [];
    closedGroup = [];
  };
  let i = start;
  while (i < src.length) {
    if (broken) return unreadable(acc, broken);
    if (word === null) wordStart = i;
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
      const before = acc.commands.length;
      const read = readExpanding(src, i + 1, '"', acc, pipeline, feedsStdin || pipeline.fed.has(words));
      if (read === null) return -1;
      wordSubstitutions.push(...acc.commands.slice(before));
      appendQuoted(read.text);
      if (!read.literal) literal = false;
      i = read.end;
    } else if ((c === "$" || c === "<" || c === ">") && next === "(") {
      literal = false;
      if (c !== "$") pipeline.piped = true;
      if (c === ">") pipeline.outputs.add(words);
      if (c === "<") feedInput();
      const before = acc.commands.length;
      const end = lexFrom(
        src,
        i + 2,
        true,
        acc,
        pipeline,
        c === ">" || feedsStdin || (c === "$" && pipeline.fed.has(words)),
      );
      if (end < 0) return -1;
      if (c === "$") wordSubstitutions.push(...acc.commands.slice(before));
      append(src.slice(i, end));
      i = end;
    } else if (c === "`") {
      literal = false;
      const before = acc.commands.length;
      const end = readBackticks(src, i + 1, acc, false, pipeline, feedsStdin || pipeline.fed.has(words));
      if (end < 0) return -1;
      wordSubstitutions.push(...acc.commands.slice(before));
      append(src.slice(i, end));
      i = end;
    } else if (c === "#" && word === null && (groupOpenAt === i - 1 || pipeAt === i - 1)) {
      // right after a grouping `(` or a `|` bash reads a comment, but zsh may read `#` inside a pattern
      // (`[[ x == (#i)y ]]`, `case x in (a|#b)`) as a glob flag and run the rest of the line
      return unreadable(acc, UNREADABLE_SYNTAX.groupComment);
    } else if (c === "#" && word === null) {
      while (i < src.length && src[i] !== "\n") i += 1;
    } else if (acc.redirections && (c === "<" || c === ">") && next === "\\" && src[i + 2] === "\n") {
      // `<\⏎<EOF`: the shells splice the line first and read `<<`, the lexer would read two `<`
      return unreadable(acc, UNREADABLE_SYNTAX.continued);
    } else if (c === "<" && next === "<" && src[i + 2] === "<") {
      redirectAt = i;
      redirectFd = endWordBeforeRedirection() || "0";
      hereString = true;
      if (redirectFd === "0") feedInput();
      redirectTarget = false;
      i += 3;
    } else if (c === "<" && next === "<") {
      redirectAt = i;
      redirectFd = endWordBeforeRedirection() || "0";
      const read = readHeredocStart(src, i + 2);
      if (read === null) return -1;
      if (read.unreadable) return unreadable(acc, UNREADABLE_SYNTAX.heredocDelimiter);
      pending.push({ ...read.doc, words, pipeline, group: closedGroup, fd: redirectFd, at: redirectAt });
      if (redirectFd === "0") feedInput();
      i = read.end;
    } else if (acc.redirections && parameter === 0 && (c === "<" || c === ">" || (c === "&" && next === ">"))) {
      // Decided here, where quoting is known: a quoted `>` or `'a>'&` never reaches this branch.
      redirectAt = i;
      if (c === "&" && !(word !== null && !quoted && FD_VARIABLE.test(word))) {
        endWord(); // `&>` takes no fd number: `2&>x` passes `2` as an argument
        redirectFd = "1";
      } else redirectFd = endWordBeforeRedirection() || (c === "<" ? "0" : "1");
      if (c === "<" && redirectFd === "0") feedInput();
      redirectWrite = c !== "<";
      const end = redirectionEnd(src, i);
      redirectOperator = src.slice(i, end);
      i = end;
      hereString = false;
      redirectTarget = true;
    } else if (c === "\n") {
      endCommand();
      i = readHeredocBodies(src, i + 1, pending.splice(0), acc, inSubst);
      if (i < 0) return -1;
      if (!awaitingPipeCommand) {
        feedsStdin = inputGroups.length ? inputGroups[inputGroups.length - 1].fed : parentFed;
        endPipeline();
      }
    } else if (
      c === "(" &&
      word !== null &&
      // only a word with no quoted or escaped text is an assignment word (`'a'=(…)` is a command word)
      !(
        quotedFrom < 0 &&
        /^[A-Za-z_][A-Za-z0-9_]*\+?=$/.test(unmarkGlobs(word)) &&
        arrayAssignmentPosition(words, facts)
      ) &&
      src[i + 1] !== ")" &&
      !conditionOpen(words.map(unmarkGlobs), facts)
    ) {
      // zsh reads any `(…)` group attached to a word as glob qualifiers, and some run code (`.(e:'cmd':)`,
      // `*(N)(+name)`, any delimiter); bash rejects the word. Every attached group is refused rather than
      // matched by content. An array assignment `a=(…)` (before the command word or after a declaration
      // builtin), a function definition `name()` and a pattern inside a `[[ … ]]` written as the reserved word,
      // where zsh generates no file names, are no qualifier.
      return unreadable(acc, UNREADABLE_SYNTAX.zshQualifier);
    } else if (
      c === "(" &&
      word === null &&
      (groupClosed || redirectTarget || hereString || !groupingPosition(words, facts, src[i + 1]))
    ) {
      // a `(` opening a word after the command word starts a zsh glob word (`echo (*)(e:'cmd':)`), not a group
      return unreadable(acc, UNREADABLE_SYNTAX.wordStartParen);
    } else if (c === "(") {
      if (inputGroups.at(-1)?.phase === "pattern") {
        inputGroups.at(-1).parenthesized = true;
        endCommand();
        groupOpenAt = i;
        i += 1;
        continue;
      }
      const signature = word === null ? words : [...words, unmarkGlobs(word)];
      const signatureFacts = word === null ? facts : [...facts, quoted ? 0 : WORD_BARE];
      if (
        next === ")" &&
        NAME_WORD.test(signature[signature.length - 1] || "") &&
        signature.every(
          (w, k) =>
            signatureFacts[k] & WORD_BARE && (k === signature.length - 1 || COMMAND_OPENERS.has(w) || w === "function"),
        )
      ) {
        functionParenAt = i;
        acc.functions.add(signature.at(-1));
        functionBodyPending = true;
      }
      endCommand();
      inputGroups.push({ fed: feedsStdin, start: pipeline.commands.length, end: ")" });
      depth += 1;
      groupOpenAt = i;
      i += 1;
    } else if (c === ")") {
      endCommand();
      // An unparenthesized case pattern ends with `)`, but does not close its surrounding stdin scope.
      if (inputGroups.at(-1)?.phase === "pattern") {
        inputGroups.at(-1).phase = "body";
        inputGroups.at(-1).parenthesized = false;
        i += 1;
        continue;
      }
      if (inSubst && depth === 0) return broken ? unreadable(acc, broken) : i + 1;
      closeInputGroup();
      // Only a verified function signature is exempt: empty groups attached to an argument can precede a
      // code-running zsh qualifier (`ls name()(e:…:)`) and must keep the closed-group refusal.
      groupClosed = depth > 0 && functionParenAt !== i - 1;
      functionParenAt = -1;
      depth = Math.max(0, depth - 1);
      i += 1;
    } else if (c === ";" || c === "&" || c === "|") {
      endWord();
      const outputCommands = [...closedGroup, words];
      endCommand();
      if (c === ";" && (next === ";" || next === "&") && inputGroups.at(-1)?.end === "esac")
        inputGroups.at(-1).phase = "pattern";
      if (c === "|") pipeAt = i;
      if (c === "|" && next !== "|") {
        pipeline.piped = true;
        for (const command of outputCommands) pipeline.outputs.add(command);
        feedsStdin = true;
        awaitingPipeCommand = true;
      } else {
        feedsStdin = inputGroups.length ? inputGroups[inputGroups.length - 1].fed : parentFed;
        endPipeline();
      }
      i += (c === "|" && (next === "|" || next === "&")) || (c === "&" && next === "&") ? 2 : 1;
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
  return broken ? unreadable(acc, broken) : i;
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
function readExpanding(src, start, terminator, acc, pipeline = null, feedsStdin = false) {
  let text = "";
  let literal = true;
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
      const read = readExpanding(src, i + 1, '"', acc, pipeline, feedsStdin);
      if (read === null) return null;
      text += src.slice(i, read.end);
      i = read.end;
    } else if (c === terminator) {
      return { text, end: i + 1, literal };
    } else if (terminator === '"' && c === "$" && next === "{") {
      literal = false;
      parameter += 1;
      text += "${";
      i += 2;
    } else if (c === "}" && parameter > 0) {
      parameter -= 1;
      text += c;
      i += 1;
    } else if (c === "$" && next === "(") {
      literal = false;
      const end = lexFrom(src, i + 2, true, acc, pipeline, feedsStdin);
      if (end < 0) return null;
      text += src.slice(i, end);
      i = end;
    } else if (c === "`") {
      literal = false;
      const end = readBackticks(src, i + 1, acc, terminator === '"', pipeline, feedsStdin);
      if (end < 0) return null;
      text += src.slice(i, end);
      i = end;
    } else {
      if (c === "$") literal = false;
      text += c;
      i += 1;
    }
  }
  return terminator === null ? { text, end: i, literal } : null;
}

/**
 * Lex a backtick substitution's body; returns the index after the closing backtick, or -1. Inside
 * double quotes the shells also unescape `\"` in the body.
 */
function readBackticks(src, start, acc, inDoubleQuotes = false, pipeline = null, feedsStdin = false) {
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
  return lexFrom(body, 0, false, acc, pipeline, feedsStdin) < 0 ? -1 : i + 1;
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
// What may follow a `$` the lexer reads (admit set): a name or digit, a special parameter (`$?` `$#` `$@` `$*` `$$`
// `$!` `$-`), the zsh split sigil `=` before a name (`$=x`), `{` (judged by PARAMETER_READ), `(` (a substitution or
// `$((…))`), a quote (`$'…'` and `$"…"` have their own branches; inside double quotes or a heredoc body both are text),
// or a character that opens no expansion in bash or zsh, where the `$` is text. Anything else — zsh `$~x` `$^x` `$+x`,
// `$[…]` — is unreadable.
const DOLLAR_READ = /\$(?:[A-Za-z0-9_?#@*$!-]|=(?=[A-Za-z_])|['"({]|[ \t\n/.,:;|&)<>\\%}\]]|$)/y;
// A `${…}` the lexer reads: the zsh split sigil `=`/`==` (zshexpn: word splitting by the SH_WORD_SPLIT rules, "done
// by default in most other shells" — it splits a run-time value, as an unquoted bash `$X` does, and runs no code),
// before a nested `${`/`$(` or the usual head; an optional `#` (length), a name, digits or one special parameter, an
// optional `[@]`, `[*]` or digit subscript, then `}` or a reading operator (`:-` `:+` `:N` `-` `+` `#` `%` `/`;
// error-if-unset `:?`/`?`; bash case modification `^` `^^` `,` `,,`, text-only and a zsh error, so neither runs code).
// Indirection `${!x}`, the zsh sigils `${~x}` (GLOB_SUBST: the value is globbed, so a `(e:…:)` qualifier in it runs)
// and `${^x}`, flags `${(…)…}`, assigning `${x=…}` `${x:=…}` `${x::=…}`, the transform `${x@Q}` and any other subscript
// are unreadable.
const PARAMETER_READ =
  /\$\{(?:={1,2}(?=\$[{(])|(?:={1,2})?#?(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|[?#@*$!-])(?:\[(?:[@*]|[0-9]+)\])?(?:\}|:[-+?0-9 ]|[-+#%/?^,]))/y;
// What `info.unreadableReason` names for each kind of unreadable syntax, so a guard blocking the command
// can name the real cause instead of the push or gh write the command may not hold.
const UNREADABLE_SYNTAX = Object.freeze({
  dollars: "`$$` right before a quote, brace or parenthesis",
  commandBrace: "bash 5.3's `${ cmd; }` or `${|cmd;}`",
  continued: "a backslash-newline the shells splice before reading what follows it",
  zshFlags: "zsh `${(flags)…}` parameter flags",
  dollarForm:
    "a `$` form outside the plain ones (zsh `$~x`, `${~x}`, `$^x`, indirect `${!x}`, `$[…]`, an assigning " +
    "`${x:=…}` or `${x::=…}`, a subscript or operator the lexer does not read)",
  fdVariable: "a `{name}` descriptor variable on a redirection, which bash 3.2 reads as a word",
  runtimeOptions:
    "a shell option switched at run time (`setopt`, `unsetopt`, `shopt`, `set -o`, `emulate`, an interpreter " +
    "`-o`/`-O`, `options`/`SHELLOPTS`/`BASHOPTS`) outside pipefail, errexit, nounset and nomatch, which changes how " +
    "the shells read the rest of the input",
  ansiC: "a `$'…'` escape bash and zsh decode differently",
  locale: 'a `$"…"` locale string',
  heredocDelimiter: "a heredoc delimiter written with `$'…'`, `$\"…\"` or a backslash-newline",
  zshQualifier: "a `(` group attached to a word, which zsh reads as glob qualifiers (`(e:…:)`, `(+name)`)",
  wordStartParen: "a `(` opening a word after the command word, which zsh reads as a glob word (`(*)(e:…:)`)",
  groupComment: "a `#` right after a `(` or `|`, which zsh may read as a glob flag rather than a comment",
  quoteInExpansion: "a single quote inside a double-quoted `${…}`",
  printedSource: "printed shell source outside the decoded set",
  envSplit: "an env split-string escape or expansion without a fixed reading",
  sourceLimit: "shell source discovery exceeded its nesting or fixed-point limit",
  heredocInSubstitution:
    "a heredoc inside `$(…)` whose body could close the substitution early in bash 3.2 (an unbalanced `)` such " +
    "as a `1)` list item, a backtick or `$'`); pass the text by file instead (`git commit -F <file>`, `--body-file`)",
});

/**
 * The unreadable syntax a `$` at `i` opens (`${ cmd; }`, `$\⏎`, `${(flags)…}`, any form outside DOLLAR_READ and
 * PARAMETER_READ), or null.
 */
function unreadableExpansion(src, i) {
  if (COMMAND_BRACE.test(src.slice(i, i + 4))) return UNREADABLE_SYNTAX.commandBrace;
  if (CONTINUED_DOLLAR.test(src.slice(i, i + 3))) return UNREADABLE_SYNTAX.continued;
  if (ZSH_FLAGS.test(src.slice(i, i + 3))) return UNREADABLE_SYNTAX.zshFlags;
  const read = src[i + 1] === "{" ? PARAMETER_READ : DOLLAR_READ;
  read.lastIndex = i;
  return read.test(src) ? null : UNREADABLE_SYNTAX.dollarForm;
}

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
    acc.heredocs.push({
      body,
      words: doc.words,
      pipeline: doc.pipeline,
      group: doc.group,
      literal: doc.quoted || !/[$`]/.test(body),
      expands: !doc.quoted,
      fd: doc.fd,
      at: doc.at,
    });
    if (doc.quoted) acc.quotedBodies.push({ src, start: bodyStart, end: Math.min(i, src.length) });
    // An unquoted delimiter leaves `$(…)` and backticks in the body live.
    if (!doc.quoted) {
      const before = acc.commands.length;
      if (readExpanding(body, 0, null, acc, doc.pipeline) === null) return -1;
      for (const nested of acc.commands.slice(before)) acc.substitutionOutputs.add(nested);
    }
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
 * Shell source in which a command switches a shell option at run time (setsOptions) before another command, or
 * starts a shell with such an option (startsWithOptions), is unreadable: the lexer reads every later command, and a
 * shell's own script, with the options at their defaults.
 */
function lexShell(src, redirections = true, failure = {}) {
  const acc = {
    commands: [],
    wordFacts: new Map(),
    wordTexts: new Map(),
    substitutionOutputs: new Set(),
    functions: new Set(),
    functionCommands: new Set(),
    caseData: new Set(),
    pipelines: [],
    heredocs: [],
    redirectFiles: [], // redirection file, direction, owning command and enclosed compound commands
    quotedBodies: [],
    overflow: false,
    unreadable: false,
    unreadableReason: "",
    redirections,
  };
  if (lexFrom(src, 0, false, acc) >= 0 && !acc.overflow) {
    const { commands } = acc;
    const switching = redirections ? commands.findIndex(setsOptions) : -1;
    const starting =
      redirections &&
      acc.pipelines.some((pipeline) =>
        pipeline.commands.some((words) => startsWithOptions(words, pipeline.fed.has(words))),
      );
    if (!starting && (switching < 0 || switching === commands.length - 1)) return acc;
    markUnreadable(acc, UNREADABLE_SYNTAX.runtimeOptions);
  }
  failure.unreadable = acc.unreadable;
  failure.reason = acc.unreadableReason;
  return null;
}

// --- run-time shell options ---

// Options the lexer reads as fixed: every other one may change quoting, globbing or brace expansion (`setopt
// rcquotes`, `shopt -s extglob`, `setopt braceccl`). Compared as lexed, quotes resolved.
const SAFE_SHELL_OPTIONS = new Set(["pipefail", "errexit", "nounset", "nomatch"]);
// Options that change no quoting, globbing or word reading, so the lexer reads the rest of the input the same with
// them on or off: the safe four, the trace and verbose flags (bash errtrace/functrace/xtrace/verbose, zsh
// PUSHD_SILENT `-E` and CDABLE_VARS `-T`), and zsh SH_WORD_SPLIT (field splitting of unquoted parameter expansions,
// which bash always does and the lexer already reads as run-time text). Named as zsh normalizes them (zshoptions:
// "case insensitive and underscores are ignored"; a leading `no` inverts, once), so `setopt SH_WORD_SPLIT`,
// `sh_word_split` and `nonomatch` are each one listed option or its inversion, and both senses are reading-neutral.
// The blueprint and sentinel lanes still refuse them (a prompt, not a hard block); the gh/commit guards read past them
// so an ordinary `set -E`, `set -o errtrace` or `setopt SH_WORD_SPLIT` before a command is not hard-blocked (F2).
const READING_NEUTRAL_OPTIONS = new Set([
  ...SAFE_SHELL_OPTIONS,
  ...["unset", "errtrace", "functrace", "xtrace", "verbose", "shwordsplit"],
]);
function readableOption(name) {
  const normalized = name.toLowerCase().replace(/_/g, "");
  return (
    READING_NEUTRAL_OPTIONS.has(normalized) ||
    (normalized.startsWith("no") && READING_NEUTRAL_OPTIONS.has(normalized.slice(2)))
  );
}
const OPTION_SETTERS = new Set(["setopt", "unsetopt", "shopt"]);
// A `set` cluster of reading-neutral letters: exit-on-error `e`, unset `u`, trace `x`, allexport `a`, `-o`, and the
// trace/verbose `E`/`T`/`v` (zsh `-E` PUSHD_SILENT, `-T` CDABLE_VARS, `-v` VERBOSE — none changes reading). `-k`
// (zsh INTERACTIVE_COMMENTS) is excluded: it changes whether `#` starts a comment.
const SET_FLAG_CLUSTER = /^[-+][euxaoETv]+$/;
const SHELL_LONG_OPTIONS = new Set(["--", "--norc", "--noprofile", "--login"]);
// The lead of a lexed word that ends a shell's options: literal text no expansion, tilde or glob can turn into a flag.
const OPTION_OPERAND_LEAD = /^[A-Za-z0-9_./]/;
// Assignments that set options: zsh's `options` array and the variables bash reads its options from at start-up,
// judged where a word assigns: before the command word, or as an operand of a declaring builtin.
const OPTION_VARIABLE = /^(?:options|SHELLOPTS|BASHOPTS)(?:\[[^\]]*\])?\+?=/;
const OPTION_VARIABLE_DECLARERS = new Set(["export", "typeset", "declare", "local", "readonly"]);

/**
 * True when one simple command (lexed words) switches a shell option outside SAFE_SHELL_OPTIONS for the commands
 * after it: `setopt`, `unsetopt` or `shopt` with any other operand, `set` with a flag outside SET_FLAG_CLUSTER or
 * `-o` naming another option, or any `emulate`. Query-only forms do not change subsequent word reading.
 */
function setsOptions(words) {
  const plain = words.map(unmarkGlobs);
  const s = commandStart(plain);
  return plain[s] === "emulate" || (s < plain.length && optionUnmodeled(plain[s], plain.slice(s + 1)));
}

/**
 * True when one simple command starts a shell whose own script the lexer reads with other options: an assignment to
 * `options`, `SHELLOPTS` or `BASHOPTS` (before the command word or as a declaring builtin's operand), or a shell
 * interpreter word with an option outside SAFE_SHELL_OPTIONS (`zsh -o rcquotes -c …`, `bash -O extglob`) that runs
 * text the lexer reads: a `-c` script, or — as the command word itself — stdin it is fed (`feedsText`: a heredoc or
 * another command in the input). An unfed shell candidate without a `-c` script (`rg zsh --glob '*.md'`) and a shell
 * run for its own flags (`bash --version`) read no script, so they stay readable. Only modeled exec wrappers
 * expose a later program word; ordinary argv does not start another shell.
 */
function startsWithOptions(words, feedsText) {
  const plain = words.map(unmarkGlobs);
  // leading assignment words, subscripted ones included (`options[rcquotes]=on`), then a declaring builtin's operands
  let a = 0;
  while (a < plain.length && (BUILTIN_PREFIXES.has(plain[a]) || ASSIGNMENT_WORD.test(plain[a]))) a += 1;
  const assigning = [...plain.slice(0, a), ...(OPTION_VARIABLE_DECLARERS.has(plain[a]) ? plain.slice(a + 1) : [])];
  if (assigning.some((word) => OPTION_VARIABLE.test(word))) return true;
  return executableShells(plain).some((k) => {
    const rest = plain.slice(k + 1);
    const input = shellInput(rest, programName(plain[k]));
    if (programName(plain[k]) === "fish") {
      const options = rest.filter(
        (_, index) => !input.code?.some((code) => code.index === index || (!code.prefix && code.index === index + 1)),
      );
      return optionUnmodeled("sh", options) && (input.source.length > 0 || (feedsText && input.stdin));
    }
    if (programName(plain[k]) === "pwsh") return false;
    return optionUnmodeled("sh", rest) && (input.source.length > 0 || (feedsText && input.stdin));
  });
}

/** True when `head` (an option setter, `set` or a shell, given as "sh") with lexed `operands` leaves the safe options. */
function optionUnmodeled(head, operands) {
  if (OPTION_SETTERS.has(head)) {
    const flags = operands.filter((value) => /^-[supqo]+$/.test(value)).join("");
    if (
      head === "shopt" &&
      /[pq]/.test(flags) &&
      !/[su]/.test(flags) &&
      operands.every((value) => /^-[pqo]+$/.test(value) || OPTION_OPERAND_LEAD.test(value))
    )
      return false;
    return operands.some((value) => !readableOption(value) && !(head === "shopt" && /^-[supqo]+$/.test(value)));
  }
  if (head !== "set" && head !== "sh") return false;
  let index = 0;
  while (index < operands.length) {
    const value = operands[index];
    if (value === "--" || value === "-") return false;
    // the first operand ends the options only when its lead is literal text no expansion can turn into a flag; a
    // shell's run-time operand (`bash -c "$CMD"`, a script the lexer never reads) refuses only when option-like
    // words follow it, which its value could turn into options for a script the lexer does read
    if (!(value.startsWith("-") || value.startsWith("+"))) {
      if (OPTION_OPERAND_LEAD.test(value)) return false;
      return head === "set" || operands.slice(index + 1).some((word) => /^[-+]./.test(word));
    }
    if (head === "sh" && /^(?:--rcfile|--init-file)(?:=|$)/.test(value)) {
      index = programOption(operands, index, SHELL_OPTIONS).next;
    } else if (value.startsWith("--") || !/^[-+][A-Za-z]+$/.test(value)) {
      if (head === "set" || !SHELL_LONG_OPTIONS.has(value)) return true;
      index += 1;
    } else if (head === "set" && !SET_FLAG_CLUSTER.test(value)) {
      return true;
    } else {
      const option = programOption(operands, index, SHELL_OPTIONS);
      if (option.values.some((name) => !readableOption(name) && !(head === "set" && name === ""))) return true;
      index = option.next;
    }
  }
  return false;
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
 * word receives and those an interpreter's code names. Stdin source is selected separately by its consuming
 * command and pipeline; the presence of a runner alone never makes every heredoc on the line executable.
 */
function collectWords(words, found, depth, target, scripts = new Set(), facts = [], bindings = new Map(), texts = []) {
  words = literalPathCommands(words, facts, bindings);
  const runners = [];
  let interpreter = -1;
  let subscript = -1;
  words.forEach((word, j) => {
    const name = programName(word);
    if (name === target.name) found.push(withXargsTail(words, j));
    else if (
      SOURCE_RUNNERS.has(name) &&
      (name !== "." || j === commandStart(words)) &&
      (name !== "eval" || executablePrograms(words).includes(j))
    )
      runners.push(j);
    else if (interpreter < 0 && INTERPRETERS.test(name)) interpreter = j;
    else if (subscript < 0 && SUBSCRIPT_BUILTINS.has(unmarkGlobs(name))) subscript = j;
  });
  if (interpreter >= 0) target.scanCode(words.slice(interpreter + 1).join(" "), found);
  if (subscript >= 0 && depth < MAX_NESTING) {
    for (const arg of words.slice(subscript + 1).map(unmarkGlobs)) {
      if (arg.includes("[") && /[$`]/.test(arg)) collect(arg, found, depth + 1, target, scripts);
    }
  }
  const count = commandStart(words);
  if (depth < MAX_NESTING && ARITHMETIC_COUNT_BUILTINS.has(unmarkGlobs(words[count] || ""))) {
    // the first non-option operand is the count, evaluated as arithmetic: a quoted `$(…)`/backtick in it re-expands
    for (const word of words.slice(count + 1)) {
      if (word.startsWith("-") && word.length > 1) continue;
      const arg = unmarkGlobs(word);
      if (/\$\(|`/.test(arg)) collect(arg, found, depth + 1, target, scripts);
      break;
    }
  }
  for (const runner of runners) {
    if (depth >= MAX_NESTING) continue; // the raw fallback calls this helper to retain candidates, without recursion
    const failure = {};
    let resolved = words;
    const name = programName(words[runner]);
    if (executablePrograms(words).includes(runner) && (name === "eval" || STDIN_SHELLS.has(name))) {
      const input = name === "eval" ? null : shellInput(words.slice(runner + 1), name);
      const operands = name === "eval" ? words.slice(runner + 1) : input.source;
      resolved = [...words];
      for (let k = runner + 1; k < words.length; k += 1) {
        const code = input?.code?.find((code) => code.index === k - runner - 1);
        if ((!code && !operands.includes(words[k])) || facts[k] & WORD_LITERAL) continue;
        const text = consumerWord(
          code?.text ?? words[k],
          depth + 1,
          bindings,
          code?.startup ? "" : texts[k]?.slice(code?.prefix?.length || 0),
        );
        if (text === null) {
          markTargetUnreadable(target, UNREADABLE_SYNTAX.printedSource);
          target.unlexable(words[k], found, true);
          return;
        }
        resolved[k] = (code?.prefix || "") + text;
      }
    }
    const source = sourceArguments(resolved, runner, failure);
    if (failure.unreadable) {
      markTargetUnreadable(target, failure.unreadable);
      target.unlexable(words.join(" "), found, true);
    }
    if (source === null) continue;
    const wrappers = words.slice(0, runner + 1).map(programName);
    if (wrappers.some((word) => ["xargs", "parallel", "find"].includes(word))) {
      const replacements = ["{}"];
      for (let k = 0; k < runner; k += 1) {
        const match = /^(?:-[IJi]|--replace=)(.+)$/.exec(words[k]);
        if (match) replacements.push(match[1]);
        else if (["-I", "-J", "-i", "--replace"].includes(words[k])) replacements.push(words[++k] || "{}");
      }
      if (source.some((text) => joinedTemplate(text, replacements))) {
        markTargetUnreadable(target, "replacement joins shell source words");
        target.unlexable(words.join(" "), found, true);
        return;
      }
    }
    for (const arg of source) {
      collect(arg, found, depth + 1, target, scripts, bindings, true);
    }
  }
}

/** Refuse replacement fragments that can construct command names across the template/input boundary. */
function joinedTemplate(source, replacements, depth = 0, fragmentsOnly = true) {
  if (depth >= MAX_NESTING) return true;
  const lexed = lexShell(source);
  if (!lexed) return true;
  return lexed.commands.some((words) =>
    words.some((word, r) => {
      if (
        executablePrograms(words).includes(r) &&
        replacements.some((replacement) => {
          if (!fragmentsOnly) return replacement && word.includes(replacement);
          if (!replacement || word === replacement) return false;
          let at = word.indexOf(replacement);
          while (at >= 0) {
            if (/[\w$]/.test(word[at - 1] || "") || /[\w$]/.test(word[at + replacement.length] || "")) return true;
            at = word.indexOf(replacement, at + replacement.length);
          }
          return false;
        })
      )
        return true;
      return (
        SOURCE_RUNNERS.has(programName(word)) &&
        (sourceArguments(words, r) || []).some((nested) =>
          joinedTemplate(nested, replacements, depth + 1, fragmentsOnly),
        )
      );
    }),
  );
}

/**
 * The arguments of the runner at `words[r]` that may be shell source, or null when it takes no source from the input.
 * `env` runs its program with plain argv; only an `-S`/`--split-string` value is split into a command line (that
 * program, when a runner itself, is a runner word of its own: `env bash -c '…'`). A shell runs a later argument only
 * with a `-c` option before its first operand; only that option's value is source. `eval` joins its operands before
 * reading them. Other source runners retain their conservative argument scan.
 */
function sourceArguments(words, r, failure = {}) {
  const start = commandStart(words);
  const positions = executablePrograms(words);
  const owner = positions.length === 1 ? positions[0] : start;
  const command = programName(words[owner] || "");
  // A known shell or interpreter owns its remaining argv. Runner names appearing in its data arguments do not
  // start another execution context; substitutions were already read by the outer lexer, and code is scanned below.
  if (r > owner && (STDIN_SHELLS.has(command) || INTERPRETERS.test(command) || PRINT_PROGRAMS.has(command))) return [];
  const name = programName(words[r]);
  const rest = words.slice(r + 1).map(unmarkGlobs);
  if (name === "env") {
    const split = [];
    for (let k = 0; k < rest.length; k += 1) {
      const arg = rest[k];
      if (arg === "--") break;
      if (!arg.startsWith("-")) {
        if (!ENVIRONMENT_ASSIGNMENT.test(arg)) break;
        continue;
      }
      const option = programOption(rest, k, EXEC_WRAPPERS.env);
      if (option.unknown) return rest; // an unmodeled option must not hide a later executable candidate
      k = option.next - 1;
      if (option.name !== "-S" && option.name !== "--split-string") continue;
      const source = envSplitSource(option.value || "");
      if (source === null) {
        failure.unreadable = UNREADABLE_SYNTAX.envSplit;
        return [];
      }
      // env appends its remaining argv after the split words. Preserve each trailing argument as one word when
      // reading that synthetic command, so `env -Sbash -c '…'` exposes the shell script without re-reading its data.
      const tail = rest
        .slice(k + 1)
        .map((word) => "'" + word.replace(/'/g, "'\\''") + "'")
        .join(" ");
      split.push(`${source} ${tail}`);
      break; // the remaining argv belongs to the split command
    }
    return split.length ? split.filter(Boolean) : null;
  }
  if (STDIN_SHELLS.has(name)) return executableShells(words).includes(r) ? shellInput(rest, name).source : [];
  if (name === "eval") return [rest.join(" ")];
  if (name === "source" || name === ".") return [];
  return rest;
}

/** Read one program option, consuming its value in a cluster, an attached value, or the following word. */
function programOption(args, index, options) {
  const arg = args[index];
  const equal = arg.indexOf("=");
  const name = equal < 0 ? arg : arg.slice(0, equal);
  const result = { name, next: index + 1, flags: [], value: "", values: [], unknown: false };
  if (arg === "--" || arg === "-") return result;
  // The old xargs spellings and these long options take optional attached values only.
  if (options === EXEC_WRAPPERS.xargs && ["--eof", "--replace", "--max-lines"].includes(arg)) return result;
  if (arg.startsWith("--")) {
    if (options.values.test(name)) {
      result.value = equal < 0 ? args[index + 1] || "" : arg.slice(equal + 1);
      result.next += equal < 0 ? 1 : 0;
    } else result.unknown = !options.flags.test(arg);
    return result;
  }
  for (let k = 1; k < arg.length; k += 1) {
    const flag = arg[0] + arg[k];
    result.flags.push(arg[k]);
    if (options === EXEC_WRAPPERS.xargs && /[eil]/.test(arg[k])) {
      result.name = flag;
      result.value = arg.slice(k + 1) || (arg[k] === "i" ? "{}" : "");
      return result;
    }
    if (options.values.test(flag)) {
      result.name = flag;
      if (options === SHELL_OPTIONS) {
        // Shell value letters consume the next word; later letters in the same cluster remain flags.
        result.value = args[result.next++] || "";
        result.values.push(result.value);
        continue;
      }
      result.value = arg.slice(k + 1).replace(/^=/, "") || args[index + 1] || "";
      result.next += k === arg.length - 1 ? 1 : 0;
      return result;
    }
    if (!options.flags.test(flag)) {
      result.unknown = true;
      return result;
    }
  }
  return result;
}

/** Split env's own quoting and escapes, then quote each resulting argv word for the shell-source reader. */
function envSplitSource(source) {
  let quote = "";
  let word = null;
  const words = [];
  const endWord = () => {
    if (word !== null) words.push(word);
    word = null;
  };
  const escapes = { t: "\t", n: "\n", v: "\v", f: "\f", r: "\r", "#": "#", $: "$", "\\": "\\", '"': '"', "'": "'" };
  for (let k = 0; k < source.length; k += 1) {
    const c = source[k];
    if (c === "\\" && (quote !== "'" || "\\'".includes(source[k + 1] || "\0"))) {
      const next = source[++k];
      if (next === "c" && quote !== '"') break;
      if (next === "_" && !quote) {
        endWord();
        continue;
      }
      const decoded = next === "_" ? " " : escapes[next];
      if (decoded === undefined) return null;
      word = (word ?? "") + decoded;
    } else if (!quote && /[ \t\n\v\f\r]/.test(c)) {
      endWord();
    } else if (!quote && c === "#" && word === null) {
      break;
    } else if (c === quote) {
      quote = "";
    } else if (!quote && (c === "'" || c === '"')) {
      quote = c;
      word ??= "";
    } else {
      // env expands ${NAME} itself; a runtime value cannot stand in for a literal executable or source string.
      if (c === "$" && quote !== "'") return null;
      word = (word ?? "") + c;
    }
  }
  if (quote) return null;
  endWord();
  return words.map((arg) => "'" + arg.replace(/'/g, "'\\''") + "'").join(" ");
}

/**
 * The shell's executable argument, whether its invocation reads source on stdin, and the script file it runs ("" for
 * none), past startup options.
 */
function shellInput(args, name = "sh") {
  if (name === "fish") {
    const source = [];
    const code = [];
    for (let k = 0; k < args.length; k += 1) {
      if (args[k] === "--" || !args[k].startsWith("-"))
        return {
          source,
          code,
          stdin: false,
          script: args[k] === "--" ? args[k + 1] || "" : args[k],
          scriptIndex: args[k] === "--" ? k + 1 : k,
        };
      const option = programOption(args, k, FISH_OPTIONS);
      if (["-c", "-C", "--command", "--init-command"].includes(option.name)) {
        const index = option.next - 1;
        const attached = index === k;
        const prefix = attached ? args[k].slice(0, args[k].length - option.value.length) : "";
        source.push(option.value);
        code.push({ index, text: option.value, prefix, startup: ["-C", "--init-command"].includes(option.name) });
      }
      k = option.next - 1;
    }
    return { source, code, stdin: !code.some((input) => !input.startup), script: "" };
  }
  let stdin = false;
  let command = false;
  for (let k = 0; k < args.length; k += 1) {
    const arg = args[k];
    if (arg === "--version" || arg === "--help") return { source: [], stdin: false, script: "" };
    if (arg === "--" || arg === "-") {
      if (command) return { source: args.slice(k + 1, k + 2), stdin: k + 1 >= args.length, script: "" };
      const fromStdin = stdin || k === args.length - 1 || STDIN_FILE.test(args[k + 1]);
      return { source: [], stdin: fromStdin, script: fromStdin ? "" : args[k + 1], scriptIndex: k + 1 };
    } else if (!/^[-+]/.test(arg)) {
      if (command) return { source: [arg], stdin: false, script: "" };
      const fromStdin = stdin || STDIN_FILE.test(arg);
      return { source: [], stdin: fromStdin, script: fromStdin ? "" : arg, scriptIndex: k };
    }
    if (name === "pwsh" && /^-(?:command|c)$/i.test(arg))
      return { source: [args.slice(k + 1).join(" ")], stdin: false, script: "" };
    const option = programOption(args, k, SHELL_OPTIONS);
    if (arg[0] === "-" && option.flags.includes("c")) command = true;
    if (arg[0] === "-" && option.flags.includes("s")) stdin = true;
    if (arg === "--command") command = true;
    if (arg.startsWith("--command="))
      return {
        source: [arg.slice(10)],
        stdin: false,
        script: "",
        code: [{ index: k, text: arg.slice(10), prefix: "--command=" }],
      };
    k = option.next - 1;
  }
  return { source: [], stdin: true, script: "" };
}

/** Stable path identity; runtime-only paths retain spelling while literal expansion suffixes normalize. */
function scriptPath(file, literal, command = false, raw = file) {
  const text = literal && !command ? path.posix.normalize(unmarkGlobs(file)) : unmarkGlobs(file);
  const tool = command && /^(?:~(?:\/|$)|\/usr\/|\/bin\/)|(?:^|\/)\.venv\/|(?:^|\/)bin\/[^/]+/.test(text);
  // A literal suffix still supplies a static path identity. Entirely runtime words need exact spelling:
  // neither different variables nor different quoting establish that their values name the same file.
  const expansion = /^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))(.*)$/.exec(text);
  const suffix = expansion && expansion[3] && !/[$`*?\[\]]/.test(expansion[3]);
  return (tool ? (literal ? "T" : "V") : literal ? "L" : "E") + (literal || suffix ? text : raw);
}

/** Retain literal assignments and finite for-list values at each command, without evaluating shell text. */
function commandBindings(lexed, inherited = new Map()) {
  const current = new Map(inherited);
  const bindings = new Map();
  // Substitution commands are flattened into this reading, but their assignments do not update the caller.
  // A name written there remains unknown until substitution-local variable scope is modeled.
  const nestedWrites = new Set();
  for (const words of lexed.substitutionOutputs) {
    const facts = lexed.wordFacts.get(words);
    words.forEach((word, k) => {
      if (facts[k] & WORD_ASSIGNS) nestedWrites.add(/^([A-Za-z_][A-Za-z0-9_]*)/.exec(word)?.[1]);
    });
  }
  for (const name of nestedWrites) current.delete(name);
  for (const words of lexed.commands) {
    const facts = lexed.wordFacts.get(words);
    const start = commandStart(words);
    const declaration = ARRAY_DECLARERS.has(words[start]);
    words.forEach((word, k) => {
      if (k >= start && !declaration) return;
      if (!(facts[k] & WORD_ASSIGNS)) return;
      const name = /^([A-Za-z_][A-Za-z0-9_]*)/.exec(word)?.[1];
      const match = /^([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(word);
      if (match && facts[k] & WORD_LITERAL && !nestedWrites.has(name)) current.set(name, [match[2]]);
      else current.delete(name);
    });
    if (words[start] === "for" && NAME_WORD.test(words[start + 1] || "")) {
      const values = words.slice(start + 3);
      if (words[start + 2] === "in" && values.every((_, k) => facts[start + 3 + k] & WORD_LITERAL))
        current.set(words[start + 1], values);
      else current.delete(words[start + 1]);
    }
    if (["read", "unset"].includes(words[start])) {
      for (const word of words.slice(start + 1)) if (NAME_WORD.test(word)) current.delete(word);
    }
    if (words[start] === "printf" && words[start + 1] === "-v") current.delete(words[start + 2]);
    bindings.set(words, new Map(current));
    if (["eval", "source", "."].includes(words[start])) current.clear();
  }
  return bindings;
}

/** Expand only a known leading variable with a literal suffix, retaining all finite loop alternatives. */
function boundPaths(file, literal, bindings, command = true, raw = file) {
  if (!literal) {
    const match = /^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))([^$`*?\[\]]*)$/.exec(file);
    const values = match && bindings?.get(match[1] || match[2]);
    if (values) return values.map((value) => scriptPath(value + match[3], true, command));
  }
  return [scriptPath(file, literal, command, raw)];
}

/** Resolve a single known path in an executable position, so a variable-held shell still owns its source. */
function literalPathCommands(words, facts, bindings) {
  const positions = executablePrograms(words);
  return words.map((word, k) => {
    if (!positions.includes(k) || facts?.[k] & WORD_LITERAL) return word;
    const paths = boundPaths(word, false, bindings);
    if (paths.length !== 1 || !["L", "T"].includes(paths[0][0]) || !paths[0].includes("/")) return word;
    return paths[0].slice(1);
  });
}

/** Discover script paths in argv and nested executable text before any heredoc is classified as data. */
function scriptOperands(words, facts, depth = 0, bindings = new Map(), texts = []) {
  words = literalPathCommands(words, facts, bindings);
  const s = commandStart(words);
  const files = [];
  const positions = executablePrograms(words);
  for (const r of positions) {
    if (!STDIN_FILE.test(words[r]))
      files.push(
        ...boundPaths(words[r], facts?.[r] & WORD_LITERAL, bindings, true, texts[r]).filter(
          (file) => file.includes("/") || (file[0] === "E" && /[$`]/.test(file)),
        ),
      );
    if (words[r] !== "source" && words[r] !== ".") continue;
    const index = r + (words[r + 1] === "--" ? 2 : 1);
    const file = words[index] || "";
    if (file && !STDIN_FILE.test(file))
      files.push(...boundPaths(file, facts?.[index] & WORD_LITERAL, bindings, true, texts[index]));
  }
  for (const r of executableShells(words)) {
    for (let k = r + 1; k < words.length; k += 1) {
      if (!words[k].startsWith("-")) break;
      const option = programOption(words, k, SHELL_OPTIONS);
      if (["--rcfile", "--init-file"].includes(option.name))
        files.push(
          ...boundPaths(
            option.value,
            facts?.[option.next - 1] & WORD_LITERAL,
            bindings,
            true,
            texts[option.next - 1]?.replace(/^--[^=]+=/, ""),
          ),
        );
      k = option.next - 1;
    }
  }
  words.forEach((word, k) => {
    const match = /^(?:ENV|BASH_ENV|ZDOTDIR)=(.*)$/.exec(word);
    if (!match) return;
    if (word.startsWith("ZDOTDIR=")) {
      for (const file of [".zshenv", ".zprofile", ".zshrc", ".zlogin"])
        files.push(
          ...boundPaths(
            `${match[1]}/${file}`,
            facts?.[k] & WORD_LITERAL,
            bindings,
            true,
            `${texts[k]?.replace(/^ZDOTDIR=/, "")}/${file}`,
          ),
        );
    } else
      files.push(...boundPaths(match[1], facts?.[k] & WORD_LITERAL, bindings, true, texts[k]?.replace(/^[^=]+=/, "")));
  });
  files.push(
    ...executableShells(words)
      .flatMap((r) => {
        const input = shellInput(words.slice(r + 1), programName(words[r]));
        return input.script
          ? boundPaths(
              input.script,
              facts?.[r + 1 + input.scriptIndex] & WORD_LITERAL,
              bindings,
              true,
              texts[r + 1 + input.scriptIndex],
            )
          : [];
      })
      .filter(Boolean),
  );
  if (depth >= MAX_NESTING) return files;
  words.forEach((word, r) => {
    if (
      !SOURCE_RUNNERS.has(programName(word)) ||
      (word === "." && r !== s) ||
      (word === "eval" && !positions.includes(r))
    )
      return;
    let resolved = words;
    const name = programName(word);
    if (executablePrograms(words).includes(r) && (name === "eval" || STDIN_SHELLS.has(name))) {
      const input = name === "eval" ? null : shellInput(words.slice(r + 1), name);
      const operands = name === "eval" ? words.slice(r + 1) : input.source;
      resolved = [...words];
      for (let k = r + 1; k < words.length; k += 1) {
        const code = input?.code?.find((code) => code.index === k - r - 1);
        if ((!code && !operands.includes(words[k])) || facts?.[k] & WORD_LITERAL) continue;
        const text = consumerWord(
          code?.text ?? words[k],
          depth + 1,
          bindings,
          code?.startup ? "" : texts[k]?.slice(code?.prefix?.length || 0),
        );
        if (text === null) return; // collectWords records the unknown eval/-c input, not fictitious file consumers
        resolved[k] = (code?.prefix || "") + text;
      }
    }
    for (const text of sourceArguments(resolved, r) || []) {
      if (!SHELL_SYNTAX.test(text)) continue;
      const lexed = lexShell(text);
      if (lexed) {
        const nestedBindings = commandBindings(lexed, bindings);
        files.push(
          ...lexed.commands.flatMap((nested) =>
            scriptOperands(
              nested,
              lexed.wordFacts.get(nested),
              depth + 1,
              nestedBindings.get(nested),
              lexed.wordTexts.get(nested),
            ),
          ),
        );
      }
    }
  });
  return files;
}

/** File targets of redirects and modeled writers, preserving literal versus expanded path provenance. */
function writtenPaths(lexed, words) {
  const bindings = lexed.pathBindings?.get(words);
  const texts = lexed.wordTexts.get(words);
  const targets = (file, literal, raw) => boundPaths(file, literal, bindings, false, raw);
  const written = lexed.redirectFiles
    .filter((r) => r.write && (r.words === words || r.group.includes(words)))
    .flatMap((r) => targets(r.file, r.literal, r.text));
  const facts = lexed.wordFacts.get(words);
  for (const s of executablePrograms(words)) {
    const name = programName(words[s]);
    if (!["tee", "dd", "cp", "mv", "install", "ln", "sed", "perl"].includes(name)) continue;
    const inPlace = name === "sed" ? /^(?:--in-place(?:=|$)|-[nEruslz]*i)/ : /^-[0acdlnpPsStTuUvVwWX]*i/;
    if (["sed", "perl"].includes(name) && !words.slice(s + 1).some((word) => inPlace.test(word))) continue;
    const runtimeArgs = words.slice(0, s).some((word) => ["xargs", "parallel"].includes(programName(word)));
    // Input wrappers may supply the final target or replace any shown operand. This is a possible expanded
    // target, never evidence that an unrelated literal tool executable has been overwritten.
    if (runtimeArgs) written.push("U");
    if (name === "tee") {
      let options = true;
      for (let k = s + 1; k < words.length; k += 1) {
        if (options && words[k] === "--") options = false;
        else if (!options || !words[k].startsWith("-"))
          written.push(...targets(words[k], facts?.[k] & WORD_LITERAL, texts[k]));
      }
    }
    if (name === "dd")
      words.slice(s + 1).forEach((word, k) => {
        if (word.startsWith("of="))
          written.push(
            ...targets(word.slice(3), facts?.[s + 1 + k] & WORD_LITERAL, texts[s + 1 + k]?.replace(/^of=/, "")),
          );
      });
    if (["cp", "mv", "install", "ln"].includes(name) && words.length > s + 2) {
      const directory = words.findIndex((word, k) => k > s && (word === "-t" || word === "--target-directory"));
      const attached = words.findIndex((word, k) => k > s && /^(?:-t.|--target-directory=)/.test(word));
      if (directory >= 0 && words[directory + 1])
        written.push(...targets(words[directory + 1], facts?.[directory + 1] & WORD_LITERAL, texts[directory + 1]));
      else if (attached >= 0)
        written.push(
          ...targets(
            words[attached].replace(/^(?:-t|--target-directory=)/, ""),
            facts?.[attached] & WORD_LITERAL,
            texts[attached]?.replace(/^(?:-t|--target-directory=)/, ""),
          ),
        );
      else written.push(...targets(words.at(-1), facts?.at(-1) & WORD_LITERAL, texts.at(-1)));
    }
    if (!["sed", "perl"].includes(name)) continue;
    let program = false;
    let options = true;
    for (let k = s + 1; k < words.length; k += 1) {
      const word = words[k];
      if (options && word === "--") {
        options = false;
        continue;
      }
      if (options && word.startsWith("-")) {
        if (inPlace.test(word)) continue;
        const expression = name === "sed" ? /^(?:-[nEruslz]*[ef]|--(?:expression|file)(?:=|$))/ : /^-[lnpwW]*[eE]/;
        if (expression.test(word)) {
          program = true;
          if (!word.includes("=") && (word.startsWith("--") || /[efE]$/.test(word))) k += 1;
        }
        continue;
      }
      if (!program)
        program = true; // the first positional operand is the editing program
      else written.push(...targets(word, facts?.[k] & WORD_LITERAL, texts[k]));
    }
  }
  return written;
}

/** Match modeled writes against every file consumer, retaining uncertainty across expanded paths. */
function writesRunScript(lexed, words, scripts) {
  return writtenPaths(lexed, words).some((file) =>
    [...scripts].some((script) => {
      if (file[0] === "L") return path.posix.normalize(file.slice(1)) === path.posix.normalize(script.slice(1));
      if (script[0] === "T" || script[0] === "V") return false;
      if (file[0] === "U") return true; // a modeled writer under xargs can receive an unshown target
      if (file === script) return true;
      if (script[0] !== "E") return false;
      const expansion = /^\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))(.*)$/;
      const sink = expansion.exec(file.slice(1));
      const input = expansion.exec(script.slice(1));
      return Boolean(
        sink &&
        input &&
        sink[3] &&
        input[3] &&
        (sink[1] || sink[2]) === (input[1] || input[2]) &&
        !/[$`*?\[\]]/.test(sink[3] + input[3]) &&
        path.posix.normalize("." + sink[3]) === path.posix.normalize("." + input[3]),
      );
    }),
  );
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
 * Program word positions that may execute. Known wrapper options consume their own values before following the
 * wrapped program. Ordinary program arguments are data; only unmodeled options of a known wrapper retain later
 * candidates because their operand positions cannot be selected reliably.
 */
function executablePrograms(words) {
  const plain = words.map(unmarkGlobs);
  let s = commandStart(plain);
  // commandStart skips precommand modifiers, but these three have options whose values must not become programs.
  const prefix = plain.slice(0, s).findIndex((word) => ["time", "command", "exec"].includes(word));
  if (prefix >= 0) s = prefix;
  const uncertain = (from) => plain.flatMap((word, index) => (index >= from ? [index] : []));
  while (s < plain.length) {
    const name = programName(plain[s]);
    if (name === "find") {
      const templates = [];
      for (let k = s + 1; k < plain.length; k += 1) {
        if (!["-exec", "-execdir", "-ok", "-okdir"].includes(plain[k])) continue;
        const end = plain.findIndex((word, index) => index > k && (word === ";" || word === "+"));
        const start = k + 1;
        templates.push(...executablePrograms(plain.slice(start, end < 0 ? undefined : end)).map((r) => r + start));
        k = end < 0 ? plain.length : end;
      }
      return templates.length ? templates : [s];
    }
    if (STDIN_SHELLS.has(name) || PRINT_PROGRAMS.has(name) || INTERPRETERS.test(name)) return [s];
    if (
      ENVIRONMENT_ASSIGNMENT.test(plain[s]) ||
      (BUILTIN_PREFIXES.has(plain[s]) && !Object.hasOwn(EXEC_WRAPPERS, name))
    ) {
      s += 1;
      continue;
    }
    const options = Object.hasOwn(EXEC_WRAPPERS, name) ? EXEC_WRAPPERS[name] : null;
    if (!options) return [s];
    const runner = s;
    s += 1;
    while (s < plain.length && plain[s].startsWith("-") && plain[s] !== "-") {
      const arg = plain[s];
      if (arg === "--") {
        s += 1;
        break;
      }
      // These command flags inspect names instead of executing them. env's split-string syntax needs re-lexing
      // (sourceArguments does that separately), so its stdin consumer cannot be selected by plain argv positions.
      if (name === "command" && /^-[p]*[vV]/.test(arg)) return [];
      if (name === "env" && /^(?:-[^-]*S|--split-string(?:=|$))/.test(arg)) return [runner];
      const option = programOption(plain, s, options);
      if (name === "parallel" && ["--replace", "-i"].includes(arg) && s + 1 < plain.length)
        Object.assign(option, { value: plain[s + 1], next: s + 2 });
      if (option.unknown) return uncertain(s + 1);
      s = option.next;
    }
    if (name === "ssh") s += 1; // destination, after ssh's options
    if (name === "timeout") s += 1; // duration, after timeout's options
    if (name === "flock") s += 1; // lock file or descriptor, before the command
  }
  return [];
}

/** Shell commands past wrapper options; ordinary program arguments never select a consumer. */
function executableShells(words) {
  return executablePrograms(words).filter((index) => STDIN_SHELLS.has(programName(words[index])));
}

/**
 * True when `words` runs a shell that takes its commands from stdin: `sh` or `bash -s` with no script,
 * `xargs sh -c` with the string left to xargs, or `source`/`.` reading stdin or `<(…)`.
 */
function readsStdinSource(words, depth = 0, replacements = []) {
  const s = commandStart(words);
  const positions = executablePrograms(words);
  for (let k = s; k < (positions.at(-1) ?? s); k += 1) {
    const name = programName(words[k]);
    if (name === "find" && words.slice(k + 1).some((word) => ["-exec", "-execdir", "-ok", "-okdir"].includes(word)))
      replacements = [...replacements, "{}"];
    if (name !== "xargs" && name !== "parallel") continue;
    if (name === "parallel") replacements = [...replacements, "{}"];
    for (let a = k + 1; a < words.length && words[a] !== "--";) {
      if (!words[a].startsWith("-")) break;
      const option = programOption(words, a, EXEC_WRAPPERS.xargs);
      if (name === "parallel" && ["--replace", "-i"].includes(words[a]) && a + 1 < words.length)
        Object.assign(option, { value: words[a + 1], next: a + 2 });
      if (["-I", "-J", "-i", "--replace"].includes(option.name)) replacements = [...replacements, option.value || "{}"];
      a = option.next;
    }
  }
  if (depth < MAX_NESTING && words.slice(0, positions.at(-1)).some((word) => programName(word) === "parallel")) {
    for (const r of positions) {
      const template = lexShell(words[r]);
      if (template?.commands.some((nested) => readsStdinSource(nested, depth + 1, replacements))) return true;
    }
  }
  if ((words[s] === "source" || words[s] === ".") && words.length > s + 1)
    return STDIN_FILE.test(words[s + (words[s + 1] === "--" ? 2 : 1)] || "");
  if (
    depth < MAX_NESTING &&
    words.some((word, r) => {
      const name = programName(word);
      if (!positions.includes(r)) return false;
      if (name !== "env" && name !== "eval" && !STDIN_SHELLS.has(name)) return false;
      return (sourceArguments(words, r) || []).some((source) => {
        // Replacement bytes supplied by xargs/parallel remain stdin source across every re-read string.
        if (joinedTemplate(source, replacements, depth + 1, false)) return true;
        const lexed = lexShell(source);
        return lexed && lexed.commands.some((nested) => readsStdinSource(nested, depth + 1, replacements));
      });
    })
  )
    return true;
  return executableShells(words).some((r) => {
    const input = shellInput(words.slice(r + 1), programName(words[r]));
    if (input.stdin) return true;
    return input.source.some((source) => joinedTemplate(source, replacements, depth + 1, false));
  });
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

/** Decode only literal printers' closed output grammar; null means a different program owns stdout. */
function printedSource(words, facts, depth = 0) {
  const positions = executablePrograms(words);
  const start = positions.length === 1 ? positions[0] : commandStart(words);
  const name = programName(words[start] || "");
  const outside = { reason: UNREADABLE_SYNTAX.printedSource };
  const runtimeArgs = words.slice(0, start).some((word) => ["xargs", "parallel"].includes(programName(word)));
  if (name === "env") {
    const failure = {};
    const sources = sourceArguments(words, start, failure);
    if (failure.unreadable) return { reason: failure.unreadable };
    if (!sources) return null;
    if (!sources.length) return outside;
    if (depth >= MAX_NESTING || !facts || words.slice(start).some((_, k) => !(facts[start + k] & WORD_LITERAL)))
      return outside;
    const lexed = lexShell(sources[0]);
    if (!lexed || lexed.commands.length !== 1) return outside;
    const nested = lexed.commands[0];
    const printed = printedSource(nested, lexed.wordFacts.get(nested), depth + 1);
    return printed && runtimeArgs ? outside : printed;
  }
  if (!PRINT_PROGRAMS.has(name)) return null;
  if (runtimeArgs) return outside; // input wrappers replace or append arguments, even when every shown word is literal
  const args = words.slice(start + 1).map(unmarkGlobs);
  if (name === "printf" && args[0] === "-v") return { texts: [] }; // this form writes a variable, not stdout
  if (!facts || words.slice(start).some((word, k) => !(facts[start + k] & WORD_LITERAL))) return outside;
  if (name === "echo" || name === "print") {
    let newline = "\n";
    let formatted = false;
    while (args[0]?.startsWith("-")) {
      const option = args.shift();
      if (!(name === "echo" ? /^-n+$/ : /^-[nrf]+$/).test(option)) return outside;
      if (option.includes("n")) newline = "";
      if (option.includes("f")) {
        formatted = true;
        break;
      }
    }
    if (!formatted) {
      const text = args.join(" ");
      return text.includes("\\") ? outside : { texts: [text + newline] };
    }
  }
  if (name === "printf" && args[0] === "--") args.shift();
  const format = args.shift() || "";
  if (name === "printf" && format.startsWith("-")) return outside;
  let text = "";
  let operand = 0;
  do {
    const previous = operand;
    for (let k = 0; k < format.length; k += 1) {
      if (format[k] === "\\") {
        const escape = format[++k];
        if (!["n", "t", "\\"].includes(escape)) return outside;
        text += { n: "\n", t: "\t", "\\": "\\" }[escape];
        continue;
      }
      if (format[k] !== "%") {
        text += format[k];
        continue;
      }
      const conversion = format[k + 1];
      if (conversion === "%") {
        text += "%";
        k += 1;
        continue;
      }
      const match = /^%(?:-([0-9]+))?s/.exec(format.slice(k));
      if (!match) return outside;
      const width = Number(match[1] || 0);
      // Bound padding allocations; larger formats refuse instead of exhausting the hook process.
      if (!Number.isSafeInteger(width) || width > 65536) return outside;
      const value = args[operand++] || "";
      if (width && /[^\x00-\x7f]/.test(value)) return outside; // byte and character padding need not agree
      text += value.padEnd(width, " ");
      k += match[0].length - 1;
    }
    if (operand === previous) break; // a format without conversions ignores surplus arguments
  } while (operand < args.length);
  return { texts: [text] };
}

/** Identify runtime command words in source, keeping argument expansions and known path commands as values. */
function hasRuntimeCommand(lexed, bindings) {
  return lexed.commands.some((words) => {
    if (lexed.substitutionOutputs.has(words) || lexed.caseData.has(words)) return false;
    const facts = lexed.wordFacts.get(words);
    return executablePrograms(words).some((r) => {
      if (unmarkGlobs(words[r]) === "[") return false;
      if (facts[r] & WORD_LITERAL) return false;
      const paths = boundPaths(words[r], false, bindings.get(words));
      return paths.length !== 1 || paths.some((file) => !["L", "T"].includes(file[0]) || !file.includes("/"));
    });
  });
}

/** Read source bytes; whole variable eval/-c operands are runtime state, while substitutions need proof. */
function consumerWord(word, depth, bindings = new Map(), runtimeWord = "") {
  if (depth >= MAX_NESTING) return null;
  // This exemption belongs only to expanded argv operands, never stdin, printed source or literal shell text.
  // Inspect original spelling: adjacent literal fragments such as "$""V" also dequote to $V, but are source text.
  const value = runtimeWord.replace(/^"(.*)"$/s, "$1");
  if (/^\$(?:[A-Za-z_][A-Za-z0-9_]*|\{=?[A-Za-z_][A-Za-z0-9_]*(?::-[^$`{}]*)?\})$/.test(value)) return "";
  const body =
    word.startsWith("$(") && word.endsWith(")")
      ? word.slice(2, -1)
      : word.startsWith("`") && word.endsWith("`")
        ? word.slice(1, -1)
        : null;
  if (body === null) {
    const source = lexShell(word);
    return source && !hasRuntimeCommand(source, commandBindings(source, bindings)) ? word : null;
  }
  const lexed = lexShell(body);
  if (!lexed || lexed.redirectFiles.length || lexed.functions.size) return null;
  let output = "";
  for (const pipeline of lexed.pipelines) {
    let text = "";
    for (const words of pipeline.commands) {
      const positions = executablePrograms(words);
      if (positions.length !== 1) return null;
      const start = positions[0];
      const name = programName(words[start]);
      const printed = printedSource(words, lexed.wordFacts.get(words));
      if (printed?.reason) return null;
      if (printed) text = printed.texts.join("");
      else if (name === ":" || name === "true" || name === "false") text = "";
      else if (
        (name === "cat" || name === "tee") &&
        words.slice(start + 1).every((arg) => arg === "-" || arg === "--")
      ) {
        const inputs = lexed.heredocs.filter((input) => input.words === words);
        if (inputs.length) {
          if (!inputs.every((input) => input.literal)) return null;
          text = inputs.at(-1).body;
        } else if (!pipeline.fed.has(words)) return null;
      } else return null;
      if (!pipeline.outputs.has(words)) output += text;
    }
  }
  return output.replace(/\n+$/, "");
}

/** Detect generated scripts whose bytes cannot be inferred from literal printing or cat/tee input. */
function hasUnknownScriptOutput(lexed, pipeline, scripts) {
  if (!pipeline.commands.some((words) => writesRunScript(lexed, words, scripts))) return false;
  return pipeline.commands.some((words, index) => {
    const writesScript = writesRunScript(lexed, words, scripts);
    if (
      !writesScript &&
      pipeline.commands
        .slice(index + 1)
        .some((later) => writesRunScript(lexed, later, scripts) && printedSource(later, lexed.wordFacts.get(later)))
    )
      return false;
    if (!writesScript && pipeline.fed.has(words) && (!pipeline.outputs.has(words) || readsStdinSource(words)))
      return false;
    const positions = executablePrograms(words);
    const start = positions.length === 1 ? positions[0] : commandStart(words);
    const name = programName(words[start] || "");
    const printed = printedSource(words, lexed.wordFacts.get(words));
    if (
      writesScript &&
      !printed &&
      words.slice(0, start).some((word) => ["xargs", "parallel"].includes(programName(word)))
    )
      return true; // wrapper stdin supplies argv, not the written file's bytes
    if (!name || printed || (words.length === 1 && ["}", "fi", "done", "esac"].includes(name))) return false;
    const readsInput =
      name === "tee" || (name === "cat" && words.slice(start + 1).every((arg) => arg === "-" || arg === "--"));
    const literalInput = lexed.heredocs.some(
      (input) =>
        input.literal &&
        input.pipeline === pipeline &&
        (input.words === words || input.group?.includes(words) || pipeline.piped),
    );
    return !(readsInput && (literalInput || (pipeline.piped && pipeline.fed.has(words))));
  });
}

/** Resolve saved input descriptors only from literal, single bindings in this source context. */
function descriptorInputs(lexed) {
  const saved = new Map();
  const redirects = [...lexed.heredocs, ...lexed.redirectFiles].sort((a, b) => a.at - b.at);
  for (const redirect of redirects) {
    const { words, fd } = redirect;
    const persistent = words[0] === "exec" && executablePrograms(words).length === 0;
    const duplicate = redirect.operator === "<&";
    let input = redirect;
    if (duplicate) {
      const match = /^(\d+)(-?)$/.exec(redirect.file);
      input = redirect.literal && match ? saved.get(match[1]) : null;
      if (match?.[2]) saved.set(match[1], null);
    }
    if (persistent) {
      // Reassignment and bindings inside substitutions/functions cannot prove the later descriptor's contents.
      const known = !saved.has(fd) && !lexed.substitutionOutputs.has(words) && !lexed.functionCommands.has(words);
      saved.set(fd, known && input?.literal && !input.write ? input : null);
    }
    if (!duplicate || fd !== "0" || ![words, ...(redirect.group || [])].some((command) => readsStdinSource(command)))
      continue;
    if (!input?.literal || input.write) return false;
    const pipeline = lexed.pipelines.find((pipeline) => pipeline.commands.includes(words));
    if (Object.hasOwn(input, "body")) {
      lexed.heredocs.push({ ...input, words, group: redirect.group, pipeline, fd: "0" });
    } else {
      lexed.redirectFiles.push({ ...input, words, group: redirect.group, fd: "0" });
    }
  }
  return true;
}

/**
 * Push every invocation of `target` in `command` onto `found`. Returns the lexed command when the lexer read all
 * of it, or null when it fell back to the raw scan.
 */
function collect(command, found, depth, target, scripts = new Set(), bindings = new Map(), codeContext = false) {
  const failure = {};
  const lexed = lexShell(command, true, failure);
  if (lexed === null) {
    if (failure.unreadable) markTargetUnreadable(target, failure.reason);
    target.unlexable(command, found, failure.unreadable);
    return null;
  }
  if (depth >= MAX_NESTING) {
    markTargetUnreadable(target, UNREADABLE_SYNTAX.sourceLimit);
    target.unlexable(command, found, true);
    return lexed;
  }
  lexed.pathBindings = commandBindings(lexed, bindings);
  if (!descriptorInputs(lexed)) {
    markTargetUnreadable(target, "saved shell input descriptor is not provably literal");
    target.unlexable(command, found, true);
    return null;
  }
  // Preserve command-array identities used by pipelines and redirects while resolving known executable paths.
  for (const words of lexed.commands) {
    const facts = lexed.wordFacts.get(words);
    const resolved = literalPathCommands(words, facts, lexed.pathBindings.get(words));
    resolved.forEach((word, k) => {
      if (word === words[k]) return;
      words[k] = word;
      facts[k] |= WORD_LITERAL;
    });
  }
  if (
    hasRuntimeCommand(codeContext ? lexed : { ...lexed, commands: [...lexed.functionCommands] }, lexed.pathBindings)
  ) {
    markTargetUnreadable(target, UNREADABLE_SYNTAX.printedSource);
    target.unlexable(command, found, true);
    return null;
  }
  // Source contexts share paths: a file written outside eval/-c/env -S may be executed inside one, or conversely.
  for (const words of lexed.commands)
    for (const file of scriptOperands(
      words,
      lexed.wordFacts.get(words),
      depth,
      lexed.pathBindings.get(words),
      lexed.wordTexts.get(words),
    ))
      scripts.add(file);
  for (const redirect of lexed.redirectFiles)
    if (!redirect.write && [redirect.words, ...redirect.group].some((words) => readsStdinSource(words)))
      for (const file of boundPaths(
        redirect.file,
        redirect.literal,
        lexed.pathBindings.get(redirect.words),
        true,
        redirect.text,
      ))
        scripts.add(file);
  const definitions = new Set(lexed.functions);
  const aliasBodies = [];
  let unknownDefinition = false;
  for (const words of lexed.commands) {
    for (const r of executablePrograms(words)) {
      if (words[r] === "function" && !lexed.functions.has(words[r + 1])) unknownDefinition = true;
      if (words[r] !== "alias") continue;
      for (let k = r + 1; k < words.length; k += 1) {
        const equal = words[k].indexOf("=");
        if (equal < 0) {
          if (!(lexed.wordFacts.get(words)[k] & WORD_LITERAL)) unknownDefinition = true;
          continue;
        }
        definitions.add(words[k].slice(0, equal));
        if (!(lexed.wordFacts.get(words)[k] & WORD_LITERAL)) unknownDefinition = true;
        else aliasBodies.push(words[k].slice(equal + 1));
      }
    }
  }
  const shadowed = [...definitions].some(
    (name) =>
      name === "gh" ||
      name === "git" ||
      name === target.name ||
      [
        "alias",
        "function",
        ":",
        "true",
        "false",
        "set",
        "find",
        "dd",
        "cp",
        "mv",
        "install",
        "ln",
        "sed",
        "perl",
      ].includes(name) ||
      DATA_PROGRAMS.has(name) ||
      OPTION_SETTERS.has(name) ||
      ARRAY_DECLARERS.has(name) ||
      ARITHMETIC_COUNT_BUILTINS.has(name) ||
      PRINT_PROGRAMS.has(name) ||
      SOURCE_RUNNERS.has(name) ||
      BUILTIN_PREFIXES.has(name) ||
      SUBSCRIPT_BUILTINS.has(name) ||
      INTERPRETERS.test(name) ||
      Object.hasOwn(EXEC_WRAPPERS, name),
  );
  if (shadowed || unknownDefinition) {
    markTargetUnreadable(target, "function or alias changes a modeled command");
    target.unlexable(command, found, true);
    return null;
  }
  for (const body of aliasBodies) collect(body, found, depth + 1, target, scripts, bindings, true);
  if (lexed.pipelines.some((pipeline) => hasUnknownScriptOutput(lexed, pipeline, scripts))) {
    markTargetUnreadable(target, "generated shell file has unknown contents or write targets");
    target.unlexable(command, found, true);
    return null;
  }
  // Independent file writes can concatenate command-name fragments. Until file overwrite/append order is
  // modeled, refuse multiple producing pipelines instead of reading each fragment as a complete script.
  for (const script of scripts) {
    const writes = lexed.pipelines.filter((pipeline) =>
      pipeline.commands.some((words) => writesRunScript(lexed, words, new Set([script]))),
    );
    if (writes.length > 1) {
      markTargetUnreadable(target, "multiple writes to one consumed shell file");
      target.unlexable(command, found, true);
      return null;
    }
  }
  // A generated file is source only after every write feeding it passed the closed-output proof.
  const scriptPipelines = new Set(
    lexed.pipelines.filter(
      (pipeline) =>
        pipeline.commands.some((words) => writesRunScript(lexed, words, scripts)) &&
        !hasUnknownScriptOutput(lexed, pipeline, scripts),
    ),
  );
  for (const words of lexed.commands)
    collectWords(
      words,
      found,
      depth,
      target,
      scripts,
      lexed.wordFacts.get(words),
      lexed.pathBindings.get(words),
      lexed.wordTexts.get(words),
    );
  for (const input of lexed.heredocs) {
    if (input.fd !== "0") continue;
    const { body, words, pipeline } = input;
    const positions = executablePrograms(words);
    const start = positions.length === 1 ? positions[0] : commandStart(words);
    const name = programName(words[start] || "");
    const forwardsInput =
      name === "tee" || (name === "cat" && words.slice(start + 1).every((arg) => arg === "-" || arg === "--"));
    const compound = words.length === 0 || words[0] === "}" || input.group?.length > 0;
    const ownShell = executableShells(words).some((shell) => {
      const mode = shellInput(words.slice(shell + 1), programName(words[shell]));
      return mode.stdin;
    });
    if (
      (input.literal && forwardsInput && writesRunScript(lexed, words, scripts)) ||
      readsStdinSource(words) ||
      ownShell ||
      (pipeline.piped && scriptPipelines.has(pipeline)) ||
      ((compound || pipeline.piped) &&
        pipeline.commands.some((words) => pipeline.fed.has(words) && readsStdinSource(words)))
    ) {
      const text = input.literal ? body : consumerWord(body.trimEnd(), depth + 1);
      if (text === null) {
        markTargetUnreadable(target, UNREADABLE_SYNTAX.printedSource);
        target.unlexable(command, found, true);
        return null;
      }
      collect(text, found, depth + 1, target, scripts, lexed.pathBindings.get(words), true);
    }
  }
  for (const pipeline of lexed.pipelines) {
    // Captured substitutions are read by their operand consumer, independently of sibling pipeline output.
    const feedsShell =
      pipeline.piped && pipeline.commands.some((words) => pipeline.fed.has(words) && readsStdinSource(words));
    const feedsSource = feedsShell || (pipeline.piped && scriptPipelines.has(pipeline));
    const fragments = [];
    for (const words of pipeline.commands) {
      // In a simple producer | printer file write, the printer ignores stdin. The producer's output
      // never reaches the file; compound groups retain their separate conservative output proof.
      if (
        pipeline.commands.length === 2 &&
        words === pipeline.commands[0] &&
        writesRunScript(lexed, pipeline.commands[1], scripts) &&
        printedSource(pipeline.commands[1], lexed.wordFacts.get(pipeline.commands[1]))
      )
        continue;
      if (
        lexed.substitutionOutputs.has(words) ||
        !(feedsSource || writesRunScript(lexed, words, scripts)) ||
        (pipeline.fed.has(words) &&
          (readsStdinSource(words) || (!pipeline.outputs.has(words) && !writesRunScript(lexed, words, scripts))))
      )
        continue;
      const printed = printedSource(words, lexed.wordFacts.get(words));
      // A terminal print command can inherit stdin in a substitution without feeding its own output to that shell.
      if (printed && pipeline.fed.has(words) && !pipeline.outputs.has(words) && !writesRunScript(lexed, words, scripts))
        continue;
      if (!printed) {
        const positions = executablePrograms(words);
        const start = positions.length === 1 ? positions[0] : commandStart(words);
        const name = programName(words[start] || "");
        const literalInput = lexed.heredocs.some(
          (input) => input.literal && (input.words === words || input.group?.includes(words)),
        );
        const forwards =
          name === "tee" || (name === "cat" && words.slice(start + 1).every((arg) => arg === "-" || arg === "--"));
        if (
          !name ||
          [":", "true", "false", "}", "fi", "done", "esac"].includes(name) ||
          (forwards && (literalInput || pipeline.fed.has(words)))
        )
          continue;
        markTargetUnreadable(target, UNREADABLE_SYNTAX.printedSource);
        target.unlexable(command, found, true);
        return null;
      }
      // Literal printf output must be formatted before lexing: a conversion can construct the executable name.
      if (printed.reason) {
        markTargetUnreadable(target, printed.reason);
        target.unlexable(command, found, true);
        return null;
      }
      fragments.push(...printed.texts);
    }
    if (fragments.length) collect(fragments.join(""), found, depth + 1, target, scripts, bindings, true);
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
// The only unquoted characters a plain skeleton may hold: word text, blanks, newlines and the operators of plain
// commands. Anything else is refused, among them every character opening a context where the lexer may read a
// heredoc the shells do not, or place its body elsewhere: `$[…]`, `${…}`, `$((…))`, `((…))`, `[[…]]` (where `<<`
// is a shift or text), backticks, a comment, an escape, a CR (the shells keep it in the delimiter word), a
// vertical tab, form feed, NUL, and any character outside ASCII.
const SKELETON_PLAIN = /[A-Za-z0-9 \t\n_./:=,+@%^~!?*&;|<>-]/;

/** True when the sticky `pattern` matches `text` at `at`: the index after the match, or -1. */
function matchAt(pattern, text, at) {
  pattern.lastIndex = at;
  return pattern.test(text) ? pattern.lastIndex : -1;
}

/**
 * True when `skeleton` (a command with its quoted heredoc bodies cut out) holds only plain words, quotes,
 * operators and quoted heredoc operators, so every `<<` in it is a heredoc the shells read where the lexer does:
 * single-quoted text, double-quoted text without `$`, backtick or backslash (or the commit-message substitution),
 * a here-string `<<<`, and only SKELETON_PLAIN characters outside quotes. An unquoted-delimiter heredoc fails.
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
      i = SKELETON_PLAIN.test(c) ? i + 1 : -1;
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
  const scripts = new Set();
  let lexed;
  for (let pass = 0; pass < MAX_NESTING; pass += 1) {
    const previous = scripts.size;
    found.length = 0;
    lexed = collect(command, found, 0, run, scripts);
    if (scripts.size === previous) break;
    // A newly discovered script can make an earlier write executable; rescan until all contexts agree.
    if (pass === MAX_NESTING - 1) {
      markTargetUnreadable(run, UNREADABLE_SYNTAX.sourceLimit);
      run.unlexable(command, found, true);
    }
  }
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
  try {
    const program = String(name).toLowerCase();
    const target = program === GIT.name ? GIT : programTarget(program);
    const found = findInvocations(command, target, info);
    return target.approximate ? null : found.map((tokens) => tokens.map(unmarkGlobs));
  } catch {
    info.unreadable = true;
    info.unreadableReason = "lexer error";
    return null;
  }
}

module.exports = { gitSubcommandArgs, programInvocations, XARGS_TAIL };
