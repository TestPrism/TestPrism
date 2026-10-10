import { promises as fs } from 'node:fs';
import path from 'node:path';
import { z } from 'zod';
import { parse as parseYAML } from 'yaml';
import { Budget, BudgetExceeded, type Reservation } from './budget.js';
import { loadConfig, loadDocument } from './config.js';
import { SDKModel } from './model.js';
import { applyGenome, child, genesis, policyGenes, random, type Gene, type Genome } from './genome.js';
import { fitness, pairedGate, pareto, rank, validateEvaluation, type Evaluation, type Sample } from './evolution-metrics.js';
import { InterruptedEvaluation, officialEvaluator, type Evaluator } from './evolution-evaluator.js';
import { atomicJSON, errorText, hash, implementationDigest, readJSON, relativePath, requestSize } from './util.js';
import { configSchema, type Config, type Model } from './types.js';
import { Store } from './store.js';
import { withRunLock } from './lock.js';
import { skillContext } from './skill-context.js';
import { evolutionArms, selectEvolutionArm, type EvolutionArm } from './evolution-bandit.js';
import {reflectionSystem, lessonReflectionTools, reflectedLesson} from './evolution-reflection.js';
import {lessonComponents, updateLesson} from './lessons.js';
import {reflectionFeedback} from './evolution-feedback.js';
import {replayLessonMutation} from './lesson-replay.js';
import type { ToolSpec } from './types.js';
import {screeningSamples, passesScreen} from './evolution-screen.js';

const planSchema = z.object({
  baseConfig: z.string(), trainManifest: z.string(), validationManifest: z.string(), holdoutManifest: z.string(),
  benchmark: z.object({root: z.string(), dataset: z.string(), patchSets: z.string(), python: z.string().default('python3'), runtimeCopy: z.string().optional()}),
  seed: z.string().default('testhelix-evolve-v1'), population: z.number().int().min(2).max(12).default(4),
  generations: z.number().int().min(1).max(20).default(3), offspring: z.number().int().min(1).max(8).default(3),
  reflection: z.enum(['model', 'catalog']).default('model'),
  scheduler: z.enum(['ucb', 'pareto']).default('ucb'),
  mutation: z.strictObject({
    components: z.array(z.enum(policyGenes)).min(1).default(policyGenes),
    lessonComponents: z.array(z.enum(lessonComponents)).min(1).default([...lessonComponents]),
    evidenceGuided: z.boolean().default(false),
    lessonReplay: z.enum(['off', 'report', 'filter']).default('report'),
  }).prefault({}),
  screening: z.object({enabled: z.boolean().default(false), tasks: z.number().int().min(2).max(100).default(8)}).prefault({}),
  budget: z.object({maxCalls: z.number().int().positive(), maxInputTokens: z.number().int().positive(),
    maxOutputTokens: z.number().int().positive(), wallMs: z.number().int().positive(), maxCostUSD: z.number().positive().optional(),
    maxEvaluations: z.number().int().positive()}),
  promotion: z.object({repeats: z.number().int().min(1).max(5).default(2), minTasks: z.number().int().min(2).default(20),
    mode: z.enum(['accuracy', 'efficiency']).default('accuracy'),
    minTokenReduction: z.number().min(.01).max(.9).default(.15),
    minGain: z.number().min(0).max(1).default(0), maxSourceRegression: z.number().min(0).max(1).default(0),
    confidence: z.number().min(.8).max(.999).default(.95)}).prefault({}),
});
export type EvolutionPlan = z.infer<typeof planSchema>;
function eligibleArms(plan: EvolutionPlan, base: Config, parent: Genome, samples: Sample[]) {
  const arms = evolutionArms(parent, base.search.skills, samples, plan.mutation)
    .filter(arm => base.planner.enabled || arm.scope !== 'planning');
  if (!arms.length) throw new Error('Mutation focus has no eligible policy components for this configuration and training set');
  return arms;
}
function assertMutationFocus(plan: EvolutionPlan, base: Config, parent: Genome, proposal: Genome) {
  if (!plan.mutation.components.includes(proposal.mutation!.component)) throw new Error('Mutation is outside the configured component focus');
  if (proposal.mutation!.component !== 'lessons') return;
  const before = parent.genes.lessons, after = proposal.genes.lessons;
  const changed = [...new Set([...before, ...after].map(item => item.id))].filter(id =>
    hash(before.find(item => item.id === id) ?? null) !== hash(after.find(item => item.id === id) ?? null));
  for (const item of [...before, ...after].filter(item => changed.includes(item.id))) {
    const component = item.component ?? 'generation';
    if (!plan.mutation.lessonComponents.includes(component) || component === 'planning' && !base.planner.enabled)
      throw new Error('Lesson mutation is outside the configured component focus');
  }
}
type Work = {calls: number; input: number; output: number};
const workFor = (base: Config, tasks: number): Work => ({calls: base.budget.maxCalls * tasks,
  input: base.budget.maxInputTokens * tasks, output: base.budget.maxOutputTokens * tasks});
const addWork = (...items: Work[]): Work => items.reduce((a, b) => ({calls: a.calls + b.calls, input: a.input + b.input, output: a.output + b.output}), {calls: 0, input: 0, output: 0});
class SearchBudgetProtected extends BudgetExceeded {}
export function evolutionCapacity(plan: EvolutionPlan, base: Config, trainTasks: number, validationTasks: number) {
  const gateEvaluations = 2 * plan.promotion.repeats;
  const gate = workFor(base, validationTasks * gateEvaluations);
  const screenTasks = plan.screening.enabled && plan.screening.tasks < trainTasks ? plan.screening.tasks * 2 : 0;
  const oneComparison = workFor(base, 2 * trainTasks + screenTasks + validationTasks * gateEvaluations);
  const planned = workFor(base, (1 + plan.generations * plan.offspring) * trainTasks + screenTasks * plan.generations * plan.offspring + validationTasks * gateEvaluations);
  const fits = (work: Work) => work.calls <= plan.budget.maxCalls && work.input <= plan.budget.maxInputTokens && work.output <= plan.budget.maxOutputTokens
    && (plan.budget.maxCostUSD === undefined || (work.input * base.model.inputPricePerMillion + work.output * base.model.outputPricePerMillion) / 1e6 <= plan.budget.maxCostUSD);
  return {trainTasks, validationTasks, perTaskCaps: workFor(base, 1), gateEvaluations, protectedValidation: gate,
    oneComparisonCaps: oneComparison, plannedRolloutCaps: planned,
    fitsOneComparisonCaps: fits(oneComparison) && plan.budget.maxEvaluations >= gateEvaluations + 2 + (screenTasks ? 2 : 0),
    fitsPlannedRolloutCaps: fits(planned) && plan.budget.maxEvaluations >= gateEvaluations + 1 + plan.generations * plan.offspring * (screenTasks ? 3 : 1),
    caveats: ['Caps are conservative reservations, not predicted usage. Reflection and interrupted attempts require additional headroom.',
      'Wall time depends on containers and harness execution; token headroom does not guarantee completion.',
      '20 validation tasks is a pilot floor, not proof of generalization over hardest500 or statistical power for small gains.']};
}
export async function inspectEvolutionPlan(file: string): Promise<ReturnType<typeof evolutionCapacity>> {
  file = path.resolve(file); const plan = planSchema.parse(await loadDocument(file));
  const resolve = (name: string) => path.resolve(path.dirname(file), name);
  const base = await loadConfig(resolve(plan.baseConfig));
  const train = await split(resolve(plan.trainManifest)), validation = await split(resolve(plan.validationManifest)), holdout = await split(resolve(plan.holdoutManifest));
  assertDisjoint(train.samples, validation.samples, holdout.samples);
  if (!base.search.review) throw new Error('Evolution requires semantic review');
  if (validation.samples.length < plan.promotion.minTasks) throw new Error('Not enough independent validation tasks for the configured promotion gate');
  eligibleArms(plan, base, genesis(base), train.samples);
  return evolutionCapacity(plan, base, train.samples.length, validation.samples.length);
}
interface Split { samples: Sample[]; digest: string }
interface State {
  identity: string; genomes: Genome[]; evaluations: Evaluation[]; attempts: number; evaluationAttempts: number;
  phase: 'search' | 'validation' | 'done'; challenger?: string; summary?: Record<string, unknown>;
  armAttempts?: Record<string, number>;
  screens?: Evaluation[];
  replays?: Record<string, ReturnType<typeof replayLessonMutation>>;
  pending?: {key: string; genome: string; split: 'train' | 'validation'; seed: number; reservation: Reservation; screenKeys?: string[]};
}
export interface Release {
  schemaVersion: 1; id: string; genome: Genome; config: Config; implementationDigest: string;
  baseConfigDigest: string; evaluatorDigest: string; splits: Record<string, string>;
  trainingEvidence: string[]; validationEvidence: string[]; status: 'baseline' | 'promoted'; rollback?: string;
  comparison?: ReturnType<typeof pairedGate>;
  evidenceKind: 'official' | 'injected';
}
async function split(file: string): Promise<Split> {
  const raw = await fs.readFile(file, 'utf8'), value = JSON.parse(raw);
  const samples: Sample[] = Object.entries(value.sources as Record<string, {task_ids: string[]}>).flatMap(([source, data]) =>
    data.task_ids.map(id => ({key: relativePath(`${source}/${id}`), source})));
  if (!samples.length || samples.length !== value.total_selected || new Set(samples.map(s => s.key)).size !== samples.length)
    throw new Error(`Invalid split manifest: ${file}`);
  return {samples, digest: hash(raw)};
}
export function assertDisjoint(train: Sample[], validation: Sample[], holdout: Sample[]): void {
  const seen = new Set<string>();
  for (const [name, samples] of [['train', train], ['validation', validation], ['holdout', holdout]] as const)
    for (const sample of samples) {
      if (seen.has(sample.key)) throw new Error(`${name} overlaps another split: ${sample.key}`);
      seen.add(sample.key);
    }
}
function catalogMutation(parent: Genome, evidence: Evaluation[], attempt: number, forbidden: string[], arm?: EvolutionArm): Genome {
  const metrics = fitness(evidence), genes = structuredClone(parent.genes);
  let component: Gene;
  const rule = metrics.valid < metrics.invalid
    ? 'Audit every assertion against explicit requirements. Test multiple valid representations and avoid requiring one particular repair or undocumented exception behavior.'
    : 'Cover combinations of documented edge cases and state transitions. Prefer deterministic exhaustive small inputs or an independent oracle to a single regression example.';
  const selected = arm?.component ?? ['guidance', 'lessons', 'agentSteps', 'contextTokens', 'toolOutputChars'][attempt % 5];
  if (selected === 'skillRoutes') {
    component = 'skillRoutes'; genes.skillRoutes = [...genes.skillRoutes.filter(route => route.context !== arm!.context),
      {context: arm!.context!, skill: arm!.skill!}].sort((a, b) => a.context.localeCompare(b.context));
  } else if (selected === 'guidance') { component = 'guidance'; genes.guidance = rule; }
  else if (selected === 'lessons') {
    component = 'lessons';
    const scope = arm?.scope ?? 'generation';
    const update = updateLesson(genes.lessons, {operation: 'upsert', id: `${scope}-contract-and-controls`, component: scope,
      triggers: ['boundary', 'representation', 'serialization', 'state transition'],
      when: `A ${scope} decision depends on a public contract boundary.`, action: rule,
      verify: 'Check the claimed condition against a public requirement and an independent observable control.',
      rationale: 'Target repeated development execution failures within one component.'});
    genes.lessons = update.lessons;
  } else if (selected === 'contextTokens') {
    component = 'contextTokens'; genes.contextTokens = Math.max(2000, Math.floor(genes.contextTokens * .8));
  } else if (selected === 'toolOutputChars') {
    component = 'toolOutputChars'; genes.toolOutputChars = Math.max(512, Math.floor(genes.toolOutputChars * .75));
  } else if (selected === 'initialCandidates') {
    component = selected; genes.initialCandidates = genes.initialCandidates > 1 ? genes.initialCandidates - 1 : Math.min(2, genes.maxCandidates);
  } else if (selected === 'maxCandidates') {
    component = selected; genes.maxCandidates = genes.maxCandidates < 4 ? genes.maxCandidates + 1 : Math.max(genes.initialCandidates, 3);
  } else if (selected === 'repairSteps') {
    component = selected; genes.repairSteps = genes.repairSteps < 8 ? genes.repairSteps + 1 : genes.repairSteps - 1;
  } else if (selected === 'maxRounds') {
    component = selected; genes.maxRounds = genes.maxRounds < 3 ? genes.maxRounds + 1 : 2;
  } else if (selected === 'reviewMaxOutputTokens') {
    component = selected; genes.reviewMaxOutputTokens = genes.reviewMaxOutputTokens < 8192 ? Math.min(8192, genes.reviewMaxOutputTokens * 2) : 4096;
  } else {
    component = 'agentSteps'; genes.agentSteps = genes.agentSteps < 18 ? genes.agentSteps + 2 : genes.agentSteps - 2;
  }
  return child(parent, genes, {component, rationale: 'Target the observed acceptance/rejection imbalance or artifact completion budget',
    evidence: evidence.map(e => e.evidenceDigest), operator: 'catalog', ...(arm ? {arm: arm.id} : {})}, forbidden);
}

type EvolveOptions = {resume?: boolean; evaluator?: Evaluator; model?: Model};
export async function runEvolution(planFile: string, outputDir: string, options: EvolveOptions = {}): Promise<Record<string, unknown>> {
  if (!options.evaluator) {
    const plan = planSchema.parse(await loadDocument(planFile));
    const canonical = async (file: string): Promise<string> => {
      try {return await fs.realpath(file);}
      catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
        return path.join(await canonical(path.dirname(file)), path.basename(file));
      }
    };
    const output = await canonical(path.resolve(outputDir));
    for (const input of [plan.benchmark.root, plan.benchmark.dataset, plan.benchmark.patchSets]) {
      const root = await canonical(path.resolve(path.dirname(planFile), input));
      if (output === root || output.startsWith(root + path.sep)) throw new Error('Evolution output must be outside external benchmark inputs');
    }
  }
  return withRunLock(outputDir, () => runEvolutionUnlocked(planFile, outputDir, options));
}
async function runEvolutionUnlocked(planFile: string, outputDir: string, options: EvolveOptions): Promise<Record<string, unknown>> {
  planFile = path.resolve(planFile); outputDir = path.resolve(outputDir);
  const plan = planSchema.parse(await loadDocument(planFile));
  const resolve = (file: string) => path.resolve(path.dirname(planFile), file);
  const base = await loadConfig(resolve(plan.baseConfig));
  if (!base.search.review) throw new Error('Evolution keeps semantic review enabled; it cannot optimize by disabling validation');
  const train = await split(resolve(plan.trainManifest)), validation = await split(resolve(plan.validationManifest)), holdout = await split(resolve(plan.holdoutManifest));
  assertDisjoint(train.samples, validation.samples, holdout.samples);
  if (validation.samples.length < plan.promotion.minTasks) throw new Error('Not enough independent validation tasks for the configured promotion gate');
  const benchmark = {...plan.benchmark, root: resolve(plan.benchmark.root), dataset: resolve(plan.benchmark.dataset), patchSets: resolve(plan.benchmark.patchSets),
    python: plan.benchmark.python.includes('/') ? resolve(plan.benchmark.python) : plan.benchmark.python,
    ...(plan.benchmark.runtimeCopy ? {runtimeCopy: resolve(plan.benchmark.runtimeCopy)} : {})};
  const implementation = await implementationDigest(), frozen: Array<[string, string]> = [];
  if (!options.evaluator) {
    for (const file of ['testprism/harness/testbench_harness.py'])
      frozen.push([file, hash(await fs.readFile(path.join(benchmark.root, file)))]);
    if (benchmark.runtimeCopy) frozen.push(['runtime-audit.json', hash(await fs.readFile(path.join(benchmark.runtimeCopy, 'runtime-audit.json')))]);
    const freezeCode = async (directory: string): Promise<void> => {
      for (const entry of (await fs.readdir(directory, {withFileTypes: true})).sort((a, b) => a.name.localeCompare(b.name))) {
        const file = path.join(directory, entry.name);
        if (entry.isDirectory() && entry.name !== '__pycache__') await freezeCode(file);
        else if (entry.isFile() && entry.name.endsWith('.py')) frozen.push([path.relative(benchmark.root, file), hash(await fs.readFile(file))]);
      }
    };
    await freezeCode(path.join(benchmark.root, 'testprism'));
    const instructionHashes = new Map<string, string>();
    for (const sample of [...train.samples, ...validation.samples]) {
      const task = await fs.readFile(path.join(benchmark.dataset, sample.key, 'task.yaml'));
      const instruction = z.string().min(1).parse(parseYAML(task.toString()).instruction);
      sample.context = skillContext(instruction, sample.source);
      const instructionHash = hash(instruction.replaceAll('\r\n', '\n').trim());
      if (instructionHashes.has(instructionHash)) throw new Error(`Duplicate public instructions across evolution samples: ${sample.key}`);
      instructionHashes.set(instructionHash, sample.key); frozen.push([sample.key + '/task.yaml', hash(task)]);
      const manifest = await fs.readFile(path.join(benchmark.patchSets, sample.key, 'manifest.json'), 'utf8');
      const patchSet = JSON.parse(manifest);
      if (patchSet.schema_version !== 3 || patchSet.status !== 'ready' || patchSet.task_key !== sample.key)
        throw new Error(`Evolution requires an identity-matched ready schema-v3 patch set: ${sample.key}`);
      frozen.push([sample.key + '/patch-set-manifest.json', hash(manifest)]);
    }
  }
  const evaluatorDigest = hash({implementation, frozen, evaluator: options.evaluator ? 'injected-evaluator' : 'official-harness'});
  const splits = {train: train.digest, validation: validation.digest, holdout: holdout.digest};
  const identity = hash({plan, base, splits, evaluatorDigest});
  const store = new Store(outputDir); await store.init();
  const stateFile = path.join(outputDir, 'evolution.json'), existing = await readJSON<State>(stateFile);
  if (existing && !options.resume) throw new Error('Evolution run exists; use --resume or a fresh directory');
  if (existing && existing.identity !== identity) throw new Error('Evolution configuration, split, evaluator or implementation changed');
  const baseline = genesis(base);
  eligibleArms(plan, base, baseline, train.samples);
  const state: State = existing ?? {identity, genomes: [baseline], evaluations: [], attempts: 0, evaluationAttempts: 0, phase: 'search'};
  if (state.phase === 'done') return state.summary!;
  const globalConfig = configSchema.parse({...base, budget: {...plan.budget, concurrency: 1}});
  const budget = await Budget.open(globalConfig, path.join(outputDir, 'budget.json'), identity, !!options.resume);
  const evaluate = options.evaluator ?? officialEvaluator;
  const forbidden = [...train.samples, ...validation.samples, ...holdout.samples].map(sample => sample.key);
  const save = () => atomicJSON(stateFile, state);
  await store.save('capacity.json', evolutionCapacity(plan, base, train.samples.length, validation.samples.length));
  const canSearch = (extra: Work, evaluations: number): boolean => {
    const required = addWork(budget.snapshot(), extra, workFor(base, validation.samples.length * 2 * plan.promotion.repeats));
    return required.calls <= plan.budget.maxCalls && required.input <= plan.budget.maxInputTokens && required.output <= plan.budget.maxOutputTokens
      && state.evaluationAttempts + evaluations + 2 * plan.promotion.repeats <= plan.budget.maxEvaluations
      && (plan.budget.maxCostUSD === undefined || (required.input * base.model.inputPricePerMillion + required.output * base.model.outputPricePerMillion) / 1e6 <= plan.budget.maxCostUSD);
  };
  await save();
  const runEvaluation = async (genome: Genome, which: 'train' | 'validation', seed: number, screen?: Sample[]): Promise<Evaluation> => {
    const samples = screen ?? (which === 'train' ? train.samples : validation.samples);
    const key = hash({identity, genome: genome.id, split: which, seed, ...(screen ? {screenKeys: screen.map(s => s.key)} : {})});
    const collection = screen ? (state.screens ??= []) : state.evaluations;
    const previous = collection.find(e => e.genomeId === genome.id && e.split === which && e.seed === seed
      && JSON.stringify(e.taskKeys) === JSON.stringify(samples.map(s => s.key)));
    if (previous) return previous;
    if (state.pending && state.pending.key !== key) throw new Error('Resume the pending evaluation before starting another');
    if (!state.pending) {
      if (which === 'train' && !canSearch(workFor(base, samples.length), 1))
        throw new BudgetExceeded('Insufficient budget for training while protecting the independent validation gate; inspect capacity.json');
      if (state.evaluationAttempts >= plan.budget.maxEvaluations) throw new BudgetExceeded('Evolution evaluation limit reached');
      const reservation = await budget.reserveWork({calls: base.budget.maxCalls * samples.length,
        input: base.budget.maxInputTokens * samples.length, output: base.budget.maxOutputTokens * samples.length});
      state.pending = {key, genome: genome.id, split: which, seed, reservation, ...(screen ? {screenKeys: screen.map(s => s.key)} : {})}; state.evaluationAttempts++; await save();
    }
    const pending = state.pending, file = path.join(outputDir, 'evaluations', `${key}.json`);
    let result = await readJSON<Evaluation>(file);
    try {
      if (!result) {
        process.stderr.write(`[evolve] ${which} ${genome.id} repeat=${seed}, tasks=${samples.length}\n`);
        result = await evaluate({genome, config: applyGenome(base, genome), split: which, samples, seed,
          directory: path.join(outputDir, 'evaluation-work', key, pending.reservation.id), timeoutMs: Math.max(1, budget.remainingMs), benchmark});
      }
      if (result.genomeId !== genome.id || result.split !== which || result.seed !== seed) throw new Error('Evaluator identity mismatch');
      result = validateEvaluation(result, samples.map(sample => sample.key));
      await atomicJSON(file, result); // Seal evidence before idempotent settlement.
      await budget.settle(pending.reservation, result.usage);
      collection.push(result); delete state.pending; await save(); return result;
    } catch (error) {
      await budget.settle(pending.reservation); delete state.pending; await save();
      if (error instanceof InterruptedEvaluation) {
        await store.event('evolution.interrupted_evaluation', {genome: genome.id, retainedReservation: pending.reservation.id});
        return runEvaluation(genome, which, seed, screen);
      }
      throw error;
    }
  };
  const replayCandidate = async (genome: Genome): Promise<boolean> => {
    if (plan.mutation.lessonReplay === 'off' || genome.mutation?.component !== 'lessons') return true;
    const parent = state.genomes.find(item => item.id === genome.parents[0])!;
    if (!state.replays?.[genome.id]) {
      const evidence = state.evaluations.filter(item => item.split === 'train' && item.genomeId === parent.id);
      const replay = replayLessonMutation(parent, genome, evidence);
      state.replays ??= {}; state.replays[genome.id] = replay;
      await store.save(`lesson-replays/${genome.id}.json`, {parent: parent.id, candidate: genome.id,
        evidence: evidence.map(item => item.evidenceDigest), ...replay});
      await store.event('evolution.lesson_replayed', {genome: genome.id, mode: plan.mutation.lessonReplay, ...replay});
      await save();
    }
    const replay = state.replays[genome.id]!;
    return plan.mutation.lessonReplay !== 'filter' || replay.status !== 'no_observed_intervention' || replay.tasks < 2;
  };
  const screenCandidate = async (genome: Genome): Promise<boolean> => {
    if (!plan.screening.enabled || plan.screening.tasks >= train.samples.length || !genome.parents.length) return true;
    const samples = screeningSamples(train.samples, plan.screening.tasks, `${plan.seed}:${genome.id}`);
    const parent = state.genomes.find(g => g.id === genome.parents[0])!;
    const left = await runEvaluation(parent, 'train', 0, samples), right = await runEvaluation(genome, 'train', 0, samples);
    const passed = passesScreen(left, right);
    await store.event('evolution.screened', {genome: genome.id, parent: parent.id, passed,
      taskKeys: samples.map(s => s.key), evidence: [left.evidenceDigest, right.evidenceDigest], purpose: 'training triage; never promotion'});
    return passed;
  };
  const propose = async (parent: Genome, attempt: number, arm?: EvolutionArm): Promise<Genome> => {
    const evidence = state.evaluations.filter(e => e.genomeId === parent.id && e.split === 'train');
    if (plan.reflection === 'catalog' || arm?.component === 'skillRoutes') return catalogMutation(parent, evidence, attempt, forbidden, arm);
    const system = reflectionSystem;
    const message = {parent: parent.genes, attempt, aggregate: fitness(evidence),
      sourceMetrics: fitness(evidence).bySource, evidenceIds: evidence.map(e => e.evidenceDigest),
      trainingExecutionFeedback: evidence.flatMap(e => e.feedback ?? []).slice(0, 8),
      structuredExecutionFeedback: reflectionFeedback(evidence, arm?.scope),
      ...(arm?.scope ? {targetComponent: arm.scope, instruction: 'Change one reusable method belonging to this component; preserve all other components.'} : {})};
    const components = arm ? [arm.component] : (Object.keys(parent.genes) as Gene[]).filter(key => key !== 'skillRoutes');
    const mutationSchema = z.strictObject({component: z.enum(components as [Gene, ...Gene[]]), value: z.unknown(), rationale: z.string().min(1).max(2000)});
    const toolName = arm?.component === 'lessons' ? 'submit_lesson' : 'submit_mutation';
    const tools: Record<string, ToolSpec> = toolName === 'submit_lesson'
      ? lessonReflectionTools()
      : {submit_mutation: {description: 'Propose one reusable policy change grounded in training outcomes', parse: (value: unknown) => mutationSchema.parse(value), schema: {
      type: 'object', properties: {component: {type: 'string', enum: components}, value: {anyOf: [{type: 'string'}, {type: 'integer'},
        {type: 'array', items: {type: 'object', properties: {id: {type: 'string'}, triggers: {type: 'array', items: {type: 'string'}}, text: {type: 'string'}}, required: ['id', 'triggers', 'text'], additionalProperties: false}}]}, rationale: {type: 'string'}},
      required: ['component', 'value', 'rationale'], additionalProperties: false}}};
    const messages = [{role: 'user' as const, content: JSON.stringify(message)}];
    const reflection = {input: requestSize({system, messages, tools}).bytes + 64, output: Math.min(2048, base.model.maxOutputTokens), calls: 1};
    if (!canSearch(addWork(reflection, workFor(base, train.samples.length)), 1))
      throw new SearchBudgetProtected('Stop reflection to preserve a training comparison and the independent validation gate');
    const reservation = await budget.reserve(reflection.input, reflection.output);
    try {
      const reply = await (options.model ?? new SDKModel(base)).complete({system, messages, tools, forceTool: toolName,
        maxOutputTokens: reservation.output, signal: AbortSignal.timeout(Math.max(1, Math.min(base.model.timeoutMs, budget.remainingMs)))});
      await budget.settle(reservation, reply.usage.estimated ? undefined : reply.usage);
      const call = reply.calls.find(c => c.name === toolName && !c.invalid);
      if (toolName === 'submit_lesson') {
        return reflectedLesson(parent, call?.input, evidence.map(e => e.evidenceDigest), forbidden, arm);
      }
      const value = mutationSchema.parse(call?.input);
      return child(parent, {...parent.genes, [value.component]: value.value}, {component: value.component,
        rationale: value.rationale, evidence: evidence.map(e => e.evidenceDigest), operator: 'reflection', ...(arm ? {arm: arm.id} : {})}, forbidden);
    } catch (error) { await budget.settle(reservation); throw error; }
  };
  try {
    if (state.pending) await runEvaluation(state.genomes.find(g => g.id === state.pending!.genome)!, state.pending.split, state.pending.seed,
      state.pending.screenKeys?.map(key => train.samples.find(sample => sample.key === key)!));
    if (state.phase === 'search') {
      for (const genome of [...state.genomes]) if (await replayCandidate(genome) && await screenCandidate(genome)) await runEvaluation(genome, 'train', 0);
      while (state.attempts < plan.generations * plan.offspring) {
        budget.assertAvailable();
        const screening = plan.screening.enabled && plan.screening.tasks < train.samples.length;
        if (!canSearch(workFor(base, train.samples.length + (screening ? 2 * plan.screening.tasks : 0)), screening ? 3 : 1)) {
          await store.event('evolution.search_stopped', {reason: 'Protected independent validation budget'}); break;
        }
        const attempt = state.attempts++, archive = pareto(state.genomes, state.evaluations, plan.population);
        const rng = random(`${plan.seed}:${attempt}`), parent = archive[Math.floor(rng() * archive.length)]!;
        const arms = eligibleArms(plan, base, parent, train.samples);
        const parentEvidence = state.evaluations.filter(item => item.genomeId === parent.id && item.split === 'train');
        const priorities = plan.mutation.evidenceGuided ? reflectionFeedback(parentEvidence).componentPriorities : {};
        const choice = plan.scheduler === 'ucb' ? selectEvolutionArm(arms,
          state.armAttempts ?? {}, state.genomes, state.evaluations, train.samples, priorities) : undefined;
        const arm = choice?.arm ?? arms[Math.floor(rng() * arms.length)]!;
        if (choice) {
          state.armAttempts ??= {}; state.armAttempts[choice.arm.id] = (state.armAttempts[choice.arm.id] ?? 0) + 1;
          await store.event('evolution.arm_selected', {attempt, parent: parent.id, ...choice,
            attribution: 'training-only preload-policy or mutation experiment; no individual skill causal claim'});
        }
        await save();
        let proposal: Genome;
        try {
          if (!choice && attempt % 3 === 2 && archive.length > 1
            && plan.mutation.components.some(key => hash(parent.genes[key]) !== hash(archive.filter(g => g.id !== parent.id)[0]!.genes[key]))) {
            const donor = archive.filter(g => g.id !== parent.id)[0]!;
            const keys = plan.mutation.components.filter(key => hash(parent.genes[key]) !== hash(donor.genes[key]));
            const component = keys[Math.floor(rng() * keys.length)]!;
            proposal = child(parent, {...parent.genes, [component]: donor.genes[component]}, {component,
              rationale: 'Combine one policy component from a complementary training Pareto candidate', operator: 'crossover',
              evidence: state.evaluations.filter(e => e.split === 'train' && [parent.id, donor.id].includes(e.genomeId)).map(e => e.evidenceDigest)}, forbidden, donor);
          } else proposal = await propose(parent, attempt, arm);
          assertMutationFocus(plan, base, parent, proposal);
        } catch (error) {
          if (error instanceof SearchBudgetProtected) {
            await store.event('evolution.search_stopped', {reason: error.message}); break;
          }
          if (error instanceof BudgetExceeded) throw error;
          await store.event('evolution.mutation_rejected', {attempt, parent: parent.id, error: errorText(error)}); continue;
        }
        if (state.genomes.some(g => g.id === proposal.id)) { await store.event('evolution.duplicate', {attempt, genome: proposal.id}); continue; }
        applyGenome(base, proposal); // Validate routes against the frozen enabled library before any rollout.
        state.genomes.push(proposal); await store.save(`genomes/${proposal.id}.json`, proposal); await save();
        if (!await replayCandidate(proposal) || !await screenCandidate(proposal)) continue;
        const result = await runEvaluation(proposal, 'train', 0);
        await store.event('evolution.credit', {genome: proposal.id, parent: parent.id, component: proposal.mutation!.component,
          parentMetrics: fitness(state.evaluations.filter(e => e.genomeId === parent.id && e.split === 'train')), childMetrics: fitness([result]),
          attribution: proposal.mutation!.operator === 'crossover' ? 'interaction not isolated' : 'single policy component comparison; stochastic confounding remains'});
      }
      const archive = pareto(state.genomes, state.evaluations, plan.population);
      archive.sort((a, b) => rank(fitness(state.evaluations.filter(e => e.genomeId === a.id && e.split === 'train')),
        fitness(state.evaluations.filter(e => e.genomeId === b.id && e.split === 'train'))) || a.id.localeCompare(b.id));
      state.challenger = archive[0]!.id; state.phase = 'validation'; await save();
    }
    const challenger = state.genomes.find(g => g.id === state.challenger)!;
    // The held-out development gate is used once, after search is frozen. A failed
    // challenger never sends validation feedback back into the mutation loop.
    for (let seed = 0; seed < plan.promotion.repeats; seed++) {
      const pair = seed % 2 ? [challenger, baseline] : [baseline, challenger];
      for (const genome of pair) await runEvaluation(genome, 'validation', seed);
    }
    const baseValidation = state.evaluations.filter(e => e.split === 'validation' && e.genomeId === baseline.id);
    const nextValidation = state.evaluations.filter(e => e.split === 'validation' && e.genomeId === challenger.id);
    const comparison = pairedGate(baseValidation, nextValidation, {...plan.promotion, seed: plan.seed});
    const release = async (genome: Genome, status: Release['status'], rollback?: string): Promise<Release> => {
      const payload = {schemaVersion: 1 as const, genome, config: applyGenome(base, genome), implementationDigest: implementation,
        evidenceKind: options.evaluator ? 'injected' as const : 'official' as const,
        baseConfigDigest: hash(base), evaluatorDigest, splits, status, ...(rollback ? {rollback} : {}),
        trainingEvidence: state.evaluations.filter(e => e.genomeId === genome.id && e.split === 'train').map(e => e.evidenceDigest),
        validationEvidence: state.evaluations.filter(e => e.genomeId === genome.id && e.split === 'validation').map(e => e.evidenceDigest), comparison};
      const value = {...payload, id: `release-${hash(payload)}`}; await store.save(`releases/${value.id}.json`, value); return value;
    };
    const baseRelease = await release(baseline, 'baseline');
    const active = comparison.accepted && challenger.id !== baseline.id ? await release(challenger, 'promoted', baseRelease.id) : baseRelease;
    await store.save('active.json', {schemaVersion: 1, release: active.id});
    state.summary = {status: 'completed', promoted: active.status === 'promoted', activeRelease: active.id,
      activeConfig: path.join(outputDir, 'active.json'), comparison, proposals: state.attempts,
      evaluated: state.evaluationAttempts, genomes: state.genomes.length, usage: budget.snapshot()};
    state.phase = 'done'; await save(); await store.save('summary.json', state.summary); return state.summary;
  } catch (error) {
    await save();
    if (!(error instanceof BudgetExceeded)) throw error;
    const result = {status: 'budget_exhausted', reason: error.message, phase: state.phase,
      proposals: state.attempts, evaluated: state.evaluationAttempts, usage: budget.snapshot(), promoted: false};
    await store.save('summary.json', result); return result;
  }
}

export async function loadRelease(file: string, options: {allowInjected?: boolean} = {}): Promise<Release> {
  file = path.resolve(file);
  const pointer = await readJSON<Release | {release: string}>(file);
  if (!pointer) throw new Error('Release file is missing');
  const raw = 'release' in pointer
    ? await readJSON<Release>(path.join(path.dirname(file), 'releases', `${/^release-[a-f0-9]{64}$/.test(pointer.release) ? pointer.release : (() => { throw new Error('Invalid release ID'); })()}.json`)) : pointer;
  if (!raw) throw new Error('Release artifact is missing');
  const {id, ...payload} = raw;
  if (id !== `release-${hash(payload)}`) throw new Error('Release content digest mismatch');
  if (raw.evidenceKind !== 'official' && !options.allowInjected) throw new Error('A synthetic/injected-evaluator release cannot be deployed through the production CLI');
  if (raw.implementationDigest !== await implementationDigest()) throw new Error('Release belongs to a different agent implementation; revalidate before activation');
  configSchema.parse(raw.config); return raw;
}
export async function rollbackEvolution(outputDir: string, options: {allowInjected?: boolean} = {}): Promise<{release: string}> {
  return withRunLock(outputDir, () => rollbackUnlocked(outputDir, options));
}
async function rollbackUnlocked(outputDir: string, options: {allowInjected?: boolean}): Promise<{release: string}> {
  const active = path.join(path.resolve(outputDir), 'active.json'), current = await loadRelease(active, options);
  if (!current.rollback) throw new Error('The active release has no rollback parent');
  const parent = await loadRelease(path.join(path.dirname(active), 'releases', `${current.rollback}.json`), options);
  const pointer = {release: parent.id}; await atomicJSON(active, pointer); return pointer;
}
