import { describe, expect, it } from "vitest"
import { redact, redactValue } from "../../../src/journal.ts"

// Provider-shaped placeholders are joined at runtime: a contiguous literal is flagged by secret scanners and blocks pushes.
const joined = (separator: string, ...parts: string[]) => parts.join(separator)
const PROJECT_KEY = joined("-", "sk", "proj", "abcdefghijklmnopqrstuv", "0123456789")

describe("journal secret redaction", () => {
  it.each([
    joined("-", "sk", "ant", "api03", "abcdefghij", "klmnopqrstuv"),
    PROJECT_KEY,
    joined("-", "xoxb", "1234567890", "1234567890", "abcdefghijklmnopqrstuv"),
    joined("_", "npm", "abcdefghijklmnopqrstuv1234567890"),
    joined("-", "glpat", "abcdefghijklmnopqrstuv"),
    "Basic dXNlcjpzZWNyZXQ=",
    '"token": "short-value"',
    "'password': 'another-secret'",
    'api_key = "a value with spaces"',
    "-----BEGIN PRIVATE KEY-----\nABCDEF\n012345\n-----END PRIVATE KEY-----",
    "-----BEGIN RSA PRIVATE KEY-----\nABCDEF\n012345\n-----END RSA PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----\nABCDEF\n012345\n-----END EC PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nABCDEF\n012345\n-----END OPENSSH PRIVATE KEY-----",
  ])("removes credentials from text: %s", (secret) => {
    const output = redact(`prefix ${secret} suffix`)
    expect(output).toContain("[redacted]")
    expect(output).not.toContain(secret)
    expect(output).not.toContain("a value with spaces")
    expect(output).not.toContain("ABCDEF")
  })

  it("redacts sensitive keys recursively regardless of value shape", () => {
    expect(
      redactValue({
        password: "short",
        nested: [{ apiKey: { value: "nested secret" }, access_token: "small", label: "useful" }],
        Authorization: "Basic abc",
        clientSecret: ["secret"],
      }),
    ).toEqual({
      password: "[redacted]",
      nested: [{ apiKey: "[redacted]", access_token: "[redacted]", label: "useful" }],
      Authorization: "[redacted]",
      clientSecret: "[redacted]",
    })
  })

  it("redacts credentials embedded in object property names", () => {
    expect(JSON.stringify(redactValue({ [PROJECT_KEY]: "value" }))).not.toContain(PROJECT_KEY)
  })

  it("redacts provider-prefixed token keys while retaining token configuration", () => {
    expect(redactValue({ github_token: "short-secret", GITHUB_TOKEN: "another", max_tokens: 200 })).toEqual({
      github_token: "[redacted]",
      GITHUB_TOKEN: "[redacted]",
      max_tokens: 200,
    })
  })

  it("preserves nonsensitive diagnostic context", () => {
    expect(redactValue({ command: "git status", token_count: 12, nested: [true, null] })).toEqual({
      command: "git status",
      token_count: 12,
      nested: [true, null],
    })
  })
})
