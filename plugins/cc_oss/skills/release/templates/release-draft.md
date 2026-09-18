# <version>: <release name>

## 📋 Summary

\<2–4 sentence para — what changed + why matters to users/devs>

## ✨ Spotlights / highlights

\<top 3–5 features or fixes, each with short code example>

## 🔄 Migration guide

\<Breaking change = worked before, fails/differs now, no prior warning or deprecation shim. API deprecated (with warning + forwarding) in prior release then removed → classify as ❌ Removed, not ⚠️ Breaking Changes. Guide migration; include before/after code for each breaking change. If none: "No migration required for this release.">

<!-- Use Draft migration guide content — do not regenerate independently. -->

## 📝 Notable changes

\<Significant changes, grouped by area/component; list all PRs/commits.>

### 🚀 Added

- **Feature Name** — what it does and why it matters. (#PR or commit)

### ⚠️ Breaking Changes

- **[Area]** — what changed and what callers must do to migrate. (#PR)

### 🌱 Changed

- Behaviour change: old behaviour → new behaviour. (#PR)

### 🗑️ Deprecated

- `OLD_NAME` deprecated in favour of `NEW_NAME`. (#PR)

### ❌ Removed

- `OLD_API` removed (deprecated since vX.Y). Migrate to `NEW_API`. (#PR)

### 🔧 Fixed

- Fixed what was broken when condition. (#PR)

## 🏆 Contributors

- **Name** (@github_handle, [LinkedIn](https://linkedin.com/in/handle)) — brief what they did

______________________________________________________________________

**Full changelog**: https://github.com/[org]/[repo]/compare/vPREV...vNEXT
