/** Remote option command channels retain recursive Git checks and the shell grammar boundary. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const
const QUIET_KINDS = ["other", "read-only-git", "plain-commit"]

/** Wrap a probe as a saved script that the same command line then runs. */
const persistedThenRun = (body: string): string => `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`

describe.each([
  ["top level", (body: string) => body],
  ["saved script", persistedThenRun],
] as const)("remote path shell channels: %s", (_, wrap) => {
  it.each([
    "rsync 'h:$(gi$9t push)' .",
    "scp -O 'h:$(gi$9t push)' .",
    "rsync -a 'h:$(gi$9t push)' out",
    "scp -O 'h:$(gi$9t push)' out",
    "rsync -av src/ 'h:dst;gi$9t push'",
    "rsync -a a C:/dir/file",
    // The equals suffix is a path word too; a lone dash is not an admitted write target.
    "scp -O a user@host:dir/file_@%+,=-",
    ...["rsync -a", "scp -O", "scp"].flatMap((program) =>
      ["h:dir name", "h:dir;echo x", "h:dir|echo x", "h:`echo x`", 'h:"x"', "h:'x'", "h:x\\y", "h:x*"].map(
        (path) => `${program} a "${path.replaceAll('"', '\\"')}"`,
      ),
    ),
  ])("asks for remote metacharacters: %s", (command) => {
    expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "rsync -av a/ host:dir/",
    "scp -O a host:dir/file",
    "rsync user@host:dir/file out",
    "rsync -a a host:dir/file:part",
    "rsync -a a h:~/dir/file",
    "rsync -a ./local:part out",
  ])("keeps safe remote paths and local colon names quiet: %s", (command) => {
    expect(classifyCommand(wrap(command)).kind).toBe("other")
    expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)).toBeUndefined()
  })
})

const SSH_PROGRAMS = [
  ["ssh", "host"],
  ["scp", "host:input.txt output.txt"],
  ["sftp", "host"],
] as const

const REMOTE_SPELLINGS = [
  (body: string) => `-o RemoteCommand='${body}'`,
  (body: string) => `-oRemoteCommand='${body}'`,
  (body: string) => `-o 'remotecommand ${body}'`,
  (body: string) => `-o'REMOTECOMMAND=${body}'`,
  (body: string) => `-o '  ReMoTeCoMmAnD = ${body}'`,
  (body: string) => `-o 'RemoteCommand\t${body}'`,
]

const REMOTE_PUSH = SSH_PROGRAMS.flatMap(([program, operands]) =>
  REMOTE_SPELLINGS.map((option) => `${program} ${option("git push")} ${operands}`),
)
const REMOTE_QUIET = SSH_PROGRAMS.flatMap(([program, operands]) =>
  REMOTE_SPELLINGS.flatMap((option) =>
    ["git status", "echo ok"].map((body) => `${program} ${option(body)} ${operands}`),
  ),
)

const UNKNOWN_REMOTE = SSH_PROGRAMS.flatMap(([program, operands]) =>
  [
    '-o "RemoteCommand=$cmd"',
    '-oRemoteCommand="$cmd"',
    "-o RemoteCommand=echo*",
    "-o RemoteCommand=echo?",
    "-o $option",
    "-o *",
    "-o RemoteCommand",
    "-o RemoteCommand=",
    "-o 'RemoteCommand=   '",
    "-o 'RemoteCommand=-command'",
    "-o 'RemoteCommand=echo %h'",
    "-o 'RemoteCommand=gi$9t push'",
  ].map((option) => `${program} ${option} ${operands}`),
)

const LOCAL_EXEC_OPTIONS = SSH_PROGRAMS.flatMap(([program, operands]) =>
  [
    "ProxyCommand",
    "LocalCommand",
    "KnownHostsCommand",
    "PKCS11Provider",
    "SecurityKeyProvider",
    "XAuthLocation",
  ].flatMap((key) => [
    `${program} -o '${key}=echo ok' ${operands}`,
    `${program} -o'${key.toLowerCase()} echo ok' ${operands}`,
  ]),
)

const OTHER_EXEC_OPTIONS = [
  "ssh -I provider.so host",
  "ssh -Iprovider.so host",
  "ssh -vIprovider.so host",
  "scp -S helper host:input.txt output.txt",
  "scp -Shelper host:input.txt output.txt",
  "scp -D helper host:input.txt output.txt",
  "scp -Dhelper host:input.txt output.txt",
  "sftp -S helper host",
  "sftp -Shelper host",
  "sftp -D helper host",
  "sftp -Dhelper host",
  "sftp -b commands.txt host",
  "sftp -bcommands.txt host",
  "rsync -e 'echo ok' host:input.txt output.txt",
  "rsync -e'echo ok' host:input.txt output.txt",
  "rsync --rsh='echo ok' host:input.txt output.txt",
  "rsync --rsh 'echo ok' host:input.txt output.txt",
]

const OTHER_REMOTE_PUSH = [
  "ssh -o 'RemoteCommand=echo ok\nnpm publish' host",
  "scp -o 'RemoteCommand=echo ok\nnpm publish' host:input.txt output.txt",
  "sftp -o 'RemoteCommand=echo ok\nnpm publish' host",
  "ssh -voRemoteCommand='git push' host",
  "ssh -vo 'RemoteCommand git push' host",
  "ssh host -o 'RemoteCommand=git push'",
  "rsync --rsync-path='git push' host:input.txt output.txt",
  "rsync --rsync-path 'git push' host:input.txt output.txt",
  "rsync --rsync-p='git push' host:input.txt output.txt",
  "rsync --rsync-p 'git push' host:input.txt output.txt",
  "sftp -s 'git push' host",
  "sftp -s'git push' host",
]

const OTHER_REMOTE_QUIET = [
  "ssh -voRemoteCommand='git status' host",
  "ssh -vo 'RemoteCommand echo ok' host",
  "ssh host -o 'RemoteCommand=echo ok'",
  "rsync --rsync-path='git status' host:input.txt output.txt",
  "rsync --rsync-path 'echo ok' host:input.txt output.txt",
  "rsync --rsync-p='git status' host:input.txt output.txt",
  "rsync --rsync-p 'echo ok' host:input.txt output.txt",
  "sftp -s 'git status' host",
  "sftp -s'echo ok' host",
]

const OTHER_UNKNOWN_REMOTE = [
  "ssh -o",
  "scp -o",
  "sftp -o",
  'rsync --rsync-path "$cmd" host:input.txt output.txt',
  'rsync --rsync-path="$cmd" host:input.txt output.txt',
  "rsync --rsync-path echo* host:input.txt output.txt",
  "rsync --rsync-path -command host:input.txt output.txt",
  "rsync --rsync-path= host:input.txt output.txt",
  "rsync --rsync-path",
  'sftp -s "$cmd" host',
  'sftp -s"$cmd" host',
  "sftp -s echo* host",
  "sftp -s -command host",
  "sftp -s '' host",
  "sftp -s",
]

describe.each([
  ["at top level", (body: string) => body],
  ["in a saved script run in the same text", persistedThenRun],
] as const)("remote option command channels %s", (_name, wrap) => {
  it.each([...REMOTE_PUSH, ...OTHER_REMOTE_PUSH])("asks for recursive publication: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([...REMOTE_QUIET, ...OTHER_REMOTE_QUIET])("classifies a literal benign command: %s", (body) => {
    const result = classifyCommand(wrap(body))
    // Saved Git reads retain the conservative body verdict of an equivalent sh -c 'git status'.
    const savedGitRead = wrap === persistedThenRun && body.includes("git status")
    if (savedGitRead) expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
    else {
      expect(QUIET_KINDS).toContain(result.kind)
      expect(decide(result, "standard", GRANTED)).toBeUndefined()
    }
  })

  it.each([...UNKNOWN_REMOTE, ...OTHER_UNKNOWN_REMOTE])("asks for an unproved remote command: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([...LOCAL_EXEC_OPTIONS, ...OTHER_EXEC_OPTIONS])("preserves existing local execution stops: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})
