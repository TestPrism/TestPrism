import type { ModelMessage } from 'ai';
import type { Config, ToolSpec } from './types.js';
import type { Store } from './store.js';
import { clip, estimateTokens, hash, requestSize } from './util.js';
import { getEncoding, type Tiktoken } from 'js-tiktoken';
import {piCut, piSummary, piBoundedTail, type SummaryCompletion} from './pi-runtime.js';

type ContextConfig = Pick<Config['context'], 'maxTokens' | 'recentTurns' | 'toolOutputChars'> & Partial<Config['context']>;
interface Overhead { system?: string; tools?: Record<string, ToolSpec>; tail?: ModelMessage[]; maxInputReservation?: number | (() => number); imageTokenReservation?: number; summarize?: SummaryCompletion }
const encoders = new Map<string, Tiktoken>();
export function contextTokens(value: unknown, encoding: ContextConfig['tokenizer'] = 'heuristic'): number {
  if (encoding === 'heuristic') return estimateTokens(value);
  let encoder = encoders.get(encoding);
  if (!encoder) { encoder = getEncoding(encoding); encoders.set(encoding, encoder); }
  let images = 0;
  const serialized = JSON.stringify(value, (_key, item) => {
    if (item?.type === 'image') { images++; return {type: 'image', mediaType: item.mediaType}; }
    return item;
  });
  // Wire formatting and model image tokenization still differ; this is an estimate, not billing.
  return encoder.encode(serialized, [], []).length + images * 4096;
}
/** A model-readable archive is evidence, not a replay of the provider wire protocol.
 * Keep the canonical transcript intact for stateless continuation. Never present
 * encrypted reasoning as text, and link artifact reads instead of nesting them. */
export function readableTranscript(messages: ModelMessage[]): unknown[] {
  return messages.map(message => ({role: message.role,
    content: typeof message.content === 'string' ? message.content : message.content.flatMap<unknown>(part => {
      if (part.type === 'reasoning') return [];
      if (part.type === 'tool-call') return [{type: part.type, toolCallId: part.toolCallId, toolName: part.toolName, input: part.input}];
      if (part.type === 'tool-result') {
        const output = part.output;
        const value = output.type === 'json' && output.value && typeof output.value === 'object' && !Array.isArray(output.value)
          ? output.value as Record<string, unknown> : undefined;
        return [{type: part.type, toolCallId: part.toolCallId, toolName: part.toolName,
          output: part.toolName === 'read_artifact' && value ? {type: 'json', value: {
            ok: value.ok, id: value.id, offset: value.offset, nextOffset: value.nextOffset, totalChars: value.totalChars,
            note: 'Previously read artifact; recover a specific missing range by ID. Content is not duplicated in this archive.'}} : output}];
      }
      if (part.type === 'text') return [{type: part.type, text: part.text}];
      return [{type: 'text', text: 'Public attachment omitted from evidence archive; inspect the original file if needed.'}];
    })}));
}
interface ProjectionState { archivedUntil: number; archive?: string; summaryArtifact?: string; summary?: string; signature: string }
const states = new WeakMap<ModelMessage[], ProjectionState>();
export const contextSnapshot = (messages: ModelMessage[]) => structuredClone(states.get(messages));
export function restoreContext(messages: ModelMessage[], state?: ProjectionState): void {
  if (state && Number.isInteger(state.archivedUntil) && state.archivedUntil >= 0 && state.archivedUntil <= messages.length)
    states.set(messages, structuredClone(state));
}

/** Pi chooses safe conversation boundaries and generates cumulative summaries.
 * This adapter pins the benchmark contract, preserves native provider messages,
 * accounts for host schemas/budgets, and scopes raw evidence to this session. */
export async function projectContext(messages: ModelMessage[], notes: string, config: ContextConfig, store: Store, overhead: Overhead = {}): Promise<ModelMessage[]> {
  const signature = hash({config, root: store.root});
  let state = states.get(messages);
  if (!state || state.signature !== signature) { state = {archivedUntil: 0, signature}; states.set(messages, state); }
  const count = (view: ModelMessage[]) => contextTokens({system: overhead.system ?? '', tools: overhead.tools ?? {},
    messages: [...view, ...(overhead.tail ?? [])]}, config.tokenizer);
  const reservation = (view: ModelMessage[]) => {
    const all = [...view, ...(overhead.tail ?? [])];
    const size = requestSize({system: overhead.system ?? '', messages: all, tools: overhead.tools ?? {}}, overhead.imageTokenReservation ?? 32768);
    return size.bytes + size.imageTokens + all.length * 64;
  };
  const maxInput = () => typeof overhead.maxInputReservation === 'function' ? overhead.maxInputReservation() : overhead.maxInputReservation;
  const exceeds = (view: ModelMessage[], limit = config.maxTokens) => {
    const cap = maxInput();
    return count(view) > limit || (cap !== undefined && reservation(view) > cap);
  };
  const starts = messages.flatMap((message, i) => message.role === 'assistant' ? [i] : []);
  const pinnedEnd = starts[0] ?? messages.length;
  const memory: ModelMessage[] = notes ? [{role: 'user', content: `Working memory; reverify stale evidence:\n${notes}`}]: [];
  const fixed = [...messages.slice(0, pinnedEnd), ...memory], fixedCap = maxInput();
  // Do not buy a summary when even the immutable task/tools cannot fit.
  if (count(fixed) > config.maxTokens) throw new Error(`Pinned task and tools exceed context limit (${count(fixed)} > ${config.maxTokens}).`);
  if (fixedCap !== undefined && reservation(fixed) > fixedCap)
    throw new Error(`Pinned task and tools exceed remaining input reservation (${reservation(fixed)} > ${fixedCap}).`);
  const build = (until = state!.archivedUntil, summary = state!.summary): ModelMessage[] => [
    ...messages.slice(0, pinnedEnd),
    ...(state!.archive ? [{role: 'user' as const, content: `Earlier evidence index: artifact ${state!.archive}. Recover only specific missing facts; current files remain authoritative.`}] : []),
    ...(summary ? [{role: 'user' as const, content: `Pi session summary (working memory, not public-contract evidence):\n${summary}`}] : []),
    ...messages.slice(Math.max(pinnedEnd, until)), ...memory,
  ];
  const before = count(build());
  // Pi estimates visible content differently from the host's native messages.
  // Check the configured target with the same counter that triggers compaction,
  // otherwise a minimal cut can buy another summary on the very next turn.
  const target = Math.min(config.maxTokens, Math.max(count(fixed) + 256,
    Math.floor(config.maxTokens * (config.compactTarget ?? .7))));
  let projected = build();
  let summaryAttempted = false;
  while (exceeds(projected) && starts.length) {
    const start = Math.max(pinnedEnd, state.archivedUntil);
    const keep = Math.max(256, Math.floor(config.maxTokens * (config.compactTarget ?? .7))
      - count([...messages.slice(0, pinnedEnd), ...memory]));
    let cut = piCut(messages, start, keep);
    while (exceeds(build(cut, 'Summary pending.'), target) && cut < starts.at(-1)!) {
      const next = piCut(messages, cut + 1, 0);
      if (next <= cut) break;
      cut = next;
    }
    // Pi's estimator excludes opaque signatures. If the retained provider bytes
    // still exceed the host cap, ask Pi to cut at a fresh user checkpoint after
    // the completed exchange. Preserve trailing corrections verbatim.
    if (cut <= start || exceeds(build(cut, 'Summary pending.'))) {
      let end = messages.length;
      while (end > start && messages[end - 1]!.role === 'user') end--;
      cut = piCut([...messages.slice(0, end), {role: 'user', content: 'Continue from current workspace after completed tools.'}], start, 0);
    }
    const calls = messages.slice(start, cut).flatMap(m => m.role === 'assistant' && Array.isArray(m.content)
      ? m.content.filter(p => p.type === 'tool-call') : []);
    const results = messages.slice(start, cut).flatMap(m => m.role === 'tool' ? m.content.filter(p => p.type === 'tool-result') : []);
    const complete = calls.length === results.length && new Set(calls.map(c => c.toolCallId)).size === calls.length
      && calls.every(c => results.filter(r => r.toolCallId === c.toolCallId && r.toolName === c.toolName).length === 1);
    if (cut > start && complete) {
      const archive = await store.artifact(JSON.stringify(readableTranscript(messages.slice(pinnedEnd, cut))));
      let summary: string, semantic = false;
      try {
        if (!overhead.summarize || summaryAttempted) throw new Error('Use archived evidence without another paid summary');
        summaryAttempted = true;
        // The full summary remains recoverable by artifact ID. Feeding it back
        // recursively lets each checkpoint grow even though its visible form was
        // already shortened to fit the next action.
        const previous = state.summary;
        const continuationInput = reservation(build(cut, '')) + 1800; // Minimal summary + archive index after JSON escaping.
        summary = await piSummary(messages.slice(start, cut), previous,
          (system, messages, maxOutput) => overhead.summarize!(system
            + '\nKeep this checkpoint concise: preserve only decisions, exact file paths, execution outcomes and the next unresolved action. Earlier raw evidence remains archived.',
          messages, Math.min(maxOutput, config.compactMaxOutputTokens ?? 2048), continuationInput)); semantic = true;
      } catch (error) {
        // A failed or unaffordable summary must not corrupt history or invent a
        // successful checkpoint. Pi's bounded extraction remains recoverable.
        summary = `Semantic summary unavailable; completed commands must not be repeated. Full evidence artifact ${archive}.\n`
          + (state.summaryArtifact ? `Prior checkpoint artifact ${state.summaryArtifact}.\n` : '')
          + piBoundedTail(JSON.stringify(readableTranscript(messages.slice(start, cut))), Math.min(1800, config.maxTokens));
        await store.event('context.summary_fallback', {reason: error instanceof Error ? error.message : 'Error', evidenceArtifact: archive});
      }
      state.archivedUntil = cut; state.archive = archive;
      state.summaryArtifact = await store.artifact(summary);
      // Larger tool schemas can leave room only for an excerpt. Preserve the
      // fallback status so an extracted tail cannot look like a successful summary.
      const presentSummary = (bytes: number) => semantic ? piBoundedTail(summary, bytes)
        : 'Semantic summary unavailable; use archived evidence.\n' + piBoundedTail(summary,
          Math.max(0, bytes - Buffer.byteLength('Semantic summary unavailable; use archived evidence.\n')));
      state.summary = presentSummary(Math.min(6000, config.maxTokens));
      projected = build();
      // Presentation can be shortened without changing Pi's complete archived
      // summary, the original public contract, or the retained native exchange.
      let summaryBytes = Buffer.byteLength(state.summary);
      while (exceeds(projected, target) && summaryBytes > 160) {
        summaryBytes = Math.floor(summaryBytes / 2);
        state.summary = presentSummary(summaryBytes); projected = build();
      }
      await store.event('context.compacted', {engine: 'pi-coding-agent@0.85.1', semantic, archivedUntil: cut,
        evidenceArtifact: archive, summaryArtifact: state.summaryArtifact, canonicalMessagesUnchanged: true,
        targetTokens: target, afterTokens: count(projected), summaryBytes: Buffer.byteLength(summary),
        visibleSummaryBytes: Buffer.byteLength(state.summary)});
      // A summary or a parallel request may change the available reservation.
      // Re-cut only complete retained exchanges if necessary, without buying
      // another summary for this projection. Canonical messages stay intact.
    } else break;
  }
  const after = count(projected), inputReservation = reservation(projected);
  if (after > config.maxTokens) throw new Error(`Pinned task, tools and latest turn exceed context limit (${after} estimated > ${config.maxTokens}).`);
  const finalCap = maxInput();
  if (finalCap !== undefined && inputReservation > finalCap)
    throw new Error(`Pinned task, tools and latest turn exceed remaining input reservation (${inputReservation} > ${finalCap}).`);
  await store.event('context.projected', {beforeTokens: before, afterTokens: after, encoding: config.tokenizer ?? 'heuristic',
    engine: 'pi-coding-agent@0.85.1', archivedUntil: state.archivedUntil,
    ...(finalCap === undefined ? {} : {inputReservation, maxInputReservation: finalCap})});
  return projected;
}
