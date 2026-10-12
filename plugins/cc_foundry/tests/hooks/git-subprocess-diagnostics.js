// Test-only preload: observe Git subprocess failures without changing hook decisions.
const childProcess = require("child_process");
const execFileSync = childProcess.execFileSync;

childProcess.execFileSync = function (file, args, options) {
  if (file !== "git") return execFileSync(file, args, options);
  const started = Date.now();
  // The approval library deliberately discards Git stderr; retain it for failed CI diagnostics.
  const captured = { ...options };
  if (Array.isArray(options?.stdio) && options.stdio[2] === "ignore") {
    captured.stdio = [...options.stdio];
    captured.stdio[2] = "pipe";
  }
  try {
    return execFileSync(file, args, captured);
  } catch (error) {
    process.stderr.write(
      "Git subprocess failed: " +
        JSON.stringify({
          args,
          cwd: options?.cwd || process.cwd(),
          code: error.code || null,
          status: error.status ?? null,
          signal: error.signal || null,
          timeout_ms: options?.timeout ?? null,
          elapsed_ms: Date.now() - started,
          stderr: error.stderr ? error.stderr.toString("utf8").slice(0, 2048) : "",
        }) +
        "\n",
    );
    throw error;
  }
};
