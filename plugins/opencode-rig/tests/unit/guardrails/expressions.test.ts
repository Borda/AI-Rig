/** Arithmetic and conditional expression syntax admits safe forms and hides no executable text. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

const consumers = ["BODY", "cat > x.sh <<'EOF'\nBODY\nEOF", "bash x.sh <<'EOF'\nBODY\nEOF"] as const
/** Saved data no same-text shell runs keeps its existing verdict; elsewhere the grammar gate applies. */
const SAVED_ONLY = "cat > x.sh <<'EOF'\nBODY\nEOF"

describe.each(consumers)("expression boundaries in %s", (consumer) => {
  it.each([
    "((git push) )",
    "((git push) ) && echo ok",
    "echo $((git push) )",
    'echo "$((git push) )"',
    "echo [[ ; git push ; echo ]]",
    "true [[ x\ngit push\necho ]]",
    "echo [[ && git push && echo ]]",
    "echo [[ | git push | echo ]]",
    "echo ((echo hi); git push; (echo hi))",
    "time -p [[ -f x ]]",
    "time -p (( n < 5 ))",
  ])("asks for executable text hidden by expression syntax: %s", (body) => {
    expect(decide(classifyCommand(consumer.replace("BODY", body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "(( n < 5 ))",
    "for ((i=0;i<3;i++)); do echo $i; done",
    "echo $(( 1 + 2 ))",
    "[[ -f x ]] && echo ok",
    "if [[ $a < $b ]]; then echo y; fi",
    "! [[ -f x ]]",
    "time [[ -d x ]]",
    "if true; then [[ -f x ]]; elif [[ -f y ]]; then echo y; fi",
    "while [[ -f x ]]; do (( n < 5 )); break; done",
    "until [[ -f x ]]; do echo waiting; break; done",
    "true && [[ -f x ]] || [[ -f y ]]",
    "{ [[ -f x ]]; }",
    "( [[ -f x ]] )",
  ])("admits genuine expressions only as saved data: %s", (body) => {
    // Grammar gate: arithmetic, [[ ]], loops, groups and time leave the grammar, so these expressions ask
    // wherever it applies.
    const result = classifyCommand(consumer.replace("BODY", body))
    if (consumer === SAVED_ONLY) {
      expect(result.kind).toBe("other")
      expect(decide(result, "standard", GRANTED)).toBeUndefined()
    } else expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })
})

// Probe sets in source order; `safe` marks the fixed positions of quiet controls, including saved inputs.
const probeSets = [
  [
    "((echo A); (git push))",
    "((git push); (echo B))",
    "echo $((echo A); (git push))",
    "x=$((echo A); (git push))",
    "true; [[ -n x # it's\n]]\ngit push\n: ' ]]'",
    '[[ -n x # "\n]]\ngit push\n: " ]]"',
    "cat > x.sh <<'EOF2'\n((echo A); (git push))\nEOF2",
    "bash <<'EOF2'\n((echo A); (git push))\nEOF2",
    "cat > x.sh <<'EOF2'\n[[ -n x # it's\n]]\ngit push\n: ' ]]'\nEOF2",
    "((echo A) ; (git push))",
    "((echo A)|(git push))",
    "((echo A)&&(git push))",
    "if ((echo A); (git push)); then :; fi",
    "((echo A)\n(git push))",
    "sudo --preserve-env=PATH make install",
    "flock -s /tmp/l make",
    "env - PATH=/bin make",
    "timeout -v 5 make",
    "setsid -w make",
  ],
  [
    "[[ -n x # it's\n]]\ngit push\n: \\' ]]",
    "true && [[ -n x # it's\n]]\ngit push\n: \\' ]]",
    "cat > x.sh <<'EOF2'\n[[ -n x # it's\n]]\ngit push\n: \\' ]]\nEOF2",
    "time -p [[ x ; git push ; echo ]]",
    "time -p [[ x && git push && echo ]]",
    "cat > x.sh <<'EOF2'\ntime -p [[ x ; git push ; echo ]]\nEOF2",
    "! time -p [[ x ; git push ; echo ]]",
    "time [[ x ; git push ; echo ]]",
    "cat > x.sh <<'EOF2'\n((echo A); (git push))\nEOF2",
    "diff <( ((echo A); (git push)) ) f",
    "cat > x.sh <<'EOF2'\necho $((echo A); (git push))\nEOF2",
    "((a) + (b))",
    "(( (a) + (b) ))",
    "((x = (1) ))",
  ],
  [
    "2>/dev/null [[ x ; git push ; echo ]]",
    "<<<x [[ x ; git push ; echo ]]",
    "true; 2>/dev/null [[ x ; git push ; echo ]]",
    "cat > x.sh <<'EOF2'\n2>/dev/null [[ x ; git push ; echo ]]\nEOF2",
    "2>/dev/null ((x ; git push ; y))",
    "2>/dev/null [[ -f x ]]",
    "time [[ -f x ]]",
  ],
] as const

const probes = probeSets.flatMap((set, setIndex) =>
  set.map((body, index) => ({
    body,
    safe: (setIndex === 0 && index >= 14) || (setIndex === 1 && index >= 12) || (setIndex === 2 && index === 6),
  })),
)

// `grammar` marks contexts where the grammar gate applies to the body: live text and shell-fed input.
const contexts = [
  { name: "top level", wrap: (body: string) => body, shellStdin: false, grammar: true },
  { name: "persisted", wrap: (body: string) => `cat > x.sh <<'EOF'\n${body}\nEOF`, shellStdin: false, grammar: false },
  // Launcher-gate control only: any `bash <<EOF` asks whatever the body, so this context proves nothing about the body.
  {
    name: "fed (launcher-gate control)",
    wrap: (body: string) => `bash <<'EOF'\n${body}\nEOF`,
    shellStdin: true,
    grammar: true,
  },
  // A script operand removes the blanket shell-stdin gate, testing body propagation itself.
  { name: "script fed", wrap: (body: string) => `bash x.sh <<'EOF'\n${body}\nEOF`, shellStdin: false, grammar: true },
] as const

/** Saved-only controls whose live expressions or project execution require confirmation. */
const GRAMMAR_COSTS = new Set([
  "sudo --preserve-env=PATH make install",
  "timeout -v 5 make",
  "setsid -w make",
  "env - PATH=/bin make",
  "(( (a) + (b) ))",
  "((x = (1) ))",
  "time [[ -f x ]]",
  "flock -s /tmp/l make",
])

describe.each(contexts)("expression probes at $name", ({ wrap, shellStdin, grammar }) => {
  it.each(probes)("classifies expression and project boundaries: $body", ({ body, safe }) => {
    const result = classifyCommand(wrap(body))
    if (safe && !shellStdin && !(grammar && GRAMMAR_COSTS.has(body))) {
      expect(result.kind).toBe("other")
      expect(decide(result, "standard", GRANTED)).toBeUndefined()
    } else expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe.each(contexts)("expression whitelist at $name", ({ wrap, shellStdin, grammar }) => {
  it.each(["sudo --preserve-env make install"])("asks for project execution where the grammar applies: %s", (body) => {
    const result = classifyCommand(wrap(body))
    if (shellStdin || grammar) expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
    else {
      expect(result.kind).toBe("other")
      expect(decide(result, "standard", GRANTED)).toBeUndefined()
    }
  })

  // Grammar gate: arithmetic, [[ ]], loops, if and read leave the grammar, so these forms ask wherever it
  // applies and stay quiet only as saved data.
  it.each([
    "(( n < 5 ))",
    "for ((i=0;i<3;i++)); do echo $i; done",
    "if (( $# < 1 )); then echo x; fi",
    "echo $(( 1 + 2 ))",
    "[[ -f x ]] && echo ok",
    'if [[ "$a" == "b" ]]; then echo y; fi',
    '[[ -n "$x" && $y = z ]] || echo no',
    'while read l; do echo "$l"; done < f',
    "(( (a) + (b) ))",
    "((x = (1) ))",
    "for ((;;)); do break; done",
    "echo $(( ${n} + $1 ))",
    '[[ ( -f x || -f y ) && "a;b" = "a;b" ]]',
  ])("admits safe syntax only as saved data: %s", (body) => {
    const result = classifyCommand(wrap(body))
    if (grammar) expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
    else {
      expect(result.kind).toBe("other")
      expect(decide(result, "standard", GRANTED)).toBeUndefined()
    }
  })

  it.each([
    "((a) + (b))",
    "(( n; n ))",
    "(( n & 1 ))",
    "(( n | 1 ))",
    "(( n\n+ 1 ))",
    '(( n + "1" ))',
    "(( n # comment ))",
    "for ((i=0;i<3;i++;i++)); do :; done",
    "for ((i=0;i<3)); do :; done",
    "[[ x # ' ]]",
    "[[ x\n= y ]]",
    "[[ x ; echo y ]]",
    "[[ x & echo y ]]",
    "[[ x | echo y ]]",
    "[[ $(echo x) = x ]]",
    '[[ "`echo x`" = x ]]',
    '[[ "x\ny" = z ]]',
    "[[ ( x = y ]]",
    "[[ x = y ]]suffix",
    "[[ x=y]]",
    "time -p [[ -f x ]]",
    "time -p (( n < 5 ))",
    "X=1 [[ -f x ]]",
    "2>/dev/null [[ -f x ]]",
    "2>/dev/null (( n < 5 ))",
    "sudo [[ -f x ]]",
  ])("asks outside the grammar even without a Git mention: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "sudo --preserve-env=PATH",
    "sudo --preserve-env",
    "flock -s /tmp/l",
    "env - PATH=/bin",
    "timeout -v 5",
    "setsid -w",
  ])("retains the guarded executable after %s", (prefix) => {
    expect(decide(classifyCommand(wrap(`${prefix} git push`)), "standard", GRANTED)?.effect).toBe("ask")
  })
})

const savedScript = (body: string) => `cat > x.sh <<'EOF'\n${body}\nEOF`
const CONTROL_CONTEXTS = [
  { name: "top level", wrap: (body: string) => body, grammar: true },
  { name: "saved", wrap: savedScript, grammar: false },
  { name: "saved then sh", wrap: (body: string) => `${savedScript(body)}\nsh x.sh`, grammar: true },
  { name: "saved then bash", wrap: (body: string) => `${savedScript(body)}\nbash x.sh`, grammar: true },
  { name: "script fed", wrap: (body: string) => `bash x.sh <<'EOF'\n${body}\nEOF`, grammar: true },
] as const

// Common [[ ]] and arithmetic idioms with the verdict each keeps as saved-only data; wherever the grammar
// gate applies (live text, saved scripts run in the same command, shell input) every one of them asks.
const EXPRESSION_CONTROLS = [
  ["[[ $x =~ ^[0-9]+$ ]] && echo num", "ask"],
  ["[[ -z ${x:-} ]] && echo empty", "ask"],
  ["[[ ${#a[@]} -gt 0 ]] && echo y", "ask"],
  ["(( ${#a[@]} > 0 )) && echo y", "other"],
  ["(( a[i]++ ))", "other"],
  ["[[ $f == *.py ]] && echo py", "other"],
  ["[[ $x == [abc]* ]] && echo y", "ask"],
  ["if true; then :; else (( n++ )); fi", "other"],
  ["[[ -n ${VAR-} ]] && echo y", "ask"],
  ['[[ $a != "$b" ]] && echo y', "other"],
  ['[[ -d $dir ]] || mkdir -p "$dir"', "other"],
  ['[[ "$1" == -* ]] && echo opt', "other"],
  ['[[ "$(uname)" == Darwin ]] && echo mac', "ask"],
  ["[[ $(uname) == Darwin ]] && echo mac", "ask"],
  ["x=$(( $(wc -l < f) + 1 ))", "other"],
  ["while (( i < 3 )); do i=$((i+1)); done", "other"],
  ["[[ -f x ]]; git push", "ask"],
  ["(( n < 5 )) && git push", "ask"],
  ["[[ -f a ]] && git push", "ask"],
  ["if [[ -f a ]]; then git push; fi", "ask"],
  ['[[ $x == "#" ]] && echo hash', "ask"],
  ["[[ -n $x && -z $y ]] && echo both", "other"],
  ["(( i % 2 == 0 )) && echo even", "other"],
  ["echo $(( RANDOM % 10 ))", "other"],
  ["[[ $x =~ foo ]] && echo y", "other"],
  ["[[ ${x} == y ]] && echo y", "other"],
  ["[[ -e ~/.bashrc ]] && echo y", "other"],
  ["[[ $OSTYPE == darwin* ]] && echo mac", "other"],
  ["(( $# > 0 )) || exit 1", "other"],
  ["[[ -t 1 ]] && echo tty", "other"],
] as const

describe.each(CONTROL_CONTEXTS)("expression idioms at $name", ({ wrap, grammar }) => {
  it.each(EXPRESSION_CONTROLS)(
    "classifies %s as %s when saved only, and asks where the grammar applies",
    (body, saved) => {
      const result = classifyCommand(wrap(body))
      if (saved === "ask" || grammar) expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
      else {
        expect(result.kind).toBe("other")
        expect(decide(result, "standard", GRANTED)).toBeUndefined()
      }
    },
  )
})
