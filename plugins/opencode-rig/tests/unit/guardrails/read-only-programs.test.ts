/** Read operands stay quiet while output options and unknown programs retain the positive path rule. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

it.each([
  "file -mmyfile.magic a.txt",
  "file -m myfile.magic a.txt",
  "file -Ff a.txt",
  "wget -Pdeps https://example.test",
  "curl -uKim:pw https://example.test",
])("keeps attached reader option values separate from option letters: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", { status: "granted", path: "approval.json" })).toBeUndefined()
})

it.each(["ruby -Itest", "perl -Iinc", "php -dmemory_limit=-1", "julia -Cnative"])(
  "asks for interpreter load and configuration options with attached values: %s",
  (body) => {
    expect(decide(classifyCommand(body), "standard", { status: "granted", path: "approval.json" })?.effect).toBe("ask")
  },
)

it.each(["du -X input", "grep --exclude-from=input pattern file", "cmp @left @right"])(
  "keeps literal reader file options quiet: %s",
  (body) => {
    expect(decide(classifyCommand(body), "standard", { status: "granted", path: "approval.json" })).toBeUndefined()
  },
)

const GRANTED = { status: "granted", path: "approval.json" } as const
const savedThenRun = (body: string): string => `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`

describe.each(["top level", "saved script"])("response-file operands: %s", (scope) => {
  const wrap = (body: string) => (scope === "top level" ? body : savedThenRun(body))
  it.each(
    (
      "file less more cd pwd test [ true false " +
      "find sed uniq jq du df realpath readlink basename dirname cut tr nl column fold comm join " +
      "paste od hexdump xxd shasum sha256sum md5 date which base64"
    ).split(" "),
  )("asks for option-file operands to %s", (program) => {
    expect(decide(classifyCommand(wrap(`${program} @args.txt`)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "head -n@args.txt input",
    "head --lines=@args.txt input",
    "head -n @args.txt input",
    "find . -name=@args.txt",
    "sed -e@args.txt input",
    "jq --arg key @args.txt . input",
    "cd @args.txt; echo x > out",
  ])("asks for decoded option-file values: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "head @args.txt",
    "cat @args.txt",
    "ls @args.txt",
    "tail @args.txt",
    "stat @args.txt",
    "sort @args.txt",
    "wc @args.txt",
    "diff @args.txt",
    "grep @args.txt",
    "egrep @args.txt",
    "fgrep @args.txt",
    "rgrep @args.txt",
    "rg @args.txt",
    "find . -name @args.txt",
    "grep -e@args.txt input",
    "echo '@args.txt'",
    "echo 'mail@example.com'",
    "printf '%s' '@args.txt'",
    "printf '%s' 'mail@example.com'",
    "echo 'key=@args.txt'",
    "printf '%s' 'key=@args.txt'",
  ])("keeps literal read operands, patterns and output data quiet: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)).toBeUndefined()
  })
})

const READS = [
  "cd src && python3 -m pytest -q",
  "find /usr/include -name '*.h'",
  "find .. -name '*.md'",
  "find . -name '.env*'",
  "find . -path ./node_modules -prune -o -name '*.ts' -print",
  "sed -n 1,5p /etc/hosts",
  "sort /etc/shells",
  "jq . ~/settings.json",
  "rg TODO ~/notes",
  "du -sh ~/Library/Caches",
  "df -h /",
  "python3 /opt/tools/report.py",
  "node ../scripts/build.js",
  "realpath ..",
  "uniq -c counts.txt",
  "tar -czf - src | wc -c",
  "curl -s https://example.com/x.json | jq . -",
  "gzip -dc data.gz | head",
  "sort -u words.txt -o words.txt",
  "diff -u a.txt b.txt > patch.diff",
  "cd .github/workflows && cp ci.yml ci.bak",
  "cd docs && cat index.md > out.md",
  "head -c 100 /dev/urandom | base64",
  "openssl rand -hex 16",
  "date -u +%Y-%m-%dT%H:%M:%SZ",
  "ls -la ~",
  "mkdir -p .temp/run && cp a.txt .temp/run/",
  "git diff -- ../other/file.py",
  "chmod +x scripts/run.sh",
  "comm -12 - other.txt",
  'basename "$PWD"',
  "sed -n '/foo/,/bar/p' /etc/hosts",
  "sed -e 's/e/w/g' ../input",
  "sort -- ../input",
  "uniq -f 2 ../input",
  "xxd /tmp/input",
  "env",
]

const READERS = [
  "cat",
  "ls",
  "head",
  "tail",
  "grep",
  "egrep",
  "fgrep",
  "rgrep",
  "wc",
  "diff",
  "cmp",
  "stat",
  "file",
  "less",
  "more",
  "jq",
  "rg",
  "du",
  "df",
  "realpath",
  "readlink",
  "basename",
  "dirname",
  "cut",
  "tr",
  "nl",
  "column",
  "fold",
  "comm",
  "join",
  "paste",
  "od",
  "hexdump",
  "shasum",
  "sha256sum",
  "md5",
]
const TOOLS = ["tsc", "ruff", "uv", "mypy", "pytest", "npm", "pnpm", "yarn", "cargo", "go", "make"]

const WRITES = [
  "pytest --rootdir=.. tests",
  "uv run --project .. pytest",
  "pytest -c ../pytest.ini tests",
  "cargo build --manifest-path ../x/Cargo.toml",
  // Code and output modes have no read exemption; npx does not borrow its target's input flags.
  "awk '{print $1}' /etc/hosts",
  "tree -L 2 ..",
  "npx tsc -p ../tsconfig.json",
  "cd -'HOME[$(gi$9t push)]'",
  "find .. -delete",
  "find . -exec cp a {}/x \\;",
  "find . -execdir cp a b \\;",
  "find . -ok cp a {} \\;",
  "find . -okdir tee config \\;",
  "find . -fprint .git/config",
  "find . -fprint0 ../out",
  "find . -fprintf .ssh/config '%p'",
  "find . -fls ~/.config/list",
  "sed -i p /etc/hosts",
  "sed -ni.bak p ../in",
  "sed --in-place p .git/config",
  "sed --in-place=.bak p ../in",
  "sed 'w .git/config' input",
  "sed 'W ../out' input",
  "sed 's/a/b/w .git/config' input",
  "sed 's/a/b/e' input",
  "sed -n '1p;w .git/config' input",
  'sed -e "$script" input',
  "sed -f script input",
  "sort -o .git/config input",
  "sort -o../out input",
  "sort --output=~/.profile input",
  "uniq input .git/config",
  "uniq -f 1 input ../out",
  "xxd -r input .git/config",
  "xxd input .git/config",
  "xxd input .ssh/config",
  "find . -execdir xxd input config \\;",
  "xargs -0I{} -a input cp payload {}",
  "sort --compress-prog=sh input",
  "sort --temporary-directory=.ssh input",
  "less --log-f=.ssh/config input",
  "tree --output=.ssh/config .",
  "rg --pr='sh -c x' .",
  "npm install --prefix .ssh",
  "xxd -s 0 input ../out",
  "xxd -rinput ../out",
  "less -o .git/config input",
  "less -O.git/config input",
  "file -C -m magic",
  "tree -o .git/config ..",
  "rg --pre 'sh -c x' ..",
  "rg --hostname-bin sh ..",
  "sort --compress-program sh ../input",
  "date -s now",
  "docker run --rm -v /tmp/data:/data alpine ls /data",
  "podman run -v ../data:/data alpine ls /data",
  "docker run -v .git:/data alpine true",
  "touch .gitkeep",
  "cp .env.example .env.local",
  "ln -s ../shared shared",
  "echo $HOME:t",
  "unlisted-reader ../file",
  "unlisted-reader --config=../file",
  // These existing asks concern command-substitution provenance and unknown cwd, not input path spelling.
  "wc -l $(git ls-files '*.py')",
  "env -C web npm test",
]

describe.each([
  ["top level", (body: string) => body],
  ["saved script", savedThenRun],
] as const)("read-only operands and output exceptions: %s", (_, wrap) => {
  it.each(READS)("keeps ordinary reads quiet: %s", (command) => {
    if (wrap === savedThenRun && command.startsWith("git ")) {
      expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)?.effect).toBe("ask")
      return
    }
    expect(["other", "read-only-git"]).toContain(classifyCommand(wrap(command)).kind)
    expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)).toBeUndefined()
  })

  it.each(READERS)("allows outside input but checks redirected output for %s", (program) => {
    expect(decide(classifyCommand(wrap(`${program} ../input`)), "standard", GRANTED)).toBeUndefined()
    expect(decide(classifyCommand(wrap(`${program} ../input > .git/config`)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(WRITES)("asks for writes, command channels and unknown programs: %s", (command) => {
    expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    ["tsc", "-p tsconfig.json"],
    ["tsc", "--project=project"],
    ["ruff", "--config ../ruff.toml"],
    ["uv", "--project=project"],
    ["mypy", "--config-file ../mypy.ini"],
    ["pytest", "-c pytest.ini"],
    ["pytest", "--rootdir=project"],
    ["cargo", "--manifest-path x/Cargo.toml"],
  ])("asks for tool path options for %s %s", (program, option) => {
    expect(decide(classifyCommand(wrap(`${program} ${option}`)), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(wrap(`${program} ../out`)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(
    TOOLS.flatMap((program) =>
      ["-o", "--output", "--output-file", "--outDir", "--out", "--target-dir", "--cache-dir"].flatMap((flag) => [
        `${program} ${flag} ../out`,
        `${program} ${flag}=.git/config`,
      ]),
    ),
  )("keeps tool output flags under the write rule: %s", (command) => {
    expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "docker run -v /tmp/data:/data:ro alpine ls /data",
    "podman run --volume=../data:/data:ro alpine ls /data",
    "docker run -v data:/data alpine true",
    "podman run -v ./data:/data alpine true",
  ])("asks for containers with any mount source: %s", (command) => {
    expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(["git diff -- ../input", "git diff -- /tmp/input", "git log -- ../input", "git ls-files -- ../input"])(
    "retains the read-only Git kind for outside pathspecs: %s",
    (command) => {
      if (wrap === savedThenRun) {
        expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)?.effect).toBe("ask")
        return
      }
      expect(classifyCommand(wrap(command)).kind).toBe("read-only-git")
      expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)).toBeUndefined()
    },
  )
})

describe.each(["top level", "saved script"])("restricted tool forms: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each([
    "pip install -r requirements.txt",
    "ruff check src --config ../ruff.toml",
    "cd src && make test",
    "npm install --prefix ./web",
    "brew install jq",
  ])("asks for the complete invocation: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})
