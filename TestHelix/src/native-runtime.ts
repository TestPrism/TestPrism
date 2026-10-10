import net from 'node:net';
import {randomUUID} from 'node:crypto';
import {DockerSandbox, publishTests, type SandboxFactory} from './runtime.js';
import type {Execution, FileArtifact, Task} from './types.js';

export function nativeRequest<T>(socket: string, method: string, params: Record<string, unknown>, signal?: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const connection = net.createConnection({path: socket});
    connection.setEncoding('utf8');
    let data = '', settled = false;
    const finish = (error?: unknown, value?: T) => {
      if (settled) return; settled = true;
      signal?.removeEventListener('abort', abort); connection.destroy();
      if (error) reject(error); else resolve(value!);
    };
    const abort = () => finish(signal?.reason ?? new Error('Native request cancelled'));
    connection.on('error', error => finish(error));
    connection.on('end', () => {if (!settled) finish(new Error('Native bridge closed before returning a result'));});
    connection.on('connect', () => connection.write(JSON.stringify({method, params}) + '\n'));
    connection.on('data', chunk => {
      data += chunk.toString('utf8');
      if (data.length > 8_000_000) {finish(new Error('Native response exceeds its limit')); return;}
      if (!data.endsWith('\n')) return;
      try {const response = JSON.parse(data); finish(response.ok ? undefined : new Error(response.error), response.result);}
      catch (error) {finish(error);}
    });
    signal?.addEventListener('abort', abort, {once: true});
    if (signal?.aborted) abort();
  });
}

class NativeSandbox extends DockerSandbox {
  constructor(readonly socket: string, readonly nativeId: string, container: string, root: string,
    readonly remainingMs: () => number) {super(container, root, false);}
  override async exec(script: string, timeoutMs: number, signal?: AbortSignal): Promise<Execution> {
    timeoutMs = Math.min(timeoutMs, this.remainingMs());
    if (timeoutMs <= 0) throw new Error('Native task deadline exhausted');
    const deadline = AbortSignal.any([AbortSignal.timeout(timeoutMs + 15000), ...(signal ? [signal] : [])]);
    try {return await nativeRequest(this.socket, 'exec', {id: this.nativeId, script, timeoutMs}, deadline);}
    catch (error) {await this.close().catch(() => {}); throw error;}
  }
  async terminal(action: string, name: string, text: string, signal: AbortSignal): Promise<Record<string, unknown>> {
    const timeoutMs = Math.min(30000, this.remainingMs());
    if (timeoutMs <= 0) throw new Error('Native task deadline exhausted');
    return nativeRequest(this.socket, 'terminal', {id: this.nativeId, action, name, text, timeoutMs},
      AbortSignal.any([signal, AbortSignal.timeout(timeoutMs + 5000)]));
  }
  override close(): Promise<void> {
    return nativeRequest(this.socket, 'close', {id: this.nativeId}, AbortSignal.timeout(95000));
  }
}

export class NativeFactory implements SandboxFactory {
  constructor(readonly runtime: Extract<Task['runtime'], {kind: 'testprism'}>, readonly remainingMs = () => 300000) {}
  async fork(label: string): Promise<NativeSandbox> {
    const timeoutMs = Math.min(300000, this.remainingMs()), operationId = randomUUID().replaceAll('-', '');
    if (timeoutMs <= 0) throw new Error('Native task deadline exhausted');
    try {
      const result = await nativeRequest<{id: string; container: string; root: string}>(this.runtime.socket, 'fork',
        {label, operationId, timeoutMs}, AbortSignal.timeout(timeoutMs + 1000));
      return new NativeSandbox(this.runtime.socket, result.id, result.container, result.root, this.remainingMs);
    } catch (error) {
      // Cover the race where startup succeeded but the response was lost.
      await nativeRequest(this.runtime.socket, 'cancel', {operationId}, AbortSignal.timeout(3000)).catch(() => {});
      throw error;
    }
  }
  publish(files: FileArtifact[]): Promise<void> {return publishTests(new DockerSandbox(this.runtime.container, this.runtime.root, false), files);}
  close(): Promise<void> {return nativeRequest(this.runtime.socket, 'close_all', {}, AbortSignal.timeout(95000));}
}
