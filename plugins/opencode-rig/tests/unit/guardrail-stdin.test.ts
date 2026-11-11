/** Executing consumers retain stdin provenance through shell nesting and grouping. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("stdin execution boundaries", () => {
  it.each([
    `printf '%s' 'git push' | bash -c 'eval "$(cat)"'`,
    `printf '%s' 'git push' | bash -c 'source /dev/stdin'`,
    `cat <<'EOF' | bash -c 'eval "$(cat)"'\ngit push\nEOF\n`,
    "source /dev/stdin <<'EOF'\ngit push\nEOF\n",
    `eval "$(cat)" <<'EOF'\ngit push\nEOF\n`,
    `bash -c 'source /dev/stdin' <<'EOF'\ngit push\nEOF\n`,
    "printf safe |& sh",
    "printf safe |\nsh",
    "printf safe | ! sh",
    "printf safe | { sh; }",
    "printf safe | { echo ready; sh; }",
    "printf safe | { { sh; }; }",
    "printf safe | (sh)",
    "printf safe | ( echo ready; sh )",
    "(sh) <<'EOF'\nsafe\nEOF\n",
    "printf safe | bash -c 'exec 3<&0; source /dev/fd/3'",
    "printf safe | bash -c 'exec 3<&0; source /proc/self/fd/3'",
    "{ sh; } <<'EOF'\nsafe\nEOF\n",
    "! { source /dev/stdin; } <<'EOF'\nsafe\nEOF\n",
    "{ echo ready; { eval cat; }; } <<< safe",
    "printf safe | bash *.sh",
    "printf safe | sh /dev/stdin",
    "printf safe | sh /dev/fd/0",
    "printf safe | sh /proc/self/fd/0",
    "printf safe | sh -",
    "printf safe | bash -c 'bash -c source\\ /dev/stdin'",
    "sh <<< safe",
    "printf safe | source /dev/stdin",
    "printf safe | source -- /dev/stdin",
    "printf safe | bash -c 'source -- /dev/stdin'",
    "printf safe | . /dev/stdin",
    "printf safe | eval cat",
    "printf safe | bash -c 'cat | sh'",
    `printf safe | bash -c 'eval "$(cat)"'`,
    "printf safe | bash -c 'sh < /dev/stdin'",
  ])("asks when stdin reaches executable text: %s", (command) => {
    const classification = classifyCommand(command)
    expect(classification.kind).toBe("unparsed-git")
    expect(decide(classification, "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    'echo "$(case x in x) :;; esac; g=gi; ${g}t push)"',
    'echo "$(# )\ng=gi; ${g}t push\n)"',
    "echo \"$(cat <<'EOF'\n)\nEOF\ng=gi; ${g}t push\n)\"",
    "echo \"$(printf %s $')'; g=gi; ${g}t push)\"",
    "echo \"$(printf %s $'\\')'; g=gi; ${g}t push)\"",
  ])("asks for substitutions outside the balanced scanner grammar: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
  })

  it.each([
    "bash task.sh | tee output.log",
    "printf safe | bash task.sh",
    "printf safe |& bash task.sh",
    "bash task.sh <<'EOF'\nsafe\nEOF\n",
    "printf safe | bash -c 'bash task.sh'",
    "printf safe | bash -c 'echo safe'",
    "printf safe | { bash task.sh; }",
    "printf safe | { echo safe; }; bash task.sh",
    "printf safe | cat; bash task.sh",
    "printf safe | source task.sh",
    "source task.sh <<'EOF'\nsafe\nEOF\n",
    "cat <<'EOF'\ngit push\nEOF\n",
    'echo "$(printf safe)"',
    "echo \"$(printf %s 'case # <<')\"",
    "grep -rl Git docs | xargs wc -l",
  ])("keeps ordinary scripts and inert data readable: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("other")
  })
})
