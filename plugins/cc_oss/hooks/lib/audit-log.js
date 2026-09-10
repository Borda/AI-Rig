// audit-log.js — shared append-only audit record writer (library, not a hook)
//
// PURPOSE
//   Persist one JSON record per line describing what a plugin's PreToolUse
//   decision hook decided, and what its PostToolUse closer later observed.
//   Every writer in every plugin goes through this module so the on-disk
//   record format has exactly one definition.
//
// HOW IT WORKS
//   1. Records go to `~/.claude/logs/audit/s-<key>.jsonl`, one file per
//      session, where <key> is the first 32 hex characters of the SHA-256 of
//      the session id. Records with no usable session id go to the shared
//      `_no-session.jsonl` stream instead.
//   2. A record is sealed with `record_hash`, the SHA-256 of its own canonical
//      serialization with `record_hash` removed. That detects a mutated line.
//      It is NOT tamper-evidence: the same user owns the file and the checker.
//   3. Writing is a single `appendFileSync`. Nothing else touches the
//      filesystem.
//
// APPEND-ONLY — THE INVARIANT THIS MODULE EXISTS TO KEEP
//   Nothing here locks, claims, creates a marker or tombstone, stats-then-acts
//   on another process's file, unlinks, renames, replaces, truncates or sweeps.
//   Several processes append the same file concurrently and coordinate through
//   nothing at all; the reader reconciles. A function added here that removes
//   or rewrites a file is a design regression, not a feature — deletion lives
//   solely in the explicitly invoked `verify_blueprint_audit.py prune`.
//
// NEVER THROWS
//   Every exported function is total. Callers are hooks whose decision must not
//   depend on whether logging worked, so a failure returns a value describing
//   itself and is never raised. `appendRecord` returns the record with
//   `_unwritten: true` and a `reason`.
//
// PRIVACY
//   Raw command text never enters a record. `redact` returns a digest and has
//   no code path that returns text. Note the limit honestly: `task-log.js`
//   already writes the first 200 characters of every Bash command into
//   `timings.jsonl` under the same `tool_use_id`, so this is a guarantee about
//   THIS file, not about the log directory.
//
// CANONICAL FORM — normative, because a Python reader must agree byte-for-byte
//   * UTF-8, no whitespace, keys sorted, non-ASCII left unescaped.
//     JS `JSON.stringify` over a key-sorted object equals Python
//     `json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
//   * Every object key is a fixed ASCII identifier. JS sorts UTF-16 code units
//     and Python sorts code points; those differ above U+FFFF, and keeping
//     every key far below that boundary is what makes the two orders identical.
//     Identifier shape is load-bearing for a second reason: a key that is a
//     decimal integer is an array index in JS, which orders such keys first and
//     numerically no matter what order they were inserted in, while Python
//     `sort_keys` orders them lexicographically. `{"9":1,"10":2}` therefore
//     serializes as `{"9":1,"10":2}` in JS and `{"10":2,"9":1}` in Python —
//     two different byte strings, two different hashes, and a Python reader
//     that would report every such record as corrupt. Requiring a leading
//     letter or underscore removes the whole class.
//   * Every string value must be well-formed Unicode. A lone surrogate makes
//     `JSON.stringify` emit an escape Python cannot UTF-8 encode, so an
//     ill-formed free-form value is replaced with null. The null IS the signal;
//     no extra field records that it happened.
//   * Every number is a safe integer. No floats, so no float-formatting
//     divergence is possible.
//   * `null` is written explicitly; an absent optional key stays absent.
//
// ENVIRONMENT
//   RIG_AUDIT=0                        disable all audit writes (default on).
//   RIG_AUDIT_NOSESSION_MAX_BYTES=<n>  soft cutoff for `_no-session.jsonl`
//                                      (default 67108864). Soft: concurrent
//                                      writers can overshoot by one record
//                                      each; the file is never truncated.

"use strict";

const crypto = require("crypto");
const fs = require("fs");
const os = require("os");
const path = require("path");

/** Directory holding the per-session audit logs, relative to the user's home. */
const LOG_SUBPATH = [".claude", "logs", "audit"];

/** Filename for records whose session id is missing or unusable. */
const NO_SESSION_FILE = "_no-session.jsonl";

/** Default soft cutoff for `_no-session.jsonl`, in bytes. */
const DEFAULT_NOSESSION_MAX_BYTES = 64 * 1024 * 1024;

/** Length of the hex filesystem key: 32 characters, i.e. 128 bits of SHA-256. */
const FS_KEY_HEX_LENGTH = 32;

/** Free-form values that come from outside this process and must be validated before they are serialized. */
const FREE_FORM_FIELDS = ["session_id", "tool_use_id", "project"];

/**
 * The only key shape the canonical form admits: an ASCII identifier.
 * A leading letter or underscore is what keeps JS and Python key order identical — see CANONICAL FORM above for the
 * integer-index divergence this excludes. Every key this system writes is a fixed literal that already matches.
 */
const CANONICAL_KEY = /^[A-Za-z_][A-Za-z0-9_]*$/;

/**
 * Keys that mean something to the JavaScript object model and nothing to a JSON reader.
 * `__proto__` is the dangerous one: assigning it into an ordinary object sets the prototype rather than creating an
 * own property, so `JSON.stringify` would omit it while a Python reader kept it — the same record, two hashes.
 */
const RESERVED_KEYS = new Set(["__proto__", "constructor", "prototype"]);

/** Whether audit writing is enabled for this process. */
function isEnabled() {
  return process.env.RIG_AUDIT !== "0";
}

/**
 * Return true when `value` contains no unpaired surrogate.
 * Node 20+ has String.prototype.isWellFormed; the regex is the fallback for anything older.
 */
function isWellFormed(value) {
  if (typeof value.isWellFormed === "function") return value.isWellFormed();
  return !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(^|[^\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(value);
}

/**
 * Return `value` when it is a usable identifier, else null.
 * Usable means: a string, non-empty, and well-formed Unicode. Ill-formed input is rejected BEFORE hashing, because
 * UTF-8 encoding of ill-formed UTF-16 is not injective — distinct ids would otherwise share one filesystem key.
 */
function usableId(value) {
  if (typeof value !== "string" || value.length === 0) return null;
  return isWellFormed(value) ? value : null;
}

/** Return the SHA-256 hex digest of `text` encoded as UTF-8. */
function sha256Hex(text) {
  return crypto.createHash("sha256").update(text, "utf8").digest("hex");
}

/**
 * Return the filesystem key for `id`: the first 32 hex characters of its SHA-256, or null when `id` is unusable.
 * Hashing rather than sanitising is deliberate — a `[^a-zA-Z0-9_-] -> _` transform maps `a/b` and `a?b` onto one
 * name and would merge two sessions into a single file. Hashing also removes path traversal, Windows reserved
 * names, Unicode normalisation and case-insensitive aliasing in one step.
 */
function fsKey(id) {
  const usable = usableId(id);
  return usable === null ? null : sha256Hex(usable).slice(0, FS_KEY_HEX_LENGTH);
}

/** Cached result of the one-time log-directory sanity check: null = not yet run, string = refusal reason, "" = ok. */
let _dirCheck = null;

/** Cached plugin identity, read at most once per process. */
let _pluginInfo = null;

/**
 * Return the audit log directory, creating it 0700, or null when it must not be used.
 * `mkdirSync` silently succeeds on an existing directory without changing its mode, so the directory is inspected
 * once: a symlink, another user's directory, or one writable by group or other means this process degrades to
 * no-audit rather than writing somewhere it cannot vouch for. It lives under $HOME rather than a shared directory,
 * so this is a sanity check, not a defence against a co-tenant.
 */
function logDir() {
  const dir = path.join(os.homedir(), ...LOG_SUBPATH);
  if (_dirCheck === null) _dirCheck = inspectLogDir(dir);
  return _dirCheck === "" ? dir : null;
}

/** Create and inspect the log directory once; return "" when usable, else a short refusal reason. */
function inspectLogDir(dir) {
  try {
    fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  } catch (_error) {
    return "mkdir-failed";
  }
  let stat;
  try {
    stat = fs.lstatSync(dir);
  } catch (_error) {
    return "stat-failed";
  }
  if (stat.isSymbolicLink()) return "log-dir-symlink";
  if (!stat.isDirectory()) return "log-dir-not-a-directory";
  // POSIX ownership and mode bits do not exist on native Windows, where os.homedir() resolves USERPROFILE and the
  // directory is already user-scoped. Skipping the check there is the production fallback, not a test artefact.
  if (typeof process.getuid !== "function") return "";
  if (stat.uid !== process.getuid()) return "log-dir-foreign-owner";
  if ((stat.mode & 0o022) !== 0) return "log-dir-world-writable";
  return "";
}

/**
 * Return the absolute path of the log file for `sessionId`, or null when audit is unavailable.
 * A usable session id yields `s-<key>.jsonl`; anything else yields the shared `_no-session.jsonl` stream. The `s-`
 * prefix keeps real keys disjoint from that name. Every rule phrased as "one file is one session" excludes it.
 */
function logPath(sessionId) {
  const dir = logDir();
  if (dir === null) return null;
  const key = fsKey(sessionId);
  return path.join(dir, key === null ? NO_SESSION_FILE : `s-${key}.jsonl`);
}

/** Return the configured soft cutoff for `_no-session.jsonl`, falling back to the default on anything unparsable. */
function noSessionMaxBytes() {
  const raw = Number.parseInt(process.env.RIG_AUDIT_NOSESSION_MAX_BYTES ?? "", 10);
  return Number.isSafeInteger(raw) && raw >= 0 ? raw : DEFAULT_NOSESSION_MAX_BYTES;
}

/**
 * Return a deep copy of `value` with every object's keys in sorted order.
 * Arrays keep their order — `verdicts` is rank-ordered and sorting it would destroy meaning.
 */
function sortDeep(value) {
  if (Array.isArray(value)) return value.map(sortDeep);
  if (value === null || typeof value !== "object") return value;
  // A null-prototype accumulator, because assigning `__proto__` into an ordinary object sets the prototype instead of
  // creating an own property — the key would vanish from the output while Python kept it, and the two hashes would
  // disagree. `assertCanonicalizable` rejects that key outright; this makes the loss impossible rather than merely
  // unreachable.
  const sorted = Object.create(null);
  for (const key of Object.keys(value).sort()) sorted[key] = sortDeep(value[key]);
  return sorted;
}

/**
 * Return the canonical serialization of `record` with `record_hash` omitted.
 * Throws on a value the canonical form forbids; `appendRecord` is what turns that into a returned failure.
 */
function canonicalize(record) {
  // Null-prototype, for the same reason `sortDeep` uses one: copying a parsed `__proto__` key into an ordinary object
  // sets the prototype and drops the key, which would happen HERE, before the guard below ever sees it.
  const body = Object.create(null);
  for (const key of Object.keys(record)) {
    if (key !== "record_hash") body[key] = record[key];
  }
  assertCanonicalizable(body);
  return JSON.stringify(sortDeep(body));
}

/** Walk `value` and reject anything the canonical form does not admit: non-ASCII keys, floats, ill-formed strings. */
function assertCanonicalizable(value) {
  if (typeof value === "number") {
    if (!Number.isSafeInteger(value)) throw new Error("non-integer numeric value");
    return;
  }
  if (typeof value === "string") {
    if (!isWellFormed(value)) throw new Error("ill-formed string value");
    return;
  }
  if (Array.isArray(value)) {
    value.forEach(assertCanonicalizable);
    return;
  }
  if (value === null || typeof value !== "object") return;
  for (const key of Object.keys(value)) {
    if (!CANONICAL_KEY.test(key) || RESERVED_KEYS.has(key)) {
      throw new Error(`non-canonical record key: ${JSON.stringify(key)}`);
    }
    assertCanonicalizable(value[key]);
  }
}

/** Return the SHA-256 hex of the record's canonical bytes. */
function recordHash(record) {
  return sha256Hex(canonicalize(record));
}

/**
 * Return `{ digest }` for a piece of command text — never the text itself.
 * The caller passes text already normalized by the module that owns normalization, so exactly one definition of the
 * digest exists. This library deliberately does not normalize: the normalizer is part of the permission decision and
 * must not acquire a second implementation here.
 */
function redact(normalizedCommandText) {
  return { digest: sha256Hex(typeof normalizedCommandText === "string" ? normalizedCommandText : "") };
}

/**
 * Return `{ name, version }` for the plugin this copy of the library ships in, cached for the process.
 * The installed cache lays out as `<plugin>/<version>/<relative path>`, so the plugin root's basename is a version
 * number rather than a name; identity comes from `.claude-plugin/plugin.json` instead.
 */
function pluginInfo() {
  if (_pluginInfo !== null) return _pluginInfo;
  _pluginInfo = { name: null, version: null };
  const roots = [path.join(__dirname, "..", "..")];
  if (process.env.CLAUDE_PLUGIN_ROOT) roots.push(process.env.CLAUDE_PLUGIN_ROOT);
  for (const root of roots) {
    try {
      const parsed = JSON.parse(fs.readFileSync(path.join(root, ".claude-plugin", "plugin.json"), "utf8"));
      if (typeof parsed.name === "string" && parsed.name.length > 0) {
        _pluginInfo = {
          name: parsed.name,
          version: typeof parsed.version === "string" && parsed.version.length > 0 ? parsed.version : null,
        };
        return _pluginInfo;
      }
    } catch (_error) {
      // Try the next candidate root; an unreadable manifest is reported as unknown identity, never as a failure.
    }
  }
  return _pluginInfo;
}

/**
 * Return the logical plugin name — `cc_` plus the short name in `plugin.json`, or `unknown`.
 * Injective across the four plugins and identical in the source tree and the installed cache.
 */
function logicalPlugin() {
  const { name } = pluginInfo();
  return name === null ? "unknown" : `cc_${name}`;
}

/** Return the plugin's semantic version, or null when `plugin.json` is unreadable. */
function pluginVersion() {
  return pluginInfo().version;
}

/**
 * Append one record and return it as written. Never throws.
 * Fills `record_id`, `timestamp` and `record_hash` when absent, replaces an ill-formed free-form value with null,
 * and writes a single line. On any failure the returned object carries `_unwritten: true` and a `reason`; `_unwritten`
 * is a return-value marker only and is never serialized.
 */
function appendRecord(record) {
  if (!isEnabled()) return { ...record, _unwritten: true, reason: "disabled" };
  let sealed;
  try {
    sealed = seal(record);
  } catch (error) {
    return { ...record, _unwritten: true, reason: `unserializable: ${error && error.message}` };
  }
  const target = logPath(sealed.session_id);
  if (target === null) return { ...sealed, _unwritten: true, reason: _dirCheck || "log-dir-unavailable" };
  if (path.basename(target) === NO_SESSION_FILE && atNoSessionCutoff(target)) {
    return { ...sealed, _unwritten: true, reason: "no-session-cutoff" };
  }
  try {
    fs.appendFileSync(target, `${JSON.stringify(sealed)}\n`, { mode: 0o600 });
  } catch (error) {
    return { ...sealed, _unwritten: true, reason: `append-failed: ${error && error.message}` };
  }
  return sealed;
}

/** Return true when `_no-session.jsonl` has already reached its soft cutoff. A missing file is never at the cutoff. */
function atNoSessionCutoff(target) {
  try {
    return fs.statSync(target).size >= noSessionMaxBytes();
  } catch (_error) {
    return false;
  }
}

/** Return `record` with identity fields filled and `record_hash` computed over the canonical form. */
function seal(record) {
  const filled = { ...record };
  for (const field of FREE_FORM_FIELDS) {
    if (field in filled) filled[field] = usableId(filled[field]);
  }
  if (filled.action_detail && typeof filled.action_detail.reason === "string") {
    const reason = usableId(filled.action_detail.reason);
    filled.action_detail = { ...filled.action_detail };
    if (reason === null) delete filled.action_detail.reason;
    else filled.action_detail.reason = reason;
  }
  if (typeof filled.record_id !== "string") filled.record_id = crypto.randomUUID();
  if (typeof filled.timestamp !== "string") filled.timestamp = new Date().toISOString();
  delete filled.record_hash;
  filled.record_hash = recordHash(filled);
  return filled;
}

module.exports = {
  isEnabled,
  usableId,
  fsKey,
  logPath,
  canonicalize,
  sha256Hex,
  recordHash,
  appendRecord,
  pluginVersion,
  logicalPlugin,
  redact,
};
