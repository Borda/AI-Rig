"""Tests for ``hooks/github-read-allow.js``, the gh-read lane behind ``allow-dispatch.js``.

The module allows a Bash command whenever every simple command in it is an allowlisted gh read, with no grant, record or
question (user policy: gh reads run free). Contracts tested here:

* **Allowed shapes** — the read verbs with any option (``gh pr view/diff/checks/list/status``,
  ``gh issue view/list/status``, ``gh repo view/list``, ``gh release view/list``, ``gh run view/list``,
  ``gh workflow view/list``, ``gh label list``, every ``gh search`` subcommand, ``gh gist list/view``, ``gh ruleset
  list/view``, ``gh cache list``, ``gh status``, ``gh project list/view``, ``gh variable list``, ``gh secret list``),
  local reads and help (``gh --version``, ``gh help``, ``gh <group> --help``, ``gh extension list``, ``gh alias list``,
  ``gh config get/list``), ``gh api`` REST reads (a ``?query`` endpoint, quoted or not; an explicit ``-X GET`` with
  plain fields; a double-quoted endpoint holding plain variables after a literal path) and inline GraphQL read
  queries, alone or joined by newlines, ``;``, ``&&``, ``||``, each optionally piped into a closed set of filters
  (``jq``, ``head``, ``tail``, ``wc``, ``sort``, ``uniq``, ``grep``, ``cut``, ``tr``, ``column``) with literal options
  only, with only harmless redirections.
* **No-grant shapes** — every write, a verb that writes local files, logs in or opens a session, a ``gh api`` option
  outside its allowlist, a method other than GET, a query file, a run-time word outside the endpoint, an endpoint whose
  variable or glob could change its host or reach GraphQL, an alias or extension, a filter reading a file, writing one
  or running a program, anything else in the command, a host other than github.com however the option or URL is
  spelled, a ``--jq`` or ``jq`` program naming ``env``, quoting the lexer cannot read exactly, and any spelling where
  the strict grammar and the shared lexer disagree.
* **The write guard agrees** — every allowed shape passes ``gh-write-guard.js``.
* **No record needed** — a covered read is allowed in a repository without any record, outside any repository, and in a
  spawned agent. A write or unreadable quoting gets no allow.
* **Never denies** — the hook prints an allow or nothing, and exits 0.
* **Libraries** — without the lexer nothing is allowed; the approval-record library is not needed.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from _grant_repo import git_env, make_repo

HOOKS_DIR = Path(__file__).resolve().parents[2] / "hooks"
MODULE = HOOKS_DIR / "github-read-allow.js"
WRITE_GUARD = HOOKS_DIR / "gh-write-guard.js"

#: A read every grant test uses where the shape itself is not under test.
GH_READ = "gh pr view 12 --json title,body"

#: Reads covered since the lane was built: the core verbs, ``gh api`` and the ways to join them.
COVERED_READS = [
    pytest.param("gh pr view 12", id="pr-view"),
    pytest.param("gh pr view 12 --json title,body --jq .title", id="pr-view-jq"),
    pytest.param("gh pr view feature/x --comments -R cli/cli", id="pr-view-branch-repo"),
    pytest.param("gh pr diff 12 --name-only", id="pr-diff"),
    pytest.param("gh pr checks 12 --required", id="pr-checks"),
    pytest.param("gh pr list --repo=cli/cli --state open -L 50", id="pr-list"),
    pytest.param("gh issue view 7 --comments", id="issue-view"),
    pytest.param("gh issue list --label bug --search 'is:open sort:updated'", id="issue-list"),
    pytest.param("gh api repos/o/r/pulls --paginate", id="api-rest-paginate"),
    pytest.param("gh api 'repos/o/r/pulls?state=all&per_page=100' --jq '.[].title'", id="api-quoted-query"),
    pytest.param("gh api /user", id="api-leading-slash"),
    pytest.param("gh api repos/{owner}/{repo}/pulls", id="api-placeholders"),
    pytest.param("gh api -H 'Accept: application/vnd.github.raw+json' repos/o/r/readme", id="api-accept"),
    pytest.param("gh api graphql -f query='query { viewer { login } }'", id="graphql-query"),
    pytest.param("gh api graphql -f query='{ viewer { login } }'", id="graphql-selection-set"),
    pytest.param(
        "gh api graphql -F number=12 -f owner=o -f query='query($owner: String!, $number: Int!) "
        '{ repository(owner: $owner, name: "r") { pullRequest(number: $number) { title } } }\'',
        id="graphql-variables",
    ),
    pytest.param("gh pr view 1 && gh pr diff 1", id="and-list"),
    pytest.param("gh pr view 1\ngh issue view 2", id="newline-list"),
    pytest.param("gh pr view 1 2>/dev/null", id="stderr-null"),
    pytest.param("gh pr view 1 >/dev/null 2>&1", id="stdout-null-dup"),
    pytest.param("gh pr view 12 --json title>/dev/null", id="word-before-redirect"),
    pytest.param("gh pr view 1  # note", id="trailing-comment"),
    pytest.param("gh api graphql \\\n  -f query='\n  query { viewer { login } }\n'", id="multi-line"),
]

#: Read-verb options and positionals the first per-verb option tables refused, covered since the relax pass.
RELAXED_READS = [
    pytest.param("gh pr view 1 --web", id="web"),
    pytest.param("gh pr checks 1 --watch", id="watch"),
    pytest.param("gh pr view -- 1", id="end-of-options"),
    pytest.param("gh pr list -L50", id="attached-short-value"),
    pytest.param("gh pr view -cw 1", id="short-cluster"),
    pytest.param("gh pr view 1 --comments=true", id="flag-with-value"),
    pytest.param("gh pr list --author @me --app dependabot --some-future-flag x", id="unlisted-options"),
    pytest.param("gh pr view -", id="dash-positional"),
    pytest.param("gh pr view owner:feature", id="owner-branch"),
    pytest.param("gh issue view //evil.example/o/r/issues/1", id="scheme-less-network-path"),
    pytest.param("gh pr view https://github.com/o/r/pull/1", id="github-url"),
    pytest.param("gh issue view HTTPS://GitHub.com/o/r/issues/1", id="github-url-any-case"),
    pytest.param("gh pr view 1 -Ro/r", id="repo-attached"),
    pytest.param("gh pr view 1 -R=o/r", id="repo-short-equals"),
    pytest.param("gh pr view 1 -cR o/r", id="repo-in-cluster"),
    pytest.param("gh pr view 1 -R github.com/o/r", id="repo-github-host"),
    pytest.param("gh pr view 1 --repo https://github.com/o/r", id="repo-github-url"),
    pytest.param("gh issue list -q '.[].title' --json title", id="jq-short"),
]

#: Read verbs added in the relax pass, with options from their gh manual pages.
ADDED_READS = [
    pytest.param("gh pr status --json number", id="pr-status"),
    pytest.param("gh issue status -R o/r", id="issue-status"),
    pytest.param("gh repo view", id="repo-view-current"),
    pytest.param("gh repo view my-repo", id="repo-view-own-repo"),
    pytest.param("gh repo view o/r --json name,description", id="repo-view-owner-repo"),
    pytest.param("gh repo view github.com/o/r", id="repo-view-github-host"),
    pytest.param("gh repo view https://github.com/o/r --branch dev", id="repo-view-github-url"),
    pytest.param("gh release view v1.0 -R o/r", id="release-view"),
    pytest.param("gh release list -L 5 --exclude-drafts", id="release-list"),
    pytest.param("gh run view 123 --log-failed", id="run-view"),
    pytest.param("gh run list --workflow ci.yml -L 10 --json databaseId", id="run-list"),
    pytest.param("gh workflow view .github/workflows/ci.yml --yaml", id="workflow-view-file"),
    pytest.param("gh workflow list --all", id="workflow-list"),
    pytest.param("gh search issues 'is:open label:bug' --repo o/r", id="search-issues"),
    pytest.param("gh search code foo --language python --limit 5", id="search-code"),
    pytest.param("gh search prs --author @me --state open", id="search-prs"),
    pytest.param("gh label list -R o/r --sort name", id="label-list"),
]

#: Shapes added in round 9b (live lane F3, review GH-READ-GET; user policy "all gh read are allowed"): a read piped
#: into a closed filter set, a ``?query`` endpoint, an explicit GET with plain fields, a double-quoted endpoint with
#: plain variables, more read verbs, local reads and help.
WIDENED_READS = [
    pytest.param("gh pr list --state all --limit 100 --json number | jq length", id="pipe-jq-length"),
    pytest.param("gh pr view 1 | head -5", id="pipe-head-count"),
    pytest.param("gh pr view 1 | jq .", id="pipe-jq-identity"),
    pytest.param("gh pr view 12 --json body | jq -r '.body'", id="pipe-jq-raw"),
    pytest.param("gh pr list --json number,title | jq -rc '.[] | select(.number > 1)' | head -n 20", id="pipe-chain"),
    pytest.param("gh pr view 1 --json body | jq --arg n x --argjson m 1 -r '.body'", id="pipe-jq-args"),
    pytest.param("gh pr view 1 --json title | jq -S -M -e -s .", id="pipe-jq-flags"),
    pytest.param("gh pr view 1 --json title | jq --raw-output --compact-output .title", id="pipe-jq-long-flags"),
    pytest.param("gh pr diff 1 | grep -n 'TODO'", id="pipe-grep"),
    pytest.param("gh pr diff 1 | grep -c -e foo -e bar", id="pipe-grep-patterns"),
    pytest.param("gh pr diff 1 | grep -A3 -iE 'fix|bug'", id="pipe-grep-context-cluster"),
    pytest.param("gh run view 1 --log | tail -n 50", id="pipe-tail"),
    pytest.param("gh run view 1 --log | tail -n +10 | head -c 400", id="pipe-tail-head-bytes"),
    pytest.param("gh pr list --json title --jq '.[].title' | sort | uniq -c | sort -rn", id="pipe-sort-uniq"),
    pytest.param("gh pr list | sort -t, -k2 -u", id="pipe-sort-keys"),
    pytest.param("gh issue list | wc -l", id="pipe-wc"),
    pytest.param("gh pr list | cut -f1,3 -d,", id="pipe-cut"),
    pytest.param("gh issue list | tr -s ' '", id="pipe-tr"),
    pytest.param("gh pr list | column -t -s ,", id="pipe-column"),
    pytest.param("gh pr view 1 2>&1 | head -5", id="pipe-after-redirect"),
    pytest.param("gh pr view 1 | head -5 && gh pr diff 1 | wc -l", id="two-pipelines"),
    pytest.param("gh pr view 1 |\n  head -5", id="pipe-continued-line"),
    pytest.param("gh api repos/o/r/issues/1/comments?per_page=100", id="api-unquoted-query"),
    pytest.param('gh api "repos/o/r/pulls?state=all&per_page=100"', id="api-double-quoted-query"),
    pytest.param("gh api -X GET repos/o/r/pulls -f state=open", id="api-get-field"),
    pytest.param("gh api repos/o/r/pulls -f state=open --method GET", id="api-method-get-after"),
    pytest.param("gh api --method=get search/issues -F per_page=50 -f q=repo:o/r", id="api-method-equals-lowercase"),
    pytest.param("gh api -X GET repos/o/r/pulls", id="api-get-no-field"),
    pytest.param("gh api -X GET -X get repos/o/r/pulls -f state=open", id="api-get-repeated"),
    pytest.param('gh api "repos/$SLUG/pulls/$N"', id="api-quoted-variables"),
    pytest.param('gh api "repos/${SLUG}/commits/${TAG}" --jq .sha', id="api-quoted-braced-variables"),
    pytest.param('gh api "/repos/$SLUG"', id="api-quoted-variable-after-slash"),
    pytest.param("gh repo list o --limit 5", id="repo-list"),
    pytest.param("gh gist list", id="gist-list"),
    pytest.param("gh gist view abc --raw", id="gist-view"),
    pytest.param("gh ruleset list", id="ruleset-list"),
    pytest.param("gh ruleset view 1 -R o/r", id="ruleset-view"),
    pytest.param("gh cache list", id="cache-list"),
    pytest.param("gh status", id="status"),
    pytest.param("gh project list --owner o", id="project-list"),
    pytest.param("gh project view 1 --owner o", id="project-view"),
    pytest.param("gh variable list", id="variable-list"),
    pytest.param("gh secret list -R o/r", id="secret-list"),
    pytest.param("gh --version", id="version-flag"),
    pytest.param("gh version", id="version-command"),
    pytest.param("gh help", id="help"),
    pytest.param("gh help pr view", id="help-topic"),
    pytest.param("gh --help", id="help-flag"),
    pytest.param("gh pr --help", id="group-help"),
    pytest.param("gh extension list", id="extension-list"),
    pytest.param("gh alias list", id="alias-list"),
    pytest.param("gh config get editor", id="config-get"),
    pytest.param("gh config get git_protocol -h github.com", id="config-get-host"),
    pytest.param("gh config list", id="config-list"),
]

_skip_tools_unavailable = pytest.mark.skipif(
    shutil.which("node") is None or shutil.which("git") is None, reason="requires node and git"
)

#: Prints ``require(<module>).<function>(<stdin>)`` as JSON.
_CALL = (
    "const m = require(process.argv[1]); let raw = '';"
    "process.stdin.setEncoding('utf8'); process.stdin.on('data', (c) => (raw += c));"
    "process.stdin.on('end', () => process.stdout.write(JSON.stringify(m[process.argv[2]](raw))));"
)


def _call(function: str, stdin: str, cwd: Path, env: dict[str, str], module: Path = MODULE) -> dict:
    """Run one exported function of the module in a fresh node process and return its JSON result."""
    proc = subprocess.run(
        ["node", "-e", _CALL, str(module), function],
        input=stdin,
        capture_output=True,
        encoding="utf-8",
        cwd=str(cwd),
        env=env,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _payload(command: object, cwd: Path, **extra: object) -> str:
    """Return a PreToolUse(Bash) stdin payload for ``command`` run in ``cwd``."""
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(cwd),
            "session_id": "session-under-test",
            **extra,
        }
    )


@pytest.fixture(name="home")
def _home(tmp_path: Path) -> Path:
    """Return a throwaway home directory outside any Git repository."""
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture(name="repo")
def _repo(tmp_path: Path) -> Path:
    """Return a fresh Git work tree with no record in it."""
    return make_repo(tmp_path)


@pytest.fixture(name="classify")
def _classify(home: Path):
    """Return a callable classifying a command without consulting any grant (no Git runs)."""
    return lambda command: _call("classify", command, home, git_env(home))


@pytest.fixture(name="evaluate")
def _evaluate(repo: Path, home: Path):
    """Return a callable evaluating a Bash payload run in ``repo``, the session project."""

    def run(command: object, **extra: object) -> dict:
        return _call("evaluate", _payload(command, repo, **extra), repo, git_env(home, repo))

    return run


@_skip_tools_unavailable
class TestAllowedShapes:
    """Commands made only of allowlisted gh reads classify as covered by a grant."""

    @pytest.mark.parametrize("command", COVERED_READS)
    def test_is_covered(self, classify, command: str) -> None:
        """An allowlisted read, alone or joined with others, classifies as covered.

        These are the shapes a grant exists for. A classifier that rejected them would leave every gh read prompting
        even after the user answered ``Approve always``.
        """
        assert classify(command) == {"ok": True}

    @pytest.mark.parametrize("command", RELAXED_READS)
    def test_option_once_refused_is_covered(self, classify, command: str) -> None:
        """A read-verb option or positional the first per-verb tables refused is covered now.

        The user asked for the grant to prompt only where tightness buys safety: an unlisted option, a cluster, an
        attached value, ``--``, ``--web`` or ``--watch`` reaches no other host, prints no secret and writes nothing, so
        refusing it only re-created the prompt the grant exists to remove. Scheme-less positionals (``owner:branch``,
        ``//x``) are a branch or number to gh, never a host: gh takes a PR or issue locator as a URL only for an
        ``http``/``https`` scheme. github.com stays reachable through ``-R`` or a URL in any spelling.
        """
        assert classify(command) == {"ok": True}

    @pytest.mark.parametrize("command", ADDED_READS)
    def test_added_read_verb_is_covered(self, classify, command: str) -> None:
        """A read verb added in the relax pass is covered with its own options.

        ``pr status``, ``issue status``, ``repo view``, ``release view/list``, ``run view/list``, ``workflow
        view/list``, ``label list`` and ``gh search`` only read; leaving them out kept a prompt on every common
        inspection call. A workflow file path keeps its slashes: only ``gh repo view`` reads a positional as a
        repository.
        """
        assert classify(command) == {"ok": True}

    @pytest.mark.parametrize("command", WIDENED_READS)
    def test_widened_read_is_covered(self, classify, command: str) -> None:
        """A read shape added in round 9b is covered: user policy is that no gh read stops or asks.

        The live lane's 65-command probe found these prompting with no ``Bash(gh:*)`` rule: reads piped to a filter,
        ``?query`` endpoints, an explicit GET with fields, a variable endpoint, and read verbs outside the first list.
        Each filter takes literal options only and none that reads or writes a file or runs a program, so the pipe adds
        no reach; a variable or glob in an endpoint sits after a literal path, so it cannot change the host.
        """
        assert classify(command) == {"ok": True}


@_skip_tools_unavailable
class TestNoGrantShapes:
    """Everything outside the read allowlist gets no allow, whatever the grant says."""

    @pytest.mark.parametrize(
        ("command", "why"),
        [
            pytest.param("gh api repos/o/r/issues -f title=x", "not-a-read", id="rest-raw-field"),
            pytest.param("gh api repos/o/r/issues -F title=x", "not-a-read", id="rest-typed-field"),
            pytest.param("gh api repos/o/r/issues --raw-field title=x", "not-a-read", id="rest-raw-field-long"),
            pytest.param("gh api repos/o/r -X POST", "not-a-read", id="method-short"),
            pytest.param("gh api repos/o/r --method=PATCH", "not-a-read", id="method-long"),
            pytest.param("gh api repos/o/r --input body.json", "not-a-read", id="input"),
            pytest.param("gh api graphql -f query='mutation { a }'", "not-a-read", id="graphql-mutation"),
            pytest.param("gh api graphql -f query='subscription { a }'", "not-a-read", id="graphql-subscription"),
            pytest.param("gh api graphql -f query='{ a }' -f Query='mutation { b }'", "not-a-read", id="graphql-twice"),
            pytest.param("gh api graphql -F query=@q.graphql", "not-a-read", id="graphql-query-file"),
            pytest.param("gh api graphql -f query='{ a }' -F n=@-", "not-a-read", id="graphql-field-stdin"),
            pytest.param("gh api graphql -f 'a[]=1' -f query='{ a }'", "not-a-read", id="graphql-nested-key"),
            pytest.param("gh api graphql", "not-a-read", id="graphql-no-query"),
            pytest.param("gh api graphql -f query='fragment F on User { login }'", "not-a-read", id="graphql-fragment"),
            pytest.param('gh api graphql -f query="$Q"', "not-gh-only", id="graphql-variable"),
            pytest.param("gh pr view $(echo 1)", "not-gh-only", id="substitution"),
            pytest.param("gh pr view `echo 1`", "not-gh-only", id="backticks"),
            pytest.param("gh co 12", "not-a-read", id="alias"),
            pytest.param("gh my-extension list", "not-a-read", id="extension"),
            pytest.param("gh pr create -t x", "not-a-read", id="pr-create"),
            pytest.param("gh issue comment 1 -b x", "not-a-read", id="issue-comment"),
            pytest.param("gh release create v1", "not-a-read", id="release-create"),
            pytest.param("gh run rerun 1", "not-a-read", id="run-rerun"),
            pytest.param("gh workflow run ci.yml", "not-a-read", id="workflow-run"),
            pytest.param("gh label create bug", "not-a-read", id="label-create"),
            pytest.param("gh repo edit --visibility public", "not-a-read", id="repo-edit"),
            pytest.param("gh run download 1", "not-a-read", id="run-download"),
            pytest.param("gh release download v1", "not-a-read", id="release-download"),
            pytest.param("gh repo clone o/r", "not-a-read", id="repo-clone"),
            pytest.param("gh pr checkout 1", "not-a-read", id="pr-checkout"),
            pytest.param("gh codespace list", "not-a-read", id="codespace"),
            pytest.param("gh auth status", "not-a-read", id="auth-status"),
            pytest.param("gh auth token", "not-a-read", id="auth-token"),
            pytest.param("gh pr view 1 | sh", "not-gh-only", id="pipe"),
            pytest.param("gh pr view 1 > out.txt", "not-gh-only", id="redirect-file"),
            pytest.param("gh pr view 1 >> out.txt", "not-gh-only", id="redirect-append"),
            pytest.param("gh pr view 1 < in.txt", "not-gh-only", id="redirect-input"),
            pytest.param("gh pr view 1 && rm x", "not-gh-only", id="other-program"),
            pytest.param("GH_HOST=evil.example gh pr view 1", "not-gh-only", id="env-prefix"),
            pytest.param("gh pr view 1 -R evil.example/o/r", "not-a-read", id="repo-with-host"),
            pytest.param("gh pr view 1 --repo=evil.example/o/r", "not-a-read", id="repo-long-equals-host"),
            pytest.param("gh pr view 1 -Revil.example/o/r", "not-a-read", id="repo-attached-host"),
            pytest.param("gh pr view 1 -R=evil.example/o/r", "not-a-read", id="repo-short-equals-host"),
            pytest.param("gh pr view 1 -cR evil.example/o/r", "not-a-read", id="repo-in-cluster-host"),
            pytest.param("gh pr view 1 -R https://evil.example/o/r", "not-a-read", id="repo-url-host"),
            pytest.param("gh pr view 1 -R git@evil.example:o/r", "not-a-read", id="repo-ssh-host"),
            pytest.param("gh release view v1 -R evil.example/o/r", "not-a-read", id="release-repo-host"),
            pytest.param("gh pr view 1 --hostname evil.example", "not-a-read", id="hostname"),
            pytest.param("gh issue list --hostname=evil.example", "not-a-read", id="hostname-equals"),
            pytest.param("gh pr view https://evil.example/o/r/pull/1", "not-a-read", id="pr-url"),
            pytest.param("gh pr view https://github.com@evil.example/o/r/pull/1", "not-a-read", id="url-userinfo"),
            pytest.param("gh pr view https://github.com.evil.example/o/r/pull/1", "not-a-read", id="url-lookalike"),
            pytest.param("gh issue view HTTPS://EVIL.EXAMPLE/o/r/issues/1", "not-a-read", id="url-upper-scheme"),
            pytest.param("gh repo view evil.example/o/r", "not-a-read", id="repo-view-host"),
            pytest.param("gh repo view https://evil.example/o/r", "not-a-read", id="repo-view-url"),
            pytest.param("gh repo view git@evil.example:o/r", "not-a-read", id="repo-view-ssh"),
            pytest.param("gh api https://evil.example/x", "not-a-read", id="api-url"),
            pytest.param("gh api //evil.example/x", "not-a-read", id="api-network-path"),
            pytest.param("gh api repos/o/r --hostname evil.example", "not-a-read", id="api-hostname"),
            pytest.param("gh api repos/o/r --verbose", "not-a-read", id="api-verbose"),
            pytest.param("gh api repos/o/r -H 'X-HTTP-Method-Override: POST'", "not-a-read", id="method-override"),
            pytest.param("gh api repos/o/r -H 'Authorization: token x'", "not-a-read", id="other-header"),
            pytest.param("gh api repos/o/r a/b", "not-a-read", id="two-endpoints"),
            pytest.param("gh pr view 1 --json title --jq env.GH_TOKEN", "not-a-read", id="jq-env"),
            pytest.param("gh pr view 1 --json title -qenv.GH_TOKEN", "not-a-read", id="jq-attached-env"),
            pytest.param("gh issue list --json title --jq=env.GH_TOKEN", "not-a-read", id="jq-equals-env"),
            pytest.param("gh pr view 1 --json title -cq '$ENV'", "not-a-read", id="jq-in-cluster-env"),
            pytest.param("gh run view 1 --json jobs --jq '$ENV.GH_TOKEN'", "not-a-read", id="jq-env-added-verb"),
            pytest.param("gh api user --jq '$ENV'", "not-a-read", id="jq-env-object"),
            pytest.param("bash -c 'gh pr view 1'", "not-gh-only", id="bash-c"),
            pytest.param("eval 'gh pr view 1'", "not-gh-only", id="eval"),
            pytest.param("echo 1 | xargs gh pr view", "not-gh-only", id="xargs"),
            pytest.param("gh pr view $'\\u0031'", "unreadable", id="ansi-c-unreadable"),
            pytest.param("gh pr view 'unbalanced", "unreadable", id="unbalanced-quote"),
            pytest.param("gh api re?os/o/r/pulls", "not-gh-only", id="unquoted-glob"),
            pytest.param("gh pr view ~/x", "not-gh-only", id="tilde"),
            pytest.param("gh pr view {1,2}", "lexer-mismatch", id="brace-expansion"),
            pytest.param("gh issue list -l gh", "lexer-mismatch", id="gh-word-in-arguments"),
            pytest.param("gh pr view 1 {fd}>/dev/null", "lexer-mismatch", id="named-fd-redirect"),
            pytest.param("gh pr -R o/r view 1", "not-a-read", id="flag-before-verb"),
            pytest.param("/usr/bin/gh pr view 1", "not-gh-only", id="program-path"),
            pytest.param("gh pr view 1 &", "not-gh-only", id="background"),
            pytest.param("gh pr view 1 &&", "not-gh-only", id="dangling-and"),
            pytest.param("gh pr view 1 ;; gh pr view 2", "not-gh-only", id="double-semicolon"),
            pytest.param("gh api repos/o/r -- x", "not-a-read", id="api-end-of-options"),
            pytest.param("gh api repos/o/r --slurp=true", "not-a-read", id="api-flag-with-value"),
            pytest.param("gh api repos/o/r -iq .x", "not-a-read", id="api-short-cluster"),
            pytest.param("gh pr view 1 =x", "not-gh-only", id="zsh-equals-word"),
            pytest.param("gh pr view 1#x", "not-gh-only", id="mid-word-hash"),
            pytest.param('gh pr view "1\\"', "unreadable", id="escape-in-double-quotes"),
        ],
    )
    def test_is_not_covered(self, classify, command: str, why: str) -> None:
        """A command outside the allowlist classifies as not covered, for the stated reason.

        Each case is a way a grant meant for reads could otherwise reach a write, a local file or session, another
        program, another host, a secret, or an argv the shell builds differently from the one inspected. Read-verb
        options are free, so the host and ``--jq`` checks are pinned in every pflag spelling (``--repo=v``, ``-Rv``,
        ``-R=v``, a cluster); ``gh api`` keeps its strict option grammar. The reason pins which check caught it, so a
        regression in one check cannot hide behind another.
        """
        result = classify(command)
        assert (result["ok"], result["decision"], result["why"]) == (False, "passthrough", why)

    @pytest.mark.parametrize(
        ("command", "why"),
        [
            pytest.param("gh pr view 1 | jq . file.json", "not-gh-only", id="jq-file"),
            pytest.param("gh pr view 1 | jq -f prog.jq", "not-gh-only", id="jq-program-file"),
            pytest.param("gh pr view 1 | jq --rawfile a secret.txt .", "not-gh-only", id="jq-rawfile"),
            pytest.param("gh pr view 1 | jq --slurpfile a x.json .", "not-gh-only", id="jq-slurpfile"),
            pytest.param("gh pr view 1 | jq -L lib .", "not-gh-only", id="jq-library-path"),
            pytest.param("gh pr view 1 | jq 'import \"m\" as m; .'", "not-gh-only", id="jq-import"),
            pytest.param("gh pr view 1 | jq 'include \"m\"; .'", "not-gh-only", id="jq-include"),
            pytest.param("gh pr view 1 | jq env.GH_TOKEN", "not-gh-only", id="jq-env"),
            pytest.param("gh pr view 1 | jq -r '$ENV.GH_TOKEN'", "not-gh-only", id="jq-env-object"),
            pytest.param("gh pr view 1 | jq -n 'input'", "not-gh-only", id="jq-unlisted-flag"),
            pytest.param("gh pr view 1 | jq --arg n", "not-gh-only", id="jq-arg-without-value"),
            pytest.param("gh pr view 1 | head -5 notes.txt", "not-gh-only", id="head-file"),
            pytest.param("gh run view 1 --log | tail -f", "not-gh-only", id="tail-follow"),
            pytest.param("gh pr list | sort -o out.txt", "not-gh-only", id="sort-output-file"),
            pytest.param("gh pr list | sort -ro out.txt", "not-gh-only", id="sort-output-file-in-cluster"),
            pytest.param("gh pr list | sort --compress-program=sh", "not-gh-only", id="sort-compress-program"),
            pytest.param("gh pr list | sort -T /tmp", "not-gh-only", id="sort-temp-dir"),
            pytest.param("gh pr list | uniq - out.txt", "not-gh-only", id="uniq-output-file"),
            pytest.param("gh pr diff 1 | grep -f patterns.txt", "not-gh-only", id="grep-pattern-file"),
            pytest.param("gh pr diff 1 | grep -r x", "not-gh-only", id="grep-recursive"),
            pytest.param("gh pr diff 1 | grep x notes.txt", "not-gh-only", id="grep-file"),
            pytest.param("gh pr diff 1 | grep -e x notes.txt", "not-gh-only", id="grep-pattern-option-and-file"),
            pytest.param("gh pr list | wc --files0-from=list", "not-gh-only", id="wc-file-list"),
            pytest.param("gh pr list | cut -f1 notes.txt", "not-gh-only", id="cut-file"),
            pytest.param("gh pr list | column -t notes.txt", "not-gh-only", id="column-file"),
            pytest.param("gh pr list | tee out.txt", "not-gh-only", id="tee"),
            pytest.param("gh pr list | xargs echo", "not-gh-only", id="xargs-filter"),
            pytest.param("gh pr list |& head", "not-gh-only", id="pipe-stderr"),
            pytest.param("jq . x.json | gh pr view 1", "not-gh-only", id="filter-first"),
            pytest.param("gh pr view 1 | gh pr view 2", "not-gh-only", id="gh-after-pipe"),
            pytest.param("gh pr view 1 | head -5 > out.txt", "not-gh-only", id="filter-redirect-to-file"),
            pytest.param("gh pr view 1 | HEAD -5", "not-gh-only", id="filter-name-case"),
            pytest.param("gh pr view 1 | /usr/bin/head -5", "not-gh-only", id="filter-path"),
            pytest.param("gh pr view 1 | grep a?b", "not-gh-only", id="filter-glob"),
            pytest.param('gh pr view 1 | head -n "$N"', "not-gh-only", id="filter-variable"),
            pytest.param("gh pr view 1 | head --", "not-gh-only", id="filter-end-of-options"),
            pytest.param("gh pr view 1 |", "not-gh-only", id="dangling-pipe"),
            pytest.param("gh pr view 1 | jq {.,x}", "lexer-mismatch", id="filter-brace-expansion"),
            pytest.param("gh pr merge 1 | head", "not-a-read", id="write-piped"),
        ],
    )
    def test_pipe_outside_the_filter_set_is_not_covered(self, classify, command: str, why: str) -> None:
        """A pipe is covered only into the closed filter set, with literal options that touch no file or program.

        An allow covers the whole command, so a filter that reads a file (``jq . f``, ``grep -f``, ``head f``), writes
        one (``sort -o``, ``uniq in out``, ``tee``), runs a program (``sort --compress-program``, ``xargs``, ``sh``) or
        reads the environment (``jq env``) would skip the prompt that file or program access gets on its own.
        """
        result = classify(command)
        assert (result["ok"], result["decision"], result["why"]) == (False, "passthrough", why)

    @pytest.mark.parametrize(
        ("command", "why"),
        [
            pytest.param("gh api -X GET repos/o/r -F body=@file.json", "not-a-read", id="get-field-from-file"),
            pytest.param("gh api -X GET repos/o/r -X POST", "not-a-read", id="get-then-post"),
            pytest.param("gh api -X GET graphql -f query='{ a }'", "not-a-read", id="get-graphql"),
            pytest.param("gh api -X GET /graphql -f query='mutation { a }'", "not-a-read", id="get-graphql-path"),
            pytest.param("gh api -X GET repos/../graphql -f q=x", "not-a-read", id="get-dot-segment"),
            pytest.param("gh api -X GET repos/%2e%2e/graphql -f q=x", "not-a-read", id="get-encoded-dot-segment"),
            pytest.param("gh api -X GET repos/o/r --input body.json", "not-a-read", id="get-input"),
            pytest.param("gh api -X GET repos/o/r -f 'a[]=1'", "not-a-read", id="get-nested-field"),
            pytest.param("gh api -XGET repos/o/r", "not-a-read", id="get-attached"),
            pytest.param("gh api -X GET repos/o/r -H 'X-HTTP-Method-Override: POST'", "not-a-read", id="get-override"),
            pytest.param(
                'gh api -X GET "repos/$S/pulls" -f state=open', "not-a-read", id="get-field-variable-endpoint"
            ),
            pytest.param('gh api -X "$M" repos/o/r', "not-gh-only", id="method-variable"),
            pytest.param('gh api -X GET repos/o/r -f q="$Q"', "not-gh-only", id="get-field-variable"),
            pytest.param("gh api repos/$SLUG/commits/$TAG", "not-gh-only", id="unquoted-variables"),
            pytest.param('gh api "$EP"', "not-gh-only", id="variable-endpoint"),
            pytest.param('gh api "graph$X"', "not-gh-only", id="variable-before-a-slash"),
            pytest.param('gh api "h$X"', "not-gh-only", id="variable-could-open-a-scheme"),
            pytest.param('gh api "repos/${X:-a}/x"', "not-gh-only", id="variable-with-default"),
            pytest.param('gh api "repos/$X[1]"', "not-gh-only", id="variable-subscript"),
            pytest.param('gh api "repos/${X}[1]"', "not-gh-only", id="braced-variable-subscript"),
            pytest.param('gh api "repos/$1/x"', "not-gh-only", id="positional-parameter"),
            pytest.param('gh api "repos/$(id)/x"', "not-gh-only", id="substitution-in-endpoint"),
            pytest.param('gh api "repos/$"', "not-gh-only", id="lone-dollar"),
            pytest.param("gh api graph?l", "not-gh-only", id="glob-could-match-graphql"),
            pytest.param("gh api repos/o/r/pulls?state=open&per_page=1", "not-gh-only", id="unquoted-query-ampersand"),
            pytest.param("gh pr view 1?", "not-gh-only", id="glob-outside-endpoint"),
            pytest.param("gh pr view 1 ?R evil.example/o/r", "not-gh-only", id="glob-could-match-an-option"),
            pytest.param('gh pr view "$N"', "not-gh-only", id="variable-outside-endpoint"),
        ],
    )
    def test_api_widening_keeps_its_bounds(self, classify, command: str, why: str) -> None:
        """An explicit GET, a ``?query`` or a variable endpoint is covered only where it stays a read on the host.

        A method other than GET writes; under GET a path reaching GraphQL (``/graphql``, a dot segment) could carry a
        mutation, so fields need a literal REST path. A variable or glob is run-time text: covered only inside the
        endpoint and after a literal ``/``, where no value can open a scheme or become ``graphql``; anywhere else a glob
        could match a planted ``-R`` file and a variable could name another host.
        """
        result = classify(command)
        assert (result["ok"], result["decision"], result["why"]) == (False, "passthrough", why)

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh config get oauth_token -h github.com", id="config-token"),
            pytest.param("gh config get foo", id="config-unknown-key"),
            pytest.param("gh config set editor vim", id="config-set"),
            pytest.param("gh config clear-cache", id="config-clear-cache"),
            pytest.param("gh extension install o/gh-x", id="extension-install"),
            pytest.param("gh extension exec x", id="extension-exec"),
            pytest.param("gh extension upgrade --all", id="extension-upgrade"),
            pytest.param("gh extension list x", id="extension-list-argument"),
            pytest.param("gh alias set co 'pr checkout'", id="alias-set"),
            pytest.param("gh alias import x.yml", id="alias-import"),
            pytest.param("gh auth --help", id="auth-help"),
            pytest.param("gh my-ext --help", id="extension-help"),
            pytest.param("gh help my-ext", id="help-extension"),
            pytest.param("gh pr create --help", id="write-verb-help"),
            pytest.param("gh --version x", id="version-argument"),
            pytest.param("gh gist create x.md", id="gist-create"),
            pytest.param("gh gist clone abc", id="gist-clone"),
            pytest.param("gh gist view https://gist.github.com/o/abc", id="gist-url-other-host"),
            pytest.param("gh cache delete 1", id="cache-delete"),
            pytest.param("gh project create --title x", id="project-create"),
            pytest.param("gh variable set X", id="variable-set"),
            pytest.param("gh secret set X", id="secret-set"),
            pytest.param("gh ruleset check", id="ruleset-check"),
            pytest.param("gh repo list https://evil.example/o", id="repo-list-other-host"),
            pytest.param("gh status -R evil.example/o/r", id="status-other-host"),
        ],
    )
    def test_added_verbs_keep_their_bounds(self, classify, command: str) -> None:
        """Only the listed read verbs, help and local reads are covered: anything that sets, installs, runs or logs in.

        ``gh config get oauth_token`` prints the token from the keyring, so ``config get`` takes known keys only;
        ``--help`` on an extension runs it, and on a write verb the write guard blocks it anyway.
        """
        result = classify(command)
        assert (result["ok"], result["decision"], result["why"]) == (False, "passthrough", "not-a-read")

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("git status", id="git"),
            pytest.param("ls high", id="gh-substring"),
            pytest.param("echo ok", id="no-gh-text"),
        ],
    )
    def test_command_without_gh_has_no_opinion(self, classify, command: str) -> None:
        """A command running no gh at all is ``none``: the lane has nothing to say about it.

        The dispatcher distinguishes no opinion from a decline; every non-gh Bash call landing as a decline would bury
        the gh decisions in the audit log.
        """
        assert classify(command) == {"ok": False, "decision": "none", "why": "no-gh"}


@_skip_tools_unavailable
@pytest.mark.integration
class TestWriteGuardAgrees:
    """Every shape the lane covers passes ``gh-write-guard.js``: the read allowlist never reaches a guarded write."""

    @pytest.mark.parametrize("command", [*COVERED_READS, *RELAXED_READS, *ADDED_READS, *WIDENED_READS])
    def test_guard_lets_covered_read_through(self, home: Path, command: str) -> None:
        """The write guard exits 0 on each covered read.

        Read-verb options are free now, so "never a write" rests on the verb list alone; running the real guard over
        every covered shape pins that the two hooks agree, instead of trusting that no listed verb shares a word with a
        write gh-write-guard.js knows.
        """
        proc = subprocess.run(
            ["node", str(WRITE_GUARD)],
            input=_payload(command, home),
            capture_output=True,
            encoding="utf-8",
            cwd=str(home),
            env=git_env(home),
            timeout=30,
            check=False,
        )
        assert (proc.returncode, proc.stderr) == (0, "")


@_skip_tools_unavailable
@pytest.mark.integration
class TestAllowWithoutRecord:
    """A covered shape is allowed with no grant, record or question, in any session or agent."""

    def test_read_is_allowed(self, evaluate) -> None:
        """A covered read in a repository holding no record is an allow from lane 3.

        User policy is "all gh read are allowed": the read must never reach a permission prompt or a question.
        """
        result = evaluate(GH_READ)
        assert (result["decision"], result["lane"], result["rank"]) == ("allow", "gh-read", 3)
        assert result["payload"]["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_read_outside_a_repository_is_allowed(self, home: Path) -> None:
        """A covered read run outside any Git repository is allowed too.

        Nothing is looked up on disk, so a read from a scratch directory gets the same answer as one in a project.
        """
        result = _call("evaluate", _payload(GH_READ, home), home, git_env(home))
        assert result["decision"] == "allow"

    def test_read_in_a_spawned_agent_is_allowed(self, evaluate) -> None:
        """A call made inside a spawned agent (``agent_id`` in the payload) is allowed like the session's own.

        Before the lane existed, subagents ran these reads through the static allow rules; the lane must not take that
        away.
        """
        assert evaluate(GH_READ, agent_id="agent-under-test")["decision"] == "allow"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh api repos/o/r/issues -f title=x", id="rest-post"),
            pytest.param("gh api graphql -f query='mutation { a }'", id="graphql-mutation"),
            pytest.param("gh pr merge 1", id="pr-merge"),
            pytest.param("gh api -X DELETE repos/o/r", id="api-delete"),
            pytest.param("gh release delete v1 --yes", id="release-delete"),
            pytest.param("gh workflow run ci.yml --ref main", id="workflow-run"),
        ],
    )
    def test_write_is_never_allowed(self, evaluate, command: str) -> None:
        """A write gets no allow from the read lane.

        gh-write-guard.js gates these with its one-time token; this asserts the allow lane can never be the thing that
        lets one through, even if the guard were missing.
        """
        result = evaluate(command)
        assert (result["decision"], result["why"], result["payload"]) == ("passthrough", "not-a-read", None)

    def test_unreadable_quoting_is_not_allowed(self, evaluate) -> None:
        """Quoting bash and zsh read differently has no exact argv, so the read lane gives no allow."""
        result = evaluate("gh pr view $'\\u0031'")
        assert (result["decision"], result["why"]) == ("passthrough", "unreadable")


@_skip_tools_unavailable
class TestNotApplicable:
    """Payloads that carry no Bash command to examine get no opinion."""

    @pytest.mark.parametrize(
        "stdin",
        [
            pytest.param("not json", id="not-json"),
            pytest.param("{}", id="empty-object"),
            pytest.param(json.dumps({"tool_name": "Read", "tool_input": {"command": GH_READ}}), id="non-bash"),
            pytest.param(json.dumps({"tool_name": "Bash", "tool_input": {"command": 42}}), id="non-string"),
            pytest.param(json.dumps({"tool_name": "Bash", "tool_input": {"command": "   "}}), id="blank"),
        ],
    )
    def test_is_none(self, home: Path, stdin: str) -> None:
        """A malformed or foreign payload is ``none``, never a module error and never an allow."""
        result = _call("evaluate", stdin, home, git_env(home))
        assert (result["decision"], result["why"], result["payload"]) == ("none", "not-applicable", None)


@_skip_tools_unavailable
@pytest.mark.integration
class TestStandaloneDriver:
    """The module runs as its own hook."""

    def _hook(self, stdin: str, cwd: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess:
        """Run the module's own driver and return the completed process."""
        return subprocess.run(
            ["node", str(MODULE), *args],
            input=stdin,
            capture_output=True,
            encoding="utf-8",
            cwd=str(cwd),
            env=env,
            timeout=30,
            check=False,
        )

    def test_allow_is_printed_for_a_read(self, repo: Path, home: Path) -> None:
        """For a covered read the driver prints the allow payload and exits 0."""
        proc = self._hook(_payload(GH_READ, repo), repo, git_env(home, repo))
        assert proc.returncode == 0
        assert json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"] == "allow"

    @pytest.mark.parametrize(
        "command",
        [
            pytest.param("gh pr merge 1", id="write"),
            pytest.param("gh pr view 'unbalanced", id="unreadable"),
        ],
    )
    def test_never_prints_a_deny(self, repo: Path, home: Path, command: str) -> None:
        """Without an allow the driver prints nothing at all and exits 0: it never denies or blocks."""
        proc = self._hook(_payload(command, repo), repo, git_env(home, repo))
        assert (proc.returncode, proc.stdout, proc.stderr) == (0, "", "")


@_skip_tools_unavailable
class TestMissingLibraries:
    """A module installed without its lexer allows nothing; it needs no other library."""

    def _install(self, base: Path, libraries: tuple[str, ...]) -> Path:
        """Copy the module and only ``libraries`` into a fresh hooks directory; return the module path."""
        hooks = base / "plugin" / "hooks"
        (hooks / "lib").mkdir(parents=True)
        shutil.copy(MODULE, hooks / MODULE.name)
        for name in libraries:
            shutil.copy(HOOKS_DIR / "lib" / name, hooks / "lib" / name)
        return hooks / MODULE.name

    def test_read_without_lexer_is_not_allowed(self, tmp_path: Path, repo: Path, home: Path) -> None:
        """Without the lexer no argv is exact, so a read gets no allow.

        Requiring the module must not throw, or the dispatcher would record a module error instead of a decline.
        """
        module = self._install(tmp_path, ())
        result = _call("evaluate", _payload(GH_READ, repo), repo, git_env(home, repo), module=module)
        assert (result["decision"], result["why"]) == ("passthrough", "unreadable")

    def test_read_with_lexer_alone_is_allowed(self, tmp_path: Path, repo: Path, home: Path) -> None:
        """With only the lexer installed a read is allowed: the approval-record library is not needed."""
        module = self._install(tmp_path, ("shell-git.js",))
        result = _call("evaluate", _payload(GH_READ, repo), repo, git_env(home, repo), module=module)
        assert result["decision"] == "allow"
