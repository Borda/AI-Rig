/** Every command catalogue shares program identity across wrappers, options and saved code. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

it.each([
  "sudoedit a.txt",
  "sudoedit cat",
  "sudoedit -l",
  "sudoedit2 cat a",
  "sudoedit-1.9 cat a",
  ...["sudo-rs", "gsudo", "pfexec", "dzdo", "pbrun", "please"].flatMap((program) => [
    `${program} mytool`,
    `${program} tee out`,
    `/usr/bin/${program} mytool`,
    `${program}.exe mytool`,
  ]),
])("requires privilege admission for elevator identities: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", { status: "granted", path: "approval.json" })?.effect).toBe("ask")
})

it.each([
  "sudo-rs cat a",
  "gsudo cat a",
  "sudo2 cat a",
  "sudo-1.9 cat a",
  "sudo /usr/bin/[ -f x ]",
  "sudo /bin/test -d x",
])("keeps modelled elevator reader targets quiet: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", { status: "granted", path: "approval.json" })).toBeUndefined()
})

const GRANTED = { status: "granted", path: "approval.json" } as const
it.each(["pkexec", "pkexec --user root", "run0", "run0 --shell", "sudo run0"])(
  "asks for privilege wrappers without a positive target: %s",
  (body) => {
    expect(decide(classifyCommand(body), "standard", GRANTED)?.effect).toBe("ask")
  },
)

it.each([
  "rtx install node",
  "nix-shell",
  "nix-build",
  "nix-channel --update",
  "nix-store --add input",
  "nix-collect-garbage",
  "nix-instantiate input",
])("asks for manager aliases: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", GRANTED)?.effect).toBe("ask")
})

describe.each(["sudo", "doas", "/usr/bin/SUDO.exe", "sudo doas"])("privilege target admission: %s", (wrapper) => {
  it.each(["mytool build", "apk add package", "python3 script.py", "sudo mytool build"])(
    "asks for targets outside positive lists: %s",
    (target) => {
      expect(decide(classifyCommand(`${wrapper} ${target}`), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
  it.each(["ls", "cat input", "git status", "npm test", "time ls"])(
    "keeps positively admitted targets quiet: %s",
    (target) => {
      expect(decide(classifyCommand(`${wrapper} ${target}`), "standard", GRANTED)).toBeUndefined()
    },
  )
})
it("keeps an unlisted unprivileged program quiet", () => {
  expect(decide(classifyCommand("mytool build"), "standard", GRANTED)).toBeUndefined()
})
it.each(
  [
    "python",
    "python3",
    "python3.12",
    "python3.13t",
    "python3.8m",
    "python3.12d",
    "python3.2u",
    "python3.2mu",
    "python3.2dm",
    "python3.2du",
    "python3.2dmu",
    "python3.13td",
    "python3-dbg",
    "python3.12-dbg",
    "python3-debug",
    "python3.12-debug",
    "pythonw",
    "python3w",
    "python3.12w",
    "py",
    "pypy",
    "pypy3",
    "pypy3.10",
    "graalpy",
    "pyston",
  ].flatMap((program) => [program, `${program}.exe`]),
)("asks for package installation through interpreter spelling: %s", (program) => {
  expect(decide(classifyCommand(`${program} -m pip install x`), "standard", GRANTED)?.effect).toBe("ask")
})
it.each(["python3-config", "python3-pip", "python-config", "python3.12-config"])(
  "does not borrow interpreter rules for unrelated executables: %s",
  (program) => {
    expect(decide(classifyCommand(`${program} -c input`), "standard", GRANTED)).toBeUndefined()
    expect(decide(classifyCommand(`${program} -m pip install x`), "standard", GRANTED)).toBeUndefined()
  },
)
it.each(["su root mytool build", "sudo su root -c 'mytool build'"])("asks for privilege shell targets: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", GRANTED)?.effect).toBe("ask")
})
const ASK = [
  "node node_modules/eslint/bin/eslint.js --fix src",
  "node node_modules/typescript/lib/tsc.js --outDir output",
  "php8.3 -r 1",
  "tsx4 -e 1",
  "ts-node10 -e 1",
  "nodejs20 -e 1",
  "gawk-5.3 --eval=1",
  "xargs -I{} rg -e -- {} < list.txt",
  ...["rg x", "less", "file", "diff input"].flatMap((target) => [
    `xargs -a list.txt ${target}`,
    `xargs ${target} < list.txt`,
    `cat list.txt | xargs ${target}`,
    `xargs ${target} <<'INPUT'\n--pre=tool\nINPUT`,
  ]),
  ...[
    "perl -e",
    "perl -E",
    "ruby -e",
    "php -r",
    "lua -e",
    "Rscript -e",
    "R -e",
    "julia -e",
    "tsx -e",
    "ts-node -e",
    "ipython -c",
    "osascript -e",
  ].map((prefix) => `${prefix} 1`),
  "awk 'BEGIN{system(\"true\")}'",
  "gawk '{print > \"../output\"}' input",
  "mawk 'BEGIN{print 1}'",
  "busybox awk 'BEGIN{print 1}'",
  ...["py.test", "pytest-3", "pytest-3.12", "pytest.exe"].map((tool) => `${tool} --basetemp=src`),
  "python3 .venv/bin/pytest --basetemp=src",
  "python3 .venv/bin/pip install package",
  "node node_modules/npm/bin/npm-cli.js publish",
  "node node_modules/.bin/tsc --outDir ../output",
  "pip3.13t install package",
  "python3.13t -m pip install package",
  "colima nerdctl run image",
  "limactl shell default nerdctl run image",
  ...[
    "apt",
    "apt-get",
    "dnf",
    "yum",
    "port",
    "nix-env",
    "nix profile",
    "scoop",
    "winget",
    "choco",
    "nodenv",
    "goenv",
    "proto",
    "pkgx",
    "easy_install",
    "anacron",
    "fcrontab",
  ].flatMap((tool) => [`${tool} install package`, `sudo ${tool} install package`]),
  "patch -p1 < input.diff",
  "patch -i input.diff",
  "patch input input.diff",
  "gtimeout 5 npm install x",
  "gnohup npm install x",
  "genv npm install x",
  "gnice npm install x",
  "gstdbuf -o0 npm install x",
  "gtime npm install x",
  "gxargs npm install x",
  "/opt/homebrew/bin/gtimeout 5 npm install x",
  "pip-3 install x",
  "pip-3.12 install x",
  "pypy3 -m pip install x",
  "pypy -m pip install x",
  "graalpy -m pip install x",
  "mingw32-make install",
  "nmake install",
  "podman-remote run x",
  "docker-buildx build .",
  "finch run x",
  "nerdctl.lima run x",
  "prek install",
  "env npm install x",
  "/usr/bin/env npm install x",
  "/usr/bin/env npm publish",
  "/usr/bin/env pip install x",
  "/usr/bin/env python3 -m pip install x",
  "/usr/bin/env -S 'npm publish'",
  "/usr/bin/env FOO=1 npm publish",
  "/usr/bin/nohup npm publish",
  "/usr/bin/nice npm publish",
  "/usr/bin/time npm publish",
  "/usr/bin/timeout 5 npm publish",
  "/usr/bin/sudo npm install -g x",
  "/usr/bin/xargs npm publish",
  "/usr/bin/command npm publish",
  "./env npm publish",
  "/usr/bin/env pre-commit install",
  "/usr/bin/env launchctl load x.plist",
  "/usr/bin/env crontab x.txt",
  "/usr/bin/env bash x.sh",
  "/usr/bin/env sh -c 'touch ../x'",
  "/usr/bin/env git push",
  "/usr/bin/env touch ../x",
  "/usr/bin/nohup touch ../x",
  "/usr/bin/env cargo publish",
  "/usr/bin/env docker run x",
  "/usr/bin/env watch 'touch ../x'",
  "/usr/bin/caffeinate npm publish",
  "/usr/bin/env -i npm publish",
  "/usr/bin/env node --run x",
  "/usr/bin/env yarnpkg publish",
  "ENV npm publish",
  "Env npm publish",
  "NOHUP npm publish",
  "env.exe npm publish",
  "nohup.exe npm publish",
  "/usr/bin/env watch 'touch ~/Library/LaunchAgents/x.plist'",
  "/usr/bin/env tmux new -d 'touch ../x'",
  "/usr/bin/env at -f x.sh now",
  "/usr/bin/env expect -c 'spawn true'",
  "/usr/bin/env hyperfine 'touch ../x'",
  "/usr/bin/env parallel ::: 'touch ../x'",
  "/usr/bin/env screen -dm true",
  "/usr/bin/nohup npm install x",
  "/usr/bin/nohup watch 'touch ../x'",
  "/usr/bin/nice pip install x",
  "/usr/bin/time cargo publish",
  "/usr/bin/xargs cargo publish",
  "/usr/bin/sudo cargo publish",
  "/usr/bin/env husky",
  "/usr/bin/env lefthook install",
  "/usr/bin/env defaults write com.x k v",
  "/usr/bin/env systemctl --user enable x",
  "/usr/bin/env python -c pass",
  "/usr/bin/env node -e 1",
  "/usr/bin/env pytest -p x",
  "/usr/bin/env ruff check --fix",
  "/usr/bin/env pytest @args.txt",
  "/usr/bin/env eslint --fix .",
  "/usr/bin/env make install",
  "/usr/bin/env go run x.go",
  "/usr/bin/env uv pip install x",
  "/usr/bin/env npx x",
  "/usr/bin/env pip3.12 install x",
  "ENV npm install x",
  "Env pip install x",
  "NICE cargo publish",
  "/usr/bin/ENV npm install x",
  "xargs -a list.txt rm",
  "xargs -a list.txt touch",
  "xargs --arg-file=list.txt rm",
  "xargs rm < list.txt",
  "xargs -0 rm < list.txt",
  "printf '%s\\n' ../x > list.txt; xargs -a list.txt touch",
  "printf '%s\\n' ../x > list.txt && xargs touch < list.txt",
  "nohup xargs -a list.txt rm",
  "timeout 5 xargs rm < list.txt",
  "printf '%s\\n' ../x > list.txt && xargs rm < list.txt",
  "xargs touch < list.txt",
  "xargs mkdir -p < list.txt",
  "xargs cp y.txt < list.txt",
  "xargs ln -s y.txt < list.txt",
  "xargs chmod +x < list.txt",
  "xargs mv -t d < list.txt",
  "/usr/bin/sudo -s",
  "/usr/bin/sudo -i",
  "/usr/bin/doas -s",
  "find . -exec /usr/bin/env npm install x \\;",
  "direnv allow",
  "mise trust",
  "mise install",
  "asdf install",
  "gcc x.c -Wl,@r.rsp",
  "clang x.c -Wl,@r.rsp",
  "cc -Wl,@r.rsp x.c",
]
const QUIET = [
  ...["py.test", "pytest-3", "pytest-3.12", "pytest.exe"].map((tool) => `${tool} -q`),
  ".venv/Scripts/python.exe -m pytest -q",
  "python3 .venv/bin/pytest -q",
  "/usr/bin/time ls",
  "/usr/bin/time cat input",
  ...["cat", "ls", "tail", "stat", "sort"].map((tool) => `${tool} @input`),
  "find -iname '@*'",
  "cd node_modules && ls @types",
  ...["python3.12", "pypy3", "nodejs"].map((tool) => `${tool} ../script`),
  "xargs -a list.txt rg -- x",
  "grep -rn '@pytest.fixture' tests",
  "grep -rn @Override src",
  "rg '@decorator' src",
  "rg -n '@types/node' package.json",
  "grep -c @ y.txt",
  "git log @{u}..HEAD",
  "git diff @{upstream}",
  "git rev-parse @",
  "git show @~1",
  "git log --oneline @{1}..@",
  "find . -name '@*'",
  "head @x",
  "diff @a @b",
  "wc -l @x",
  "python3 -m pytest -q",
  ".venv/bin/python -m pytest tests",
  "python -m pytest tests",
]
const spellings = (name: string): string[] => [
  name,
  name.toUpperCase(),
  `/usr/bin/${name}`,
  `./bin/${name}`,
  `./node_modules/.bin/${name}`,
  `${name}.exe`,
  `${name}.cmd`,
  `${name}.bat`,
  `${name}.ps1`,
  `/opt/bin/${name.toUpperCase()}.EXE`,
]

describe.each(["top level", "saved script"])("program identity admission: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`

  it.each(ASK)("asks before guarded execution or indirect operands: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(QUIET)("keeps established literal data and safe test forms quiet: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)).toBeUndefined()
  })

  it.each([
    "prek install",
    "direnv allow",
    "direnv trust",
    "mise trust",
    "mise install",
    "mise use node",
    "mise activate",
    "asdf install",
    "asdf plugin add tool",
    "asdf global tool",
    "asdf local tool",
    "nvm",
    "fnm",
    "pyenv install tool",
    "pyenv global tool",
    "pyenv local tool",
    "rbenv install tool",
    "rbenv global tool",
    "rbenv local tool",
    "volta install tool",
    "volta pin tool",
    "rustup",
    "sdk",
    "corepack enable",
    "corepack prepare",
    "corepack npm install package",
    "corepack pnpm install package",
    "nerdctl run image",
    "lima nerdctl run image",
    "gcp input ../output",
    "xargs touch",
    "xargs rm",
    "xargs cp input",
    "xargs sed -n p",
    "xargs sort",
    "xargs uniq",
    "xargs xxd",
    "xargs find .",
    "printf x | xargs chmod +x",
    "xargs touch <<'INPUT'\n../outside\nINPUT",
    "xargs touch <<< '../outside'",
    "unknown-tool key=@options",
    "unknown-tool -Wl,@options",
    "cat -Wl,@options",
    "grep -f @patterns src",
    "rg -f @patterns src",
    "git commit -m @options",
    "git log --format=@options",
    "git grep -f @patterns",
    "git grep -e @pattern --output=@file",
  ])("asks for installers and unjudged argument sources: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(["cat", "echo", "grep pattern", "ls"])("keeps read-only xargs operands quiet: %s", (operand) => {
    expect(decide(classifyCommand(wrap(`xargs ${operand}`)), "standard", GRANTED)).toBeUndefined()
    expect(decide(classifyCommand(wrap(`xargs -a list.txt ${operand}`)), "standard", GRANTED)).toBeUndefined()
    expect(decide(classifyCommand(wrap(`xargs ${operand} < list.txt`)), "standard", GRANTED)).toBeUndefined()
    expect(decide(classifyCommand(wrap(`printf 'file' | xargs ${operand}`)), "standard", GRANTED)).toBeUndefined()
  })

  it.each(["python", "python3", "python3.12", ".venv/bin/python"])(
    "checks pytest module flags through %s",
    (python) => {
      expect(decide(classifyCommand(wrap(`${python} -m pytest -qxm unit tests`)), "standard", GRANTED)).toBeUndefined()
      expect(decide(classifyCommand(wrap(`${python} -m pytest -p plugin`)), "standard", GRANTED)?.effect).toBe("ask")
      expect(decide(classifyCommand(wrap(`${python} -m pytest @args`)), "standard", GRANTED)?.effect).toBe("ask")
      expect(decide(classifyCommand(wrap(`${python} -m unknown_module`)), "standard", GRANTED)?.effect).toBe("ask")
      expect(decide(classifyCommand(wrap(`${python} -m ruff check`)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )

  it.each(["pypy", "pypy3", "graalpy", "python3.12", "nodejs", "node20"])(
    "keeps interpreter script inputs independent of spelling: %s",
    (program) => {
      expect(decide(classifyCommand(wrap(`${program} /tmp/script`)), "standard", GRANTED)).toBeUndefined()
    },
  )

  it.each(["env", "nohup", "nice", "time", "xargs", "sudo", "doas"])(
    "keeps wrapper classification independent of the spelling of %s",
    (name) => {
      expect(spellings(name).map((word) => classifyCommand(`${word} git push`).kind)).toEqual(
        spellings(name).map(() => "remote-mutation"),
      )
    },
  )

  it.each([
    "git grep @pattern",
    "git grep -e @pattern",
    "grep -e @pattern src",
    "rg -e @pattern src",
    "git diff @{u}",
    "git log @~1",
    "head @file",
    "diff @before @after",
    "wc @file",
    "find . -name '@*'",
    "echo @literal",
    "printf '%s' @literal",
    "cat input",
    "cp input output",
  ])("keeps literal read patterns and existing controls quiet: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)).toBeUndefined()
  })

  it.each(
    [
      ["env", "npm install package"],
      ["nohup", "cargo publish"],
      ["nice", "pip install package"],
      ["timeout", "5 npm install package"],
      ["stdbuf", "-o0 npm install package"],
      ["time", "cargo publish"],
      ["xargs", "touch"],
      ["sudo", "-s"],
      ["doas", "-s"],
      ["watch", "true"],
      ["pytest", "-p plugin"],
      ["cat", "--unknown"],
      ["tar", "--checkpoint-action=exec=echo"],
      ["ssh", "-o ProxyCommand=echo host"],
      ["rm", "-rf build"],
    ].flatMap(([name, args]) => spellings(name!).map((word) => `${word} ${args}`)),
  )("uses the same spellings in every command catalogue: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(wrap(`env ${body}`)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(spellings("rm"))("retains nested destructive identity for %s", (word) => {
    expect(classifyCommand(`unknown-wrapper ${word} -rf build`).kind).toBe("destructive-local")
    expect(decide(classifyCommand(wrap(`unknown-wrapper ${word} -rf build`)), "standard", GRANTED)?.effect).toBe("ask")
  })
})
