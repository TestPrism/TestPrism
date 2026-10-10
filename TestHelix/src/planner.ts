import {z} from 'zod';
import type {Config, Model, Task, ToolSpec} from './types.js';
import type {Sandbox} from './runtime.js';
import {Budget} from './budget.js';
import {Store} from './store.js';
import {hash, errorText, readJSON, requestSize} from './util.js';
import path from 'node:path';
import {contextTokens} from './context.js';
import {capabilitiesFor, capabilityTags} from './skill-context.js';
import {retrieveLessons} from './lessons.js';
import {recordLessonRetrieval} from './lesson-replay.js';
import {retryPlan} from './retry.js';
import {numberedPublicTask, publicTaskExcerpt, taskLines} from './review.js';

export const taskPlanSchema = z.strictObject({
  capabilities: z.array(z.enum(capabilityTags)).min(1).max(7),
  requirements: z.array(z.strictObject({requirement: z.string().min(1).max(220), quote: z.string().min(1).max(280)})).min(1).max(6),
  preparation: z.array(z.string().min(1).max(180)).max(4),
  branches: z.array(z.strictObject({focus: z.string().min(1).max(240), verification: z.string().min(1).max(240)})).min(1).max(2),
  unknowns: z.array(z.string().min(1).max(180)).max(4),
  completion: z.string().min(1).max(300),
});
export type TaskPlan = z.infer<typeof taskPlanSchema>;
const taskPlanWireSchema = taskPlanSchema.extend({requirements: z.array(z.strictObject({
  requirement: z.string().min(1).max(220), taskLines,
})).min(1).max(6)});
const planningStateSchema = z.strictObject({identity: z.string(), calls: z.number().int().min(0).max(2), plan: taskPlanSchema.optional()});
const environmentSchema = z.strictObject({output: z.string().max(2400), code: z.number().int().nullable(),
  files: z.array(z.string()).max(60).refine(files => JSON.stringify(files).length <= 2400,
    'Environment file list exceeds its serialized context bound'), timedOut: z.boolean(), truncated: z.boolean()});
export type EnvironmentObservation = z.infer<typeof environmentSchema>;
const environmentStateSchema = z.strictObject({identity: z.string(), observation: environmentSchema});
const planningIdentity = (task: Task, config: Config) => hash({task, planner: config.planner, model: config.model});

/** Host facts survive a lossy model plan and are shared without sharing solver strategies. */
export async function observeTaskEnvironment(task: Task, config: Config, sandbox: Sandbox, budget: Budget,
  store: Store): Promise<EnvironmentObservation | undefined> {
  const identity = planningIdentity(task, config), file = path.join(store.root, 'environment.json');
  const saved = await readJSON(file);
  if (saved) {
    const state = environmentStateSchema.parse(saved);
    if (state.identity !== identity) throw new Error('Environment checkpoint identity mismatch');
    return state.observation;
  }
  if (budget.remainingMs < 1000) return;
  try {
    const probe = await sandbox.exec('pwd\nfor tool in python python3 uv node npm java javac go cargo gcc g++ ruby php bash; do command -v "$tool" 2>/dev/null || true; done\nfor project_python in .venv/bin/python venv/bin/python; do if [ -x "$project_python" ]; then printf \'%s\\n\' "$project_python"; fi; done\nif command -v conda >/dev/null 2>&1; then conda env list --json 2>/dev/null || true; fi\n',
      Math.min(10000, budget.remainingMs));
    const listed = await sandbox.list(''), files: string[] = [];
    for (const file of listed) {
      if (files.length === 60 || JSON.stringify([...files, file]).length > 2400) break;
      files.push(file);
    }
    const observation = environmentSchema.parse({output: probe.stdout.slice(0, 2400), code: probe.code,
      files, timedOut: probe.timedOut,
      truncated: probe.truncated || probe.stdout.length > 2400 || files.length < listed.length});
    await store.save('environment.json', {identity, observation});
    return observation;
  } catch (error) {
    await store.event('environment.failed', {error: errorText(error)});
    return;
  }
}

/** One bounded planning pass, optionally revised once; no workspace mutation. */
export async function planTask(task: Task, config: Config, sandbox: Sandbox, budget: Budget, store: Store, model: Model,
  correction?: string): Promise<TaskPlan | undefined> {
  if (!config.planner.enabled) return;
  const identity = planningIdentity(task, config);
  const file = path.join(store.root, 'planner.json');
  const state = planningStateSchema.parse(await readJSON(file) ?? {identity, calls: 0});
  if (state.identity !== identity) throw new Error('Planner checkpoint identity mismatch');
  if (state.plan) taskPlanSchema.parse(state.plan);
  if (state.plan && !correction || state.calls >= config.planner.maxCalls) return state.plan;
  // Keep candidate execution and independent review available under the shared cap.
  if (budget.remainingCalls <= 8 || budget.remainingMs < 5000) return state.plan;
  try {
    const environment = await observeTaskEnvironment(task, config, sandbox, budget, store);
    if (!environment) return state.plan;
    const system = 'Plan a complete regression-and-repair workflow from the original public contract and observed environment. Return one branch for the first solver: write and run public-contract tests, implement a minimal source repair as an internal positive control, rerun, and submit. Completion requires baseline failure and repaired-source success; only tests are exported. Preserve agent-tests/ at the initial working directory even if public source lives elsewhere. For command/state changes or public-source edits outside that workspace, plan a captured internal replayScript stored inside it, outside agent-tests/; the host replays it before positive-control validation. Reserve execution turns for that repair loop. Independent peers derive their own approach without this plan. Cite each requirement by taskLines in the numbered original task; the host extracts quotes. Do not invent expected values, implementations, tooling availability, or new acceptance rules. Prefer one focused inspection that resolves a concrete unknown over a broad repository survey. The host owns budgets and independent validation. Replanning must address the observed invalid premise, preserving unaffected work.';
    const lessonScope = {component: 'planning' as const, capabilities: capabilitiesFor(task.instruction, task.source)};
    const methods = retrieveLessons(config.search.lessons, task.instruction, 1200, lessonScope);
    await recordLessonRetrieval(store, task.instruction, lessonScope, 1200, methods);
    const messages = [{role: 'user' as const, content: JSON.stringify({instruction: numberedPublicTask(task.instruction),
      environment,
      capabilities: lessonScope.capabilities, methods: methods.map(x => x.text),
      ...(correction ? {previousPlan: state.plan, correction: correction.slice(0, 2000)} : {})})}];
    const {$schema: _, ...schema} = z.toJSONSchema(taskPlanWireSchema, {target: 'draft-7'});
    const tools: Record<string, ToolSpec> = {submit_plan: {description: 'Seal a bounded task-local plan. Cite requirement taskLines; the host extracts original text.', schema,
      parse: value => taskPlanWireSchema.parse(value)}};
    if (contextTokens({system, messages, tools}, config.context.tokenizer) > config.context.maxTokens) {
      await store.event('planner.skipped', {reason: 'Public evidence exceeds planning context; original contract remains with executors'}); return state.plan;
    }
    const reservation = await budget.reserve(requestSize({system, messages, tools}).bytes + 128,
      config.planner.maxOutputTokens, {calls: 8, input: Math.min(48000, Math.floor(budget.remainingInputTokens / 2)), output: 8192});
    let reply;
    let dispatched = false;
    try {
      state.calls++; await store.save('planner.json', state);
      await store.event('model.requested', {session: 'planner', role: 'planner', model: config.model.name, reasoningEffort: 'xhigh', outputCap: reservation.output});
      reply = await budget.slot(() => {
        dispatched = true;
        return model.complete({system, messages, tools, forceTool: 'submit_plan', maxOutputTokens: reservation.output,
          signal: AbortSignal.timeout(Math.max(1, Math.min(config.model.timeoutMs, budget.remainingMs)))});
      });
      await budget.settle(reservation, reply.usage.estimated ? undefined : reply.usage);
    } catch (error) {
      await budget.settle(reservation, !dispatched || retryPlan(error, 0, 0, 0).rejected ? {input: 0, output: 0, cached: 0} : undefined);
      throw error;
    }
    const call = reply.calls.find(call => call.name === 'submit_plan' && !call.invalid);
    const wire = taskPlanWireSchema.parse(call?.input);
    const next = taskPlanSchema.parse({...wire, requirements: wire.requirements.map(item => ({
      requirement: item.requirement, quote: publicTaskExcerpt(task.instruction, item.taskLines, 1).trim().slice(0, 280),
    }))});
    state.plan = next; await store.save('planner.json', state);
    await store.event('planner.completed', {revision: state.calls, digest: hash(next), capabilities: next.capabilities, usage: reply.usage});
    return next;
  } catch (error) {
    await store.event('planner.failed', {error: errorText(error), status: (error as {statusCode?: number}).statusCode,
      calls: state.calls, retainedPlan: !!state.plan});
    return state.plan;
  }
}

export function planBrief(plan: TaskPlan, branch: number): string {
  const focus = plan.branches[branch % plan.branches.length];
  // The full original contract is pinned beside this brief. Keep verified
  // quotes in planner.json, without paying to repeat them on every action.
  return 'Task-local approach; revise disproved premises. Complete the full original contract with a baseline-failing regression and a passing source repair control, then submit.\n' +
    JSON.stringify({focus, preparation: plan.preparation, unknowns: plan.unknowns});
}
