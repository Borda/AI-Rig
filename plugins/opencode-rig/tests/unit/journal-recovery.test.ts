/** Journal redaction preserves diagnostic text and bounds secret scanning. */
import { expect, it } from "vitest"
import { redact, redactValue } from "../../src/journal.ts"

it.each([
  "src/task-runner-integration-tests.ts",
  "see risk-assessment-and-mitigation-plan.md",
  "Basic setup instructions follow",
  "the bearer of bad news",
  "Basic instructions follow",
  "basic dXNlcjpzZWNyZXQ=",
  "max_token=500",
  "token_count=12",
  "secretary=Alice",
  "path=src/client-secret-handler.ts",
])("preserves ordinary prose and paths: %s", (value) => {
  expect(redact(value)).toBe(value)
})

it("preserves ordinary object keys while redacting delimited credential keys", () => {
  expect(
    redactValue({
      max_token: 500,
      token_count: 12,
      secretary: "Alice",
      access_token: "x",
      client_secret: "x",
      "api-key": "x",
    }),
  ).toEqual({
    max_token: 500,
    token_count: 12,
    secretary: "Alice",
    access_token: "[redacted]",
    client_secret: "[redacted]",
    "api-key": "[redacted]",
  })
})

it.each([
  "Cookie: session=abcdef0123456789abcdef",
  "Set-Cookie: sid=abcdef0123456789; HttpOnly",
  "session=abcdef0123456789",
])("redacts session headers: %s", (value) => {
  expect(redact(value)).not.toContain("abcdef0123456789")
  expect(redact(value)).toContain("[redacted]")
})

it("redacts cookie and session objects", () => {
  expect(redactValue({ cookie: "private", "set-cookie": ["private"], session: { id: "private" } })).toEqual({
    cookie: "[redacted]",
    "set-cookie": "[redacted]",
    session: "[redacted]",
  })
})

it("retains all values when redacted keys collide with each other or existing keys", () => {
  expect(redactValue({ ["ghp_" + "a".repeat(24)]: 1, ["ghp_" + "b".repeat(24)]: 2, "[redacted]#2": 3 })).toEqual({
    "[redacted]": 1,
    "[redacted]#2": 2,
    "[redacted]#2#2": 3,
  })
})

it("bounds public redaction input before applying patterns", () => {
  const value = "ordinary prose ".repeat(1000)
  expect(redact(value).length).toBeLessThanOrEqual(2000)
  expect(redactValue(value)).toBe(redact(value))
})

it("redacts an incomplete private key when the bounded input cuts off its end", () => {
  const value = "-----BEGIN PRIVATE KEY-----\n" + "A".repeat(4000)
  expect(redactValue(value)).toBe("[redacted]")
})

it.each([
  "Authorization: Basic dXNlcjpzZWNyZXQ=",
  "Authorization: Bearer abc.def.ghi",
  "Cookie: sid=abcdef; secretcookie=123456789",
  "Set-Cookie: sid=abcdef; sessionid=123456789; HttpOnly",
])("removes every credential in a header: %s", (value) => {
  const result = redact(value)
  expect(result).not.toMatch(/dXNlcjpzZWNyZXQ|abc.def.ghi|abcdef|123456789/)
  expect(result).toContain("[redacted]")
})

it("redacts a provider token cut short at the input bound", () => {
  const value = "x".repeat(1989) + " ghp_" + "A".repeat(30)
  expect(redact(value)).toBe("x".repeat(1989) + " [redacted]")
})
