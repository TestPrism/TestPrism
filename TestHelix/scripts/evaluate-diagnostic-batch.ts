/** Host-only, frozen cohort evaluation and compact failure accounting. Never calls a model. */
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {parseArgs} from 'node:util';
import assert from 'node:assert/strict';
import {atomicJSON, command, hash, mapLimit, readJSON} from '../src/util.js';
import type {RunResult} from '../src/types.js';

async function main() {
  const {values} = parseArgs({options: {batch: {type: 'string'}}});
  assert.ok(values.batch, '--batch required'); const batch = path.resolve(values.batch);
  const cohort = JSON.parse(await fs.readFile(path.join(batch, 'cohort.json'), 'utf8')) as {eligibleForEvolutionValidation: boolean;
    sampleSplit?: 'development' | 'hardest500'; tasks: Array<{id: string; instance: string}>};
  const frozen = JSON.parse(await fs.readFile(path.join(batch, 'generation-freeze.json'), 'utf8'));
  assert.equal(frozen.referenceRead, false);
  assert.equal(cohort.eligibleForEvolutionValidation, false);
  assert.ok(cohort.sampleSplit === undefined || ['development', 'hardest500'].includes(cohort.sampleSplit));
  // Verify every result in the cohort before opening any reference solution.
  const tasks = await Promise.all(cohort.tasks.map(async (task: {id: string; instance: string}) => {
    const directory = path.join(batch, task.instance), file = path.join(directory, 'run/result.json');
    const bytes = await fs.readFile(file).catch(() => undefined);
    assert.equal(bytes ? hash(bytes) : null, frozen.runs.find((r: {taskId: string}) => r.taskId === task.id)?.resultDigest);
    return {...task, directory, run: bytes ? JSON.parse(bytes.toString()) as RunResult : undefined};
  }));
  const rows = await mapLimit(tasks, 2, async task => {
    const run = task.run;
    const events = (await fs.readFile(path.join(task.directory, 'run/events.jsonl'), 'utf8').catch(() => '')).trim()
      .split('\n').filter(Boolean).map(line => JSON.parse(line));
    let evaluation = await readJSON<any>(path.join(task.directory, 'evaluation/evaluation.json'));
    if (evaluation) assert.equal(evaluation.sampleSplit ?? 'development', cohort.sampleSplit ?? 'development', 'Cached evaluation split differs');
    let evaluationError: string | undefined;
    if (run?.candidates.length && !evaluation) {
      const result = await command([process.execPath, '--import', 'tsx', 'scripts/evaluate_model_diagnostic.ts', '--diagnostic', task.directory,
        ...(cohort.sampleSplit === 'hardest500' ? ['--hardest-sample'] : [])],
        {timeoutMs: 1800000, maxBytes: 10000});
      await atomicJSON(path.join(task.directory, 'evaluation-process.json'), result);
      evaluation = await readJSON(path.join(task.directory, 'evaluation/evaluation.json'));
      if (result.code !== 0 || !evaluation) evaluationError = result.stderr.slice(-1500) || 'Incomplete host evaluation';
    }
    const tools = events.filter(e => e.type === 'tool.completed');
    const counts = (items: string[]) => Object.fromEntries([...new Set(items)].map(name => [name, items.filter(item => item === name).length]));
    const successful = events.filter(e => e.type === 'model.completed');
    return {taskId: task.id, status: run?.status ?? 'error', usage: run?.usage, elapsedMs: run?.elapsedMs,
      successfulModelResponses: successful.length, apiErrors: events.filter(e => e.type === 'model.error').map(e => e.data.error),
      toolCounts: counts(tools.map(e => e.data.name)),
      activeSkillCounts: counts(events.filter(e => e.type === 'skills.active').flatMap(e => e.data.ids)),
      candidates: run?.candidates.map(c => ({id: c.id, files: c.tests.map(f => f.path), sourceFiles: c.source.length,
        selected: run.selected.includes(c.id), testBytes: c.tests.reduce((s, f) => s + Buffer.from(f.data, 'base64').length, 0)})),
      cells: run?.matrix.map(c => ({test: c.testId, source: c.sourceId, status: c.status, output: c.output})),
      reviews: run?.reviews, evaluation: evaluation?.results, evaluationError,
      publishedF2P: evaluation?.results.some((r: {labels: string[]; f2p: boolean}) => r.labels.includes('published') && r.f2p) ?? false,
      candidateF2P: evaluation?.results.some((r: {f2p: boolean}) => r.f2p) ?? false};
  });
  const summary = {schemaVersion: 1, generatedAt: new Date().toISOString(), batch, frozenAt: frozen.frozenAt,
    referenceIsSingleSuppliedSolution: true, officialScore: null, eligibleForEvolutionValidation: false, modelCallsDuringEvaluation: 0,
    sampleSplit: cohort.sampleSplit ?? 'development',
    evaluationComplete: rows.every(r => !r.evaluationError),
    tasks: rows.length, publishedF2P: rows.filter(r => r.publishedF2P).length, candidateF2P: rows.filter(r => r.candidateF2P).length,
    usage: rows.reduce((s, r) => ({calls: s.calls + (r.usage?.calls ?? 0), input: s.input + (r.usage?.input ?? 0),
      output: s.output + (r.usage?.output ?? 0), cached: s.cached + (r.usage?.cached ?? 0)}), {calls: 0, input: 0, output: 0, cached: 0}), rows};
  await atomicJSON(path.join(batch, 'diagnostic-summary.json'), summary);
  process.stdout.write(JSON.stringify({tasks: summary.tasks, publishedF2P: summary.publishedF2P, candidateF2P: summary.candidateF2P, usage: summary.usage}) + '\n');
}
main().catch(error => {process.stderr.write(String(error) + '\n'); process.exitCode = 1;});
