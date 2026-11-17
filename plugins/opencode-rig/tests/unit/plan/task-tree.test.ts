/** The frozen plan's task tree is a bounded, depth-first decomposition; invalid trees fail closed. */
import { describe, expect, it } from "vitest"
import { MAX_TREE_LEAVES, planTreeProblems, planTreeState } from "../../../src/plan.ts"

const node = (over: Record<string, unknown> = {}) => ({
  id: "T-1",
  parent: null,
  goal: "Deliver the scoped change",
  owned_paths: ["src/a.ts"],
  acceptance_check: "focused test",
  depends_on: [],
  leaf: true,
  ...over,
})

function tree(nodes: unknown[], root = "T-1") {
  return { schema: 1, root, nodes }
}

function problems(nodes: unknown[], root = "T-1"): string[] {
  return planTreeProblems(tree(nodes, root) as any)
}

describe("task tree", () => {
  it("accepts a single leaf and a nested tree", () => {
    expect(problems([node()])).toEqual([])
    expect(
      problems([
        node({ leaf: false, split_reason: "not one verifiable unit", owned_paths: [] }),
        node({ id: "T-1.1", parent: "T-1", owned_paths: ["src/a.ts"] }),
        node({ id: "T-1.2", parent: "T-1", owned_paths: ["tests/a.test.ts"] }),
      ]),
    ).toEqual([])
  })

  it("ignores a fenced example and uses the last real section", () => {
    const example = ["## Task tree", "", "```json", "{}", "```"].join("\n")
    const markdown = [
      "```md",
      example,
      "```",
      "",
      "## Task tree",
      "",
      "```json",
      '{"schema":1,"root":"R","nodes":[{"id":"R","parent":null,"goal":"real","owned_paths":["src/real.ts"],"acceptance_check":"check","depends_on":[],"leaf":true}]}',
      "```",
    ].join("\n")
    expect(planTreeState(markdown).tree?.root).toBe("R")
    expect(planTreeState(["```md", example, "```"].join("\n")).problems).toContain(
      "plan has no parseable ## Task tree JSON section",
    )
  })

  it("rejects a non-leaf owned path and a non-boolean leaf", () => {
    expect(
      problems([
        node({ leaf: false, split_reason: "x", owned_paths: ["src/a.ts"] }),
        node({ id: "T-1.1", parent: "T-1" }),
      ]).join("; "),
    ).toMatch(/only a leaf may own paths/)
    expect(problems([node({ leaf: "false" as any })]).join("; ")).toMatch(/leaf must be a boolean/)
  })

  it("rejects a missing or unparsable tree", () => {
    expect(planTreeState("# Plan\n").problems).toContain("plan has no parseable ## Task tree JSON section")
    expect(planTreeState("## Task tree\n\n```json\n{not json}\n```\n").problems).toContain(
      "plan has no parseable ## Task tree JSON section",
    )
  })

  it.each([
    ["duplicate id", [node(), node()], /duplicate node id T-1/],
    ["missing root", [node({ id: "T-2" })], /root T-1 is not a node/],
    ["second root", [node(), node({ id: "T-2", owned_paths: ["src/b.ts"] })], /a second root/],
    ["leaf without a path", [node({ owned_paths: [] })], /a leaf needs an owned path/],
    ["leaf without a check", [node({ acceptance_check: "" })], /acceptance_check is empty/],
    ["non-leaf without split reason", [node({ leaf: false }), node({ id: "T-1.1", parent: "T-1" })], /split_reason/],
    ["non-leaf without children", [node({ leaf: false, split_reason: "x" })], /a non-leaf needs children/],
    [
      "sibling path overlap",
      [
        node({ leaf: false, split_reason: "x", owned_paths: [] }),
        node({ id: "T-1.1", parent: "T-1" }),
        node({ id: "T-1.2", parent: "T-1" }),
      ],
      /both own src\/a.ts/,
    ],
    ["missing dependency", [node({ depends_on: ["T-9"] })], /depends_on T-9 does not exist/],
    ["self dependency", [node({ depends_on: ["T-1"] })], /depends on itself/],
    [
      "depth cap",
      [
        node({ leaf: false, split_reason: "x", owned_paths: [] }),
        node({ id: "T-1.1", parent: "T-1", leaf: false, split_reason: "x", owned_paths: [] }),
        node({ id: "T-1.1.1", parent: "T-1.1", leaf: false, split_reason: "x", owned_paths: [] }),
        node({ id: "T-1.1.1.1", parent: "T-1.1.1", leaf: false, split_reason: "x", owned_paths: [] }),
        node({ id: "T-1.1.1.1.1", parent: "T-1.1.1.1", leaf: false, split_reason: "x", owned_paths: [] }),
        node({ id: "T-1.1.1.1.1.1", parent: "T-1.1.1.1.1" }),
      ],
      /depth exceeds 4/,
    ],
  ])("rejects %s", (_label, nodes, pattern) => {
    expect(problems(nodes).join("; ")).toMatch(pattern)
  })

  it("rejects a parent cycle and an absolute or escaping owned path", () => {
    expect(
      problems(
        [
          node({ id: "T-1", parent: "T-2", leaf: false, split_reason: "x", owned_paths: [] }),
          node({ id: "T-2", parent: "T-1", leaf: false, split_reason: "x", owned_paths: [] }),
        ],
        "T-1",
      ).join("; "),
    ).toMatch(/root T-1 must have no parent/)
    expect(problems([node({ owned_paths: ["/etc/passwd"] })]).join("; ")).toMatch(/project-relative/)
    expect(problems([node({ owned_paths: ["../outside"] })]).join("; ")).toMatch(/project-relative/)
  })

  it("caps the number of leaves", () => {
    const nodes = [node({ leaf: false, split_reason: "x", owned_paths: [] })]
    for (let index = 0; index <= MAX_TREE_LEAVES; index += 1)
      nodes.push(node({ id: `T-1.${index}`, parent: "T-1", owned_paths: [`src/${index}.ts`] }))
    expect(problems(nodes).join("; ")).toMatch(/leaves exceed/)
  })
})
