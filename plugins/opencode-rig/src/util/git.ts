/** Run bounded local Git reads and classify literal command arguments. */
import type { Classification, GuardrailClass } from "../guardrails.ts"
import type { GuardrailMode } from "../options.ts"
import { spawnSync, type SpawnSyncReturns } from "node:child_process"

export function runGit(
  cwd: string,
  args: readonly string[],
  options: { timeout?: number; maxBuffer?: number } = {},
): SpawnSyncReturns<Buffer> {
  const env = { ...process.env }
  for (const key of Object.keys(env)) {
    if (key.toUpperCase().startsWith("GIT_")) delete env[key]
  }
  return spawnSync("git", ["-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false", ...args], {
    cwd,
    env: { ...env, LC_ALL: "C" },
    timeout: options.timeout ?? 1_000,
    maxBuffer: options.maxBuffer ?? 64 * 1024 * 1024,
  })
}

const REASONS: Record<GuardrailClass, string> = {
  "remote-mutation": "remote mutation",
  "unparsed-git": "cannot safely classify Git shell syntax",
  "destructive-local": "destructive local operation",
  "history-rewrite": "history rewrite",
  "plain-commit": "plain local commit",
  "read-only-git": "read-only Git",
  other: "no guarded pattern matched",
}

const REMOTE_PATTERNS: readonly RegExp[] = [
  /^git\s+push\b/,
  /^git\s+remote\s+(add|remove|rm|set-url|prune|update)\b/,
  /^npm\s+publish\b/,
  /^uv\s+publish\b/,
  /^twine\s+upload\b/,
  /^docker\s+push\b/,
  /^git\s+tag\b.*\s+push\b/,
]

const HISTORY_PATTERNS: readonly RegExp[] = [
  /^git\s+rebase\b(?!\s+--(abort|continue|skip|quit))/,
  /^git\s+filter-repo\b/,
  /^git\s+filter-branch\b/,
  /^git\s+replace\b/,
]

const DESTRUCTIVE_PATTERNS: readonly RegExp[] = [
  /^git\s+reset\s+--hard\b/,
  /^git\s+clean\b.*-[a-z]*[fdx]/,
  /^git\s+branch\s+(-D|--delete\s+--force)\b/,
  /^git\s+checkout\s+(-f|--force|--\s+\.?)/,
  /^git\s+stash\s+(drop|clear)\b/,
  /^git\s+worktree\s+remove\b.*--force\b/,
  /^rm\s+-[a-z]*[rf][a-z]*\s/,
  /^find\b.*\s-delete\b/,
  /^truncate\s+-s\s+0\b/,
]

const STRICT_EXTRA_PATTERNS: readonly RegExp[] = [/^git\s+merge\b/, /^git\s+cherry-pick\b/]

const DIFF_OPTIONS =
  "-p -u --patch --no-patch -s --stat --stat= --numstat --shortstat --name-only --name-status --raw --summary --check --exit-code --quiet --cached --staged --no-ext-diff --no-textconv --color= --no-color --binary --full-index --abbrev= --no-renames --find-renames= --find-copies= -M -M= -S= --diff-filter= --color-words --color-words= -w -b --ignore-space-at-eol --ignore-blank-lines --word-diff --word-diff= -U= --unified= --src-prefix= --dst-prefix= --no-prefix --relative --relative="
const LOG_OPTIONS =
  "--oneline --format= --pretty= --decorate --decorate= --no-decorate --graph --all --branches --tags --remotes --first-parent --merges --no-merges --reverse --topo-order --date-order --author= --committer= --grep= --since= --until= --after= --before= --date= --max-count= -n= --skip= --follow --no-walk --no-notes --show-signature"
const GIT_OPTIONS: Readonly<Record<string, string>> = {
  status:
    "--short -s --branch -b --porcelain --porcelain= -z -u -u= --untracked-files= --ignored --ignored= --no-renames",
  diff: DIFF_OPTIONS,
  log: `${DIFF_OPTIONS} ${LOG_OPTIONS}`,
  show: `${DIFF_OPTIONS} ${LOG_OPTIONS}`,
  "rev-parse":
    "--show-toplevel --git-dir --git-common-dir --is-inside-work-tree --is-bare-repository --show-prefix --show-cdup --verify --quiet -q --short --short= --abbrev-ref --symbolic --symbolic-full-name --sq --end-of-options",
  "rev-list":
    "--count --all --branches --tags --remotes --objects --object-names --max-count= -n= --reverse --first-parent --parents --children --left-right --cherry-pick --boundary --topo-order --date-order --no-merges",
  describe:
    "--all --tags --contains --always --long --dirty --dirty= --abbrev= --match= --exclude= --exact-match --first-parent",
  branch:
    "--list -l -a --all -r --remotes -v -vv --verbose --show-current --contains= --no-contains= --merged --merged= --no-merged --no-merged= --format= --sort= --color= --no-color --column --no-column --points-at=",
  "ls-files":
    "-z -t -v -f -c --cached -d --deleted -m --modified -o --others -i --ignored -s --stage -u --unmerged -k --killed --directory --no-empty-directory --exclude-standard --full-name --error-unmatch --debug --eol --format=",
  "ls-tree": "-d -r -t -l -z --name-only --name-status --full-name --full-tree --long --abbrev= --format=",
  "cat-file": "-t -s -e -p --batch --batch= --batch-check --batch-check= --batch-all-objects --buffer --unordered",
  blame: "-w -b --root --line-porcelain --porcelain --incremental --show-email --show-name --show-number -L= --date=",
  shortlog: "-n --numbered -s --summary -e --email --all --no-merges --group= --format=",
  remote: "-v --verbose --all --push --fetch -n",
  config:
    "--get --get-all --list -l --global --local --system --worktree --show-origin --show-scope --includes --no-includes --null -z",
  add: "-n --dry-run -v --verbose -f --force -u --update -A --all --ignore-removal --no-all --intent-to-add -N --ignore-errors --ignore-missing --renormalize",
  stash: `-p -u --patch --no-patch -s --stat --numstat --shortstat --name-only --name-status --raw --summary --check --exit-code --quiet --no-ext-diff --no-textconv --color= --no-color --binary --full-index --abbrev= --no-renames -w -b ${LOG_OPTIONS} --include-untracked --only-untracked`,
  tag: "--list -l -n --contains= --no-contains= --merged --merged= --no-merged --no-merged= --points-at= --sort= --format= --color= --no-column",
  "show-ref":
    "--head --heads --tags --verify --quiet -q --hash --hash= --abbrev= -d --dereference --exclude-existing --exclude-existing=",
  grep: "-a --text -I -i --ignore-case -w --word-regexp -v --invert-match -h -H -n --line-number --column -l --files-with-matches -L --files-without-match -c --count -z --null -e= -f= -F --fixed-strings -E --extended-regexp -G --basic-regexp -P --perl-regexp --all-match --and --or --not --break --heading --full-name --cached --no-index --untracked --no-exclude-standard -q --quiet",
  reflog: LOG_OPTIONS,
  worktree: "--porcelain -z -v --verbose --expire=",
  "name-rev": "--name-only --tags --refs= --exclude= --all --no-undefined --always --stdin",
  "merge-base": "-a --all --octopus --independent --is-ancestor --fork-point",
  "for-each-ref":
    "--count= --sort= --format= --shell --perl --python --tcl --points-at= --merged --merged= --no-merged --no-merged= --contains= --no-contains= --ignore-case",
  "symbolic-ref": "-q --quiet --short --no-recurse",
  var: "-l",
  version: "--build-options",
  commit:
    "-m= --message= -F= --file= -C= --reuse-message= -c= --reedit-message= --author= --date= --cleanup= --fixup= --squash= --trailer= --pathspec-from-file= --pathspec-file-nul -a --all -s --signoff -v --verbose -q --quiet --no-edit --allow-empty --allow-empty-message --no-verify --no-gpg-sign --dry-run --short --branch --porcelain --null -z --only -o --include -i --reset-author",
  checkout: "-b= -B= --orphan= -t --track --track= --no-track -q --quiet --detach --guess --no-guess",
  restore: "-s= --source= -S --staged --ignore-unmerged --ignore-skip-worktree-bits --no-overlay --overlay -q --quiet",
  switch:
    "-c= --create= -C= --force-create= --orphan= --detach -d --guess --no-guess --track --track= --no-track -q --quiet",
  rm: "-f --force -n --dry-run -r --cached --ignore-unmatch -q --quiet",
}

/** Separate option values and validate each short flag before interpreting guarded effects. */
function gitArguments(
  args: string[],
  options: string,
  extraOperands = "",
): { flags: string[]; operands: string[]; valid: boolean } {
  const allowed = options.split(" ")
  const takesValue = [...allowed, ...extraOperands.split(" ")]
    .filter((option) => option.endsWith("="))
    .map((option) => option.slice(0, -1))
  const flags: string[] = []
  const operands: string[] = []
  let valid = true
  for (let index = 0; index < args.length; index += 1) {
    const arg = args[index]!
    if (arg === "--") {
      operands.push(...args.slice(index + 1))
      break
    }
    if (!arg.startsWith("-") || arg === "-") {
      operands.push(arg)
      continue
    }
    const name = arg.split("=", 1)[0]!
    const shorts = arg.startsWith("--") ? [] : [...arg.slice(1)].map((letter) => "-" + letter)
    const valueAt = shorts.findIndex((flag) => takesValue.includes(flag))
    const shortFlags = shorts.slice(0, valueAt < 0 ? undefined : valueAt + 1)
    valid &&= arg.startsWith("--")
      ? allowed.includes(arg) || allowed.includes(name + "=")
      : (/^-\d+$/.test(arg) && allowed.includes("-n=")) ||
        shortFlags.every((flag) => allowed.includes(flag) || allowed.includes(flag + "="))
    flags.push(...(arg.startsWith("--") ? [name] : shortFlags))
    const separateValue = arg.startsWith("--")
      ? !arg.includes("=") &&
        takesValue.some((option) => option.startsWith(name)) &&
        (!allowed.includes(name) ||
          (/^--(?:no-)?merged$/.test(name) && args[index + 1] !== undefined && !args[index + 1]!.startsWith("-")))
      : valueAt >= 0 && valueAt === shorts.length - 1 && !allowed.includes(shorts[valueAt]!)
    if (separateValue) {
      index += 1
      valid &&= args[index] !== undefined
    }
  }
  return { flags, operands, valid }
}

/** Admit only explicit literal Git forms after the guarded operations have been checked. */
function gitAllowedClass(words: string[]): GuardrailClass {
  const verb = words[1] ?? ""
  if (!Object.hasOwn(GIT_OPTIONS, verb)) return "unparsed-git"
  const configRead =
    verb === "config" && ["get", "list"].includes(words.slice(2).find((word) => !word.startsWith("-")) ?? "")
  const options = GIT_OPTIONS[verb]! + (configRead ? " --all --show-names --regexp --value=" : "")
  const { flags, operands, valid } = gitArguments(words.slice(2), options)
  if (!valid) return "unparsed-git"
  if (verb === "config") {
    const action = ["get", "list"].includes(operands[0] ?? "") ? operands.shift() : undefined
    const list = action === "list" || flags.some((flag) => ["--list", "-l"].includes(flag))
    return operands.length === (list ? 0 : 1) && (list || action === "get" || operands[0]!.includes("."))
      ? "other"
      : "unparsed-git"
  }
  if (["branch", "tag"].includes(verb) && operands.length && !flags.some((flag) => ["--list", "-l"].includes(flag)))
    return "unparsed-git"
  const forms: Record<string, readonly string[]> = {
    remote: ["show", "get-url"],
    stash: ["list", "show"],
    reflog: ["show"],
    worktree: ["list"],
  }
  if (
    Object.hasOwn(forms, verb) &&
    !forms[verb]!.includes(operands[0] ?? "") &&
    !(operands.length === 0 && ["remote", "reflog"].includes(verb))
  )
    return "unparsed-git"
  if ((verb === "symbolic-ref" && operands.length !== 1) || (verb === "version" && operands.length))
    return "unparsed-git"
  if (verb === "commit") return "plain-commit"
  return ["add", "checkout", "restore", "switch", "rm"].includes(verb) ? "other" : "read-only-git"
}

/** Recognize only explicit GitHub read forms; every other literal invocation is guarded. */
function githubClass(words: string[]): GuardrailClass {
  if (words[1]?.startsWith("-")) return "unparsed-git"
  if (words[1] === "api") {
    const operands = new Set(["-H", "--header", "-q", "--jq", "-t", "--template", "--hostname", "--cache", "--preview"])
    for (let index = 2; index < words.length; index += 1) {
      const arg = words[index]!
      if (/^(?:-[fFX]|--(?:method|field|raw-field|input)(?:=|$))/.test(arg)) return "remote-mutation"
      if (operands.has(arg)) {
        if (!words[++index]) return "unparsed-git"
      } else if (
        arg.startsWith("-") &&
        !["--paginate", "--slurp", "--silent", "-i", "--include", "--verbose"].includes(arg)
      )
        return "unparsed-git"
    }
    return "read-only-git"
  }
  if (words[2]?.startsWith("-")) return "unparsed-git"
  if (words[1] === "auth") return words[2] === "status" ? "read-only-git" : "remote-mutation"
  if (
    !"pr issue repo run workflow release gist label secret variable project alias extension cache codespace org ruleset attestation"
      .split(" ")
      .includes(words[1] ?? "")
  )
    return "remote-mutation"
  return ["view", "list", "status", "diff", "checks"].includes(words[2] ?? "") ? "read-only-git" : "remote-mutation"
}

/** Classify normalized executable words, keeping argument boundaries intact. */
export function classifySegment(words: string[], original: string, mode: GuardrailMode): Classification {
  if (!["git", "gh", "rm", "find", "truncate", "npm", "uv", "twine", "docker"].includes(words[0] ?? ""))
    return { kind: "other", command: original, reason: "no guarded pattern matched" }
  if (words[0] === "gh") {
    const kind = githubClass(words)
    return { kind, command: original, reason: REASONS[kind] }
  }
  if (words[0] === "git" && ["remote", "stash", "worktree"].includes(words[1] ?? "")) {
    const verb = words.findIndex((word, index) => index > 1 && !word.startsWith("-"))
    if (words[1] === "stash" && verb > 2) {
      const kind = ["drop", "clear"].includes(words[verb]!) ? "destructive-local" : "unparsed-git"
      return { kind, command: original, reason: REASONS[kind] }
    }
    if (verb > 2) words = [...words.slice(0, 2), words[verb]!, ...words.slice(2, verb), ...words.slice(verb + 1)]
  }
  const segment = words.join(" ")
  const args = words.slice(2)
  const { flags: optionArgs, operands } = gitArguments(
    args,
    GIT_OPTIONS[words[1] ?? ""] ?? "",
    words[1] === "branch" ? "-u= --set-upstream-to=" : words[1] === "checkout" ? "--pathspec-from-file=" : "",
  )
  const guardedOptions =
    (
      {
        reset: "--hard",
        branch: "--force --delete",
        worktree: "--force",
        restore: "--staged --worktree",
        switch: "--discard-changes --force --force-create",
        checkout: "--force --pathspec-from-file --pathspec-file-nul",
        commit: "--amend",
        rm: "--recursive --force",
      } as Record<string, string>
    )[words[0] === "rm" ? "rm" : (words[1] ?? "")]?.split(" ") ?? []
  const matchesLong = (arg: string, option: string, choices = guardedOptions) =>
    arg.startsWith("--") &&
    arg.split("=", 1)[0]!.length > 2 &&
    option.startsWith(arg.split("=", 1)[0]!) &&
    (option === arg.split("=", 1)[0] || choices.filter((name) => name.startsWith(arg.split("=", 1)[0]!)).length === 1)
  const shortFlags = new Set(optionArgs.filter((arg) => /^-[^-]$/.test(arg)).map((arg) => arg[1]!))
  const has = (long: string, short = "") =>
    optionArgs.some((arg) => matchesLong(arg, long)) || Boolean(short && shortFlags.has(short))
  const commitAmend = words[0] === "git" && words[1] === "commit" && has("--amend")
  const createsBranch = words[1] === "checkout" && optionArgs.some((arg) => /^-[bB]/.test(arg) || arg === "--orphan")
  const destructive =
    words[0] === "git" &&
    ((words[1] === "reset" && has("--hard")) ||
      (words[1] === "branch" && (shortFlags.has("D") || (has("--force", "f") && has("--delete", "d")))) ||
      (words[1] === "worktree" && args[0] === "remove" && has("--force", "f")) ||
      (words[1] === "restore" && (!has("--staged", "S") || has("--worktree", "W"))) ||
      (words[1] === "rm" && has("--force", "f")) ||
      (words[1] === "switch" && (has("--discard-changes", "f") || has("--force") || has("--force-create", "C"))) ||
      (words[1] === "checkout" &&
        (has("--force", "f") ||
          shortFlags.has("B") ||
          optionArgs.some(
            (arg) => matchesLong(arg, "--pathspec-from-file") || matchesLong(arg, "--pathspec-file-nul"),
          ) ||
          (!createsBranch &&
            (operands.length > 1 ||
              args.includes("--") ||
              operands.some(
                (arg) => arg === "." || /[*?[]|^\.\/|^:|\.[^/.]+$|\/$|^\/|(^|\/)\.\.(\/|$)|^~\/|\s/.test(arg),
              ))))))
  const rmDestructive =
    words[0] === "rm" &&
    words
      .slice(1)
      .some((arg) => matchesLong(arg, "--recursive") || matchesLong(arg, "--force") || /^-[^-]*[rRf]/.test(arg))
  let kind: GuardrailClass = "other"
  let reason: string | undefined
  if (REMOTE_PATTERNS.some((pattern) => pattern.test(segment))) kind = "remote-mutation"
  else if (commitAmend || HISTORY_PATTERNS.some((pattern) => pattern.test(segment))) kind = "history-rewrite"
  else if (destructive || rmDestructive || DESTRUCTIVE_PATTERNS.some((pattern) => pattern.test(segment)))
    kind = "destructive-local"
  else if (mode === "strict" && STRICT_EXTRA_PATTERNS.some((pattern) => pattern.test(segment))) {
    kind = "destructive-local"
    reason = "strict mode: integration operation"
  } else if (words[0] === "git") kind = gitAllowedClass(words)
  return { kind, command: original, reason: reason ?? REASONS[kind] }
}
