// Deterministic bypass search for the shell reader: inside an array assignment list every element is a plain word,
// so a balanced element holding a paren that is neither a substitution nor quoted text must leave the command
// unreadable. Three oracles: (1) no readable command keeps a bare paren in its list; (2) a push held in a
// substitution inside an element is still found whenever the command reads; (3) a runtime command word after the
// list never reads, so the array marking does not reach it. Usage: node array_list_fuzz.js <path to shell-git.js>; prints {"cases": N, "suspicious": [...]}.
const lib = require(process.argv[2]);

const ELEMENTS = [
  "(x)",
  "((x))",
  "(.)(e:x:)",
  "x(y)",
  "*(N)",
  "B=(y)",
  "B+=(y)",
  "$(x)",
  "<(x)",
  ">(x)",
  "!(x)",
  "@(x)",
  "?(x)",
  "+(x)",
  "*(x)",
  "(x)(y)",
  "((x))(y)",
  "( (x) )",
  "{ x; }",
  "if x; then y; fi",
  "[[ x ]]",
  "x",
  "'(x)'",
  '"(x)"',
  "`x`",
  "$((1))",
  "(( 1 ))",
  "x=(y)",
  "-a",
  "declare",
  "time",
  "!",
  "{",
  "}",
  "[[",
  "(",
  ")",
  "$f",
  "\n",
  "#c\n",
  "x\\\n(y)",
  "=(x)",
  "> f",
  "2>&1",
  "<<EOF\nx\nEOF\n",
  "<<'EOF'\n(\nEOF\n",
];
// Elements holding a push in a substitution; the reader must report the push whenever it calls the command readable.
const PUSHES = [
  "$(git push)",
  "`git push`",
  "<(git push)",
  '"$(git push)"',
  ">(git push)",
  "=(git push)",
  "${f:-$(git push)}",
  "$(( $(git push) ))",
];
// Ways to open a list: plain, appending, subscripted (the subscript may itself hold a substitution).
const OPENERS = ["A=(", "A+=(", "A[1]=(", "A[$(x)]=("];
const HEADS = [
  "",
  "declare ",
  "declare -a ",
  "local ",
  "export ",
  "$f ",
  "x ",
  "time ",
  "! ",
  "if x; then ",
  "{ ",
  "git status && ",
  "echo $(",
];
const TAILS = ["", "; $f arg", "\n$f arg"];
const CLOSERS = { "echo $(": ")", "if x; then ": "; fi", "{ ": "; }" };

function readable(command) {
  const info = {};
  lib.programInvocations(command, "git", info);
  return !info.unreadable;
}

// Text that may hold a paren without opening a group (a heredoc body is data, never a group).
function withoutSafeParens(text) {
  return text
    .replace(/<<-?[ \t]*(['"]?)(\w+)\1\n[\s\S]*?\n\2\n/g, "")
    .replace(/\$\(\(?[^)]*\)\)?/g, "")
    .replace(/[<>]\([^)]*\)/g, "")
    .replace(/'[^']*'/g, "")
    .replace(/"[^"]*"/g, "")
    .replace(/`[^`]*`/g, "");
}

function count(text, char) {
  return text.split(char).length - 1;
}

let cases = 0;
const suspicious = [];

function pushFound(command) {
  const found = lib.programInvocations(command, "git", {}) || [];
  return found.some((argv) => argv[1] === "push");
}

function visit(head, elements, separator, tail, opener = "A=(") {
  const body = elements.join(separator);
  const command = `git status; ${head}${opener}${body})${CLOSERS[head] || ""}${tail}`;
  cases += 1;
  if (!readable(command)) return;
  // a runtime command word after the list must keep the command unreadable
  if (tail.includes("$f")) {
    suspicious.push(`later command read as data: ${command}`);
    return;
  }
  // a push inside an element must be reported whenever the command reads
  if (PUSHES.some((push) => body.includes(push)) && !pushFound(command)) {
    suspicious.push(`push not found: ${command}`);
    return;
  }
  const stripped = withoutSafeParens(command);
  // an unbalanced command is a shell syntax error, so only balanced ones can run anything
  if (count(stripped, "(") !== count(stripped, ")")) return;
  if (/[()]/.test(withoutSafeParens(body))) suspicious.push(command);
}

for (const head of HEADS) {
  for (const first of ELEMENTS) {
    for (const tail of TAILS) visit(head, [first], "", tail);
    for (const second of ELEMENTS) {
      for (const separator of ["", " "]) visit(head, [first, second], separator, "");
    }
  }
}

for (const opener of OPENERS.slice(1)) {
  for (const first of ELEMENTS) {
    for (const tail of TAILS) visit("", [first], "", tail, opener);
  }
}
for (const head of ["", "declare ", "git status && ", "echo $("]) {
  for (const push of PUSHES) {
    for (const element of ELEMENTS) {
      for (const separator of ["", " "]) {
        visit(head, [element, push], separator, "");
        visit(head, [push, element], separator, "");
      }
    }
  }
}

process.stdout.write(JSON.stringify({ cases, suspicious: suspicious.slice(0, 20) }));
