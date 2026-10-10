import {z} from 'zod';
import {lessonComponents, retrieveLessons, type Lesson, type LessonComponent} from './lessons.js';
import {capabilityTags} from './skill-context.js';
import {hash, redact} from './util.js';
import type {Store} from './store.js';
import type {Evaluation} from './evolution-metrics.js';
import type {Genome} from './genome.js';

export const retrievalCheckpointSchema = z.object({
  component: z.enum(lessonComponents), capabilities: z.array(z.enum(capabilityTags)).max(7),
  surface: z.string().max(24000), complete: z.boolean(), maxChars: z.number().int().min(1).max(1600),
});
export type RetrievalCheckpoint = z.infer<typeof retrievalCheckpointSchema>;

/** Host-only observation. This surface never enters a reflection prompt or a release. */
export async function recordLessonRetrieval(store: Store, surface: string,
  scope: {component: LessonComponent; capabilities: string[]}, maxChars: number, methods: readonly Lesson[]): Promise<void> {
  const sanitized = String(redact(surface));
  await store.event('policy.retrieval', {...scope, maxChars, surface: sanitized.slice(0, 24000),
    complete: sanitized === surface && surface.length <= 24000,
    offered: methods.map(item => ({id: item.id, version: item.version ?? hash(item)}))});
}

/** Dry replay measures prompt intervention opportunity, never task success or causal credit. */
export function replayLessonMutation(parent: Genome, candidate: Genome, evidence: readonly Evaluation[]) {
  if (evidence.some(item => item.split !== 'train')) throw new Error('Lesson replay requires training evidence');
  const before = parent.genes.lessons, after = candidate.genes.lessons;
  const changed = [...new Set([...before, ...after].map(item => item.id))]
    .filter(id => hash(before.find(item => item.id === id) ?? null) !== hash(after.find(item => item.id === id) ?? null));
  const tasks = new Set<string>(), affectedTasks = new Set<string>();
  let checkpoints = 0, incomplete = 0, interventions = 0;
  const scopes = new Set([...before, ...after].filter(item => changed.includes(item.id)).flatMap(item =>
    item.component ? [item.component] : ['preparation', 'generation', 'repair']));
  for (const evaluation of evidence) {
    incomplete += evaluation.taskKeys.filter(key => !evaluation.execution?.some(item => item.taskKey === key && item.retrieval.length)).length;
    for (const execution of evaluation.execution ?? []) {
      if (!evaluation.taskKeys.includes(execution.taskKey)) throw new Error('Lesson replay task is outside its evaluation');
      for (const raw of execution.retrieval) {
        const snapshot = retrievalCheckpointSchema.parse(raw);
        if (!scopes.has(snapshot.component)) continue;
        tasks.add(execution.taskKey); checkpoints++;
        if (!snapshot.complete) {incomplete++; continue;}
        const rendered = (items: Lesson[]) => retrieveLessons(items, snapshot.surface, snapshot.maxChars, snapshot)
          .map(item => item.text).join('\n');
        if (rendered(before) !== rendered(after)) {interventions++; affectedTasks.add(execution.taskKey);}
      }
    }
  }
  return {status: !changed.length ? 'not_applicable' : interventions ? 'intervention_observed'
    : !checkpoints || incomplete ? 'unknown' : 'no_observed_intervention',
    tasks: tasks.size, affectedTasks: affectedTasks.size, checkpoints, incomplete, interventions,
    purpose: 'Retrieval replay on observed training contexts only; not execution, quality or causality evidence'};
}
