import type {Completion, ToolSpec} from './types.js';
import {mapLimit} from './util.js';

/** Dispatch an already validated reply without another model/message loop.
 * Any non-read call makes the whole batch sequential. Infrastructure failures
 * stop queued work and drain active siblings before sandbox cleanup can start. */
export async function dispatchTools<T>(reply: Pick<Completion, 'calls'>, specs: Record<string, ToolSpec>, readOnly: ReadonlySet<string>,
  execute: (call: Completion['calls'][number], signal: AbortSignal) => Promise<T>, signal: AbortSignal, readConcurrency = 4): Promise<T[]> {
  signal.throwIfAborted();
  if (reply.calls.length > 32) throw new Error('Too many tool calls in one response; use batches of at most 32');
  if (new Set(reply.calls.map(call => call.id)).size !== reply.calls.length) throw new Error('Duplicate tool call IDs');
  if (!Number.isSafeInteger(readConcurrency) || readConcurrency < 1 || readConcurrency > 8)
    throw new Error('Read concurrency must be an integer from 1 to 8');
  const parallel = reply.calls.every(call => !call.invalid && Object.hasOwn(specs, call.name) && readOnly.has(call.name));
  // Publication observes the current workspace. If submit_candidate is emitted
  // first, defer it behind the same response's writes/receipt; an explicit
  // later submission remains a mutation barrier for trailing calls.
  const hasSubmit = reply.calls.some(call => call.name === 'submit_candidate');
  // Preserve an explicit submission barrier when the model placed submit after
  // earlier mutations (trailing calls must be rejected). Repair the dangerous
  // case where submit is the first call and would capture the old workspace.
  const ordered = parallel || !hasSubmit || reply.calls[0]?.name !== 'submit_candidate'
    ? reply.calls
    : [...reply.calls.slice(1), reply.calls[0]!];
  const results = await mapLimit(ordered, parallel ? readConcurrency : 1, async call => {
    signal.throwIfAborted();
    return execute(call, signal);
  });
  signal.throwIfAborted();
  return results;
}
