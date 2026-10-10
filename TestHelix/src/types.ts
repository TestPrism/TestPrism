import { z } from 'zod';
import type { ModelMessage } from 'ai';
import { skillIds } from './skills.js';
import { skillRoutesSchema } from './skill-policy.js';
import { lessonLibrarySchema } from './lessons.js';
import type { ReviewFinding, ReviewExecutionCheck } from './review.js';
import type { ExecutionEvidence } from './execution-evidence.js';
import {contractLimitsSchema} from './contract-schema.js';

export const configSchema = z.object({
  model: z.object({
    provider: z.enum(['compatible', 'openai-responses', 'anthropic']).default('compatible'),
    name: z.string().min(1),
    baseURL: z.url().refine(value => /^https?:\/\//.test(value) && !new URL(value).username && !new URL(value).password,
      'baseURL must use HTTP/HTTPS without embedded credentials'),
    apiKeyEnv: z.string().default('TESTHELIX_API_KEY'),
    maxOutputTokens: z.number().int().positive().default(8192),
    reviewMaxOutputTokens: z.number().int().min(128).default(2048),
    outputTokenField: z.enum(['max_tokens', 'max_completion_tokens']).default('max_tokens'),
    imageTokenReservation: z.number().int().positive().default(32768),
    timeoutMs: z.number().int().positive().default(120000),
    extraBody: z.record(z.string(), z.unknown()).default({}),
    providerOptions: z.record(z.string(), z.record(z.string(), z.json())).default({}),
    headersEnv: z.record(z.string(), z.string().regex(/^[A-Za-z_][A-Za-z0-9_]*$/)).default({}),
    forceTool: z.boolean().default(true),
    streaming: z.boolean().default(false),
    maxRetries: z.number().int().min(0).max(3).default(1),
    maxRetryDelayMs: z.number().int().min(0).max(60000).default(5000),
    inputPricePerMillion: z.number().nonnegative().default(0),
    cachedInputPricePerMillion: z.number().nonnegative().optional(),
    outputPricePerMillion: z.number().nonnegative().default(0),
  }),
  budget: z.object({
    maxCalls: z.number().int().positive().default(48),
    maxInputTokens: z.number().int().positive().default(500000),
    maxOutputTokens: z.number().int().positive().default(100000),
    maxCostUSD: z.number().positive().optional(),
    wallMs: z.number().int().positive().default(900000),
    concurrency: z.number().int().positive().default(2),
  }).prefault({}),
  search: z.object({
    skills: z.array(z.enum(skillIds)).max(skillIds.length).default([]),
    skillResources: z.boolean().default(true),
    skillRoutes: skillRoutesSchema.default([]),
    guidance: z.string().max(2400).default(''),
    lessons: lessonLibrarySchema.default([]),
    initialCandidates: z.number().int().min(1).max(8).default(2),
    schedule: z.enum(['eager', 'staged']).default('eager'),
    challengeSteps: z.number().int().min(2).max(14).default(6),
    differentialSteps: z.number().int().min(0).max(4).default(0),
    maxCandidates: z.number().int().min(1).max(8).default(4),
    maxRounds: z.number().int().min(1).max(4).default(2),
    agentSteps: z.number().int().min(2).max(100).default(12),
    repairSteps: z.number().int().min(1).max(40).default(5),
    minIndependentSupport: z.number().int().min(1).max(7).default(1),
    review: z.boolean().default(true),
    allowProvisional: z.boolean().default(true),
    provisionalPolicy: z.enum(['reviewed-positive', 'reviewed-baseline', 'baseline-only']).default('reviewed-positive')
      .describe('reviewed-positive also requires a passing repair control; reviewed-baseline requires semantic approval and a fresh baseline failure; baseline-only is an explicit diagnostic ablation.'),
  }).prefault({}),
  context: z.object({
    maxTokens: z.number().int().min(2000).default(18000),
    recentTurns: z.number().int().min(1).default(3).describe('Legacy configuration compatibility; Pi selects retained history by token budget.'),
    toolOutputChars: z.number().int().min(512).default(10000),
    tokenizer: z.enum(['heuristic', 'cl100k_base', 'o200k_base']).default('heuristic'),
    compactTarget: z.number().min(0.4).max(0.9).default(0.7),
    compactTimeFraction: z.number().min(0).max(1).default(0.15),
    compactTimeoutMs: z.number().int().positive().default(30000),
    compactMaxOutputTokens: z.number().int().positive().default(2048),
  }).prefault({}),
  execution: z.object({
    timeoutMs: z.number().int().positive().default(120000),
    testTimeoutMs: z.number().int().positive().default(180000),
    repeat: z.number().int().min(1).max(5).default(2),
    concurrency: z.number().int().positive().default(2),
    readConcurrency: z.number().int().min(1).max(8).default(4),
    network: z.enum(['none', 'inherit']).default('none'),
    maxArtifactBytes: z.number().int().positive().default(4000000),
    contract: contractLimitsSchema.prefault({}),
  }).prefault({}),
  networkSearch: z.object({
    enabled: z.boolean().default(true),
    githubApiUrl: z.url().default('https://api.github.com'),
    githubTokenEnv: z.string().regex(/^[A-Z_][A-Z0-9_]*$/).default('GITHUB_TOKEN'),
    webApiUrl: z.url().default('https://api.search.brave.com/res/v1/web/search'),
    webApiKeyEnv: z.string().regex(/^[A-Z_][A-Z0-9_]*$/).default('BRAVE_SEARCH_API_KEY'),
    timeoutMs: z.number().int().positive().max(30000).default(8000),
    maxResults: z.number().int().positive().max(20).default(10),
  }).prefault({}),
  planner: z.strictObject({
    enabled: z.boolean().default(false),
    maxCalls: z.number().int().min(1).max(2).default(2),
    maxOutputTokens: z.number().int().min(1024).max(16384).default(8192),
  }).prefault({}),
  scheduler: z.strictObject({
    providerCooldownMs: z.number().int().min(50).max(60000).default(1000),
    providerUnavailableMs: z.number().int().min(100).max(3600000).default(300000),
    enabled: z.boolean().default(false),
    directory: z.string().min(1).default('.runs/shared-api'),
    apiConcurrency: z.number().int().min(1).max(32).default(4),
    planningConcurrency: z.number().int().min(1).max(4).default(1),
    // Defaults for the outer TestPrism harness; CLI flags can override them.
    // These do not schedule candidates or enter the single-task planner prompt.
    taskConcurrency: z.number().int().min(1).max(16).default(2),
    patchConcurrency: z.number().int().min(1).max(8).default(1),
  }).prefault({}),
}).superRefine((value, ctx) => {
  if (value.scheduler.planningConcurrency > value.scheduler.apiConcurrency)
    ctx.addIssue({code: 'custom', message: 'Planning concurrency exceeds global API concurrency'});
  if (value.planner.enabled && (!['gpt-5.4', 'gpt-5.5'].includes(value.model.name) || value.model.provider !== 'openai-responses'
      || value.model.providerOptions.openai?.reasoningEffort !== 'medium'))
    ctx.addIssue({code: 'custom', message: 'The planned workflow requires GPT-5.4 or GPT-5.5 Responses medium; only its planner uses xhigh'});
  if (value.search.skillRoutes.some(route => route.skill !== null && !value.search.skills.includes(route.skill)))
    ctx.addIssue({code: 'custom', message: 'Skill routes must use enabled skills'});
  if (value.search.initialCandidates > value.search.maxCandidates)
    ctx.addIssue({code: 'custom', message: 'initialCandidates exceeds maxCandidates'});
  if (value.search.schedule === 'staged' && (value.search.initialCandidates !== 1 || value.search.maxCandidates !== 2
    || value.budget.concurrency !== 1 || value.search.maxRounds < 2 || !value.search.review))
    ctx.addIssue({code: 'custom', message: 'Staged search requires one initial/two maximum candidates, serial generation, review and at least two rounds'});
  if (value.search.differentialSteps > 0 && value.search.schedule !== 'staged')
    ctx.addIssue({code: 'custom', message: 'Differential coverage challenges require staged search'});
  if (value.budget.maxCostUSD && !(value.model.inputPricePerMillion && value.model.outputPricePerMillion))
    ctx.addIssue({code: 'custom', message: 'A dollar budget requires explicit input and output prices'});
  for (const key of ['model', 'messages', 'tools', 'tool_choice', 'max_tokens', 'max_completion_tokens', 'max_output_tokens', 'input', 'instructions', 'stream', 'n'])
    if (key in value.model.extraBody) ctx.addIssue({code: 'custom', message: `extraBody cannot override host-controlled field ${key}`});
  if (value.model.provider !== 'compatible' && Object.keys(value.model.extraBody).length)
    ctx.addIssue({code: 'custom', message: 'Use providerOptions for native providers; extraBody applies only to compatible'});
  for (const header of Object.keys(value.model.headersEnv))
    if (!/^[!#$%&'*+.^_`|~0-9A-Za-z-]+$/.test(header) || /^(host|content-length|content-type)$/i.test(header))
      ctx.addIssue({code: 'custom', message: `Unsupported custom header ${header}`});
  const openai = value.model.providerOptions.openai;
  if (value.model.provider === 'openai-responses' && openai && ['previousResponseId', 'conversation', 'background', 'store'].some(key => key in openai))
    ctx.addIssue({code: 'custom', message: 'Responses conversation state and store are owned by the host'});
});
export type Config = z.infer<typeof configSchema>;

export const taskSchema = z.object({
  id: z.string().min(1),
  instruction: z.string().min(1),
  systemPrompt: z.string().default(''),
  systemPromptMode: z.enum(['replace', 'append']).default('append'),
  source: z.string().default('local'),
  runtime: z.discriminatedUnion('kind', [
    z.object({kind: z.literal('local'), root: z.string().min(1)}),
    z.object({kind: z.literal('docker'), container: z.string().min(1), root: z.string().default('/app')}),
    z.object({kind: z.literal('testprism'), container: z.string().min(1), root: z.string().default('/app'),
      socket: z.string().min(1)}),
  ]),
  testTimeoutMs: z.number().positive().optional(),
});
export type Task = z.infer<typeof taskSchema>;
export type Role = 'candidate' | 'reviewer';
export interface ToolSpec { description: string; schema: Record<string, unknown>; parse?: (input: unknown) => unknown }
export interface Call { id: string; name: string; input: unknown; invalid?: string }
export interface Usage { input: number; output: number; cached: number; estimated?: boolean }
export interface ProtocolNotice {
  callId: string; toolName: string; status: 'normalized' | 'rejected'; inputHash: string; changes: string[]; error?: string;
}
export interface Completion {
  messages: ModelMessage[]; calls: Call[]; text: string; finish: string; usage: Usage;
  protocol?: ProtocolNotice[];
}
export interface Model {
  complete(args: {system: string; messages: ModelMessage[]; tools: Record<string, ToolSpec>;
    signal: AbortSignal; maxOutputTokens: number; forceTool?: string}): Promise<Completion>;
}
export interface FileArtifact { path: string; data: string; mode: number; deleted?: boolean }
export interface Candidate {
  id: string; lineage: string; round: number; strategy: string;
  source: FileArtifact[]; tests: FileArtifact[]; sourceHash: string; testsHash: string;
  replayScript?: string;
  requirements: string[]; rationale: string; sessionId: string;
  contract?: import('./task-contract.js').OutputContract;
  contractCheck?: import('./task-contract.js').ContractValidation;
}
export type Status = 'pass' | 'fail' | 'error' | 'timeout';
export interface Execution {
  code: number | null; stdout: string; stderr: string; durationMs: number;
  timedOut: boolean; truncated: boolean;
}
export interface Observation {
  testId: string; sourceId: string; status: Status; signature: string;
  durationMs: number; output: string; artifact?: string;
  diagnostics?: ExecutionEvidence;
  reproducibility?: {
    kind: 'status-change' | 'diagnostic-change';
    sameAssertionSites?: boolean;
    runs: Array<{status: Status; signature: string; artifact?: string}>;
    firstDifference?: {line: number; before: string; after: string};
  };
}
export interface Review {
  accepted: boolean; concerns: string[]; missingRequirements: string[];
  resolution?: 'complete' | 'needs-evidence';
  coveredRequirements: string[];
  findings?: ReviewFinding[];
  executionChecks?: ReviewExecutionCheck[];
  discardedEvidence?: Array<{finding: number; path: string; error: string}>;
  crossEvidenceHash?: string;
}
export interface RunResult {
  schemaVersion: 1; taskId: string; status: 'validated' | 'provisional' | 'no_submit';
  reason: string; testsDir: string | null; selected: string[]; witnesses: string[];
  candidates: Candidate[]; matrix: Observation[]; reviews: Record<string, Review>;
  usage: Usage & {calls: number; costUSD: number | null}; elapsedMs: number;
  taskDigest: string; configDigest: string;
  implementationDigest: string;
}
