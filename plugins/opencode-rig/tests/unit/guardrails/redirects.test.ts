/** Persistent output redirects, including those kept alive through called functions. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("persistent exec redirects", () => {
  it.each(["exec", "exec --", "exec -a label", "command exec", "builtin exec"])(
    "tracks a commandless redirect through %s",
    (prefix) => {
      const command = `${prefix} >out.sh\ncat <<'EOF'\ngit push\nEOF`
      expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    },
  )

  it.each([">out.sh", ">>out.sh", ">&3", "&>out.sh"])("tracks later output after exec %s", (redirect) => {
    const command = `exec ${redirect}\necho ready\ncat <<'EOF'\ngit push\nEOF`
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  // Grammar gate: the exec builtin, subshells and function definitions are outside the grammar,
  // so these scope controls ask.
  it.each(["exec 3>&-", "exec 2>/dev/null"])("asks for prose after %s", (prefix) => {
    const command = `${prefix}\ncat <<'EOF'\nIt's a note\nEOF`
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "( exec >out.sh; true ); cat <<'EOF'\ngit push\nEOF",
    "(exec >out.sh); cat <<'EOF'\ngit push\nEOF",
    "f() { exec >out.sh; }; cat <<'EOF'\ngit push\nEOF",
  ])("asks for a scoped exec redirect outside the grammar: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it("propagates a function's exec redirect only when called", () => {
    const command = "f() { exec >out.sh; }; f; cat <<'EOF'\ngit push\nEOF"
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("called functions retain exec output redirects", () => {
  it.each([
    "f() { exec >out.sh; }; f; cat <<'EOF'\ngit push\nEOF",
    "f() { exec >out.sh; }; { f; }; cat <<'EOF'\ngit push\nEOF",
    "f() { exec >out.sh; }; g() { f; }; g; cat <<'EOF'\ngit push\nEOF",
    "F() { exec >out.sh; }; F; cat <<'EOF'\ngit push\nEOF",
    "g() { f; }; f() { exec >out.sh; }; g; cat <<'EOF'\ngit push\nEOF",
  ])("asks after a redirecting function runs: %s", (command) => {
    expect(classifyCommand(command).kind).toBe("unparsed-git")
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "f() { exec >out.sh; }; cat <<'EOF'\ngit push\nEOF",
    "f() { exec >out.sh; }; (f); cat <<'EOF'\ngit push\nEOF",
    "f() { (exec >out.sh); }; f; cat <<'EOF'\ngit push\nEOF",
    "f() { exec >out.sh; }; echo $(f); cat <<'EOF'\ngit push\nEOF",
    "f() (exec >out.sh); f; cat <<'EOF'\ngit push\nEOF",
  ])("asks for uncalled or isolated redirects outside the grammar gate: %s", (command) => {
    // function definitions, subshells and exec leave the modelled grammar.
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("only persistent stdout redirects retain fed commands", () => {
  it.each(["cat >&2", "cat >&1", "cat 2>errors.log"])(
    "admits fed Git text without persistent stdout: %s",
    (consumer) => {
      expect(classifyCommand(`${consumer} <<'EOF'\ngit push origin main\nEOF`).kind).toBe("other")
    },
  )

  // Grammar gate: the exec builtin is outside the grammar, so these ask.
  it.each(["exec 2>&1\ncat", "exec >&2\ncat", "exec 2>errors.log\ncat"])(
    "asks for fed Git text behind exec: %s",
    (consumer) => {
      expect(
        decide(classifyCommand(`${consumer} <<'EOF'\ngit push origin main\nEOF`), "standard", GRANTED)?.effect,
      ).toBe("ask")
    },
  )

  it.each([
    "cat >out.sh",
    "cat 1>>out.sh",
    "cat &>out.sh",
    "cat &>>out.sh",
    "cat >|out.sh",
    "cat >&3",
    "exec >out.sh\ncat",
  ])("asks when stdout persists through %s", (consumer) => {
    expect(decide(classifyCommand(`${consumer} <<'EOF'\ngit push\nEOF`), "standard", GRANTED)?.effect).toBe("ask")
  })
})
