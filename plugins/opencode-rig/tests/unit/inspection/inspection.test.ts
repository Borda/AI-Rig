/** The inspection allow is a positive, stateless model: reads and configured verification only. */
import { resolve } from "node:path"
import { describe, expect, it } from "vitest"
import { inspectionSafe, inspectionSequence } from "../../../src/inspection.ts"

describe("inspection allow", () => {
  it.each([
    "git diff HEAD --stat",
    "git log --oneline -5",
    "git status --short",
    "git show HEAD:src/a.ts",
    "sed -n 1,20p src/a.ts",
    "grep -n pattern -A 40 src/a.ts",
    "find . -name '*.py'",
    "sort -u file.txt",
    "rg -n TODO src",
    "ls 2>/dev/null",
    "echo ====",
    "gh pr view 1 --json headRefOid",
    "cat src/a.ts",
    "python -m pytest tests -q",
    ".venv/bin/python -m pytest tests -q",
    "PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest tests -q",
    "uv run pytest tests -q",
    "uv run ruff check src",
    "npm test",
    "npm run check",
    "make test",
    "git config --get core.branch",
    "sed -n /a/p file.txt",
    "grep -e /a/p file.txt",
    'grep "foo bar" file.txt',
    "npm test -- --run",
    "npm run test -- --run",
    "uv run python -m pytest -q",
  ])("allows %s", (command) => {
    expect(inspectionSafe(command), command).toMatchObject({ ok: true })
  })

  it.each([
    "cat src/a.ts > out.txt",
    "sort -o out.txt src/a.ts",
    "sort --output=out.txt src/a.ts",
    "find . -fls out.txt",
    "find . -fprintf out.txt %p",
    "find . -exec cat {} ;",
    "git add src/a.ts",
    "git diff --output=out.txt",
    "bash script.sh",
    "python script.py",
    "python -c 'print(1)'",
    "node script.js",
    "awk '{print $1}' file",
    "npx vitest run",
    "npm run format",
    "npm run build",
    "make format",
    "rg --pre cat pattern",
    "sed -i s/a/b/ file",
    "echo hi > out.txt",
    "uv run python script.py",
    "/bin/cat src/a.ts",
    "./cat src/a.ts",
    "../bin/cat src/a.ts",
    "./pytest tests -q",
    "ls && ./evil",
    "cat src/a.ts; ./evil",
    "cat /etc/passwd",
    "cat ''/etc/passwd",
    'cat ""/etc/passwd',
    "cat ~/.ssh/id_rsa",
    "grep -f../secret x",
    "grep root <~/.ssh/id_rsa",
    "cat x <~/.ssh/id_rsa",
    "cat x<~/secret",
    "cat x</etc/passwd",
    "jq -f prog.jq data.json",
    "git config --get core.sshCommand",
    "jq -n env",
    "git config --list",
    "git config --get http.extraheader",
    "cat C:\\Users\\me\\.ssh\\id_rsa",
    'cat "$HOME/x"',
  ])("does not allow %s", (command) => {
    expect(inspectionSafe(command).ok, command).toBe(false)
  })

  it("is stateless and independent of the environment", () => {
    const before = inspectionSafe("git diff --stat")
    process.env.PYTEST_ADDOPTS = "-p evil"
    try {
      expect(inspectionSafe("git diff --stat")).toEqual(before)
    } finally {
      delete process.env.PYTEST_ADDOPTS
    }
  })
})

describe("inspection sequence", () => {
  it.each([
    "git status --porcelain && git diff --stat",
    "sed -n 1,20p src/a.ts | head -n 5",
    "echo ok && git status --porcelain",
  ])("allows a compound of proven commands: %s", (command) => {
    expect(inspectionSequence(command, "/repo", "standard", () => true)).toMatchObject({
      ok: true,
      kind: "inspection",
      directory: "/repo",
    })
  })

  it("moves the effective directory on a literal cd and marks verification", () => {
    expect(
      inspectionSequence("echo ok && cd /tmp/opencode/review-x && pytest -q", "/repo", "standard", () => true),
    ).toMatchObject({
      ok: true,
      kind: "verification",
      directory: resolve("/tmp/opencode/review-x"),
    })
  })

  it.each(["ls && ./evil", "cd relative && pytest -q", "cd /tmp/x &&", "git status && ./pytest"])(
    "refuses %s",
    (command) => {
      expect(inspectionSequence(command, "/repo", "standard", () => true).ok).toBe(false)
    },
  )
})
