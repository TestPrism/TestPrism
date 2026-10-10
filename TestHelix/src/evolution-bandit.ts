import { fitness, validateEvaluation, type Evaluation, type Sample } from './evolution-metrics.js';
import type { Gene, Genome } from './genome.js';
import { contextSkills, type SkillId } from './skills.js';
import { skillContext, type SkillContext } from './skill-context.js';
import {lessonComponents, type LessonComponent} from './lessons.js';

export interface EvolutionArm { id: string; component: Gene; context?: SkillContext; skill?: SkillId | null; scope?: LessonComponent }
export interface MutationFocus {components: readonly Gene[]; lessonComponents: readonly LessonComponent[]}
export function evolutionArms(parent: Genome, enabled: readonly SkillId[], samples: readonly Sample[], focus?: MutationFocus): EvolutionArm[] {
  const arms: EvolutionArm[] = [];
  const counts = new Map<SkillContext, number>();
  for (const sample of samples) {
    const context = sample.context ?? skillContext('', sample.source);
    counts.set(context, (counts.get(context) ?? 0) + 1);
  }
  for (const [context, n] of [...counts].sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))) {
    if (n < 2) continue; // A one-task bucket is not enough to motivate a specialist rule.
    const candidates = contextSkills[context].filter(id => enabled.includes(id));
    if (!candidates.length) continue;
    const route = parent.genes.skillRoutes.find(route => route.context === context);
    // No route leaves selection to the model. It is distinct from both a
    // specialist preload and explicit abstention (which also suppresses triage).
    const current = route?.skill;
    for (const skill of [...candidates, null]) if (skill !== current)
      arms.push({id: `route:${context}:${skill ?? 'none'}`, component: 'skillRoutes', context, skill});
  }
  for (const component of ['guidance', 'lessons'] as const)
    arms.push({id: component, component, ...(component === 'lessons' ? {scope: 'generation' as const} : {})});
  for (const scope of lessonComponents.filter(scope => scope !== 'generation'))
    arms.push({id: `lessons:${scope}`, component: 'lessons', scope});
  for (const component of ['agentSteps', 'contextTokens', 'toolOutputChars', 'initialCandidates', 'maxCandidates', 'repairSteps', 'reviewMaxOutputTokens', 'maxRounds'] as const)
    arms.push({id: component, component});
  return arms.filter(arm => !focus || focus.components.includes(arm.component)
    && (!arm.scope || focus.lessonComponents.includes(arm.scope)));
}

/** Paired TRAIN comparisons only. Reward a preload policy, never claim individual skill causality. */
export function trainingReward(parent: Evaluation, child: Evaluation, context?: SkillContext, samples: readonly Sample[] = []): number {
  if (parent.split !== 'train' || child.split !== 'train') throw new Error('Bandit feedback must be training evidence');
  validateEvaluation(parent, parent.taskKeys); validateEvaluation(child, child.taskKeys);
  if (parent.seed !== child.seed || parent.taskKeys.length !== child.taskKeys.length
    || parent.taskKeys.some(key => !child.taskKeys.includes(key))) throw new Error('Bandit credit requires paired tasks and repeats');
  const keys = context ? new Set(samples.filter(sample => (sample.context ?? skillContext('', sample.source)) === context).map(sample => sample.key)) : undefined;
  const subset = (evaluation: Evaluation) => ({...evaluation, trials: evaluation.trials.filter(trial => !keys || keys.has(trial.task_key ?? trial.task_id))});
  const left = subset(parent), right = subset(child);
  if (!left.trials.length || left.trials.length !== right.trials.length) throw new Error('No paired training outcomes for this context');
  const a = fitness([left]), b = fitness([right]);
  if (b.valid < a.valid || b.errors > a.errors) return 0;
  const gain = .7 * (b.strict - a.strict) + .15 * (b.valid - a.valid) + .15 * (b.invalid - a.invalid);
  // Cost can only break a quality tie; aggregate spending cannot isolate a context's cost.
  const saving = !context && !parent.usage.estimated && !child.usage.estimated && b.strict >= a.strict && b.invalid >= a.invalid
    ? .02 * Math.max(-1, Math.min(1, 1 - b.tokens / Math.max(1, a.tokens))) : 0;
  return Math.max(0, Math.min(1, .5 + .5 * gain + saving));
}

/** UCB schedules development experiments. Serving uses frozen routes and never explores or updates. */
export function selectEvolutionArm(arms: readonly EvolutionArm[], attempts: Record<string, number>, genomes: readonly Genome[],
  evaluations: readonly Evaluation[], samples: readonly Sample[], priorities: Partial<Record<LessonComponent, number>> = {}) {
  if (!arms.length) throw new Error('No eligible evolution arms');
  const rewards = new Map<string, number[]>();
  for (const genome of genomes) {
    const arm = genome.mutation?.arm;
    if (!arm || genome.mutation?.operator === 'crossover') continue;
    const parentId = genome.parents[0];
    for (const result of evaluations.filter(e => e.split === 'train' && e.genomeId === genome.id)) {
      const parent = evaluations.find(e => e.split === 'train' && e.genomeId === parentId && e.seed === result.seed);
      if (!parent) continue;
      const context = arm.startsWith('route:') ? arm.split(':')[1] as SkillContext : undefined;
      const list = rewards.get(arm) ?? []; list.push(trainingReward(parent, result, context, samples)); rewards.set(arm, list);
    }
  }
  const total = Object.values(attempts).reduce((sum, n) => sum + n, 0);
  const statistics = arms.map(arm => {
    const observed = rewards.get(arm.id) ?? [], trials = Math.max(attempts[arm.id] ?? 0, observed.length);
    const mean = observed.reduce((sum, value) => sum + value, 0) / Math.max(1, trials);
    const familyTrials = Object.entries(attempts).filter(([id]) => arm.id.startsWith('route:') ? id.startsWith('route:') : id === arm.id)
      .reduce((sum, [, n]) => sum + n, 0);
    return {id: arm.id, trials, mean, score: trials ? mean + Math.sqrt(2 * Math.log(total + 1) / trials) : null, familyTrials,
      priority: arm.scope ? priorities[arm.scope] ?? 0 : 0};
  });
  const best = statistics.reduce((a, b) => (b.score ?? Infinity) > (a.score ?? Infinity)
    || (b.score === null && a.score === null && (b.priority > a.priority
      || b.priority === a.priority && b.familyTrials < a.familyTrials)) ? b : a);
  return {arm: arms.find(arm => arm.id === best.id)!, statistics};
}
