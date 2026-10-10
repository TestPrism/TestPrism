import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { inventory, unavailableAgentImports } from '../src/benchmark.js';
import { strictScore, summarize, trialSchema } from '../src/report.js';
import { fixture } from './helpers.js';
import { atomicJSON } from '../src/util.js';

test('inventory follows exactly the supplied IDs, explicitly reports missing tasks, and excludes alternate datasets', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const bench = path.join(f.root, 'bench'), selected = path.join(bench, 'verified_tasks/test-hardest-500');
  await atomicJSON(path.join(selected, 'manifest.json'), {total_selected: 2, sources: {'from-x': {task_ids: ['one', 'two'], selected_count: 2}}});
  const flat = path.join(bench, 'verified_tasks_flat_test/from-x__one'); await fs.mkdir(flat, {recursive: true});
  await fs.writeFile(path.join(flat, 'task.yaml'), 'instruction: Repair public behavior\nworkdir: /app\n');
  await fs.writeFile(path.join(flat, 'Dockerfile'), 'FROM scratch\n');
  const wrong = path.join(bench, 'verified_tasks/hardest-500-patch-oriented/from-x/two'); await fs.mkdir(wrong, {recursive: true});
  await fs.writeFile(path.join(wrong, 'task.yaml'), 'instruction: A changed task\n');
  const result = await inventory(bench);
  assert.equal(result.expected, 2); assert.equal(result.available, 1); assert.equal(result.missing, 1);
  assert.equal(result.flatFallback, 1); assert.equal(result.officialCLIAvailable, false);
  const perfect = trialSchema.parse({task_id: 'one', baseline_detected: true, is_resolved: true,
    patch_results: Array.from({length: 10}, (_, i) => ({patch_id: `p${i}`, label: i < 5 ? 'oracle_valid' : 'oracle_invalid',
      observed_status: i < 5 ? 'passed' : 'failed', failure_mode: 'none'}))});
  assert.equal(strictScore(perfect).resolved, true);
  const report = summarize([perfect], result); assert.equal(report.strictAccuracy, 0.5); assert.equal(report.complete, false);
  assert.equal(report.missingTokenAccounting, 1);
  await assert.rejects(async () => summarize([perfect, perfect], result), /Duplicate trial/);
  const incomplete = {...perfect, patch_results: perfect.patch_results.slice(0, 9)};
  assert.equal(strictScore(incomplete).resolved, false);
  const invalidTimeout = structuredClone(perfect); invalidTimeout.patch_results[9]!.failure_mode = 'test_timeout';
  assert.equal(strictScore(invalidTimeout).resolved, false); assert.equal(strictScore(invalidTimeout).invalidRejection, 0.8);
  const overconstrained = structuredClone(perfect); overconstrained.patch_results[0]!.observed_status = 'failed';
  assert.equal(strictScore(overconstrained).resolved, false);
  assert.equal(strictScore({...perfect, baseline_detected: false}).resolved, false);
});

test('readiness reports every absent factory import and recognizes module and package layouts', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const root = path.join(f.root, 'bench'), agents = path.join(root, 'testprism/agents');
  await fs.mkdir(path.join(agents, 'package_agent'), {recursive: true});
  await fs.writeFile(path.join(agents, 'base_agent.py'), 'class BaseAgent: pass\n');
  await fs.writeFile(path.join(agents, 'package_agent/__init__.py'), '');
  await fs.writeFile(path.join(agents, 'agent_factory.py'),
    'from testprism.agents.base_agent import BaseAgent\nfrom testprism.agents.package_agent import A\nfrom testprism.agents.missing_one import A\nfrom testprism.agents.missing_two import (\n B,\n)\n');
  assert.deepEqual(await unavailableAgentImports(root), ['testprism.agents.missing_one', 'testprism.agents.missing_two']);
});
