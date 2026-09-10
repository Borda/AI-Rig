<!-- file: codemap-py-integration.md — consumers: codemap-py `integrate apply` (managed-block target only) -->

# Codemap-py integration

> This file is the consumer-owned host for the `codemap-py.integration.v2` managed-block body. Codex Rig ships it empty; a lead-run `codemap-py integrate apply` injects the `<!-- codemap-py:integration:begin v1 sha256=... -->` … `end` block below. Do not hand-author that block — see `codemap-contract.md` (this directory) for the protocol Codex Rig itself implements against the public CLI.

The managed block is integration metadata and versioned contract for consumer-owned adapter; it is not launcher wiring. It does not install `codemap-py`, add `CODEMAP_BIN` to process, or prove that Codex Rig can recognise launcher at runtime. `codemap-py integrate audit` validates managed source marker and compatibility metadata only. Runtime recognition must be demonstrated separately through adapter's `probe`/`context` path, including persisted `codemap-context.json` artifact and resolved launcher evidence.
