/** Pi owns conversation cutting and summarization. Requests stay inside the
 * existing provider adapter and shared benchmark budget. */
import type {ModelMessage} from 'ai';
import {createAssistantMessageEventStream, type AssistantMessageEventStream, type AssistantMessage, type Message, type Model as PiModel} from '@earendil-works/pi-ai';
import type {StreamFn} from '@earendil-works/pi-agent-core';
import {findCutPoint, generateSummaryWithUsage, SessionManager, truncateTail} from '@earendil-works/pi-coding-agent';
import type {Completion} from './types.js';

const zeroCost = {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0};
const zeroUsage = {input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0, cost: zeroCost};
// No request uses this URL. Every Pi inference is injected through the existing
// host model callback, including summaries, so credentials and quotas stay put.
export const hostPiModel = {id: 'host-budgeted', name: 'Host-budgeted model', api: 'openai-responses', provider: 'openai',
  baseUrl: 'https://host-adapter.invalid', reasoning: false, input: ['text'], contextWindow: 1_000_000, maxTokens: 2048,
  cost: {input: 0, output: 0, cacheRead: 0, cacheWrite: 0}} as PiModel<any>;

export function piAssistant(reply: Completion): AssistantMessage {
  return {role: 'assistant', api: hostPiModel.api, provider: hostPiModel.provider, model: hostPiModel.id, timestamp: Date.now(),
    content: [...(reply.text ? [{type: 'text' as const, text: reply.text}] : []),
      ...reply.calls.map(call => ({type: 'toolCall' as const, id: call.id, name: call.name,
        arguments: call.input && typeof call.input === 'object' && !Array.isArray(call.input) ? call.input as Record<string, unknown> : {}}))],
    stopReason: reply.calls.length ? 'toolUse' : reply.finish === 'length' ? 'length' : 'stop',
    usage: {input: Math.max(0, reply.usage.input - reply.usage.cached), output: reply.usage.output, cacheRead: reply.usage.cached,
      cacheWrite: 0, totalTokens: reply.usage.input + reply.usage.output, cost: zeroCost}};
}
export function resolvedPiStream(message: AssistantMessage): AssistantMessageEventStream {
  const stream = createAssistantMessageEventStream();
  if (message.stopReason === 'error' || message.stopReason === 'aborted') stream.push({type: 'error', reason: message.stopReason, error: message});
  else stream.push({type: 'done', reason: message.stopReason === 'pending' ? 'stop' : message.stopReason, message});
  stream.end(message); return stream;
}

/** Evidence projection only. Native provider messages remain untouched and are
 * used verbatim for inference; opaque signatures never become summary text. */
export function toPiEvidence(message: ModelMessage): Message[] {
  if (typeof message.content === 'string') return [message.role === 'assistant'
    ? {...piAssistant({text: message.content, calls: [], messages: [], finish: 'stop', usage: {input: 0, output: 0, cached: 0}}), timestamp: 0}
    : {role: 'user', content: message.content, timestamp: 0}];
  if (message.role === 'assistant') return [{role: 'assistant', api: hostPiModel.api, provider: hostPiModel.provider,
    model: hostPiModel.id, timestamp: 0, stopReason: message.content.some(p => p.type === 'tool-call') ? 'toolUse' : 'stop', usage: zeroUsage,
    content: message.content.flatMap<AssistantMessage['content'][number]>(p => p.type === 'text' ? [{type: 'text', text: p.text}]
      : p.type === 'tool-call' ? [{type: 'toolCall', id: p.toolCallId, name: p.toolName, arguments: p.input as Record<string, unknown>}] : [])}];
  if (message.role === 'tool') return message.content.filter(p => p.type === 'tool-result').map(p => ({role: 'toolResult', toolCallId: p.toolCallId, toolName: p.toolName,
    content: [{type: 'text', text: p.toolName === 'read_artifact' ? JSON.stringify({note: 'Previously read artifact; retrieve only a specific missing range.',
      ...(p.output.type === 'json' && p.output.value && typeof p.output.value === 'object' && !Array.isArray(p.output.value)
        ? Object.fromEntries(Object.entries(p.output.value).filter(([key]) => key !== 'content')) : {})}) : JSON.stringify(p.output)}],
    isError: p.output.type === 'error-text' || p.output.type === 'error-json', timestamp: 0}));
  return [{role: 'user', timestamp: 0, content: message.content.map(p => ({type: 'text' as const,
    text: p.type === 'text' ? p.text : 'Public attachment: consult the original file when needed.'}))}];
}

export function piCut(messages: ModelMessage[], start: number, keepTokens: number): number {
  const session = SessionManager.inMemory('/workspace'), origins: number[] = [];
  for (let i = start; i < messages.length; i++) for (const message of toPiEvidence(messages[i]!)) {
    // Pi's normal estimator counts visible reasoning, not encrypted transport.
    // Account for its wire size ONLY while choosing a cut; this padding never
    // enters a model request, semantic summary or stored evidence.
    const source = messages[i]!;
    if (message.role === 'assistant' && source.role === 'assistant' && Array.isArray(source.content)) {
      const bytes = source.content.filter(p => p.type === 'reasoning').reduce((n, p) => n + JSON.stringify(p).length, 0);
      if (bytes) message.content.push({type: 'thinking', thinking: ' '.repeat(Math.min(bytes, 8_000_000))});
    }
    session.appendMessage(message); origins.push(i);
  }
  const entries = session.getEntries();
  const cut = findCutPoint(entries, 0, entries.length, keepTokens);
  return origins[cut.firstKeptEntryIndex] ?? start;
}

export type SummaryCompletion = (system: string, messages: ModelMessage[], maxOutputTokens: number, continuationInput?: number) => Promise<Completion>;
export async function piSummary(messages: ModelMessage[], previous: string | undefined, complete: SummaryCompletion): Promise<string> {
  let originalError: unknown;
  const streamFn: StreamFn = async (_model, context, options) => {
    try {
      const input: ModelMessage[] = context.messages.map(message => ({role: 'user', content: typeof message.content === 'string'
        ? message.content : message.content.map(p => p.type === 'text' ? p.text : '').join('\n')}));
      return resolvedPiStream(piAssistant(await complete(context.systemPrompt ?? '', input, options?.maxTokens ?? 1200)));
    } catch (error) {
      originalError = error;
      return resolvedPiStream({...piAssistant({messages: [], calls: [], text: '', finish: 'error', usage: {input: 0, output: 0, cached: 0}}),
        stopReason: 'error', errorMessage: 'Host summary request failed'});
    }
  };
  try {
    const result = await generateSummaryWithUsage(messages.flatMap(toPiEvidence), hostPiModel, 1500, undefined, undefined, undefined,
      'Keep observed file paths, execution outcomes, unresolved causes and next actions. Never promote a source repair or summary into public-contract evidence. The full original public task is retained separately.',
      previous, 'off', streamFn, {}, {enabled: false, maxRetries: 0, baseDelayMs: 0});
    if (!result.text.trim()) throw new Error('Empty Pi summary');
    return result.text;
  } catch (error) { throw originalError ?? error; }
}

export const piBoundedTail = (text: string, maxBytes: number) => truncateTail(text, {maxBytes, maxLines: 200}).content;
