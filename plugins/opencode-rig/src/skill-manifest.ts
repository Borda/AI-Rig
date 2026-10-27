/**
 * Shipped skill inventory.
 *
 * Descriptions are the routing surface the model sees, so they state the
 * trigger and the NOT-for boundary instead of restating the workflow.
 */

export interface SkillDefinition {
  readonly id: string
  readonly name: string
  readonly description: string
}

export const SKILLS: readonly SkillDefinition[] = [
  {
    id: "rig-plan",
    name: "Rig Plan",
    description:
      "Produce a frozen implementation plan with owned paths, configured gates, risks, and acceptance criteria before code changes. Use for a multi-file or ambiguous change. NOT for a one-line edit or a read-only question.",
  },
  {
    id: "rig-investigate",
    name: "Rig Investigate",
    description:
      "Reproduce a failure and narrow it to an evidence-backed root cause before fixing. Use for bugs, CI failures, and unexplained behavior. NOT for implementing the fix.",
  },
  {
    id: "rig-implement",
    name: "Rig Implement",
    description:
      "Execute a frozen plan or bounded change, run configured project gates, and produce an evidence-backed commit handoff. Use after planning or for a clear scoped fix. NOT for review-only or investigation-only requests.",
  },
  {
    id: "rig-review",
    name: "Rig Review",
    description:
      "Review the current diff, a commit, or a path with independent read-only reviewers and a validated findings artifact. Use before merge or after a large change. NOT for applying fixes; use rig-remediate.",
  },
  {
    id: "rig-remediate",
    name: "Rig Remediate",
    description:
      "Verify and resolve review findings, account for every finding in a terminal bucket, and prove closure with gates and a re-review. Use after rig-review or with a findings artifact. NOT for an unreviewed rewrite.",
  },
  {
    id: "rig-challenge",
    name: "Rig Challenge",
    description:
      "Run a bounded adversarial convergence loop: independent challenger, validated findings, fixes, independent re-assessment, and a stop reason. Use for risky or security-sensitive changes. NOT for routine edits.",
  },
  {
    id: "rig-release",
    name: "Rig Release",
    description:
      "Assess release readiness: version baseline from HEAD, full configured gates, changelog and README synchronization, and packaged file verification. Use before tagging or publishing. NOT for performing the publication itself.",
  },
  {
    id: "rig-doctor",
    name: "Rig Doctor",
    description:
      "Check opencode-rig install health: plugin load, version synchronization, agent installation, Git grant, artifact root, and detected gates. Use when a workflow or tool is unavailable.",
  },
]
