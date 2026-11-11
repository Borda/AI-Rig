"use strict";
const fs = require("fs");
const os = require("os");
const vm = require("vm");
const path = require("path");
const child = require("child_process");
const [hook, root, trackedJson, failGit, payloadJson] = process.argv.slice(2);
const handlers = {};
const sentinel = path.join(root, "sentinel");
fs.mkdirSync(sentinel, { recursive: true });
const sandbox = {
  require(name) {
    if (name === "os") return { ...os, tmpdir: () => sentinel };
    if (name === "child_process")
      return {
        execSync: child.execSync,
        execFileSync(command, args, options) {
          if (command !== "git" || args[0] !== "ls-files") throw new Error("Git unavailable");
          if (failGit === "real") return child.execFileSync(command, args, options);
          if (failGit !== "no") {
            const err = new Error("Git unavailable");
            if (failGit === "timeout") Object.assign(err, { status: 128, code: "ETIMEDOUT" });
            if (failGit === "overflow") Object.assign(err, { status: 128, code: "ENOBUFS" });
            throw err;
          }
          if (
            JSON.stringify(args.slice(3)) !==
            JSON.stringify(["--", ".reports", ".experiments", ".developments", ".plans/blueprint", ".cache", ".temp"])
          )
            throw new Error("Git inventory must be scoped to artifact roots");
          return JSON.parse(trackedJson).join("\0") + "\0";
        },
      };
    return require(name);
  },
  Buffer,
  process: {
    platform: "win32",
    pid: process.pid,
    stderr: process.stderr,
    cwd: () => root,
    exit() {},
    stdin: {
      setEncoding() {},
      on(name, fn) {
        handlers[name] = fn;
      },
    },
  },
};
vm.runInNewContext(fs.readFileSync(hook, "utf8"), sandbox, { filename: hook });
handlers.data(payloadJson || JSON.stringify({ hook_event_name: "SessionEnd", session_id: "cleanup-test" }));
handlers.end();
