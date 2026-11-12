/** Dynamic values, assignment targets and expression provenance never hide an executable command. */
import { describe, expect, it } from "vitest"
import { classifyCommand, decide } from "../../../src/guardrails.ts"

const GRANTED = { status: "granted", path: "approval.json" } as const

/** A saved script the same command line then runs with sh. */
const savedThenSh = (body: string) => `cat > outer.sh <<'SAVED_END'\n${body}\nSAVED_END\nsh outer.sh`
/** Input fed to a shell that also has a script operand, so the blanket shell-stdin gate does not decide. */
const scriptFed = (body: string) => `bash x.sh <<'SAVED_END'\n${body}\nSAVED_END`
const LIVE_AND_SAVED_CONTEXTS = [
  { name: "top level", wrap: (body: string) => body },
  { name: "saved script run by sh", wrap: savedThenSh },
]
const ALL_CONTEXTS = [...LIVE_AND_SAVED_CONTEXTS, { name: "script-operand shell input", wrap: scriptFed }]

interface Probe {
  readonly body: string
  readonly expected: "ask" | "other" | "read-only-git"
}

// Evaluation probes: arithmetic subscripts, printf/read/declare targets, prompt and startup variables,
// positional parameters and zsh qualifiers. Expectations include the grammar gate, under which shell
// assignments, arithmetic, loops and expansions with operators ask even without a Git name.
/** Checked at top level, as a saved script run by sh, and as input fed to a script-operand shell. */
const EVERYWHERE: readonly Probe[] = [
  { body: "git status", expected: "read-only-git" },
  { body: "for i in 1 2 3; do echo $((i * 2)); done", expected: "ask" },
  { body: "echo $((16#ff))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; ! printf -v x %s \"y[$a$b]\"; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; ! let x", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; time ! let x", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; while ! let x; do break; done", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; if ! let x; then :; fi", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; n=x; printf -v \"$n\" %s \"y[$a$b]\"; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; n=x; printf -v $n %s \"y[$a$b]\"; echo $((x+1))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; echo ${!x}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; echo \"${!x}\"", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; declare \"$x=1\"", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; f(){ local \"$x=1\"; }; f", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; export \"$x=1\"", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; getopts a \"$x\"", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; echo ${@:x}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; echo ${*:x}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; echo ${1:x}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; ls \"${@:x}\"", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; y=(1 2); echo $y[x]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; y=(1 2); ls $y[x]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; cat <<EOF\n${y[x]}\nEOF", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; cat <<EOF\n$[x]\nEOF", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; cat <<EOF\n${s:x}\nEOF", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; cat <<EOF\n$((x))\nEOF", expected: "ask" },
  {
    body: "a='$(gi'; b='t push)'; printf -v x %s \"y[$a$b]\"; cat > x.sh <<'EOF'\n(( x ))\nEOF\nsh x.sh",
    expected: "ask",
  },
  { body: 'f=README.md; n=3; head -n $((n+1)) "$f"', expected: "ask" },
  { body: "i=0; for f in a b; do i=$((i+1)); done", expected: "ask" },
  { body: "arr=(a b); i=0; echo ${arr[$i]}", expected: "ask" },
  { body: "files=(*.py); n=${#files[@]}; echo $((n-1))", expected: "ask" },
  { body: "f(){ local n=$1; echo $((n*2)); }; f 3", expected: "ask" },
  { body: "n=0; while (( n < 5 )); do n=$((n+1)); done", expected: "ask" },
  { body: "i=3; echo $(( i * 2 ))", expected: "ask" },
  { body: "x=5; [[ $x -eq 5 ]] && echo ok", expected: "ask" },
  { body: "for ((i=0;i<3;i++)); do echo $i; done", expected: "ask" },
  { body: "n=$#; (( n > 0 ))", expected: "ask" },
  { body: "(( $# < 1 ))", expected: "ask" },
  { body: "files=(*.py)", expected: "ask" },
  { body: "files=(src/*.ts)", expected: "ask" },
  { body: 'files=(*.py); echo "${files[@]}"', expected: "ask" },
  { body: "(( ${#a[@]} > 0 ))", expected: "ask" },
  { body: "x=$(( $(wc -l < f) + 1 ))", expected: "ask" },
  { body: 'f=a.txt; [[ $f ]] && cat "$f"', expected: "ask" },
  { body: "git push", expected: "ask" },
  { body: "printf -v 'a''[$(gi${_+}t push)]' %s 1", expected: "ask" },
  { body: "declare 'a''[$(gi${_+}t push)]'=1", expected: "ask" },
  { body: ": 'a[$(gi${_+}t push)]'; (( _ ))", expected: "ask" },
  { body: ": 'a[$(gi${_+}t push)]'; echo $(( $_ ))", expected: "ask" },
  { body: "[[ 'a[$(gi${_+}t push)]' =~ .* ]]; (( BASH_REMATCH ))", expected: "ask" },
  { body: "f() { (( $1 )); }; f 'a[$(gi${_+}t push)]'", expected: "ask" },
  { body: "declare x\"=\"'a[$(gi${_+}t push)]'; (( x ))", expected: "ask" },
  { body: "printf -\\v x %s 'a[$(gi${_+}t push)]'; (( x ))", expected: "ask" },
  { body: "f() { local x\"=\"'a[$(gi${_+}t push)]'; (( x )); }; f", expected: "ask" },
  { body: ": ${z:='a[$(gi${_+}t push)]'}; (( z ))", expected: "ask" },
  { body: "echo *(.e:'gi${_+}t push':)", expected: "ask" },
  { body: "x='*(e:gi${_+}t push:)'; echo ${~x}", expected: "ask" },
  { body: "a='*(e:gi'; b='t push:)'; x=$a$b; echo ${~x}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v \"x\"\"[$a$b]\" %s 1", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; declare \"x\"\"[$a$b]=1\"", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; : \"x\"\"[$a$b]\"; (( _ ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; f() { (( $1 )); }; f \"x\"\"[$a$b]\"", expected: "ask" },
  { body: "echo *(.e:'git push':)", expected: "ask" },
  { body: "printf -v 'a[$(git push)]' %s 1", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v x\"[$a$b]\" %s 1", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; read \"x\"\"[$a$b]\" <<< 1", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; getopts o \"x\"\"[$a$b]\"", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; printf -v \"x[$a$b]\" %s 1", expected: "ask" },
  { body: "printf -v 'x''[$(git push)]' %s 1", expected: "ask" },
  { body: "printf -v x[1] %s 1", expected: "ask" },
  { body: "read x[1] <<< 1", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; echo \"x[$a$b]\"", expected: "ask" },
  { body: ": 'a[$(git push)]'; (( _ ))", expected: "ask" },
  { body: "printf -v x[1] 'a[$(git push)]'; echo done", expected: "ask" },
  { body: "printf -v 'x''[$(git status)]' %s 1", expected: "ask" },
  { body: "printf -v $'x\\x5b$(git push)\\x5d' %s 1", expected: "ask" },
  { body: 'echo "x[$a$b]"', expected: "other" },
  { body: 'n=3; echo "result[$n]"', expected: "ask" },
  { body: "[[ $f ]]", expected: "ask" },
  { body: "printf '%s\\n' \"item[$i]\"", expected: "other" },
  { body: 'name=foo; grep -n "name[$name]" file.txt', expected: "ask" },
  { body: "printf -v x[1] %s 5", expected: "ask" },
  { body: "declare -A m; m[$k]=1", expected: "ask" },
  { body: "printf '%s' 'x[$(git push)]'", expected: "other" },
  { body: "a[$(git push)]=1", expected: "ask" },
  { body: "a['$(git push)']=1", expected: "ask" },
  { body: "a[1+$(git push)]=1", expected: "ask" },
  { body: "unset -v 'a''[$(git push)]'", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; declare -i x; x=\"$a$b\"", expected: "ask" },
  { body: "echo $((a[$(git push)]))", expected: "ask" },
  { body: "a=([$(git push)]=1)", expected: "ask" },
  { body: "echo hi", expected: "other" },
  { body: "f=1; ((f))", expected: "ask" },
]

/** Checked at top level and as a saved script run by sh. */
const LIVE_AND_SAVED: readonly Probe[] = [
  { body: "echo safe", expected: "other" },
  { body: "(( a || git push ))", expected: "ask" },
  { body: "(( x[$(git push)] ))", expected: "ask" },
  { body: "a=$(cat f); (( a ))", expected: "ask" },
  { body: "a=$(cat f); echo $(( a + 1 ))", expected: "ask" },
  { body: "a=$1; (( a ))", expected: "ask" },
  { body: 'a="$1"; (( a ))', expected: "ask" },
  { body: "a=${1}; (( a ))", expected: "ask" },
  { body: "a=$HOME; (( a ))", expected: "ask" },
  { body: "a=$b; (( a ))", expected: "ask" },
  { body: "a=b; b=$(cat f); (( a ))", expected: "ask" },
  { body: "a=b; b=$(cat f); echo $(( a ))", expected: "ask" },
  { body: "IFS= read -r a; (( a ))", expected: "ask" },
  { body: "read a b; (( b ))", expected: "ask" },
  { body: "read -ra arr; (( arr[0] ))", expected: "ask" },
  { body: "read -r -a arr; (( arr ))", expected: "ask" },
  { body: "read -d '' a; (( a ))", expected: "ask" },
  { body: "read -u 3 a; (( a ))", expected: "ask" },
  { body: "while read -r a; do (( a )); done < f", expected: "ask" },
  { body: "cat f | while read a; do (( a )); done", expected: "ask" },
  { body: "for a in $(cat f); do (( a )); done", expected: "ask" },
  { body: "for a; do (( a )); done", expected: "ask" },
  { body: "select a in x y; do (( a )); done", expected: "ask" },
  { body: "a=$(cat f) && (( a ))", expected: "ask" },
  { body: "export a=$(cat f); (( a ))", expected: "ask" },
  { body: "declare -n r=a; (( r ))", expected: "ask" },
  { body: "let a", expected: "ask" },
  { body: "a=$(cat f); let a", expected: "ask" },
  { body: 'a=$(cat f); let "x=a"', expected: "ask" },
  { body: "a=$(cat f); test a -eq 0", expected: "ask" },
  { body: "a=$(cat f); [ a -eq 0 ]", expected: "ask" },
  { body: 'a=$(cat f); [ "$a" -eq 0 ]', expected: "ask" },
  { body: "a=$(cat f); printf '%d' a", expected: "ask" },
  { body: "a=$(cat f); printf '%d' \"$a\"", expected: "ask" },
  { body: "a=$(cat f); declare -i b=a", expected: "ask" },
  { body: "a=$(cat f); declare -i b; b=a", expected: "ask" },
  { body: "a=$(cat f); b[a]=1", expected: "ask" },
  { body: "a=$(cat f); echo ${b[a]}", expected: "ask" },
  { body: "a=$(cat f); echo ${b:a}", expected: "ask" },
  { body: "a=$(cat f); echo ${b: a}", expected: "ask" },
  { body: "a=$(cat f); echo ${b:0:a}", expected: "ask" },
  { body: "a=$(cat f); echo ${b[@]:0:a}", expected: "ask" },
  { body: "a=$(cat f); shift a", expected: "ask" },
  { body: "a=$(cat f); exit a", expected: "ask" },
  { body: "a=$(cat f); return a", expected: "ask" },
  { body: "a=$(cat f); (( a )) ", expected: "ask" },
  { body: "a=$(cat f); ((\na ))", expected: "ask" },
  { body: "a=$(cat f); (( b = a ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; let x", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; let 'q=x'", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; declare -i n=x", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; declare -i n; n=x", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; typeset -i n=x", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; f() { local -i n=x; }; f", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; declare -i n=1; n+=x", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; z=x; (( z ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; z=x; echo $(( z ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; z=x; [[ z -eq 0 ]]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; z=x; echo ${arr[z]}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; arr=( [x]=1 )", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; declare -a arr=( [x]=1 )", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; declare -A A; A[$x]=1", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; case 1 in *) (( x ));; esac", expected: "ask" },
  { body: "z=x; a='$(gi'; b='t push)'; x=\"y[$a$b]\"; (( z ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; z=x; x=\"y[$a$b]\"; let z", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo $(( 1 ? x : 0 ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; for (( i=x; i<0; i++ )); do :; done", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo ${v:x}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo \"${v:x}\"", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo ${v:x:1}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo ${v[@]:x}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo ${v:-$(( x ))}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo ${v:=$(( x ))}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo ${v[x]:-1}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo ${#v[x]}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo ${!v[x]}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; v[x]+=1", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; unset 'v[x]'", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; unset v[x]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; [[ -v v[x] ]]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; [[ $x -eq 0 ]]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; [[ 0 -eq $x ]]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; [[ 0 -eq \"$x\" ]]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; [[ 0 -eq ${x} ]]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; [[ 0 -eq ${x:-0} ]]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; [[ 0 -eq $((x)) ]]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; [[ x -eq 0 && 1 ]]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; [[ x -eq\t0 ]]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; [[ x -eq 0 ]]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; printf -v n %d 1; (( n ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; IFS=x read -r n; (( n ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"\n(( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\" true; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x+=\"y[$a$b]\"; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=y; x+=\"[$a$b]\"; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=( \"y[$a$b]\" ); (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x[0]=\"y[$a$b]\"; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x[0]=\"y[$a$b]\"; (( x[0] ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; declare x=\"y[$a$b]\"; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; declare -g x=\"y[$a$b]\"; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; readonly x=\"y[$a$b]\"; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; export -n x=\"y[$a$b]\"; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; f() { x=\"y[$a$b]\"; }; f; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; eval x=1; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\" && (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; { x=\"y[$a$b]\"; }; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; if :; then x=\"y[$a$b]\"; fi; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; for x in \"y[$a$b]\"; do (( x )); done", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; select x in \"y[$a$b]\"; do (( x )); done", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; (( X ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; ((x))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; ((x)) && :", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; time (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; ! (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo $[ x ]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo $((x))$((1))", expected: "ask" },
  { body: "IFS=_; x=git_push; (( a || $x ))", expected: "ask" },
  { body: "[[ a || git -p status ]]", expected: "ask" },
  { body: "(( 0 || sh ))", expected: "ask" },
  { body: "(( 0 || eval ))", expected: "ask" },
  { body: "[[ a || sh -s ]]", expected: "ask" },
  { body: "x='git push'; (( a || $x ))", expected: "ask" },
  { body: "set -- 'git push'; (( a || $1 ))", expected: "ask" },
  { body: "x=git_push; IFS=_; $x", expected: "ask" },
  { body: "x=git_push; IFS=_; (( $x ))", expected: "ask" },
  { body: "set -- 'git push'; (( $1 ))", expected: "ask" },
  { body: "[[ a || git -p push -f ]]", expected: "ask" },
  { body: "[[ a || git -x push -f ]]", expected: "ask" },
  { body: "[[ -f a || git -p push -f ]]", expected: "ask" },
  { body: "if [[ a || git -p push -f ]]; then :; fi", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; echo $(( $(echo \"y[$a$b]\") ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; (( $(echo \"y[$a$b]\") ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo $(( $(echo \"$x\") ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo $(( $(echo x) ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo $(( `echo x` ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo hi > \"$(( x ))\"", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo hi > $(( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; cat < $(( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo hi >&$(( x + 3 ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo hi 2>$(( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo hi > ${v:x}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo hi > ${arr[x]}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; unset arr[x]", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; readonly x; (( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; readonly x=\"y[$a$b]\"; echo $(( x ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; echo ${!arr[x]}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; declare -a ar=( [x]=1 )", expected: "ask" },
  { body: "count=$(grep -c foo f.txt); if (( count > 0 )); then echo found; fi", expected: "ask" },
  { body: "n=${#arr[@]}; (( n > 0 )) && echo y", expected: "ask" },
  { body: "n=$#; (( n > 0 )) && echo y", expected: "ask" },
  { body: "total=0; for f in *.txt; do total=$((total + 1)); done; echo $total", expected: "ask" },
  { body: "while [[ $# -gt 0 ]]; do case $1 in -v) v=1;; esac; shift; done", expected: "ask" },
  { body: "[[ $# -eq 0 ]] && echo none", expected: "ask" },
  { body: "if [[ $1 == --help ]]; then echo usage; fi", expected: "ask" },
  { body: "for i in $(seq 1 3); do echo $i; done", expected: "ask" },
  { body: "i=0; while (( i < 3 )); do echo $i; ((i++)); done", expected: "ask" },
  { body: "x=5; y=$((x + 1)); echo $y", expected: "ask" },
  { body: "[[ -n $VAR ]] && echo set", expected: "ask" },
  { body: "[[ -f setup.py && -d src ]] && echo py", expected: "ask" },
  { body: "[[ $a -lt $b ]] && echo lt", expected: "ask" },
  { body: '[[ "$a" < "$b" ]] && echo lt', expected: "ask" },
  { body: "[[ ! -d build ]] && mkdir build", expected: "ask" },
  { body: '[[ -z "$CI" ]] || echo ci', expected: "ask" },
  { body: "(( $(nproc) > 4 )) && echo many", expected: "ask" },
  { body: "jobs=$(nproc); make -j$((jobs + 1))", expected: "ask" },
  { body: "start=$(date +%s); sleep 1; end=$(date +%s); echo $((end - start))", expected: "ask" },
  { body: "size=$(stat -c %s f); echo $((size / 1024))", expected: "ask" },
  { body: "arr=(a b c); echo ${arr[1]}", expected: "ask" },
  { body: 'arr=(a b c); for i in "${!arr[@]}"; do echo $i; done', expected: "ask" },
  { body: "echo ${PATH:0:10}", expected: "ask" },
  { body: "s=hello; echo ${s:1:2}", expected: "ask" },
  { body: "let i=i+1", expected: "ask" },
  { body: "declare -i n=5", expected: "ask" },
  { body: "x=$(cat VERSION); echo ${x%.*}", expected: "ask" },
  { body: "git status && [[ -f a ]]", expected: "ask" },
  { body: "if [[ -d .git ]]; then git status; fi", expected: "ask" },
  { body: "[[ -f a ]] && git log --oneline -3", expected: "ask" },
  { body: 'ls | while read -r f; do echo "$f"; done', expected: "ask" },
  { body: "pytest -q && echo ok", expected: "other" },
  { body: "npm test -- --watch=false", expected: "ask" },
  { body: "python -c 'print(1)'", expected: "ask" },
  { body: "[[ $x == y ]] || [[ $x == z ]]", expected: "ask" },
  { body: "[[ $branch == main ]] && echo main", expected: "ask" },
  { body: "[[ -n $1 ]] || { echo usage; exit 1; }", expected: "ask" },
  { body: "test -f a && echo y", expected: "other" },
  { body: "[ -f a ] && echo y", expected: "other" },
  { body: "echo $((2**10))", expected: "ask" },
  { body: "echo $(( (1 + 2) * 3 ))", expected: "ask" },
  { body: "echo $(( x > 0 ? 1 : 0 ))", expected: "ask" },
  { body: 'echo "$(( 1 + 2 ))"', expected: "ask" },
  { body: "for ((i=0; i<3; i++)); do echo $i; done", expected: "ask" },
  { body: "(( i += 2 ))", expected: "ask" },
  { body: "echo $(( -1 ))", expected: "ask" },
  { body: "echo $(( 10 % 3 ))", expected: "ask" },
  { body: "a=git; b=' status'; x=\"$a$b\"; [[ a || $x ]]", expected: "ask" },
  { body: 'x="git status"; [[ a || $x ]]', expected: "ask" },
  { body: "set -- git status; [[ a || $1 ]]", expected: "ask" },
  { body: "x='git push'; [[ -n a || $x ]]", expected: "ask" },
  { body: "x='git push'; (( a || ${x} ))", expected: "ask" },
  { body: "[[ a || $x ]]", expected: "ask" },
  { body: "(( a || $x ))", expected: "ask" },
  { body: "(( $x ))", expected: "ask" },
  { body: "=git status", expected: "ask" },
  { body: "=git push", expected: "ask" },
  { body: "echo hi; =gh pr merge 1", expected: "ask" },
  { body: "ls *(e:'git status':)", expected: "ask" },
  { body: "ls *(e:git_status:)", expected: "ask" },
  { body: "x='git status'; echo ${(e)x}", expected: "ask" },
  { body: "x='$(git status)'; echo ${(e)x}", expected: "ask" },
  { body: "a='$(gi'; b='t status)'; x=\"$a$b\"; echo ${(e)x}", expected: "ask" },
  { body: "a='$(gi'; b='t status)'; x=\"$a$b\"; print -r -- ${(e)x}", expected: "ask" },
  { body: "f() { git status; }; ls *(+f)", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"$a$b\"; echo ${~x}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"$a$b\"; echo ${(P)x}", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"$a$b\"; eval echo \\$x", expected: "ask" },
  { body: "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.is GIT_CONFIG_VALUE_0=push git is origin main", expected: "ask" },
  {
    body: "export GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.is GIT_CONFIG_VALUE_0=push; git is origin main",
    expected: "ask",
  },
  { body: "! x='a[$(git push)]'; (( x ))", expected: "ask" },
  { body: "! x='a[$(git push)]'; echo $((x))", expected: "ask" },
  { body: "x='a[$(git push)]'; ! let x", expected: "ask" },
  { body: "! printf -v x %s 'a[$(git push)]'; (( x ))", expected: "ask" },
  { body: "x='a[$(git push)]'; cat <<EOF\n$((x))\nEOF", expected: "ask" },
  { body: "x='a[$(git push)]'; cat <<EOF\n${y[x]}\nEOF", expected: "ask" },
  { body: "x='a[$(git push)]'; printf -v \"$x\" %s 1", expected: "ask" },
  { body: "x='a[$(git push)]'; read \"$x\" <<< 1", expected: "ask" },
  { body: "x='a[$(git push)]'; echo ${!x}", expected: "ask" },
  { body: "x='a[$(git push)]'; declare \"$x=1\"", expected: "ask" },
  { body: "x='a[$(git push)]'; f() { local \"$x=1\"; }; f", expected: "ask" },
  { body: "x='a[$(git push)]'; export \"$x=1\"", expected: "ask" },
  { body: "x='a[$(git push)]'; getopts a \"$x\"", expected: "ask" },
  { body: "x='a[$(git push)]'; y=(1 2); echo $y[x]", expected: "ask" },
  { body: "x='a[$(git push)]'; : ${z:=$x}; (( z ))", expected: "ask" },
  { body: "x='a[$(git push)]'; declare -A A; echo ${A[$x]}", expected: "ask" },
  { body: "x='a[$(git push)]'; [ -v \"$x\" ]", expected: "ask" },
  { body: "x='a[$(git push)]'; unset \"$x\"", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; (( x ))", expected: "ask" },
  { body: "if ! printf -v x %s 'a[$(git push)]'; then :; fi; (( x ))", expected: "ask" },
  { body: "n=x; printf -v \"$n\" %s 'a[$(git push)]'; (( x ))", expected: "ask" },
  { body: "n=x; printf -v $n %s 'a[$(git push)]'; echo $(( x + 1 ))", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; ! let x", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; if ! let x; then :; fi", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; cat <<EOF\n$((x))\nEOF", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; cat <<EOF\n${y[x]}\nEOF", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; cat <<EOF\n$[x]\nEOF", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; echo ${!x}", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; declare \"$x=1\"", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; f() { local \"$x=1\"; }; f", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; read \"$x\" <<< 1", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; export \"$x=1\"", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; getopts a \"$x\"", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; printf -v \"$x\" %s 1", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; y=(1 2); echo $y[x]", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; echo ${y[x]}", expected: "ask" },
  { body: "printf -v x %s 'a[$(git push)]'; echo \"${!x}\"", expected: "ask" },
  { body: 'for i in "${!arr[@]}"; do echo "${arr[$i]}"; done', expected: "ask" },
  { body: 'arr=(a b c); i=1; echo "${arr[i]}"', expected: "ask" },
  { body: "s=hello; n=2; echo ${s:0:n}", expected: "ask" },
  { body: 'f() { local msg=$1; if [[ $2 -gt 0 ]]; then echo "$msg"; fi; }', expected: "ask" },
  { body: "for f in *.py; do n=$((n+1)); done; echo $n", expected: "ask" },
  { body: "[[ ${#arr[@]} -gt 0 ]] && echo yes", expected: "ask" },
  { body: "[[ -n ${VAR:-} ]] && echo yes", expected: "ask" },
  { body: '[[ -z "${VAR:-}" ]] && echo yes', expected: "ask" },
  { body: "[[ $x =~ ^(foo|bar)$ ]] && echo yes", expected: "ask" },
  { body: "[[ $x =~ ^v[0-9]+\\.[0-9]+$ ]] && echo yes", expected: "ask" },
  { body: "[[ $x == *.py ]] && echo yes", expected: "ask" },
  { body: "[[ -f a.txt && ! -d b ]] && echo yes", expected: "ask" },
  { body: "[[ ${1:-} == --help ]] && echo usage", expected: "ask" },
  { body: "[[ -n $x ]] || exit 1", expected: "ask" },
  { body: "(( ${#arr[@]} > 0 )) && echo yes", expected: "ask" },
  { body: "(( $# > 1 )) && echo many", expected: "ask" },
  { body: 'x=$(ls | wc -l); echo "$x"', expected: "ask" },
  { body: 'name=foo; echo "${name^^}"', expected: "ask" },
  { body: "n=0; while (( n < 3 )); do n=$((n+1)); done", expected: "ask" },
  { body: "line=abc; echo ${#line}", expected: "ask" },
  { body: "[[ $x == a && $y == b || $z == c ]]", expected: "ask" },
  { body: "[[ -e /tmp/x ]]", expected: "ask" },
  { body: "[[ $(uname) == Darwin ]] && echo mac", expected: "ask" },
  { body: "case $1 in -h) echo help;; esac", expected: "ask" },
  { body: 'export PATH="$HOME/bin:$PATH"; ls', expected: "ask" },
  { body: 'IFS=, read -r a b <<< "1,2"; echo $((a+b))', expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; set -- a b; echo ${@:x}", expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; echo ${*:x}", expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; echo ${1:x}", expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; ls \"${@:x}\"", expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; s=abc; cat <<EOF\n${s:x}\nEOF", expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; y=(1 2); echo $y[x]", expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; y=(1 2); ls $y[x]", expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; echo ${#y[x]}", expected: "ask" },
  { body: "! printf -v x %s 'y[$(git push)]'; echo $((x))", expected: "ask" },
  { body: "! { printf -v x %s 'y[$(git push)]'; }; (( x ))", expected: "ask" },
  { body: "n=x; printf -v \"$n\" %s 'y[$(git push)]'; echo ${y[x]}", expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; time ! let x", expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; while ! let x; do break; done", expected: "ask" },
  { body: "bash -c '! printf -v x %s \"y[\\$(git push)]\"; (( x ))'", expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; echo ${x[@]:0:x}", expected: "ask" },
  { body: "printf -v x %s 'y[$(git push)]'; printf -v z %s \"${@:x}\"", expected: "ask" },
  { body: 'for f in src/*.ts; do wc -l "$f"; done', expected: "ask" },
  { body: "for i in {1..5}; do echo $i; done", expected: "ask" },
  { body: "for ((i=0;i<5;i++)); do echo $i; done", expected: "ask" },
  { body: "for i in 1 2 3; do echo $((i*2)); done", expected: "ask" },
  { body: "for i in $(seq 1 3); do echo $((i*2)); done", expected: "ask" },
  { body: "files=(*.py); echo ${#files[@]}", expected: "ask" },
  { body: "arr=(a b); echo ${arr[0]}", expected: "ask" },
  { body: "arr=(a b); for i in 0 1; do echo ${arr[$i]}; done", expected: "ask" },
  { body: 'dir=build; mkdir -p "$dir"; ls "$dir" | wc -l', expected: "ask" },
  { body: "dir=build; i=0; while [ $i -lt 3 ]; do i=$((i+1)); done", expected: "ask" },
  { body: "start=$(date +%s); sleep 1; end=$(date +%s); echo $((end-start))", expected: "ask" },
  { body: "x=$(git rev-parse HEAD); echo $x", expected: "ask" },
  { body: "git log --oneline | head -5", expected: "read-only-git" },
  { body: "find . -name '*.py' | xargs wc -l", expected: "other" },
  { body: "ver=$(node --version); echo ${ver:1}", expected: "ask" },
  { body: "s=abcdef; echo ${s:1:3}", expected: "ask" },
  { body: "name=foo-bar; echo ${name:0:3}", expected: "ask" },
  { body: "msg='hello world'; echo ${#msg}", expected: "ask" },
  { body: "export NODE_ENV=test; npx vitest run", expected: "ask" },
  { body: "PORT=3000; echo $((PORT+1))", expected: "ask" },
  { body: 'out=dist; [[ -d $out ]] && rm -rf "$out"', expected: "ask" },
  { body: 'f() { echo "$1"; }; f a', expected: "ask" },
  { body: "f() { local n=$1; echo $((n*2)); }; f 3", expected: "ask" },
  { body: "set -- a b c; echo $#", expected: "ask" },
  { body: "TAG=v1.2.3; echo ${TAG#v}", expected: "ask" },
  { body: "x=1; y=2; echo $((x+y))", expected: "ask" },
  { body: "if [[ -f package.json ]]; then npm ci; fi", expected: "ask" },
  { body: "cmd=ls; $cmd -la", expected: "ask" },
  { body: "files=(*.py); echo hi", expected: "ask" },
  { body: "files=(a b); echo ${#files[@]}", expected: "ask" },
  { body: "x=; (( y ))", expected: "ask" },
  { body: "cat > notes.md <<'EOF'\ngit is a distributed VCS\nEOF", expected: "other" },
  { body: "cat > notes.md <<'EOF'\ngit is great: fast and small\nEOF", expected: "ask" },
  { body: "cat > notes.md <<'EOF'\nGit is a VCS. See docs/git.md\nEOF", expected: "ask" },
  { body: "cat > run.sh <<'EOF'\ngit is push\nEOF", expected: "other" },
  { body: "cat > run.sh <<'EOF'\ngit is x; git push\nEOF", expected: "ask" },
  { body: "cat > run.sh <<'EOF'\ngit is x >&2\nEOF", expected: "other" },
  { body: "cat > run.sh <<'EOF'\n! printf -v x %s 'y[$(git push)]'; (( x ))\nEOF", expected: "ask" },
  { body: "cat > run.sh <<'EOF'\nn=x; printf -v \"$n\" %s 'y[$(git push)]'; (( x ))\nEOF", expected: "ask" },
  { body: "ls -la", expected: "other" },
  { body: "x=1; (( x > 0 )) && git push", expected: "ask" },
]

/** Bodies carrying their own heredoc, checked at top level only. */
const LIVE_ONLY: readonly Probe[] = [
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; read -r n <<< x; (( n ))", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; cat <<EOF\n$(( x ))\nEOF", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; cat <<<\"$(( x ))\"", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nIFS=_; x=git_push; (( a || $x ))\nEOF\nsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nIFS=_; x=git_push; (( a || $x ))\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nIFS=_; x=git_push; (( a || ${x} ))\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nIFS=_; x=git_status; [[ a || $x ]]\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nset -- 'git push'; (( a || $1 ))\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nset -- 'git status'; [[ a || $1 ]]\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nx='git push'; (( a || $x ))\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n[[ a || git -p status ]]\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n[[ a || git -p push -f ]]\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n[[ a || sh -s ]]\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n(( 0 || sh ))\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n(( 0 || git ))\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n(( 0 || git-shell ))\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n(( 0 || git-receive-pack ))\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n[[ a || eval -n ]]\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n(( a || git push ))\nEOF\nsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\ngit push\nEOF\nsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n$x\nEOF", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nx=git_push; IFS=_; $x\nEOF", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nx=git_push; IFS=_; (( $x ))\nEOF", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nset -- 'git push'; $1\nEOF", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nset -- 'git push'; (( $1 ))\nEOF", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nset -- git_push; IFS=_; [[ -n $1 || $1 ]]\nEOF", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n[[ a || gh -p status ]]\nEOF", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n[[ a || git -p commit -a ]]\nEOF", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n[[ a || git -p push -u ]]\nEOF", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n[[ a || bash -s ]]\nEOF", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n[[ a || ! git ]]\nEOF", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n[[ a || git -n -o push ]]\nEOF", expected: "ask" },
  { body: "a='$(gi'; b='t push)'; x=\"y[$a$b]\"; cat <<<$(( x ))", expected: "ask" },
  { body: "(( x = 1 << 3 ))", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\na=git; b=' status'; x=\"$a$b\"; [[ a || $x ]]\nEOF\ndash x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nx=\"git status\"; [[ a || $x ]]\nEOF\nsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nx='git push'; [[ -n a || $x ]]\nEOF\nsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nif true; then :; else [[ a || $x ]]; fi\nEOF\nsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n(( $x ))\nEOF\nsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\n=git push\nEOF\nzsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nls *(e:'git push':)\nEOF\nzsh x.sh", expected: "ask" },
  {
    body: "cat > x.sh <<'EOF'\nGIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.is GIT_CONFIG_VALUE_0=push git is origin main\nEOF\nsh x.sh",
    expected: "ask",
  },
  {
    body: "cat > x.sh <<'EOF'\nexport GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.is GIT_CONFIG_VALUE_0=push\ngit is origin main\nEOF\nsh x.sh",
    expected: "ask",
  },
  {
    body: "cat > x.sh <<'EOF'\nenv GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.is GIT_CONFIG_VALUE_0=push git is origin main\nEOF\nsh x.sh",
    expected: "ask",
  },
  { body: "cat > x.sh <<'EOF'\ngit is fine\nEOF\nsh x.sh", expected: "other" },
  { body: "cat > x.sh <<'EOF'\nx=1 git is origin main\nEOF\nsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\ncommand git is origin main\nEOF\nsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\nsudo git is origin main\nEOF\nsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\ngit is origin main\nEOF\nsh x.sh", expected: "other" },
  { body: "cat > x.sh <<'EOF'\ngit is origin HEAD~1\nEOF\nsh x.sh", expected: "ask" },
  { body: "cat > x.sh <<'EOF'\ngit is origin +main\nEOF\nsh x.sh", expected: "ask" },
]

const PROBE_CONTEXTS = [
  { name: "top level", wrap: (body: string) => body, probes: [...EVERYWHERE, ...LIVE_AND_SAVED, ...LIVE_ONLY] },
  { name: "saved script run by sh", wrap: savedThenSh, probes: [...EVERYWHERE, ...LIVE_AND_SAVED] },
  { name: "script-operand shell input", wrap: scriptFed, probes: EVERYWHERE },
]

describe.each(PROBE_CONTEXTS)("evaluation probes at $name", ({ name, wrap, probes }) => {
  // A read-only Git read only stays quiet at top level; saved or fed, any Git command asks.
  it.each(probes)("classifies $body as $expected", ({ body, expected }) => {
    const result = classifyCommand(wrap(body))
    if (expected === "ask" || (expected === "read-only-git" && name !== "top level"))
      expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
    else {
      expect(result.kind).toBe(expected)
      expect(decide(result, "standard", GRANTED)).toBeUndefined()
    }
  })
})

const sources = [
  "input=$value",
  "input+=$value",
  "input=$(printf 1)",
  "read -r input",
  "printf -v input %s value",
  "mapfile -t input",
  "readarray input",
  "getopts ab input",
  "declare input=$value",
  "typeset input=$value",
  "local input=$value",
  "export input=$value",
  "readonly input=$value",
  "for input in $items; do :; done",
  "select input in $items; do break; done",
  "set -- 'program args'",
  "shift '$amount'",
  "env input=$value true",
]
const sinks = [
  "(( unrelated ))",
  "echo $(( unrelated ))",
  "echo $[ unrelated ]",
  "let unrelated",
  "for ((i=unrelated;i<3;i++)); do :; done",
  "[[ unrelated -eq 1 ]]",
  "[[ -v unrelated ]]",
  "declare -i n=unrelated",
  "typeset -i n=unrelated",
  "local -i n=unrelated",
  "a[unrelated]=1",
  "a[unrelated]+=1",
  "echo ${a[unrelated]}",
  "echo ${!a[unrelated]}",
  "echo ${#a[unrelated]}",
  "unset 'a[unrelated]'",
  "a=( [unrelated]=1 )",
  "declare -a a=( [unrelated]=1 )",
  "echo ${v:unrelated:1}",
  "echo hi > $(( unrelated ))",
  "echo hi >&$(( unrelated + 3 ))",
  "cat <<<$(( unrelated ))",
]

describe.each(LIVE_AND_SAVED_CONTEXTS)("dynamic values reaching arithmetic sinks at $name", ({ name, wrap }) => {
  it.each(sources.flatMap((source) => sinks.map((sink) => `${source}; ${sink}`)))(
    "asks for an uncertain sink regardless of name linkage: %s",
    (body) => {
      expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )

  it.each(["echo '*(e:code:)'"])("keeps quoted qualifier text quiet: %s", (body) => {
    const result = classifyCommand(wrap(body))
    expect(result.kind).toBe("other")
    expect(decide(result, "standard", GRANTED)).toBeUndefined()
  })

  // Grammar gate: assignments, arithmetic, loops, [[ ]], if, set and shift leave the grammar, so
  // these forms ask in both contexts (live text and a saved script run by sh).
  it.each([
    "n=$?; (( n > 0 ))",
    "n=$$; (( n > 0 ))",
    "a=b; b=c; c=3; (( a ))",
    "n=${#name}; (( n > 0 ))",
    "n=${#name[@]}; (( n > 0 ))",
    "if (( $# < 1 )); then echo x; fi",
    "echo $(( 1 + 2 ))",
    "(( a[i]++ ))",
    "n=2#101; (( n > 0 ))",
    "(( $1 > 2 ))",
    "(( ${n} + $1 ))",
    "a=$((b+1)); b=2; ((a))",
    "set -- 1 2 3; (( $1 > 0 ))",
    "shift 1; (( $1 > 0 ))",
    "[[ ( ! -d x || -f y ) && a = a ]]",
  ])("asks for literal arithmetic and external values outside the grammar: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "count=$(grep -c foo f); if (( count > 0 )); then echo found; fi",
    "jobs=$(nproc); make -j$((jobs+1))",
    "read -r n; (( n > 0 ))",
    "a=b; b=c; c=$input; (( a ))",
    "a=$((b+1)); b=$input; ((a))",
    "IFS=1; (( a || $external ))",
    "IFS=1; [[ a || $external ]]",
    "set -- 'program args'; (( a || $@ ))",
    "shift '$amount'; (( a || ${1} ))",
    "x=$input; [[ a || ${x}${x} ]]",
    "x=$input; [[ a || prefix$x ]]",
    "declare -i n; read n",
    "declare -i n; printf -v n %s input",
    "declare -i n; mapfile n",
    "declare -i n; getopts ab n",
    "getopts ab opt; (( OPTARG ))",
    "x=$input; cat <<EOF\n$(( unrelated ))\nEOF",
    "echo ${(e)value}",
    "echo ${(q)value}",
    "ls *(e@echo marker@)",
    "ls *(+function)",
    "[[ a || (git -p status) ]]",
    "[[ a -v ]]",
    "[[ a b c ]]",
    "input=$value; echo $((16#ff + 1))",
    "input=$value; [[ 1 -eq 1 ]]",
    "input=$value; a[1]=2",
    "input=$value; echo ${v:1:2}",
  ])("asks for accepted costs and executable operands: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  // Single-quoted text is inert data in the grammar, and the raw-text gate that asked for it
  // now applies only to saved data. Live text is quiet; the saved body asks.
  it("treats single-quoted zsh flag text as data only in live text", () => {
    const result = classifyCommand(wrap("echo '${(e)value}'"))
    if (name === "top level") expect(decide(result, "standard", GRANTED)).toBeUndefined()
    else expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })
})

const producers = [
  "printf '-v' x %s value",
  'printf "-v" x %s value',
  '"printf" "-v" x %s value',
  'declare "prefix$x=1"',
  'local "prefix${x}=1"',
  'export "prefix$x=1"',
  "x=(1+2)",
  "for x in 1+2; do :; done",
  "set -- 1+2",
  "x=$value",
  "x=$(echo value)",
  "x=(1 text)",
  "read x",
  'read "$n"',
  "mapfile x",
  "readarray x",
  'getopts a "$n"',
  'printf -v "$n" %s 1',
  "printf -v $n %s 1",
  'declare "$n=1"',
  'typeset "$n=1"',
  'local "$n=1"',
  'export "$n=1"',
  'readonly "$n=1"',
  "for x in 1 text; do :; done",
  "select x in text; do break; done",
  "set -- 1 text",
  "local n=$1",
]
const tokens = [
  "(( 1 ))",
  "echo $(( 1 ))",
  "echo $[1]",
  "let 1",
  "echo ${!name}",
  "echo ${s:1}",
  "echo ${@:1}",
  "echo ${*:1}",
  "echo ${1:1}",
  "echo a[1]",
  "echo ${a}[1]",
  "echo a[1][2]",
  "test 1 -eq 1",
  "test 1 -ne 1",
  "test 1 -lt 1",
  "test 1 -le 1",
  "test 1 -gt 1",
  "test 1 -ge 1",
  "[[ -v x ]]",
]
describe.each(LIVE_AND_SAVED_CONTEXTS)("value producers combined with arithmetic tokens at $name", ({ name, wrap }) => {
  // Grammar gate: assignments outside the allowed names, arithmetic, subscripts and test operators
  // leave the grammar, so these combinations ask in both contexts.
  it.each(["x=text", "x+=text"].flatMap((producer) => tokens.map((token) => `${producer}; ${token}`)))(
    "asks for literal assignment values with arithmetic outside the grammar: %s",
    (body) => {
      expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
  it.each(producers.flatMap((producer) => tokens.map((token) => `${producer}; ${token}`)))(
    "asks regardless of which variable is evaluated: %s",
    (body) => {
      expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
  it.each([
    "n=$(( $(printf '))') + 1 ))text; (( n ))",
    "n=$(( $(printf '))') + 1 ))$value; (( n ))",
    "files=($(git push))",
    "files=(`git push`)",
    "files=(a $(git push) b)",
    "files=($(git push)); echo ${files[@]}",
    'echo "${name@P}"',
  ])("asks for raw text even when quoted or prefixed: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
  // Single-quoted text is inert data in the grammar, and the raw-text gate that asked for it
  // now applies only to saved data. Live text is quiet; the saved body asks.
  it.each(["echo '${(q)x}'", "echo '${name@P}'", "echo 'x=text (( 1 ))'"])(
    "treats single-quoted raw text as data only in live text: %s",
    (body) => {
      const result = classifyCommand(wrap(body))
      if (name === "top level") expect(decide(result, "standard", GRANTED)).toBeUndefined()
      else expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
    },
  )
  it.each(["cat <<'TEXT'\nx=text\n(( 1 ))\nTEXT"])("keeps quoted heredoc data quiet: %s", (body) => {
    const result = classifyCommand(wrap(body))
    expect(result.kind).toBe("other")
    expect(decide(result, "standard", GRANTED)).toBeUndefined()
  })
  // Grammar gate: expanding heredocs, assignments, arithmetic, loops, if, set and expansions with
  // operators leave the grammar, so these controls ask in both contexts.
  it.each([
    "cat <<TEXT\nx=text\necho ${s:x}\nTEXT",
    "time ! let x; x=text",
    "while ! let x; do break; done; x=text",
    "if ! let x; then :; fi; x=text",
    "x=text; echo ${x:-default}",
    "x=text; echo ${x:+default}",
    "x=text; echo ${x:=default}",
    "x=text; echo ${x:?default}",
    "x=text; echo ${x[@]}",
    "x=text; echo ${x[*]}",
    "x=; (( n > 0 ))",
    "x=16#ff; (( x > 0 ))",
    "x=-3; (( x < 0 ))",
    "x=$?; (( x > 0 ))",
    "x=$$; (( x > 0 ))",
    "x=${#name}; (( x > 0 ))",
    "x=${#name[@]}; (( x > 0 ))",
    "arr=(1 2); echo ${arr[1]}",
    "set -- 1 2; (( $1 > 0 ))",
  ])("asks for isolated producers and numeric controls outside the grammar: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
  // Grammar gate: an IFS assignment is outside the allowed names, so these ask.
  it.each(["IFS=1; (( $n + 1 ))", 'IFS=1; [[ -n "$x" ]]', "IFS=1; echo $((1))"])(
    "asks for numeric IFS assignments outside the grammar: %s",
    (body) => {
      expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
    },
  )
})

describe.each(ALL_CONTEXTS)("assignment targets, quoting and zsh qualifiers at $name", ({ wrap }) => {
  it.each(["ls *.py", "[ -f x ] && echo ok", "grep -v foo file"])(
    "keeps literal and isolated controls quiet: %s",
    (body) => {
      const result = classifyCommand(wrap(body))
      expect(result.kind).toBe("other")
      expect(decide(result, "standard", GRANTED)).toBeUndefined()
    },
  )

  // Grammar gate: assignments outside the allowed names, arrays, printf -v, arithmetic and loops
  // leave the grammar, so these ask in every context here.
  it.each([
    "printf -v x %s 5",
    "x=foo; grep -v bar f",
    "printf -v x[10] %s 5",
    "printf -v x[@] %s 5",
    "printf -v x[*] %s 5",
    "x=foo-bar; echo $((n+1))",
    "x=./src/foo:@%+,=-; echo $((n+1))",
  ])("asks for controls outside the grammar: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  const targets = [
    "printf -v TARGET %s 5",
    "read -r TARGET",
    "mapfile -t TARGET",
    "readarray -t TARGET",
    "getopts o TARGET",
    "wait -p TARGET",
    "declare TARGET=1",
    "typeset TARGET=1",
    "local TARGET=1",
    "export TARGET=1",
    "readonly TARGET=1",
    "unset -v TARGET",
    "let TARGET=1",
  ]
  const spellings = [
    "'x''[$a$b]'",
    '"x""[$a$b]"',
    String.raw`$'x\x5b$a$b\x5d'`,
    String.raw`$'x\133$a$b\135'`,
    String.raw`$'x\u005b$a$b\u005d'`,
    String.raw`$'x\U0000005b$a$b\U0000005d'`,
    String.raw`$"x\x5b$a$b\x5d"`,
    '"[foo]"',
  ]
  it.each(
    targets.flatMap((target) =>
      spellings.map((spelling) => `a='$(gi'; b='t push)'; ${target.replace("TARGET", () => spelling)}`),
    ),
  )("asks for normalized assignment targets: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "a='$(gi'; b='t push)'; test -v x",
    "a='$(gi'; b='t push)'; [ -v x ]",
    "a='$(gi'; b='t push)'; test 1 -a -v x",
    "a='$(gi'; b='t push)'; [ 1 -a -v x ]",
    "a='foo'; test '-''v' x",
    "a='$(gi'; b='t push)'; [[ -v x ]]",
    ...["declare", "local", "typeset", "readonly"].flatMap((builtin) =>
      ["-n", "-rn", "-nx"].map((flag) => `a='$(gi'; b='t push)'; ${builtin} ${flag} x=y`),
    ),
    "echo *(.e:'echo canary':)",
    "echo *(Ne:'echo canary':)",
    "echo *(.+f)",
    "echo file(.)",
    "echo 'file'(.)",
    'echo "file"(.)',
    "echo foo?(bar)",
    "echo @(foo|bar)",
    "echo *(foo|bar)",
    "echo ${~x}",
    "setopt globsubst; echo $x",
    "setopt GLOB_SUBST; echo $x",
    "setopt -o globsubst; echo $x",
    "echo ${(|)x}",
    String.raw`a=$'foo\nbar'; (( n ))`,
    String.raw`a=$'foo\tbar'; (( n ))`,
    String.raw`a=$'foo\'bar'; (( n ))`,
    String.raw`a=$'foo\\bar'; (( n ))`,
    "a='foo'; (( n ))",
    'a="foo"; (( n ))',
    "a=foo*; (( n ))",
  ])("asks for other dynamic expression and qualifier classes: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })
})

const saved = (body: string) => `cat > x.sh <<'EOF'\n${body}\nEOF`
// `grammar` marks contexts where the grammar gate applies to the body: live text, a saved script the
// same text runs, and shell-fed input. Saved-only data keeps its existing verdict.
const PROVENANCE_CONTEXTS = [
  { name: "top level", wrap: (body: string) => body, grammar: true },
  { name: "persisted", wrap: saved, grammar: false },
  { name: "persisted then sh", wrap: (body: string) => `${saved(body)}\nsh x.sh`, grammar: true },
  { name: "persisted then bash", wrap: (body: string) => `${saved(body)}\nbash x.sh`, grammar: true },
  // Unlike a bare bash heredoc, this launcher admits the safe-body control.
  { name: "script fed", wrap: (body: string) => `bash x.sh <<'EOF'\n${body}\nEOF`, grammar: true },
] as const

const attack = `a='$(gi'; b='t push)'; x="y[$a$b]"; `
const provenanceProbes = [
  "(( a || git push ))",
  "(( (a) || (git push) ))",
  "[[ a || git push || b ]]",
  "(( 0 || git push ))",
  `${attack}(( x ))`,
  `${attack}[[ $x -eq 0 ]]`,
  `${attack}echo $(( x + 1 ))`,
  `${attack}[[ "$x" -eq 0 ]]`,
  `${attack}for ((i=0;i<x;i++)); do :; done`,
  `${attack}echo "$[x]"`,
  "if false; then :; else [[ -n x # it's\n]]\ngit push\n: ' ]]'\nfi",
  `a='$(gi'; b='t push)'; x="$a$b"; [[ -n "${"${x@P}"}" ]]`,
  "(( a || git push )) && echo ok",
  "while (( a || git push )); do break; done",
] as const

describe.each(PROVENANCE_CONTEXTS)("arithmetic provenance at $name", ({ name, wrap, grammar }) => {
  it.each(provenanceProbes)("asks for Git reachable through an expression: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  it.each([
    "(( a && git push ))",
    "[[ a && gh pr merge 1 ]]",
    "time [[ a || git push || b ]]",
    "echo $(( $(git push) + 1 ))",
    "x=$(( $(gh pr merge 1) + 1 ))",
    "read -r n; (( n > 0 ))",
    "read -p prompt -r n; echo $((n))",
    "printf -v n '%s' input; (( n ))",
    "mapfile -t n; (( n[0] ))",
    "readarray n; echo $[n]",
    "declare n='input[0]'; (( n ))",
    "typeset n='input[0]'; [[ $n -ne 0 ]]",
    "local n='input[0]'; [[ n -lt 3 ]]",
    "export n='input[0]'; [[ n -le 3 ]]",
    "for n in 'input[0]'; do [[ n -gt 0 ]]; done",
    `${attack}[[ x -ge 0 ]]`,
    `${attack}[[ -v x ]]`,
    `${attack}echo ${"${a[x]}"}`,
    `${attack}echo ${"${a[$x]}"}`,
    `${attack}echo ${"${x@P}"}`,
    `${attack}echo "${"${x@P}"}"`,
    "read -r index; a[index]=1",
    "read -r index; echo ${a[index]}",
    "read -r index; echo $((a[index]))",
    "read -p prompt; ((REPLY))",
    "mapfile -n 1; ((MAPFILE[0]))",
    "printf -v 'n[0]' '%s' 'input[0]'; ((n[0]))",
    `${attack}echo ${"${#a[x]}"}`,
    `${attack}echo $( (( x )) )`,
    `${attack}echo ${"${a[@]:x:1}"}`,
  ])("asks for executable arithmetic provenance: %s", (body) => {
    expect(decide(classifyCommand(wrap(body)), "standard", GRANTED)?.effect).toBe("ask")
  })

  // Single-quoted text is inert data in the grammar, and the raw-text gate that asked for it
  // now applies only to saved data. Live text is quiet; saved bodies ask.
  it("treats a single-quoted prompt expansion as data only in live text", () => {
    const result = classifyCommand(wrap("echo '${name@P}'"))
    if (name === "top level") expect(decide(result, "standard", GRANTED)).toBeUndefined()
    else expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  })

  it("admits plain output", () => {
    const result = classifyCommand(wrap("echo safe"))
    expect(result.kind).toBe("other")
    expect(decide(result, "standard", GRANTED)).toBeUndefined()
  })

  // Grammar gate: assignments, arithmetic, loops, [[ ]], if and read leave the grammar, so these forms ask
  // wherever it applies and stay quiet only as saved data.
  it.each([
    "n=identifier; ((n))",
    "echo n=unsafe-value; ((n))",
    "n=0; while (( n < 5 )); do n=$((n+1)); done",
    "i=3; echo $(( i * 2 ))",
    "x=5; [[ $x -eq 5 ]] && echo ok",
    "for ((i=0;i<3;i++)); do echo $i; done",
    "(( ${#a[@]} > 0 )) && echo y",
    "(( a[i]++ ))",
    "(( a[0]++ ))",
    "x=$(( $(wc -l < f) + 1 ))",
    "echo $(( $(echo 1) + 1 ))",
    "if true; then :; else (( n++ )); fi",
    "if true; then :; else ! (( n++ )); fi",
    "if true; then :; else [[ -f x ]]; fi",
    "read -r n; echo $n",
    "(( inherited + 1 ))",
  ])("admits safe expression or data only as saved data: %s", (body) => {
    const result = classifyCommand(wrap(body))
    if (grammar) expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
    else {
      expect(result.kind).toBe("other")
      expect(decide(result, "standard", GRANTED)).toBeUndefined()
    }
  })
})

const chains = [
  ["cat > x.sh <<'EOF'\n(( a || git push ))\nEOF\nsh x.sh", "ask"],
  ["cat > x.sh <<'EOF'\n#!/bin/sh\n(( 1 || git push ))\nEOF\nchmod +x x.sh && ./x.sh", "ask"],
  ["cat > x.sh <<'EOF'\ntime [[ a || git push || b ]]\nEOF\ndash x.sh", "ask"],
  ["sh x.sh", "other"],
  ["dash x.sh", "other"],
  [`${saved(`${attack}(( x ))`)}\nbash x.sh`, "ask"],
  [saved("if true; then :; else git is fine; fi"), "ask"],
  [saved("git is origin main"), "other"],
] as const

it.each(chains)("classifies persisted chain %s as %s", (command, expected) => {
  const result = classifyCommand(command)
  if (expected === "ask") expect(decide(result, "standard", GRANTED)?.effect).toBe("ask")
  else expect(result.kind).toBe("other")
})
