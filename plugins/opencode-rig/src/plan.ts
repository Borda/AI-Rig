/**
 * Task-tree parsing and validation for frozen plans.
 *
 * A plan carries a `## Task tree` JSON section: a depth-first decomposition
 * whose leaves are coherent, independently verifiable units. The tree is
 * validated when a plan is bound, so implementation can execute leaves without
 * re-deciding scope. This module reads text only; it never executes anything.
 */

export interface TaskNode {
  readonly id: string
  readonly parent: string | null
  readonly goal: string
  readonly owned_paths: readonly string[]
  readonly acceptance_check: string
  readonly depends_on: readonly string[]
  readonly leaf: boolean
  readonly split_reason?: string
}

export interface TaskTree {
  readonly schema: number
  readonly root: string
  readonly nodes: readonly TaskNode[]
}

export const PLAN_TREE_SCHEMA = 1
export const MAX_TREE_DEPTH = 4
export const MAX_TREE_LEAVES = 32

/**
 * Return the body of the last `## <title>` section outside a fenced block.
 * Fenced examples are documentation, never the authoritative section.
 */
export function markdownSection(markdown: string, title: string): string | undefined {
  const lines = markdown.split(/\r?\n/)
  const heading = new RegExp(`^##\\s+${title.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}\\s*$`, "i")
  const headings: number[] = []
  let fence: string | undefined
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]!
    const marker = /^\s*(`{3,}|~{3,})/.exec(line)
    if (marker) {
      if (!fence) fence = marker[1]![0]
      else if (marker[1]!.startsWith(fence)) fence = undefined
      continue
    }
    if (!fence && heading.test(line)) headings.push(index)
  }
  const start = headings.at(-1)
  if (start === undefined) return undefined
  const next = headings.find((index) => index > start)
  return lines.slice(start + 1, next).join("\n")
}

/** Extract the first JSON fence from a section body. */
function fencedJson(body: string): string | undefined {
  const lines = body.split(/\r?\n/)
  let fence: string | undefined
  let open = -1
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index]!
    if (open < 0) {
      const marker = /^\s*(`{3,}|~{3,})\s*json\s*$/i.exec(line)
      if (marker) {
        fence = marker[1]![0]
        open = index + 1
      }
      continue
    }
    if (new RegExp(`^\\s*${fence}{3,}\\s*$`).test(line)) return lines.slice(open, index).join("\n")
  }
  return undefined
}

/** Parse the task tree, returning undefined when the plan has no authoritative tree section. */
export function parseTaskTree(markdown: string): TaskTree | undefined {
  const section = markdownSection(markdown, "Task tree")
  const block = section === undefined ? undefined : fencedJson(section)
  if (block === undefined) return undefined
  try {
    return JSON.parse(block) as TaskTree
  } catch {
    return undefined
  }
}

/** Validate a tree and return every problem; an empty list means valid. */
export function planTreeProblems(tree: TaskTree | undefined): string[] {
  if (!tree) return ["plan has no parseable ## Task tree JSON section"]
  const problems: string[] = []
  if (tree.schema !== PLAN_TREE_SCHEMA) problems.push(`task tree schema must be ${PLAN_TREE_SCHEMA}`)
  if (!Array.isArray(tree.nodes) || tree.nodes.length === 0) return [...problems, "task tree has no nodes"]

  const ids = new Map<string, TaskNode>()
  for (const node of tree.nodes) {
    if (!node || typeof node.id !== "string" || !node.id.trim()) problems.push("a node has no id")
    else if (ids.has(node.id)) problems.push(`duplicate node id ${node.id}`)
    else ids.set(node.id, node)
  }
  if (ids.size !== tree.nodes.length) return problems

  if (!ids.has(tree.root)) problems.push(`root ${tree.root} is not a node`)
  else if (ids.get(tree.root)?.parent !== null) problems.push(`root ${tree.root} must have no parent`)
  const children = new Map<string, string[]>()
  for (const node of ids.values()) {
    if (node.parent === null) {
      if (node.id !== tree.root) problems.push(`${node.id}: a second root`)
    } else if (!ids.has(node.parent)) problems.push(`${node.id}: parent ${node.parent} does not exist`)
    else children.set(node.parent, [...(children.get(node.parent) ?? []), node.id])
  }
  for (const node of ids.values()) {
    if (!node.goal?.trim()) problems.push(`${node.id}: goal is empty`)
    if (!node.acceptance_check?.trim()) problems.push(`${node.id}: acceptance_check is empty`)
    if (!Array.isArray(node.owned_paths)) problems.push(`${node.id}: owned_paths must be an array`)
    if (!Array.isArray(node.depends_on)) problems.push(`${node.id}: depends_on must be an array`)
    if (typeof node.leaf !== "boolean") problems.push(`${node.id}: leaf must be a boolean`)
    if (!node.leaf && !node.split_reason?.trim()) problems.push(`${node.id}: split_reason is required for a non-leaf`)
    if (node.leaf) {
      if (!node.owned_paths?.length) problems.push(`${node.id}: a leaf needs an owned path`)
      if (children.get(node.id)?.length) problems.push(`${node.id}: a leaf cannot have children`)
    } else if (!children.get(node.id)?.length) problems.push(`${node.id}: a non-leaf needs children`)
    if (!node.leaf && node.owned_paths?.length) problems.push(`${node.id}: only a leaf may own paths`)
    for (const owned of node.owned_paths ?? []) {
      if (typeof owned !== "string" || !owned.trim()) problems.push(`${node.id}: owned path is empty`)
      else if (
        owned.startsWith("/") ||
        /^[A-Za-z]:[\\/]/.test(owned) ||
        owned.startsWith("~") ||
        owned.split(/[\\/]/).includes("..")
      )
        problems.push(`${node.id}: owned path must be project-relative: ${owned}`)
    }
    for (const dependency of node.depends_on ?? []) {
      if (dependency === node.id) problems.push(`${node.id}: depends on itself`)
      else if (!ids.has(dependency)) problems.push(`${node.id}: depends_on ${dependency} does not exist`)
    }
    const pending = [...(node.depends_on ?? [])]
    const visited = new Set<string>()
    while (pending.length) {
      const dependency = pending.pop()!
      if (dependency === node.id) {
        problems.push(`${node.id}: dependency cycle`)
        break
      }
      if (visited.has(dependency)) continue
      visited.add(dependency)
      pending.push(...(ids.get(dependency)?.depends_on ?? []))
    }
  }

  // Sibling leaves may not own the same path; a shared path is a depends_on edge.
  for (const siblings of children.values()) {
    const owners = new Map<string, string>()
    for (const id of siblings) {
      for (const path of ids.get(id)?.owned_paths ?? []) {
        const other = owners.get(path)
        if (other) problems.push(`${id} and ${other} both own ${path}; use depends_on`)
        else owners.set(path, id)
      }
    }
  }

  // Tree shape: one parent chain to the root, no cycles, bounded depth and leaves.
  let leaves = 0
  for (const node of ids.values()) {
    const seen = new Set<string>()
    let current: TaskNode | undefined = node
    let depth = 0
    while (current?.parent) {
      if (seen.has(current.id)) {
        problems.push(`${node.id}: cycle in parent chain`)
        break
      }
      seen.add(current.id)
      current = ids.get(current.parent)
      depth += 1
      if (depth > MAX_TREE_DEPTH) {
        problems.push(`${node.id}: depth exceeds ${MAX_TREE_DEPTH}`)
        break
      }
    }
    if (node.leaf) leaves += 1
  }
  if (leaves > MAX_TREE_LEAVES) problems.push(`${leaves} leaves exceed ${MAX_TREE_LEAVES}`)
  return problems
}

/** Read a plan file's tree and problems in one call. */
export function planTreeState(markdown: string): { tree?: TaskTree; problems: string[] } {
  const tree = parseTaskTree(markdown)
  return { ...(tree ? { tree } : {}), problems: planTreeProblems(tree) }
}
