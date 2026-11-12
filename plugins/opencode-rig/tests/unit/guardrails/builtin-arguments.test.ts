/** Arguments of allowed builtins that zsh evaluates as arithmetic must be plain numbers. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

/** Wrap a command as a saved script that the same command line then runs with sh. */
const savedThenSh = (body: string) => `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`

// zsh evaluates exit statuses, printf numeric and width arguments, and the test -t descriptor as arithmetic;
// a subscript on a set parameter there runs its command substitution.
describe("evaluated builtin arguments ask unless they are plain numbers", () => {
  it.each([
    "exit 'HOME[$(gi$9t push)]'",
    'exit "$status"',
    "exit HOME",
    "exit 1 'a[1]'",
    "printf %d 'HOME[$(gi$9t push)]'",
    "printf '%x' 'a[1]'",
    "printf '%e' x",
    "printf '%d' \"$n\"",
    "printf '%*s' 'path[$(gi$9t push)]' x",
    "printf '%.*s' \"$w\" x",
    "printf '%s %d' a 'b[1]'",
    "printf '%s %d' a b",
    "printf '%1$d' x",
    'printf "$fmt" 1',
    "TZ='HOME[$(gi$9t push)]' printf %d \"$TZ\"",
    "test -t 'argv[$(gi$9t push)]'",
    "[ -t 'HOME[$(gi$9t push)]' ]",
    "[ -t HOME ]",
    'test -t "$fd"',
    "[ 'a[1]' = x ]",
    "test x = 'y[1]'",
  ])("asks for %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(savedThenSh(command)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "exit",
    "exit 0",
    "exit -1",
    "printf '%d\\n' 5 6",
    "printf '%x' 255",
    "printf '%.2f' 1.5",
    "printf '%5.2f' 3.14159",
    "printf '%*d' 4 2",
    "printf '%s: %d\\n' \"$name\" 3",
    "printf '%s\\n' 'HOME[$(gi$9t push)]'",
    "printf '%s %c' 'a[1]' 'HOME[$(gi$9t push)]'",
    "printf '%%d %s\\n' 'a[1]'",
    "printf 'done\\n'",
    "test -t 0",
    "[ -t 1 ]",
    "echo 'HOME[$(gi$9t push)]'",
    'echo "x[1]"',
    "cd +'HOME[$(gi$9t push)]'",
    "true 'HOME[$(gi$9t push)]'",
  ])("keeps data and plain numbers quiet: %s", (command) => {
    const result = classifyCommand(command)
    expect(result.kind).toBe("other")
    expect(decide(result, "standard", GRANTED)).toBeUndefined()
  })
})

describe("printf options are read only before the format", () => {
  it.each(["printf -v x %s 1", "printf -vx %s 1", "printf '-v' x %s 1", "printf $opt %s 1", "printf -x %s 1"])(
    "asks for %s",
    (command) => {
      expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    },
  )

  it.each(["printf '%s' -v", "printf -- '-v' x", "printf %s -vfoo x", "printf -- %s -v"])(
    "keeps %s quiet",
    (command) => {
      expect(classifyCommand(command).kind).toBe("other")
      expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
    },
  )
})
