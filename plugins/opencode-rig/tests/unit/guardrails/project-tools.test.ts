/** Project tooling asks unless its complete invocation matches the bounded safe forms. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const
const PROGRAMS = (
  "npm npx pnpm yarn bun bunx uv uvx pip pipx poetry hatch pdm rye flit cargo rustc go gem bundle make " +
  "docker podman tsc ruff mypy pytest eslint prettier vite vitest webpack rollup cmake twine brew build " +
  "nerdctl buildah conda mamba micromamba pixi rustup ninja rake pipenv tox nox composer dotnet deno vp"
).split(" ")
const SAFE = [
  "npm.exe test",
  "pytest3 tests",
  "pytest",
  "pytest tests/unit",
  "pytest -q tests",
  "pytest -x tests",
  "pytest -v tests",
  "pytest -vv tests",
  "pytest -qx tests",
  "pytest -qq tests",
  "pytest -vvv tests",
  "pytest -qxm unit tests",
  "pytest -qkunit tests",
  "pytest -k=unit tests",
  "pytest -kunit tests",
  "pytest -k 'a or b' tests",
  "pytest -m 'unit and fast' tests",
  "pytest --co tests",
  "pytest --collect-only tests",
  "pytest -q -x -v -vv -k 'a or b' -m 'unit and fast' --co --collect-only tests/x.py",
  "pytest 'tests/x.py::test_value[a-b]'",
  "ruff check",
  "ruff check src tests",
  "ruff format --check",
  "ruff format --check src",
  "ruff format --diff src tests",
  "mypy",
  "mypy src tests",
  "tsc --noEmit",
  "npm test",
  "npm run test",
  "npm run lint",
  "npm run typecheck",
  "npm run build",
  "cargo check",
  "cargo test",
  "cargo clippy",
  "CI=1 npm test",
  "cd src && ruff check .",
  "env CI=1 pytest -q tests",
  "python -m pytest",
  "python3 -m pytest",
  "python3.12 -m pytest",
  "py -m pytest",
  "nice -n 5 npm run build",
]

const UNSAFE = [
  "pytest -k",
  "pytest -m",
  "pytest -k -pplugin",
  "pytest -m --override-ini=cache_dir=project",
  "pytest --collect-only=yes",
  "pytest --co=yes",
  "pytest -c config.ini tests",
  "pytest --rootdir=project tests",
  "pytest --override-ini=cache_dir=project tests",
  "pytest -o cache_dir=project tests",
  "pytest -p plugin tests",
  "pytest -- tests",
  'pytest "$TEST_PATH"',
  "pytest tests/*",
  "pytest ../tests",
  "pytest /tmp/tests",
  "pytest .git/tests",
  "pytest key=../tests",
  "pytest key=part=../tests",
  "pytest -k unit ../tests",
  "pytest -m unit .git/tests",
  'pytest -k "$FILTER" tests',
  "ruff",
  "ruff format src",
  "ruff format --check --diff src",
  "ruff check --fix src",
  "ruff check --config config.toml src",
  "ruff check -- src",
  "ruff check -q src",
  "ruff format src --check",
  "mypy --config-file config.ini src",
  "mypy --no-incremental src",
  "tsc",
  "tsc --noEmit src/x.ts",
  "tsc --noEmit --pretty",
  "tsc --noEmit=true",
  "npm test -- --runInBand",
  "npm run test --",
  "npm run test extra",
  "npm --silent test",
  "npm run start",
  "npm exec echo safe",
  "npm explore package -- echo safe",
  "npm init package",
  "cargo test --quiet",
  "cargo check --manifest-path Cargo.toml",
  "cargo clippy -- -D warnings",
  "cargo build",
  "docker run --rm image true",
  "docker run -v /tmp:/data:ro image true",
  "podman run --mount=type=bind,source=/tmp,target=/data,ro image true",
  "cd .. && pytest tests",
  "cd .. && mypy src",
  "cd .. && ruff check src",
  "env -C .. ruff check src",
  "env npm test -- extra",
  "nice -n 5 uv run echo safe",
  "npm.cmd test",
  "npm.bat run build",
  "npm.ps1 test",
  "cargo-clippy check",
  "yarnpkg test",
  "pnpx test",
  "gmake test",
  "gnumake test",
  "bmake test",
  "docker-compose version",
  "podman-compose version",
  "bundler exec task",
  "corepack npm test",
  "python3 -Impytest tests",
  "py -W ignore -m pytest tests",
  "python --check-hash-based-pycs always -m unknown_module",
  "python -X dev -m pip install package",
  "node --run=build",
]

describe.each(["top level", "saved script"])("project tool admission: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`

  it.each(SAFE)("keeps the exact project invocation quiet: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)).toBeUndefined()
  })

  it.each(UNSAFE)("asks outside the exact project forms: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(PROGRAMS)("asks for an unlisted invocation of %s", (program) => {
    expect(decide(classifyCommand(wrap(`${program} --unlisted`)), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(wrap(`env ${program} --unlisted`)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(
    PROGRAMS.flatMap((program) =>
      ["3", "3.12", ".exe", ".cmd", ".bat", ".ps1"].map((suffix) => `${program}${suffix} --unlisted`),
    ),
  )("asks for alternate executable spellings: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(
    ["python", "python3", "python3.12", "py"].flatMap((python) =>
      [...PROGRAMS.filter((program) => program !== "pytest"), "build", "twine", "unknown_module", "package.module"].map(
        (module) => `${python} -m ${module}`,
      ),
    ),
  )("asks before running a Python module: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(
    ["-k", "-m", "-qk", "-qxm"].flatMap((option) =>
      ["@args.txt", "@../args.txt", "@/tmp/args.txt"].flatMap((value) => [
        `pytest ${option} ${value} tests`,
        `pytest ${option}${value} tests`,
      ]),
    ),
  )("asks before expanding selector argument files: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(["pytest -qz tests", "pytest -qxz tests", "pytest -qk", "pytest -xzm tests", "pytest -k -q tests"])(
    "rejects unknown cluster flags and missing selector values: %s",
    (body) => {
      expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )

  it.each(
    ["pytest", "ruff check", "ruff format --check", "ruff format --diff", "mypy"].flatMap((prefix) =>
      ["../src", "/tmp/src", "~/src", ".ssh/src", ".Git/src", "key=../src", "src/*", '"$DIR"'].map(
        (path) => `${prefix} ${path}`,
      ),
    ),
  )("asks for a path outside the project rule: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(
    "npm pnpm yarn cargo gem poetry hatch pdm rye flit go pip"
      .split(" ")
      .flatMap((program) =>
        [
          "publish",
          "push",
          "yank",
          "unpublish",
          "deprecate",
          "dist-tag add",
          "owner add",
          "config set key value",
          "config-set key value",
          "env -w KEY=value",
        ].map((args) => `${program} ${args}`),
      ),
  )("asks for registry and implicit configuration changes: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe.each(["top level", "saved script"])("project-tools guarded execution: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each([
    "pytest @args.txt",
    "pytest -q @args.txt",
    "pytest @../args.txt",
    "mypy @args.txt",
    "ruff check @args.txt",
    "pytest -k @args.txt tests",
    "pytest @/tmp/args.txt",
    "pytest @~/args.txt",
    "mypy @/tmp/args.txt",
    "pytest -k @/tmp/a tests",
    "pytest -m @../a tests",
    "python -m pip install x",
    "python3 -m pip install x",
    "python3.12 -m pip install x",
    "pip3 install x",
    "pip3.12 install x",
    "python -m pytest -p x",
    "python -m ruff check --fix",
    "python -m poetry publish",
    "python -m build",
    "python -m uv pip install x",
    "node --run x",
    "yarnpkg add x",
    "pnpx x",
    "npm.cmd install x",
    "npx.cmd x",
    "gmake install",
    "gnumake install",
    "bmake install",
    "docker-compose up",
    "cargo-clippy --fix",
    "python -m flit publish",
    "python -m hatch publish",
    "python -m pdm publish",
    "python3 -m pip config set global.index-url https://e",
    "pip3 config set global.index-url https://e",
    "yarnpkg publish",
    "npm.cmd publish",
    "python3 -m pip install --user x",
  ])("asks before loading options or executing guarded code: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})
