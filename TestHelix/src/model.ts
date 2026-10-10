import { createOpenAICompatible } from '@ai-sdk/openai-compatible';
import { createOpenAI } from '@ai-sdk/openai';
import { createAnthropic } from '@ai-sdk/anthropic';
import { generateText, streamText, tool, jsonSchema, wrapLanguageModel, type ToolSet, type LanguageModel } from 'ai';
import { configSchema, type Completion, type Config, type Model, type ToolSpec, type ProtocolNotice } from './types.js';
import { AsyncLocalStorage } from 'node:async_hooks';
import { normalizeToolInput } from './protocol.js';
import { hash } from './util.js';
import {withAPISlot} from './api-scheduler.js';
import {healthyFetch, providerAdmission} from './provider-health.js';
import {EnvHttpProxyAgent, type Dispatcher} from 'undici';
import {withTransportEvidence} from './retry.js';

// Pi's transitive Undici can replace Node's global dispatcher during import.
// Bind the opt-in proxy to model requests explicitly; one process-wide pool,
// standard NO_PROXY semantics, no global dispatcher mutation or custom routing.
const proxyDispatcher = process.env.NODE_USE_ENV_PROXY === '1' || process.execArgv.includes('--use-env-proxy')
  ? new EnvHttpProxyAgent() : undefined;

export class LocalModelError extends Error { readonly requestSent = false; constructor(message: string) { super(message); this.name = 'LocalModelError'; } }

/** SDK adapters own provider wire protocols; the host owns accounting, retries and tool execution. */
export class SDKModel implements Model {
  private model: Exclude<LanguageModel, string>;
  private toolsets = new WeakMap<Record<string, ToolSpec>, ToolSet>();
  private outputCaps = new AsyncLocalStorage<number>();
  readonly config: Config;
  constructor(config: Config, readonly role: 'execution' | 'planner' = 'execution') {
    this.config = configSchema.parse(config);
    const options = this.config.model;
    const apiKey = process.env[options.apiKeyEnv];
    if (!apiKey) throw new Error(`Missing credential environment variable: ${options.apiKeyEnv}`);
    const headers = Object.fromEntries(Object.entries(options.headersEnv).map(([header, variable]) => {
      const value = process.env[variable];
      if (!value || /[\r\n]/.test(value)) throw new Error(`Missing or invalid header environment variable: ${variable}`);
      return [header, value];
    }));
    const connection = {baseURL: options.baseURL.replace(/\/$/, ''), apiKey, headers, fetch: async (url: Parameters<typeof fetch>[0], init?: RequestInit) => {
      const cap = this.outputCaps.getStore();
      if (cap === undefined) throw new LocalModelError('Model request has no host output reservation');
      const body = JSON.parse(String(init?.body));
      const wireCap = body.max_output_tokens ?? body.max_completion_tokens ?? body.max_tokens;
      if (!Number.isSafeInteger(wireCap) || wireCap < 1 || wireCap > cap)
        throw new LocalModelError(`Provider output cap ${wireCap} exceeds reserved ${cap}`);
      const deadline = init?.signal ?? AbortSignal.timeout(options.timeoutMs);
      await providerAdmission(this.config, this.role === 'planner', deadline);
      return withAPISlot(this.config, this.role === 'planner', deadline,
        signal => healthyFetch(this.config,this.role === 'planner',signal,()=> {
          const request: RequestInit & {dispatcher?: Dispatcher} = {...init, signal,
            ...(proxyDispatcher ? {dispatcher: proxyDispatcher} : {})};
          return withTransportEvidence(() => fetch(url, request).then(async response => {
            // Keep the lease until the response body is consumed, not just headers.
            const bytes = await response.arrayBuffer();
            return new Response([204, 205, 304].includes(response.status) ? null : bytes,
              {status: response.status, statusText: response.statusText, headers: response.headers});
          }));
        }));
    }};
    if (options.provider === 'openai-responses') this.model = createOpenAI(connection).responses(options.name);
    else if (options.provider === 'anthropic') this.model = createAnthropic(connection)(options.name);
    else this.model = createOpenAICompatible({
      ...connection, name: 'testhelix', includeUsage: true,
      transformRequestBody: body => {
        const result = {...body, ...options.extraBody};
        if (options.outputTokenField === 'max_completion_tokens') {
          result.max_completion_tokens = result.max_tokens; delete result.max_tokens;
        }
        return result;
      },
    }).chatModel(options.name);
  }
  async complete(args: Parameters<Model['complete']>[0]): Promise<Completion> {
    args.signal.throwIfAborted();
    let tools = this.toolsets.get(args.tools);
    if (!tools) {
      tools = Object.fromEntries(Object.entries(args.tools).map(([name, spec]) => [name, tool({
        description: spec.description, inputSchema: jsonSchema(spec.schema, spec.parse ? {validate: value => {
          try { return {success: true as const, value: spec.parse!(value)}; }
          catch (error) { return {success: false as const, error: error instanceof Error ? error : new Error(String(error))}; }
        }} : undefined),
      })]));
      this.toolsets.set(args.tools, tools);
    }
    const options = this.config.model;
    const providerOptions = structuredClone(options.providerOptions);
    if (this.role === 'planner') {
      if (!this.config.planner.enabled) throw new LocalModelError('Planner model is disabled');
      providerOptions.openai = {...providerOptions.openai, reasoningEffort: 'xhigh'};
    }
    if (options.provider === 'openai-responses') {
      // Responses gateways may supply their own large default instructions when
      // this top-level field is absent, even with a system item in input. Bind
      // the exact host/native contract here on every stateless request.
      providerOptions.openai = {...providerOptions.openai, store: false, strictJsonSchema: false, instructions: args.system,
        include: [...new Set(['reasoning.encrypted_content', ...(Array.isArray(providerOptions.openai?.include) ? providerOptions.openai.include as string[] : [])])]};
    }
    const thinking = providerOptions.anthropic?.thinking as {type?: string; budgetTokens?: number} | undefined;
    let sdkOutputTokens = args.maxOutputTokens;
    if (options.provider === 'anthropic' && thinking?.type === 'enabled') {
      if (args.maxOutputTokens < 1152) throw new LocalModelError('Anthropic thinking needs at least 1152 reserved output tokens; increase the output allowance');
      const requested = thinking.budgetTokens ?? 1024;
      if (!Number.isSafeInteger(requested) || requested < 1024) throw new LocalModelError('Anthropic thinking budgetTokens must be an integer >= 1024');
      const thinkingTokens = Math.min(requested, Math.max(1024, Math.floor(args.maxOutputTokens / 2)));
      providerOptions.anthropic!.thinking = {...thinking, budgetTokens: thinkingTokens};
      // AI SDK adds thinking tokens to maxOutputTokens for this provider.
      sdkOutputTokens -= thinkingTokens;
    }
    // Anthropic extended thinking cannot combine with forced tool_choice. Keep the final-turn instruction instead.
    const force = args.forceTool && options.forceTool && !(options.provider === 'anthropic' && thinking && thinking.type !== 'disabled');
    const protocol: ProtocolNotice[] = [], rejected = new Map<string, string>();
    // Normalize before SDK parsing so executable calls and assistant history share
    // exactly the same inputs. Keep IDs, ordering, reasoning and provider metadata.
    const normalizeCall = <T extends {type: 'tool-call'; input: string; toolName: string; toolCallId: string}>(part: T): T => {
        try {
          if (!Object.hasOwn(args.tools, part.toolName)) throw new Error('Tool is not available in this session');
          const normalized = normalizeToolInput(part.input, args.tools[part.toolName]!);
          if (normalized.changes.length) protocol.push({callId: part.toolCallId, toolName: part.toolName,
            status: 'normalized', inputHash: hash(part.input), changes: normalized.changes});
          return {...part, input: normalized.input};
        } catch (error) {
          const message = error instanceof Error ? error.message : 'Invalid tool arguments';
          rejected.set(part.toolCallId, message);
          protocol.push({callId: part.toolCallId, toolName: part.toolName, status: 'rejected',
            inputHash: hash(part.input), changes: [], error: message});
          // No SDK tool has an execute function. Host call.invalid blocks execution;
          // a small placeholder keeps malformed/huge arguments out of future prompts.
          return {...part, input: '{}'};
        }
    };
    const model = wrapLanguageModel({model: this.model, middleware: {specificationVersion: 'v4',
      wrapGenerate: async ({doGenerate}) => {
        const response = await doGenerate();
        return {...response, content: response.content.map(part => part.type === 'tool-call' ? normalizeCall(part) : part)};
      },
      wrapStream: async ({doStream}) => {
        const response = await doStream();
        return {...response, stream: response.stream.pipeThrough(new TransformStream({
          transform(part, controller) {controller.enqueue(part.type === 'tool-call' ? normalizeCall(part) : part);},
        }))};
      },
    }});
    const request = {
      model, ...(options.provider === 'openai-responses' ? {} : {system: args.system}), messages: args.messages, tools, maxRetries: 0,
      maxOutputTokens: sdkOutputTokens, abortSignal: args.signal, providerOptions,
      ...(force ? {toolChoice: {type: 'tool' as const, toolName: args.forceTool!}} : {}),
    };
    const result = await this.outputCaps.run(args.maxOutputTokens, async () => {
      if (!options.streaming) return generateText(request);
      const stream = streamText({...request, onError: () => {}});
      // The SDK owns SSE parsing and native response metadata. Do not execute a
      // partial tool call or treat a truncated response as a completed request.
      for await (const part of stream.fullStream) {
        if (part.type === 'error') throw part.error;
        if (part.type === 'abort') throw args.signal.reason ?? new DOMException('Provider stream aborted', 'AbortError');
      }
      const finishReason = await stream.finishReason;
      if (finishReason === 'other' || finishReason === 'error') throw new Error('Provider stream ended without a complete response');
      return {response: await stream.response, toolCalls: await stream.toolCalls, text: await stream.text,
        usage: await stream.usage, finishReason};
    });
    return {
      // SDK creates tool-error messages for schema-invalid calls even without an
      // execute callback. The host emits every result, including errors, exactly
      // once; forwarding SDK tool messages would duplicate call/result pairs.
      messages: result.response.messages.filter(message => message.role !== 'tool'),
      calls: result.toolCalls.map(call => ({id: call.toolCallId, name: call.toolName, input: call.input,
        ...(rejected.has(call.toolCallId) ? {invalid: rejected.get(call.toolCallId)!}
          : 'invalid' in call && call.invalid ? {invalid: String(call.error).slice(0, 500)} : {})})),
      ...(protocol.length ? {protocol} : {}),
      text: result.text, finish: result.finishReason,
      usage: {input: result.usage.inputTokens ?? 0, output: result.usage.outputTokens ?? 0,
        cached: result.usage.inputTokenDetails?.cacheReadTokens ?? 0,
        estimated: result.usage.inputTokens === undefined || result.usage.outputTokens === undefined},
    };
  }
}
