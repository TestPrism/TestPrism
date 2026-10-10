import {z} from 'zod';
import {failureClasses, failureLabels, type FailureClass} from './component-feedback.js';
import {lessonComponents, type LessonComponent} from './lessons.js';
import {retrievalCheckpointSchema, type RetrievalCheckpoint} from './lesson-replay.js';
import type {Evaluation} from './evolution-metrics.js';

const eventNames = ['planner.completed', 'planner.failed', 'planner.skipped', 'skills.component_methods',
  'skills.procedure.completed', 'context.compacted', 'model.error', 'workflow.checkpoint'] as const;
const componentSchema = z.object({
  opportunities: z.number().int().nonnegative(), offered: z.number().int().nonnegative(),
  failures: z.array(z.enum(failureClasses)),
});
const feedbackSchema = z.object({
  componentEvents: z.partialRecord(z.enum(eventNames), z.number().int().nonnegative()),
  observedFailureClasses: z.array(z.enum(failureClasses)),
  components: z.partialRecord(z.enum(lessonComponents), componentSchema),
  milestones: z.object({runnerObserved: z.boolean(), runnerExecuted: z.boolean(), sourceChangeObserved: z.boolean(),
    tokenPressure: z.boolean(), timePressure: z.boolean()}),
  status: z.enum(['validated', 'provisional', 'no_submit', 'unknown']),
});
export type ExecutionFeedback = z.infer<typeof feedbackSchema>;
export interface TaskExecutionFeedback {taskKey: string; summary: ExecutionFeedback; retrieval: RetrievalCheckpoint[]}

/** These are diagnostic targets, not claims that the component caused the failure. */
export const failureTargets: Partial<Record<FailureClass, readonly LessonComponent[]>> = {
  'framework-preparation': ['preparation'], 'missing-task-cases': ['preparation', 'generation'],
  'missing-submission': ['generation', 'repair'], 'review-citation': ['review'],
  'contract-overconstraint': ['generation', 'review'], 'nondeterministic-execution': ['generation', 'review'],
  'execution-timeout': ['preparation', 'repair'],
};

/** Keep observable state and phase counts, excluding tool text, paths, task literals and hidden scorer data. */
export function collectExecutionFeedback(events: string, run?: any): Omit<TaskExecutionFeedback, 'taskKey'> {
  const summary: ExecutionFeedback = {componentEvents: {}, observedFailureClasses: [], components: {},
    milestones: {runnerObserved: false, runnerExecuted: false, sourceChangeObserved: false, tokenPressure: false, timePressure: false},
    status: ['validated', 'provisional', 'no_submit'].includes(run?.status) ? run.status : 'unknown'};
  const retrieval: RetrievalCheckpoint[] = [], seen = new Set<string>();
  const component = (scope: LessonComponent) => summary.components[scope] ??= {opportunities: 0, offered: 0, failures: []};
  const failures = (value: unknown, scope?: LessonComponent) => {
    for (const label of failureLabels(JSON.stringify(value) ?? '')) {
      if (!summary.observedFailureClasses.includes(label)) summary.observedFailureClasses.push(label);
      if (scope && !component(scope).failures.includes(label)) component(scope).failures.push(label);
    }
  };
  for (const line of events.split('\n')) {
    if (!line.trim()) continue;
    const {type, data = {}} = JSON.parse(line);
    if (eventNames.includes(type)) summary.componentEvents[type as typeof eventNames[number]] =
      (summary.componentEvents[type as typeof eventNames[number]] ?? 0) + 1;
    if (type === 'policy.retrieval') {
      const snapshot = retrievalCheckpointSchema.parse(data);
      component(snapshot.component).opportunities++;
      if (Array.isArray(data.offered) && data.offered.length) component(snapshot.component).offered++;
      const key = JSON.stringify(snapshot);
      if (!seen.has(key)) {seen.add(key); retrieval.push(snapshot);}
    }
    if (type === 'planner.failed') failures(data, 'planning');
    if (['model.error', 'skills.procedure.completed'].includes(type)) failures(data);
    if (type === 'workflow.checkpoint') {
      summary.milestones.runnerObserved ||= data.runner === true;
      summary.milestones.runnerExecuted ||= data.executed === true;
      summary.milestones.sourceChangeObserved ||= data.sourceChanged === true;
      summary.milestones.tokenPressure ||= data.tokenPressure === true;
      summary.milestones.timePressure ||= data.timePressure === true;
    }
  }
  failures({status: run?.status, reason: run?.reason});
  failures(Object.values(run?.reviews ?? {}).flatMap((review: any) => review.concerns ?? []), 'review');
  failures((run?.matrix ?? []).filter((cell: any) => cell.testId === cell.sourceId && cell.status !== 'pass').map((cell: any) => cell.output));
  summary.observedFailureClasses.sort();
  for (const item of Object.values(summary.components)) item.failures.sort();
  return {summary: feedbackSchema.parse(summary), retrieval};
}

/** Aggregate every task; choose diverse representative mechanisms instead of the first log directories. */
export function reflectionFeedback(evaluations: readonly Evaluation[], scope?: LessonComponent, limit = 8) {
  if (evaluations.some(item => item.split !== 'train')) throw new Error('Reflection feedback requires training evidence');
  const failures: Partial<Record<FailureClass, number>> = {}, priorities: Partial<Record<LessonComponent, number>> = {};
  const cards = new Map<string, ExecutionFeedback>();
  let tasks = 0, providerAffectedTasks = 0;
  const observedTasks = new Set<string>();
  for (const evaluation of evaluations) for (const execution of evaluation.execution ?? []) {
    if (!evaluation.taskKeys.includes(execution.taskKey)) throw new Error('Feedback task is outside its evaluation');
    const pair = `${evaluation.seed}:${execution.taskKey}`;
    if (observedTasks.has(pair)) throw new Error('Duplicate task/repeat in reflection feedback');
    observedTasks.add(pair); tasks++;
    const summary = feedbackSchema.parse(execution.summary);
    const providerAffected = summary.observedFailureClasses.some(label => label.startsWith('provider-'));
    if (providerAffected) providerAffectedTasks++;
    const targets = new Set<LessonComponent>();
    const specific = summary.observedFailureClasses.some(label => label !== 'missing-submission' && failureTargets[label]);
    for (const label of summary.observedFailureClasses) {
      failures[label] = (failures[label] ?? 0) + 1;
      // Provider failures can strand a submission. Do not use those tasks as capability priorities.
      if (!providerAffected && !(specific && label === 'missing-submission'))
        for (const target of failureTargets[label] ?? []) targets.add(target);
    }
    if (!providerAffected && summary.components.planning?.failures.length) targets.add('planning');
    for (const target of targets) priorities[target] = (priorities[target] ?? 0) + 1;
    // Milestones distinguish missing artifacts, missing execution and missing source capture.
    const key = JSON.stringify([summary.status, summary.observedFailureClasses, summary.milestones,
      Object.entries(summary.components).map(([name, value]) => [name, value.opportunities > 0, value.offered > 0])]);
    if (!cards.has(key)) cards.set(key, summary);
  }
  const score = (card: ExecutionFeedback) => scope ? Number(card.components[scope]?.failures.length ||
    card.observedFailureClasses.some(label => failureTargets[label]?.includes(scope))) : Number(card.observedFailureClasses.length > 0);
  const representatives = [...cards].sort(([ka, a], [kb, b]) => score(b) - score(a) || ka.localeCompare(kb));
  const buckets = new Map<string, ExecutionFeedback[]>();
  for (const [, card] of representatives) {
    const key = JSON.stringify(card.observedFailureClasses);
    const bucket = buckets.get(key) ?? []; bucket.push(card); buckets.set(key, bucket);
  }
  const selected: ExecutionFeedback[] = [];
  // Keep a successful control if present, then rotate failure mechanisms.
  const control = representatives.find(([, card]) => card.status !== 'no_submit' && card.status !== 'unknown'
    && !card.observedFailureClasses.length)?.[1];
  const slots = Math.max(0, limit - Number(!!control));
  while (selected.length < slots) {
    let added = false;
    for (const bucket of buckets.values()) {
      if (control) while (bucket[0] === control) bucket.shift();
      const next = bucket.shift();
      if (next) {selected.push(next); added = true;}
      if (selected.length === slots) break;
    }
    if (!added) break;
  }
  if (control && limit > 0) selected.push(control);
  return {tasks, providerAffectedTasks, failures, componentPriorities: priorities,
    cards: selected,
    interpretation: 'Observable process diagnostics and mutation priorities only; method exposure is not execution or causal reward'};
}
