/** Public-only development cohort. One command prepares, runs and freezes all jobs. */
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {parseArgs} from 'node:util';
import {spawn} from 'node:child_process';
import {parse} from 'yaml';
import {atomicJSON, command, hash, mapLimit} from '../src/util.js';

const selections = [
  ['django__django-11087', 'Framework setup regression; previously used development case'],
  ['pytest-dev__pytest-10356', 'Multiple inheritance and test collection'],
  ['sympy__sympy-13974', 'Symbolic algebra and noncommutative composition'],
  ['scikit-learn__scikit-learn-25973', 'Cross-validation protocol and iterable lifetime'],
] as const;
const freshSelections = [
  ['pytest-dev__pytest-7490', 'Runtime marker state and outcome reporting; first use in development diagnostics'],
  ['scikit-learn__scikit-learn-14087', 'Cross-validation without refitting and solver compatibility; first use'],
  ['sympy__sympy-14531', 'Settings propagation into nested representations; first use'],
] as const;
const environment = {PATH: '/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'};
async function checked(argv: string[], timeoutMs = 120000): Promise<string> {
  const result = await command(argv, {timeoutMs});
  if (result.code !== 0 || result.timedOut) throw new Error(`${argv[0]} failed: ${result.stderr.slice(-1200)}`);
  return result.stdout.trim();
}
async function main() {
  const {values} = parseArgs({options: {out: {type: 'string'}, 'generation-only': {type: 'boolean', default: false}, cohort: {type: 'string', default: 'original'}, tasks: {type: 'string'}, config: {type: 'string', default: 'configs/experiments/gpt55-medium.openai-responses.json'}}});
  if (!values.out) throw new Error('--out must be a new directory');
  const out = path.resolve(values.out);
  await fs.mkdir(out); // A new cohort cannot overwrite outcomes from a prior experiment.
  await checked(['npm', 'run', 'build']); // Snapshot and execute the current source, never a stale dist/.
  const manifest = JSON.parse(await fs.readFile('TestPrism-main/verified_tasks/test-hardest-500/manifest.json', 'utf8'));
  const holdout = new Set(Object.entries(manifest.sources).flatMap(([source, value]) =>
    (value as {task_ids: string[]}).task_ids.map(id => `${source}/${id}`)));
  const config = JSON.parse(await fs.readFile(values.config!, 'utf8'));
  config.budget = {...config.budget, maxCalls: 24, maxInputTokens: 200000, maxOutputTokens: 48000, wallMs: 900000, concurrency: 2};
  const proxyConfigured = ['HTTPS_PROXY', 'https_proxy', 'HTTP_PROXY', 'http_proxy'].some(key => !!process.env[key]);
  const workerEnv = {...process.env, NODE_USE_ENV_PROXY: process.env.NODE_USE_ENV_PROXY ?? (proxyConfigured ? '1' : '0')};
  // Check transport before reserving model calls. This GET carries no API credentials.
  const transport = await command([process.execPath, '--input-type=module', '-e',
    'const r = await fetch(process.argv[1], {signal: AbortSignal.timeout(20000)}); if(r.status >= 500) throw new Error("Provider HTTP " + r.status); console.log(r.status)',
    new URL(config.model.baseURL).origin], {env: workerEnv, timeoutMs: 25000});
  await atomicJSON(path.join(out, 'transport.json'), {checkedAt: new Date().toISOString(),
    envProxyEnabled: workerEnv.NODE_USE_ENV_PROXY === '1', code: transport.code, status: transport.stdout.trim()});
  if (transport.code !== 0 || transport.timedOut) throw new Error('Provider transport preflight failed before model calls; check network and proxy settings');
  await atomicJSON(path.join(out, 'config.json'), config);
  if (!['original', 'fresh', 'fresh-regression'].includes(values.cohort!)) throw new Error('Unknown cohort; use original, fresh or fresh-regression');
  const pool: ReadonlyArray<readonly [string, string]> = values.cohort === 'fresh-regression'
    ? freshSelections.map(([instance]) => [instance, 'Previously used public hard development case; source capture and repair workflow regression'] as const)
    : values.cohort === 'fresh' ? freshSelections : selections;
  const selected = values.tasks ? pool.filter(([instance]) => values.tasks!.split(',').includes(instance)) : pool;
  if (!selected.length || (values.tasks && selected.length !== values.tasks.split(',').length)) throw new Error('Unknown or duplicate cohort task');
  const tasks = await Promise.all(selected.map(async ([instance, reason], index) => {
    const id = `from-swe-bench/${instance}`;
    if (holdout.has(id)) throw new Error(`Hardest500 overlap: ${id}`);
    const taskDirectory = path.resolve(`TestPrism-main/verified_tasks_flat_test/from-swe-bench__${instance}`);
    const publicTask = parse(await fs.readFile(path.join(taskDirectory, 'task.yaml'), 'utf8'));
    if (publicTask.difficulty !== 'hard' || publicTask.workdir !== '/testbed') throw new Error('Cohort public metadata changed');
    // Read the base image name only. Never build a context containing solution.sh.
    const image = (await fs.readFile(path.join(taskDirectory, 'Dockerfile'), 'utf8')).match(/^FROM (swebench\/\S+)\s*$/m)?.[1];
    if (!image) throw new Error(`Unsupported image declaration: ${id}`);
    return {id, instance, reason, index, taskDirectory, image, root: publicTask.workdir,
      instruction: publicTask.instruction as string, difficulty: publicTask.difficulty};
  }));
  await atomicJSON(path.join(out, 'cohort.json'), {frozenAt: new Date().toISOString(), eligibleForEvolutionValidation: false,
    officialSplit: false, cohort: values.cohort, holdoutManifestDigest: hash(manifest), referenceRead: false, config,
    tasks: tasks.map(({instruction, ...task}) => ({...task, instructionDigest: hash(instruction)}))});
  // Preserve the exact code and generic resources used before any model request.
  for (const file of ['src', 'dist', 'skills', 'integrations', 'scripts', 'package.json', 'package-lock.json', 'tsconfig.json'])
    await fs.cp(file, path.join(out, 'implementation', file), {recursive: true, filter: p => !p.includes('__pycache__') && !p.endsWith('.pyc')});
  const containers: string[] = [];
  try {
    const preparationErrors: Array<{taskId: string; error: string}> = [];
    await mapLimit(tasks, 2, async task => {
      try {
      const imageId = await checked(['docker', 'image', 'inspect', '--format', '{{.Id}}', task.image]);
      const container = await checked(['docker', 'run', '-d', '--network', 'none', '--label', 'testhelix.diagnostic=public-development-batch',
        '-e', `PATH=${environment.PATH}`, '--entrypoint', '/bin/bash', imageId, '-c', 'sleep infinity']);
      containers.push(container);
      // No host directories, credentials or benchmark answer files are mounted.
      await checked(['docker', 'exec', container, 'bash', '-c',
        'find /testbed -name .git -prune -exec rm -rf {} +; rm -rf /testbed/agent-tests /testbed/solution.sh /testbed/patch-sets /testbed/.patch-generation-state']);
      const directory = path.join(out, task.instance); await fs.mkdir(directory);
      await atomicJSON(path.join(directory, 'task.json'), {id: task.id, source: 'from-swe-bench', instruction: task.instruction,
        testTimeoutMs: 120000, runtime: {kind: 'docker', container, root: task.root}});
      await atomicJSON(path.join(directory, 'provenance.json'), {schemaVersion: 1, preparedAt: new Date().toISOString(),
        kind: 'public-development-diagnostic', eligibleForEvolutionValidation: false, excludesHardest500: true,
        taskDirectory: task.taskDirectory, image: task.image, imageId, root: task.root, env: environment,
        configDigest: hash(await fs.readFile(path.join(out, 'config.json')))});
      await fs.symlink(path.join('..', 'campaign', `task-${String(task.index + 1).padStart(4, '0')}`), path.join(directory, 'run'));
      process.stdout.write(`Prepared ${task.id}\n`);
      } catch (error) { preparationErrors.push({taskId: task.id, error: String(error)}); }
    });
    if (preparationErrors.length) {
      await atomicJSON(path.join(out, 'preparation-errors.json'), preparationErrors);
      throw new Error('Environment preparation failed before model calls; see preparation-errors.json');
    }
    await atomicJSON(path.join(out, 'plan.json'), {configFile: 'config.json',
      taskFiles: tasks.map(task => `${task.instance}/task.json`), concurrency: 2, maxTasks: tasks.length,
      maxCalls: 24 * tasks.length, maxInputTokens: 200000 * tasks.length, maxOutputTokens: 48000 * tasks.length, wallMs: 1900000});
    const code = await new Promise<number | null>((resolve, reject) => {
      const child = spawn(process.execPath, ['dist/cli.js', 'campaign', '--plan', path.join(out, 'plan.json'), '--out', path.join(out, 'campaign')], {stdio: 'inherit', env: workerEnv});
      child.on('error', reject); child.on('close', resolve);
    });
    if (code !== 0) throw new Error(`Campaign process exited ${code}`);
    const runs = await Promise.all(tasks.map(async task => {
      const bytes = await fs.readFile(path.join(out, task.instance, 'run', 'result.json')).catch(() => undefined);
      return {taskId: task.id, resultDigest: bytes ? hash(bytes) : null};
    }));
    await atomicJSON(path.join(out, 'generation-freeze.json'), {frozenAt: new Date().toISOString(), referenceRead: false, runs});
  } finally {
    await Promise.allSettled(containers.map(container => checked(['docker', 'rm', '-f', container])));
  }
  if (values['generation-only']) return;
  const evaluation = await new Promise<number | null>((resolve, reject) => {
    const child = spawn(process.execPath, ['--import', 'tsx', 'scripts/evaluate-diagnostic-batch.ts', '--batch', out], {stdio: 'inherit'});
    child.on('error', reject); child.on('close', resolve);
  });
  if (evaluation !== 0) throw new Error('Frozen host evaluation incomplete; inspect per-task logs');
}
main().catch(error => {process.stderr.write(String(error) + '\n'); process.exitCode = 1;});
