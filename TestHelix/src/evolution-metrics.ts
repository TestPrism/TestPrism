import type { Genome } from './genome.js';
import { random } from './genome.js';
import { strictScore, trialSchema, type Trial } from './report.js';
import type { Usage } from './types.js';
import type { SkillContext } from './skill-context.js';
import type {TaskExecutionFeedback} from './evolution-feedback.js';

export interface Sample { key: string; source: string; context?: SkillContext }
export interface Evaluation {
  genomeId: string; split: 'train' | 'validation'; seed: number; taskKeys: string[];
  trials: Trial[]; usage: Usage & {calls: number}; durationMs: number; evidenceDigest: string;
  feedback?: string[];
  execution?: TaskExecutionFeedback[];
}
export function validateEvaluation(evaluation: Evaluation, keys: string[]): Evaluation {
  if (!Array.isArray(evaluation.taskKeys) || new Set(evaluation.taskKeys).size !== keys.length
    || evaluation.taskKeys.length !== keys.length || keys.some(key => !evaluation.taskKeys.includes(key)))
    throw new Error('Evaluation taskKeys must match scheduled tasks exactly');
  const trials = evaluation.trials.map(raw => trialSchema.parse(raw));
  const observed = trials.map(trial => trial.task_key ?? trial.task_id);
  if (new Set(observed).size !== observed.length || observed.length !== keys.length || keys.some(key => !observed.includes(key)))
    throw new Error('Evaluation must contain exactly one result for each scheduled task');
  for (const trial of trials) if (trial.patch_results.length && !strictScore(trial).schemaOK)
    throw new Error('Evaluation contains a malformed 5+5 patch result');
  const usage = evaluation.usage;
  if (!usage || ![usage.calls, usage.input, usage.output, usage.cached].every(value => Number.isSafeInteger(value) && value >= 0))
    throw new Error('Evaluation is missing complete token/call accounting');
  const executionKeys = (evaluation.execution ?? []).map(item => item.taskKey);
  if (new Set(executionKeys).size !== executionKeys.length || executionKeys.some(key => !keys.includes(key)))
    throw new Error('Execution feedback must belong to unique scheduled tasks');
  return {...evaluation, trials};
}
export function fitness(evaluations: Evaluation[]) {
  const trials = evaluations.flatMap(evaluation => evaluation.trials), scores = trials.map(strictScore);
  const n = scores.length || 1;
  const source = (key: string) => key.split('/')[0]!;
  const perSource: Record<string, number[]> = {};
  trials.forEach((trial, i) => (perSource[source(trial.task_key ?? trial.task_id)] ??= []).push(Number(scores[i]!.resolved)));
  return {strict: scores.filter(s => s.resolved).length / n,
    valid: scores.reduce((s, v) => s + v.validAcceptance, 0) / n,
    invalid: scores.reduce((s, v) => s + v.invalidRejection, 0) / n,
    baseline: scores.filter(s => s.baselineDetected).length / n,
    tokens: evaluations.reduce((s, e) => s + e.usage.input + e.usage.output, 0) / n,
    errors: scores.filter(s => !s.schemaOK || s.patchErrors > 0).length / n,
    bySource: Object.fromEntries(Object.entries(perSource).map(([key, values]) => [key, values.reduce((s, v) => s + v, 0) / values.length]))};
}
export function rank(a: ReturnType<typeof fitness>, b: ReturnType<typeof fitness>): number {
  return b.strict - a.strict || b.valid - a.valid || b.invalid - a.invalid || a.errors - b.errors || a.tokens - b.tokens;
}
export function pareto(genomes: Genome[], evaluations: Evaluation[], maxSize: number): Genome[] {
  const metric = (genome: Genome) => fitness(evaluations.filter(e => e.split === 'train' && e.genomeId === genome.id));
  const available = genomes.filter(genome => evaluations.some(e => e.split === 'train' && e.genomeId === genome.id));
  const dominates = (a: ReturnType<typeof fitness>, b: ReturnType<typeof fitness>) => {
    const dimensions = [[a.strict, b.strict], [a.valid, b.valid], [a.invalid, b.invalid], [-a.tokens, -b.tokens],
      ...Object.keys(b.bySource).map(key => [a.bySource[key] ?? 0, b.bySource[key]!])];
    return dimensions.every(([x, y]) => x! >= y!) && dimensions.some(([x, y]) => x! > y!);
  };
  const frontier = available.filter(genome => !available.some(other => other !== genome && dominates(metric(other), metric(genome))))
    .sort((a, b) => rank(metric(a), metric(b)) || a.id.localeCompare(b.id));
  const selected = frontier.slice(0, 1);
  // Preserve complementary source specialists before filling the remaining slots.
  for (const source of Object.keys(frontier[0] ? metric(frontier[0]).bySource : {}).sort()) {
    const best = [...frontier].sort((a, b) => (metric(b).bySource[source] ?? 0) - (metric(a).bySource[source] ?? 0) || rank(metric(a), metric(b)))[0];
    if (best && !selected.includes(best) && selected.length < maxSize) selected.push(best);
  }
  for (const genome of frontier) if (!selected.includes(genome) && selected.length < maxSize) selected.push(genome);
  return selected;
}
export function pairedGate(baseline: Evaluation[], candidate: Evaluation[], options: {
  seed: string; minTasks: number; minGain: number; maxSourceRegression: number; confidence: number;
  mode?: 'accuracy' | 'efficiency'; minTokenReduction?: number;
}) {
  const byTask = (items: Evaluation[]) => {
    const values = new Map<string, Map<number, number>>();
    for (const evaluation of items) for (const trial of evaluation.trials) {
      const key = trial.task_key ?? trial.task_id, seeds = values.get(key) ?? new Map<number, number>();
      if (seeds.has(evaluation.seed)) throw new Error('Duplicate task/seed in promotion evidence');
      seeds.set(evaluation.seed, Number(strictScore(trial).resolved)); values.set(key, seeds);
    }
    return values;
  };
  const left = byTask(baseline), right = byTask(candidate), keys = [...left.keys()].sort();
  if (keys.length !== right.size || keys.some(key => !right.has(key))) throw new Error('Promotion requires paired task identities');
  const deltas = keys.map(key => {
    const a = left.get(key)!, b = right.get(key)!;
    if (a.size !== b.size || [...a.keys()].some(seed => !b.has(seed))) throw new Error('Promotion requires paired repeat seeds');
    return [...a].reduce((sum, [seed, value]) => sum + b.get(seed)! - value, 0) / a.size;
  });
  const delta = deltas.reduce((s, d) => s + d, 0) / (deltas.length || 1);
  const rng = random(options.seed), bootstrap = Array.from({length: 2000}, () =>
    deltas.reduce(sum => sum + deltas[Math.floor(rng() * deltas.length)]!, 0) / (deltas.length || 1)).sort((a, b) => a - b);
  const lower = bootstrap[Math.floor((1 - options.confidence) / 2 * bootstrap.length)] ?? -1;
  const base = fitness(baseline), next = fitness(candidate);
  const regressions = Object.keys(base.bySource).filter(source => (next.bySource[source] ?? 0) < base.bySource[source]! - options.maxSourceRegression);
  const accuracyAccepted = keys.length >= options.minTasks && delta > options.minGain && lower > 0 && !regressions.length
    && next.valid >= base.valid && next.errors <= base.errors;
  const tokenReduction = base.tokens > 0 ? 1 - next.tokens / base.tokens : 0;
  const basePairs = new Map(baseline.flatMap(evaluation => evaluation.trials.map(trial =>
    [`${evaluation.seed}:${trial.task_key ?? trial.task_id}`, strictScore(trial)] as const)));
  // Cost promotion is opt-in. Every paired trial must preserve ALL scoring dimensions,
  // not just the aggregate strict score; unknown spending cannot demonstrate savings.
  const pairRegressions = candidate.flatMap(evaluation => evaluation.trials.filter(trial => {
    const left = basePairs.get(`${evaluation.seed}:${trial.task_key ?? trial.task_id}`)!, right = strictScore(trial);
    return Number(right.resolved) < Number(left.resolved) || right.validAcceptance < left.validAcceptance
      || right.invalidRejection < left.invalidRejection || Number(right.baselineDetected) < Number(left.baselineDetected)
      || right.patchErrors > left.patchErrors || !right.schemaOK;
  }).map(trial => `${evaluation.seed}:${trial.task_key ?? trial.task_id}`));
  const efficiencyAccepted = options.mode === 'efficiency' && keys.length >= options.minTasks && !pairRegressions.length
    && baseline.length > 0 && candidate.length > 0 && [...baseline, ...candidate].every(evaluation => !evaluation.usage.estimated)
    && tokenReduction >= (options.minTokenReduction ?? .15);
  const accepted = accuracyAccepted || efficiencyAccepted;
  return {accepted, pairedTasks: keys.length, delta, lowerConfidenceBound: lower, confidence: options.confidence,
    sourceRegressions: regressions, baseline: base, candidate: next, tokenReduction, pairRegressions,
    reason: accuracyAccepted ? 'accuracy_gain' : efficiencyAccepted ? 'paired_quality_preserved_token_reduction' : 'gate_rejected'};
}
