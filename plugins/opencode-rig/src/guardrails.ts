/**
 * Shell command classification for Git guardrails.
 *
 * The permission `evaluate` hook runs after configured rules. This module only
 * decides when to escalate an `allow` to `ask`; it never auto-allows a command
 * the user configured as `ask`, and a configured `deny` stays final before the
 * hook is consulted. Escalation messages may name the working-tree paths a
 * destructive command would discard; that lookup is bounded and optional, and
 * a failure there leaves the configured effect and the plain message intact.
 *
 * Shell text stays quiet only when it parses in a small, positively modelled grammar (see
 * `ShellGrammar`); every other shell construct asks, whatever the Git classification found.
 */

import { devNull, hostname } from "node:os"
import type { GrantState } from "./git-grant.ts"
import type { GuardrailMode } from "./options.ts"
import { classifySegment, runGit } from "./util/git.ts"

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
interface ShellWord {
  value: string
  raw?: string
  dynamic: boolean
  glob?: boolean
  syntax?: boolean
  ansiQuoted?: boolean
  quoted?: boolean
  scope?: boolean
  unsafeExpression?: boolean
  substitutions?: (string | undefined)[]
}

const MAX_SHELL_CHARS = 65_536
const MAX_SHELL_DEPTH = 16
const GIT_MENTION = /\b(?:git|gh)\b/i
const DATA_COMMANDS = new Set(["echo", "printf", "grep", "which", "type", "where"])
/** POSIX-family shells whose command strings and scripts the classifier reads as shell code. */
const SHELLS = new Set(["sh", "bash", "dash", "zsh", "ksh", "ash", "mksh", "yash", "posh", "rbash", "oksh"])
/** Shells whose language is not POSIX shell code: the grammar never reads them. */
const NON_POSIX_SHELLS = new Set(["csh", "tcsh", "fish"])
const OPAQUE = new Set(
  "eval alias shopt source . su runuser sg script watch pwsh powershell cmd xargs trap csh tcsh fish".split(" "),
)
const STDIN_OPTIONS: Readonly<Record<string, string>> = {
  cat: "-A -b -e -E -n -s -t -T -u -v --show-all --number-nonblank --show-ends --number --squeeze-blank --show-tabs --show-nonprinting",
  tee: "-a -i -p --append --ignore-interrupts --output-error=",
  grep: "-E -F -G -P -e= -f= -i -v -w -x -z -c -l -L -m= -n -b -H -h -o -q -s -a -I -r -R -A= -B= -C= --regexp= --file= --ignore-case --invert-match --word-regexp --line-regexp --count --files-with-matches --files-without-match --max-count= --line-number --only-matching --quiet --recursive --after-context= --before-context= --context= --color? --include= --exclude= --exclude-dir= --exclude-from=",
  sed: "-n -E -r -e= -s --expression=",
  head: "-n= -c= -q -v -z --lines= --bytes= --quiet --silent --verbose --zero-terminated",
  tail: "-n= -c= -q -v -z --lines= --bytes= --quiet --silent --verbose --zero-terminated",
  wc: "-c -m -l -L -w --bytes --chars --lines --max-line-length --words",
  sort: "-b -d -f -g -h -i -M -n -R -r -V -c -C -m -s -u -z -k= -t= -S= --ignore-leading-blanks --dictionary-order --ignore-case --general-numeric-sort --human-numeric-sort --ignore-nonprinting --month-sort --numeric-sort --random-sort --reverse --version-sort --check --merge --stable --unique --zero-terminated --key= --field-separator= --buffer-size= --parallel=",
  jq: "-c -r -R -s -n -e -S -M -C -j -a --compact-output --raw-output --raw-input --slurp --null-input --exit-status --sort-keys --monochrome-output --color-output --join-output --ascii-output --tab --indent= --arg: --argjson: --rawfile: --slurpfile: --args",
  cut: "-b= -c= -f= -d= -s -z --bytes= --characters= --fields= --delimiter= --only-delimited --complement --output-delimiter= --zero-terminated",
  tr: "-c -C -d -s -t --complement --delete --squeeze-repeats --truncate-set1",
  uniq: "-c -d -D -i -u -z -f= -s= -w= --count --repeated --ignore-case --unique --zero-terminated --skip-fields= --skip-chars= --check-chars=",
  nl: "-b= -d= -f= -h= -i= -l= -n= -p -s= -v= -w= --body-numbering= --section-delimiter= --footer-numbering= --header-numbering= --line-increment= --join-blank-lines= --number-format= --no-renumber --number-separator= --starting-line-number= --number-width=",
  column: "-t -c= -s= -o= -x --table --output-width= --separator= --output-separator= --fillrows",
  paste: "-d= -s -z --delimiters= --serial --zero-terminated",
  fold: "-b -s -w= --bytes --spaces --width=",
  rev: "",
  comm: "-1 -2 -3 -z --check-order --nocheck-order --output-delimiter= --total --zero-terminated",
  base64: "-d -D -i -b= -w= -o= --decode --ignore-garbage --wrap= --input= --output=",
}
const NO_STDIN = new Set("echo printf which type where true false : export declare typeset".split(" "))

/** Fence delimiters and language tags are presentation; their contents use the same command grammar. */
function withoutFences(text: string): string {
  return text.replace(/^```[^\s`]*[ \t]*\r?$/gm, "")
}

/** Detect literal and quote-fragmented Git names without interpreting shell programs. */
function mentionsGit(text: string): boolean {
  return GIT_MENTION.test(text) || GIT_MENTION.test(text.replace(/\\\n|['"\\]/g, ""))
}

/** Prove a raw assignment value literal or numeric without interpreting shell context. */
function rawInertValue(text: string): boolean {
  const number = String.raw`(?:[+-]?[0-9]+|[0-9]+#[0-9A-Za-z@_]+)`
  const numeric = String.raw`(?:${number}|\$[#?$]|\$\{#[A-Za-z_][A-Za-z0-9_]*(?:\[@\])?\})`
  const boundary = String.raw`(?=$|[\s;&|()<>])`
  if (/^[A-Za-z0-9_./:@%+,=-]*(?=$|[\s;&|<>])/.test(text)) return true
  if (/^(?:$|[\s;&|<>])/.test(text)) return true
  if (new RegExp(String.raw`^(?:${numeric}|"${numeric}"|'${number}'|""|'')${boundary}`).test(text)) return true
  // Arithmetic expansions yield numbers. Scan delimiters only; their contents still
  // participate in the independent raw producer scan and the expression whitelist.
  const quoted = text.startsWith('"')
  const start = quoted ? 1 : 0
  if (text.startsWith("$((", start)) {
    let depth = 2
    for (let index = start + 3; index < text.length; index += 1) {
      // Quoted or escaped delimiters cannot prove where a numeric result ends.
      if (/['"`\\]/.test(text[index]!)) return false
      if (text[index] === "(") depth += 1
      if (text[index] === ")") depth -= 1
      if (depth) continue
      const end = index + 1 + (quoted && text[index + 1] === '"' ? 1 : 0)
      return (!quoted || end === index + 2) && /^(?:$|[\s;&|()<>])/.test(text.slice(end))
    }
  }
  // Array values are numeric only when every element is an integer literal.
  return /^\(\s*(?:[+-]?[0-9]+(?:\s+[+-]?[0-9]+)*)?\s*\)(?=$|[\s;&|()<>])/.test(text)
}

/** Expose quote concatenation and encoded syntax for the independent raw predicates. */
function normalizedShellText(text: string): string {
  return text
    .replace(/\$'((?:\\[\s\S]|[^'\\])*)'|\$"((?:\\[\s\S]|[^"\\])*)"/g, (_, single, double) =>
      (single ?? double).replace(
        /\\(?:x[\da-fA-F]{1,2}|u[\da-fA-F]{1,4}|U[\da-fA-F]{1,8}|[0-7]{1,3}|c[\s\S]|[\s\S])/g,
        (escape: string) => {
          const code = escape.slice(1)
          if (/^[xuU]/.test(code) && code.length > 1) {
            const point = Number.parseInt(code.slice(1), 16)
            return point <= 0x10ffff ? String.fromCodePoint(point) : escape
          }
          if (/^[0-7]/.test(code)) return String.fromCharCode(Number.parseInt(code, 8) & 0xff)
          if (code.startsWith("c") && code.length === 2) return String.fromCharCode(code.charCodeAt(1) & 31)
          return (
            (
              {
                a: "\x07",
                b: "\b",
                e: "\x1b",
                E: "\x1b",
                f: "\f",
                n: "\n",
                r: "\r",
                t: "\t",
                v: "\v",
                "'": "'",
                '"': '"',
                "\\": "\\",
                "?": "?",
              } as Record<string, string>
            )[code] ?? escape
          )
        },
      ),
    )
    .replace(/\\\r?\n|['"]|\\(?=[\s\S])/g, "")
}

/** Preserve generic indexing checks while exempting only literal builtin target subscripts. */
function arithmeticTargets(text: string): { text: string; arithmetic: boolean } {
  let arithmetic = false
  const masked = text.replace(
    /\b(printf|read|mapfile|readarray|getopts|wait|declare|typeset|local|export|readonly|unset|let)\b([^;\n]*)/g,
    (_, builtin: string, tail: string) => {
      let nextTarget = false
      return (
        builtin +
        tail.replace(/\S+/g, (word) => {
          const flag = word.replace(/['"\\]/g, "")
          const target = builtin === "printf" || builtin === "wait" ? nextTarget : !flag.startsWith("-")
          nextTarget = builtin === "printf" ? flag === "-v" : /^-[A-Za-z]*p$/.test(flag)
          if (!target) return word
          // A target can begin with '[' or hide its name behind an expansion.
          arithmetic ||= /\[(?!(?:[0-9]+|[@*])\])/.test(word)
          return word.replace(/\[(?:[0-9]+|[@*])\]/g, "_")
        })
      )
    },
  )
  return { text: masked, arithmetic }
}

/** Find value producers anywhere in raw text, including quoted and saved bodies. */
function rawDynamicAssignment(text: string): boolean {
  if (/\b(?:read|mapfile|readarray|getopts)\b|\bprintf["']*\s+(?:--\s+)?["']*-["']*v/.test(text)) return true
  if (/\bwait\b[^;\n]*\s-[A-Za-z]*p\b/.test(text)) return true
  for (const match of text.matchAll(/[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]\r\n]*\])?(?:\+|:)?=(?!=)/g))
    if (
      /['"`]|\$\(/.test(match[0]) ||
      /['"]/.test(text[match.index - 1] ?? "") ||
      !rawInertValue(text.slice(match.index + match[0].length))
    )
      return true
  for (const match of text.matchAll(/\b(?:declare|typeset|local|export|readonly|unset|let)\b([^;\n]*)/g))
    if (/(?:^|\s)[^\s=]*[$`]/.test(match[1]!)) return true
  // The previous command's last argument is also an implicit arithmetic input.
  if (/\b_\b/.test(text) && /['"`]/.test(text) && /(?:\(\(|\$\[)[\s\S]*?\b_\b/.test(text)) return true
  for (const match of text.matchAll(/\b(?:for|select)\s+[A-Za-z_][A-Za-z0-9_]*(?:\s+in\b([^;\n]*))?/g))
    if (match[1] === undefined || !/^\s*(?:[+-]?[0-9]+(?:\s+[+-]?[0-9]+)*)?\s*$/.test(match[1])) return true
  for (const match of text.matchAll(/\bset\s+--([^;\n]*)|\bshift\s+([^;\n]+)/g))
    if (!/^\s*(?:[+-]?[0-9]+(?:\s+[+-]?[0-9]+)*)?\s*$/.test(match[1] ?? match[2]!)) return true
  return false
}

/** Fail closed when raw producers coexist with any arithmetic-capable spelling. */
function rawDynamicCode(source: string): boolean {
  const forms = [source, normalizedShellText(source)]
  if (
    forms.some((text) =>
      /\$\{[(~]|\$\{[A-Za-z_][A-Za-z0-9_]*@P\}|\bsetopt\b[^;\n]*\b(?:globsubst|GLOB_SUBST)\b/.test(text),
    )
  )
    return true
  const arithmetic = forms.some((form) => {
    const targets = arithmeticTargets(form)
    const text = targets.text
    return (
      targets.arithmetic ||
      /\(\(|\$\[|\blet\b|\$\{!|\$\{\(|@P/.test(text) ||
      /\$\{(?:[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?|[@*]|[0-9]+):(?!(?:[-+=?]))/.test(text) ||
      /[A-Za-z0-9_}\]]\[(?![@*]\])/.test(text) ||
      /(?:^|\W)-(?:eq|ne|lt|le|gt|ge)\b/.test(text) ||
      /\btest\b[^;\n]*\s-v\b|\[\s[^;\n]*\s-v\b|\[\s+-v\b|\[\[(?:(?!\]\])[\s\S])*?\s-v\b/.test(text) ||
      /\b(?:declare|typeset|local|readonly)\s+(?:-[A-Za-z]+\s+)*-[A-Za-z]*n[A-Za-z]*\b/.test(text) ||
      // Integer declarations and indexed array elements retain their existing gate.
      /\b(?:declare|typeset|local)\s+[^;\n]*-[A-Za-z]*i|\[[^\]]+\]\+?=/.test(text)
    )
  })
  const dynamic = forms.some(rawDynamicAssignment)
  // On shells without [[ syntax, a lone expanded boolean operand can become
  // a command word. Keep that boundary while admitting the string test [[ $f ]].
  const expandedBoolean = [...source.matchAll(/\[\[([\s\S]*?)(?:\]\]|$)/g)].some(
    (match) =>
      /&&|\|\|/.test(match[1]!) &&
      match[1]!.split(/&&|\|\|/).some((piece) => /^\s*[!(]*\s*[^\s]*\$[^\s]*\s*\)*\s*$/.test(piece)),
  )
  return (
    (dynamic && (arithmetic || expandedBoolean)) ||
    // IFS changes can turn expanded expression operands into shell command words.
    (/\bIFS\+?=/.test(source) &&
      /(?:\(\(|\[\[|&&|\|\|)\s*\(*\s*\$(?:[A-Za-z_][A-Za-z0-9_]*|[1-9@*]|\{(?:[A-Za-z_][A-Za-z0-9_]*|[1-9@*])\})\s*\)*\s*(?:\)\)|\]\]|&&|\|\|)/.test(
        source,
      ))
  )
}

/** Recognize guarded executable names in incomplete shell text. */
function mentionsExecutable(text: string): boolean {
  return (
    mentionsGit(text) ||
    /(?:^|[\s;])\$\{?[A-Za-z_]/.test(text) ||
    text.split(/[^\w./\\-]+/).some((word) => SHELLS.has(programIdentity(word)) || OPAQUE.has(programIdentity(word)))
  )
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

/** Validate complete test operands, including nested boolean groups, before hiding shell text. */
function conditionalOperands(body: string): boolean {
  const tokens: { value: string; operator: boolean }[] = []
  for (let offset = 0; offset < body.length;) {
    if (/\s/.test(body[offset]!)) {
      offset += 1
      continue
    }
    const operator = /^(?:&&|\|\||==|!=|=~|[<>=!()])/.exec(body.slice(offset))
    if (operator) {
      tokens.push({ value: operator[0], operator: true })
      offset += operator[0].length
    } else {
      const token = shellWord(body, offset)
      if (token.end === offset || token.unreadable || token.word.unsafeExpression) return false
      tokens.push({ value: token.word.value, operator: false })
      offset = token.end
    }
  }
  let index = 0
  const piece = (): boolean => {
    if (tokens[index]?.value === "!" && tokens[index]?.operator) index += 1
    if (tokens[index]?.value === "(" && tokens[index]?.operator) {
      index += 1
      if (!expression() || tokens[index]?.value !== ")" || !tokens[index]?.operator) return false
      index += 1
      return true
    }
    const words: string[] = []
    while (index < tokens.length && !(tokens[index]!.operator && ["&&", "||", ")"].includes(tokens[index]!.value)))
      words.push(tokens[index++]!.value)
    if (words.length === 1) {
      const word = words[0]!
      if (SHELLS.has(programIdentity(word)) || OPAQUE.has(programIdentity(word)) || mentionsGit(word)) return false
      return !/^-(?:[abcdefghkprstuwxznovGLNOSR])$/.test(word)
    }
    if (words.length === 2 && /^-(?:[abcdefghkprstuwxznovGLNOSR])$/.test(words[0]!)) {
      return true
    }
    if (words.length !== 3 || !/^(?:==|=|!=|=~|<|>|-eq|-ne|-lt|-le|-gt|-ge|-nt|-ot|-ef)$/.test(words[1]!)) return false
    return true
  }
  const expression = (): boolean => {
    if (!piece()) return false
    while (tokens[index]?.operator && ["&&", "||"].includes(tokens[index]!.value)) {
      index += 1
      if (!piece()) return false
    }
    return true
  }
  return expression() && index === tokens.length
}

/** Admit only inert expression tokens; any ambiguity consumes the rest and fails closed. */
function inertExpression(
  text: string,
  start: number,
  kind: "arithmetic" | "for" | "conditional" | "legacy",
): {
  end: number
  unreadable: boolean
  substitutions: (string | undefined)[]
} {
  const conditional = kind === "conditional"
  const substitutions: (string | undefined)[] = []
  const begin = start + (kind === "legacy" ? 1 : 2)
  let nesting = 0
  let separators = 0
  let quote = ""
  let operand = false
  for (let index = begin; index < text.length; index += 1) {
    const char = text[index]!
    const rest = text.slice(index)
    if (!conditional && rest.startsWith("$(") && !rest.startsWith("$((")) {
      if (operand) break
      const substitution = shellSubstitution(text, index)
      if (substitution.body === undefined) break
      substitutions.push(substitution.body)
      operand = true
      index = substitution.end - 1
      continue
    }
    if (!conditional && char === "$") {
      const parameter =
        /^\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9#?@$*]|\{(?:#?[A-Za-z_][A-Za-z0-9_]*(?:\[(?:@|\*|[A-Za-z_][A-Za-z0-9_]*|[0-9]+)\])?|[0-9]+|[#?@$*])\})/.exec(
          rest,
        )
      if (!parameter || operand) break
      operand = true
      index += parameter[0].length - 1
      continue
    }
    if (!conditional && !operand) {
      const literal = /^[0-9]+#[0-9A-Za-z@_]+/.exec(rest)
      if (literal) {
        operand = true
        index += literal[0].length - 1
        continue
      }
    }
    if (/[\r\n`#]/.test(char) || /^(?:\$|<|>)\(/.test(rest)) break
    if (quote) {
      if (char === quote) quote = ""
      else if (char === "\\" && quote === '"') {
        if (!text[index + 1] || /[\r\n`#]/.test(text[index + 1]!)) break
        index += 1
      }
      continue
    }
    if (conditional && /['"]/.test(char)) {
      quote = char
      continue
    }
    if (conditional && rest.startsWith("]]")) {
      if (nesting || !/[ \t)]/.test(text[index - 1] ?? "") || !/^(?:[\s;&|<>()]|$)/.test(rest.slice(2))) break
      const body = text.slice(begin, index)
      if (!conditionalOperands(body)) break
      return { end: index + 2, unreadable: false, substitutions }
    }
    if (!conditional && kind === "legacy" && char === "]" && !nesting)
      return { end: index + 1, unreadable: false, substitutions }
    if (!conditional && /[A-Za-z0-9_]/.test(char)) {
      const token = /^[A-Za-z0-9_]+(?:\[(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+)\])?/.exec(rest)![0]
      if (operand) break
      operand = true
      index += token.length - 1
      continue
    }
    if (char === "(") {
      if (!conditional && operand) break
      if (++nesting > MAX_SHELL_DEPTH) break
      continue
    }
    if (char === ")") {
      if (nesting) nesting -= 1
      else {
        // Bash may parse (( as nested subshells unless this FIRST outer close is adjacent to ).
        if (conditional || text[index + 1] !== ")" || (kind === "for" && separators !== 2)) break
        const body = text.slice(begin, index)
        if (
          body.split(/&&|\|\|/).some((piece) => {
            const name = piece
              .trim()
              .replace(/^\(+|\)+$/g, "")
              .trim()
            return SHELLS.has(programIdentity(name)) || OPAQUE.has(programIdentity(name)) || mentionsGit(name)
          })
        )
          break
        return { end: index + 2, unreadable: false, substitutions }
      }
      continue
    }
    if (char === ";") {
      if (kind !== "for" || nesting || ++separators > 2) break
      operand = false
      continue
    }
    if (char === "&" || char === "|") {
      if (text[index + 1] !== char) break
      operand = false
      index += 1
      continue
    }
    if (char === "$") {
      // Positional parameters and $# are needed by ordinary shell argument checks.
      const parameter = /^\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9#]|\{(?:[A-Za-z_][A-Za-z0-9_]*|[0-9]+|#)\})/.exec(rest)
      if (!parameter) break
      index += parameter[0].length - 1
      continue
    }
    if (!conditional && /[+\-*/%<>=!~^?:,]/.test(char)) {
      if ((char === "+" || char === "-") && text[index + 1] === char) index += 1
      else operand = false
      continue
    }
    if (/[A-Za-z0-9_ \t+\-*/%<>=!~^?:,]/.test(char) || (conditional && /[.@]/.test(char))) continue
    break
  }
  return { end: text.length, unreadable: true, substitutions }
}

/** Read one word, retaining only the provenance needed to reject expansions. */
function shellWord(text: string, start: number): { word: ShellWord; end: number; unreadable: boolean } {
  let value = ""
  let unquoted = ""
  let quote = ""
  let quoted = false
  let dynamic = false
  let syntax = false
  let ansiQuoted = false
  let unsafeExpression = false
  const substitutions: (string | undefined)[] = []
  let index = start
  for (; index < text.length; index += 1) {
    const char = text[index]!
    const process = !quote && /^[<>]\(/.test(text.slice(index))
    // Attached unquoted groups include zsh qualifiers and bash extglobs. Their
    // contents are executable in some shells regardless of the first flag.
    if (
      !quote &&
      char === "(" &&
      /[^\s;&|<>()=]$/.test(value) &&
      text.indexOf(")", index + 1) >= 0 &&
      !/^\(\s*\)\s*[{(]/.test(text.slice(index))
    )
      unsafeExpression = true
    if (!quote && !process && /[\s;&|<>()]/.test(char)) break
    if (char === "\\" && quote !== "'") {
      const next = text[index + 1]
      if (next === undefined)
        return {
          word: { value, raw: text.slice(start, index), dynamic, syntax, ansiQuoted, quoted, substitutions },
          end: index + 1,
          unreadable: true,
        }
      // Double quotes preserve backslashes except before shell-special characters.
      if (quote === '"' && !/[\$`"\\\n]/.test(next)) value += "\\"
      if (quote === "$'") value += "\\"
      if (next !== "\n") value += next
      quoted ||= next !== "\n"
      index += 1
    } else if (quote === "$'" && char === "'") quote = ""
    else if (char === quote) quote = ""
    else if (!quote && text.slice(index, index + 2) === "$'") {
      value += "$"
      quote = "$'"
      ansiQuoted = quoted = true
      index += 1
    } else if (!quote && ["'", '"'].includes(char)) {
      quote = char
      quoted = true
    } else if (
      quote !== "'" &&
      quote !== "$'" &&
      (text.slice(index, index + 3) === "$((" || text.slice(index, index + 2) === "$[")
    ) {
      const expression = inertExpression(text, index + 1, text[index + 1] === "[" ? "legacy" : "arithmetic")
      substitutions.push(...expression.substitutions)
      if (expression.substitutions.length) dynamic = syntax = true
      if (expression.unreadable) {
        unsafeExpression = dynamic = syntax = true
      }
      value += text.slice(index, expression.end)
      index = expression.end - 1
    } else if (quote !== "'" && quote !== "$'" && (process || char === "`" || text.slice(index, index + 2) === "$(")) {
      const substitution = shellSubstitution(text, index)
      substitutions.push(substitution.body)
      value += text.slice(index, substitution.end)
      index = substitution.end - 1
      dynamic = syntax = true
    } else {
      value += char
      if (!quote) unquoted += char
      if (quote !== "'" && quote !== "$'" && ((char === "$" && text[index + 1] !== "'") || char === "`")) dynamic = true
      if (quote !== "'" && quote !== "$'" && (char === "`" || (char === "$" && /[({'"]/.test(text[index + 1] ?? ""))))
        syntax = true
    }
  }
  dynamic ||= /\{[^}]*[,]|\{[^}]*\.\./.test(unquoted)
  syntax ||= !quoted && /^[{}]$/.test(value)
  return {
    word: {
      value,
      raw: text.slice(start, index),
      dynamic,
      syntax,
      ansiQuoted,
      quoted,
      substitutions,
      unsafeExpression,
      glob: programIdentity(value) !== "[" && /[*?[]/.test(unquoted),
    },
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
  fileInputs: Set<ShellWord[]>
  outputRedirects: Set<ShellWord[]>
  inputBodies: Map<ShellWord[], string[]>
  expandingBodies: string[]
  substitutions: Map<ShellWord[], (string | undefined)[]>
  functions: Map<string, ShellWord[][]>
  gitEnvironment: boolean
  comments: string[]
} {
  const text = withoutFences(source.slice(0, MAX_SHELL_CHARS))
  const commands: ShellWord[][] = []
  const pipeInputs = new Set<ShellWord[]>()
  const fileInputs = new Set<ShellWord[]>()
  const outputRedirects = new Set<ShellWord[]>()
  const inputBodies = new Map<ShellWord[], string[]>()
  const expandingBodies: string[] = []
  const pipeSources = new Map<ShellWord[], ShellWord[][]>()
  const groups: {
    fed: boolean
    start: number
    close: string
    name?: string
    pattern?: boolean
    redirected?: boolean
    execRedirected?: boolean
    calls?: Set<string>
  }[] = []
  let outputRedirected = false
  const redirectingFunctions = new Set<string>()
  const functionCalls = new Map<string, Set<string>>()
  const retainedRedirects = new Set<ShellWord[]>()
  let redirectedGroup: number | undefined
  const substitutions = new Map<ShellWord[], (string | undefined)[]>()
  const functions = new Map<string, ShellWord[][]>()
  const comments: string[] = []
  const heredocs: {
    delimiter: string
    quoted: boolean
    tabs: boolean
    command: ShellWord[]
    consumers: ShellWord[][]
  }[] = []
  let functionName: string | undefined
  let signature = false
  let words: ShellWord[] = []
  let uncertain = source.length > MAX_SHELL_CHARS
  let unparsedGit = false
  let redirectSeen = false
  let visible = ""
  let index = 0
  // Function calls share output state with their caller; only subshells isolate it.
  const retainOutputRedirect = () => {
    if (retainedRedirects.has(words)) return
    const executable = words.filter((word) => !word.scope)
    const normalized = executableWords(executable)
    const scope = [...groups].reverse().find((group) => group.name || group.close === ")")
    const name = normalized.source?.value ?? normalized.words[0]?.value
    if (scope?.name && name) (scope.calls ??= new Set()).add(name)
    // Resolve calls when used, including functions defined after their caller's body.
    const pending = name ? [name] : []
    const visited = new Set<string>()
    let redirects = false
    while (pending.length) {
      const call = pending.pop()!
      if (visited.has(call)) continue
      visited.add(call)
      redirects ||= redirectingFunctions.has(call)
      pending.push(...(functionCalls.get(call) ?? []))
    }
    const bareExec =
      executable.some((word) => programIdentity(word.value) === "exec") &&
      outputRedirects.has(words) &&
      !normalized.words.length
    if (bareExec || redirects) {
      retainedRedirects.add(words)
      if (scope) {
        scope.redirected = true
        scope.execRedirected ||= bareExec
      } else outputRedirected = true
    }
  }
  while (index < text.length) {
    if (outputRedirected || groups.some((group) => group.redirected)) outputRedirects.add(words)
    const rest = text.slice(index)
    if (rest[0] === "#") {
      const end = text.indexOf("\n", index)
      comments.push(text.slice(index + 1, end < 0 ? text.length : end))
      index = end
      if (index < 0) break
      continue
    }
    const redirect = /^[<>]\(/.test(rest) ? undefined : /^(?:\d*(?:>>|>&|>\||>|<<-|<<<|<<|<)|&>>?)/.exec(rest)?.[0]
    if (redirect) {
      redirectSeen = true
      const operator = redirect.replace(/^\d+/, "")
      index += redirect.length
      while (/[ \t]/.test(text[index] ?? "") && index < text.length) index += 1
      const target = shellWord(text, index)
      unparsedGit ||= target.word.unsafeExpression === true
      uncertain ||= target.word.unsafeExpression === true
      const stdout = !/^\d/.test(redirect) || /^1\D/.test(redirect)
      const persistent =
        [">", ">>", ">|", ">&", "&>", "&>>"].includes(operator) &&
        stdout &&
        !["/dev/null", "/dev/stderr", "/dev/stdout", "/dev/tty"].includes(target.word.value) &&
        !(operator === ">&" && /^(?:[012]|-)$/.test(target.word.value))
      if (persistent) {
        outputRedirects.add(words)
        if (redirectedGroup !== undefined)
          commands.slice(redirectedGroup).forEach((command) => outputRedirects.add(command))
      }
      if (["<", "<<", "<<-", "<<<"].includes(operator)) {
        const inputs = operator === "<" ? fileInputs : pipeInputs
        inputs.add(words)
        if (redirectedGroup !== undefined) commands.slice(redirectedGroup).forEach((command) => inputs.add(command))
      }
      const consumers = redirectedGroup === undefined ? [words] : [...commands.slice(redirectedGroup), words]
      if (operator === "<<<")
        consumers.forEach((command) =>
          inputBodies.set(command, [...(inputBodies.get(command) ?? []), target.word.value]),
        )
      substitutions.set(words, [...(substitutions.get(words) ?? []), ...(target.word.substitutions ?? [])])
      index = target.end
      const literal = target.word.value !== "" && !target.word.dynamic && !target.word.glob && !target.word.syntax
      if (["<<", "<<-"].includes(operator) && literal) {
        heredocs.push({
          delimiter: target.word.value,
          quoted: target.word.quoted === true,
          tabs: operator === "<<-",
          command: words,
          consumers,
        })
        uncertain ||= !target.word.quoted
      } else {
        const supported =
          [">", ">>", ">|", "&>", "&>>"].includes(operator) ||
          (operator === ">&" && /^(?:\d+|-)$/.test(target.word.value)) ||
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
        retainOutputRedirect()
        commands.push(words)
        const producer = words
        words = []
        if (["|", "|&"].includes(separator))
          pipeSources.set(words, redirectedGroup === undefined ? [producer] : [...commands.slice(redirectedGroup)])
        else if (groups.some((group) => group.fed)) pipeSources.set(words, [producer])
        if (["|", "|&"].includes(separator) || groups.some((group) => group.fed)) pipeInputs.add(words)
      }
      redirectedGroup = undefined
      redirectSeen = false
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
            body += (doc.tabs ? line.replace(/^\t+/, "") : line) + "\n"
            // Escaped execution text stays conservative without making plain prose relevant.
            if (!doc.quoted && /\\[$`]/.test(line)) unparsedGit ||= mentionsGit(line)
          }
          doc.consumers.forEach((command) => inputBodies.set(command, [...(inputBodies.get(command) ?? []), body]))
          if (!doc.quoted) {
            expandingBodies.push(body)
            substitutions.set(doc.command, [...(substitutions.get(doc.command) ?? []), ...heredocSubstitutions(body)])
          }
          uncertain ||= !closed
        }
      }
      continue
    }
    if (/\s/.test(text[index]!)) {
      visible += text[index++]
      continue
    }
    const head = words.every((word) => word.scope || (word.syntax && /^[{(]$/.test(word.value)) || word.value === "!")
    const expressionHead =
      !redirectSeen &&
      words.every(
        (word) =>
          !word.quoted && !word.dynamic && /^(?:!|time|if|elif|while|until|then|else|do|\{|\()$/.test(word.value),
      )
    const forehead =
      !redirectSeen &&
      words.at(-1)?.value === "for" &&
      words.slice(0, -1).every((word) => word.scope && /^(?:\{|\(|then|do)$/.test(word.value))
    const expressionPrefix = words.some(
      (word) =>
        /^[A-Za-z_][A-Za-z0-9_]*=/.test(word.value) || Object.hasOwn(WRAPPER_OPERANDS, programIdentity(word.value)),
    )
    if (
      rest.startsWith("((") ||
      (rest.startsWith("[[") && /^(?:\s|$)/.test(rest.slice(2)) && (expressionHead || redirectSeen || expressionPrefix))
    ) {
      const expression = inertExpression(
        text,
        index,
        rest.startsWith("[[") ? "conditional" : forehead ? "for" : "arithmetic",
      )
      const value = text.slice(index, expression.end)
      const unsafe = expression.unreadable || !(expressionHead || (forehead && rest.startsWith("((")))
      uncertain ||= unsafe
      uncertain ||= expression.substitutions.length > 0
      unparsedGit ||= unsafe
      if (!unsafe) {
        words.push({
          value,
          dynamic: false,
          scope: true,
          substitutions: expression.substitutions,
        })
        visible += value
        index = expression.end
        continue
      }
      // Rejected text stays uncertain, but ordinary lexing can still retain a stronger guarded verdict.
    }
    const token = /[()]/.test(text[index]!)
      ? { word: { value: text[index]!, dynamic: false, syntax: true } as ShellWord, end: index + 1, unreadable: false }
      : shellWord(text, index)
    if (/^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?\+?=$/.test(token.word.value) && text[token.end] === "(") {
      // Array elements are assignment data, never subshell command words. Retain
      // executable substitutions so this exception cannot conceal an invocation.
      let end = token.end + 1
      while (end < text.length && text[end] !== ")") {
        if (/\s/.test(text[end]!)) {
          end += 1
          continue
        }
        const element = shellWord(text, end)
        token.word.substitutions!.push(...(element.word.substitutions ?? []))
        token.word.unsafeExpression ||= element.word.unsafeExpression
        if (element.end === end || element.unreadable) break
        end = element.end
      }
      token.unreadable ||= text[end] !== ")"
      token.word.value += text.slice(token.end, end + 1)
      token.end = end + 1
    }
    const value = token.word.value
    const literal = !token.word.quoted && !token.word.dynamic
    const prefix = words.map((word) => word.value).join(" ")
    const group = groups.at(-1)
    const functionPrefix =
      (words.length === 1 || (words.length === 2 && words[0]!.value === "function")) &&
      words.every((word) => !word.quoted && !word.dynamic && !word.syntax)
    if (literal && !token.word.syntax && prefix === "function") {
      functionName = value
      words.forEach((word) => (word.scope = true))
      token.word.scope = true
    } else if (value === "(" && functionPrefix && /^\(\s*\)/.test(rest)) {
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
      const name = functionName ?? (functionPrefix && words[0]!.value === "function" ? words.at(-1)!.value : undefined)
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
      retainOutputRedirect()
      groups.pop()
      redirectedGroup = group.start
      if (group.name) {
        functions.set(group.name, [...(functions.get(group.name) ?? []), ...commands.slice(group.start), words])
        functionCalls.set(group.name, group.close === ")" ? new Set() : (group.calls ?? new Set()))
        if (group.execRedirected && group.close !== ")") redirectingFunctions.add(group.name)
        else redirectingFunctions.delete(group.name)
      }
      token.word.scope = token.word.syntax = true
    } else if (literal && head && /^(?:then|else|elif|do)$/.test(value) && group) {
      token.word.scope = token.word.syntax = true
    }
    token.word.syntax ||= token.unreadable
    unparsedGit ||= token.word.unsafeExpression === true
    words.push(token.word)
    uncertain ||= token.unreadable || token.word.syntax === true
    visible += text.slice(index, token.end)
    index = token.end
  }
  commands.push(words)
  // A printer does not make its input inert when a downstream command persists it.
  for (const [consumer, producers] of pipeSources)
    inputBodies.set(consumer, [
      ...producers.flatMap((producer) => inputBodies.get(producer) ?? []),
      ...(inputBodies.get(consumer) ?? []),
    ])
  return {
    commands,
    uncertain: uncertain || heredocs.length > 0,
    text: visible,
    unparsedGit,
    pipeInputs,
    fileInputs,
    outputRedirects,
    inputBodies,
    expandingBodies,
    substitutions,
    functions,
    gitEnvironment: /\bGIT_[A-Za-z0-9_]*\+?=/.test(source),
    comments,
  }
}

const WRAPPER_OPERANDS: Readonly<
  Record<
    string,
    {
      options: string
      elevation?: "admit" | "always-ask"
      outputs?: string
      createsFile?: boolean
    }
  >
> = {
  env: { options: "-u --unset -C --chdir -P" },
  command: { options: "" },
  nohup: { options: "", createsFile: true },
  sudo: {
    options:
      "-u --user -U --other-user -g --group -h --host -p --prompt -C -T -R -D --chdir --chroot -r -t --role --type",
    elevation: "admit",
  },
  timeout: { options: "-s --signal -k --kill-after" },
  exec: { options: "-a" },
  time: { options: "-f -o --format --output", outputs: "-o --output" },
  builtin: { options: "" },
  nice: { options: "-n --adjustment" },
  ionice: { options: "-c --class -n --classdata -p --pid -P --pgid -u --uid" },
  flock: { options: "-w --wait --timeout -E --conflict-exit-code", createsFile: true },
  setsid: { options: "" },
  stdbuf: { options: "-i -o -e --input --output --error" },
  doas: { options: "-u -C -a", elevation: "admit" },
  pkexec: { options: "", elevation: "always-ask" },
  pfexec: { options: "", elevation: "always-ask" },
  dzdo: { options: "", elevation: "always-ask" },
  pbrun: { options: "", elevation: "always-ask" },
  please: { options: "", elevation: "always-ask" },
  pleaseedit: { options: "", elevation: "always-ask" },
  doasedit: { options: "", elevation: "always-ask" },
  sudoedit: { options: "", elevation: "always-ask" },
  machinectl: { options: "", elevation: "always-ask" },
  super: { options: "", elevation: "always-ask" },
  really: { options: "", elevation: "always-ask" },
  runas: { options: "", elevation: "always-ask" },
  ksu: { options: "", elevation: "always-ask" },
  run0: {
    options: "",
    elevation: "always-ask",
  },
  gosu: { options: "", elevation: "always-ask" },
  strace: {
    options: "-o --output -e --trace -p --attach -P --trace-path -s --string-limit -u --user -a -b",
    outputs: "-o --output",
  },
  xargs: {
    options:
      "-a --arg-file -d --delimiter -E -I -J -L -n -P -s --eof --replace --max-lines --max-args --max-procs --max-chars --process-slot-var",
  },
  su: { options: "-s --shell -g --group -G --supp-group -w --whitelist-environment", elevation: "always-ask" },
  runuser: { options: "-u --user -s --shell -g --group -G --supp-group -w --whitelist-environment" },
  sg: { options: "" },
  script: { options: "-O --log-out -I --log-in -B --log-io -T --log-timing -m --logging-format", createsFile: true },
  nsenter: { options: "-t --target -S --setuid -G --setgid" },
  chroot: { options: "--userspec --groups" },
  caffeinate: { options: "-t -w" },
  "systemd-run": {
    options:
      "-u --unit -p --property --description --slice -E --setenv --uid --gid --nice --working-directory -M --machine -H --host",
    elevation: "always-ask",
  },
  chrt: { options: "-T -P -D --sched-runtime --sched-period --sched-deadline" },
  taskset: { options: "" },
  unbuffer: { options: "" },
  busybox: { options: "" },
  toybox: { options: "" },
  coproc: { options: "" },
  noglob: { options: "" },
  nocorrect: { options: "" },
  repeat: { options: "" },
  "-": { options: "" },
}
const WRAPPER_FLAGS: Readonly<Record<string, string>> = {
  env: "- -i -0 --ignore-environment --null",
  command: "-p",
  nohup: "",
  builtin: "",
  sudo: "-n -E -H -S -A -b -k -K -v -V -l --list -s -i --shell --login --preserve-env --non-interactive",
  timeout: "-v --foreground --preserve-status --verbose",
  exec: "-l",
  time: "-l -h -p -v -a --append -q --quiet --verbose --portability",
  nice: "",
  ionice: "-t",
  flock: "-e -x -s -u -n -o -F --close --exclusive --shared --unlock --no-fork",
  setsid: "-f -w --wait --fork",
  stdbuf: "",
  doas: "-n -S -l -s",
  pkexec: "--disable-internal-agent",
  pfexec: "",
  dzdo: "-n -E -H -S -A -b -k -K -v -V -l",
  pbrun: "-b --background --disable_optimized_mode -l --local_mode -n --null_input -p --pipe_mode -X",
  please: "-n --noprompt",
  run0: "--no-ask-password --quiet --pty --pipe",
  gosu: "",
  strace: "-f -q -t",
  xargs: "-0 -p -r -t -x --no-run-if-empty --null --verbose",
  su: "- -l --login -m -p --preserve-environment -P --pty",
  runuser: "- -l --login -m -p --preserve-environment -P --pty",
  sg: "-",
  script: "-a --append -e --return -f --flush -q --quiet",
  nsenter:
    "-a --all -U --user -m --mount -u --uts -i --ipc -n --net -p --pid -C --cgroup -T --time -F --no-fork --preserve-credentials",
  chroot: "--skip-chdir",
  caffeinate: "-d -i -m -s -u",
  "systemd-run":
    "--user --system --scope -t --pty -P --pipe -q --quiet --wait -G --collect --no-block -r --remain-after-exit -d --same-dir",
  chrt: "-b -d -f -i -o -r -a -R -v -m --batch --deadline --fifo --idle --other --rr --all-tasks --reset-on-fork --verbose",
  taskset: "-a -c --all-tasks --cpu-list",
  unbuffer: "-p",
}

/** Resolve the same executable identity for every catalogue, retaining exact safe-form eligibility. */
function programIdentity(value: string): string
function programIdentity<T extends { value: string }>(
  input: readonly T[],
): {
  words: T[]
  safeForm: boolean
  module: boolean
  script: boolean
  fileInput: boolean
}
function programIdentity<T extends { value: string }>(
  input: string | readonly T[],
): string | { words: T[]; safeForm: boolean; module: boolean; script: boolean; fileInput: boolean } {
  if (typeof input === "string") {
    const basename = input.replace(/^=/, "").replaceAll("\\", "/").split("/").at(-1) ?? ""
    let name = basename.replace(/\.(?:exe|cmd|bat|ps1)$/i, "").toLowerCase()
    const entryScript = name.replace(/\.(?:[cm]?js|py)$/, "")
    if (
      ["npm", "npx", "pnpm", "yarn", "tsc", "eslint", "prettier", "vite", "vitest", "webpack", "rollup", "vp"].includes(
        entryScript,
      )
    )
      name = entryScript
    const unversioned = name.replace(/-?\d+(?:\.\d+)*t?$/, "")
    if (
      PROJECT_PROGRAMS.has(unversioned) ||
      ASK_PROGRAMS.has(unversioned) ||
      Object.hasOwn(EXEC_OPTION_SPECS, unversioned) ||
      Object.hasOwn(WRAPPER_OPERANDS, unversioned) ||
      ["node", "py", "pypy", "python", "perl", "ruby", "lua", "rscript", "julia", "ipython", "py.test"].includes(
        unversioned,
      )
    )
      name = unversioned
    if (
      /^python(?:\d+(?:\.\d+)*[dmut]*)?w?(?:-dbg|-debug)?$/.test(name) ||
      ["py", "pypy", "graalpy", "pyston"].includes(name)
    )
      return "python"
    if (name.startsWith("cargo-")) return "cargo"
    if (name.startsWith("nix-")) return "nix"
    const aliases: Readonly<Record<string, string>> = {
      "sudo-rs": "sudo",
      gsudo: "sudo",
      "py.test": "pytest",
      "npm-cli.js": "npm",
      "npx-cli.js": "npx",
      "pnpm.cjs": "pnpm",
      "yarn.js": "yarn",
      "yarn.cjs": "yarn",
      "tsc.js": "tsc",
      dmypy: "mypy",
      stubgen: "mypy",
      stubtest: "mypy",
      "ts-node-esm": "ts-node",
      "ts-node-cwd": "ts-node",
      "ts-node-transpile-only": "ts-node",
      gawk: "awk",
      mawk: "awk",
      nawk: "awk",
      nodejs: "node",
      yarnpkg: "yarn",
      pnpx: "npx",
      gmake: "make",
      gnumake: "make",
      bmake: "make",
      "mingw32-make": "make",
      nmake: "make",
      "docker-compose": "docker",
      "docker-buildx": "docker",
      "podman-compose": "podman",
      "podman-remote": "podman",
      "nerdctl.lima": "nerdctl",
      finch: "nerdctl",
      lima: "nerdctl",
      bundler: "bundle",
      rtx: "mise",
    }
    if (Object.hasOwn(aliases, unversioned)) name = unversioned
    if (Object.hasOwn(aliases, name)) return aliases[name]!
    // GNU installs prefix catalogued wrappers and coreutils with g on hosts with BSD tools.
    const gnu = name.startsWith("g") ? name.slice(1) : ""
    if (
      gnu &&
      (Object.hasOwn(WRAPPER_OPERANDS, gnu) ||
        Object.hasOwn(READ_OPTIONS, gnu) ||
        STDIO_PROGRAMS.has(gnu) ||
        ["rm", "cp", "mv", "ln", "install", "mkdir", "rmdir", "touch", "chmod", "chown", "chgrp", "truncate"].includes(
          gnu,
        ))
    )
      return gnu
    return name
  }
  const first = input[0]
  if (!first) return { words: [], safeForm: false, module: false, script: false, fileInput: false }
  let program = programIdentity(first.value)
  const basename = first.value.replaceAll("\\", "/").split("/").at(-1)!.toLowerCase()
  let safeForm =
    !/\.(?:cmd|bat|ps1)$/i.test(basename) && (basename.replace(/\.exe$/, "") === program || program === "pytest")
  let module = false
  let words = input.slice()
  if (program === "python" && input[1]?.value === "-m" && input[2]) {
    module = true
    program = programIdentity(input[2].value)
    // Only the existing pytest form is admitted through Python; all other modules ask.
    safeForm = !/\.(?:cmd|bat|ps1)$/i.test(basename) && input[2].value === "pytest"
    words = input.slice(2)
  } else if (
    ["python", "node"].includes(program) &&
    input[1] &&
    !input[1].value.startsWith("-") &&
    PROJECT_PROGRAMS.has(programIdentity(input[1].value))
  ) {
    program = programIdentity(input[1].value)
    words = input.slice(1)
    safeForm = !/\.(?:cmd|bat|ps1)$/i.test(input[1].value)
  } else if (
    (["colima", "lima"].includes(program) && programIdentity(input[1]?.value ?? "") === "nerdctl") ||
    (program === "limactl" &&
      input[1]?.value === "shell" &&
      input[2] &&
      programIdentity(input[3]?.value ?? "") === "nerdctl")
  ) {
    words = input.slice(program === "limactl" ? 3 : 1)
    program = "nerdctl"
  } else if (
    program === "corepack" &&
    input[1] &&
    ["npm", "npx", "pnpm", "yarn"].includes(programIdentity(input[1].value))
  ) {
    program = programIdentity(input[1].value)
    words = input.slice(1)
    safeForm = false
  } else if (program === "node") {
    const run = input.findIndex((word, at) => at > 0 && (word.value === "--run" || word.value.startsWith("--run=")))
    if (run > 0) {
      const option = input[run]!
      program = "npm"
      words = [
        first,
        { ...option, value: "run" },
        ...(option.value.includes("=") ? [{ ...option, value: option.value.slice(6) }] : []),
        ...input.slice(run + 1),
      ]
      safeForm = false
    }
  }
  words[0] = { ...words[0]!, value: program }
  return {
    words,
    safeForm,
    module,
    script: /\.(?:cmd|bat|ps1)$/i.test(basename),
    fileInput: ["python", "node"].includes(program),
  }
}

/** Recognize command-string options and long prefixes without decoding their payload. */
function splitStringOption(option: string, longOptions = ["--split-string"], short = "S"): boolean {
  return (
    (option.startsWith("-") && !option.startsWith("--") && option.slice(1).includes(short)) ||
    (option.startsWith("--") &&
      option.split("=", 1)[0]!.length > 2 &&
      longOptions.some((name) => name.startsWith(option.split("=", 1)[0]!)))
  )
}

/** Detect guarded names after ANSI-C decoding, including quote-fragmented executable names. */
function encodedPayloadRelevant(word: ShellWord): boolean {
  const value = word.value
  const decoded = value
    .replace(/\$/g, "")
    .replace(/\\(?:x[\da-fA-F]{1,2}|u[\da-fA-F]{1,4}|U[\da-fA-F]{1,8}|[0-7]{1,3}|c[\s\S]|[\s\S])/g, (escape) => {
      const code = escape.slice(1)
      if (/^[xuU]/.test(code) && code.length > 1) {
        const point = Number.parseInt(code.slice(1), 16)
        return point <= 0x10ffff ? String.fromCodePoint(point) : escape
      }
      if (/^[0-7]/.test(code)) return String.fromCharCode(Number.parseInt(code, 8) & 0xff)
      if (code.startsWith("c") && code.length === 2) return String.fromCharCode(code.charCodeAt(1) & 31)
      if (code === "_" && !word.ansiQuoted) return " "
      return (
        (
          { a: "\x07", b: "\b", e: "\x1b", E: "\x1b", f: "\f", n: "\n", r: "\r", t: "\t", v: "\v" } as Record<
            string,
            string
          >
        )[code] ?? code
      )
    })
  return (
    mentionsExecutable(decoded) ||
    // A split string can expand an environment variable into its executable name.
    (!word.ansiQuoted && /\$\{?[A-Za-z_]/.test(value))
  )
}

/** Remove catalogued wrapper operands without losing guarded candidates on uncertainty. */
function executableWords(input: ShellWord[]): {
  words: ShellWord[]
  uncertain: boolean
  changesDirectory: boolean
  opaque: boolean
  unparsedWrapper?: boolean
  shellBodies?: ShellWord[]
  inputOperands?: boolean
  source?: ShellWord
} {
  let index = 0
  let uncertain = false
  let changesDirectory = false
  let opaque = false
  let inputOperands = false
  while (index < input.length) {
    while (/^[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]+\])?\+?=/.test(input[index]?.value ?? "")) {
      uncertain ||= !safeAssignment(grammarArgument(input[index]!))
      index += 1
    }
    const wrapper = programIdentity(input[index]?.value ?? "")
    if (wrapper === "command" && ["-v", "-V"].includes(input[index + 1]?.value ?? ""))
      return { words: [], uncertain, changesDirectory, opaque }
    if (!Object.hasOwn(WRAPPER_OPERANDS, wrapper)) break
    const wrapperStart = index
    inputOperands ||= wrapper === "xargs"
    changesDirectory ||= wrapper === "chroot"
    if (input[index]?.dynamic || input[index]?.glob) uncertain = true
    index += 1
    if (wrapper === "sg") index += 1
    let positional = false
    let userMode = false
    let implicitShell: ShellWord | undefined
    while (index < input.length) {
      if (!input[index]!.value.startsWith("-")) {
        // env and sudo read every operand holding `=` before the program as an environment entry, even
        // when its name is not a shell identifier (`A.B=1`); it is never the program.
        if ((wrapper === "env" || wrapper === "sudo") && input[index]!.value.includes("=")) {
          uncertain ||= !safeAssignment(grammarArgument(input[index]!))
          index += 1
          continue
        }
        if (
          !positional &&
          (["flock", "su", "gosu", "script"].includes(wrapper) || (wrapper === "runuser" && !userMode))
        ) {
          index += 1
          positional = true
          continue
        }
        break
      }
      const word = input[index++]!
      const option = word.value
      if (option === "--") break
      const commandCarrier = ["flock", "su", "runuser", "sg", "script"].includes(wrapper)
      const operands = WRAPPER_OPERANDS[wrapper]!.options.split(" ").filter(Boolean)
      const valueFlags = [...operands, ...(commandCarrier ? ["-c"] : []), ...(wrapper === "env" ? ["-S"] : [])]
      const letters = option.startsWith("--") ? [] : [...option.slice(1)]
      const valueAt = letters.findIndex((letter) => valueFlags.includes("-" + letter))
      const shortOption = valueAt < 0 ? undefined : "-" + letters[valueAt]
      const shortFlags = letters.slice(0, valueAt < 0 ? undefined : valueAt)
      const flags = WRAPPER_FLAGS[wrapper]?.split(" ") ?? []
      const commandOption =
        commandCarrier &&
        (shortOption === "-c" ||
          (option.startsWith("--") && splitStringOption(option, ["--command", "--session-command"], "c")))
      const splitOption =
        wrapper === "env" && (shortOption === "-S" || (option.startsWith("--") && splitStringOption(option)))
      const operand = operands.includes(option) || (shortOption !== undefined && valueAt === letters.length - 1)
      const attached = shortOption !== undefined || operands.some((flag) => option.startsWith(flag + "="))
      const numericNice = wrapper === "nice" && /^-\d+$/.test(option)
      const preserveList =
        wrapper === "sudo" && /^--preserve-env=[A-Za-z_][A-Za-z0-9_]*(?:,[A-Za-z_][A-Za-z0-9_]*)*$/.test(option)
      const knownShortFlags = letters.length > 0 && shortFlags.every((flag) => flags.includes("-" + flag))
      const identity = shortOption ?? option.split("=", 1)[0]!
      userMode ||= wrapper === "runuser" && ["-u", "--user"].includes(identity)
      changesDirectory ||=
        (wrapper === "env" && ["-C", "--chdir"].includes(identity)) ||
        (wrapper === "sudo" && ["-D", "-R", "--chdir", "--chroot"].includes(identity))
      // Unknown options can consume operands or introduce executable text; never guess the program after them.
      if (
        (!commandOption &&
          !splitOption &&
          !operand &&
          !attached &&
          !flags.includes(option) &&
          !preserveList &&
          !knownShortFlags &&
          !numericNice) ||
        (!numericNice && shortFlags.some((flag) => !flags.includes("-" + flag)))
      )
        return {
          words: programIdentity(input.slice(wrapperStart)).words,
          uncertain: true,
          changesDirectory,
          opaque,
          unparsedWrapper: true,
          source: input[wrapperStart],
        }
      if (
        (wrapper === "sudo" &&
          (/^--(?:shell|login)$/.test(option) || shortFlags.some((flag) => "si".includes(flag)))) ||
        (wrapper === "doas" && shortFlags.includes("s"))
      )
        implicitShell = { ...word, value: "sh", dynamic: false }
      if (commandOption) {
        const attached =
          shortOption === "-c"
            ? option.slice(valueAt + 2) || undefined
            : option.includes("=")
              ? option.slice(option.indexOf("=") + 1)
              : undefined
        const body = attached === undefined ? input[index] : { ...word, value: attached }
        return {
          words: programIdentity(input.slice(wrapperStart)).words,
          uncertain: true,
          changesDirectory,
          opaque,
          shellBodies: body ? [body] : [],
        }
      }
      if (splitOption) {
        const attached = option.startsWith("--")
          ? option.split("=").slice(1).join("=")
          : option.slice(option.indexOf("S") + 1)
        const body = attached ? { ...word, value: attached } : input[index]
        return {
          words: programIdentity(input.slice(wrapperStart)).words,
          uncertain: true,
          changesDirectory,
          opaque: true,
          shellBodies: body ? [body] : [],
        }
      }
      uncertain ||= word.dynamic
      if (operand) {
        uncertain ||= !input[index] || input[index]!.dynamic
        index += 1
      }
    }
    if (implicitShell && index === input.length) return { words: [implicitShell], uncertain, changesDirectory, opaque }
    if (["timeout", "repeat", "chroot", "chrt", "taskset"].includes(wrapper) || (wrapper === "flock" && !positional))
      index += 1
    if (["chroot", "nsenter"].includes(wrapper) && index >= input.length)
      return { words: [{ ...input[wrapperStart]!, value: "sh" }], uncertain, changesDirectory, opaque }
    // su and script without a command option retain their existing opaque classification.
    if (["su", "sg", "script"].includes(wrapper) || (wrapper === "runuser" && !userMode)) {
      return {
        words: programIdentity(input.slice(wrapperStart)).words,
        uncertain,
        changesDirectory,
        opaque,
        source: input[wrapperStart],
      }
    }
  }
  const words = input.slice(index)
  uncertain ||= words[0]?.value.startsWith("=") === true
  return {
    words: programIdentity(words).words,
    uncertain,
    changesDirectory,
    opaque,
    inputOperands,
    source: input[index],
  }
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

/**
 * Builtin and reserved-word names of bash, dash and zsh. Generated on macOS from bash 3.2
 * `compgen -b` and `compgen -k`, the builtin and reserved-word tables of `zsh -f` 5.9 and the
 * dash(1) builtin list, plus the bash 4/5 additions `mapfile`, `readarray`, `compopt` and
 * `coproc`. Builtins of on-request zsh modules such as zsh/files (`mkdir`, `rm`) are excluded:
 * they are absent unless loaded and then mirror the external programs of the same name.
 */
const SHELL_BUILTINS = new Set(
  (
    "! - . : [ [[ ]] alias autoload bg bind bindkey break builtin bye caller case cd chdir command " +
    "compadd comparguments compcall compctl compdescribe compfiles compgen compgroups complete compopt " +
    "compquote compset comptags comptry compvalues continue coproc declare dirs disable disown do done " +
    "echo echotc echoti elif else emulate enable end esac eval exec exit export false fc fg fi float for " +
    "foreach function functions getln getopts hash help history if in integer jobs kill let limit local " +
    "log logout mapfile nocorrect noglob popd print printf private pushd pushln pwd r read readarray " +
    "readonly rehash repeat return sched select set setopt shift shopt source suspend test then time " +
    "times trap true ttyctl type typeset ulimit umask unalias unfunction unhash unlimit unset unsetopt " +
    "until vared wait whence where which while zcompile zformat zle zmodload zparseopts zregexparse " +
    "zstyle { }"
  ).split(" "),
)
/** Builtins whose arguments the grammar models as inert. */
const ALLOWED_BUILTINS = new Set(["cd", "echo", "printf", "pwd", "true", "false", ":", "test", "[", "exit"])
/** Environment names a command prefix may set, always to a literal value. */
const ASSIGN_ALLOW = new Set(
  (
    "CI NODE_ENV DEBUG FORCE_COLOR NO_COLOR TZ LANG LC_ALL PYTHONUNBUFFERED PYTHONDONTWRITEBYTECODE " +
    "RUST_BACKTRACE VERBOSE"
  ).split(" "),
)
/** test operators that compare strings or files; arithmetic and variable-name operators are absent. */
const TEST_OPERATORS = new Set(
  "-a -b -c -d -e -f -g -h -k -L -n -o -p -r -s -S -t -u -w -x -z -G -N -O -nt -ot -ef".split(" "),
)
/** find options whose next word is a program to run. */
const FIND_EXEC_OPTIONS = new Set(["-exec", "-execdir", "-ok", "-okdir"])
/** find predicates whose next word is their value, never a predicate. */
const FIND_VALUE_PREDICATES = new Set(
  (
    "-name -iname -path -ipath -wholename -iwholename -regex -iregex -lname -ilname -type -xtype -size -mtime " +
    "-mmin -atime -amin -ctime -cmin -newer -anewer -cnewer -used -user -uid -group -gid -perm -maxdepth " +
    "-mindepth -links -inum -samefile -fstype -context -printf -fprint -fprint0 -fls -regextype -files0-from " +
    [..."aBcm"].flatMap((x) => [..."aBcmt"].map((y) => `-newer${x}${y}`)).join(" ")
  ).split(" "),
)
/** A plain decimal, floating-point or hexadecimal number: zsh arithmetic reads it as nothing else. */
const NUMERIC_LITERAL = /^[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?$|^[-+]?0[xX][0-9a-fA-F]+$/
/** printf conversions whose argument zsh evaluates as arithmetic. */
const NUMERIC_CONVERSION = /^[diouxXeEfFgGaA]$/
/**
 * Exact read-mode options: bare = flag, `=` = one read value, `?` = optional attached read value,
 * `!` = one write value, `:` = two separate read values. Short clusters stop at a value-taking letter.
 * No long abbreviations. Conflicting BSD/GNU read-only arities (stat -f, base64 -i) are flags here:
 * a separate BSD read value remains an ordinary read operand, never swallowing another option.
 * awk and tree have no read exemption: their code/output modes are too costly to model here.
 */
const READ_OPTIONS: Readonly<Record<string, string>> = {
  cat: STDIN_OPTIONS.cat!,
  ls: "-a -A -b -B -c -C -d -D= -f -F -g -G -h -H -i -I -k -l -L -m -n -N -o -p -q -Q -r -R -s -S -t -T -u -U -v -w= -x -X -1 --all --almost-all --directory --human-readable --recursive --color? --sort= --time= --format= --quoting-style= --block-size= --hide= --ignore=",
  head: STDIN_OPTIONS.head!,
  tail: STDIN_OPTIONS.tail!,
  grep: STDIN_OPTIONS.grep!,
  egrep: STDIN_OPTIONS.grep!,
  fgrep: STDIN_OPTIONS.grep!,
  rgrep: STDIN_OPTIONS.grep!,
  wc: STDIN_OPTIONS.wc!,
  diff: "-a -b -B -c -C= -d -e -E -i -I= -n -N -p -q -r -s -t -T -u -U= -w -x= -X= -y --brief --recursive --unified? --context? --ignore-case --ignore-all-space --ignore-space-change --exclude= --exclude-from= --label= --to-file=",
  cmp: "-b -l -s -i= -n= --print-bytes --verbose --silent --quiet --ignore-initial= --bytes=",
  stat: "-L -f -F -l -n -q -r -s -t= -x --dereference --file-system --format= --printf= --terse",
  file: "-b -c -e= -F= -h -i -k -l -L -m= -n -N -p -P= -r -s -v -z -Z -0 --brief --checking-printout --exclude= --separator= --no-dereference --mime --mime-type --mime-encoding --keep-going --dereference --magic-file= --no-buffer --no-pad --preserve-date --raw --special-files --uncompress",
  less: "-a -c -e -E -f -F -g -G -i -I -j= -J -m -M -n -N -p= -q -Q -r -R -s -S -t= -u -U -w -W -x= -X -y= -z= --ignore-case --IGNORE-CASE --LINE-NUMBERS --RAW-CONTROL-CHARS --quit-if-one-screen --no-init",
  more: "-c -d -f -l -p -s -u -n=",
  cd: "-L -P -e -@",
  pwd: "-L -P",
  echo: "-n -e -E",
  printf: "",
  test: "",
  "[": "",
  true: "",
  false: "",
  find:
    "-H -L -P -X -E -d -depth -x -xdev -mount -follow -daystart -ignore_readdir_race -noignore_readdir_race -print -print0 -ls -prune -quit -true -false -a -and -o -or -not -empty -readable -writable -executable -nouser -nogroup " +
    [...FIND_VALUE_PREDICATES]
      .filter((option) => !["-fprint", "-fprint0", "-fls", "-files0-from"].includes(option))
      .map((option) => option + "=")
      .join(" "),
  sed: STDIN_OPTIONS.sed!,
  sort: STDIN_OPTIONS.sort! + " -o! --output!",
  uniq: STDIN_OPTIONS.uniq!,
  jq: STDIN_OPTIONS.jq! + " -f= -L= --from-file=",
  rg: "-a -b -c -C= -e= -f= -F -g= -h -H -i -I -l -L -m= -M= -n -N -o -p -q -r= -s -S -t= -T= -u -v -w -x -z -0 -A= -B= --after-context= --before-context= --context= --regexp= --file= --glob= --iglob= --type= --type-not= --max-count= --max-depth= --max-columns= --replace= --color= --colors= --encoding= --ignore-file= --sort= --sortr= --threads= --files --hidden --no-ignore --no-heading --heading --line-number --no-line-number --count --count-matches --files-with-matches --files-without-match --fixed-strings --ignore-case --smart-case --invert-match --word-regexp --line-regexp --only-matching --quiet --json --stats --multiline --pcre2 --text --null --null-data --follow --no-config",
  du: "-a -b -c -d= -h -H -k -L -m -P -s -S -x -X= --all --bytes --total --max-depth= --human-readable --summarize --one-file-system --exclude= --exclude-from= --block-size=",
  df: "-a -b -h -H -i -k -l -m -P -T -t= -x= --all --human-readable --inodes --local --portability --print-type --type= --exclude-type= --block-size= --output?",
  realpath:
    "-e -m -L -P -q -s -z --canonicalize-existing --canonicalize-missing --logical --physical --quiet --strip --no-symlinks --zero --relative-to= --relative-base=",
  readlink:
    "-e -f -m -n -q -s -v -z --canonicalize --canonicalize-existing --canonicalize-missing --no-newline --quiet --silent --verbose --zero",
  basename: "-a -s= -z --multiple --suffix= --zero",
  dirname: "-z --zero",
  cut: STDIN_OPTIONS.cut!,
  tr: STDIN_OPTIONS.tr!,
  nl: STDIN_OPTIONS.nl!,
  column: STDIN_OPTIONS.column!,
  fold: STDIN_OPTIONS.fold!,
  comm: STDIN_OPTIONS.comm!,
  join: "-a= -e= -i -j= -o= -t= -v= -1= -2= -z --ignore-case --check-order --nocheck-order --header --zero-terminated",
  paste: STDIN_OPTIONS.paste!,
  od: "-a -b -c -d -f -h -i -l -o -s -x -v -A= -j= -N= -t= -w? --address-radix= --skip-bytes= --read-bytes= --format= --width? --endian= --strings? --output-duplicates",
  hexdump: "-b -c -C -d -o -x -v -e= -f= -n= -s=",
  xxd: "-a -b -d -e -E -i -p -ps -r -u -C -h -v -c= -cols= -g= -l= -s= -o= -n= -R=",
  shasum:
    "-a= -b -c -p -t -U -0 --algorithm= --binary --check --portable --text --UNIVERSAL --tag --quiet --status --strict --warn",
  sha256sum: "-b -c -t -z --binary --check --text --zero --tag --ignore-missing --quiet --status --strict --warn",
  md5: "-q -r -s= -p -t -x",
  date: "-u -j -n -R -I? -r= -d= -f= -v= --utc --universal --rfc-email --iso-8601? --rfc-3339= --reference= --date= --file=",
  which: "-a -s --all --skip-alias --skip-functions --skip-dot --skip-tilde --show-dot --show-tilde",
  base64: STDIN_OPTIONS.base64!.replace("-o=", "-o!").replace("--output=", "--output!"),
}
// A parsed read mode is not enough for xargs: appended input may select an unparsed write/exec mode.
const READ_ONLY_PROGRAMS = new Set(
  "cat ls head tail grep egrep fgrep rgrep rg wc diff cmp stat file less more cd pwd echo printf test [ true false".split(
    " ",
  ),
)
/** Appended words may be options; these programs have no write or execution option. */
const LITERAL_FILENAME_READERS = new Set("head tail diff wc cmp cat ls stat sort".split(" "))
const OPERAND_SAFE_PROGRAMS = new Set([
  ...[...LITERAL_FILENAME_READERS].filter((program) => !["diff", "sort"].includes(program)),
  ..."grep egrep fgrep rgrep pwd echo true false".split(" "),
])

function appendedOperandsSafe(words: GrammarWord[]): boolean {
  if (!words.length) return true
  const program = words[0]?.value ?? ""
  if (program === "printf") {
    const format = words[words[1]?.value === "--" ? 2 : 1]
    return !!format?.literal && !format.value.startsWith("-")
  }
  if (OPERAND_SAFE_PROGRAMS.has(program)) return true
  return (
    READ_ONLY_PROGRAMS.has(program) && readOptions(READ_OPTIONS[program] ?? "", words.slice(1))?.terminated === true
  )
}

/** Project tools default to asking; only the complete forms in projectTool are admitted. */
const PROJECT_PROGRAMS = new Set(
  (
    "npm npx pnpm yarn bun bunx uv uvx pip pipx poetry hatch pdm rye flit cargo rustc go gem bundle make " +
    "docker podman tsc ruff mypy pytest eslint prettier vite vitest webpack rollup cmake twine brew build " +
    "nerdctl buildah conda mamba micromamba pixi rustup ninja rake pipenv tox nox composer dotnet deno vp"
  ).split(" "),
)
/** Implicit protected writers and command carriers whose operand language is deliberately not modelled. */
const ASK_PROGRAMS = new Set(
  (
    "pre-commit husky lefthook overcommit crontab at batch launchctl systemctl schtasks defaults chsh " +
    "watch parallel entr arch xcrun expect tmux screen hyperfine " +
    "prek direnv mise asdf nvm fnm pyenv rbenv volta rustup sdk corepack " +
    "apt apt-get dnf yum port nix-env nix scoop winget choco nodenv goenv proto pkgx easy_install anacron fcrontab patch awk"
  ).split(" "),
)
/** Programs for which a lone dash is the standard input/output stream, never an operand path. */
const STDIO_PROGRAMS = new Set("tee tar curl wget gzip gunzip bzip2 bunzip2 xz unxz dd".split(" "))

/** Decode only positively listed option identities, retaining values and positional output slots. */
function readOptions(
  table: string,
  args: readonly GrammarWord[],
  inertOptions = false,
  stopAtOperand = false,
):
  | {
      operands: GrammarWord[]
      values: { option: string; word: GrammarWord; write: boolean }[]
      options: string[]
      terminated: boolean
    }
  | undefined {
  const entries = table.split(" ")
  const operands: GrammarWord[] = []
  const values: { option: string; word: GrammarWord; write: boolean }[] = []
  const identities: string[] = []
  let options = true
  let terminated = false
  for (let index = 0; index < args.length; index += 1) {
    const word = args[index]!
    if (options && word.literal && word.value === "--") {
      options = false
      terminated = true
      continue
    }
    if (!options || !word.value.startsWith("-") || word.value === "-") {
      if (options && word.optionCapable && !inertOptions) return undefined
      operands.push(word)
      if (stopAtOperand) options = false
      continue
    }
    if (!word.literal || (word.glob && !(word.value.startsWith("--") && word.value.includes("=")))) return undefined
    const text = word.value
    const equal = text.indexOf("=")
    const name = equal < 0 ? text : text.slice(0, equal)
    const exact = entries.find((entry) => entry.replace(/[=?!:]$/, "") === name)
    // Single-dash long options (find, xxd) match whole identities before short clustering.
    const parts = exact || text.startsWith("--") ? [name] : [...text.slice(1)].map((letter) => "-" + letter)
    let offset = 1
    for (const option of parts) {
      const entry = entries.find((candidate) => candidate.replace(/[=?!:]$/, "") === option)
      if (!entry) return undefined
      identities.push(option)
      const arity = /[=?!:]$/.exec(entry)?.[0]
      if (!arity) {
        if (equal >= 0) return undefined
        offset += 1
        continue
      }
      const attached =
        exact || text.startsWith("--") ? (equal < 0 ? "" : text.slice(equal + 1)) : text.slice(offset + 1)
      if (arity === ":" && equal >= 0) return undefined
      for (let at = 0; at < (arity === ":" ? 2 : 1); at += 1) {
        const value = attached ? { ...word, value: attached } : arity === "?" ? undefined : args[++index]
        if (value) {
          if ((value.glob && !(attached && arity === "=")) || (!value.literal && !/^"[^\n]*"$/.test(value.raw)))
            return undefined
          values.push({ option, word: value, write: arity === "!" })
        } else if (arity !== "?") return undefined
      }
      break
    }
  }
  return { operands, values, options: identities, terminated }
}

/** Retain lexical uncertainty when an existing shell scan shares the positive option parser. */
function grammarArgument(word: ShellWord): GrammarWord {
  return {
    raw: word.raw ?? word.value,
    value: word.value,
    literal: !word.dynamic && !word.syntax,
    bare: !word.quoted && !word.dynamic && !word.syntax,
    substitution: false,
    special: word.syntax === true,
    glob: word.glob === true,
    optionCapable: word.dynamic || word.glob === true,
    lead: undefined,
  }
}

/** Dot directories explicitly admitted as ordinary project locations. */
const DOT_ALLOW = new Set(".github .temp .reports .plans .notes .cache .experiments .developments".split(" "))

/** Admit only original literal ASCII spellings of ordinary relative project files. */
function ordinaryProjectPath(word: Pick<GrammarWord, "value" | "literal" | "glob">): boolean {
  const { value } = word
  return (
    word.literal &&
    !word.glob &&
    !/[^\x00-\x7e]/.test(value) &&
    !/^[A-Za-z]:|\\|~[0-9]/.test(value) &&
    !/^[/~]/.test(value) &&
    value !== "-" &&
    value
      .split("/")
      .every(
        (segment) =>
          segment !== ".." && (!segment.startsWith(".") || segment === "." || DOT_ALLOW.has(segment.toLowerCase())),
      )
  )
}

/** Remote copy paths are shell text after the first host colon, not shell-quoted local data. */
function safeRemotePath(word: GrammarWord): boolean {
  const remote = /^(?:[^@/:]+@)?(\[[^\]]+\]|::1|[^/:\\]+):(.*)$/.exec(word.value)
  if (remote && !/^[A-Za-z]:[/\\]/.test(word.value)) {
    const host = remote[1]!.replace(/^\[|\]$/g, "").toLowerCase()
    const path = remote[2]!
    if (path.startsWith("-")) return false
    if (
      ["localhost", "127.0.0.1", "::1", hostname().toLowerCase()].includes(host) &&
      !ordinaryProjectPath({ ...word, value: path })
    )
      return false
    return word.literal && !word.glob && /^[A-Za-z0-9_./~@%+,=:-]*$/.test(path)
  }
  const colon = word.value.indexOf(":")
  if (colon <= 0 || /^[A-Za-z]:[/\\]/.test(word.value) || /[/\\]/.test(word.value.slice(0, colon))) return true
  return word.literal && !word.glob && /^[A-Za-z0-9_./~@%+,=:-]*$/.test(word.value.slice(colon + 1))
}

/** Index of the first remote command word after ssh's options and destination; the length when there is none. */
function sshCommandIndex(values: readonly string[]): number {
  const valueLetters = EXEC_OPTION_SPECS.ssh!.shortValues ?? ""
  let optionsOpen = true
  let destination = false
  for (let index = 0; index < values.length; index += 1) {
    const value = values[index]!
    if (optionsOpen && value === "--") optionsOpen = false
    else if (optionsOpen && value.startsWith("-") && value !== "-") {
      // ssh reads options before and after the destination; a value-taking letter ends its cluster.
      const letters = value.slice(1)
      const at = [...letters].findIndex((letter) => valueLetters.includes(letter))
      if (at === letters.length - 1) index += 1
    } else if (!destination) destination = true
    else return index
  }
  return values.length
}
/** Wrappers that start a shell or take a user, file or root before any program. */
const GRAMMAR_LAUNCHERS = new Set(["su", "runuser", "sg", "script", "nsenter", "chroot"])
/** Characters admitted in an unquoted word; anything else rejects. `#` and `^` never start a word. */
const GRAMMAR_WORD_CHAR = /[A-Za-z0-9_./:@%+,=~*?{}#^-]/
/** Characters a backslash may make literal outside quotes: a blank or ASCII punctuation, never a letter or digit. */
const ESCAPABLE = /^[ \t!-/:-@[-`{-~]$/
/** Characters that end an unquoted word. */
const GRAMMAR_WORD_END = /[ \t\n;&|<>()]/
/** One double-quoted parameter expansion and nothing else, such as "$name". */
const QUOTED_PARAMETER = /^"\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\}|[1-9#?])"$/
/** A prefix assignment whose value is literal text. */
const SAFE_ASSIGNMENT = /^([A-Za-z_][A-Za-z0-9_]*)=((?:[A-Za-z0-9_./:@%+,-]|'[^']*'|"[^"$`\\]*")*)$/

interface GrammarWord {
  /** Spelling with quotes. */
  readonly raw: string
  /** Text after quote removal; meaningful only for literal words. */
  readonly value: string
  /** No parameter expansion or command substitution. */
  readonly literal: boolean
  /** Literal and unquoted. */
  readonly bare: boolean
  /** Contains a command substitution. */
  readonly substitution: boolean
  /** A lone `[`, `]`, `=`, `==` or `!=`, admitted only as test syntax. */
  readonly special: boolean
  /** Contains an unquoted `*` or `?`, so filenames may supply further words. */
  readonly glob: boolean
  /**
   * May expand to a word starting with `-` that its spelling does not fix: it begins with an expansion
   * or glob, or its literal start is a dash prefix other than a literal `--name=`.
   */
  readonly optionCapable: boolean
  /** Literal text before the first expansion or glob; undefined when the word is wholly fixed. */
  readonly lead: string | undefined
}

/**
 * How an option-reading program takes values and which options make it run a command line.
 * `shortValues` lists short option letters whose value follows (attached or as the next word);
 * `longValues` lists long option names whose value is the next word when no `=` is given.
 */
interface OptionSpec {
  /** Exact inert flags; every other option asks, without guessing aliases or option arity. */
  readonly safeOptions?: string
  readonly shortValues?: string
  readonly longValues?: readonly string[]
  /** Option identities whose source or indirect arguments cannot be judged; values list aliases. */
  readonly unjudged?: Readonly<Record<string, string>>
  readonly singleDashLong?: boolean
  /** Leading old-style option letters without a dash, as in `tar cf`. */
  readonly oldStyle?: boolean
  /** Options end at the first operand, whose following words belong to the program it names. */
  readonly stopAtOperand?: boolean
  /** Decide whether an option runs a command line; `value` is undefined for flags or unknown values. */
  readonly runs?: (option: string, value: GrammarWord | string | undefined) => boolean
  /** Extract a newly modelled shell command channel; existing exec-capable options still always ask. */
  readonly commandLine?: (option: string, value: string) => string | undefined
}

/** Treat a long option written as any unambiguous-looking prefix of a dangerous name as that name. */
function longPrefixOf(option: string, names: readonly string[]): boolean {
  const name = option.replace(/^-+/, "").split("=", 1)[0]!
  return names.some((candidate) => candidate === name || (name.length >= 2 && candidate.startsWith(name)))
}

/** ssh, scp and sftp `-o` keys that run a local command line. */
function sshCommandOption(value: GrammarWord | string | undefined): boolean {
  if (value === undefined) return false
  if (typeof value !== "string" && !value.literal) return true
  const text = typeof value === "string" ? value : value.value
  return /^\s*(?:proxycommand|localcommand|knownhostscommand|pkcs11provider|securitykeyprovider|xauthlocation)\s*(?:=|\s|$)/i.test(
    text,
  )
}

/** Read the case-insensitive SSH directive using either its equals or whitespace separator. */
function sshRemoteCommand(option: string, value: string): string | undefined {
  if (option !== "-o") return undefined
  return /^\s*remotecommand(?:\s*=\s*|\s+|$)(.*)$/is.exec(value)?.[1]
}

const COMPILER_CODE_OPTIONS = "-fplugin -fpass-plugin -load -B -Xclang -mllvm"

// Interpreters and debuggers admit only documented informational or harmless mode flags. Other entries model specific
// indirect-input or command channels; inherited startup files remain outside this text classifier.
const EXEC_OPTION_SPECS: Readonly<Record<string, OptionSpec>> = {
  python: { unjudged: { code: "-c" } },
  node: { unjudged: { eval: "-e --eval", print: "-p --print" } },
  deno: { safeOptions: "--help --version" },
  bun: { safeOptions: "--help --version" },
  r: { safeOptions: "--help --version" },
  rscript: { safeOptions: "--help --version" },
  perl: { safeOptions: "-h -v -V -w -X --help --version" },
  ruby: { safeOptions: "-h -v -w -c --help --version" },
  php: { safeOptions: "-h -v --help --version" },
  lua: { safeOptions: "-v" },
  julia: { safeOptions: "-h -v --help --version" },
  tsx: { safeOptions: "--help --version" },
  "ts-node": { safeOptions: "-h -v --help --version" },
  ipython: { safeOptions: "--help --version" },
  osascript: { safeOptions: "", stopAtOperand: true },
  gdb: { safeOptions: "--help --version" },
  lldb: { safeOptions: "-v -h -x --no-lldbinit --help --version" },
  clang: { singleDashLong: true, unjudged: { config: "--config", code: COMPILER_CODE_OPTIONS } },
  "clang++": { singleDashLong: true, unjudged: { config: "--config", code: COMPILER_CODE_OPTIONS } },
  gcc: { singleDashLong: true, unjudged: { specs: "-specs", code: COMPILER_CODE_OPTIONS } },
  "g++": { singleDashLong: true, unjudged: { specs: "-specs", code: COMPILER_CODE_OPTIONS } },
  cc: { singleDashLong: true, unjudged: { specs: "-specs", code: COMPILER_CODE_OPTIONS } },
  "c++": { singleDashLong: true, unjudged: { specs: "-specs", code: COMPILER_CODE_OPTIONS } },
  curl: { shortValues: "AbcCdDeEFHhKmoPQrTtuUwxyXYz", unjudged: { config: "-K --config", engine: "--engine" } },
  wget: {
    shortValues: "aABDeiIloOPQtTURwX",
    unjudged: { config: "--config", execute: "-e --execute", credentials: "--use-askpass" },
  },
  nvcc: { singleDashLong: true, unjudged: { options: "--options-file -optf" } },
  file: { shortValues: "eFfmP", unjudged: { files: "-f --files-from" } },
  find: { singleDashLong: true, unjudged: { files: "-files0-from" } },
  sort: { unjudged: { files: "--files0-from" } },
  du: { unjudged: { files: "--files0-from" } },
  wc: { unjudged: { files: "--files0-from" } },
  ssh: {
    shortValues: "BbcDEeFIiJLlmOoPpQRSWw",
    longValues: [],
    runs: (option, value) => option === "-I" || (option === "-o" && sshCommandOption(value)),
    commandLine: sshRemoteCommand,
  },
  scp: {
    shortValues: "cDFiJloPSX",
    longValues: [],
    runs: (option, value) => ["-S", "-D"].includes(option) || (option === "-o" && sshCommandOption(value)),
    commandLine: sshRemoteCommand,
  },
  sftp: {
    shortValues: "BbcDFiJloPRSsX",
    longValues: [],
    runs: (option, value) => ["-S", "-D", "-b"].includes(option) || (option === "-o" && sshCommandOption(value)),
    commandLine: (option, value) => (option === "-s" ? value : sshRemoteCommand(option, value)),
  },
  rsync: {
    unjudged: { files: "--files-from" },
    shortValues: "efTMB@",
    longValues: (
      "rsh rsync-path filter exclude include exclude-from include-from files-from temp-dir partial-dir " +
      "compare-dest copy-dest link-dest backup-dir suffix chmod chown usermap groupmap timeout contimeout port " +
      "sockopts password-file log-file log-file-format out-format bwlimit max-size min-size max-delete " +
      "block-size modify-window compress-choice compress-level checksum-choice info debug iconv " +
      "remote-option outbuf protocol stop-after stop-at write-batch only-write-batch read-batch address config"
    ).split(" "),
    runs: (option) => option === "-e" || (option.startsWith("--") && longPrefixOf(option, ["rsh"])),
    commandLine: (option, value) =>
      option.startsWith("--") && longPrefixOf(option, ["rsync-path"]) ? value : undefined,
  },
  tar: {
    unjudged: { files: "-T --files-from" },
    shortValues: "bCfFgHIKLNTVX",
    longValues: (
      "file directory files-from exclude exclude-from to-command checkpoint-action use-compress-program " +
      "rsh-command info-script new-volume-script format label newer newer-mtime after-date " +
      "listed-incremental blocking-factor record-size tape-length owner group mode mtime transform xform"
    ).split(" "),
    oldStyle: true,
    runs: (option) =>
      ["-I", "-F"].includes(option) ||
      (option.startsWith("--") &&
        longPrefixOf(option, [
          "to-command",
          "checkpoint-action",
          "use-compress-program",
          "rsh-command",
          "info-script",
          "new-volume-script",
        ])),
  },
}

/**
 * Walk an option-reading program's arguments up to `--`: reject exec-capable options and any word in an
 * option position that could expand to an option. Values of value-taking options are not option positions.
 */
function optionsRejected(spec: OptionSpec, args: GrammarWord[], commandAllowed?: (body: string) => boolean): boolean {
  if (spec.safeOptions !== undefined) {
    const safe = spec.safeOptions.split(" ").filter(Boolean)
    const end = spec.stopAtOperand ? args.findIndex((word) => !word.value.startsWith("-")) : -1
    return args
      .slice(0, end < 0 ? undefined : end)
      .some((word) => word.optionCapable || (word.value.startsWith("-") && !safe.includes(word.value)))
  }
  const aliases = Object.values(spec.unjudged ?? {}).flatMap((value) => value.split(" "))
  const shortValues =
    (spec.shortValues ?? "") +
    aliases
      .filter((option) => /^-[^-]$/.test(option))
      .map((option) => option[1])
      .join("")
  const longValues = [
    ...(spec.longValues ?? []),
    ...aliases.filter((option) => option.startsWith("--")).map((option) => option.slice(2)),
  ]
  const longAliases = aliases
    .filter((option) => option.startsWith("--") || spec.singleDashLong)
    .map((option) => option.replace(/^-+/, ""))
  const rejects = (option: string, value: GrammarWord | string | undefined): boolean => {
    if (
      aliases.includes(option) ||
      ((option.startsWith("--") || spec.singleDashLong) && longPrefixOf(option, longAliases))
    )
      return true
    if (spec.runs?.(option, value)) return true
    const text = typeof value === "string" ? value : (value?.value ?? "")
    const body = spec.commandLine?.(option, text)
    if (body === undefined) return false
    // SSH expands percent tokens without shell escaping; the text cannot prove their resulting grammar.
    return (
      value === undefined ||
      (typeof value !== "string" && (!value.literal || value.glob || value.optionCapable)) ||
      !body.trim() ||
      /^\s*-/.test(body) ||
      body.includes("%") ||
      !commandAllowed?.(body)
    )
  }
  for (let index = 0; index < args.length; index += 1) {
    const word = args[index]!
    if (word.literal && word.value === "--") return false
    const oldStyle = spec.oldStyle && index === 0 && word.literal && /^[A-Za-z]+$/.test(word.value)
    if (!oldStyle && (!word.literal || !word.value.startsWith("-") || word.value === "-")) {
      // A value-taking short option with an expanded value keeps its identity: `make -j$(nproc)`.
      const attachedTo = /^-([A-Za-z0-9])$/.exec(word.lead ?? "")?.[1]
      if (attachedTo !== undefined && shortValues.includes(attachedTo)) {
        if (rejects("-" + attachedTo, word)) return true
        continue
      }
      if (word.optionCapable) return true
      if (spec.stopAtOperand) return false
      continue
    }
    const option = word.value
    const singleLong = option.split("=", 1)[0]!
    if (
      !option.startsWith("--") &&
      (aliases.includes(singleLong) || (spec.singleDashLong && longPrefixOf(singleLong, longAliases)))
    )
      return true
    if (option.startsWith("--")) {
      const takesValue = !option.includes("=") && longPrefixOf(option, longValues)
      const value = option.includes("=")
        ? option.slice(option.indexOf("=") + 1)
        : takesValue
          ? args[index + 1]
          : undefined
      if (takesValue && value === undefined) return true
      if (rejects(option.split("=", 1)[0]!, value)) return true
      if (takesValue) index += 1
      continue
    }
    const letters = oldStyle ? option : option.slice(1)
    for (const [offset, letter] of [...letters].entries()) {
      if (!shortValues.includes(letter) || oldStyle) {
        if (rejects("-" + letter, undefined)) return true
        continue
      }
      const attached = letters.slice(offset + 1)
      if (!attached && !args[index + 1]) return true
      if (rejects("-" + letter, attached || args[index + 1])) return true
      if (!attached) index += 1
      break
    }
  }
  return false
}

/** List, per consumed argument, whether a printf format reads it as an arithmetic number; undefined is unknown. */
function printfConversions(format: string): boolean[] | undefined {
  // Positional `%N$` and `%(...)T` specifiers break the one-argument-per-conversion order.
  if (/%\d+\$|%\(/.test(format)) return undefined
  const kinds: boolean[] = []
  for (const match of format.matchAll(/%(?:%|[-+ #0']*(\*|\d+)?(?:\.(\*|\d*))?(?:hh|h|ll|l|L|q|j|z|t)?([A-Za-z]))/g)) {
    if (match[0] === "%%") continue
    if (match[1] === "*") kinds.push(true)
    if (match[2] === "*") kinds.push(true)
    kinds.push(NUMERIC_CONVERSION.test(match[3] ?? ""))
  }
  return kinds
}

/**
 * Accept only the modelled shell grammar; any unexpected character, construct or name rejects.
 *
 * The grammar is a list of simple commands joined by `;`, `&&`, `||`, `|` or newlines. Command
 * words are bare names and never uncatalogued builtins or reserved words; prefix assignments are
 * limited to {@link ASSIGN_ALLOW}; arguments hold literal characters, quotes and plain parameter
 * expansions; command substitutions recurse; only literal redirections are admitted. Heredoc bodies
 * are data unless a shell or script runner in the same text could execute them, in which case
 * they must satisfy the grammar too. A comment must itself read as grammar code, as an interactive
 * zsh without comments would run it.
 */
class ShellGrammar {
  /** Heredoc bodies seen by this scanner. */
  readonly bodies: string[] = []
  /**
   * Set when any command starts a shell, an opaque launcher or a path-named program.
   */
  launches = false
  private index = 0
  private readonly text: string
  private depth: number
  /** False once a directory change leaves ordinary relative project locations. */
  private cwdKnown: boolean

  constructor(text: string, depth: number, cwdKnown = true) {
    this.text = text
    this.depth = depth
    this.cwdKnown = cwdKnown
  }

  /** Accept the whole text as one command list. */
  accepts(): boolean {
    return this.depth <= MAX_SHELL_DEPTH && this.list(false) && this.index === this.text.length
  }

  /** Parse a command list; a nested list stops before its closing parenthesis. */
  private list(nested: boolean): boolean {
    const text = this.text
    const pending: { delimiter: string; strip: boolean; quoted: boolean }[] = []
    let words: GrammarWord[] = []
    let started = false
    let needCommand = false
    let writes: GrammarWord[] = []
    const finish = (): boolean => {
      const valid = (!started || this.command(words)) && writes.every((target) => this.writeTarget(target))
      words = []
      writes = []
      started = false
      needCommand = false
      return valid
    }
    while (this.index < text.length) {
      const char = text[this.index]!
      const rest = text.slice(this.index)
      if (char === " " || char === "\t") {
        this.index += 1
        continue
      }
      // A line continuation between words is whitespace; inside a word it would join two spellings.
      if (rest.startsWith("\\\n") && /^$|[ \t]/.test(text[this.index - 1] ?? "")) {
        this.index += 2
        continue
      }
      if (char === "\n") {
        if (started && !finish()) return false
        this.index += 1
        if (pending.length && !this.heredocBodies(pending.splice(0))) return false
        continue
      }
      if (char === ")") {
        if (!nested) return false
        break
      }
      if (char === "#") {
        if (nested) return false
        const end = text.indexOf("\n", this.index)
        const comment = new ShellGrammar(
          text.slice(this.index + 1, end < 0 ? text.length : end),
          this.depth + 1,
          this.cwdKnown,
        )
        if (!comment.accepts()) return false
        this.launches ||= comment.launches
        this.index = end < 0 ? text.length : end
        continue
      }
      const operator = /^(?:&&|\|\||\|&|[;&|(])/.exec(rest)?.[0]
      if (operator) {
        if (operator === "|&" || operator === "&" || operator === "(" || !started) return false
        if (!finish()) return false
        needCommand = operator !== ";"
        this.index += operator.length
        continue
      }
      const redirect = /^(\d*)(<<<|<<-|<<|>>|>&|<&|<>|>\||&>|>|<)/.exec(rest)
      if (redirect) {
        // `{name}>file` assigns a variable in bash and zsh.
        if (text[this.index - 1] === "}") return false
        this.index += redirect[0].length
        const document = this.redirection(redirect[2]!, nested, (target) => writes.push(target))
        if (document === false) return false
        if (document) pending.push(document)
        started = true
        continue
      }
      const special = /^(?:\[|\]|==?|!=)(?=[ \t\n;&|<>)]|$)/.exec(rest)?.[0]
      if (special) {
        words.push({
          raw: special,
          value: special,
          literal: true,
          bare: true,
          substitution: false,
          special: true,
          glob: false,
          optionCapable: false,
          lead: undefined,
        })
        this.index += special.length
        started = true
        continue
      }
      const word = this.word()
      if (!word) return false
      words.push(word)
      started = true
    }
    // An operator that still waits for its command, or an unread heredoc, leaves the list incomplete.
    if (needCommand && !started) return false
    return finish() && !pending.length
  }

  /** Read pending heredoc bodies after a newline; an unclosed body rejects. */
  private heredocBodies(pending: { delimiter: string; strip: boolean; quoted: boolean }[]): boolean {
    const text = this.text
    for (const doc of pending) {
      let body = ""
      let closed = false
      while (this.index < text.length) {
        const end = text.indexOf("\n", this.index)
        const raw = text.slice(this.index, end < 0 ? text.length : end)
        const line = doc.strip ? raw.replace(/^\t+/, "") : raw
        this.index = end < 0 ? text.length : end + 1
        if (line === doc.delimiter) {
          closed = true
          break
        }
        body += line + "\n"
      }
      // An unquoted delimiter expands its body; only a body without expansion syntax stays data.
      if (!closed || (!doc.quoted && /[$`\\]/.test(body))) return false
      this.bodies.push(body)
    }
    return true
  }

  /** Validate one redirection; returns its heredoc, `undefined` for a file redirection, or `false`. */
  private redirection(
    operator: string,
    nested: boolean,
    onWrite: (target: GrammarWord) => void,
  ): { delimiter: string; strip: boolean; quoted: boolean } | undefined | false {
    const rest = this.text.slice(this.index)
    if (operator === ">&") {
      const target = /^[0-9]+(?=[ \t\n;&|<>)]|$)/.exec(rest)?.[0]
      if (!target) return false
      this.index += target.length
      return undefined
    }
    if (operator === "<<" || operator === "<<-") {
      if (nested) return false
      const spaces = /^[ \t]*/.exec(rest)![0].length
      const delimiter = /^(?:'([A-Za-z0-9_.-]+)'|"([A-Za-z0-9_.-]+)"|([A-Za-z0-9_]+))(?=[ \t\n;&|<>]|$)/.exec(
        rest.slice(spaces),
      )
      if (!delimiter) return false
      this.index += spaces + delimiter[0].length
      return {
        delimiter: delimiter[1] ?? delimiter[2] ?? delimiter[3]!,
        strip: operator === "<<-",
        quoted: delimiter[3] === undefined,
      }
    }
    if (![">", ">>", "<"].includes(operator)) return false
    this.index += /^[ \t]*/.exec(rest)![0].length
    const target = /^[ \t\n;&|<>()]|^$/.test(this.text.slice(this.index)) ? undefined : this.word()
    // A glob target can name a hook directory; bash and zsh treat /dev/tcp and /dev/udp paths as sockets.
    if (!target || target.substitution || target.glob) return false
    if (/^\/dev\/(?:tcp|udp)\//.test(target.value) || (!target.literal && target.value.startsWith("/dev/")))
      return false
    if (operator !== "<") {
      if (!this.writeTarget(target)) return false
      onWrite(target)
    }
    return undefined
  }

  /** Check paths and option files; echo/printf equality data retains its existing literal-path checks. */
  private writeTarget(word: GrammarWord, literalData = false): boolean {
    // Earlier-call symlinks, archives unpacked into config directories, core.hooksPath in ordinary directories,
    // editors and other-language writers remain limits of this lexical guard.
    if (!this.cwdKnown || !ordinaryProjectPath(word) || (!literalData && /(?:^|[,=])@/.test(word.value))) return false
    for (let equal = word.value.indexOf("="); equal >= 0; equal = word.value.indexOf("=", equal + 1)) {
      const value = word.value.slice(equal + 1)
      if ((!literalData && value.startsWith("@")) || !ordinaryProjectPath({ ...word, value })) return false
    }
    return true
  }

  /** Check every possible path operand and attached long-option/dd output value, without a sink catalogue. */
  private writeOperands(args: readonly GrammarWord[], program = ""): boolean {
    let options = true
    for (let index = 0; index < args.length; index += 1) {
      const word = args[index]!
      if (options && word.literal && word.value === "--") {
        options = false
        continue
      }
      if (word.literal && word.value === "-" && (Object.hasOwn(READ_OPTIONS, program) || STDIO_PROGRAMS.has(program)))
        continue
      const equals = word.value.indexOf("=")
      if (equals >= 0) {
        if (!this.writeTarget(word)) return false
        continue
      }
      if (options && word.literal && word.value.startsWith("-") && word.value !== "-") {
        // Attached short values can hide a root or dot segment behind option letters; no sink list is needed.
        const valueOption = WRAPPER_OPERANDS[program]?.options.split(" ").includes(word.value.slice(0, 2))
        const suffix = valueOption ? word.value.slice(2) || undefined : /^-[A-Za-z]+(.+)$/s.exec(word.value)?.[1]
        if (suffix !== undefined && !this.writeTarget({ ...word, value: suffix })) return false
        continue
      }
      if (!this.writeTarget(word)) return false
    }
    return true
  }

  /** Read one argument word; `undefined` rejects. */
  private word(): GrammarWord | undefined {
    const text = this.text
    const start = this.index
    let value = ""
    let literal = true
    let quoted = false
    let substitution = false
    let glob = false
    let braces = 0
    // Literal text before the first expansion or glob: it fixes how the expanded word can begin.
    let lead: string | undefined
    while (this.index < text.length) {
      const char = text[this.index]!
      if (GRAMMAR_WORD_END.test(char)) break
      if (char === "\\") {
        // An escaped blank or punctuation character is literal data; a letter, digit or newline is not.
        const next = text[this.index + 1] ?? ""
        if (!ESCAPABLE.test(next)) return undefined
        value += next
        quoted = true
        this.index += 2
      } else if (char === "'") {
        const end = text.indexOf("'", this.index + 1)
        if (end < 0) return undefined
        value += text.slice(this.index + 1, end)
        quoted = true
        this.index = end + 1
      } else if (char === '"') {
        const part = this.doubleQuoted()
        // An expansion inside a brace group can complete a sequence expression such as `{1.$9.3}`.
        if (!part || (braces && !part.literal)) return undefined
        if (!part.literal) lead ??= value + part.lead
        value += part.value
        literal &&= part.literal
        substitution ||= part.substitution
        quoted = true
      } else if (char === "$" && /^(?:[ \t\n;&|<>)]|$)/.test(text[this.index + 1] ?? "")) {
        // A dollar sign ending a word is literal text.
        value += char
        this.index += 1
      } else if (char === "$" || char === "`") {
        if (braces) return undefined
        lead ??= value
        const expansion = this.expansion()
        if (!expansion) return undefined
        literal = false
        substitution ||= expansion.substitution
      } else {
        if (
          !GRAMMAR_WORD_CHAR.test(char) &&
          !(char === "[" && programIdentity(value + char) === "[" && GRAMMAR_WORD_END.test(text[this.index + 1] ?? " "))
        )
          return undefined
        // Word-initial `=` is zsh command-path and process substitution, `^` a zsh negation; `~[` runs a function.
        if (this.index === start && (char === "=" || char === "^")) return undefined
        if (
          this.index === start &&
          char === "~" &&
          !/^(?:[/A-Za-z0-9_+ \t\n;&|<>)-]|$)/.test(text[this.index + 1] ?? "")
        )
          return undefined
        // A brace group holding `,` or `..` expands into several words, option words included.
        if (braces && (char === "," || (char === "." && text[this.index + 1] === "."))) return undefined
        if (char === "{") braces += 1
        if (char === "}" && braces) braces -= 1
        if (char === "*" || char === "?") {
          glob = true
          lead ??= value
        }
        value += char
        this.index += 1
      }
    }
    if (this.index === start) return undefined
    // A literal `--name=` keeps its option identity whatever its value expands to.
    const fixedOption = lead !== undefined && /^--?[A-Za-z0-9][A-Za-z0-9_-]*=/.test(lead)
    const optionCapable = lead !== undefined && (lead === "" || (lead.startsWith("-") && !fixedOption))
    // An option-shaped glob can only expand to option words.
    if (glob && lead?.startsWith("-") && !fixedOption) return undefined
    return {
      raw: text.slice(start, this.index),
      value,
      literal,
      bare: literal && !quoted,
      substitution,
      special: false,
      glob,
      optionCapable,
      lead,
    }
  }

  /** Read double-quoted text; only literal text, plain expansions and substitutions are admitted. */
  private doubleQuoted(): { value: string; literal: boolean; substitution: boolean; lead: string } | undefined {
    const text = this.text
    let value = ""
    let literal = true
    let substitution = false
    let lead: string | undefined
    this.index += 1
    while (this.index < text.length) {
      const char = text[this.index]!
      if (char === '"') {
        this.index += 1
        return { value, literal, substitution, lead: lead ?? value }
      }
      if (char === "\\") {
        // A backslash pair is data: it escapes $, `, " or \, and stays literal before anything else.
        const next = text[this.index + 1]
        if (next === undefined || next === "\n") return undefined
        value += /[$`"\\]/.test(next) ? next : char + next
        this.index += 2
        continue
      }
      // A dollar sign before whitespace or the closing quote is literal text.
      if ((char === "$" && !/^[\s"]/.test(text[this.index + 1] ?? "")) || char === "`") {
        lead ??= value
        const expansion = this.expansion()
        if (!expansion) return undefined
        literal = false
        substitution ||= expansion.substitution
        continue
      }
      value += char
      this.index += 1
    }
    return undefined
  }

  /** Read one parameter expansion or command substitution; `undefined` rejects. */
  private expansion(): { substitution: boolean } | undefined {
    const text = this.text
    const rest = text.slice(this.index)
    if (rest[0] === "`") {
      // Backquoted bodies end at the next backquote whatever their quoting; escapes are rejected anyway.
      const end = text.indexOf("`", this.index + 1)
      if (end < 0) return undefined
      const body = new ShellGrammar(text.slice(this.index + 1, end), this.depth + 1, this.cwdKnown)
      if (!body.accepts() || body.bodies.length) return undefined
      this.launches ||= body.launches
      this.index = end + 1
      return { substitution: true }
    }
    if (rest.startsWith("$((") || rest.startsWith("$[")) return undefined
    if (rest.startsWith("$(")) {
      if (this.depth + 1 > MAX_SHELL_DEPTH) return undefined
      this.index += 2
      this.depth += 1
      // Directory changes in command substitutions stay in their subshell.
      const cwdKnown = this.cwdKnown
      const valid = this.list(true)
      this.cwdKnown = cwdKnown
      this.depth -= 1
      if (!valid || text[this.index] !== ")") return undefined
      this.index += 1
      return { substitution: true }
    }
    const parameter = /^\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\}|[1-9@*#?$])/.exec(rest)?.[0]
    if (!parameter) return undefined
    const next = text[this.index + parameter.length] ?? ""
    // A following bracket is a zsh subscript, which evaluates arithmetic; zsh reads `$#name` as a length.
    if (next === "[" || (parameter === "$#" && /[A-Za-z_{]/.test(next))) return undefined
    // zsh history modifiers on unbraced parameters can evaluate arithmetic and assign while expanding.
    // Braces terminate the parameter explicitly; these colon suffixes are then plain text.
    if (
      !parameter.startsWith("${") &&
      next === ":" &&
      !/^(?:[/\.~0-9\-\s'";&|<>()]|$)/.test(text[this.index + parameter.length + 1] ?? "")
    )
      return undefined
    this.index += parameter.length
    return { substitution: false }
  }

  /** Validate one simple command: allowed assignments, then a modelled command word. */
  private command(words: GrammarWord[]): boolean {
    let index = 0
    while (index < words.length && /^[A-Za-z_][A-Za-z0-9_]*\+?=/.test(words[index]!.raw)) {
      if (!safeAssignment(words[index]!)) return false
      index += 1
    }
    return index === words.length || this.program(words, index)
  }

  /** Validate a command word and its arguments, following catalogued wrappers to their program. */
  private program(words: GrammarWord[], index: number, privileged = false, external = false): boolean {
    const original = words[index]!
    const identity = programIdentity(words.slice(index))
    words = identity.words
    const word = words[0]!
    const args = words.slice(1)
    const program = word.value
    const wrapper = Object.hasOwn(WRAPPER_OPERANDS, program) ? wrapperProgram(program, words, 1) : undefined
    const elevation = WRAPPER_OPERANDS[program]?.elevation
    if (elevation === "always-ask" && !(program === "systemd-run" && wrapper?.userMode && !privileged)) return false
    privileged ||= elevation === "admit"
    if (
      privileged &&
      !Object.hasOwn(WRAPPER_OPERANDS, program) &&
      !Object.hasOwn(READ_OPTIONS, program) &&
      !ALLOWED_BUILTINS.has(program) &&
      !(PROJECT_PROGRAMS.has(program) && identity.safeForm) &&
      !(
        program === "git" &&
        classifySegment([program, ...args.map((arg) => arg.value)], "", "standard").kind === "read-only-git"
      )
    )
      return false
    if (ASK_PROGRAMS.has(program)) return false
    if (privileged && Object.hasOwn(READ_OPTIONS, program) && !["test", "["].includes(program)) {
      const form = readOptions(READ_OPTIONS[program]!, args)
      if (!form || form.values.some((value) => value.write)) return false
      if (program === "xxd" && form.options.includes("-r")) return false
      if (["xxd", "uniq"].includes(program) && form.operands.length > 1) return false
    }
    if (privileged && WRAPPER_OPERANDS[program]?.createsFile) return false
    if (
      Object.hasOwn(EXEC_OPTION_SPECS, program) &&
      optionsRejected(
        EXEC_OPTION_SPECS[program]!,
        args.map((arg) => ({ ...arg, optionCapable: false })),
        (body) => shellGrammar(body, this.depth + 1),
      )
    )
      return false
    if (identity.script && !Object.hasOwn(WRAPPER_OPERANDS, program)) return false
    if (identity.module && program !== "pytest") return false
    if (
      !privileged &&
      original.bare &&
      word.bare &&
      !identity.script &&
      EXEC_OPTION_SPECS[program]?.safeOptions !== undefined &&
      args.length > 0 &&
      args.every((arg) => arg.literal && EXEC_OPTION_SPECS[program]!.safeOptions!.split(" ").includes(arg.value))
    )
      return true
    // Alias recognition must not widen the project safe list; Python's pytest module is explicit.
    if (PROJECT_PROGRAMS.has(program) && !identity.safeForm) return false
    // Python options can select modules, inline code or startup configuration. Unknown option arity
    // asks; resolving a version or launcher alias must not create another quiet script-file form.
    if (["python", "node"].includes(program) && args[0]?.value.startsWith("-")) return false
    // Embedded response files can be passed through compiler/linker option values.
    if (!["echo", "printf"].includes(program) && args.some((arg) => /[,=]@/.test(arg.value))) return false
    const valuedOptions = (READ_OPTIONS[program] ?? "").split(" ")
    // Equality words retain both path spellings even in builtin/data-reader modes. Only known option
    // values can use their program's input exemption; output values are checked by the mode below.
    if (
      args.some(
        (arg) =>
          arg.value.includes("=") &&
          !valuedOptions.some(
            (option) => /[=?!:]$/.test(option) && option.slice(0, -1) === arg.value.split("=", 1)[0],
          ) &&
          !this.writeTarget(arg, ["echo", "printf"].includes(program)),
      )
    )
      return false
    if (word.special || program === "[")
      return word.value === "[" && !args.some((arg) => arg.value.startsWith("@")) && testOperands(args, true)
    const name = program
    if (
      !original.bare ||
      !word.bare ||
      !(original.value === ":" || program === "[" || /^[A-Za-z0-9_./-]+$/.test(original.value))
    )
      return false
    // These builtin readers never execute their literal operands. Other builtin modes still reject below.
    if (name === "which" || name === "type" || (name === "command" && /^(?:-v|-V)$/.test(args[0]?.value ?? ""))) {
      const table = name === "which" ? READ_OPTIONS.which! : name === "type" ? "-a -f -p -P -t" : "-v -V"
      return (
        args.every(
          (arg) =>
            arg.literal &&
            !arg.glob &&
            !arg.value.startsWith("@") &&
            (!arg.value.includes("=") || this.writeTarget(arg)),
        ) && readOptions(table, args) !== undefined
      )
    }
    if (
      SHELL_BUILTINS.has(name) &&
      !ALLOWED_BUILTINS.has(name) &&
      name !== "time" &&
      !(name === "command" && programIdentity(args[0]?.value ?? "") === "time")
    )
      return false
    if (name === "test") return !args.some((arg) => arg.value.startsWith("@")) && testOperands(args, false)
    // A lone `=` is a literal word; the other test tokens stay test syntax.
    if (!Object.hasOwn(WRAPPER_OPERANDS, name) && args.some((arg) => arg.special && arg.value !== "=")) return false
    if (name === "printf") return external ? appendedOperandsSafe(words) : printfArguments(args)
    // zsh evaluates an exit status as arithmetic, so only a plain number is inert there.
    if (name === "exit") return args.every((arg) => arg.literal && /^-?\d+$/.test(arg.value))
    if (NON_POSIX_SHELLS.has(program)) return false
    const launcher = SHELLS.has(program) || OPAQUE.has(program)
    this.launches ||= launcher || original.value.includes("/")
    // A directory change to a glob or an expansion may enter a hook directory before a relative write.
    this.launches ||= name === "cd" && args.some((arg) => !arg.literal || arg.glob)
    if (name === "cd") this.changeDirectory(args)
    // Wrapper operands belong to the wrapped program, whose reader/writer contract is checked below.
    if (!Object.hasOwn(WRAPPER_OPERANDS, name) && !this.programOperands(program, args, identity.fileInput)) return false
    // ssh joins the words after its destination into a command line the remote shell runs, like `sh -c`.
    if (program === "ssh") {
      this.launches = true
      const start = sshCommandIndex(args.map((arg) => arg.value))
      const remote = args.slice(start)
      if (optionsRejected(EXEC_OPTION_SPECS.ssh!, args.slice(0, start), (body) => shellGrammar(body, this.depth + 1)))
        return false
      if (remote.some((arg) => !arg.literal || arg.glob)) return false
      if (remote.length && !shellGrammar(remote.map((arg) => arg.value).join(" "), this.depth + 1)) return false
    }
    // Filenames and expansions can supply option or command words: launchers never take them, and Git and
    // GitHub never take a word that can expand to an option before the `--` separator.
    if (launcher && args.some((arg) => arg.glob || arg.optionCapable)) return false
    if (["git", "gh"].includes(program)) {
      const separator = args.findIndex((arg) => arg.literal && arg.value === "--")
      if (args.slice(0, separator < 0 ? undefined : separator).some((arg) => arg.optionCapable)) return false
    }
    if (program === "find" && !this.findArguments(args)) return false
    if (
      program !== "ssh" &&
      Object.hasOwn(EXEC_OPTION_SPECS, program) &&
      (EXEC_OPTION_SPECS[program]!.runs !== undefined || EXEC_OPTION_SPECS[program]!.commandLine !== undefined) &&
      optionsRejected(EXEC_OPTION_SPECS[program]!, args, (body) => shellGrammar(body, this.depth + 1))
    )
      return false
    // busybox and toybox run their first operand as an applet; a shell applet is a shell.
    const applet = ["busybox", "toybox"].includes(program) ? programIdentity(args[0]?.value ?? "") : ""
    if (SHELLS.has(applet) || NON_POSIX_SHELLS.has(applet) || OPAQUE.has(applet)) return false
    if (!Object.hasOwn(WRAPPER_OPERANDS, name)) return true
    const target = wrapper
    if (!target || !this.writeOperands(words.slice(1, target.index), name)) return false
    if (privileged && WRAPPER_OPERANDS[name]?.outputs) {
      const outputs = WRAPPER_OPERANDS[name]!.outputs!.split(" ")
      const table = [
        WRAPPER_FLAGS[name] ?? "",
        ...WRAPPER_OPERANDS[name]!.options.split(" ").map((option) => option + (outputs.includes(option) ? "!" : "=")),
      ].join(" ")
      const form = readOptions(table, words.slice(1, target.index))
      if (!form || form.values.some((value) => value.write)) return false
    }
    // Use the decoded option identity, including clusters, and keep an unknown directory sticky.
    if (target.changesDirectory) this.cwdKnown = false
    // xargs appends/replaces operands from stdin or an argument file. Only a reader may consume them;
    // the input source is irrelevant, and an explicit operand program must still be checked below.
    if (name === "xargs") {
      let final = programIdentity(words.slice(target.index)).words
      // Appended input can alter a wrapper's option/program slots; only a direct final reader is modelled.
      const wrapped = Object.hasOwn(WRAPPER_OPERANDS, final[0]?.value ?? "")
      while (Object.hasOwn(WRAPPER_OPERANDS, final[0]?.value ?? "")) {
        const nested = wrapperProgram(final[0]!.value, final, 1)
        if (!nested || nested.index === 0 || nested.index >= final.length) return false
        final = programIdentity(final.slice(nested.index)).words
      }
      if (!appendedOperandsSafe(final) || wrapped) return false
    }
    // A wrapped script file is opaque executable input; do not infer its content from its path.
    const operand = programIdentity(words.slice(target.index)).words
    if (name === "xargs" && target.replacement && !appendedOperandsSafe(operand)) return false
    if (SHELLS.has(operand[0]?.value ?? "") && operand[1] && !operand[1].value.startsWith("-")) return false
    if (name === "xargs" && target.replacement) {
      if (words[target.index]?.value.includes(target.replacement)) return false
      words = words.map((arg, at) =>
        at > target.index && arg.value.includes(target.replacement!) ? { ...arg, literal: false } : arg,
      )
    }
    if (target.index === words.length) {
      // A list-only sudo invocation prints policy; validation, invalidation and bare sudo still ask.
      const list =
        WRAPPER_OPERANDS[name]?.elevation === "admit"
          ? readOptions("-l --list -n --non-interactive -u= --user= -U= --other-user=", args)
          : undefined
      if (!privileged) return true
      if (!list || list.operands.length) return false
      return list.options.some((option) => ["-l", "--list"].includes(option))
    }
    return this.program(words, target.index, privileged, name === "xargs")
  }

  /** Apply the operand contract of a concrete program without treating its input paths as outputs. */
  private programOperands(program: string, args: GrammarWord[], fileInput: boolean): boolean {
    if (PROJECT_PROGRAMS.has(program)) return this.projectTool(program, args)
    if (Object.hasOwn(READ_OPTIONS, program)) {
      if (program === "find") return this.findOperands(args)
      const inertOptions = ["echo", "cd", "ls", "basename", "grep", "egrep", "fgrep", "rgrep"].includes(program)
      const parsed = readOptions(
        READ_OPTIONS[program]!,
        args.filter((arg) => !(["head", "tail"].includes(program) && arg.literal && /^-\d+$/.test(arg.value))),
        inertOptions,
        program === "echo",
      )
      if (!parsed) return false
      const search = ["grep", "egrep", "fgrep", "rgrep", "rg"].includes(program)
      const pattern =
        search && !parsed.values.some(({ option }) => ["-e", "--regexp", "-f", "--file"].includes(option))
          ? parsed.operands[0]
          : undefined
      // Patterns are data; head/diff/wc read literal filenames and do not expand response files.
      if (
        parsed.operands.some(
          (arg) =>
            arg.value.startsWith("@") &&
            arg !== pattern &&
            program !== "echo" &&
            !LITERAL_FILENAME_READERS.has(program),
        )
      )
        return false
      if (
        parsed.values.some(
          ({ option, word }) => word.value.startsWith("@") && !(search && ["-e", "--regexp"].includes(option)),
        )
      )
        return false
      if (
        [...parsed.operands, ...parsed.values.map((value) => value.word)].some(
          (arg) => arg.value.includes("=") && !this.writeTarget(arg, program === "echo"),
        )
      )
        return false
      if (["less", "more"].includes(program) && parsed.operands.some((arg) => arg.value.startsWith("+"))) return false
      if (program === "sed") {
        const expressions = parsed.values.filter((value) => ["-e", "--expression"].includes(value.option))
        const scripts = expressions.length ? expressions.map((value) => value.word) : parsed.operands.slice(0, 1)
        if (
          !scripts.length ||
          scripts.some((script) => !script.literal || script.glob || !readOnlySedScript(script.value))
        )
          return false
        return true
      }
      if (program === "date") {
        const formattedInput = parsed.options.includes("-f")
        if (formattedInput && !parsed.options.includes("-j")) return false
        if (parsed.operands.some((arg) => !arg.literal || (!formattedInput && !arg.value.startsWith("+")))) return false
      }
      if (parsed.values.some((value) => value.write))
        return (
          parsed.values.filter((value) => value.write).every((value) => this.writeOperands([value.word], program)) &&
          this.writeOperands(parsed.operands, program)
        )
      if (program === "xxd" && parsed.options.includes("-r")) return this.writeOperands(args, program)
      if (["xxd", "uniq"].includes(program) && parsed.operands.length > 1) return this.writeOperands(args, program)
      return true
    }
    if (program === "git") {
      const classification = classifySegment([program, ...args.map((arg) => arg.value)], "", "standard")
      if (classification.kind === "read-only-git") {
        // Git revisions and grep patterns do not expand @files. File-valued options still ask.
        const options = readOptions(args[0]?.value === "grep" ? READ_OPTIONS.grep! : "", args.slice(1))
        if (args[0]?.value === "grep" && args.some((arg) => arg.value.includes("@")) && !options) return false
        if (
          args[0]?.value === "grep" &&
          options?.values.some(({ option, word }) => ["-f", "--file"].includes(option) && word.value.startsWith("@"))
        )
          return false
        return true
      }
    }
    if (fileInput) {
      // Literal script files are launcher inputs. Their absent contents retain the existing file-script limit.
      const script = args.findIndex((arg) => !arg.value.startsWith("-"))
      if (args.slice(0, script).some((arg) => /^-(?:[A-Za-z]*[cemp]|-eval|-print)$/.test(arg.value)))
        return this.writeOperands(args, program)
      return this.writeOperands(
        args.filter((arg, at) => at !== script || !arg.literal || arg.glob),
        program,
      )
    }
    if (["rsync", "scp"].includes(program)) {
      if (args.some((arg) => !safeRemotePath(arg))) return false
      args = args.map((arg) => {
        const colon = arg.value.indexOf(":")
        return colon === 1 && !/^[A-Za-z]:[/\\]/.test(arg.value)
          ? { ...arg, value: "remote" + arg.value.slice(colon) }
          : arg
      })
    }
    // A literal shell operand could consume code through options this wrapper does not model: ask.
    // Other unlisted wrappers remain a limit; their operand language is not inferred from arbitrary data.
    if (args.some((arg) => SHELLS.has(programIdentity(arg.value)))) return false
    return this.writeOperands(args, program)
  }

  /** Admit complete project invocations; their project-defined test/build code is a by-design limit. */
  private projectTool(program: string, args: GrammarWord[]): boolean {
    if (!this.cwdKnown || args.some((arg) => !arg.literal || arg.glob || arg.value.startsWith("@"))) return false
    const path = (arg: GrammarWord): boolean => !arg.value.startsWith("-") && this.writeTarget(arg)
    const values = args.map((arg) => arg.value)
    switch (program) {
      case "pytest": {
        if (values.includes("--")) return false
        const parsed = readOptions("-q -x -v -k= -m= --co --collect-only", args)
        return (
          parsed !== undefined &&
          parsed.operands.every(path) &&
          parsed.values.every(({ word }) => !/^[-@]/.test(word.value))
        )
      }
      case "ruff": {
        const start =
          values[0] === "check" ? 1 : values[0] === "format" && ["--check", "--diff"].includes(values[1] ?? "") ? 2 : 0
        return start > 0 && args.slice(start).every(path)
      }
      case "mypy":
        return args.every(path)
      case "tsc":
        return values.length === 1 && values[0] === "--noEmit"
      case "npm":
        return (
          (values.length === 1 && values[0] === "test") ||
          (values.length === 2 && values[0] === "run" && ["test", "lint", "typecheck", "build"].includes(values[1]!))
        )
      case "cargo":
        return values.length === 1 && ["check", "test", "clippy"].includes(values[0]!)
      default:
        return false
    }
  }

  /** Keep find predicate inputs separate from filenames it writes and programs it starts. */
  private findOperands(args: GrammarWord[]): boolean {
    const reads: GrammarWord[] = []
    for (let index = 0; index < args.length; index += 1) {
      const arg = args[index]!
      if (FIND_EXEC_OPTIONS.has(arg.value)) {
        while (index + 1 < args.length && ![";", "+"].includes(args[index + 1]!.value)) index += 1
      } else if (/^-(?:fprint0?|fprintf|fls)$/.test(arg.value)) {
        if (!args[index + 1] || !this.writeTarget(args[++index]!)) return false
        if (arg.value === "-fprintf") index += 1
      } else if (arg.value === "-delete") {
        // Deletion is independently classified as destructive; roots must still be ordinary project locations.
        if (!args.filter((word) => !word.value.startsWith("-")).every((word) => this.writeTarget(word))) return false
      } else {
        reads.push(arg)
        if (FIND_VALUE_PREDICATES.has(arg.value)) {
          const value = args[++index]
          if (!value) return false
          reads.push(value)
        }
      }
    }
    const parsed = readOptions(READ_OPTIONS.find!, reads)
    return (
      parsed !== undefined &&
      parsed.operands.every(
        (word) => !word.value.startsWith("@") && (!word.value.includes("=") || this.writeTarget(word)),
      ) &&
      parsed.values.every(
        ({ option, word }) =>
          (["-name", "-iname", "-path", "-ipath"].includes(option) || !word.value.startsWith("@")) &&
          (!word.value.includes("=") || this.writeTarget(word)),
      )
    )
  }

  /** Keep unknown directories sticky; a safe relative cd cannot prove that it returns to the project. */
  private changeDirectory(args: readonly GrammarWord[]): void {
    const parsed = readOptions(READ_OPTIONS.cd!, args, true)
    this.cwdKnown &&= parsed?.operands.length === 1 && ordinaryProjectPath(parsed.operands[0]!)
  }

  /**
   * Validate find's arguments: every predicate position holds a word whose option identity is fixed,
   * and each -exec family program obeys the command-word rule and is never a launcher.
   */
  private findArguments(args: GrammarWord[]): boolean {
    for (let index = 0; index < args.length; index += 1) {
      const arg = args[index]!
      if (arg.literal && FIND_EXEC_OPTIONS.has(arg.value)) {
        const target = args[index + 1]
        if (!target?.bare || SHELLS.has(programIdentity(target.value)) || OPAQUE.has(programIdentity(target.value)))
          return false
        let end = index + 2
        while (end < args.length && !(args[end]!.literal && [";", "+"].includes(args[end]!.value))) end += 1
        const call = args
          .slice(index + 1, end)
          .map((word, at) => (at > 0 && word.value.includes("{}") ? { ...word, literal: false } : word))
        const cwdKnown = this.cwdKnown
        if (arg.value.endsWith("dir")) this.cwdKnown = false
        const valid = this.program(call, 0)
        this.cwdKnown = cwdKnown
        if (!valid) return false
        // A word that may expand to `;` or `+` can end the command early: what follows it is also read as predicates.
        for (let word = index + 2; word < end; word += 1)
          if ((!args[word]!.literal || args[word]!.glob) && !this.findArguments(args.slice(word + 1))) return false
        index = end
        continue
      }
      if (arg.optionCapable) return false
      if (arg.literal && FIND_VALUE_PREDICATES.has(arg.value)) index += 1
      else if (arg.literal && arg.value === "-fprintf") index += 2
    }
    return true
  }
}

/** Admit a prefix assignment only for an allowed name and a literal value. */
function safeAssignment(word: GrammarWord): boolean {
  if (!word.literal || word.glob) return false
  const match = SAFE_ASSIGNMENT.exec(word.raw)
  return match !== null && ASSIGN_ALLOW.has(match[1]!)
}

/**
 * Admit test operands that are literal or one quoted parameter, with string and file operators only.
 * zsh evaluates the `-t` descriptor as arithmetic, so it must be a plain number, and a literal holding
 * `[` could be a subscript there.
 */
function testOperands(input: GrammarWord[], bracket: boolean): boolean {
  if (bracket && !(input.at(-1)?.special && input.at(-1)?.value === "]")) return false
  const operands = bracket ? input.slice(0, -1) : input
  return operands.every((word, index) => {
    if (word.special) return ["=", "==", "!="].includes(word.value)
    if (word.substitution || word.glob) return false
    const previous = operands[index - 1]
    if (previous?.literal && previous.value === "-t") return word.literal && /^\d+$/.test(word.value)
    if (!word.literal) return QUOTED_PARAMETER.test(word.raw)
    if (word.value.includes("[")) return false
    return !word.value.startsWith("-") || TEST_OPERATORS.has(word.value)
  })
}

/**
 * Admit printf with no option before its format, and only plain numbers where zsh evaluates arithmetic.
 *
 * Options come only before the format (up to `--`); printf's one option, `-v`, assigns a variable, and a
 * word there that could expand to an option asks. After the format, an argument consumed by a numeric
 * conversion or a `*` width or precision must be a plain number; string arguments are data.
 */
function printfArguments(args: GrammarWord[]): boolean {
  let index = 0
  const first = args[0]
  if (first && (!first.literal || first.glob || first.optionCapable)) return false
  if (first?.value === "--") index = 1
  else if (first && first.value.startsWith("-") && first.value !== "-") return false
  const format = args[index]
  const rest = args.slice(index + 1)
  if (!format || !rest.length) return true
  const kinds = format.literal ? printfConversions(format.value) : undefined
  return rest.every((arg, position) => {
    const numeric = kinds === undefined || (kinds.length > 0 && kinds[position % kinds.length]!)
    return !numeric || (arg.literal && NUMERIC_LITERAL.test(arg.value))
  })
}

/**
 * Find the program slot after a catalogued wrapper's options; `undefined` rejects.
 *
 * Only catalogued flags and operand options are skipped. Operands of `env` and `sudo` that carry
 * `=` before the program are assignments and must be allowed literal assignments. Command-string,
 * split-string and implicit-shell options reject.
 */
function wrapperProgram(
  name: string,
  words: GrammarWord[],
  start: number,
): { index: number; changesDirectory: boolean; replacement?: string; userMode: boolean } | undefined {
  if (GRAMMAR_LAUNCHERS.has(name)) return undefined
  const operands = WRAPPER_OPERANDS[name]!.options.split(" ").filter(Boolean)
  const flags = (WRAPPER_FLAGS[name] ?? "").split(" ").filter(Boolean)
  let positional = ["timeout", "flock", "chrt", "taskset", "su", "gosu"].includes(name) ? 1 : 0
  let index = start
  let changesDirectory = false
  let replacement: string | undefined
  let userMode = false
  let managerTarget = false
  // User-mode systemd options remain reachable and preserve the assignment contract.
  const optionValueAllowed = (option: string, word: GrammarWord): boolean => {
    if (name !== "systemd-run") return true
    if (["-E", "--setenv"].includes(option)) return safeAssignment(word)
    if (!["-p", "--property"].includes(option) || !word.value.startsWith("Environment=")) return true
    const value = word.value.slice("Environment=".length)
    return !/[\s\\"']/.test(value) && safeAssignment({ ...word, raw: value, value })
  }
  const directoryOption = name === "env" ? "-C" : name === "sudo" ? "-D" : undefined
  // strace writes its trace to a command when the output name starts with a pipe or exclamation mark.
  const runsOutput = (option: string, value: string | undefined): boolean =>
    name === "strace" && /^(?:-o|--output)$/.test(option) && /^[|!]/.test(value ?? "")
  while (index < words.length) {
    const word = words[index]!
    if (word.special && !(word.value === "[" && positional === 0)) return undefined
    if (word.substitution || word.glob || word.optionCapable) return undefined
    const option = word.value
    if ((name === "env" || name === "sudo") && word.raw.includes("=") && !word.raw.startsWith("-")) {
      if (!safeAssignment(word)) return undefined
      index += 1
      continue
    }
    if (!word.literal || !option.startsWith("-")) {
      if (positional === 0) return { index, changesDirectory, replacement, userMode: userMode && !managerTarget }
      positional -= 1
      index += 1
      continue
    }
    if (option === "--")
      return { index: index + 1, changesDirectory, replacement, userMode: userMode && !managerTarget }
    if (
      (name === "sudo" && ["--shell", "--login", "-s", "-i"].includes(option)) ||
      (name === "doas" && option === "-s")
    )
      return undefined
    if (
      flags.includes(option) ||
      (name === "nice" && /^-\d+$/.test(option)) ||
      (name === "sudo" && /^--preserve-env=[A-Za-z_][A-Za-z0-9_]*(?:,[A-Za-z_][A-Za-z0-9_]*)*$/.test(option)) ||
      (option.startsWith("--") && operands.includes(option.split("=", 1)[0]!) && option.includes("="))
    ) {
      if (
        option.includes("=") &&
        !optionValueAllowed(option.split("=", 1)[0]!, {
          ...word,
          raw: word.raw.slice(word.raw.indexOf("=") + 1),
          value: option.slice(option.indexOf("=") + 1),
        })
      )
        return undefined
      managerTarget ||= name === "systemd-run" && ["--uid", "--gid", "--machine"].includes(option.split("=", 1)[0]!)
      if (name === "systemd-run" && ["--user", "--system"].includes(option)) userMode = option === "--user"
      if (runsOutput(option.split("=", 1)[0]!, option.split("=").slice(1).join("="))) return undefined
      if (name === "xargs" && option.split("=", 1)[0] === "--replace")
        replacement = option.slice(option.indexOf("=") + 1)
      changesDirectory ||= directoryOption !== undefined && option.split("=", 1)[0] === "--chdir"
      index += 1
      continue
    }
    if (operands.includes(option)) {
      const value = words[index + 1]
      if (!value || value.special || value.substitution) return undefined
      if (!optionValueAllowed(option, value)) return undefined
      managerTarget ||= name === "systemd-run" && ["--uid", "--gid", "-M", "--machine"].includes(option)
      if (runsOutput(option, value.literal ? value.value : "|")) return undefined
      if (name === "xargs" && ["-I", "-J", "--replace"].includes(option)) {
        if (!value.literal || value.glob || !value.value) return undefined
        replacement = value.value
      }
      changesDirectory ||= directoryOption !== undefined && (option === directoryOption || option === "--chdir")
      index += 2
      continue
    }
    if (option.startsWith("--")) return undefined
    // A short cluster: catalogued flags, optionally ending in one operand option with an attached or next value.
    const letters = [...option.slice(1)]
    const operandAt = letters.findIndex((letter) => operands.includes("-" + letter))
    const flagLetters = operandAt < 0 ? letters : letters.slice(0, operandAt)
    if (!flagLetters.every((letter) => flags.includes("-" + letter))) return undefined
    if (
      (name === "sudo" && flagLetters.some((letter) => ["s", "i"].includes(letter))) ||
      (name === "doas" && flagLetters.includes("s"))
    )
      return undefined
    const separate = operandAt === letters.length - 1
    if (separate && !words[index + 1]) return undefined
    if (operandAt >= 0) {
      const next = words[index + 1]
      const value = separate ? (next?.literal ? next.value : "|") : letters.slice(operandAt + 1).join("")
      if (
        !optionValueAllowed(
          "-" + letters[operandAt],
          separate ? next! : { ...word, raw: word.raw.slice(operandAt + 2), value },
        )
      )
        return undefined
      managerTarget ||= name === "systemd-run" && letters[operandAt] === "M"
      if (runsOutput("-" + letters[operandAt], value)) return undefined
      if (name === "xargs" && ["I", "J"].includes(letters[operandAt]!)) {
        if (separate && (!next?.literal || next.glob || !next.value)) return undefined
        replacement = value
      }
      changesDirectory ||= directoryOption !== undefined && "-" + letters[operandAt] === directoryOption
    }
    index += separate ? 2 : 1
  }
  return { index, changesDirectory, replacement, userMode: userMode && !managerTarget }
}

/** Decide whether text is inside the modelled shell grammar, including bodies a same-text shell may run. */
function shellGrammar(text: string, depth = 0): boolean {
  if (depth > MAX_SHELL_DEPTH || text.length > MAX_SHELL_CHARS) return false
  const scanner = new ShellGrammar(withoutFences(text), depth)
  if (!scanner.accepts()) return false
  // bash f and a remote shell fed a script from a file remain limits: the file's content is absent here.
  // A leading interpreter line is the only comment not read as code: it names the shell, never runs.
  return (
    !scanner.launches ||
    scanner.bodies.every((body) => shellGrammar(body.replace(/^#!\/[A-Za-z0-9_./ -]*\n/, ""), depth + 1))
  )
}

/** Classify supported shell shapes; unsupported Git syntax always asks. */
export function classifyCommand(command: string, mode: GuardrailMode = "standard"): Classification {
  try {
    const { bodyHit: _, ...classification } = classifyShellCommand(command, mode, 0)
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

/** Identify executable body commands after the shared wrapper normalization. */
function executableBodyRelevant(first: ShellWord | undefined): boolean {
  return Boolean(
    first &&
    (["git", "gh"].includes(programIdentity(first.value)) ||
      SHELLS.has(programIdentity(first.value)) ||
      OPAQUE.has(programIdentity(first.value)) ||
      first.ansiQuoted ||
      (first.dynamic && (!first.substitutions?.includes(undefined) || mentionsExecutable(first.value))) ||
      first.glob),
  )
}

/** Admit literal data-filter arguments, optionally excluding forms that persist input. */
function stdinDataAllowed(words: ShellWord[], allowFileWrites = true): boolean {
  words = programIdentity(words).words
  const program = words[0]!.value
  if (program === "git") return true
  if (!Object.hasOwn(STDIN_OPTIONS, program) || words.slice(1).some((word) => word.dynamic || word.syntax || word.glob))
    return false
  const args = words
    .slice(1)
    .map(grammarArgument)
    .filter((word) => !(["head", "tail"].includes(program) && /^-\d+$/.test(word.value)))
  const parsed = readOptions(STDIN_OPTIONS[program]!, args)
  if (!parsed) return false
  if (
    !allowFileWrites &&
    ((program === "tee" && parsed.operands.length > 0) || (program === "uniq" && parsed.operands.length > 1))
  )
    return false
  if (program !== "sed") return true
  const scripts = parsed.values
    .filter((value) => ["-e", "--expression"].includes(value.option))
    .map((value) => value.word)
  return (
    (scripts.length ? scripts : parsed.operands.slice(0, 1)).length > 0 &&
    (scripts.length ? scripts : parsed.operands.slice(0, 1)).every((script) => readOnlySedScript(script.value))
  )
}

/** Scan literal sed command lists without mistaking separators inside patterns for commands. */
function readOnlySedScript(script: string): boolean {
  const address = String.raw`(?:\d+|\$|/(?:\\[^\r\n]|[^/\\\r\n])*/[Ii]?)`
  const prefix = new RegExp(String.raw`^[ \t]*(?:${address}(?:[ \t]*,[ \t]*${address})?)?[ \t]*`)
  let rest = script
  while (rest.length) {
    rest = rest.slice(prefix.exec(rest)![0].length)
    const command = rest[0]
    if (!command || !"pdsyq=".includes(command)) return false
    rest = rest.slice(1)
    if (command === "s" || command === "y") {
      const delimiter = rest[0]
      if (!delimiter || !"/|#,:".includes(delimiter)) return false
      rest = rest.slice(1)
      for (let part = 0; part < 2; part += 1) {
        let at = 0
        while (at < rest.length && rest[at] !== delimiter) {
          if (rest[at] === "\\") at += 1
          at += 1
        }
        if (at >= rest.length || /[\r\n]/.test(rest.slice(0, at))) return false
        rest = rest.slice(at + 1)
      }
      if (command === "s") rest = rest.slice(/^[gpIiN0-9]*/.exec(rest)![0].length)
    } else if (command === "q") rest = rest.slice(/^[ \t]*\d*/.exec(rest)![0].length)
    rest = rest.trimStart()
    if (!rest.length) return true
    if (rest[0] !== ";") return false
    rest = rest.slice(1).trimStart()
  }
  return false
}

/**
 * How a recursive classification reads its text: `code` runs in a shell and must satisfy the
 * modelled grammar; `persisted` is saved or fed data judged by executable relevance; `data` is an
 * expanding heredoc body, whose substitutions alone run.
 */
type ShellContext = "code" | "persisted" | "data"

/** Bound literal shell recursion and retain the strongest guarded operation. */
function classifyShellCommand(
  command: string,
  mode: GuardrailMode,
  depth: number,
  stdin = false,
  inheritedFunctions: ReadonlyMap<string, ShellWord[][]> = new Map(),
  stdinBodies: readonly string[] = [],
  context: ShellContext = "code",
): Classification & { bodyHit: boolean } {
  const unparsed = {
    kind: "unparsed-git",
    command,
    reason: "cannot safely classify Git shell syntax",
    bodyHit: true,
  } as const
  if (depth > MAX_SHELL_DEPTH || command.length > MAX_SHELL_CHARS) return unparsed
  const parsed = shellCommands(command)
  const persistedBody = context === "persisted"
  if (persistedBody && parsed.gitEnvironment) return unparsed
  const functions = new Map([...inheritedFunctions, ...parsed.functions])
  let winner: Classification = { kind: "other", command, reason: "no guarded pattern matched" }
  const windowsAmbiguous = /(?:[A-Za-z]:\\|\\\\)[^\r\n]*(?:git|gh)\.exe\b/i.test(parsed.text)
  // Shell code is quiet only inside the modelled grammar, which subsumes the raw producer gate there.
  // Saved and expanding data bodies are outside the grammar's scope and keep that gate unchanged.
  const outsideGrammar = context === "code" && !shellGrammar(command, depth)
  const dynamicCode = context !== "code" && rawDynamicCode(command)
  let relevant = parsed.unparsedGit || windowsAmbiguous || dynamicCode
  let uncertain = parsed.uncertain || windowsAmbiguous || dynamicCode
  let changesDirectory = false
  let executableBody = false
  let unparsedBody = parsed.unparsedGit || dynamicCode
  for (const body of parsed.expandingBodies) {
    if (!mentionsGit(body)) continue
    const candidate = classifyShellCommand(body, mode, depth + 1, false, functions, [], "data")
    relevant ||= candidate.bodyHit
  }
  // An interactive zsh without comment support runs comment text, so code comments classify as code.
  for (const comment of context === "code" ? parsed.comments : []) {
    const candidate = classifyShellCommand(comment, mode, depth + 1, false, functions, [], "code")
    executableBody ||= candidate.bodyHit
    changesDirectory ||= candidate.changesDirectory === true
    if (SEVERITY[candidate.kind] > SEVERITY[winner.kind]) winner = { ...candidate, command }
  }
  for (const raw of parsed.commands) {
    const quotedReads = new Set<string>()
    let stdinFed = stdin || parsed.pipeInputs.has(raw)
    const fileFed = parsed.fileInputs.has(raw)
    const fedBodies = [...(stdin ? stdinBodies : []), ...(parsed.inputBodies.get(raw) ?? [])]
    for (const body of [...raw.flatMap((word) => word.substitutions ?? []), ...(parsed.substitutions.get(raw) ?? [])]) {
      const substitution =
        body === undefined
          ? undefined
          : classifyShellCommand(body, mode, depth + 1, stdinFed, functions, fedBodies, "code")
      const quotedRead =
        substitution?.kind === "read-only-git" &&
        body !== undefined &&
        raw.some((word) => word.quoted && word.substitutions?.includes(body)) &&
        raw.filter((word) => word.substitutions?.includes(body)).every((word) => word.quoted)
      if (quotedRead) quotedReads.add(body!)
      const hit = substitution ? substitution.kind !== "other" && !quotedRead : mentionsExecutable(command)
      relevant ||= hit
      executableBody ||= hit || (!quotedRead && substitution?.bodyHit === true)
      changesDirectory ||= substitution?.changesDirectory === true
    }
    const quotedReadWord = (word: ShellWord): boolean =>
      Boolean(
        word.quoted &&
        word.substitutions?.length &&
        word.substitutions.every((body) => body !== undefined && quotedReads.has(body)),
      )
    relevant ||= raw.some((word) => (word.dynamic || word.syntax) && mentionsGit(word.value) && !quotedReadWord(word))
    const input = raw.filter(
      (word) => !word.scope && !(word.syntax && /^[{}()]$/.test(word.value)) && (word.quoted || word.value !== "!"),
    )
    const normalized = executableWords(input)
    relevant ||= normalized.unparsedWrapper === true
    const wrapper = input.find((word) => !/^[A-Za-z_][A-Za-z0-9_]*=/.test(word.value))
    // The list marker also prefixes prose; its existing executable-body checks remain authoritative.
    unparsedBody ||=
      normalized.unparsedWrapper === true ||
      (wrapper !== undefined &&
        wrapper.value !== "-" &&
        Object.hasOwn(WRAPPER_OPERANDS, programIdentity(wrapper.value)) &&
        (normalized.uncertain || raw.some((word) => word.syntax)))
    // env split strings introduce another decoding/expansion boundary, even after ANSI-C quoting.
    relevant ||=
      normalized.opaque &&
      raw.some((word) => word.ansiQuoted || /\\|\$\{/.test(word.value) || encodedPayloadRelevant(word))
    executableBody ||=
      normalized.opaque &&
      raw.some((word) => word.ansiQuoted || /\\|\$\{/.test(word.value) || encodedPayloadRelevant(word))
    changesDirectory ||= normalized.changesDirectory
    relevant ||= raw
      .slice(0, normalized.source ? raw.indexOf(normalized.source) : raw.length - normalized.words.length)
      .some((word) => mentionsGit(word.value.replace(/^(?:-[A-Za-z0-9]*S|--[^=]+=)/, "")))
    uncertain ||= normalized.uncertain
    if (["export", "declare", "typeset"].includes(raw[0]?.value ?? ""))
      uncertain ||= raw.slice(1).some((word) => /^(?:GIT_\w*|SSH_ASKPASS|EDITOR|PAGER|VISUAL)(?:=|$)/.test(word.value))
    let words = normalized.words
    if (!words.length) continue
    const first = words[0]!
    const program = first.value
    const sourceName = normalized.source?.value ?? first.value
    if (normalized.inputOperands && appendedOperandsSafe(words.map(grammarArgument))) {
      // A reader receives appended filenames/data, not shell source, regardless of how xargs was fed.
      stdinFed = false
      fedBodies.length = 0
    }
    // Only a raw literal subject/copula command qualifies as prose.
    if (
      persistedBody &&
      raw.length === words.length &&
      raw.every((word, index) => !word.scope && word.value === words[index]!.value) &&
      !parsed.gitEnvironment &&
      ["git", "gh"].includes(program) &&
      words.length > 2 &&
      words[1]!.value === "is" &&
      !words.slice(2).some((word) => /^[-+]|[/:]/.test(word.value)) &&
      words.every((word) => !word.quoted && !word.dynamic && !word.syntax && !word.glob && !word.substitutions?.length)
    )
      continue
    stdinFed ||=
      fileFed &&
      (SHELLS.has(program) || OPAQUE.has(program) || normalized.opaque || first.dynamic || first.glob === true)
    for (const body of normalized.shellBodies ?? []) {
      const candidate = classifyShellCommand(body.value, mode, depth + 1, stdinFed, functions, fedBodies, "code")
      relevant ||= body.dynamic || body.ansiQuoted === true || candidate.kind !== "other"
      executableBody ||= body.dynamic || body.ansiQuoted === true || candidate.bodyHit
      changesDirectory ||= candidate.changesDirectory === true
    }
    executableBody ||=
      executableBodyRelevant(first) ||
      raw.some((word) => word.syntax && !word.dynamic && mentionsGit(word.value) && !quotedReadWord(word))
    // Persisted text may be executed in a later tool call; classify commands, not Git mentions.
    if (
      parsed.outputRedirects.has(raw) ||
      (!NO_STDIN.has(program) && !functions.has(sourceName) && !stdinDataAllowed(words, false))
    ) {
      for (const body of fedBodies) {
        const candidate = classifyShellCommand(body, mode, depth + 1, false, functions, [], "persisted")
        if (
          candidate.kind === "read-only-git" &&
          body.includes("@") &&
          shellGrammar(body, depth + 1) &&
          shellCommands(body).commands.some((command) => {
            const words = executableWords(command).words
            return words[0]?.value === "git" && words.slice(1).some((word) => word.value.startsWith("@"))
          })
        ) {
          if (SEVERITY[candidate.kind] > SEVERITY[winner.kind]) winner = { ...candidate, command }
          continue
        }
        if (candidate.bodyHit) {
          executableBody = true
          relevant = true
          uncertain = true
        }
      }
    }
    const envIndex = words.findIndex((word) => programIdentity(word.value) === "env")
    const splitIndex =
      envIndex < 0 ? -1 : words.findIndex((word, index) => index > envIndex && splitStringOption(word.value))
    if (splitIndex >= 0 && !DATA_COMMANDS.has(program)) {
      relevant ||= words
        .slice(splitIndex)
        .some((word) => word.ansiQuoted || /\\|\$\{/.test(word.value) || encodedPayloadRelevant(word))
      uncertain = true
    }
    if (words.some((word) => word.ansiQuoted)) {
      // Only known data consumers may use relevance matching to admit ANSI-C arguments.
      const dataProgram =
        DATA_COMMANDS.has(program) || NO_STDIN.has(program) || Object.hasOwn(STDIN_OPTIONS, program) || program === "ls"
      relevant ||=
        first.ansiQuoted === true || !dataProgram || (!DATA_COMMANDS.has(program) && raw.some(encodedPayloadRelevant))
      executableBody ||=
        first.ansiQuoted === true || !dataProgram || (!DATA_COMMANDS.has(program) && raw.some(encodedPayloadRelevant))
      uncertain = true
    }
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
    if (stdinFed || fileFed) {
      const name = sourceName
      for (const body of functions.get(name) ?? []) {
        const previous = parsed.inputBodies.get(body) ?? []
        const added = fedBodies.filter((text) => !previous.includes(text))
        const inputs = stdinFed ? parsed.pipeInputs : parsed.fileInputs
        if (inputs.has(body) && !added.length) continue
        parsed.inputBodies.set(body, [...previous, ...added])
        inputs.add(body)
        parsed.commands.push(body)
      }
    }
    const values = words.map((word) => word.value)
    const dynamic = first.dynamic || first.glob
    relevant ||= Boolean(dynamic) || ["git", "gh"].includes(program)
    uncertain ||= Boolean(dynamic) || normalized.uncertain
    if (SHELLS.has(program)) {
      let index = 1
      let optionUncertain = false
      let commandString = false
      while (/^[-+]./.test(values[index] ?? "")) {
        const word = words[index]!
        const option = values[index++]!
        optionUncertain ||= word.dynamic || word.glob === true || word.syntax === true
        if (option === "--") break
        if (/^(?:[-+][oO]|--rcfile|--init-file)(?:=|$)/.test(option)) {
          if (option.includes("=")) optionUncertain ||= option.endsWith("=")
          else {
            const argument = words[index++]
            optionUncertain ||= !argument || argument.dynamic || argument.glob === true || argument.syntax === true
            optionUncertain ||= /^[-+][oO]$/.test(option) && !/^[A-Za-z0-9_]+$/.test(argument?.value ?? "")
          }
        } else if (/^-[euvxnc]+$/.test(option)) {
          commandString ||= option.includes("c")
        } else optionUncertain = true
        // Options after -c are ambiguous across shells; never consume the command as an option operand.
        if (/^-[a-z]*c$/.test(option) || commandString) {
          commandString = true
          break
        }
      }
      const source = words[index]
      if (
        commandString &&
        (!optionUncertain || (!stdinFed && index === 2 && /^-[a-z]*c$/.test(values[1] ?? ""))) &&
        source &&
        !source.value.startsWith("-") &&
        !source.dynamic &&
        !normalized.uncertain &&
        !/[{}]/.test(source.value)
      ) {
        const candidate = classifyShellCommand(source.value, mode, depth + 1, stdinFed, functions, fedBodies, "code")
        changesDirectory ||= candidate.changesDirectory === true
        relevant ||= candidate.kind !== "other"
        if (SEVERITY[candidate.kind] > SEVERITY[winner.kind]) winner = { ...candidate, command }
      } else {
        uncertain = true
        // A skipped -c body is unjudged executable code, regardless of its words or stdin source.
        if (commandString) relevant = unparsedBody = true
        else
          relevant ||=
            mentionsGit(words.map((word) => word.value).join(" ")) ||
            (stdinFed && (optionUncertain || !hasScriptOperand(source))) ||
            words.some((word) => /[$`]/.test(word.value) && !word.substitutions?.length)
      }
      continue
    }
    if (
      stdinFed &&
      !NO_STDIN.has(program) &&
      !(["source", "."].includes(program) && hasScriptOperand(words[1])) &&
      !functions.has(sourceName) &&
      (!stdinDataAllowed(words) ||
        values
          .slice(1)
          .some((value) => value !== "." && (SHELLS.has(programIdentity(value)) || OPAQUE.has(programIdentity(value)))))
    ) {
      relevant = true
      uncertain = true
      unparsedBody ||= ["read", "mapfile", "readarray", "ssh", "patch"].includes(program)
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
    // Option command channels use the same recursive classifier as positional remote commands and sh -c.
    let commandChannel = false
    if (["ssh", "scp", "sftp", "rsync"].includes(program)) {
      const args = words.slice(1).map((word): GrammarWord => ({
        raw: word.value,
        value: word.value,
        literal: !word.dynamic && !word.syntax,
        bare: !word.quoted,
        substitution: false,
        special: false,
        glob: word.glob === true,
        optionCapable: word.dynamic || word.glob === true,
        lead: undefined,
      }))
      const optionArgs = program === "ssh" ? args.slice(0, sshCommandIndex(values.slice(1))) : args
      const rejected = optionsRejected(EXEC_OPTION_SPECS[program]!, optionArgs, (body) => {
        commandChannel = true
        const candidate = classifyShellCommand(body, mode, depth + 1, false, functions, [], "code")
        relevant ||= candidate.kind !== "other"
        executableBody ||= candidate.bodyHit
        if (SEVERITY[candidate.kind] > SEVERITY[winner.kind]) winner = { ...candidate, command }
        return true
      })
      if (rejected) {
        relevant = true
        uncertain = true
        unparsedBody = true
      }
      if (commandChannel && program !== "ssh") continue
    }
    // ssh runs the words after its destination as one command line in the remote shell.
    if (program === "ssh") {
      const remote = words.slice(sshCommandIndex(values.slice(1)) + 1)
      if (remote.some((word) => word.dynamic || word.glob || word.syntax)) {
        relevant = true
        uncertain = true
      } else if (remote.length) {
        const candidate = classifyShellCommand(
          remote.map((word) => word.value).join(" "),
          mode,
          depth + 1,
          false,
          functions,
          [],
          "code",
        )
        relevant ||= candidate.kind !== "other"
        executableBody ||= candidate.bodyHit
        if (SEVERITY[candidate.kind] > SEVERITY[winner.kind]) winner = { ...candidate, command }
        // The remote command line is classified itself, as a shell `-c` body is; its words are not local Git text.
        continue
      }
      if (commandChannel) continue
    }
    if (DATA_COMMANDS.has(program)) {
      relevant ||= raw.some((word) => word.syntax && !quotedReadWord(word)) && mentionsGit(values.join(" "))
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
    if (execIndex > 0) {
      unparsedBody = true
      for (let index = execIndex; index < words.length; index += 1) {
        if (!["-exec", "-execdir"].includes(values[index]!)) continue
        executableBody ||= executableBodyRelevant(executableWords(words.slice(index + 1)).words[0])
      }
    }
    if (
      (normalized.uncertain ||
        OPAQUE.has(program) ||
        execIndex > 0 ||
        !["git", "gh", "find", "truncate"].includes(program)) &&
      !["docker", "npm", "uv", "twine", "rm"].includes(program)
    ) {
      // Uncertain wrappers cannot erase a literal destructive operation they may execute.
      for (let index = execIndex > 0 ? execIndex + 1 : 1; index < words.length; index += 1) {
        const nested = programIdentity(words[index]!.value.split(/\s+/).map((value) => ({ value }))).words
        const nestedProgram = nested[0]?.value ?? ""
        if (DATA_COMMANDS.has(nestedProgram)) break
        if (["rm", "npm", "uv", "twine", "docker"].includes(nestedProgram) && nested.length > 1) {
          const guarded = classifySegment(
            nested.map((word) => word.value),
            command,
            mode,
          )
          if (SEVERITY[guarded.kind] > SEVERITY[winner.kind]) winner = guarded
        }
        if (!["rm", "npm", "uv", "twine", "docker"].includes(nestedProgram)) continue
        const guarded = classifySegment(
          programIdentity(words.slice(index)).words.map((word) => word.value),
          command,
          mode,
        )
        if (SEVERITY[guarded.kind] > SEVERITY[winner.kind]) winner = guarded
        break
      }
    }
  }
  const weaker = SEVERITY[winner.kind] < SEVERITY["unparsed-git"]
  const classification =
    uncertain && relevant && weaker
      ? unparsed
      : outsideGrammar && weaker
        ? { kind: "unparsed-git" as const, command, reason: "shell syntax outside the modelled grammar" }
        : winner
  // Preserve fail-closed wrapper verdicts in bodies; uncatalogued launchers remain outside the body grammar.
  return {
    ...classification,
    changesDirectory,
    bodyHit:
      executableBody ||
      winner.kind !== "other" ||
      (classification.kind === "unparsed-git" && (unparsedBody || outsideGrammar)),
  }
}

const STATUS_TIMEOUT_MS = 1_000
const STATUS_MAX_BYTES = 4_000_000
const MAX_NAMED_PATHS = 10
const MAX_NAMED_PATH_CHARS = 120
const MAX_NAMED_TOTAL_CHARS = 600

/**
 * List working-tree paths with uncommitted changes. Bounded and failure-safe:
 * `undefined` means the escalation message stays unchanged instead of the
 * permission decision waiting on or failing with a broken repository lookup.
 * `ignored` holds plugin-owned relative paths, which are never someone else's
 * work.
 */
export function dirtyPaths(cwd: string, ignored: readonly string[] = []): string[] | undefined {
  const result = runGit(
    cwd,
    ["-c", `core.excludesFile=${devNull}`, "status", "--porcelain=v1", "-z", "--untracked-files=all"],
    {
      timeout: STATUS_TIMEOUT_MS,
      maxBuffer: STATUS_MAX_BYTES,
    },
  )
  if (result.error || result.status !== 0) return undefined
  const paths = parseStatusPaths(result.stdout.toString("utf8"))
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
