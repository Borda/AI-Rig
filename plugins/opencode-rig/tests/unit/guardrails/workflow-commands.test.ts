/** The standard workflow forms stay quiet; their write, publish and execution edges still ask. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

function effect(command: string): string | undefined {
  return decide(classifyCommand(command), "standard", GRANTED)?.effect
}

describe("workflow commands", () => {
  it.each([
    "cd /private/tmp/opencode/review-pr563 && git diff 0ee62b3..59042e0 --stat",
    "cd /private/tmp/opencode/review-pr563 && .venv/bin/python -m pytest tests/tune/test_tuner.py -q 2>&1 | tail -20",
    "cd /tmp/checkout && python -m pytest tests -n 4",
    ".venv/bin/python -m pytest tests -q",
    ".venv/bin/python3 -m pytest tests -q",
    ".venv/Scripts/python.exe -m pytest tests -q",
    "node --check src/a.js",
    "ruff check src",
    "mypy src",
    "npm --prefix plugins/opencode-rig run check",
    "make test",
    "make lint",
    "make format",
    "make typecheck",
    "pre-commit run --files a.py b/c.py",
    "npm run format",
    "npm run build",
    "make format",
    "cd /tmp/checkout && pre-commit run --files a.py",
    "npm pack --dry-run",
    "npm run check",
    "rg -n TODO src",
  ])("keeps the standard form quiet: %s", (command) => {
    expect(effect(command)).toBeUndefined()
  })

  it.each([
    "make deploy",
    "make test extra",
    "pre-commit run",
    "pre-commit run --all-files",
    "pre-commit run --files ../outside.py",
    "pre-commit run --files /etc/passwd",
    "npm pack",
    "npm pack --pack-destination dist",
    "node --check /etc/passwd",
    "pre-commit run --files",
    "npm --prefix /tmp/other run check",
    "cd /tmp/checkout && pytest tests; rm -rf build",
    "pytest tests.py > /etc/passwd",
    "rg --pre cat pattern",
    "rg --pre=cat pattern",
    "rg --passthru --pre cat pattern",
    "git diff HEAD --outp=x",
    "npm pack --dry-run --json",
  ])("asks for a form outside the standard gate: %s", (command) => {
    expect(effect(command)).toBe("ask")
  })
})

describe("saved workflow scripts", () => {
  const saved = (body: string) => `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`

  it.each([
    "cd /tmp/checkout && pytest tests -q",
    "cd /tmp/checkout && .venv/bin/python -m pytest tests -q 2>&1 | tail -20",
    "make test",
    "pre-commit run --files a.py",
    "npm pack --dry-run",
  ])("keeps the standard form quiet when saved: %s", (body) => {
    expect(effect(saved(body))).toBeUndefined()
  })
})
