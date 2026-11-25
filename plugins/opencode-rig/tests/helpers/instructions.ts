/**
 * Negation-aware reads of instruction prose.
 *
 * A phrase-presence check passes for an inverted rule ("never link to the
 * original" still contains "link to the original"). These helpers bind an
 * assertion to the sentence that states the rule and pin the negating words
 * inside it, so adding a negation to an obligation or removing one from a
 * prohibition fails.
 */

import { expect } from "vitest"

/** A word that inverts the instruction it appears in. */
const NEGATION = /\b(?:not|never|no|none|without|cannot|neither|nor)\b|n't\b/gi

/** Leading list marker, including a lettered sub-step label such as `- h.`. */
const LIST_MARKER = /^\s*(?:[-*]|\d+\.)\s+(?:[a-z]\.\s+)?/

/** Split a prose line into sentences at a period followed by a new sentence. */
function sentences(line: string): string[] {
  return line.replace(LIST_MARKER, "").split(/(?<=\.)\s+(?=[A-Z`])/)
}

/** Inline code span: a literal command, flag or token, never prose polarity (`--no-tags`, `not-closed`). */
const CODE_SPAN = /`[^`]*`/g

/** List the negating words of a sentence's prose in reading order, lower-cased; inline code spans are skipped. */
function negations(sentence: string): string[] {
  return (sentence.replace(CODE_SPAN, "").match(NEGATION) ?? []).map((word) => word.toLowerCase())
}

/**
 * Return the sentence of `line` that opens with `opening`, after checking its negating words are exactly `negators`.
 *
 * Fails when the line is missing, when no sentence opens with `opening`, or when the sentence's negating words differ
 * from `negators` in number, order or wording. A prohibition lists its own negation (`["never"]`); an obligation lists
 * none.
 */
export function ruleSentence(line: string | undefined, opening: string, negators: readonly string[]): string {
  expect(line, `instruction line for: ${opening}`).toBeDefined()
  const sentence = sentences(line ?? "").find((candidate) => candidate.startsWith(opening))
  expect(sentence, `sentence opening with: ${opening}`).toBeDefined()
  expect(negations(sentence ?? ""), `negating words in: ${sentence}`).toEqual([...negators])
  return sentence ?? ""
}

/**
 * Return the sentence of `line` that opens with `opening`, after checking it is stated affirmatively.
 *
 * Fails when the line is missing, when no sentence opens with `opening`, or when that sentence contains a negating word.
 */
export function affirmedSentence(line: string | undefined, opening: string): string {
  return ruleSentence(line, opening, [])
}
