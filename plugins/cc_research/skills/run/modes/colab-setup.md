<!-- file: colab-setup.md — consumers: plugins/cc_research/skills/run/SKILL.md -->

Execute this section only when `--colab` flag is set. Skip entirely for local or docker runs.

**Purpose**: route metric verification and GPU code testing to Colab runtime instead of local. Essential for ML training metrics, CUDA benchmarks, GPU-required workloads.

**Hardware selection** (`--colab=HW`): specify GPU type (optional). Known: `H100`, `L4`, `T4`, `A100`. Omitted → Colab picks default. Advisory — actual hardware configured in notebook UI. Claude Code validates GPU identity at Phase 5 via `torch.cuda.get_device_name()` assertion; halts if mismatch.

<!-- policy-sibling: plugins/cc_research/skills/run/SKILL.md, plugins/cc_research/skills/plan/SKILL.md, plugins/cc_research/skills/sweep/SKILL.md, plugins/cc_research/skills/judge/SKILL.md — known-hardware set restated in each; keep in sync (plugins/CLAUDE.md §Policy Duplication Marker). -->

**Setup** (before running `--colab`):

1. Add `"colab-mcp"` to `enabledMcpjsonServers` in `settings.local.json`:
   ```json
   {
     "enabledMcpjsonServers": [
       "colab-mcp"
     ]
   }
   ```
2. Ensure `colab-mcp` server defined in `.mcp.json` under `mcpServers` (see project `.mcp.json`).
3. Open Colab notebook with runtime connected and execute MCP connection cell.

**How it works during a run:**

- Step R2 (preconditions): checks for `mcp__colab-mcp__runtime_execute_code` availability.
- Phase 5 (verify metric): calls `mcp__colab-mcp__runtime_execute_code` with `metric_cmd` instead of local `timeout <cmd>`.
- `VERIFY_TIMEOUT_SEC` = 300 (vs 120 local) to account for network + GPU startup latency.

If Colab MCP unavailable at R2, print:

```markdown
⚠ Colab MCP not available. To enable:
  1. Add "colab-mcp" to enabledMcpjsonServers in settings.local.json
  2. Open a Colab notebook and connect the runtime
  3. Execute the MCP connection cell in the notebook
Then re-run with --colab.
```
