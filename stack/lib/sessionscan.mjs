/**
 * Offline read of the Harness session store.
 *
 * This is the supervisor's independent view: it decodes `session.v*.jsonl.zstd`
 * directly and applies the *same* analysis the Host plugin uses, so
 * `dsh-stack sessions` can show what recovery will act on, and the post-boot
 * verification pass can report how many logs still end inside an open turn.
 *
 * It is intentionally advisory:
 * - a session that is **live in a running Harness** also ends inside an open
 *   turn, and only the plugin (inside that process, or via the persistence write
 *   lease) can tell the difference;
 * - a log being appended while this scan runs may be observed mid-batch.
 * Nothing here mutates storage; the plugin owns recovery.
 *
 * @module dsh-stack/sessionscan
 */

import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';

import { analyze, isEligibleHeader } from '../plugins/dsh-session-keeper/lib/events.mjs';

/** Supported on-disk generations, newest first. */
const LOG_NAMES = [
  'session.v4.jsonl.zstd',
  'session.v3.jsonl.zstd',
  'session.v2.jsonl.zstd',
  'session.v1.jsonl.zstd',
  'session.jsonl.zstd',
];

/**
 * @typedef {object} StoredSession
 * @property {string} sessionId
 * @property {string} project
 * @property {string} file
 * @property {number} sizeBytes
 * @property {number} mtime
 * @property {Record<string, any>} header
 * @property {import('../plugins/dsh-session-keeper/lib/events.mjs').LogAnalysis} analysis
 * @property {boolean} eligible - not a subagent child, so the keeper may resume it
 */

/**
 * Scan `<dshHome>/sessions` for stored sessions.
 * @param {{dshHome: string, limit?: number, maxBytes?: number, onProgress?: (done: number, total: number) => void}} options
 * @returns {{scanned: number, interrupted: StoredSession[], resumable: StoredSession[], children: number, balanced: number, errors: Array<{file: string, error: string}>, skipped: number}}
 *   `interrupted` is every stored session whose last turn never closed — which
 *   includes sessions live in a running Harness and subagent children.
 *   `resumable` is the subset the keeper would actually consider.
 */
export function scanInterrupted(options) {
  const root = path.join(options.dshHome, 'sessions');
  const limit = options.limit ?? 500;
  const maxBytes = options.maxBytes ?? 32 * 1024 * 1024;
  const result = { scanned: 0, interrupted: [], resumable: [], children: 0, balanced: 0, errors: [], skipped: 0 };

  let projects;
  try {
    projects = fs.readdirSync(root, { withFileTypes: true }).filter((entry) => entry.isDirectory());
  } catch (error) {
    if (/** @type {any} */ (error)?.code === 'ENOENT') return result;
    throw error;
  }

  /** @type {Map<string, StoredSession>} */
  const newest = new Map();
  for (const project of projects) {
    const projectDir = path.join(root, project.name);
    let sessions;
    try {
      sessions = fs.readdirSync(projectDir, { withFileTypes: true }).filter((entry) => entry.isDirectory());
    } catch {
      continue;
    }
    for (const session of sessions) {
      const dir = path.join(projectDir, session.name);
      const file = pickLog(dir);
      if (file === undefined) continue;
      let stat;
      try {
        stat = fs.statSync(file);
      } catch {
        continue;
      }
      const previous = newest.get(session.name);
      // A session id is normally unique; if a copy exists, prefer the newest artifact.
      if (previous !== undefined && previous.mtime >= stat.mtimeMs) continue;
      newest.set(session.name, {
        sessionId: session.name,
        project: project.name,
        file,
        sizeBytes: stat.size,
        mtime: stat.mtimeMs,
        header: {},
        analysis: /** @type {any} */ ({}),
      });
    }
  }

  for (const record of newest.values()) {
    if (result.scanned >= limit) {
      result.skipped += 1;
      continue;
    }
    result.scanned += 1;
    options.onProgress?.(result.scanned, newest.size);
    if (record.sizeBytes > maxBytes) {
      result.errors.push({ file: record.file, error: `log larger than ${maxBytes} bytes` });
      continue;
    }
    try {
      const buffer = fs.readFileSync(record.file);
      const text = zlib.zstdDecompressSync(buffer).toString('utf8');
      const lines = text.split('\n').filter((line) => line.length > 0);
      if (lines.length === 0) continue;
      const header = safeJson(lines[0]);
      if (header === null || header.type !== 'session') {
        result.errors.push({ file: record.file, error: 'missing session header' });
        continue;
      }
      const events = [];
      for (let index = 1; index < lines.length; index += 1) {
        const event = safeJson(lines[index]);
        if (event !== null) events.push(event);
      }
      record.header = header;
      record.analysis = analyze(events, { atLogStart: true });
      if (record.analysis.open) {
        const eligibility = isEligibleHeader(header, {});
        record.eligible = eligibility.eligible;
        result.interrupted.push(record);
        if (record.eligible) result.resumable.push(record);
        else result.children += 1;
      } else {
        result.balanced += 1;
      }
    } catch (error) {
      result.errors.push({ file: record.file, error: /** @type {Error} */ (error).message });
    }
  }

  result.interrupted.sort((a, b) => b.mtime - a.mtime);
  result.resumable.sort((a, b) => b.mtime - a.mtime);
  return result;
}

/**
 * Every stored session's metadata, newest first, without analysis.
 * @param {{dshHome: string, limit?: number}} options
 * @returns {Array<{sessionId: string, project: string, file: string, cwd: string|undefined, origin: string|undefined, sizeBytes: number, mtime: number}>}
 */
export function listStoredSessions(options) {
  const root = path.join(options.dshHome, 'sessions');
  const limit = options.limit ?? 200;
  /** @type {any[]} */
  const out = [];
  let projects;
  try {
    projects = fs.readdirSync(root, { withFileTypes: true }).filter((entry) => entry.isDirectory());
  } catch {
    return out;
  }
  for (const project of projects) {
    const projectDir = path.join(root, project.name);
    let sessions;
    try {
      sessions = fs.readdirSync(projectDir, { withFileTypes: true }).filter((entry) => entry.isDirectory());
    } catch {
      continue;
    }
    for (const session of sessions) {
      const file = pickLog(path.join(projectDir, session.name));
      if (file === undefined) continue;
      let stat;
      try {
        stat = fs.statSync(file);
      } catch {
        continue;
      }
      let header = {};
      try {
        const text = zlib.zstdDecompressSync(fs.readFileSync(file)).toString('utf8');
        header = safeJson(text.slice(0, text.indexOf('\n') > 0 ? text.indexOf('\n') : text.length)) ?? {};
      } catch {
        header = {};
      }
      out.push({
        sessionId: session.name,
        project: project.name,
        file,
        cwd: header.cwd,
        origin: header.origin,
        sizeBytes: stat.size,
        mtime: stat.mtimeMs,
      });
    }
  }
  out.sort((a, b) => b.mtime - a.mtime);
  return out.slice(0, limit);
}

/**
 * Pick the newest supported log generation inside one session directory.
 * @param {string} dir
 * @returns {string|undefined}
 */
function pickLog(dir) {
  for (const name of LOG_NAMES) {
    const candidate = path.join(dir, name);
    if (fs.existsSync(candidate)) return candidate;
  }
  return undefined;
}

/** @param {string} line */
function safeJson(line) {
  try {
    return JSON.parse(line);
  } catch {
    return null;
  }
}

export { isEligibleHeader };
