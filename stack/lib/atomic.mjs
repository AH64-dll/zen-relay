/**
 * Crash-safe file replacement.
 *
 * Status and lock documents must never be observed half-written by a reader
 * (the plugin, a parallel `dsh-stack status`, or the next boot after a power
 * loss), so every write goes to a sibling temp file, is fsynced, renamed over
 * the target, and the directory entry is fsynced.
 *
 * @module dsh-stack/atomic
 */

import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';

/**
 * fsync a directory so a rename survives power loss.
 * @param {string} dir
 */
export function fsyncDirSync(dir) {
  let fd;
  try {
    fd = fs.openSync(dir, 'r');
    fs.fsyncSync(fd);
  } catch {
    /* not supported everywhere; the rename is still atomic */
  } finally {
    if (fd !== undefined) {
      try {
        fs.closeSync(fd);
      } catch {
        /* ignore */
      }
    }
  }
}

/**
 * @param {string} dir
 */
export async function fsyncDir(dir) {
  let handle;
  try {
    handle = await fsp.open(dir, 'r');
    await handle.sync();
  } catch {
    /* best effort */
  } finally {
    await handle?.close().catch(() => {});
  }
}

/**
 * Replace `file` with `data` atomically and durably.
 * @param {string} file
 * @param {string} data
 * @param {{mode?: number}} [options]
 */
export async function writeFileAtomic(file, data, options = {}) {
  const dir = path.dirname(file);
  await fsp.mkdir(dir, { recursive: true, mode: 0o700 });
  const temp = `${file}.${process.pid}.tmp`;
  const handle = await fsp.open(temp, 'w', options.mode ?? 0o600);
  try {
    await handle.writeFile(data);
    await handle.sync();
  } finally {
    await handle.close();
  }
  await fsp.rename(temp, file);
  await fsyncDir(dir);
}

/**
 * Serialize and atomically write JSON.
 * @param {string} file
 * @param {unknown} value
 * @param {{mode?: number}} [options]
 */
export async function writeJsonAtomic(file, value, options = {}) {
  await writeFileAtomic(file, `${JSON.stringify(value, null, 2)}\n`, options);
}

/**
 * Same as {@link writeJsonAtomic} but synchronous, for signal handlers.
 * @param {string} file
 * @param {unknown} value
 */
export function writeJsonAtomicSync(file, value) {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true, mode: 0o700 });
  const temp = `${file}.${process.pid}.tmp`;
  const fd = fs.openSync(temp, 'w', 0o600);
  try {
    fs.writeSync(fd, `${JSON.stringify(value, null, 2)}\n`);
    fs.fsyncSync(fd);
  } finally {
    fs.closeSync(fd);
  }
  fs.renameSync(temp, file);
  fsyncDirSync(dir);
}
