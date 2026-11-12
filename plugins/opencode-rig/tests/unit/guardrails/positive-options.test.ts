/** Exact option identities and arities govern read exemptions and command channels. */
import { describe, expect, it } from "vitest"
import { hostname } from "node:os"
import { classifyCommand, decide } from "../../../src/guardrails.ts"
const GRANTED = { status: "granted", path: "approval.json" } as const

describe.each([
  ["perl", "-h -v -V -w -X --help --version"],
  ["ruby", "-h -v -w -c --help --version"],
  ["php", "-h -v --help --version"],
  ["lua", "-v"],
  ["julia", "-h -v --help --version"],
  ["R", "--help --version"],
  ["Rscript", "--help --version"],
  ["ipython", "--help --version"],
  ["tsx", "--help --version"],
  ["ts-node", "-h -v --help --version"],
  ["deno", "--help --version"],
  ["bun", "--help --version"],
  ["osascript", ""],
  ["gdb", "--help --version"],
  ["lldb", "-v -h -x --no-lldbinit --help --version"],
])("interpreter and debugger safe flags: %s", (program, flags) => {
  it.each(flags ? flags.split(" ") : [""])("keeps an explicitly admitted form quiet: %s", (flag) => {
    expect(decide(classifyCommand(`${program} ${flag}`), "standard", GRANTED)).toBeUndefined()
  })
})

describe.each([
  ["perl", "d:MOD M m I x e E n p i s d"],
  ["ruby", "I r e n p i x C S s"],
  ["php", "d r B R F E f S t run process-code"],
  ["lua", "e l"],
  ["julia", "e E L J C eval print load"],
  ["R", "e f file"],
  ["Rscript", "e f file"],
  ["ipython", "c c=code InteractiveShellApp.code_to_run=code m exec"],
  ["tsx", "e p r import eval print require"],
  ["ts-node", "e p r require import compiler-options"],
  ["deno", "eval import config"],
  ["bun", "e p eval print preload require"],
  ["osascript", "e l s"],
  ["gdb", "x ex iex ix command eval-command init-command init-eval-command ie batch"],
  ["lldb", "o s O S k K one-line source local-lldbinit"],
])("interpreter and debugger unknown options: %s", (program, options) => {
  it.each(
    options
      .split(" ")
      .flatMap((option) =>
        ["-", "--", "---"].flatMap((dash) =>
          Array.from({ length: option.length }, (_, index) => option.slice(0, index + 1)).flatMap((name) => [
            `${dash}${name} value`,
            `${dash}${name}=value`,
          ]),
        ),
      ),
  )("asks for non-admitted options and abbreviations: %s", (option) => {
    expect(decide(classifyCommand(`${program} ${option}`), "standard", GRANTED)?.effect).toBe("ask")
  })
})

it.each(["perl script.pl", "ruby script.rb", "lua script.lua"])("keeps literal script input quiet: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", GRANTED)).toBeUndefined()
})

it.each([
  ...["gcc", "g++", "clang", "clang++", "cc", "c++"].flatMap((program) =>
    ["fplugin", "fpass-plugin", "load", "B", "Xclang", "mllvm"].flatMap((option) =>
      ["-", "--", "---"].flatMap((dash) => [`${program} ${dash}${option}=input`, `${program} ${dash}${option} input`]),
    ),
  ),
  "gcc -fpl=input",
  "clang --Xcl=input",
  "gcc -Binput",
  "clang -mll input",
  "wget --use-askpass=handler",
  "wget --use-ask handler",
  "curl --engine=engine",
  "curl --eng engine",
])("asks for indirect plugin and credential-handler options: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", GRANTED)?.effect).toBe("ask")
})

it.each(["gcc", "g++", "cc", "c++", "clang", "clang++", "nvcc"])(
  "asks for compiler option-file spellings: %s",
  (program) => {
    const option = program.startsWith("clang") ? "config" : program === "nvcc" ? "options-file" : "specs"
    expect(decide(classifyCommand(`${program} --${option}=input`), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(`${program} -${option.slice(0, 3)}=input`), "standard", GRANTED)?.effect).toBe("ask")
  },
)

it.each([
  "julia --eva x",
  "wget --exec robots=off URL",
  "tar cfT out.tar list.txt",
  "tar --files-f=list.txt -cf out.tar",
  "rsync -a --files-fr=list.txt src/ dst/",
  ...["-command", "-eval-command", "-init-command", "--init-eval-command", "--ex", "--eval", "-iex", "-ix"].map(
    (option) => `gdb ${option}=input`,
  ),
  ...["--run", "--process-begin", "--process-code", "--process-end"].map((option) => `php ${option}=input`),
  "gcc -specs=input",
  ...[
    "-o",
    "--one-line",
    "-s",
    "--source",
    "-O",
    "--one-line-before-file",
    "-S",
    "--source-before-file",
    "-k",
    "-K",
    "--one-line-on-crash",
    "--source-on-crash",
  ].map((option) => `lldb ${option} input`),
])("asks for source and argument-file option identities: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", GRANTED)?.effect).toBe("ask")
})

it.each([
  ...["--config file", "--config=file"].map((option) => `clang ${option} input.c`),
  ...["-K file", "-Kfile", "-K=file", "--config file", "--config=file"].map((option) => `curl ${option}`),
  "wget --config=file https://example.test",
  "wget --config file https://example.test",
  "file -flist",
  "file -flist",
  "find -files0-from list -print",
  "tsc @options",
  "javac @options",
  "gcc @options",
  "nvcc --options-file options",
  "nvcc --options-file=options",
  "gdb --args program",
])("asks before reading indirect options or operands: %s", (body) => {
  expect(decide(classifyCommand(body), "standard", GRANTED)?.effect).toBe("ask")
})

describe.each(["top level", "saved script"])("sed read modes: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each([
    "sed -i p input",
    "sed -i '' p input",
    "sed -i backup p input",
    "sed -i.bak p input",
    "sed -ni p input",
    "sed -in p input",
    "sed -Ei '' p input",
    "sed --in-place p input",
    "sed --in-place=.bak p input",
    "sed --in-place backup p input",
    "sed --in-placebackup p input",
    "sed -i q '1w ../out' input",
    "sed -u p input",
    "sed -z p ../input",
    "sed '-z=value'",
    "sed '-z~'",
    "sed -f commands input",
    "sed -e p -e 'w ../out' input",
    "sed -ep --expression='w ../out' input",
    "sed --expression=p -e'w ../out' input",
    "sed -n -e's,i,j,w ../out' input",
  ])("asks for in-place modes and unchecked scripts: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each([
    "sed -n p ../input",
    "sed -E p ../input",
    "sed -r p ../input",
    "sed -s p ../input",
    "sed -e p ../input",
    "sed --expression=p ../input",
    "sed -n -e p --expression=d ../input",
    "sed -nep -ed ../input",
  ])("keeps literal read scripts quiet: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)).toBeUndefined()
  })
})

describe.each(["top level", "saved script"])("option values and identities: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each([
    "sed -iext -e 's/i/j/w .ssh/config' input",
    "sed -ni -e 'p;w .ssh/config' input",
    "sed -e 'p' --expression='w .ssh/config' input",
    "sed -f commands input",
    "sed -i -f commands input",
    "sed -i../.ssh/config p input",
    "sed --in-place=.bak p input",
    "pytest --override-ini=k=v=.ssh/config",
    "tool -aok=k=../out",
    "tool -abk=k=.ssh/config",
    "date -uf %s +1",
    "date -f %s -u +1",
    "date -fj %s +1",
    "jq --arg name",
    "jq --argjson name",
    "jq --rawfile name",
    "jq --slurpfile name",
    "jq --arg=name",
    "jq --argjson=name",
    "jq --rawfile=name",
    "jq --slurpfile=name",
    "ls -D",
    "ls -I --unmodelled",
    "ls -T --unmodelled",
    "stat -f --unmodelled",
    "base64 -i --unmodelled",
    "go vet -vettool sh",
    "cargo -C ../project build",
    "cargo --config 'target.runner=sh' build",
    "cargo rustc -- -Clinker=sh",
    "cargo rustc -- -C linker=sh",
    "cargo rustdoc -- --test-builder sh",
    "cargo --config config.toml build",
    "make -E 'x=1'",
    "make -Ex=1",
    "make --eval 'x=1'",
    "make --eval=x=1",
  ])("asks when a value or mode cannot prove safety: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each([
    "sed -e's/i/j/' ../input",
    "sed -ne's/i/j/' ../input",
    "xxd -nreverse ../input",
    "date -ujf %s 1 +%Y",
    "date -f %s -j 1 +%Y",
    "jq --arg a b . ../input",
    "jq --argjson a 1 . ../input",
    "jq --rawfile a ../input .",
    "jq --slurpfile a ../input .",
    "jq --args . a",
    "find -E .. -regex 'x'",
    "xxd -e ../input",
    "diff --to-file ../input current",
    "ls -D %s ../input",
    "ls -I ../input",
    "ls -T ../input",
    "stat -f",
    "base64 -i",
    "tool KEY=VAL",
    "tool KEY=part=value",
    "tool -ak=part=value",
  ])("keeps literal read inputs and inert values quiet: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)).toBeUndefined()
  })
  it.each([
    "tsc -p",
    "tsc --project",
    "pytest -c",
    "pytest --rootdir",
    "uv --project",
    "cargo --manifest-path",
    "eslint --config",
    "prettier --config",
    "vite --config",
    "vitest --config",
    "webpack --config",
    "rollup --config",
  ])("asks for tool configuration options even inside the project: %s", (prefix) => {
    expect(decide(classifyCommand(wrap(`${prefix} ../project`)), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(wrap(`${prefix}=.ssh/config`)), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(wrap(`${prefix} project/config`)), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(wrap(`${prefix}=.temp/config`)), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each(["a", "B", "c", "m"].flatMap((x) => ["a", "B", "c", "m", "t"].map((y) => `-newer${x}${y}`)))(
    "takes exactly one read value for find %s",
    (option) => {
      expect(decide(classifyCommand(wrap(`find .. ${option} ../reference`)), "standard", GRANTED)).toBeUndefined()
      expect(decide(classifyCommand(wrap(`find . ${option}`)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})
const ASKS = [
  "sort --o=.ssh/config in.txt",
  "sort --o .ssh/config in.txt",
  "base64 -d -i in.txt --o=.ssh/config",
  "xxd -b in.txt .ssh/config",
  "xxd -i in.txt .ssh/config",
  "xxd -c32 -b m.txt .ssh/rc",
  "find . -execdir sort --o=config {} ';'",
  "npm -p --userconfig=.ssh/config config set loglevel=warn",
  'docker run --mount=type=bind,source=/Users/u/.ssh,target=/s alpine sh -c "cat /s/k > /s/rc"',
  "date --s=x",
  "tree --o=.ssh/config",
  "cat > .temp/w.sh <<'E'\nsort --o=.ssh/rc in.txt\nE\nsh .temp/w.sh",
  "npm exec -c 'gi$9t push'",
  "npm exec -yc 'gi$9t push'",
  "pnpm exec -yc 'gi$9t push'",
  "yarn exec --c='gi$9t push'",
  "docker --context local run --mount=type=bind,source=.ssh,target=/data image true",
  "docker --context local run image sh -c 'gi$9t push'",
  "podman container run image sh -c 'gi$9t push'",
  "npm x -c 'gi$9t push'",
  "npm exec --call='gi$9t push'",
  'cargo --config \'target.x.runner=["sh","-c","gi$9t push"]\' run',
  "npx -c 'gi$9t push'",
  "rsync -av a.txt host:-e.sh",
  "scp a.txt localhost:.ssh/rc",
  "less '+!gi$9t push' README.md",
  "awk -o.ssh/rc 'BEGIN{}'",
  "docker run --rm img sh -c 'gi$9t push'",
  "cp k ../x=a/b",
  "tee ~/x=a/b < in.txt",
  "cp k /tmp/x=a/b",
  "cp k ../.ssh=a/b",
  "echo x > ../x=a/b",
  "cp k ../x=a",
  "echo x > x=.ssh/config",
  "tee x=/tmp/file < input",
  "cp k x=../file",
  "cat x=.ssh/config",
  "grep x=../file input",
  "which x=.ssh/config",
  "printf '%s' x=.ssh/config",
  "test x=.ssh/config",
  "[ x=.ssh/config ]",
  "find . -name x=.ssh/config",
  "sort --out=.ssh/config input",
  "sort --outp=.ssh/config input",
  "base64 --o=.ssh/config input",
  "date --s now",
  "date 010101012026",
  "docker run --tmpfs /data image true",
  "podman run --device /dev/disk:/data image true",
  "docker run -v 'C:\\Users\\u\\.ssh:/data' image true",
  "docker run --mount type=bind,src=../data,target=/data image true",
  "docker run --mount=type=bind,source=.ssh,target=/data image true",
  "docker run --mount=type=bind,source=data,target=/data image env sh -c 'gi$9t push'",
  "scp a user@127.0.0.1:.ssh/rc",
  "scp a '[::1]:.ssh/rc'",
  "rsync a host:-option",
  "less +p input",
  "sed -n 'p;F' input",
  "sed -n 'p;s/a/b/e' input",
  "sed -f ../commands input",
  "sed -Zni p input",
  "sed --in-plac p input",
  "ruff check --config 'cache-dir=\".ssh\"'",
  "npm -p --userconfig=.ssh/config config set loglevel=warn",
]
const QUIET = [
  "which python3",
  "which -a node",
  "command -v python3",
  "type python3",
  "sed -n '1,5p;10p' file",
  "sed 's/a/b/;s/c/d/' file",
  "sed -n '/x/Ip' file",
  "sed '/^#/d;/^$/d' file",
  "echo a | base64 -d",
  'echo "$(git rev-parse HEAD)"',
  "sed -sn 'p;d' ../input",
  "sed -n 'y/ab/cd/;2q;=' ../input",
  "sed -n 's/a/b/2gpI' ../input",
  "printf a | base64 --decode > out.txt",
  "echo a | base64 -D -o out.txt",
  "echo a | base64 --decode --output=out.txt",
  "which -a python3",
  "type -a python3",
  "type -f python3",
  "type -p python3",
  "type -P python3",
  "type -t python3",
  "command -V python3",
]
describe.each(["top level", "saved script"])("positive options: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each(ASKS)("asks for unsafe option or operand: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each(QUIET)("keeps ordinary reading quiet: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)).toBeUndefined()
  })
  it("retains confirmation for a persisted Git read without script execution provenance", () => {
    const body = "cat > .temp/s.sh <<'E'\ngit status\nE\nsh .temp/s.sh"
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
  it("checks the local hostname as a local copy target", () => {
    expect(decide(classifyCommand(wrap(`scp input ${hostname()}:.ssh/config`)), "standard", GRANTED)?.effect).toBe(
      "ask",
    )
  })
  it("keeps awk under path checks because its code and output modes are not modelled", () => {
    expect(decide(classifyCommand(wrap("awk '{print $1}' ../input")), "standard", GRANTED)?.effect).toBe("ask")
  })
  it("keeps tree under path checks because recursive HTML output is not modelled", () => {
    expect(decide(classifyCommand(wrap("tree -R -H . -o ../index.html")), "standard", GRANTED)?.effect).toBe("ask")
  })
})

const READ_OPTION_CASES: Record<string, string> = {
  cat: "-A -b -e -E -n -s -t -T -u -v --show-all --number-nonblank --show-ends --number --squeeze-blank --show-tabs --show-nonprinting",
  ls: "-a -A -b -B -c -C -d -D= -f -F -g -G -h -H -i -I -k -l -L -m -n -N -o -p -q -Q -r -R -s -S -t -T -u -U -v -w= -x -X -1 --all --almost-all --directory --human-readable --recursive --color? --sort= --time= --format= --quoting-style= --block-size= --hide= --ignore=",
  head: "-n= -c= -q -v -z --lines= --bytes= --quiet --silent --verbose --zero-terminated",
  tail: "-n= -c= -q -v -z --lines= --bytes= --quiet --silent --verbose --zero-terminated",
  grep: "-E -F -G -P -e= -f= -i -v -w -x -z -c -l -L -m= -n -b -H -h -o -q -s -a -I -r -R -A= -B= -C= --regexp= --file= --ignore-case --invert-match --word-regexp --line-regexp --count --files-with-matches --files-without-match --max-count= --line-number --only-matching --quiet --recursive --after-context= --before-context= --context= --color? --include= --exclude= --exclude-dir=",
  egrep:
    "-E -F -G -P -e= -f= -i -v -w -x -z -c -l -L -m= -n -b -H -h -o -q -s -a -I -r -R -A= -B= -C= --regexp= --file= --ignore-case --invert-match --word-regexp --line-regexp --count --files-with-matches --files-without-match --max-count= --line-number --only-matching --quiet --recursive --after-context= --before-context= --context= --color? --include= --exclude= --exclude-dir=",
  fgrep:
    "-E -F -G -P -e= -f= -i -v -w -x -z -c -l -L -m= -n -b -H -h -o -q -s -a -I -r -R -A= -B= -C= --regexp= --file= --ignore-case --invert-match --word-regexp --line-regexp --count --files-with-matches --files-without-match --max-count= --line-number --only-matching --quiet --recursive --after-context= --before-context= --context= --color? --include= --exclude= --exclude-dir=",
  rgrep:
    "-E -F -G -P -e= -f= -i -v -w -x -z -c -l -L -m= -n -b -H -h -o -q -s -a -I -r -R -A= -B= -C= --regexp= --file= --ignore-case --invert-match --word-regexp --line-regexp --count --files-with-matches --files-without-match --max-count= --line-number --only-matching --quiet --recursive --after-context= --before-context= --context= --color? --include= --exclude= --exclude-dir=",
  wc: "-c -m -l -L -w --bytes --chars --lines --max-line-length --words",
  diff: "-a -b -B -c -C= -d -e -E -i -I= -n -N -p -q -r -s -t -T -u -U= -w -x= -X= -y --brief --recursive --unified? --context? --ignore-case --ignore-all-space --ignore-space-change --exclude= --exclude-from= --label= --to-file=",
  cmp: "-b -l -s -i= -n= --print-bytes --verbose --silent --quiet --ignore-initial= --bytes=",
  stat: "-L -f -F -l -n -q -r -s -t= -x --dereference --file-system --format= --printf= --terse",
  file: "-b -c -e= -F= -h -i -k -l -L -m= -n -N -p -P= -r -s -v -z -Z -0 --brief --checking-printout --exclude= --separator= --no-dereference --mime --mime-type --mime-encoding --keep-going --dereference --magic-file= --no-buffer --no-pad --preserve-date --raw --special-files --uncompress",
  less: "-a -c -e -E -f -F -g -G -i -I -j= -J -m -M -n -N -p= -q -Q -r -R -s -S -t= -u -U -w -W -x= -X -y= -z= --ignore-case --IGNORE-CASE --LINE-NUMBERS --RAW-CONTROL-CHARS --quit-if-one-screen --no-init",
  more: "-c -d -f -l -p -s -u -n=",
  cd: "-L -P -e -@",
  pwd: "-L -P",
  echo: "-n -e -E",
  printf: "",
  test: "",
  "[": "",
  true: "",
  false: "",
  find: "-H -L -P -X -E -d -depth -x -xdev -mount -follow -daystart -ignore_readdir_race -noignore_readdir_race -print -print0 -ls -prune -quit -true -false -a -and -o -or -not -empty -readable -writable -executable -nouser -nogroup -name= -iname= -path= -ipath= -wholename= -iwholename= -regex= -iregex= -lname= -ilname= -type= -xtype= -size= -mtime= -mmin= -atime= -amin= -ctime= -cmin= -newer= -anewer= -cnewer= -used= -user= -uid= -group= -gid= -perm= -maxdepth= -mindepth= -links= -inum= -samefile= -fstype= -context= -printf= -regextype=",
  sed: "-n -E -r -e= -s --expression=",
  sort: "-b -d -f -g -h -i -M -n -R -r -V -c -C -m -s -u -z -k= -t= -S= --ignore-leading-blanks --dictionary-order --ignore-case --general-numeric-sort --human-numeric-sort --ignore-nonprinting --month-sort --numeric-sort --random-sort --reverse --version-sort --check --merge --stable --unique --zero-terminated --key= --field-separator= --buffer-size= --parallel= -o! --output!",
  uniq: "-c -d -D -i -u -z -f= -s= -w= --count --repeated --ignore-case --unique --zero-terminated --skip-fields= --skip-chars= --check-chars=",
  jq: "-c -r -R -s -n -e -S -M -C -j -a --compact-output --raw-output --raw-input --slurp --null-input --exit-status --sort-keys --monochrome-output --color-output --join-output --ascii-output --tab --indent= --arg: --argjson: --rawfile: --slurpfile: --args -f= -L= --from-file=",
  rg: "-a -b -c -C= -e= -f= -F -g= -h -H -i -I -l -L -m= -M= -n -N -o -p -q -r= -s -S -t= -T= -u -v -w -x -z -0 -A= -B= --after-context= --before-context= --context= --regexp= --file= --glob= --iglob= --type= --type-not= --max-count= --max-depth= --max-columns= --replace= --color= --colors= --encoding= --ignore-file= --sort= --sortr= --threads= --files --hidden --no-ignore --no-heading --heading --line-number --no-line-number --count --count-matches --files-with-matches --files-without-match --fixed-strings --ignore-case --smart-case --invert-match --word-regexp --line-regexp --only-matching --quiet --json --stats --multiline --pcre2 --text --null --null-data --follow --no-config",
  du: "-a -b -c -d= -h -H -k -L -m -P -s -S -x --all --bytes --total --max-depth= --human-readable --summarize --one-file-system --exclude= --exclude-from= --block-size=",
  df: "-a -b -h -H -i -k -l -m -P -T -t= -x= --all --human-readable --inodes --local --portability --print-type --type= --exclude-type= --block-size= --output?",
  realpath:
    "-e -m -L -P -q -s -z --canonicalize-existing --canonicalize-missing --logical --physical --quiet --strip --no-symlinks --zero --relative-to= --relative-base=",
  readlink:
    "-e -f -m -n -q -s -v -z --canonicalize --canonicalize-existing --canonicalize-missing --no-newline --quiet --silent --verbose --zero",
  basename: "-a -s= -z --multiple --suffix= --zero",
  dirname: "-z --zero",
  cut: "-b= -c= -f= -d= -s -z --bytes= --characters= --fields= --delimiter= --only-delimited --complement --output-delimiter= --zero-terminated",
  tr: "-c -C -d -s -t --complement --delete --squeeze-repeats --truncate-set1",
  nl: "-b= -d= -f= -h= -i= -l= -n= -p -s= -v= -w= --body-numbering= --section-delimiter= --footer-numbering= --header-numbering= --line-increment= --join-blank-lines= --number-format= --no-renumber --number-separator= --starting-line-number= --number-width=",
  column: "-t -c= -s= -o= -x --table --output-width= --separator= --output-separator= --fillrows",
  fold: "-b -s -w= --bytes --spaces --width=",
  comm: "-1 -2 -3 -z --check-order --nocheck-order --output-delimiter= --total --zero-terminated",
  join: "-a= -e= -i -j= -o= -t= -v= -1= -2= -z --ignore-case --check-order --nocheck-order --header --zero-terminated",
  paste: "-d= -s -z --delimiters= --serial --zero-terminated",
  od: "-a -b -c -d -f -h -i -l -o -s -x -v -A= -j= -N= -t= -w? --address-radix= --skip-bytes= --read-bytes= --format= --width? --endian= --strings? --output-duplicates",
  hexdump: "-b -c -C -d -o -x -v -e= -f= -n= -s=",
  xxd: "-a -b -d -e -E -i -p -ps -r -u -C -h -v -c= -cols= -g= -l= -s= -o= -n= -R=",
  shasum:
    "-a= -b -c -p -t -U -0 --algorithm= --binary --check --portable --text --UNIVERSAL --tag --quiet --status --strict --warn",
  sha256sum: "-b -c -t -z --binary --check --text --zero --tag --ignore-missing --quiet --status --strict --warn",
  md5: "-q -r -s= -p -t -x",
  date: "-u -j -n -R -I? -r= -d= -f= -v= --utc --universal --rfc-email --iso-8601? --rfc-3339= --reference= --date= --file=",
  which: "-a -s --all --skip-alias --skip-functions --skip-dot --skip-tilde --show-dot --show-tilde",
  base64: "-d -D -i -b= -w= -o! --decode --ignore-garbage --wrap= --input= --output!",
}
const TOOL_PATH_CASES: Record<string, string> = {
  tsc: "-p= --project=",
  ruff: "--config=",
  uv: "--project=",
  mypy: "--config-file=",
  pytest: "-c= --rootdir=",
  npm: "",
  pnpm: "",
  yarn: "",
  cargo: "--manifest-path=",
  go: "",
  make: "",
  npx: "",
  cmake: "",
  eslint: "--config=",
  prettier: "--config=",
  vite: "--config=",
  vitest: "--config=",
  webpack: "--config=",
  rollup: "--config=",
}

const OPTION_CASES = Object.entries(READ_OPTION_CASES).flatMap(([program, entries]) =>
  entries
    .split(" ")
    .filter(Boolean)
    .map((entry) => [program, entry] as const),
)
const PROGRAMS = Object.keys(READ_OPTION_CASES)

/** Give scripts a safe command and options a literal input, never execute the tested program. */
function optionCommand(program: string, entry: string, attached = false): string {
  const option = entry.replace(/[=?!:]$/, "")
  const arity = /[=?!:]$/.exec(entry)?.[0]
  const value = program === "sed" ? "p" : arity === "!" ? "out.txt" : "1"
  const argument =
    arity === ":"
      ? `${option} name ${value}`
      : arity
        ? attached
          ? `${option}${option.startsWith("--") ? "=" : ""}${value}`
          : `${option} ${value}`
        : option
  const script = program === "sed" && !["-e", "--expression"].includes(option) ? " p" : ""
  // Optional arguments consume attached values only; the separate word is a normal read operand.
  const dateMode = program === "date" && option === "-f" ? " -j" : ""
  const operand = program === "date" ? "" : program === "xxd" && option === "-r" ? " input.txt" : " ../input"
  return `${program}${dateMode} ${argument}${script}${arity === "!" ? " input.txt" : operand}`
}

describe.each(["top level", "saved script"])("read option tables: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`

  it.each(OPTION_CASES)("admits the exact option %s %s", (program, entry) => {
    const command = optionCommand(program, entry, entry.endsWith("?"))
    expect(decide(classifyCommand(wrap(command)), "standard", GRANTED), command).toBeUndefined()
  })

  it.each(OPTION_CASES.filter(([, entry]) => /[=!]$/.test(entry)))("requires one value for %s %s", (program, entry) => {
    const command = `${program} ${entry.slice(0, -1)}`
    expect(decide(classifyCommand(wrap(command)), "standard", GRANTED)?.effect, command).toBe("ask")
  })

  it.each(OPTION_CASES.filter(([, entry]) => /[=?!]$/.test(entry) && (entry.startsWith("--") || entry.length === 3)))(
    "admits an attached value for %s %s",
    (program, entry) => {
      const command = optionCommand(program, entry, true)
      expect(decide(classifyCommand(wrap(command)), "standard", GRANTED), command).toBeUndefined()
    },
  )

  it.each(OPTION_CASES.filter(([, entry]) => !/[=?!:]$/.test(entry)))(
    "does not turn a flag into a valued option for %s %s",
    (program, entry) => {
      expect(decide(classifyCommand(wrap(`${program} '${entry}=value'`)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )

  it.each(PROGRAMS)("rejects an unlisted long option for %s", (program) => {
    expect(decide(classifyCommand(wrap(`${program} --unmodelled-option=out.txt`)), "standard", GRANTED)?.effect).toBe(
      "ask",
    )
  })

  it.each(PROGRAMS)("rejects an unlisted short option for %s", (program) => {
    expect(decide(classifyCommand(wrap(`${program} '-~'`)), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each(OPTION_CASES.filter(([, entry]) => /^-.$/.test(entry)))(
    "rejects an unknown letter clustered with %s %s",
    (program, entry) => {
      expect(decide(classifyCommand(wrap(`${program} '${entry}~'`)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )

  it.each(OPTION_CASES.filter(([, entry]) => entry.startsWith("--") && entry.replace(/[=?!:]$/, "").length > 3))(
    "rejects shortened long names for %s %s",
    (program, entry) => {
      const option = entry.replace(/[=?!:]$/, "")
      const name = option.slice(0, -1)
      // A shorter name that is itself listed remains that distinct exact option.
      const listed = READ_OPTION_CASES[program]!.split(" ").some(
        (candidate) => candidate.replace(/[=?!:]$/, "") === name,
      )
      if (!listed)
        expect(decide(classifyCommand(wrap(`${program} ${name}=value`)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})

describe.each(["top level", "saved script"])("tool input ownership: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  const cases = Object.entries(TOOL_PATH_CASES).flatMap(([program, entries]) =>
    entries
      .split(" ")
      .filter(Boolean)
      .map((entry) => [program, entry.slice(0, -1)] as const),
  )
  it.each(cases)("asks for tool path options %s %s", (program, option) => {
    expect(
      decide(
        classifyCommand(
          wrap(
            `${program} ${option} ${program === "ruff" ? "../ruff.toml" : program === "mypy" ? "../mypy.ini" : "input"}`,
          ),
        ),
        "standard",
        GRANTED,
      )?.effect,
    ).toBe("ask")
    expect(
      decide(
        classifyCommand(
          wrap(
            `${program} ${option}=${program === "ruff" ? "../ruff.toml" : program === "mypy" ? "../mypy.ini" : "input"}`,
          ),
        ),
        "standard",
        GRANTED,
      )?.effect,
    ).toBe("ask")
    expect(decide(classifyCommand(wrap(`${program} ${option}`)), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each(Object.keys(TOOL_PATH_CASES))("does not borrow an unknown input option for %s", (program) => {
    expect(decide(classifyCommand(wrap(`${program} --unknown-input ../input`)), "standard", GRANTED)?.effect).toBe(
      "ask",
    )
  })
})

describe.each(["top level", "saved script"])("decoded option semantics: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each([
    "date -f %s +1",
    "sed -e 's,a,b,w ../x.txt' y.txt",
    "sed -e's,a,b,w ../x.txt' y.txt",
    "sed -e's,i,j,w ../x.txt' y.txt",
    "sed -ne's,i,j,w ../x.txt' y.txt",
    "sed --expression='s,i,j,w ../x.txt' y.txt",
    "tsc -p ../x",
    "tsc --project=../x",
    "pytest -o cache_dir=../x",
    "pytest -ocache_dir=../x",
    "pytest --override-ini=cache_dir=../x",
    "pytest -c ../x.ini",
    "ruff --config=../x check",
    "cargo --manifest-path=../x/Cargo.toml build",
    "go build -toolexec=true",
    "go build -toolexec true",
    "uv --project=../x run x",
    "go build -toolexec=sh",
    "go build -toolexec sh",
    "go test -exec sh",
    "go test -exec=sh ./x",
    "go vet -vettool=sh",
    "uv run rm -r x",
    "uv run -- rm -r x",
    "npx rm -r x",
    "npm exec rm -r x",
    "npm exec -- rm -r x",
    "pnpm exec rm -r x",
    "yarn exec rm -r x",
    "uv run --with y rm -r x",
    "uv tool run rm -r x",
    "uvx rm -r x",
    "npm x rm -r x",
    "npm exec -c 'rm -r x'",
    "uv run find . -delete",
    "npm exec find . -delete",
    "uv run env rm -r x",
    "sed -e's,i,j,w ../x.txt' p",
    "sed -ne's,i,j,w ../x.txt' p",
    "sed -e's,a,b,w ../x.txt' p",
    "sed -e 's,i,j,w ../x.txt' p",
    "pytest --rootdir=../x",
    "uv --project=../x sync",
  ])("asks for guarded option semantics: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
  it.each([
    "xxd -e y.txt",
    "jq --arg a b . y.txt",
    "jq --rawfile a x.txt . y.txt",
    "date -jf %s +1",
    "date -j -f %s 1 +%Y",
    "diff --to-file=a.txt b.txt",
    "find . -newermt x",
    "find -E . -name x",
    "ls -D %s x.txt",
    "ls -T x.txt",
    "stat -f %z x.txt",
    "sed -e's,i,j,' p",
    "sed p y.txt",
    "jq --argjson a 1 . y.txt",
    "jq --args . a",
  ])("admits ordinary read option semantics: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)).toBeUndefined()
  })
})

describe.each(["top level", "saved script"])("restricted configuration and edit forms: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each(["sed -i '' p input", "ruff --config ../ruff.toml check", "mypy --config-file ../mypy.ini src"])(
    "asks for the complete invocation: %s",
    (body) => {
      expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})

describe.each(["top level", "saved script"])("restricted project and container forms: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each([
    "mypy --config-file ../mypy.ini src",
    "go test ../...",
    "docker run --mount type=bind,src=../data,target=/data,readonly image true",
    "podman run --mount=type=bind,source=/tmp/data,target=/data,ro image true",
    "docker run --mount=type=bind,source=data,target=/data image true",
    "docker run --rm image sh -c 'echo safe'",
    "sed -i p input.txt",
    "sed --in-place p input.txt",
  ])("asks for the complete invocation: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})
