/**
 * Shell command classification for Git guardrails.
 *
 * The permission `evaluate` hook runs after configured rules. This module only
 * decides when to escalate an `allow` to `ask`; it never auto-allows a command
 * the user configured as `ask`, and a configured `deny` stays final before the
 * hook is consulted. Escalation messages may name the working-tree paths a
 * destructive command would discard; that lookup is bounded and optional, and
 * a failure there leaves the configured effect and the plain message intact.
 */

import { spawnSync } from "node:child_process"
import { devNull } from "node:os"
import type { GrantState } from "./git-grant.ts"
import type { GuardrailMode } from "./options.ts"

export type GuardrailClass =
  | "remote-mutation"
  | "unparsed-git"
  | "destructive-local"
  | "history-rewrite"
  | "plain-commit"
  | "read-only-git"
  | "other"

export interface Classification {
  readonly kind: GuardrailClass
  readonly command: string
  readonly reason: string
  readonly changesDirectory?: boolean
}

export interface Decision {
  readonly effect: "ask"
  readonly message: string
  readonly kind: GuardrailClass
}

/** Evidence for naming what a destructive command would discard. */
export interface DiscardContext {
  /** Working-tree paths with uncommitted changes. */
  readonly dirtyPaths: readonly string[]
  /** Paths recorded by the active run; absent when no run is active. */
  readonly scopePaths?: readonly string[]
  /** Set when the active run scope could not be read, so no path claim is made. */
  readonly scopeUnknown?: boolean
}

const SEVERITY: Record<GuardrailClass, number> = {
  "remote-mutation": 5,
  "unparsed-git": 2.5,
  "destructive-local": 4,
  "history-rewrite": 3,
  "plain-commit": 2,
  "read-only-git": 1,
  other: 0,
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
  stash: `${DIFF_OPTIONS} ${LOG_OPTIONS} --include-untracked --only-untracked`,
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

interface ShellWord {
  value: string
  dynamic: boolean
  glob?: boolean
  syntax?: boolean
  quoted?: boolean
  scope?: boolean
  substitutions?: (string | undefined)[]
}

const MAX_SHELL_CHARS = 65_536
const MAX_SHELL_DEPTH = 16
const GIT_MENTION = /\b(?:git|gh)\b/i
const DATA_COMMANDS = new Set(["echo", "printf", "grep", "which", "type", "where"])
const SHELLS = new Set(["sh", "bash", "dash", "zsh", "ksh"])
const OPAQUE = new Set("eval alias shopt source . su script watch pwsh powershell cmd xargs".split(" "))

/** Detect literal and quote-fragmented Git names without interpreting shell programs. */
function mentionsGit(text: string): boolean {
  return GIT_MENTION.test(text) || GIT_MENTION.test(text.replace(/\\\n|['"\\]/g, ""))
}

/** Find a balanced executable substitution without treating its quotes as outer quotes. */
function shellSubstitution(text: string, start: number): { body?: string; end: number } {
  const backtick = text[start] === "`"
  const begin = start + (backtick ? 1 : 2)
  const quotes = [""]
  for (let index = begin; index < text.length; index += 1) {
    const char = text[index]!
    const quote = quotes.at(-1)!
    if (
      !quote &&
      /^(?:case\b|#|<<|\$')/.test(text.slice(index)) &&
      (text.slice(index, index + 2) === "<<" ||
        text.slice(index, index + 2) === "$'" ||
        /[\s;|&()]|^$/.test(text[index - 1] ?? ""))
    )
      return { end: text.length }
    if (char === "\\" && quote !== "'") index += 1
    else if (backtick && char === "`") return { body: text.slice(begin, index), end: index + 1 }
    else if (char === quote) quotes[quotes.length - 1] = ""
    else if (!quote && /['"]/.test(char)) quotes[quotes.length - 1] = char
    else if (!backtick && ((!quote && char === "(") || (quote !== "'" && text.slice(index, index + 2) === "$("))) {
      quotes.push("")
      if (char === "$") index += 1
    } else if (!backtick && !quote && char === ")" && quotes.pop() !== undefined && !quotes.length)
      return { body: text.slice(begin, index), end: index + 1 }
  }
  return { end: text.length }
}

/** Retain executable substitutions in expanding heredocs, where quotes are literal data. */
function heredocSubstitutions(text: string): (string | undefined)[] {
  const bodies: (string | undefined)[] = []
  const starts = /\\[\s\S]|\$\(|`/g
  for (let match = starts.exec(text); match; match = starts.exec(text)) {
    if (match[0].startsWith("\\")) continue
    const substitution = shellSubstitution(text, match.index)
    bodies.push(substitution.body)
    starts.lastIndex = substitution.end
  }
  return bodies
}

/** Read one word, retaining only the provenance needed to reject expansions. */
function shellWord(text: string, start: number): { word: ShellWord; end: number; unreadable: boolean } {
  let value = ""
  let unquoted = ""
  let quote = ""
  let quoted = false
  let dynamic = false
  let syntax = false
  const substitutions: (string | undefined)[] = []
  let index = start
  for (; index < text.length; index += 1) {
    const char = text[index]!
    if (!quote && /[\s;&|<>()]/.test(char)) break
    if (char === "\\" && quote !== "'") {
      const next = text[index + 1]
      if (next === undefined)
        return { word: { value, dynamic, syntax, quoted, substitutions }, end: index + 1, unreadable: true }
      // Double quotes preserve backslashes except before shell-special characters.
      if (quote === '"' && !/[\$`"\\\n]/.test(next)) value += "\\"
      if (next !== "\n") value += next
      quoted ||= next !== "\n"
      index += 1
    } else if (char === quote) quote = ""
    else if (!quote && ["'", '"'].includes(char)) {
      quote = char
      quoted = true
    } else if (quote !== "'" && (char === "`" || text.slice(index, index + 2) === "$(")) {
      const substitution = shellSubstitution(text, index)
      substitutions.push(substitution.body)
      value += text.slice(index, substitution.end)
      index = substitution.end - 1
      dynamic = syntax = true
    } else {
      value += char
      if (!quote) unquoted += char
      if (quote !== "'" && (char === "$" || char === "`")) dynamic = true
      if (quote !== "'" && (char === "`" || (char === "$" && /[({'"]/.test(text[index + 1] ?? "")))) syntax = true
    }
  }
  dynamic ||= /\{[^}]*[,]|\{[^}]*\.\./.test(unquoted)
  syntax ||= /^[{}]$/.test(unquoted)
  return {
    word: { value, dynamic, syntax, quoted, substitutions, glob: /[*?[]/.test(unquoted) },
    end: index,
    unreadable: quote !== "",
  }
}

/** Split simple commands and remove only literal redirections and inert heredoc bodies. */
function shellCommands(source: string): {
  commands: ShellWord[][]
  uncertain: boolean
  text: string
  unparsedGit: boolean
  pipeInputs: Set<ShellWord[]>
  substitutions: Map<ShellWord[], (string | undefined)[]>
  functions: Map<string, ShellWord[][]>
} {
  const text = source.slice(0, MAX_SHELL_CHARS)
  const commands: ShellWord[][] = []
  const pipeInputs = new Set<ShellWord[]>()
  const groups: { fed: boolean; start: number; close: string; name?: string; pattern?: boolean }[] = []
  let redirectedGroup: number | undefined
  const substitutions = new Map<ShellWord[], (string | undefined)[]>()
  const functions = new Map<string, ShellWord[][]>()
  const heredocs: { delimiter: string; quoted: boolean; tabs: boolean; command: ShellWord[] }[] = []
  let functionName: string | undefined
  let signature = false
  let words: ShellWord[] = []
  let uncertain = source.length > MAX_SHELL_CHARS
  let unparsedGit = false
  let visible = ""
  let index = 0
  while (index < text.length) {
    const rest = text.slice(index)
    if (rest[0] === "#") {
      index = text.indexOf("\n", index)
      if (index < 0) break
      continue
    }
    const redirect = /^(?:\d*(?:>>|>&|>|<<-|<<<|<<|<)|&>>?)/.exec(rest)?.[0]
    if (redirect) {
      const operator = redirect.replace(/^\d+/, "")
      index += redirect.length
      while (/[ \t]/.test(text[index] ?? "") && index < text.length) index += 1
      const target = shellWord(text, index)
      if (["<<", "<<-", "<<<"].includes(operator)) {
        pipeInputs.add(words)
        if (redirectedGroup !== undefined) commands.slice(redirectedGroup).forEach((command) => pipeInputs.add(command))
      }
      substitutions.set(words, [...(substitutions.get(words) ?? []), ...(target.word.substitutions ?? [])])
      index = target.end
      const literal = target.word.value !== "" && !target.word.dynamic && !target.word.glob && !target.word.syntax
      if (["<<", "<<-"].includes(operator) && literal) {
        heredocs.push({
          delimiter: target.word.value,
          quoted: target.word.quoted === true,
          tabs: operator === "<<-",
          command: words,
        })
        uncertain ||= !target.word.quoted
      } else {
        const supported =
          [">", ">>", "&>", "&>>"].includes(operator) ||
          (operator === ">&" && /^(?:\d+|-)$/.test(target.word.value) && /^\d/.test(redirect)) ||
          redirect === "<"
        uncertain ||= !supported || !literal || target.unreadable
        unparsedGit ||= !literal && mentionsGit(target.word.value)
      }
      visible += ` ${redirect}${target.word.value}`
      continue
    }
    const separator = /^(?:&&|\|\||\|&|[;&|\n])/.exec(rest)?.[0]
    if (separator) {
      if (separator === ";" && /[;&]/.test(text[index + 1] ?? "") && groups.at(-1)?.close === "esac")
        groups.at(-1)!.pattern = true
      if (!(separator === "\n" && !words.length && pipeInputs.has(words))) {
        commands.push(words)
        words = []
        if (["|", "|&"].includes(separator) || groups.some((group) => group.fed)) pipeInputs.add(words)
      }
      redirectedGroup = undefined
      index += separator.length
      visible += separator
      if (separator === "\n") {
        for (const doc of heredocs.splice(0)) {
          let closed = false
          let body = ""
          while (index < text.length) {
            const end = text.indexOf("\n", index)
            const line = text.slice(index, end < 0 ? text.length : end)
            index = end < 0 ? text.length : end + 1
            if ((doc.tabs ? line.replace(/^\t+/, "") : line) === doc.delimiter) {
              closed = true
              break
            }
            if (!doc.quoted) {
              body += line + "\n"
              visible += line + "\n"
              unparsedGit ||= mentionsGit(line)
            }
          }
          if (!doc.quoted)
            substitutions.set(doc.command, [...(substitutions.get(doc.command) ?? []), ...heredocSubstitutions(body)])
          uncertain ||= !closed
        }
      }
      continue
    }
    if (/\s/.test(text[index]!)) {
      visible += text[index++]
      continue
    }
    const token = /[()]/.test(text[index]!)
      ? { word: { value: text[index]!, dynamic: false, syntax: true } as ShellWord, end: index + 1, unreadable: false }
      : shellWord(text, index)
    const value = token.word.value
    const literal = !token.word.quoted && !token.word.dynamic
    const head = words.every((word) => word.scope || (word.syntax && /^[{(]$/.test(word.value)) || word.value === "!")
    const prefix = words.map((word) => word.value).join(" ")
    const group = groups.at(-1)
    if (literal && prefix === "function" && /^[A-Za-z_][A-Za-z0-9_]*$/.test(value)) {
      functionName = value
      words.forEach((word) => (word.scope = true))
      token.word.scope = true
    } else if (value === "(" && /^(?:function )?[A-Za-z_][A-Za-z0-9_]*$/.test(prefix) && /^\(\s*\)/.test(rest)) {
      functionName = words.at(-1)!.value
      signature = true
      words.forEach((word) => (word.scope = true))
      token.word.scope = true
    } else if (value === ")" && signature) {
      signature = false
      token.word.scope = true
    } else if (group?.close === "esac" && group.pattern && /^[()]$/.test(value)) {
      token.word.scope = true
      if (value === ")") {
        words.forEach((word) => (word.scope = true))
        group.pattern = false
      }
    } else if (
      literal &&
      ((head && /^(?:if|while|until|for|select|case)$/.test(value)) || (token.word.syntax && /^[{(]$/.test(value)))
    ) {
      const name = functionName ?? (/^function [A-Za-z_][A-Za-z0-9_]*$/.test(prefix) ? words.at(-1)!.value : undefined)
      if (name) words.forEach((word) => (word.scope = true))
      functionName = undefined
      groups.push({
        fed: pipeInputs.has(words) || groups.some((scope) => scope.fed),
        start: commands.length,
        close: value === "{" ? "}" : value === "(" ? ")" : value === "if" ? "fi" : value === "case" ? "esac" : "done",
        name,
        pattern: value === "case",
      })
      token.word.scope = token.word.syntax = true
    } else if (literal && group && value === group.close && (head || /^[})]$/.test(value))) {
      groups.pop()
      redirectedGroup = group.start
      if (group.name)
        functions.set(group.name, [...(functions.get(group.name) ?? []), ...commands.slice(group.start), words])
      token.word.scope = token.word.syntax = true
    } else if (literal && head && /^(?:then|else|elif|do)$/.test(value) && group) {
      token.word.scope = token.word.syntax = true
    }
    token.word.syntax ||= token.unreadable
    words.push(token.word)
    uncertain ||= token.unreadable || token.word.syntax === true
    visible += text.slice(index, token.end)
    index = token.end
  }
  commands.push(words)
  return {
    commands,
    uncertain: uncertain || heredocs.length > 0,
    text: visible,
    unparsedGit,
    pipeInputs,
    substitutions,
    functions,
  }
}

const WRAPPER_OPERANDS: Readonly<Record<string, string>> = {
  env: "-u --unset -C --chdir",
  command: "",
  nohup: "",
  sudo: "-u --user -g --group -h --host -p --prompt -C -T -R -D --chdir",
  timeout: "-s --signal -k --kill-after",
  exec: "",
  time: "",
  builtin: "",
  nice: "-n --adjustment",
  ionice: "-c --class -n --classdata -p --pid -P --pgid -u --uid",
  flock: "-w --wait -E --conflict-exit-code",
  setsid: "",
  stdbuf: "-i -o -e --input --output --error",
  doas: "-u -C",
  strace: "-o --output -e --trace -p --attach -P --trace-path -s --string-limit -u --user",
  xargs:
    "-a --arg-file -d --delimiter -E -I -L -n -P -s --eof --replace --max-lines --max-args --max-procs --max-chars --process-slot-var",
}
const WRAPPER_FLAGS: Readonly<Record<string, string>> = {
  env: "-i -0 --ignore-environment --null",
  command: "-p",
  nohup: "",
  builtin: "",
  sudo: "-n -E -H -S -b -k -K -v -V -l --preserve-env --non-interactive",
  timeout: "--foreground --preserve-status --verbose",
  exec: "-l",
  time: "-p -v",
  nice: "",
  ionice: "-t",
  flock: "-e -x -n --close --exclusive --shared --unlock --no-fork",
  setsid: "-f --wait --fork",
  stdbuf: "",
  doas: "-n -S -l",
  strace: "-f -q -t",
  xargs: "-0 -p -r -t -x --no-run-if-empty --null --verbose",
}

/** Preserve Windows and path-qualified executable identities. */
function executableName(value: string): string {
  const basename = value.replaceAll("\\", "/").split("/").at(-1) ?? ""
  return basename.replace(/\.exe$/i, "").toLowerCase()
}

/** Remove catalogued wrapper operands without losing guarded candidates on uncertainty. */
function executableWords(input: ShellWord[]): { words: ShellWord[]; uncertain: boolean; changesDirectory: boolean } {
  let index = 0
  let uncertain = false
  let changesDirectory = false
  while (index < input.length) {
    while (/^[A-Za-z_][A-Za-z0-9_]*=/.test(input[index]?.value ?? "")) {
      uncertain = true
      index += 1
    }
    const wrapper = executableName(input[index]?.value ?? "")
    if (wrapper === "command" && ["-v", "-V"].includes(input[index + 1]?.value ?? ""))
      return { words: [], uncertain, changesDirectory }
    if (!Object.hasOwn(WRAPPER_OPERANDS, wrapper)) break
    uncertain ||= wrapper === "xargs"
    if (input[index]?.dynamic || input[index]?.glob) uncertain = true
    index += 1
    while (input[index]?.value.startsWith("-")) {
      const word = input[index++]!
      const option = word.value
      if (option === "--") break
      changesDirectory ||=
        (wrapper === "env" && /^(?:-C|--chdir(?:=|$))/.test(option)) ||
        (wrapper === "sudo" && /^(?:-[DR]|--(?:chdir|chroot)(?:=|$))/.test(option))
      if (wrapper === "env" && /^(?:-[A-Za-z0-9]*S|--split-string(?:=|$))/.test(option)) uncertain = true
      const operands = WRAPPER_OPERANDS[wrapper]!.split(" ").filter(Boolean)
      const xargsOperand = wrapper === "xargs" ? /^-[0prtx]+([adEILnPs])(.*)$/.exec(option) : null
      const operand = operands.includes(option) || xargsOperand?.[2] === ""
      const attached = operands.some(
        (flag) => option.startsWith(flag + "=") || (flag.length === 2 && option.startsWith(flag) && option.length > 2),
      )
      uncertain ||=
        word.dynamic ||
        (!operand &&
          !attached &&
          !xargsOperand &&
          !WRAPPER_FLAGS[wrapper]?.split(" ").includes(option) &&
          !(wrapper === "nice" && /^-\d+$/.test(option))) ||
        (wrapper === "env" && option === "-S")
      if (operand) {
        uncertain ||= !input[index] || input[index]!.dynamic
        index += 1
      }
    }
    if (["timeout", "flock"].includes(wrapper)) index += 1
  }
  const words = input.slice(index)
  if (words[0]) words[0] = { ...words[0], value: executableName(words[0].value) }
  return { words, uncertain, changesDirectory }
}

/** Remove known Git global options and flag executable configuration. */
function gitWords(words: ShellWord[]): { words: ShellWord[]; uncertain: boolean; changesDirectory: boolean } {
  let index = 1
  let uncertain = false
  let changesDirectory = false
  while (words[index]?.value.startsWith("-")) {
    const word = words[index++]!
    const option = word.value
    changesDirectory ||= /^(?:-C|--(?:git-dir|work-tree)(?:=|$))/.test(option)
    const separate = ["-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"].includes(option)
    uncertain ||= word.dynamic || word.syntax === true
    if (separate) {
      const setting = words[index]
      uncertain ||= !setting || setting.dynamic || setting.glob === true || setting.syntax === true
      uncertain ||= option !== "-c" || !/^user\.(?:name|email)=[^\r\n]*$/.test(setting?.value ?? "")
      index += 1
    } else if (
      !["--no-pager", "--no-replace-objects", "--no-optional-locks", "--literal-pathspecs", "--version"].includes(
        option,
      )
    ) {
      if (!/^(?:-C.|-c.|--(?:git-dir|work-tree|namespace|config-env)=)/.test(option))
        return { words, uncertain: true, changesDirectory }
      uncertain = true
    }
    if (option === "--version")
      return { words: [words[0]!, { ...word, value: "version" }, ...words.slice(index)], uncertain, changesDirectory }
  }
  return { words: [words[0]!, ...words.slice(index)], uncertain, changesDirectory }
}

/** Classify supported shell shapes; unsupported Git syntax always asks. */
export function classifyCommand(command: string, mode: GuardrailMode = "standard"): Classification {
  try {
    const classification = classifyShellCommand(command, mode, 0)
    return { ...classification, changesDirectory: classification.changesDirectory ?? false }
  } catch {
    return {
      kind: "unparsed-git",
      command,
      reason: "cannot safely classify Git shell syntax after parser failure",
      changesDirectory: true,
    }
  }
}

/** Identify a literal script path that does not read the shell's input stream. */
function hasScriptOperand(word: ShellWord | undefined): boolean {
  return Boolean(
    word &&
    word.value &&
    !word.value.startsWith("-") &&
    !word.dynamic &&
    !word.glob &&
    !word.syntax &&
    !/^(?:-|\/dev\/stdin|\/dev\/fd\/\d+|\/proc\/self\/fd\/\d+)$/.test(word.value),
  )
}

/** Bound literal shell recursion and retain the strongest guarded operation. */
function classifyShellCommand(command: string, mode: GuardrailMode, depth: number, stdin = false): Classification {
  const unparsed: Classification = { kind: "unparsed-git", command, reason: "cannot safely classify Git shell syntax" }
  if (depth > MAX_SHELL_DEPTH || command.length > MAX_SHELL_CHARS) return unparsed
  const parsed = shellCommands(command)
  let winner: Classification = { kind: "other", command, reason: "no guarded pattern matched" }
  const windowsAmbiguous = /(?:[A-Za-z]:\\|\\\\)[^\r\n]*(?:git|gh)\.exe\b/i.test(parsed.text)
  let relevant = parsed.unparsedGit || windowsAmbiguous
  let uncertain = parsed.uncertain || windowsAmbiguous
  let changesDirectory = false
  for (const raw of parsed.commands) {
    relevant ||= raw.some((word) => (word.dynamic || word.syntax) && mentionsGit(word.value))
    const stdinFed = stdin || parsed.pipeInputs.has(raw)
    for (const body of [...raw.flatMap((word) => word.substitutions ?? []), ...(parsed.substitutions.get(raw) ?? [])]) {
      const substitution = body === undefined ? undefined : classifyShellCommand(body, mode, depth + 1, stdinFed)
      relevant ||= substitution === undefined || substitution.kind !== "other"
      changesDirectory ||= substitution?.changesDirectory === true
    }
    const input = stdinFed
      ? raw.filter(
          (word) => !word.scope && !(word.syntax && /^[{}()]$/.test(word.value)) && (word.quoted || word.value !== "!"),
        )
      : raw
    const normalized = executableWords(input)
    changesDirectory ||= normalized.changesDirectory
    relevant ||= raw
      .slice(0, raw.length - normalized.words.length)
      .some((word) => mentionsGit(word.value.replace(/^(?:-[A-Za-z0-9]*S|--[^=]+=)/, "")))
    uncertain ||= normalized.uncertain
    if (["export", "declare", "typeset"].includes(raw[0]?.value ?? ""))
      uncertain ||= raw.slice(1).some((word) => /^(?:GIT_\w*|SSH_ASKPASS|EDITOR|PAGER|VISUAL)(?:=|$)/.test(word.value))
    let words = normalized.words
    if (!words.length) continue
    const first = words[0]!
    const program = first.value
    changesDirectory ||= [
      "cd",
      "chdir",
      "pushd",
      "popd",
      "set-location",
      "sl",
      "push-location",
      "pop-location",
    ].includes(program)
    if (stdinFed && !stdin) {
      const name = input[input.length - words.length]!.value
      for (const body of parsed.functions.get(name) ?? []) {
        if (parsed.pipeInputs.has(body)) continue
        parsed.pipeInputs.add(body)
        parsed.commands.push(body)
      }
    }
    const values = words.map((word) => word.value)
    const dynamic = first.dynamic || first.glob
    relevant ||= Boolean(dynamic) || ["git", "gh"].includes(program)
    uncertain ||= Boolean(dynamic) || normalized.uncertain
    if (SHELLS.has(program)) {
      let index = 1
      while (values[index]?.startsWith("-")) {
        const option = values[index++]!
        if (option === "--") break
        if (["-o", "-O"].includes(option)) index += 1
      }
      const source = words[index]
      if (
        index === 2 &&
        /^-[a-z]*c$/.test(values[1] ?? "") &&
        source &&
        !source.dynamic &&
        !normalized.uncertain &&
        !/[{}]/.test(source.value)
      ) {
        const candidate = classifyShellCommand(source.value, mode, depth + 1, stdinFed)
        changesDirectory ||= candidate.changesDirectory === true
        relevant ||= candidate.kind !== "other"
        if (SEVERITY[candidate.kind] > SEVERITY[winner.kind]) winner = { ...candidate, command }
      } else {
        uncertain = true
        relevant ||=
          mentionsGit(parsed.text) ||
          (stdinFed && (!hasScriptOperand(source) || values.slice(1, index).some((arg) => /^-[^-]*s/.test(arg)))) ||
          words.some((word) => /[$`]/.test(word.value) && !word.substitutions?.length)
      }
      continue
    }
    if (OPAQUE.has(program)) {
      // These programs interpret another language or argument stream, outside this classifier.
      uncertain = true
      relevant ||=
        mentionsGit(parsed.text) ||
        (stdinFed && !(["source", "."].includes(program) && hasScriptOperand(words[1]))) ||
        words.some((word) => /['"\\]/.test(word.value) || (/[$`]/.test(word.value) && !word.substitutions?.length)) ||
        (["pwsh", "powershell"].includes(program) &&
          values.slice(1).some((arg) => /^-(?:e|ec|encodedcommand)$/i.test(arg)))
    }
    if (
      SEVERITY[winner.kind] < SEVERITY["destructive-local"] &&
      raw.some((word) => word.dynamic && word.syntax) &&
      /(?:\$\s*\(|`|[<>]\s*\()\s*rm\s+-[^\s]*[rRf]/.test(raw.map((word) => word.value).join(" "))
    ) {
      winner = { kind: "destructive-local", command, reason: "destructive local operation" }
    }
    if (DATA_COMMANDS.has(program)) {
      relevant ||= raw.some((word) => word.syntax) && mentionsGit(values.join(" "))
      continue
    }
    relevant ||= mentionsGit(values.join(" "))
    if (program === "git") {
      const git = gitWords(words)
      changesDirectory ||= git.changesDirectory
      words = git.words
      uncertain ||=
        git.uncertain || words.slice(1).some((word) => word.dynamic || word.syntax) || Boolean(words[1]?.glob)
    } else if (program === "gh") {
      uncertain ||= words.slice(1).some((word) => word.dynamic || word.syntax) || values[1]?.startsWith("-") === true
      if (words.some((word) => word.syntax || word.dynamic)) continue
    } else if (mentionsGit(values.join(" "))) uncertain = true
    const candidate = classifySegment(
      words.map((word) => word.value),
      command,
      mode,
    )
    if (SEVERITY[candidate.kind] > SEVERITY[winner.kind]) winner = candidate
    const execIndex = program === "find" ? values.findIndex((value) => ["-exec", "-execdir"].includes(value)) : -1
    if (
      (normalized.uncertain ||
        OPAQUE.has(program) ||
        execIndex > 0 ||
        !["git", "gh", "find", "truncate"].includes(program)) &&
      !["docker", "npm", "uv", "twine", "rm"].includes(program)
    ) {
      // Uncertain wrappers cannot erase a literal destructive operation they may execute.
      for (let index = execIndex > 0 ? execIndex + 1 : 1; index < words.length; index += 1) {
        if (DATA_COMMANDS.has(words[index]!.value)) break
        if (/^(?:rm|npm|uv|twine|docker)\s/.test(words[index]!.value)) {
          const guarded = classifySegment(words[index]!.value.split(/\s+/), command, mode)
          if (SEVERITY[guarded.kind] > SEVERITY[winner.kind]) winner = guarded
        }
        if (!["rm", "npm", "uv", "twine", "docker"].includes(words[index]!.value)) continue
        const guarded = classifySegment(
          words.slice(index).map((word) => word.value),
          command,
          mode,
        )
        if (SEVERITY[guarded.kind] > SEVERITY[winner.kind]) winner = guarded
        break
      }
    }
  }
  const classification = uncertain && relevant && SEVERITY[winner.kind] < SEVERITY["unparsed-git"] ? unparsed : winner
  return { ...classification, changesDirectory }
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
  const { flags, operands, valid } = gitArguments(words.slice(2), GIT_OPTIONS[verb]!)
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
function classifySegment(words: string[], original: string, mode: GuardrailMode): Classification {
  if (!["git", "gh", "rm", "find", "truncate", "npm", "uv", "twine", "docker"].includes(words[0] ?? ""))
    return { kind: "other", command: original, reason: "no guarded pattern matched" }
  if (words[0] === "gh") {
    const kind = githubClass(words)
    return { kind, command: original, reason: REASONS[kind] }
  }
  if (words[0] === "git" && ["remote", "stash", "worktree"].includes(words[1] ?? "")) {
    const verb = words.findIndex((word, index) => index > 1 && !word.startsWith("-"))
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
              operands.some((arg) => arg === "." || /[*?[]|^\.\/|^:|\.[^/.]+$/.test(arg)))))))
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

// Mirrors the denylist in `paths.ts`: a planted override must not redirect the
// working-tree lookup away from the repository the hook is guarding.
const GIT_OVERRIDE_ENV = [
  "GIT_DIR",
  "GIT_WORK_TREE",
  "GIT_COMMON_DIR",
  "GIT_INDEX_FILE",
  "GIT_OBJECT_DIRECTORY",
  "GIT_ALTERNATE_OBJECT_DIRECTORIES",
  "GIT_NAMESPACE",
  "GIT_PREFIX",
  "GIT_CONFIG_GLOBAL",
  "GIT_CONFIG_SYSTEM",
  "GIT_CONFIG_COUNT",
  "GIT_CONFIG_PARAMETERS",
] as const

// `GIT_CONFIG_KEY_<n>` / `GIT_CONFIG_VALUE_<n>` inject arbitrary config, including
// `core.fsmonitor`, whose path Git executes. They are stripped by prefix.
const GIT_CONFIG_ENV_PREFIXES = ["GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"] as const

// Repository-controlled settings that can execute a program during `git status`.
// `-c` wins over the repository, the user, and system config.
const STATUS_CONFIG_OVERRIDES = [
  "-c",
  "core.fsmonitor=false",
  "-c",
  "core.untrackedCache=false",
  "-c",
  `core.excludesFile=${devNull}`,
]

const STATUS_TIMEOUT_MS = 1_000
const STATUS_MAX_BYTES = 4_000_000
const MAX_NAMED_PATHS = 10
const MAX_NAMED_PATH_CHARS = 120
const MAX_NAMED_TOTAL_CHARS = 600

/** Strip environment overrides that let a planted value redirect or execute code. */
function cleanGitEnv(): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env }
  for (const key of GIT_OVERRIDE_ENV) delete env[key]
  for (const key of Object.keys(env)) {
    if (GIT_CONFIG_ENV_PREFIXES.some((prefix) => key.startsWith(prefix))) delete env[key]
  }
  return env
}

/**
 * List working-tree paths with uncommitted changes. Bounded and failure-safe:
 * `undefined` means the escalation message stays unchanged instead of the
 * permission decision waiting on or failing with a broken repository lookup.
 * `ignored` holds plugin-owned relative paths, which are never someone else's
 * work.
 */
export function dirtyPaths(cwd: string, ignored: readonly string[] = []): string[] | undefined {
  const result = spawnSync(
    "git",
    [...STATUS_CONFIG_OVERRIDES, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
    {
      cwd,
      env: cleanGitEnv(),
      encoding: "utf8",
      timeout: STATUS_TIMEOUT_MS,
      maxBuffer: STATUS_MAX_BYTES,
    },
  )
  if (result.error || result.status !== 0) return undefined
  const paths = parseStatusPaths(result.stdout)
  return ignored.length > 0 ? paths.filter((path) => !underAny(path, ignored)) : paths
}

function parseStatusPaths(output: string): string[] {
  const records = output.split("\0")
  const paths: string[] = []
  for (let index = 0; index < records.length; index += 1) {
    const record = records[index]
    if (!record || record.length < 4) continue
    const path = record.slice(3)
    if (!path) continue
    paths.push(path)
    // A rename or copy report carries the source path in the next NUL record.
    const code = record.slice(0, 2)
    if (code.startsWith("R") || code.startsWith("C")) {
      const source = records[index + 1]
      if (source) {
        paths.push(source)
        index += 1
      }
    }
  }
  return paths
}

function underAny(path: string, prefixes: readonly string[]): boolean {
  return prefixes.some((prefix) => path === prefix || path.startsWith(`${prefix}/`))
}

function foreignDirtyPaths(dirty: readonly string[], scope: readonly string[] | undefined): string[] {
  if (scope === undefined) return [...dirty]
  // An empty recorded filter means the run covers every changed path.
  if (scope.length === 0) return []
  return dirty.filter((path) => !underAny(path, scope))
}

/**
 * Render one untrusted filename for the approval prompt.
 *
 * Git reports whatever bytes the filesystem holds, including newlines, ANSI
 * escapes, and text shaped like the prompt's own delimiters. Flattening control
 * characters and whitespace keeps a filename from forging extra lines, and the
 * length caps keep a single path from crowding out the rest of the message.
 */
function sanitizePath(path: string): string {
  const cleaned = path
    .replace(/[\u0000-\u001f\u007f-\u009f]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
  if (cleaned === "") return "<unprintable path>"
  return cleaned.length > MAX_NAMED_PATH_CHARS ? `${cleaned.slice(0, MAX_NAMED_PATH_CHARS - 1)}…` : cleaned
}

function formatPaths(paths: readonly string[]): string {
  const shown: string[] = []
  let total = 0
  for (const raw of paths) {
    if (shown.length >= MAX_NAMED_PATHS) break
    const path = sanitizePath(raw)
    if (shown.length > 0 && total + path.length + 2 > MAX_NAMED_TOTAL_CHARS) break
    shown.push(path)
    total += path.length + 2
  }
  const remaining = paths.length - shown.length
  return remaining > 0 ? `${shown.join(", ")}, and ${remaining} more` : shown.join(", ")
}

function discardWarning(context: DiscardContext | undefined): string {
  if (!context || context.scopeUnknown || context.dirtyPaths.length === 0) return ""
  const foreign = foreignDirtyPaths(context.dirtyPaths, context.scopePaths)
  if (foreign.length === 0) return ""
  const named = formatPaths(foreign)
  if (context.scopePaths === undefined) return ` This would discard uncommitted changes: ${named}.`
  return ` This would discard changes that are not part of this run: ${named}.`
}

/**
 * Decide whether to escalate a permission decision. `undefined` means the
 * caller must leave the configured effect untouched.
 */
export function decide(
  classification: Classification,
  mode: GuardrailMode,
  grant: GrantState,
  discard?: DiscardContext,
): Decision | undefined {
  if (mode === "off") return undefined
  if (classification.kind === "unparsed-git") {
    return {
      effect: "ask",
      kind: classification.kind,
      message: `opencode-rig: ${classification.reason}; confirmation required (${classification.command}).`,
    }
  }
  if (classification.kind === "remote-mutation") {
    return {
      effect: "ask",
      kind: classification.kind,
      message: `opencode-rig: remote mutations are never automatic (${classification.command}). Publication stays human-owned.`,
    }
  }
  if (classification.kind === "destructive-local" || classification.kind === "history-rewrite") {
    const warning = discardWarning(discard)
    return {
      effect: "ask",
      kind: classification.kind,
      message: `opencode-rig: ${classification.reason} requires confirmation (${classification.command}). No approval grant covers it.${warning}`,
    }
  }
  if (classification.kind === "plain-commit") {
    // A grant restores the permission the user already configured; it never
    // upgrades a configured `ask`, and `allow` is not ours to grant.
    if (grant.status === "granted") return undefined
    return {
      effect: "ask",
      kind: classification.kind,
      message:
        "opencode-rig: no local Git grant. Approve this commit, record a project grant, or deny it. Grants never cover push, force, destructive operations, or history rewrites.",
    }
  }
  return undefined
}
