/** Catalogued wrapper programs, modifiers and command-string carriers keep the guarded program visible. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

it.each([
  ...["sudo", "doas", "pkexec", "run0", "gosu root", "systemd-run", "runuser -u root --"].flatMap((wrapper) =>
    ["sort -o out a", "uniq a b", "find . -fprint out", "strace -o out ls", "flock l cat x", "xxd -r a", "xxd a b"].map(
      (target) => `${wrapper} ${target}`,
    ),
  ),
  "systemd-run mytool",
  "systemd-run mytool --user",
  "systemd-run --description --user mytool",
  "systemd-run --user --system mytool",
  "systemd-run --uid=0 mytool",
  "systemd-run -t mytool",
  "sudo systemd-run --user mytool",
  "xargs sudo cat",
  "xargs env cat",
  "xargs nohup cat",
  "xargs timeout 5 cat",
  "su root cat",
  "ipython -m pip install package",
])("asks for privileged writes and appended wrapper operands: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", { status: "granted", path: "approval.json" })?.effect).toBe("ask")
})

it.each([
  "sudo xargs cat",
  "xargs cat",
  "systemd-run --user mytool",
  "systemd-run --system --user mytool",
  "sudo sort input",
  "sudo uniq input",
  "sudo find . -print",
  "sudo strace ls",
  "xargs printf -- '%s\\n'",
  "time -l ls",
  "time -h ls",
  "time -lp ls",
  "/usr/bin/time -l ls",
  "command time -l ls",
])("keeps bounded read forms behind wrappers quiet: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", { status: "granted", path: "approval.json" })).toBeUndefined()
})

it.each([
  "xargs -I{} rg x -- {}",
  "xargs -I{} file -- {}",
  "xargs -I{} diff input -- {}",
  "xargs printf '%d'",
  "xargs -I{} printf '%f' {}",
  "xargs -alist.txt cat",
  ...["-a", "--append", "-q", "--quiet", "--verbose", "--portability"].map((flag) => `time ${flag} ls`),
  "command time ls",
])("keeps bounded external reader wrappers quiet: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", { status: "granted", path: "approval.json" })).toBeUndefined()
})
it.each([
  "xargs -I{} r{}g x -- input",
  "xargs printf -v",
  "printf '%d' dynamic",
  "time --quiet npm install x",
  "command time cp input ../output",
])("preserves wrapper and builtin restrictions: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", { status: "granted", path: "approval.json" })?.effect).toBe("ask")
})

const GRANTED = { status: "granted", path: "approval.json" } as const

describe.each(["", "env ", "sudo ", "sudo env "])("literal assignments after %s", (prefix) => {
  it.each(["CI=1", "TZ=UTC", "CI='1'", "CI='hello world'"])(
    "admits the same literal name and value: %s",
    (assignment) => {
      expect(decide(classifyCommand(`${prefix}${assignment} git log`), "standard", GRANTED)).toBeUndefined()
    },
  )
  it.each(["'CI=1'", "C\\I=1", "'CI'=1", "CI=$VALUE", "PAGER=less"])(
    "asks for a nonliteral name or unsafe value: %s",
    (assignment) => {
      expect(decide(classifyCommand(`${prefix}${assignment} git log`), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})

describe.each(["top level", "saved script"])("elevation admission: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each(
    [
      "run0",
      "systemd-run",
      "machinectl",
      "pkexec",
      "su",
      "gosu",
      "pfexec",
      "dzdo",
      "pbrun",
      "please",
      "pleaseedit",
      "doasedit",
      "sudoedit",
      "super",
      "really",
      "runas",
      "ksu",
    ].flatMap((program) =>
      ["cat a", "--help", "-u root cat a", "cat a --user"].map((target) => `${program} ${target}`),
    ),
  )("asks for every non-admitting elevator: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each([
    "run0 --setenv=CI=1 cat a",
    "run0 --property EnvironmentFile=env.txt cat a",
    "systemd-run -p ExecStartPost=mytool cat a",
    "systemd-run -p Environment= cat a",
    "systemd-run -p StandardOutput=file:out cat a",
    "sudo systemd-run -p ExecStartPre=true cat a",
    "sudo systemd-run --user cat a",
    "sudo run0 cat a",
    "pleaseedit a.txt",
    "doasedit a.txt",
    "machinectl shell root@.host",
    "machinectl login",
    "sudo2 tee out",
    "sudo-1.9 tee out",
  ])("asks for indirect elevation and writes: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each(["sudo", "doas", "sudo-rs", "gsudo", "/usr/bin/SUDO.exe"])(
    "admits bounded rights listings for %s",
    (program) => {
      expect(decide(classifyCommand(wrap(`${program} -n -l`)), "standard", GRANTED)).toBeUndefined()
      expect(decide(classifyCommand(wrap(`${program} -nl`)), "standard", GRANTED)).toBeUndefined()
      expect(decide(classifyCommand(wrap(`${program} -l -u root`)), "standard", GRANTED)).toBeUndefined()
    },
  )
})

it.each([
  ...["run0 --setenv", "systemd-run --setenv", "systemd-run -E"].flatMap((option) =>
    [" PAGER=less", "=GIT_PAGER=less", " FOO=1", "=CI=$VALUE"].map((value) => `${option}${value} git log`),
  ),
  "run0 --property=Environment=PAGER=less git log",
  "systemd-run -p Environment=FOO=1 cat a",
  "systemd-run -p 'Environment=CI=1 PAGER=less' git log",
  "systemd-run --property='Environment=CI=1 FOO=1' cat a",
  ...["--machine=root@.host", "-M root@", "-Mroot@", "--uid=0", "--gid 0"].flatMap((option) => [
    `systemd-run --user ${option} mytool`,
    `systemd-run ${option} --user mytool`,
  ]),
  "sudo -v",
  "sudo -k",
  "sudo",
  "sudo -lv",
  "sudo test -v NAME",
  "sudo [ -t dynamic ]",
  "env PAGER=less git log",
  "systemd-run --user -E PAGER=less cat a",
  "systemd-run --user -E PAGER=less tee out",
  "systemd-run --user --setenv=LD_PRELOAD=./payload.so cat a",
  "systemd-run --user --property=Environment=LD_PRELOAD=./payload.so cat a",
])("asks for unsafe wrapper state and builtin operands: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", GRANTED)?.effect).toBe("ask")
})

it.each([
  "sudo test -d x",
  "sudo [ -f x ]",
  "sudo -l",
  "sudo -ll",
  "sudo --list",
  "sudo -l -U bob",
  "env CI=1 git status",
  "sudo env CI=1 git log",
  "env CI='1' git log",
  "env CI='hello world' git status",
  "sudo env CI='hello world' git log",
])("keeps admitted wrapper state and builtin reads quiet: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", GRANTED)).toBeUndefined()
})

describe.each(["top level", "saved script"])("wrapper command-body boundaries: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each([
    "xargs",
    "watch",
    "parallel",
    "entr",
    "nohup",
    "timeout 5",
    "time",
    "nice",
    "env",
    "command",
    "arch -arm64",
    "xcrun",
    "sudo",
    "doas",
    "setsid",
    "stdbuf -o0",
    "ionice -c 3",
    "script -c",
    "unbuffer",
    "expect -c",
    "tmux new -d",
    "screen -dm",
    "hyperfine",
  ])("asks before guarded shell code through %s", (wrapper) => {
    expect(decide(classifyCommand(wrap(`${wrapper} sh -c 'touch ../outside'`)), "standard", GRANTED)?.effect).toBe(
      "ask",
    )
    expect(decide(classifyCommand(wrap(`${wrapper} npm install package`)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "xargs bash -c '{ echo safe; }'",
    "bash -c '{ echo safe; }'",
    "bash --unknown -c 'echo safe'",
    "unknown-wrapper sh -c 'touch ../outside'",
    "unknown-wrapper sh -Cc 'touch ../outside'",
    "unknown-wrapper bash -Cec 'touch ../outside'",
  ])("asks when a command body cannot be fully judged: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe.each(["top level", "saved script"])("runner operand classification: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  const runners = [
    "uv run",
    "uv tool run",
    "npm exec",
    "npm x",
    "pnpm exec",
    "pnpm dlx",
    "yarn exec",
    "yarn dlx",
    "bunx",
    "pipx run",
    "poetry run",
    "hatch run",
    "pdm run",
    "rye run",
    "uvx",
    "npx",
    "env",
    "nice",
    "time",
    "command",
  ]
  it.each(
    runners.flatMap((runner) =>
      ["rm -rf output", "git push", "sh -c 'git push'", "find . -delete", "cp a .ssh/config"].map(
        (target) => `${runner} ${target}`,
      ),
    ),
  )("asks for a guarded command after %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each([
    "uv run --with rm rm -rf output",
    "uv run --with=rm rm -rf output",
    "uv --project project run rm -rf output",
    "uv tool run --from rm rm -rf output",
    "npm exec --package rm -- rm -rf output",
    "npm x -p rm -- rm -rf output",
    "uv run --unknown value echo safe",
    "npm exec --unknown value echo safe",
    "uv run --with",
    "npm exec --package",
    "uv run -p python rm -rf output",
    "uv tool --quiet run rm -rf output",
    "uv tool -q run rm -rf output",
    "uv tool --unknown run rm -rf output",
    "uv tool --project ../project run echo safe",
    "uv tool --project=../project run echo safe",
  ])("never mistakes an option value for the command: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
  const optionCases = [
    ["uv", "--project= --directory= --offline --quiet -q --verbose -v", "run "],
    ["uv tool", "--project= --directory= --offline --quiet -q --verbose -v", "run "],
    ["npm", "--prefix= --workspace= -w= --offline --quiet -q --verbose -v", "exec "],
    ["uv run", "--with= --project= --directory= --no-sync --locked --frozen --offline -q --quiet -v --verbose", ""],
    ["uv tool run", "--from= --with= --offline -q --quiet -v --verbose", ""],
    ["npm exec", "--package= -p= --yes -y --offline --quiet -q", ""],
    ["npm x", "--package= -p= --yes -y --offline --quiet -q", ""],
    ["npx", "--package= -p= --yes -y --offline --quiet -q", ""],
  ].flatMap(([prefix, options, subcommand]) =>
    options!.split(" ").map((entry) => [prefix!, entry, subcommand!] as const),
  )
  it.each(optionCases)("asks for package runner options %s %s", (prefix, entry, subcommand) => {
    const option = entry.endsWith("=") ? `${entry.slice(0, -1)} package` : entry
    expect(
      decide(classifyCommand(wrap(`${prefix} ${option} ${subcommand}echo safe`)), "standard", GRANTED)?.effect,
    ).toBe("ask")
    expect(
      decide(classifyCommand(wrap(`${prefix} ${option} ${subcommand}rm -rf output`)), "standard", GRANTED)?.effect,
    ).toBe("ask")
  })
  it.each(optionCases.filter(([, entry, subcommand]) => entry.endsWith("=") && !subcommand))(
    "requires the value and a real program after %s %s",
    (prefix, entry, subcommand) => {
      expect(
        decide(classifyCommand(wrap(`${prefix} ${entry.slice(0, -1)} ${subcommand}`)), "standard", GRANTED)?.effect,
      ).toBe("ask")
    },
  )
})

const consumers = ["BODY", "cat > x.sh <<'EOF'\nBODY\nEOF", "bash x.sh <<'EOF'\nBODY\nEOF"] as const
/** Saved data no same-text shell runs keeps its existing verdict; elsewhere the grammar gate applies. */
const SAVED_ONLY = "cat > x.sh <<'EOF'\nBODY\nEOF"

describe.each(["coproc", "noglob", "nocorrect", "repeat 1", "-"])("modifier %s", (modifier) => {
  it.each(["BODY", "cat > x.sh <<'EOF'\nBODY\nEOF", "bash x.sh <<'EOF'\nBODY\nEOF"])(
    "keeps top-level and body verdicts aligned in %s",
    (consumer) => {
      expect(
        decide(classifyCommand(consumer.replace("BODY", `${modifier} git push`)), "standard", GRANTED)?.effect,
      ).toBe("ask")
    },
  )

  it("admits a persisted safe command", () => {
    expect(classifyCommand(`cat > x.sh <<'EOF'\n${modifier} echo safe\nEOF`).kind).toBe("other")
  })
})

describe("catalogued command-string carriers", () => {
  const carriers = [
    "flock lock -c BODY",
    "flock lock --command=BODY",
    "flock -c BODY lock",
    "flock -xc BODY lock",
    "flock -xcBODY lock",
    "su -lcBODY root",
    "runuser -lcBODY root",
    "script -qcBODY /dev/null",
    "su -cBODY root",
    "su --command=BODY root",
    "runuser -c BODY root",
    "runuser -cBODY root",
    "runuser --command=BODY root",
    "sg group -c BODY",
    "sg group -cBODY",
    "script -cBODY /dev/null",
    "script --command=BODY /dev/null",
    "env -S BODY",
    "env -SBODY",
    "env --split-string=BODY",
  ]

  it.each(
    carriers.flatMap((carrier) =>
      ["BODY", "cat > x.sh <<'EOF'\nBODY\nEOF"].map((consumer) => consumer.replace("BODY", carrier)),
    ),
  )("guards a fragmented executable in %s", (carrier) => {
    expect(decide(classifyCommand(carrier.replace("BODY", "\"r'm' -rf build\"")), "standard", GRANTED)?.effect).toBe(
      "ask",
    )
  })

  // Grammar gate: command-string, split-string and launcher options leave the grammar, so data in
  // these command strings asks.
  it.each(carriers)("asks for data in the command string of %s", (carrier) => {
    expect(decide(classifyCommand(carrier.replace("BODY", "'printf rm -rf build'")), "standard", GRANTED)?.effect).toBe(
      "ask",
    )
  })

  it.each([
    "xargs printf 'rm -rf build'",
    "setsid printf 'rm -rf build'",
    "timeout 5 printf 'rm -rf build'",
    "nice -n 5 printf 'rm -rf build'",
  ])("admits wrapper argv data: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
  })

  it("asks for unmodelled watch operands even in exec mode", () => {
    expect(decide(classifyCommand("watch --exec printf 'rm -rf build'"), "standard", GRANTED)?.effect).toBe("ask")
  })

  // Grammar gate: launcher wrappers and implicit-shell options leave the grammar, so these ask.
  it.each([
    "nsenter --target 1 printf 'rm -rf build'",
    "chroot /root printf 'rm -rf build'",
    "sudo -s printf 'rm -rf build'",
    "doas -s printf 'rm -rf build'",
  ])("asks for launcher wrapper argv data: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "sudo -s",
    "sudo -i",
    "sudo --shell",
    "sudo --login",
    "doas -s",
    "sudo -su root",
    "sudo -iu root",
    "doas -su root",
  ])("guards input reaching a wrapper's implicit shell: %s", (wrapper) => {
    expect(decide(classifyCommand(`${wrapper} < f`), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(`printf safe | ${wrapper}`), "standard", GRANTED)?.effect).toBe("ask")
    // Grammar gate: implicit-shell options leave the grammar, so the program asks too.
    expect(decide(classifyCommand(`${wrapper} echo safe`), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("wrapper program operands", () => {
  it.each(["nsenter --target 1", "chroot /root"])("guards input to the default shell in %s", (wrapper) => {
    expect(decide(classifyCommand(`${wrapper} < f`), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each([
    "env -i",
    "nohup",
    "sudo -u root",
    "timeout 5",
    "nice -n 5",
    "ionice -c 3",
    "flock lock",
    "setsid",
    "stdbuf -o L",
    "doas -u root",
    "strace -o trace.log",
    "xargs -n 1",
  ])("retains executable relevance after %s", (wrapper) => {
    expect(decide(classifyCommand(`cat > x.sh <<'EOF'\n${wrapper} git push\nEOF`), "standard", GRANTED)?.effect).toBe(
      "ask",
    )
    expect(classifyCommand(`${wrapper} printf 'rm -rf build'`).kind).toBe("other")
  })

  // Grammar gate: builtin wrappers (command, exec, builtin) and launcher wrappers (nsenter,
  // chroot, runuser) leave the grammar, so their top-level data asks.
  it.each(["command -p", "exec -a label", "builtin", "nsenter --target 1", "chroot /root", "runuser -u root --"])(
    "retains executable relevance and asks at top level after %s",
    (wrapper) => {
      expect(decide(classifyCommand(`cat > x.sh <<'EOF'\n${wrapper} git push\nEOF`), "standard", GRANTED)?.effect).toBe(
        "ask",
      )
      expect(decide(classifyCommand(`${wrapper} printf 'rm -rf build'`), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})

describe.each(consumers)("catalogued wrappers in %s", (consumer) => {
  it.each([
    "flock --comm='git push' lock",
    "flock --comm 'git push' lock",
    "su --comm='git push' root",
    "runuser --comm 'git push' root",
    "script --comm='git push' /dev/null",
    "su --session-comm='git push' root",
    "runuser --session-c 'git push' root",
    "flock --comm='rm -rf build' lock",
    "sudo --uncatalogued role git push",
    "sudo --uncatalogued true",
    "flock --uncatalogued value lock git push",
    "env --uncatalogued true",
    "env X=git true",
    'sudo -u "${git}" true',
    "env X=git echo hi",
    "env X=git true; echo hi",
    "sudo -Znc 'echo hi'",
  ])("asks for command prefixes or uncatalogued options: %s", (body) => {
    expect(decide(classifyCommand(consumer.replace("BODY", body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "flock --timeout 5 lock",
    "flock -w 5 lock",
    "sudo -r role",
    "sudo -t type",
    "sudo --role=role",
    "sudo --type type",
    "strace -a 40",
    "strace -b execve",
    "strace -e trace=execve",
    "doas -a style",
    "env -P bin",
  ])("retains the program after value-taking options: %s", (wrapper) => {
    expect(decide(classifyCommand(consumer.replace("BODY", `${wrapper} git push`)), "standard", GRANTED)?.effect).toBe(
      "ask",
    )
    expect(classifyCommand(consumer.replace("BODY", `${wrapper} echo hi`)).kind).toBe("other")
  })

  it.each(["flock lock echo hi", "sudo -n true", "echo 'run git status to check'"])(
    "admits known wrapper options or prose: %s",
    (body) => {
      expect(classifyCommand(consumer.replace("BODY", body)).kind).toBe("other")
    },
  )

  // Time options retain the wrapped program and validate any output path.
  it.each(["time -f %e", "time -o timing.log", "time --format=%e", "time --output timing.log"])(
    "retains the program after value-taking options of time: %s",
    (wrapper) => {
      expect(
        decide(classifyCommand(consumer.replace("BODY", `${wrapper} git push`)), "standard", GRANTED)?.effect,
      ).toBe("ask")
      const safe = classifyCommand(consumer.replace("BODY", `${wrapper} echo hi`))
      expect(decide(safe, "standard", GRANTED)).toBeUndefined()
    },
  )

  it("keeps time-wrapped reads quiet", () => {
    const result = classifyCommand(consumer.replace("BODY", "time ls"))
    expect(decide(result, "standard", GRANTED)).toBeUndefined()
  })
})

describe("catalogued scheduling and terminal wrappers", () => {
  it.each([
    "caffeinate gi$9t push",
    "caffeinate -i git push",
    "systemd-run --user gi$9t push",
    "systemd-run --user --scope git push",
    "chrt 10 gi$9t push",
    "chrt -f 10 git push",
    "taskset 1 gi$9t push",
    "taskset -c 0 git push",
    "unbuffer gi$9t push",
    "unbuffer git push",
    "caffeinate -z make",
  ])("asks for %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "caffeinate -i make",
    "systemd-run --user --scope make",
    "chrt 10 make",
    "taskset -c 0 make",
    "unbuffer make test",
  ])("asks for project execution through scheduling wrappers: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe.each(["top level", "saved script"])("wrappers guarded execution: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each([
    "xargs bash -c 'touch ../x'",
    "xargs -I{} bash -c 'touch ../x'",
    "xargs -I {} bash -c 'touch ../x'",
    "xargs -I{} sh -c 'touch ../x'",
    "xargs -n1 bash -c 'touch ../x'",
    "xargs -0 bash -c 'touch ../x'",
    "xargs -I{} bash -c 'npm install x'",
    "xargs -I{} bash -c 'touch ~/Library/LaunchAgents/x.plist'",
    "watch 'npm install x'",
    "watch -n 1 'touch ~/Library/LaunchAgents/x.plist'",
    "xargs -a list.txt bash -c 'touch ../x'",
    "xargs --arg-file=list.txt bash -c 'touch ../x'",
    "xcrun bash -c 'touch ../x'",
    "arch -arm64 bash -c 'touch ../x'",
    "arch -arm64 bash -c 'touch ~/x'",
    "arch -arm64 sh -c 'touch ../x'",
    "arch -arm64 zsh -c 'touch ../x'",
    "arch -arm64 bash -c 'touch ~/Library/LaunchAgents/x.plist'",
    "watch 'touch ../x'",
    "watch -n 1 'touch ../x'",
    "entr bash -c 'touch ../x'",
    "tmux new -d 'touch ../x'",
    "screen -dm bash -c 'touch ../x'",
    "hyperfine 'touch ../x'",
    "parallel ::: 'touch ../x'",
  ])("asks before loading options or executing guarded code: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})
