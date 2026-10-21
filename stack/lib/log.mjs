/**
 * Structured, rotated, durable-enough logging for the supervisor.
 *
 * Every line is one JSON object so `jq` and log shippers work without a parser.
 * The file is opened per append (the process is long-lived but the volume is
 * tiny), and warnings are mirrored to stderr, which systemd routes to journald.
 *
 * @module dsh-stack/log
 */

import fs from 'node:fs';
import path from 'node:path';

/**
 * @param {{file: string, maxBytes?: number, keep?: number, echoWarnings?: boolean, echo?: NodeJS.WritableStream, component?: string}} options
 */
export function createLogger(options) {
  const file = options.file;
  const maxBytes = options.maxBytes ?? 8 * 1024 * 1024;
  const keep = options.keep ?? 5;
  const echoWarnings = options.echoWarnings !== false;
  const echo = options.echo ?? process.stderr;
  let appends = 0;
  let lastRotationCheck = 0;
  let broken = false;

  try {
    fs.mkdirSync(path.dirname(file), { recursive: true, mode: 0o700 });
  } catch {
    broken = true;
  }

  /** Rotate `stack.log` → `stack.log.1` … when it grows past the bound. */
  function rotateIfNeeded() {
    const now = Date.now();
    if (now - lastRotationCheck < 5_000) return;
    lastRotationCheck = now;
    let size;
    try {
      size = fs.statSync(file).size;
    } catch {
      return;
    }
    if (size < maxBytes) return;
    try {
      for (let index = keep - 1; index >= 1; index -= 1) {
        const from = `${file}.${index}`;
        const to = `${file}.${index + 1}`;
        if (fs.existsSync(from)) fs.renameSync(from, to);
      }
      fs.renameSync(file, `${file}.1`);
    } catch {
      /* rotation is best effort; an oversized log never stops the supervisor */
    }
  }

  /**
   * @param {'debug'|'info'|'warn'|'error'} level
   * @param {string} event
   * @param {Record<string, unknown>} [fields]
   */
  function write(level, event, fields) {
    const record = { ts: new Date().toISOString(), level, event, pid: process.pid, ...fields };
    const line = `${JSON.stringify(record)}\n`;
    if (level === 'warn' || level === 'error') {
      if (echoWarnings) {
        try {
          echo.write(`dsh-stack ${level}: ${event}${renderFields(fields)}\n`);
        } catch {
          /* stderr closed: keep running */
        }
      }
    }
    if (broken) return;
    appends += 1;
    if (appends % 50 === 0) rotateIfNeeded();
    try {
      const fd = fs.openSync(file, 'a', 0o600);
      try {
        fs.writeSync(fd, line);
      } finally {
        fs.closeSync(fd);
      }
    } catch {
      broken = true;
    }
  }

  return {
    file,
    debug: (event, fields) => write('debug', event, fields),
    info: (event, fields) => write('info', event, fields),
    warn: (event, fields) => write('warn', event, fields),
    error: (event, fields) => write('error', event, fields),
    /** A logger whose records carry a component tag. */
    child: (component) => ({
      file,
      debug: (event, fields) => write('debug', event, { component, ...fields }),
      info: (event, fields) => write('info', event, { component, ...fields }),
      warn: (event, fields) => write('warn', event, { component, ...fields }),
      error: (event, fields) => write('error', event, { component, ...fields }),
      child: (sub) => write('info', `${sub}/mounted`, { component }),
    }),
  };
}

/** Render a few scalar fields for the stderr mirror. */
function renderFields(fields) {
  if (fields === undefined) return '';
  const parts = [];
  for (const [key, value] of Object.entries(fields)) {
    if (value === undefined || value === null) continue;
    if (typeof value === 'object') continue;
    parts.push(`${key}=${String(value)}`);
    if (parts.length >= 6) break;
  }
  return parts.length === 0 ? '' : ` (${parts.join(' ')})`;
}
