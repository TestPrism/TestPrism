import {z} from 'zod';
import {applyDiff} from '@openai/agents-core';
import type {Sandbox} from './runtime.js';
import type {ToolSpec} from './types.js';
import {hash, publicPath} from './util.js';

const target = z.string().min(1).max(1024), diff = z.string().min(1).max(1_000_000);
const input = z.strictObject({operation: z.discriminatedUnion('type', [
  z.strictObject({type: z.literal('create_file'), path: target, diff}),
  z.strictObject({type: z.literal('update_file'), path: target, diff}),
  z.strictObject({type: z.literal('delete_file'), path: target}),
])});
const {$schema: _, ...schema} = z.toJSONSchema(input, {target: 'draft-7'});
export const v4aPatchTool: ToolSpec = {schema,
  description: 'Create, update or delete one public workspace file. diff uses headerless V4A hunks (@@ with context, - and + lines), not unified-diff headers or Begin/End Patch wrappers.', parse: value => input.parse(value)};

/** The SDK owns V4A parsing; all filesystem operations stay in the existing sandbox. */
export async function applyV4APatch(sandbox: Sandbox, raw: unknown, signal: AbortSignal): Promise<Record<string, unknown>> {
  const {operation} = input.parse(raw), file = publicPath(operation.path);
  signal.throwIfAborted();
  let before: Buffer | undefined;
  try {before = await sandbox.read(file);}
  catch (error) {if (operation.type !== 'create_file' || (error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;}
  if (operation.type === 'create_file' && before !== undefined) throw new Error(`New file already exists: ${file}`);
  if (before && (before.length > 1_000_000 || !Buffer.from(before.toString('utf8')).equals(before) || before.includes(0)))
    throw new Error('V4A patch requires a UTF-8 text file of at most 1 MB');
  const mode = before !== undefined ? await sandbox.mode(file) : file.endsWith('.sh') ? 0o755 : 0o644;
  if (operation.type === 'delete_file') {
    signal.throwIfAborted(); await sandbox.remove(file);
    return {ok: true, files: [{path: file, deleted: true}]};
  }
  if (Buffer.byteLength(operation.diff) > 1_000_000) throw new Error('Patch exceeds 1 MB');
  // Bound the SDK's synchronous context search before applying model input.
  // Large patches can be split without guessing or rewriting their contents.
  const sourceLines = before?.toString('utf8').split('\n').length ?? 0;
  const patchLines = operation.diff.split('\n');
  if (patchLines.filter(line => /^@@(?: |$)/.test(line)).length > 64 || sourceLines * patchLines.length > 4_000_000)
    throw new Error('Patch exceeds the context-search limit; split it into smaller focused operations');
  const after = Buffer.from(applyDiff(before?.toString('utf8') ?? '', operation.diff, operation.type === 'create_file' ? 'create' : 'default'));
  if (after.length > 1_000_000 || after.includes(0)) throw new Error('Patched file must be UTF-8 text of at most 1 MB');
  signal.throwIfAborted(); await sandbox.write(file, after, mode);
  return {ok: true, files: [{path: file, deleted: false, hash: hash(after)}]};
}
