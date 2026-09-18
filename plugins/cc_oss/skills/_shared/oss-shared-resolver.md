# OSS Shared Dir Resolver

Pointer only, no bash to execute. Canonical resolver: bin script `bin/resolve_shared_path.py`. Consumers set `$_OSS_SHARED` invoking it directly:

```bash
_OSS_SHARED=$(python "${CLAUDE_PLUGIN_ROOT:-plugins/cc_oss}/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)
```

Tiered cascade: env root, registry, cache semver, source-tree fallback. Supersedes old `ls | sort -V | tail -1` snippet.
