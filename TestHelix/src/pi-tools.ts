import {createEditTool, createReadTool} from '@earendil-works/pi-coding-agent';
import type {Sandbox} from './runtime.js';
import {hash, publicPath} from './util.js';

/** All Pi filesystem/process operations are redirected into Sandbox. Never use
 * Pi's default host operations or discover user extensions/credentials. */
export class PiWorkspaceTools {
  private readonly root: string;
  private readonly files = new Map<string, string>();
  constructor(readonly sandbox: Sandbox) { this.root = `/testhelix-sandboxes/${hash(sandbox.id)}`; }
  private toolPath(file: string): string {
    const relative = publicPath(file), virtual = this.root + '/' + hash(relative);
    this.files.set(virtual, relative); return virtual;
  }
  private relative(file: string): string {
    if (file === this.root) return '.';
    const relative = this.files.get(file);
    if (!relative) throw new Error('Unknown Pi sandbox path');
    return relative;
  }
  private async write(file: string, text: string): Promise<void> {
    if (Buffer.byteLength(text) > 1_000_000) throw new Error('Edited file exceeds 1 MB');
    const relative = this.relative(file);
    await this.sandbox.write(relative, Buffer.from(text), relative.endsWith('.sh') ? 0o755 : undefined);
  }
  async editFile(file: string, oldText: string, newText: string, signal: AbortSignal): Promise<void> {
    const tool = createEditTool(this.root, {operations: {
      access: async file => {await this.sandbox.mode(this.relative(file));},
      readFile: file => this.sandbox.read(this.relative(file)),
      writeFile: (file, text) => this.write(file, text),
    }});
    await tool.execute('host-edit', {path: this.toolPath(file), edits: [{oldText, newText}]}, signal);
  }
  /** Pi selects and bounds text lines. The host adds evidence hashes and JSON
   * column cursors; only an oversized single line needs that extra pagination. */
  async readLines(file: string, start: number, limit: number, read: (file: string) => Promise<Buffer>, signal: AbortSignal) {
    const tool = createReadTool(this.root, {operations: {
      access: async file => {this.relative(file);},
      readFile: file => read(this.relative(file)),
    }});
    const result = await tool.execute('host-read', {path: this.toolPath(file), offset: start, limit}, signal);
    const details = result.details as {truncation?: {firstLineExceedsLimit: boolean; content: string}} | undefined;
    if (details?.truncation?.firstLineExceedsLimit) return undefined;
    const text = details?.truncation?.content ?? result.content.filter(p => p.type === 'text').map(p => p.text).join('\n');
    return text.split('\n');
  }
}
