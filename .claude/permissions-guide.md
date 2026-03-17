# `.claude/settings.json` Permissions Reference

Annotated companion to the `permissions.allow` and `permissions.deny` lists in `settings.json`.
Every entry here must have a matching entry there, and vice versa — kept in sync by `/audit` (drift check) and `/manage add perm` / `/manage remove perm`.

**Remote/upstream operations and destructive git commands are explicitly denied** — see the Deny List section below. Deny rules are evaluated before allow rules; a matching deny always blocks execution regardless of any allow entry.

______________________________________________________________________

## Deny List — always blocked

| Permission              | Description                    | Why denied                                               |
| ----------------------- | ------------------------------ | -------------------------------------------------------- |
| `Bash(git push:*)`      | Push commits to a remote       | Remote mutations require explicit user action            |
| `Bash(git branch -D:*)` | Force-delete a local branch    | Destructive; may lose unmerged work                      |
| `Bash(git branch -d:*)` | Delete a merged local branch   | Requires explicit user intent                            |
| `Bash(git tag -d:*)`    | Delete a local tag             | Tags are release markers; user-only                      |
| `Bash(git remote:*)`    | Add, remove, or modify remotes | Remote config changes are user-owned                     |
| `Bash(git clean:*)`     | Discard untracked files        | Irreversible; untracked files are permanently deleted    |
| `Bash(git restore:*)`   | Discard working-tree changes   | Destructive; discards uncommitted edits without recovery |

______________________________________________________________________

## Web

| Permission  | Description                            | Typical use case                                                          |
| ----------- | -------------------------------------- | ------------------------------------------------------------------------- |
| `WebSearch` | Search the web for current information | Fetch current docs, CVE advisories, package release notes, ecosystem news |

______________________________________________________________________

## Shell utilities

| Permission             | Description                                      | Typical use case                                                                               |
| ---------------------- | ------------------------------------------------ | ---------------------------------------------------------------------------------------------- |
| `Bash(curl:*)`         | HTTP requests and file downloads                 | Hit a REST API, download a file, fetch raw URLs for link verification                          |
| `Bash(echo:*)`         | Print strings to stdout                          | Pipe content into another command, emit simple diagnostics                                     |
| `Bash(find:*)`         | Locate files by name, type, or modification time | Discover files matching a pattern across a directory tree                                      |
| `Bash(grep:*)`         | Search file content by regex pattern             | Filter command output, find usages across a codebase                                           |
| `Bash(head:*)`         | Read the first N lines of a file                 | Inspect file headers, preview log beginnings                                                   |
| `Bash(tail:*)`         | Read the last N lines of a file                  | Follow live logs with `-f`, inspect recent entries                                             |
| `Bash(ls:*)`           | List directory contents                          | Check file existence, inspect directory structure                                              |
| `Bash(wc:*)`           | Count lines, words, or bytes                     | Measure file count, line budget checks                                                         |
| `Bash(diff:*)`         | Compare two files line-by-line                   | Confirm patch outcome, spot drift between config files                                         |
| `Bash(cp:*)`           | Copy files                                       | `/sync` uses this to propagate config files to `~/.claude/`                                    |
| `Bash(mkdir:*)`        | Create directories                               | Ensure target paths exist before writing                                                       |
| `Bash(time:*)`         | Measure wall-clock execution time                | Establish baseline before an optimisation pass                                                 |
| `Bash(rsync:*)`        | Efficient file sync between directories          | `/sync` uses this to propagate config files; `--dry-run` for drift reports, no `--delete` ever |
| `Bash(sed:*)`          | Stream editor for text transformation            | Rewrite paths, strip comments, process file content in pipelines                               |
| `Bash(awk:*)`          | Column-oriented text processing                  | Extract fields, compute sums, reformat tabular output                                          |
| `Bash(cat:*)`          | Concatenate and print file contents              | Pipe multi-file content into a command; display small files                                    |
| `Bash(sort:*)`         | Sort lines of text                               | Deduplicate sorted output, produce ordered lists for diffing                                   |
| `Bash(uniq:*)`         | Filter adjacent duplicate lines                  | Count occurrences, collapse repeated log lines                                                 |
| `Bash(cut:*)`          | Extract fixed columns or delimited fields        | Pull specific CSV/TSV columns, trim output fields                                              |
| `Bash(tr:*)`           | Translate or delete characters                   | Normalise line endings, uppercase/lowercase transforms                                         |
| `Bash(xargs:*)`        | Build and execute commands from stdin            | Batch-apply a command to a list of files or arguments                                          |
| `Bash(tee:*)`          | Write stdin to stdout and a file simultaneously  | Capture command output while still piping it downstream                                        |
| `Bash(jq:*)`           | Query and transform JSON                         | Parse API responses, inspect settings.json, filter JSONL logs                                  |
| `Bash(date:*)`         | Print or format the current date/time            | Timestamp log entries, generate dated filenames                                                |
| `Bash(which:*)`        | Locate an executable on PATH                     | Verify a tool is installed before invoking it                                                  |
| `Bash(env:*)`          | Print or set environment variables               | Inspect current env, run a command with a modified environment                                 |
| `Bash(comm:*)`         | Compare two sorted files line by line            | `/audit` Check 1: diff on-disk agent/skill names against MEMORY.md roster                      |
| `Bash(mktemp:*)`       | Create a temporary file with a unique name       | `/sync` creates a temp settings.json before comparing with rsync --checksum                    |
| `Bash(touch:*)`        | Create a file or update its modification time    | `/audit` health monitoring: create per-agent checkpoint files for stall detection              |
| `Bash(printf:*)`       | Formatted output (supports escape sequences)     | Color-coded terminal output in audit and hook scripts                                          |
| `Bash(basename:*)`     | Strip directory and suffix from a file path      | Extract agent/skill names from full file paths in audit and manage scripts                     |
| `Bash(dirname:*)`      | Extract directory component from a file path     | Compute parent directory of a file path in shell pipelines                                     |
| `Bash(node --check:*)` | Validate Node.js script syntax without running   | `/audit upgrade` correctness check for hook JS files after applying config changes             |

______________________________________________________________________

## GitHub CLI — primarily read-only

| Permission                | Description                                    | Typical use case                                                          |
| ------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------- |
| `Bash(gh auth status:*)`  | Check GitHub CLI authentication state          | Pre-flight check in `/resolve` and any skill that requires `gh` auth      |
| `Bash(gh pr view:*)`      | Inspect PR metadata, body, and review status   | Used by `/review` and `/develop fix` to understand the PR under review    |
| `Bash(gh pr checkout:*)`  | Check out a PR branch locally                  | `/resolve` uses this to enter the PR branch state before applying changes |
| `Bash(gh pr diff:*)`      | Fetch the full diff of a PR                    | `/review` fetches the diff for static analysis                            |
| `Bash(gh pr list:*)`      | List open or merged PRs                        | `/analyse health` and duplicate-detection modes                           |
| `Bash(gh pr checks:*)`    | Read CI check status on a PR                   | Verify CI passed before marking a fix complete                            |
| `Bash(gh repo view:*)`    | Fetch repository metadata (name, owner)        | `/resolve` detects owner/repo slug for constructing API call paths        |
| `Bash(gh run list:*)`     | List recent workflow runs                      | `/ci-guardian` diagnosis: find the failing run                            |
| `Bash(gh run view:*)`     | View logs and status of a specific CI run      | Read error output from a failed job                                       |
| `Bash(gh issue view:*)`   | Read issue body, labels, and comments          | `/analyse` and `/develop fix` read the issue before starting work         |
| `Bash(gh issue list:*)`   | List issues                                    | `/analyse dupes` and health overview                                      |
| `Bash(gh release view:*)` | Inspect an existing release's notes and assets | `/release` uses this to read the previous release as a baseline           |
| `Bash(gh release list:*)` | List releases                                  | Find the most recent tag to set a changelog range                         |

______________________________________________________________________

## Git — read-only

| Permission              | Description                                          | Typical use case                                                      |
| ----------------------- | ---------------------------------------------------- | --------------------------------------------------------------------- |
| `Bash(git log:*)`       | Browse commit history                                | `/release` reads commits since last tag; general history inspection   |
| `Bash(git shortlog:*)`  | Summarise history grouped by author                  | Contributor stats for release notes                                   |
| `Bash(git describe:*)`  | Derive version string from nearest tag               | Determine current version in release automation                       |
| `Bash(git diff:*)`      | Show unstaged / staged / commit-to-commit changes    | Pre-commit review, diffing a patch before applying                    |
| `Bash(git show:*)`      | Inspect a specific commit, tag, or blob              | Read the content of a tagged release or a specific file at a ref      |
| `Bash(git rev-list:*)`  | Enumerate commits in a range                         | Count distance between refs, find commits to include in release notes |
| `Bash(git rev-parse:*)` | Resolve refs to hashes; get project root             | `/sync` uses `--show-toplevel` to locate the project root             |
| `Bash(git ls-files:*)`  | List tracked files in the index                      | `/sync` uses this inside `cd $PROJECT && git ls-files .claude/`       |
| `Bash(git branch:*)`    | List or inspect local branches                       | Check which branch is active; list branches without touching remote   |
| `Bash(git tag:*)`       | List or inspect local tags                           | Find the latest release tag without pushing                           |
| `Bash(git status:*)`    | Show working-tree state: staged, unstaged, untracked | Pre-commit check, verifying clean state before a release              |

______________________________________________________________________

## Git — local write

| Permission               | Description                                                         | Typical use case                                                                                                                                          |
| ------------------------ | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `Bash(git merge:*)`      | Merge a branch into the current branch (with or without committing) | `/resolve` merges the PR head branch to detect and stage conflict resolution; `--no-commit --no-ff` for inspection, `--ff-only` for clean pointer advance |
| `Bash(git merge-base:*)` | Find the common ancestor commit of two branches                     | `/resolve` uses this to find the diverge point between source and target for diff analysis                                                                |
| `Bash(git worktree:*)`   | Add, list, or remove linked working trees                           | `/resolve` creates a temporary isolated worktree in `/tmp` to run the merge without touching the user's main working directory                            |
| `Bash(git add:*)`        | Stage files for the next commit                                     | Stage changes after an edit before prompting user to commit                                                                                               |
| `Bash(git checkout:*)`   | Switch branches or restore individual files from a ref              | Switch to a feature branch; restore a file to HEAD state                                                                                                  |
| `Bash(git stash:*)`      | Shelve uncommitted changes temporarily                              | Save work in progress before pulling or switching context                                                                                                 |
| `Bash(git restore:*)`    | Discard working-directory changes for specific paths                | Undo accidental edits to a file                                                                                                                           |
| `Bash(git clean:*)`      | Delete untracked files                                              | Ensure a clean build directory before running tests                                                                                                       |
| `Bash(git apply:*)`      | Apply a patch file to the working tree                              | Apply a generated diff or a contributor's patch                                                                                                           |

______________________________________________________________________

## Python toolchain

| Permission                          | Description                                     | Typical use case                                                          |
| ----------------------------------- | ----------------------------------------------- | ------------------------------------------------------------------------- |
| `Bash(python3:*)`                   | Run Python 3 scripts directly                   | Execute helper scripts, one-off data transforms, quick interpreter checks |
| `Bash(pytest:*)`                    | Run the test suite via the `pytest` entry point | Quick test run during TDD loop in `/develop feature` and `/develop fix`   |
| `Bash(pre-commit run:*)`            | Run pre-commit hooks on staged or all files     | Verify formatting and linting before marking a task done                  |
| `Bash(python -m pytest:*)`          | Run tests via the module interface              | Environment-safe alternative when `pytest` binary is not on PATH          |
| `Bash(python -m doctest:*)`         | Execute doctests embedded in a module           | Validate inline usage examples in docstrings                              |
| `Bash(python -m pre_commit run:*)`  | Run pre-commit via module interface             | Alternative invocation inside virtual environments                        |
| `Bash(python -m cProfile:*)`        | Profile a script and output timing data         | `/optimize` Step 1 baseline measurement                                   |
| `Bash(ruff:*)`                      | Lint and auto-fix Python source                 | Run after edits; `check` for diagnostics, `format` for style              |
| `Bash(mypy:*)`                      | Static type-checking                            | Validate type annotations on a module or package                          |
| `Bash(pip show:*)`                  | Display metadata for an installed package       | Check installed version, confirm dependency is present                    |
| `Bash(pip list:*)`                  | List all installed packages and their versions  | Dependency audit, environment snapshot                                    |
| `Bash(pip index:*)`                 | Query PyPI for available versions of a package  | Check whether a newer release is available                                |
| `Bash(pip3 show:*)`                 | `pip3` variant of pip show                      | Explicit Python 3 pip metadata check                                      |
| `Bash(pip3 list:*)`                 | `pip3` variant of pip list                      | Explicit Python 3 dependency audit                                        |
| `Bash(pip3 index:*)`                | `pip3` variant of pip index                     | Explicit Python 3 PyPI version query                                      |
| `Bash(pip-audit:*)`                 | Scan installed packages for known CVEs          | Pre-release dependency CVE scan                                           |
| `Bash(uv run pytest:*)`             | Run tests via uv-managed pytest                 | Same as `pytest:*` but uses the project's uv-managed environment          |
| `Bash(uv run python -m pytest:*)`   | Run tests via uv python module interface        | Environment-safe pytest invocation through uv                             |
| `Bash(uv run python -m doctest:*)`  | Execute doctests via uv python                  | Validate inline usage examples via uv-managed interpreter                 |
| `Bash(uv run python -m cProfile:*)` | Profile a script via uv python                  | `/optimize` baseline measurement through uv-managed interpreter           |
| `Bash(uv run ruff:*)`               | Lint and auto-fix Python source via uv          | Run ruff through uv to ensure project venv rules apply                    |
| `Bash(uv run mypy:*)`               | Static type-checking via uv                     | Run mypy through uv to use project-pinned version                         |
| `Bash(uv run pre-commit run:*)`     | Run pre-commit hooks via uv                     | Verify formatting/linting via uv-managed pre-commit                       |
| `Bash(uv run pip-audit:*)`          | Scan packages for CVEs via uv                   | Pre-release CVE scan through uv-managed environment                       |
| `Bash(uv pip show:*)`               | Display metadata for an installed package       | Check installed version in the uv-managed environment                     |
| `Bash(uv pip list:*)`               | List all packages installed via uv              | Dependency audit of a uv-managed environment                              |
| `Bash(uv pip check:*)`              | Verify package compatibility in uv environment  | Detect dependency conflicts without installing anything                   |
| `Bash(uv tree:*)`                   | Show dependency tree for the project            | Visualize transitive deps; identify why a package is installed            |

______________________________________________________________________

## macOS / ecosystem

| Permission          | Description                                                   | Typical use case                                                                |
| ------------------- | ------------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `Bash(brew info:*)` | Check whether a Homebrew formula is installed and its version | Verify a tool dependency (e.g. `codex`, `gh`) is available                      |
| `Bash(codex:*)`     | Invoke the OpenAI Codex CLI                                   | `/codex` skill delegates mechanical coding tasks to Codex                       |
| `Bash(claude:*)`    | Invoke the Claude Code CLI                                    | SessionStart hook runs `claude auth status` to cache plan info                  |
| `Bash(node:*)`      | Run Node.js scripts                                           | Hooks (`task-log.js`, `statusline.js`) are Node scripts executed by Claude Code |
| `Bash(npm show:*)`  | Fetch npm package metadata                                    | Check latest version of a Node package without installing it                    |
| `Bash(open:*)`      | Open a file or URL with the default macOS application         | Open a browser, Finder, or app from a workflow step                             |

______________________________________________________________________

## WebFetch — allowed domains

| Permission                                   | Description                              | Typical use case                                                                             |
| -------------------------------------------- | ---------------------------------------- | -------------------------------------------------------------------------------------------- |
| `WebFetch(domain:github.com)`                | GitHub web pages and repo content        | Fetch README, release pages, action marketplace entries                                      |
| `WebFetch(domain:docs.github.com)`           | GitHub documentation                     | GitHub Actions syntax, REST API reference                                                    |
| `WebFetch(domain:raw.githubusercontent.com)` | Raw file content from GitHub repos       | Read source files, configs, or changelogs directly                                           |
| `WebFetch(domain:pypi.org)`                  | PyPI package metadata                    | Release history, classifiers, dependency info                                                |
| `WebFetch(domain:pre-commit.ci)`             | pre-commit.ci run status and badge URLs  | Verify CI badges before adding to README                                                     |
| `WebFetch(domain:claude.ai)`                 | Claude product pages                     |                                                                                              |
| `WebFetch(domain:claude.com)`                | Claude Code landing and docs             |                                                                                              |
| `WebFetch(domain:anthropic.com)`             | Anthropic blog, model cards, policy docs | Research model capabilities, fetch release announcements                                     |
| `WebFetch(domain:docs.anthropic.com)`        | Claude Code documentation                | Fetch Claude Code docs; redirects to code.claude.com — both domains needed for full coverage |
| `WebFetch(domain:code.claude.com)`           | Claude Code documentation                | `/audit` fetches hook, agent, and skill schemas for validation                               |
| `WebFetch(domain:arxiv.org)`                 | ML preprints                             | `/survey` and `ai-researcher` fetch papers                                                   |
| `WebFetch(domain:developers.openai.com)`     | OpenAI developer documentation           | Codex CLI docs, API reference                                                                |
| `WebFetch(domain:platform.openai.com)`       | OpenAI platform and API reference        | Model capabilities, pricing, endpoint docs                                                   |
| `WebFetch(domain:openai.com)`                | OpenAI blog and model release notes      | Track new model releases                                                                     |
| `WebFetch(domain:www.anthropic.com)`         | Anthropic main site                      | Research blog posts, model announcements, policy pages                                       |
| `WebFetch(domain:support.claude.com)`        | Anthropic support and help centre        | Lookup Claude feature behaviour, plan limits, billing FAQs                                   |
| `WebFetch(domain:github.blog)`               | GitHub official blog                     | Track new GitHub Actions features, changelog entries, ecosystem news                         |
| `WebFetch(domain:api.codecov.io)`            | Codecov REST API                         | `/ci-guardian` and CI audit reads: coverage reports, branch summaries, PR coverage deltas    |
| `WebFetch(domain:app.codecov.io)`            | Codecov web application                  | Fetch coverage badge URLs and web-facing coverage reports                                    |

______________________________________________________________________

## Skills — pre-approved invocations

| Permission         | Description                                        | Typical use case                                              |
| ------------------ | -------------------------------------------------- | ------------------------------------------------------------- |
| `Skill(sync)`      | Invoke the `/sync` skill without user confirmation | Called from hooks and automation flows that need drift checks |
| `Skill(calibrate)` | Invoke the `/calibrate` skill without confirmation | Agent quality benchmarking in CI or post-fix verification     |
