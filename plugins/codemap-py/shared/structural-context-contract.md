<!-- file: structural-context-contract.md — consumers: plugins/codemap-py/claude-skills/_shared/codemap-context.md (Claude review, resolve and remediation wrappers read it through that contract), Codex Rig `shared/codemap_adapter.py` and `shared/codemap-contract.md` (public CLI surface only), tests/integration/test_structural_context_contract.py -->

# Structural context contract — v1

Runtime-neutral contract for `codemap-py context`, the one call a review, resolve or remediation workflow makes to get structural evidence for a change set at the head under review. Claude and Codex consumers read the same record and the same cause vocabulary; neither may invent a free-text reason for missing structural context.

## Call

```text
codemap-py context --root <worktree-at-head> --diff-file <change.diff> [--expect-head <sha>]
                   [--index-dir <base-outside-root>] [--no-build] [--timeout <seconds>] [--out <record.json>]
```

- `--root` is the checkout under review, normally a detached worktree at the pull-request head. The consumer creates it; codemap-py never checks anything out.
- `--expect-head` is the commit the review targets (full or abbreviated id). A different commit, a failed `git status`, or uncommitted indexed Python returns `head-mismatch` before index work. Modified, staged, deleted, renamed and untracked `.py`/`.pyi` paths count, including Git-ignored files that the scanner indexes. Scanner exclusions still apply.
- Omit `--expect-head` for a local working-tree or path review mirror. The provider analyzes the working tree as found and records `expected_head: null`. Codex verifies the mirror and requested diff around the query and records `source_snapshot_sha256` in its adapter artifact. For this route, the adapter's `expected_head` is the collector receipt's Git anchor. The snapshot digest identifies the reviewed overlay.
- `--index-dir` is the index base. The index, its coordination directory and the runtime logs go to a directory keyed by root, Git head and source mode. A base inside the root is a usage error (exit `2`), so the reviewed tree is never written. Default: `codemap-context/` in the host temporary directory.
- `--no-build`, or an inherited `SCAN_NO_AUTOBUILD=1`, forbids building and refreshing; an absent or outdated index then becomes the cause.
- `--timeout` bounds the build and, separately, the query (default `120` seconds).
- Exit `0` whenever a record was produced, whatever its status; exit `2` for invalid arguments with one JSON error line on stderr.

Building parses source with `ast.parse` only. The scanner never imports or executes the indexed code; the only programs it starts are `git rev-parse` and `git status` probes. Indexing a contributor's head is therefore reading, not running, their code, and needs no contributor-code gate.

## Record

Schema `codemap-py.structural-context`, `schema_version` `1`. Every field is always present.

| Field | Meaning |
| -- | -- |
| `status` | One value from the status table below. |
| `cause` | One slug from the cause table below when `status` is `unavailable` or `not-applicable`; `null` otherwise. |
| `detail` | Bounded one-line diagnostic for the cause; empty when there is none. |
| `root`, `diff_file`, `expected_head`, `head` | The verified inputs; `head` is the commit checked out at `root`, `null` when unreadable. |
| `changed_python_files` | Changed `.py` paths read from the diff, deleted and renamed-away modules included. |
| `index` | `{path, dir, action}` with `action` one of `built`, `refreshed`, `reused`, `none`; `null` before index work. |
| `method`, `confidence` | `static-ast` and `exact` describe the indexed source, which can be a working tree when `expected_head` is `null`. `text-search` and `low` describe the fallback. Both fields are `null` when neither ran. |
| `query_complete` | `true` only for a complete index answer; always `false` for the fallback. |
| `degraded_reasons` | Why a `degraded` index answer is incomplete, including deleted or renamed-away modules `diff-impact` does not analyse. |
| `diff_impact` | The provider's `diff-impact` answer verbatim; `null` when unavailable. |
| `fallback` | The text-search record below; `null` unless the cause is marked fallback in the cause table. |
| `fallback_error` | Why a fallback the cause calls for is absent (the walk itself failed); `null` otherwise. |

## Status

| Status | Meaning | Consumer action |
| -- | -- | -- |
| `available` | Fresh index at the verified head answered completely. | Use `diff_impact` as authoritative for the queried direction. |
| `degraded` | Fresh index answered, but `degraded_reasons` names a gap (unmapped file, incomplete direction). | Use the evidence and surface each reason as a caveat. |
| `unavailable` | The index could not answer; `cause` says why. | Report the cause slug; use `fallback` when present. |
| `not-applicable` | The change set has no structural question (`non-python-scope`). | Record no structural gap. |

## Causes

Closed set. A consumer reports structural context as missing only with one of these slugs.

| Cause | Meaning | Detected by | Fallback |
| -- | -- | -- | -- |
| `codemap-not-installed` | No `codemap-py` launcher is reachable. | consumer | no |
| `codemap-interpreter-unsupported` | The launcher exited `127`: no eligible CPython (set `CODEMAP_PYTHON`). | consumer | no |
| `context-request-invalid` | The command refused the request as written: exit `2` with `{"error": "invalid_context_request"}` on stderr (a consumer or version-skew defect, not a failed query). | consumer | no |
| `codemap-timeout` | The build or the query exceeded its deadline. | provider | yes |
| `index-missing` | No index at this head and building is disabled. | provider | yes |
| `index-stale` | The index records another commit than the verified head, or the answer reports it older than the source. | provider | yes |
| `index-root-mismatch` | The answer comes from an index of another root. | provider | yes |
| `index-busy` | Another writer held the index lease. | provider | yes |
| `index-build-failed` | The scanner failed or wrote no index. | provider | yes |
| `query-failed` | The `diff-impact` query failed or answered with an error. | provider | yes |
| `head-mismatch` | The root is not checked out at `--expect-head`, or its indexed Python differs from that commit (uncommitted or untracked changes, or an unreadable `git status`). | provider | no |
| `source-unavailable` | The root is not a readable directory. | provider | no |
| `diff-unavailable` | The diff file cannot be read. | provider | no |
| `non-python-scope` | The diff names no `.py` path; status is `not-applicable`. | provider | no |

- `codemap-*` and `index-*` causes are environment problems a rerun or provisioning closes; the others describe the request.
- A consumer records `codemap-not-installed`, `codemap-interpreter-unsupported` or `context-request-invalid` itself, in the same record shape, because codemap-py cannot start or writes no record to report them. Only a launcher without the `context` command (exit `2`, `unknown command 'context'`) selects a consumer's older query path; any other missing record is one of these causes or `query-failed`.
- A consumer that stops the command on its own outer deadline records `codemap-timeout`.
- `head-mismatch` has no fallback on purpose: searching the wrong checkout would describe code the review is not about. Check out the head and run again.

## Fallback

Present exactly when the cause row says `yes`. It searches the verified root at the head, line by line, under the scanner's own exclusions, in sorted file order, and never executes or imports the searched code. Identical inputs produce an identical record.

- `method` `text-search`, `confidence` `low`, `query_complete` `false`, always.
- `modules[]`: per changed module, `module`, `path`, `deleted`, `importer_files` (files importing it by absolute, relative or aliased import, or using its dotted path), `importer_file_count`, `importer_truncated`.
- `symbols[]`: per changed `def` or `class` name, `name`, `path`, `reference_files` (files spelling the name as a whole word), `reference_file_count`, `reference_truncated`.
- `files_scanned`, `files_skipped` (larger than 1 MiB or unreadable), `scan_truncated`, `modules_truncated`, `symbols_truncated`: every cap that applied is flagged.
- `not_covered[]`: what a text search cannot see (dynamic imports and dispatch, string references, continuation lines of a parenthesized import, re-exports, aliased names).

Hits are candidates, not resolved edges. Zero hits means unknown, never no impact: the fallback may raise review depth, never lower it. A consumer that tiers impact keeps the unknown-impact default for an `unavailable` record and cites the fallback's files as evidence for the reviewer.
