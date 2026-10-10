import assert from 'node:assert/strict';
import path from 'node:path';
import { runEvolution, rollbackEvolution, loadRelease } from '../src/evolve.js';
import { atomicJSON, hash } from '../src/util.js';
import { config } from '../tests/helpers.js';
import { trialSchema } from '../src/report.js';

const output = path.resolve(process.argv[2] ?? `.runs/evolve-smoke-${Date.now()}`);
for (const [name, size] of [['train', 4], ['validation', 20], ['holdout', 5]] as const)
  await atomicJSON(path.join(output, `${name}.json`), {total_selected: size,
    sources: {'from-synthetic': {task_ids: Array.from({length: size}, (_, i) => `${name}-${i}`)}}});
await atomicJSON(path.join(output, 'base.json'), config());
await atomicJSON(path.join(output, 'plan.json'), {baseConfig: 'base.json', trainManifest: 'train.json', validationManifest: 'validation.json', holdoutManifest: 'holdout.json',
  benchmark: {root: '.', dataset: '.', patchSets: '.'}, seed: 'synthetic-smoke', population: 2, generations: 1, offspring: 1, reflection: 'catalog',
  budget: {maxCalls: 5000, maxInputTokens: 50000000, maxOutputTokens: 10000000, wallMs: 60000, maxEvaluations: 10}, promotion: {minTasks: 20, repeats: 2}});
const result = await runEvolution(path.join(output, 'plan.json'), path.join(output, 'evolution'), {evaluator: async request => ({
  genomeId: request.genome.id, split: request.split, seed: request.seed, taskKeys: request.samples.map(s => s.key),
  trials: request.samples.map(sample => trialSchema.parse({task_id: sample.key, task_key: sample.key, baseline_detected: true,
    patch_results: Array.from({length: 10}, (_, i) => ({patch_id: `p${i}`, label: i < 5 ? 'oracle_valid' : 'oracle_invalid',
      observed_status: i < 5 && !(i === 0 && request.genome.generation === 0) ? 'passed' : 'failed', failure_mode: 'none'}))})),
  usage: {input: request.samples.length * 100, output: request.samples.length * 50, calls: request.samples.length * 2, cached: 0},
  durationMs: 1, evidenceDigest: hash({id: request.genome.id, split: request.split, seed: request.seed}),
})});
assert.equal(result.promoted, true);
await assert.rejects(loadRelease(path.join(output, 'evolution/active.json')), /synthetic/);
await rollbackEvolution(path.join(output, 'evolution'), {allowInjected: true});
assert.equal((await loadRelease(path.join(output, 'evolution/active.json'), {allowInjected: true})).status, 'baseline');
const evidence = {kind: 'Synthetic evaluator control-flow check; no real-model performance or learning claim',
  mutationEvaluationPromotionPassed: true, syntheticProductionDeploymentBlocked: true, rollbackPassed: true, evaluated: result.evaluated, usage: result.usage};
await atomicJSON(path.join(output, 'smoke-evidence.json'), evidence); process.stdout.write(JSON.stringify(evidence, null, 2) + '\n');
