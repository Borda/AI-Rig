/** Table-driven shell probes with their expected verdicts. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

const persisted_probes = [
  ["cat > x.sh <<'EOF'\nfind . -maxdepth 0 -exec git push \\;\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\ncaffeinate git push\nEOF", "ask"], // caffeinate is a catalogued wrapper
  ["cat > x.sh <<'EOF'\nparallel git push ::: a\nEOF", "other"],
  ["cat > x.sh <<'EOF'\nif git push; then :; fi\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\nfor i in 1; do git push; done\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\n{ git push; }\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\n( git push )\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\n! git push\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\ntrue && git push\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\nX=1 git push\nEOF", "ask"],
  ["bash x.sh <<'EOF'\nif git push; then :; fi\nEOF", "ask"],
  ["bash x.sh <<'EOF'\n{ git push; }\nEOF", "ask"],
  ["bash x.sh <<'EOF'\ncaffeinate git push\nEOF", "ask"], // caffeinate is a catalogued wrapper
  ["bash x.sh <<'EOF'\nfind . -exec git push \\;\nEOF", "ask"],
  ["if git push; then :; fi", "ask"],
  ["caffeinate git push", "ask"],
  ["find . -exec git push \\;", "ask"],
  ["{ git push; }", "ask"],
  ["bash <(cat <<'EOF'\ngit push\nEOF\n)", "ask"],
  ["source /dev/stdin <<'EOF'\ngit push\nEOF", "ask"],
  ["cat <<'EOF' | sudo tee x.sh\ngit push\nEOF", "ask"],
  ["exec 3>x.sh; cat <<'EOF' >&3\ngit push\nEOF", "ask"],
  ["exec 3>x.sh\ncat >&3 <<'EOF'\ngit push\nEOF", "ask"],
  ["cat <<'EOF' 1<>x.sh\ngit push\nEOF", "ask"],
  ["cat <<'EOF' >x.sh 2>&1\ngit push\nEOF", "ask"],
  ["time cat > x.sh <<'EOF'\ngit push\nEOF", "ask"],
  ["case a in a) cat > x.sh;; esac <<'EOF'\ngit push\nEOF", "ask"],
  ["while read l; do echo \"$l\"; done > x.sh <<'EOF'\ngit push\nEOF", "ask"],
  ["while read -r l; do printf '%s\\n' \"$l\"; done <<'EOF' > x.sh\ngit push\nEOF", "ask"],
  ["mapfile -t a <<'EOF'\ngit push\nEOF\nprintf '%s\\n' \"${a[@]}\" > x.sh", "ask"],
  ["read -r -d '' S <<'EOF'\ngit push\nEOF\nbash -c \"$S\"", "ask"],
  ["cat <<'EOF' | bash -s\ngit push\nEOF", "ask"],
  ["bash -s x <<'EOF'\ngit push\nEOF", "ask"],
  ["zsh -o shwordsplit <<'EOF'\ngit push\nEOF", "ask"],
  ["bash -o -s <<'EOF'\ngit push\nEOF", "ask"],
  ["bash -O -s <<'EOF'\ngit push\nEOF", "ask"],
  ["bash +O -s <<'EOF'\ngit push\nEOF", "ask"],
  ["bash --rcfile -s <<'EOF'\ngit push\nEOF", "ask"],
  ["bash -ec 'cat' <<'EOF'\ngit push\nEOF", "ask"],
  ["bash -c 'bash' <<'EOF'\ngit push\nEOF", "ask"],
  ["bash -c 'cat | bash' <<'EOF'\ngit push\nEOF", "ask"],
  ["tee x.sh >/dev/null <<'EOF'\ngit push\nEOF", "ask"],
  ["cat <<'EOF' > /dev/fd/3 3>x.sh\ngit push\nEOF", "ask"],
  ["cat <<'EOF' >/dev/stdout >x.sh\ngit push\nEOF", "ask"],
  ["sed -n p <<'EOF' >x.sh\ngit push\nEOF", "ask"],
  ["sort -ox.sh <<'EOF'\ngit push\nEOF", "ask"],
  ["head -n1 - x.sh <<'EOF'\ngit push\nEOF", "other"],
  ["cat - <<'EOF' >>x.sh\ngit push\nEOF", "ask"],
  ["cat <<'EOF' | tee -a x.sh\ngit push\nEOF", "ask"],
  ["cat <<'EOF' | tee -- x.sh\ngit push\nEOF", "ask"],
  ["uniq -- - x.sh <<'EOF'\ngit push\nEOF", "ask"],
  ["cat <<'EOF' | uniq - x.sh\ngit push\nEOF", "ask"],
] as const

const persisted_probes2 = [
  ["cat > x.sh <<'EOF'\n{ git push; }\nEOF\nbash x.sh", "ask"],
  ["cat > x.sh <<< '{ git push; }'", "ask"],
  ["tee x.sh <<'EOF'\nwhile true; do gh pr merge 1; break; done\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\ncase a in a) git push;; esac\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\nf() { git push; }\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\nif true; then bash -c 'git push'; fi\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\nif true; then eval x; fi\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\n{ $'\\x67it' push; }\nEOF", "ask"],
  ["bash script.sh <<'EOF'\n{ git push; }\nEOF", "ask"],
  ["cat <<'EOF' | bash\n{ git push; }\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\ntimeout 5 git push\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\nnice -n 5 git push\nEOF", "ask"],
] as const

const persisted_overask = [
  ["cat > notes.md <<'EOF'\nUse `git push` to publish.\nEOF", "ask"],
  ["cat > README.md <<'EOF'\n## Install\n\n```bash\npip install foo\n```\nEOF", "other"],
  ['cat > a.json <<\'EOF\'\n{"cmd": "git status"}\nEOF', "other"],
  ["cat > loop.sh <<'EOF'\nfor f in *.py; do echo \"$f\"; done\nEOF", "other"],
  ["cat > t.sh <<'EOF'\nif [ -f x ]; then echo ok; fi\nEOF", "other"],
  ["python3 - <<'EOF'\nprint('hi')\nEOF", "ask"],
  ["python3 <<'EOF'\nimport subprocess\nsubprocess.run(['git','status'])\nEOF", "ask"],
  ["git log --oneline | head -5", "read-only-git"],
  ["git diff | grep -c '^+'", "read-only-git"],
  ["git status --short | wc -l", "read-only-git"],
  ["ls | sort -r | head -3", "other"],
  ["tail -20 build.log", "other"],
  ["head -5 README.md", "other"],
  ["sed -n '1,20p' file.txt", "other"],
  ["sed -n '/start/,/end/p' f", "other"],
  ["sed 's|a|b|g' f > g", "other"],
  ["echo $'a\\tb'", "ask"], // grammar gate: ANSI-C quoting
  ["printf '%s\\n' x > out.txt", "other"],
  ["npm test 2>&1 | tail -30", "other"],
  ["jq -r .version package.json", "other"],
  ["cat <<'EOF' | jq .\n{\"a\":1}\nEOF", "other"],
  ["gh pr view 3 --json title | jq -r .title", "read-only-git"],
  ["find . -name '*.ts' | xargs grep -l foo", "other"],
  ["bash -c 'npm run build'", "other"],
  ["cat > .env <<'EOF'\nGIT_AUTHOR_NAME=x\nEOF", "ask"],
  ["cat > Makefile <<'EOF'\ntest:\n\tpytest\nEOF", "other"],
  ["cat > x.yml <<'EOF'\nsteps:\n  - run: git fetch\nEOF", "other"],
  ["cat > notes.txt <<'EOF'\n(see git docs)\nEOF", "other"],
  ["cat > notes.txt <<'EOF'\n{ git is great }\nEOF", "ask"],
  ["cat > notes.txt <<'EOF'\nif git is installed, run it\nEOF", "ask"],
] as const

const persisted_overask2 = [
  ["cat > README.md <<'EOF'\n```python\nprint(1)\n```\nEOF", "ask"],
  ["cat > README.md <<'EOF'\n```\nnpm install\n```\nEOF", "other"],
  ["cat > README.md <<'EOF'\n```bash\nnpm install\n```\nEOF", "other"],
  ["cat > README.md <<'EOF'\nRun `npm test` first.\nEOF", "other"],
  ['cat > a.json <<\'EOF\'\n{"name": "x"}\nEOF', "other"],
  ['cat > a.json <<\'EOF\'\n{"cmd": "make"}\nEOF', "other"],
  ['cat > a.json <<\'EOF\'\n{\n  "scripts": {"hook": "git status"}\n}\nEOF', "other"],
  ['cat > a.json <<\'EOF\'\n{"x": "the git docs"}\nEOF', "other"],
  ["cat > CHANGELOG.md <<'EOF'\n- Fixed `git worktree` handling\nEOF", "ask"],
  ["cat > CHANGELOG.md <<'EOF'\n- Fixed git worktree handling (see `docs/`)\nEOF", "other"],
] as const

describe("persisted body probes", () => {
  it.each([...persisted_probes, ...persisted_probes2, ...persisted_overask, ...persisted_overask2])(
    "classifies %s as %s",
    (command, expected) => {
      const result = classifyCommand(command)
      if (expected === "ask") expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
      else {
        expect(result.kind).toBe(expected)
        expect(decide(result, "standard", GRANTED)).toBeUndefined()
      }
    },
  )
})

const quote_probes = [
  [String.raw`echo $'a\'b' ; x=gi ; ${"${x}"}t push ; echo \'`, "ask"],
  [String.raw`echo $'a\'b' ; git push ; echo \'`, "ask"],
  ["x=gi ; ${x}t push", "ask"],
  ["cat > x.sh <<'EOF'\n" + String.raw`echo $'a\'b' ; x=gi ; ${"${x}"}t push ; echo \'` + "\nEOF", "ask"],
  ['(( true << "1" ))\ngit push\n1', "ask"],
  ["x=gi\n(( true << 1 ))\n${x}t push\n1", "ask"],
  ["(( true << 1 ))\ngit push\n1", "ask"],
  ["cat > x.sh <<'EOF'\ncoproc git push\nEOF", "ask"],
  ["coproc git push", "ask"],
  ["bash x.sh <<'EOF'\ncoproc git push\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\nflock /tmp/l -c 'git push'\nEOF", "ask"],
  ["flock /tmp/l -c 'git push'", "ask"],
  ["cat > x.sh <<'EOF'\nnoglob git push\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\nrepeat 1 git push\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\n{ git push } always { true }\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\nselect i in a; do git push; done\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\n[[ -n x ]] && git push\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\ngi\\\nt push\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\nif true; then # c\n git push; fi\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\ncat <<< \"$(git push)\"\nEOF", "ask"],
  ["bash <<< 'echo hi'", "ask"],
  ["bash x <<'EOF'\necho safe\nEOF", "other"],
  ["cat > README.md <<'EOF'\n```bash\ngit clone https://example.com/r.git\n```\nEOF", "ask"],
  ["cat > run.sh <<'EOF'\n\"$PYTHON\" -m pytest\nEOF", "ask"],
  ["(( n > 0 )) && echo big", "ask"], // grammar gate: arithmetic command
  ["cat > x.sh <<'EOF'\nfor (( i=0; i<1; i++ )); do git push; done\nEOF", "ask"],
  ['flock /tmp/l -c "${x}t push"', "ask"],
  ['x=gi; flock /tmp/l -c "${x}t push"', "ask"],
  ["n < 5", "other"],
  ["( n < 5 )", "ask"], // grammar gate: subshell group
  ["bash <<'EOF'\nbash <<'IN'\ngit push\nIN\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\n{ \"git\" push; }\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\n( true && git push )\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\nuntil false; do git push; done\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\ntime git push\nEOF", "ask"],
  ["cat > x.sh <<'EOF'\ncommand git push\nEOF", "ask"],
  ["(( n < 5 )) && echo small", "ask"], // grammar gate: arithmetic command
  ["for ((i=0; i<3; i++)); do echo $i; done", "ask"], // grammar gate: arithmetic for loop
  ["[[ a < b ]] && echo x", "ask"], // grammar gate: [[ ]] conditional
  ["if (( $# < 1 )); then echo usage; fi", "ask"], // grammar gate: if with arithmetic
  ["cat > x.sh <<'EOF'\nfor ((i=0; i<3; i++)); do echo $i; done\nEOF", "other"],
  ["bash <<'EOF'\necho safe\nEOF", "ask"],
  ["python3 x.py < in.txt", "other"],
  ["node x.js < data.json", "other"],
  ["psql db < schema.sql", "other"],
  ["wc -l < f", "other"],
  ['while read l; do echo "$l"; done < f', "ask"], // grammar gate: while loop and read builtin
  ["diff a <(sort b)", "ask"], // grammar gate: process substitution
] as const

describe("quote, arithmetic and carrier probes", () => {
  it.each(quote_probes)("classifies %s as %s", (command, expected) => {
    const result = classifyCommand(command)
    if (expected === "ask") expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
    else {
      expect(result.kind).toBe(expected)
      expect(decide(result, "standard", GRANTED)).toBeUndefined()
    }
  })
})

const expression_probes = [
  ["git status", "read-only-git"],
  ["echo $((git status) )", "ask"],
  ['echo "$((git status) )"', "ask"],
  ["((git status) )", "ask"],
  ["((git status) ) && echo ok", "ask"],
  ["echo [[ ; git status ; echo ]]", "ask"], // grammar gate: unquoted [[ word
  ["true [[ x\ngit status\necho ]]", "ask"], // grammar gate: unquoted [[ word
  ["cat > x.sh <<'EOF2'\n((git status) )\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\necho $((git status) )\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\necho [[ ; git status ; echo ]]\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\nflock --comm='git status' lock\nEOF2", "ask"],
  ["flock --comm='git status' lock", "ask"],
  ["cat > x.sh <<'EOF2'\nsu --comm='git status' root\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\nscript --comm='git status' /dev/null\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\nsudo -r role git status\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\nflock --timeout 5 lock git status\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\ntime -f %e git status\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\nenv -P /bin git status\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\nstrace -a 40 git status\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\ndoas -a style git status\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\nsudo git status\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\ngit status\nEOF2", "ask"],
  ["sudo -r role git status", "read-only-git"],
  ["flock --timeout 5 lock git status", "read-only-git"],
  ["(( count++ ))", "ask"], // grammar gate: arithmetic command
  ["((i++)); echo $i", "ask"], // grammar gate: arithmetic command
  ["echo $(( $(wc -l < f) + 1 ))", "ask"], // grammar gate: arithmetic expansion
  ['[[ $a == "x" ]] || exit 1', "ask"], // grammar gate: [[ ]] conditional
  ["[[ -f a ]] && [[ -f b ]] && echo both", "ask"], // grammar gate: [[ ]] conditional
  ['for f in *.py; do python3 "$f" < in.txt; done', "ask"], // grammar gate: for loop
  ["sort < in.txt > out.txt", "other"],
  ["sqlite3 db < schema.sql", "other"],
  ["timeout 60 python3 x.py < in.txt", "other"],
  ["docker exec -i c psql < x.sql", "ask"],
  ["jq . < f.json", "other"],
  ["cat > s.sh <<'EOF2'\n#!/bin/bash\nset -euo pipefail\npython3 main.py\nEOF2", "other"],
  ["cat > x.sh <<'EOF2'\necho \"run git status to check\"\nEOF2", "other"],
  ["cat > ci.yml <<'EOF2'\nscript:\n  - git fetch origin\nEOF2", "ask"],
  ["cat > notes.md <<'EOF2'\n- git clone is how you start\nEOF2", "ask"],
  ["cat > x.sh <<'EOF2'\nif [[ $# -lt 1 ]]; then echo usage; exit 1; fi\n(( n = $1 * 2 ))\necho $n\nEOF2", "other"],
  ["cat > x.sh <<'EOF2'\nselect opt in a b; do echo $opt; done\nEOF2", "other"],
  ["echo $'it\\'s' && echo ok", "ask"], // grammar gate: ANSI-C quoting
  ["printf '%s\\n' $'a\\tb'", "ask"], // grammar gate: ANSI-C quoting
  ["grep -c $'\\t' f.tsv", "ask"], // grammar gate: ANSI-C quoting
  ["awk 'NR<5' < f", "ask"],
  ["cat > x.sh <<'EOF2'\nexec 3< in.txt\nwhile read -u 3 l; do echo \"$l\"; done\nEOF2", "other"],
  ['while IFS= read -r l; do echo "$l"; done <<< "$(ls)"', "ask"],
  ["diff <(sort a) <(sort b)", "ask"], // grammar gate: process substitution
  ["tee >(wc -l) < f > /dev/null", "ask"], // grammar gate: process substitution
  ["cat > t.zsh <<'EOF2'\n{ echo a } always { echo b }\nEOF2", "other"],
  ["for ((;;)); do break; done", "ask"], // grammar gate: arithmetic for loop
  ['ls | while read -r f; do echo "$f"; done', "ask"],
  ["find . -name '*.py' | while read -r f; do wc -l \"$f\"; done", "ask"],
  ["if ! [[ -d x ]]; then mkdir x; fi", "ask"], // grammar gate: if and [[ ]] conditional
  ["time [[ -d x ]]", "ask"], // grammar gate: time reserved word
  ["x=$(( (1 + 2) * 3 )); echo $x", "ask"], // grammar gate: assignment and arithmetic
  ["echo $(( (1 + 2) ))", "ask"], // grammar gate: arithmetic expansion
  ["(( (a + b) > 0 )) && echo y", "ask"], // grammar gate: arithmetic command
  ["((x = (1) ))", "ask"], // grammar gate: arithmetic command
  ['echo "$((1+2))"', "ask"], // grammar gate: arithmetic expansion
] as const

describe("expression and wrapper option probes", () => {
  it.each(expression_probes)("classifies %s as %s", (command, expected) => {
    const result = classifyCommand(command)
    if (expected === "ask") expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
    else {
      expect(result.kind).toBe(expected)
      expect(decide(result, "standard", GRANTED)).toBeUndefined()
    }
  })
})

// Evaluation-sink probes: zsh arithmetic in builtin arguments, option words built from expansions, other shells,
// exec-capable options, Git and shell configuration writes. Each was checked in real shells with an echo marker.
const SINK_ASKS: readonly string[] = [
  'python3 -c "import sys; print(sys.version)"',
  'python3 -c \'import os; os.system("gi"+"t push")\'',
  'node -e "console.log(1)"',
  "cat <<'EOF' $(\nEOF\n)\ngi$9t push\nEOF\necho END",
  "find . -ex$9ec sh -c 'gi$9t push' \\;",
  "touch ./-exec; find . -maxdepth 0 ?exec sh -c 'gi$9t push' \\;",
  "find . -maxdepth 0 -ok$9 sh -c 'gi$9t push' \\;",
  "find . -maxdepth 0 \"-exec\"$9 sh -c 'gi$9t push' \\;",
  "exit 'a[$(gi$9t push)]'",
  "printf %d 'a[$(gi$9t push)]'",
  "test -t 'a[$(gi$9t push)]'",
  "[ -t 'a[$(gi$9t push)]' ]",
  "cd .gi?; cat >> config <<'EOF'\n[core]\n\tfsmonitor = sh -c 'gi$9t push'\nEOF\ncd ..; git status",
  "cat >> .gi?/config <<'EOF'\n[core]\n\tfsmonitor = sh -c 'gi$9t push'\nEOF\ngit status",
  "cat >> ~/.gitconfig <<'EOF'\n[core]\n\tfsmonitor = sh -c 'gi$9t push'\nEOF\ngit status",
  "cat >> ~/.config/git/config <<'EOF'\n[core]\n\tfsmonitor = sh -c 'gi$9t push'\nEOF\ngit status",
  "cd \"$(git rev-parse --git-dir)\"; cat >> config <<'EOF'\n[core]\n\tfsmonitor = sh -c 'gi$9t push'\nEOF\ncd ..; git status",
  "tee -a \"$(git rev-parse --git-path config)\" <<'EOF'\n[core]\n\tfsmonitor = sh -c 'gi$9t push'\nEOF\ngit status",
  "cat >> ~/.ssh/config <<'EOF'\nHost *\n  ProxyCommand sh -c 'gi$9t push'\nEOF\ngit fetch",
  "tcsh -c 'set x=gi; ${x}t push'",
  "csh -c 'set x=gi; ${x}t push'",
  "ash -c 'gi$9t push'",
  "mksh -c 'gi$9t push'",
  "busybox sh -c 'gi$9t push'",
  "fish -c 'gi$9t push'",
  "yash -c 'gi$9t push'",
  'osascript -e \'do shell script "gi" & "t push"\'',
  "ssh -o ProxyCommand='sh -c \"gi$9t push\"' host",
  "make --eval='x:=$(shell gi$9t push)'",
  "npx -c 'gi$9t push'",
  "tar -cf x.tar --to-command='sh -c \"gi$9t push\"' f",
  "rsync -e 'sh -c \"gi$9t push\"' a b:",
  "at now <<'EOF'\ngi$9t push\nEOF",
  "crontab - <<'EOF'\n* * * * * gi$9t push\nEOF",
  "make -f - <<'EOF'\nall:\n\tgi$9t push\nEOF",
  "cat > Makefile <<'EOF'\nall:\n\tgi$9t push\nEOF\nmake",
  'cat > package.json <<\'EOF\'\n{"scripts":{"build":"gi$9t push"}}\nEOF\nnpm run build',
  "caffeinate gi$9t push",
  "echo {1.$9.3}",
  "git log {-$9-2..-3}",
  "exit 'HOME[$(gi$9t push)]'",
  "printf %d 'HOME[$(gi$9t push)]'",
  "printf '%*s' 'path[$(gi$9t push)]' x",
  "test -t 'argv[$(gi$9t push)]'",
  "[ -t 'HOME[$(gi$9t push)]' ]",
  "ls # exit 'HOME[$(gi$9t push)]'",
  "echo $(exit 'HOME[$(gi$9t push)]')",
  "cat > x.zsh <<'EOF'\nexit 'HOME[$(gi$9t push)]'\nEOF\nzsh x.zsh",
  "find . -maxdepth 0 -ex$9ec gi$9t push \\;",
  "find . -maxdepth 0 -e{x.$9.x}ec gi$9t push \\;",
  "tcsh -c 'gi\"\"t push'",
  "strace -o '|gi$9t push' true",
  "strace -o'|sh' true",
  "flock f -c 'gi$9t push'",
  "cat >> ~/.gitconfig <<'EOF'\n[core]\n\tfsmonitor = ./h\nEOF\ngit status",
  "cat > .gi?/hooks/pre-commit <<'EOF'\nexit 'HOME[$(gi$9t push)]'\nEOF",
  "cd .gi?/hooks && cat > post-checkout <<'EOF'\n#!/bin/zsh\nexit 'HOME[$(gi$9t push)]'\nEOF",
  "echo x > /dev/tcp/127.0.0.1/80",
  "sudo -u root gi$9t push",
  "env -i gi$9t push",
  "timeout 5 sh -c 'gi$9t push'",
  "xargs -I{} gi$9t {}",
  "echo push | xargs git",
  "TZ='HOME[$(gi$9t push)]' printf %d \"$TZ\"",
  'printf %d "$1"',
  "echo $(printf %d 'HOME[$(gi$9t push)]')",
  "echo `exit 'HOME[$(gi$9t push)]'`",
  "find . -name x -exec$9 echo {} \\;",
  "find . $X sh -c 'gi$9t push' \\;",
  "find . -maxdepth 0 -execdir$9 gi$9t push \\;",
  "env -C dir gi$9t push",
  "nice -n 5 gi$9t push",
  'cat > .gi?/hooks/post-index-change <<\'EOF\'\n#!/usr/bin/env python3\nimport os\nos.system("gi"+"t push")\nEOF\nchmod +x .gi?/hooks/post-index-change\ngit status',
  'cat > .git/hooks/post-index-change <<\'EOF\'\n#!/usr/bin/env python3\nimport os\nos.system("gi"+"t push")\nEOF\nchmod +x .git/hooks/post-index-change\ngit status',
  'cd .gi?/hooks && cat > post-index-change <<\'EOF\'\n#!/usr/bin/env python3\nimport os\nos.system("gi"+"t push")\nEOF\nchmod +x post-index-change',
  "cat > .gi*/hooks/post-index-change <<'EOF'\n#!/bin/sh\nx=gi; ${x}t push\nEOF",
  'cat > .git/hooks/post-index-change <<\'EOF\'\n#!/usr/bin/perl\nsystem "gi" . "t push"\nEOF\nchmod +x .git/hooks/post-index-change\ngit status',
  "cat > .git/hooks/post-index-change <<'EOF'\n#!/bin/zsh\nexit 'HOME[$(gi$9t push)]'\nEOF\nchmod +x .git/hooks/post-index-change\ngit status",
  "printf '%.*s' 'HOME[$(gi$9t push)]' x",
  "printf '%f' 'HOME[$(gi$9t push)]'",
  "echo -{x.$9.x}ec",
  "cat > x.csh <<'EOF'\nset x=gi\n${x}t push\nEOF\ntcsh x.csh",
  "cat <<'EOF'; echo \"a\nEOF\ngi$9t push\nEOF\necho END",
]

// Ordinary commands from the same probe set: data, literal numbers and plain Git reads stay quiet.
const SINK_CONTROLS: readonly string[] = [
  "git show HEAD^",
  "git diff HEAD^ HEAD",
  'printf "%s\\n" "$x"',
  'echo "a\\nb"',
  'grep -E "^\\s+foo" file',
  "ls my\\ file",
  "grep -rn TODO . --include=*.py",
  "grep 'x' f | sed 's/a/b/'",
  "echo a$",
  "git log --oneline -5",
  "git log main..feature",
  "gh pr view 12 --json title,body",
  "ls -la ~",
  "date -u +%Y-%m-%dT%H:%M:%SZ",
  "find . -name '*.py' -exec grep -l foo {} +",
  'echo "Done!"',
  "env | sort",
  "git rev-parse @{u}",
  "git log @{u}..HEAD",
  "echo C#",
  "ls ~+",
  "echo ~root",
  "cat <<'EOF'\nhello $x\nEOF",
  "cat <<EOF\nhello\nEOF",
  "TZ=UTC date",
  "LC_ALL=C sort f",
  "DEBUG=1 VERBOSE=1 npm test",

  "echo ${HOME}x",
  'echo "$@" "$*"',
  "echo $$ $? $#",
  "printf -- '-v' x",
  "printf '%s' -v",
  "printf %s -vfoo x",
  "echo -e 'a\\tb'",
  "doas -C x cat input",
  "ls # see docs",
  "ls\t-la",
  "ls  &&  pwd",
  "test -t 1 && echo tty",
  "[ -t 0 ]",
  "exit 1",
  "printf '%d\\n' 5",
  "printf '%5.2f' 3.14159",
  "cd +'HOME[$(gi$9t push)]'",
  "pwd 'HOME[$(gi$9t push)]'",
  "printf %c 'HOME[$(gi$9t push)]'",
  "true 'HOME[$(gi$9t push)]'",
]

// Ordinary commands from the same probe set that keep asking for a reason outside these sinks.
const SINK_CONTROLS_ASKING: readonly string[] = [
  "git diff *.py",
  '[ -f "$HOME/.bashrc" ] && echo ok',
  '[ -d "$dir/sub" ]',
  "kill -9 1234",
  'cd "$(git rev-parse --show-toplevel)" && ls',
  "cat <<EOF\nhello $USER\nEOF",
  "ls -la\r",
  "ls -la",
  "echo $#HOME",
  "ls # done!",
  "ls # run tests (fast)",
]

// Documented limits: inline programs and scripts of other languages run code the shell guard does not read.
const OTHER_LANGUAGE_LIMITS: readonly string[] = [
  'perl -e \'system("gi"."t push")\'',
  'awk \'BEGIN{system("gi" "t push")}\'',

  'cat > x.pl <<\'EOF\'\n#!/usr/bin/perl\nsystem "gi" . "t push"\nEOF\nchmod +x x.pl\n./x.pl',
  'cat > x.pl <<\'EOF\'\nsystem "gi" . "t push"\nEOF\nperl x.pl',
  "nohup ./x.sh",
]

/** Wrap a probe as a saved script that the same command line then runs with sh. */
const savedThenSh = (body: string) => `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`

describe("evaluation sinks ask at top level and as a saved script run by sh", () => {
  it.each([...SINK_ASKS, "echo $HOME:h", "sed -n '1e gi$9t push' f"])("asks for %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
    expect(decide(classifyCommand(savedThenSh(command)), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("ordinary commands beside evaluation sinks", () => {
  it.each(SINK_CONTROLS)("keeps %s quiet", (command) => {
    const result = classifyCommand(command)
    expect(["other", "read-only-git", "plain-commit"]).toContain(result.kind)
    expect(decide(result, "standard", GRANTED)).toBeUndefined()
  })

  it.each(SINK_CONTROLS_ASKING)("still asks for %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe("ask")
  })
})

describe("other-language programs stay documented limits", () => {
  it.each(OTHER_LANGUAGE_LIMITS)("judges inline source and retains opaque file limits: %s", (command) => {
    expect(decide(classifyCommand(command), "standard", GRANTED)?.effect).toBe(
      command.startsWith("perl -e") || command.startsWith("awk ") ? "ask" : undefined,
    )
  })
})

describe.each(["top level", "saved script"])("restricted tool forms: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level" ? body : `cat > x.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh x.sh`
  it.each(["docker ps --format {{.Names}}", "npm run test -- --watch=false", "ls\n# Step 1: build\nmake"])(
    "asks for the complete invocation: %s",
    (body) => {
      expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})

const RESTRICTED_OPTION_COMMANDS = [
  "sed -i p '1w /tmp/x.txt' y.txt",
  "sed -i '' '1w /tmp/x.txt' y.txt",
  "sed -i.bak '1w /tmp/x.txt' y.txt",
  "sed -i p 's/a/b/' y.txt",
  "sed -i q '1w ../x.txt' y.txt",
  "sed -n '1w /tmp/x.txt' y.txt",
  "sed -i = 's/a/b/w /tmp/x.txt' y.txt",
  "sed -e p -i '1w /tmp/x.txt' y.txt",
  "rm -rf src",
  "uv run rm -rf src",
  "npm explore x -- rm -rf src",
  "npm init x -- rm -rf src",
  "arch -arm64 rm -rf src",
  "npm explore x -- git push",
  "npm explore x -- curl -o y.txt https://example.invalid/a",
  "npm explore x -- sed -n '1w /tmp/x.txt' y.txt",
  "npm explore x -- sort --compress-program=gzip y.txt",
  "npm explore x -- bash y.sh",
  "npm explore x",
  "npm init x",
  "npm create x",
  "npm exec -- sed -n '1w /tmp/x.txt' y.txt",
  "npm explore y -- sed -n '1w /tmp/x.txt' y.txt",
  "yarn create x",
  "npm init -y",
  "npm init",
  "npm exec -p rm -- -r x",
  "npm exec -p rm -r x",
  "npm exec --package=rm -- -r x",
  "npx -p rm -- -r x",
  "npm exec -p sed -- -n '1w /tmp/x.txt' y.txt",
  "pnpm -w exec sed -n '1w /tmp/x.txt' y.txt",
  "pnpm -w exec rm -r x",
  "pnpm exec sed -n '1w /tmp/x.txt' y.txt",
  "npm exec -p x -- y",
  "npm exec rm -r x",
  "npm publish",
  "pnpm publish",
  "yarn npm publish",
  "yarn publish",
  "uv publish",
  "twine upload dist/x",
  "poetry publish",
  "hatch publish",
  "pdm publish",
  "cargo publish",
  "cargo yank --version 1.0.0",
  "npm unpublish x",
  "npm deprecate x y",
  "npm dist-tag add x@1 latest",
  "docker push img",
  "gem push x.gem",
  "flit publish",
  "rye publish",
  "npm owner add u x",
  "uv run twine upload dist/x",
  "npx twine upload dist/x",
  "npx np",
  "gh release create v1",
  "go env -w GOFLAGS=-toolexec=x",
  "go env -w CC=x",
  "npm config set script-shell x",
  "npm set script-shell x",
  "npm config set node-options x",
  "pnpm config set script-shell x",
  "yarn config set x y",
  "pip config set global.x y",
  "cargo config set x",
  "uv python pin 3.12",
  "git config core.pager x",
  "npm pkg set scripts.test=x",
  "npm config get x",
  "go env GOPATH",
  "go build -toolexec x",
  "go build --toolexec=x",
  "go build --toolexec x",
  "go test --exec=x",
  "go vet --vettool=x",
  "go -C ../x build",
  "go build -C=../x",
  "go build -overlay=../o.json",
  "go test -exec=x ./...",
  "go build ./...",
  "go test ./...",
  "go vet ./...",
  "go build -o ../bin/x .",
  "go build -modfile=../go.mod .",
  "go generate ./...",
  "mypy --config-file ../mypy.ini src",
  "mypy --config-file=/tmp/mypy.ini src",
  "ruff check --config ../ruff.toml",
  "ruff check --config=/tmp/ruff.toml",
  "pytest -c ../pytest.ini tests",
  "mypy --cache-dir ../c src",
  "ruff check --cache-dir ../c",
  "diff -D X a.txt b.txt",
  "less -T tags y.txt",
  "tail -f y.txt",
  "tail -r y.txt",
  "date -z UTC +%s",
  "sort --files0-from=l.txt",
  "diff --from-file=a.txt b.txt",
  "jq --jsonargs . 1",
  "sed -u p y.txt",
  "sed -i.bak -e p y.txt",
  "sed --in-place=.bak p y.txt",
  "sed -i.bak s/a/b/ y.txt",
  "sed -i s/a/b/ y.txt",
  "sed -e's,i,j,w ../x.txt' p",
  "sed -ne's,i,j,w ../x.txt' p",
  "pytest --rootdir=../x",
  "uv --project=../x sync",
  "uv run rm -r x",
  "uv run -- rm -r x",
  "npx rm -r x",
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
  "date -f %s +1",
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
]

describe.each(["top level", "saved script"])("tool and option corpus: %s", (scope) => {
  const wrap = (body: string) =>
    scope === "top level"
      ? body
      : `cat > x.sh <<'SAVED_END'
${body}
SAVED_END
sh x.sh`
  it.each(RESTRICTED_OPTION_COMMANDS)("asks for restricted execution or writes: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})
