import {z} from 'zod';
import {child, type Genome} from './genome.js';
import {lessonUpdateSchema, updateLesson, type LessonComponent} from './lessons.js';
import type {ToolSpec} from './types.js';

export const reflectionSystem = 'Improve a test-generation policy from development-set execution evidence. Change exactly one policy component. For lessons, add, revise or remove ONE item and preserve every other item. Write applicability, action and an observable verification condition, with specific retrieval triggers. Triggers match literal normalized phrases in public task instructions, the current phase/reason, and workspace evidence: runnerPresent, execution command and status, source repair and task coverage. Aggregate failure labels and metrics are not automatically present in that retrieval surface. Choose phrases observable when the method applies; a useful lesson that cannot be retrieved has no effect. Use structuredExecutionFeedback to distinguish absent artifacts, unexecuted runners, preparation failures, review issues and resource pressure. A runner receipt or source change is an observation, not proof of task coverage or correctness. Component priorities are diagnostic hypotheses; provider failures are not evidence of a deficient skill. Lesson retrieval means a prompt intervention opportunity, not that the model followed the method. Abstract failure mechanisms across tasks; do not copy names, paths, literal expected values, solutions, task IDs, code, hidden patches, or instructions to weaken validation. Preserve the public task as authoritative. Do not alter model, tools, validators, or global budgets. Explain the observed failure that motivates the mutation. Aggregate training evidence is noisy; it is not proof of causality.';

export function lessonReflectionTools(): Record<string, ToolSpec> {
  const {$schema: _, ...schema} = z.toJSONSchema(lessonUpdateSchema, {target: 'draft-7'});
  return {submit_lesson: {
    description: 'Add, revise or remove one reusable lesson. State applicability, action and observable verification; host preserves all other lessons.',
    schema, parse: value => lessonUpdateSchema.parse(value),
  }};
}

/** The same single-item, component-ownership and genome validation for every reflection caller. */
export function reflectedLesson(parent: Genome, raw: unknown, evidence: string[], forbidden: string[],
  arm?: {id: string; scope?: LessonComponent}): Genome {
  let proposed = lessonUpdateSchema.parse(raw);
  if (arm?.scope) {
    const original = parent.genes.lessons.find(item => item.id === proposed.id);
    if (proposed.operation === 'remove') {
      if (original && (original.component ?? 'generation') !== arm.scope) throw new Error('Mutation targets a different component');
    } else {
      if (proposed.component && proposed.component !== arm.scope) throw new Error('Mutation targets a different component');
      if (original && (original.component ?? 'generation') !== arm.scope) throw new Error('Cannot move an unrelated component method');
      proposed = {...proposed, component: arm.scope};
    }
  }
  const update = updateLesson(parent.genes.lessons, proposed);
  return child(parent, {...parent.genes, lessons: update.lessons}, {component: 'lessons', rationale: update.rationale,
    evidence, operator: 'reflection', ...(arm ? {arm: arm.id} : {})}, forbidden);
}
