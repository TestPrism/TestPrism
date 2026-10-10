import { promises as fs } from 'node:fs';
import path from 'node:path';
import lockfile from 'proper-lockfile';

/** One writer per run/campaign/evolution directory, with heartbeat-based crash recovery. */
export async function withRunLock<T>(directory: string, work: () => Promise<T>): Promise<T> {
  const target = path.resolve(directory); await fs.mkdir(target, {recursive: true});
  let release: () => Promise<void>;
  try { release = await lockfile.lock(target, {realpath: true, retries: 0, stale: 60000, update: 10000}); }
  catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ELOCKED')
      throw new Error(`Run directory is locked by another writer: ${target}. A crashed writer's lease expires after 60 seconds.`);
    throw error;
  }
  try { return await work(); } finally { await release(); }
}
