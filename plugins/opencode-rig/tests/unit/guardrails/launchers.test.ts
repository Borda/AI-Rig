/** Shell launchers and persisted input cannot hide executable Git commands. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

describe("fed shell launcher options", () => {
  it.each([
    "bash --rcfile /dev/null",
    "bash --init-file /dev/null",
    "bash --rcfile=/dev/null",
    "bash --init-file=/dev/null",
    "bash +o noexec",
    "bash +O extglob",
    "bash -o noexec",
    "bash -O extglob",
    "bash +o=noexec",
    "bash +O=extglob",
    "bash -o=noexec",
    "bash -O=extglob",
    "bash +x",
    "bash --posix",
    "zsh +o noexec",
    "zsh +x",
    "sh +o noexec",
    "sh +x",
    "dash +o noexec",
    "dash +x",
    "ksh +o noexec",
    "ksh +x",
    "bash --unknown ignored.sh",
    "bash --rcfile",
    "bash -o",
    "bash -euvxn",
    "bash --",
    "bash -c +x",
  ])("asks for a launcher fed by a quoted heredoc: %s", (launcher) => {
    const result = classifyCommand(`${launcher} <<'EOF'\ngit push origin main\nEOF\n`)
    expect(result.kind).toBe("unparsed-git")
    expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "bash +o noexec <<EOF\ngit push origin main\nEOF\n",
    "printf safe | bash --rcfile /dev/null",
    "printf safe | bash +o noexec",
    "printf safe | bash --unknown script.sh",
    "bash --init-file /dev/null <<< safe",
    "bash +x <<< safe",
    "zsh <<< safe",
    "sh <<< safe",
    "dash <<< safe",
    "ksh <<< safe",
    "bash < script-input",
    "bash --rcfile /dev/null < script-input",
  ])("asks when stdin cannot be proved to be ordinary script data: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("persisted heredoc and here-string bodies", () => {
  it.each([
    "tee x.sh <<'EOF'\ngit push origin main\nEOF\nsh x.sh",
    "tee x.sh <<'EOF'\ngit push origin main\nEOF\n./x.sh",
    "tee x.sh <<'EOF'\ngit push origin main\nEOF\nsource x.sh",
    "tee x.sh <<'EOF'\ngit push origin main\nEOF\n. x.sh",
    "tee x.sh <<'EOF'; sh x.sh\ngit push origin main\nEOF\n",
    "cat > x.sh <<'EOF'\ngit push origin main\nEOF\n",
    "cat >> x.sh <<'EOF'\ngh pr merge 1\nEOF\n",
    "cat <<'EOF' > x.sh\ngit push origin main\nEOF\n",
    "sort -o x.sh <<'EOF'\ngit push origin main\nEOF\n",
    "sort --output=x.sh <<'EOF'\ngit push origin main\nEOF\n",
    "uniq - x.sh <<'EOF'\ngit push origin main\nEOF\n",
    "uniq in out <<'EOF'\ngh pr merge 1\nEOF\n",
    "uniq -c - x.sh <<'EOF'\ngit push origin main\nEOF\n",
    "dd of=x.sh <<'EOF'\ngit push origin main\nEOF\n",
    "tee x.sh <<'EOF'\ngit status\nEOF\n",
    "cat > x.sh <<< 'gh pr merge 1'",
    "tee x.sh <<< 'git push origin main'",
    "uniq - x.sh <<< 'git status'",
    "tee x.sh <<-'EOF'\n\tgit push origin main\n\tEOF\n",
    "env tee x.sh <<'EOF'\ngit push origin main\nEOF\n",
    "cat <<'EOF' | tee x.sh\ngit push origin main\nEOF\n",
    "cat <<'EOF' | cat > x.sh\ngh pr merge 1\nEOF\n",
    "cat <<'EOF' | cat | tee x.sh\ngit status\nEOF\n",
    "{ cat; } > x.sh <<'EOF'\ngit push origin main\nEOF\n",
    "{ tee x.sh; } <<'EOF'\ngit push origin main\nEOF\n",
    "{ tee x.sh; } <<< 'git push origin main'",
    "cat >&x.sh <<'EOF'\ngit push origin main\nEOF\n",
    "bash -c 'tee x.sh' <<'EOF'\ngit push origin main\nEOF\n",
    "bash -c 'cat > x.sh' <<< 'git push origin main'",
    "f() { tee x.sh; }; f <<'EOF'\ngit push origin main\nEOF\n",
    "f() { cat > x.sh; }; f <<< 'git push origin main'",
    "{ cat <<'EOF'; } | tee x.sh\ngit push origin main\nEOF\n",
    "{ cat <<'EOF'; } | cat | tee x.sh\ngit push origin main\nEOF\n",
    "cat <<'EOF' | { echo ready; tee x.sh; }\ngit push origin main\nEOF\n",
    "f() { tee x.sh; }; f <<'A'; f <<'B'\necho ok\nA\ngit push\nB\n",
  ])("asks before guarded commands can be persisted: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "cat <<'EOF'\ngit push is documentation, and it's prose\nEOF\n",
    "cat <<'EOF'\ngit push origin main\nEOF\n",
    "tee <<'EOF'\ngit push origin main\nEOF\n",
    "tee x.sh <<'EOF'\necho git push\nEOF\n",
    "cat > x.sh <<'EOF'\necho ok\nEOF\n",

    "tee x.sh <<'EOF'\necho 'unterminated\nEOF\n",
    "bash script.sh",
    "bash -c 'echo ok'",
    "bash -e script.sh",
    "printf safe | bash -eu script.sh",

    "bash -o errexit -c 'echo ok'",
    "echo <<'EOF'\ngit push origin main\nEOF\n",
  ])("keeps script-file operands and non-executing input unguarded: %s", (command) => {
    const result = classifyCommand(command)
    expect(result.kind).toBe("other")
    expect(decide(result, "standard", GRANTED)).toBeUndefined()
  })

  // Grammar gate: here-strings are outside the modelled redirections, so these ask.
  it.each(["cat <<< 'git push is documentation'", "tee x.sh <<< 'printf git'"])(
    "asks for here-strings outside the grammar: %s",
    (command) => {
      expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})

describe("shell option-name arity", () => {
  it.each(["bash", "zsh", "sh", "dash", "ksh"])("does not swallow stdin options for %s", (shell) => {
    const command = `${shell} -o -s ignored.sh <<'EOF'\ngit push\nEOF\n`
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(["-o", "+o", "-O", "+O"])("rejects option-shaped operands of %s", (option) => {
    const command = `printf safe | bash ${option} -s ignored.sh`
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(["+s", "bad.name", "bad/name"])("rejects non-name option operands: %s", (argument) => {
    const command = `bash -o ${argument} ignored.sh <<'EOF'\ngit push\nEOF\n`
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })
})

/** Wrap a command as a saved script that the same command line then runs with sh. */
const savedThenSh = (body: string) => `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`

describe("other shell interpreters", () => {
  it.each([
    "ash -c 'gi$9t push'",
    "mksh -c 'gi$9t push'",
    "yash -c 'gi$9t push'",
    "posh -c 'gi$9t push'",
    "rbash -c 'gi$9t push'",
    "oksh -c 'gi$9t push'",
    "cat > x.sh <<'EOF'\n(( n ))\nEOF\nash x.sh",
    // csh-family and fish code is not POSIX shell code, so the grammar cannot read it at all.
    "tcsh -c 'echo hi'",
    "csh -c 'set x=gi; ${x}t push'",
    "fish -c 'echo hi'",
    "cat > x.csh <<'EOF'\nset x=gi\n${x}t push\nEOF\ntcsh x.csh",
    // busybox and toybox run their first operand as an applet; a shell applet is a shell.
    "busybox sh -c 'echo hi'",
    "toybox sh -c 'echo hi'",
    "busybox ash x.sh",
    "busybox gi$9t push",
    "busybox git push",
  ])("asks for %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(savedThenSh(command)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(["ash -c 'echo hi'", "mksh x.sh", "busybox ls -la", "toybox wc -l f"])("keeps %s quiet", (command) => {
    expect(classifyCommand(command).kind).toBe("other")
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
  })
})

// Options whose value is a command line the program runs. Inline programs of other languages (awk, sed, perl,
// python3 -c, node -e, ruby -e) stay documented limits.
describe("exec-capable option forms", () => {
  it.each([
    "strace -o '|gi$9t push' true",
    "strace -o'|sh' true",
    "strace --output='!sh' true",
    "ssh -o ProxyCommand='sh -c \"gi$9t push\"' host",
    "ssh -oProxyCommand=x host",
    "ssh -o 'proxycommand x' host",
    "ssh -v -o LocalCommand=x host",
    "ssh -o KnownHostsCommand=x host",
    "scp -o ProxyCommand=x a host:b",
    "scp -S ./prog a host:b",
    "sftp -S ./prog host",
    "rsync -e 'sh -c \"gi$9t push\"' a b:",
    "rsync -avze ssh a b:",
    "rsync --rsh=ssh a b:",
    "rsync --rsh ssh a b:",
    "tar -cf x.tar --to-command='sh -c \"gi$9t push\"' f",
    "tar -xf x.tar --to-com=sh",
    "tar -cf x.tar --checkpoint=1 --checkpoint-action=exec=sh f",
    "tar -I prog -cf x.tar f",
    "tar --use-compress-program=prog -cf x.tar f",
    "tar --rsh-command=prog -cf host:x.tar f",
    "tar -F prog -cf x.tar f",
    "tar --info-script=prog -cf x.tar f",
    "tar cIf prog x.tar f",
    "make --eval='x:=$(shell gi$9t push)'",
    "make --ev=x",
    "npx -c 'gi$9t push'",
    "npx --call 'echo hi'",
    'osascript -e \'do shell script "gi" & "t push"\'',
  ])("asks for %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(savedThenSh(command)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "ssh -o StrictHostKeyChecking=no host",
    "ssh host",
    "scp a host:b",
    "rsync -av src/ dest/",
    "tar -czf out.tgz dist",
    "tar xf a.tar",
    "strace -o trace.log true",
    "osascript script.scpt",
    "osascript script.scpt -e x",
  ])("keeps %s quiet", (command) => {
    expect(classifyCommand(command).kind).toBe("other")
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
  })
})

// ssh joins the words after the destination into one command line that the remote shell runs.
describe("remote shell commands", () => {
  it.each([
    "ssh host git push",
    "ssh host 'git push'",
    "ssh -p 22 host git push origin main",
    "ssh host -t git push",
    "ssh host -- git push",
    "ssh host 'cd repo && git push'",
    "ssh host gi$9t push",
    'ssh host "$cmd"',
    "ssh host 'for f in *; do echo $f; done'",
    "ssh host ls *.log",
    'ssh "$host" ls',
    "ssh $X ls",
    "ssh host <<'EOF'\n(( n ))\nEOF",
  ])("asks for %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each(["ssh host", "ssh host ls", "ssh host 'ls -la'", "ssh host git status"])("keeps %s quiet", (command) => {
    expect(["other", "read-only-git"]).toContain(classifyCommand(command).kind)
    expect(decide(classifyCommand(command), "standard", GRANTED)).toBeUndefined()
  })
})

describe.each(["top level", "saved script"])("restricted tool forms: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each(["make test", "npx tsc --noEmit", "npx prettier -c ."])("asks for the complete invocation: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})
