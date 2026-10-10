import { promises as fs } from 'node:fs';
import path from 'node:path';
import { atomicJSON, hash, redact } from './util.js';

export class Store {
  private artifacts = new Map<string, Promise<void>>();
  constructor(readonly root: string, private readonly artifactScope?: string) {
    if (artifactScope !== undefined && !/^[A-Za-z0-9_-]+$/.test(artifactScope)) throw new Error('Invalid artifact scope');
  }
  get scope(): string | undefined { return this.artifactScope; }
  /** Share host telemetry/checkpoints, but never expose another session's archives. */
  forSession(session: string): Store { return new Store(this.root, session); }
  private artifactPath(digest: string): string {
    return path.join(this.root, 'artifacts', ...(this.artifactScope === undefined ? [] : [this.artifactScope]), `${digest}.txt`);
  }
  async init(): Promise<void> { await fs.mkdir(this.root, {recursive: true}); }
  async save(name: string, value: unknown): Promise<void> { await atomicJSON(path.join(this.root, name), value); }
  async artifact(value: string): Promise<string> {
    const digest = hash(value), file = this.artifactPath(digest);
    let pending = this.artifacts.get(digest);
    if (!pending) {
      pending = (async () => {
        await fs.mkdir(path.dirname(file), {recursive: true});
        await fs.writeFile(file, value, {mode: 0o600});
      })();
      this.artifacts.set(digest, pending);
      if (this.artifacts.size > 2048) this.artifacts.delete(this.artifacts.keys().next().value!);
      pending.catch(() => this.artifacts.delete(digest));
    }
    await pending;
    return digest;
  }
  async readArtifact(digest: string): Promise<string> {
    if (!/^[a-f0-9]{64}$/.test(digest)) throw new Error('Invalid artifact identifier');
    return fs.readFile(this.artifactPath(digest), 'utf8');
  }
  async event(type: string, data: unknown): Promise<void> {
    await fs.appendFile(path.join(this.root, 'events.jsonl'), JSON.stringify({time: new Date().toISOString(), type, data: redact(data)}) + '\n', {mode: 0o600});
  }
}
