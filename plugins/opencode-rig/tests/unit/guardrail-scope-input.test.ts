/** Stdin execution follows substitution hosts, compound bodies, and local function calls. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("execution scope input", () => {
  it.each([
    "cat <<'EOF' | echo \"$(sh)\"\ngit push\nEOF\n",
    "echo \"$(sh)\" <<'EOF'\ngit push\nEOF\n",
    "echo `sh` <<'EOF'\ngit push\nEOF\n",
    "cat <<'EOF' | echo >\"$(sh)\"\ngit push\nEOF\n",
    "if true; then sh; fi <<'EOF'\ngit push\nEOF\n",
    "if false; then :; else sh; fi <<'EOF'\ngit push\nEOF\n",
    "if false; then :; elif sh; then :; fi <<'EOF'\ngit push\nEOF\n",
    "while true; do sh; done <<'EOF'\ngit push\nEOF\n",
    "until false; do sh; done <<'EOF'\ngit push\nEOF\n",
    "for item in x; do sh; done <<'EOF'\ngit push\nEOF\n",
    "select item in x; do sh; done <<'EOF'\ngit push\nEOF\n",
    "case x in x) sh;; esac <<'EOF'\ngit push\nEOF\n",
    "case x in (x) sh;; esac <<'EOF'\ngit push\nEOF\n",
    "case x in x) :;& y) sh;; esac <<'EOF'\ngit push\nEOF\n",
    "case x in x) :;;& x) sh;; esac <<'EOF'\ngit push\nEOF\n",
    "if true\nthen\nsh\nfi <<'EOF'\ngit push\nEOF\n",
    "f() { sh; }; f <<'EOF'\ngit push\nEOF\n",
    "F() { sh; }; F <<'EOF'\ngit push\nEOF\n",
    "f() { sh; }; F() { echo ready; }; f <<'EOF'\ngit push\nEOF\n",
    "function f { sh; }; f <<'EOF'\ngit push\nEOF\n",
    "function f\n{\nsh\n}\nf <<'EOF'\ngit push\nEOF\n",
    "function f() { sh; }; f <<'EOF'\ngit push\nEOF\n",
    "f() (sh); f <<'EOF'\ngit push\nEOF\n",
    "f()\n{\nsh\n}\nf <<'EOF'\ngit push\nEOF\n",
    "f() { g; }; g() { sh; }; f <<'EOF'\ngit push\nEOF\n",
    "f() { sh; }; f <<'EOF'\ngit push\nEOF\nf() { echo ready; }\n",
    "f() { echo \"$(sh)\"; }; f <<'EOF'\ngit push\nEOF\n",
    "cat <<'EOF' | if true; then sh; fi\ngit push\nEOF\n",
    "cat <<'EOF' | while true; do sh; done\ngit push\nEOF\n",
    "cat <<'EOF' | for item in x; do sh; done\ngit push\nEOF\n",
    "cat <<'EOF' | case x in x) sh;; esac\ngit push\nEOF\n",
    "f() { sh; }; cat <<'EOF' | f\ngit push\nEOF\n",
    "cat <<'EOF' | if true; then echo \"$(sh)\"; fi\ngit push\nEOF\n",
    "if true; then while true; do sh; done; fi <<'EOF'\ngit push\nEOF\n",
    'echo "$(sh)" <<< safe',
    "cat <<EOF | echo safe\n$(sh)\nEOF\n",
  ])("asks when a scope executes its input: %s", (command) => {
    const classification = classifyCommand(command)
    expect(classification.kind).toBe("unparsed-git")
    expect(decide(classification, "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "echo \"$(sh)\"; cat <<'EOF'\ngit push\nEOF\n",
    "cat <<'EOF' | cat; echo \"$(sh)\"\ngit push\nEOF\n",
    "cat <<'EOF' | cat; echo >\"$(sh)\"\ngit push\nEOF\n",
    "if true; then echo ready; fi <<'EOF'\ngit push\nEOF\n",
    "while true; do bash task.sh; done <<'EOF'\ngit push\nEOF\n",
    "case x in x) cat;; esac <<'EOF'\ngit push\nEOF\n",
    "f() { sh; }; cat <<'EOF' | cat; f\ngit push\nEOF\n",
    "f() { bash task.sh; }; f <<'EOF'\ngit push\nEOF\n",
    "f() { f; }; f <<'EOF'\ngit push\nEOF\n",
    "if true; then cat; fi <<'EOF'\ngit push\nEOF\necho \"$(sh)\"",
    "cat <<'EOF' | if true; then cat; fi; echo \"$(sh)\"\ngit push\nEOF\n",
  ])("keeps separate input scopes and inert data unguarded: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("other")
  })
})
