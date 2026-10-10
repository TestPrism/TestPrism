import {promises as fs} from 'node:fs';
import path from 'node:path';
import lockfile from 'proper-lockfile';
import {setTimeout as delay} from 'node:timers/promises';
import type {Config} from './types.js';
import {hash} from './util.js';

/** Heartbeating third-party filesystem leases share slots across Node processes. */
export async function withAPISlot<T>(config: Config, planning: boolean, signal: AbortSignal, work: (signal: AbortSignal) => Promise<T>): Promise<T> {
  let dispatched = false;
  try {
    return await scheduled(config, planning, signal, async signal => {dispatched = true; return work(signal);});
  } catch (error) {
    if (!dispatched) throw Object.assign(new Error(error instanceof Error ? error.message : String(error)), {requestSent: false});
    throw error;
  }
}
async function scheduled<T>(config: Config, planning: boolean, signal: AbortSignal, work: (signal: AbortSignal) => Promise<T>): Promise<T> {
  signal.throwIfAborted();
  if (!config.scheduler.enabled) return work(signal);
  const lease = new AbortController();
  signal = AbortSignal.any([signal, lease.signal]);
  const onCompromised = (error: Error) => lease.abort(error);
  const directory = path.resolve(config.scheduler.directory,
    hash({provider: config.model.provider, endpoint: new URL(config.model.baseURL).href.replace(/\/+$/, '')}).slice(0, 20));
  await fs.mkdir(directory, {recursive: true, mode: 0o700});
  // A conflicting process must not silently create additional slots.
  const capacity = JSON.stringify({api: config.scheduler.apiConcurrency, planning: config.scheduler.planningConcurrency});
  const capacityFile = path.join(directory, 'capacity.json');
  let capacityRelease: (() => Promise<void>) | undefined;
  while (!capacityRelease) {
    signal.throwIfAborted();
    try {capacityRelease = await lockfile.lock(directory, {realpath: true, retries: 0, stale: 60000, update: 10000, onCompromised});}
    catch (error) {if ((error as NodeJS.ErrnoException).code !== 'ELOCKED') throw error; await delay(40, undefined, {signal});}
  }
  try {
    try {await fs.writeFile(capacityFile, capacity, {flag: 'wx', mode: 0o600});}
    catch (error) {if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;}
    if (await fs.readFile(capacityFile, 'utf8') !== capacity)
      throw Object.assign(new Error('Shared API scheduler capacity differs; use one capacity per scheduler directory'), {requestSent: false});
  } finally {await capacityRelease();}
  const releases: Array<() => Promise<void>> = [];
  const acquire = async (group: string, count: number) => {
    for (;;) {
      signal.throwIfAborted();
      for (let index = 0; index < count; index++) {
        try {
          const release = await lockfile.lock(path.join(directory, `${group}-${index}`),
            {realpath: false, retries: 0, stale: 60000, update: 10000, onCompromised});
          releases.push(release); return;
        } catch (error) {if ((error as NodeJS.ErrnoException).code !== 'ELOCKED') throw error;}
      }
      await delay(75, undefined, {signal});
    }
  };
  try {
    if (planning) await acquire('planning', config.scheduler.planningConcurrency);
    await acquire('api', config.scheduler.apiConcurrency);
    signal.throwIfAborted();
    return await work(signal);
  } finally {
    const results = await Promise.allSettled(releases.reverse().map(release => release()));
    const failed = results.find(result => result.status === 'rejected');
    if (failed?.status === 'rejected') throw failed.reason;
  }
}
