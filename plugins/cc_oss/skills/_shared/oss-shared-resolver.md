# OSS Shared Dir Resolver

Pointer only, no bash to execute. Canonical resolver: bin script `bin/resolve_shared_path.py`. Consumers set `$_OSS_SHARED` invoking it directly:

```bash
PLUGIN_ROOT=$(python "${CLAUDE_PLUGIN_ROOT}/bin/resolve_plugin_root.py" --plugin-name oss) || exit $?; export PLUGIN_ROOT
_OSS_SHARED=$(python "$PLUGIN_ROOT/bin/resolve_shared_path.py" oss skills/_shared 2>/dev/null)
```

Tiered cascade: env root, registry, cache semver, source-tree fallback. Supersedes old `ls | sort -V | tail -1` snippet.
