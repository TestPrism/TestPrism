import assert from 'node:assert/strict';
import path from 'node:path';
import { runTask } from '../src/pipeline.js';
import { DockerFactory, DockerSandbox, cleanupResources } from '../src/runtime.js';
import { command, id, atomicJSON } from '../src/util.js';
import { taskSchema } from '../src/types.js';
import { config, FixtureModel, buggy } from '../tests/helpers.js';

const image = process.env.TESTHELIX_SMOKE_IMAGE ?? 'node:22-bookworm-slim';
const name = id('testhelix-fixture'), output = path.resolve(process.argv[2] ?? `.runs/docker-smoke-${Date.now()}`);
let factory: DockerFactory | undefined;
try {
  const started = await command(['docker', 'run', '-d', '--name', name, '--label', 'testhelix.fixture=true', '--network', 'none',
    '--workdir', '/app', '--entrypoint', 'sh', image, '-c', 'while :; do sleep 3600; done'], {timeoutMs: 120000});
  assert.equal(started.code, 0, started.stderr);
  const original = new DockerSandbox(name, '/app', false);
  await original.write('prime.cjs', Buffer.from(buggy));
  const model = new FixtureModel(), settings = config(); settings.budget.wallMs = 180000;
  const result = await runTask(taskSchema.parse({id: 'docker/prime-fixture', instruction:
    'Fix isPrime(n) for nonnegative integers. In particular, 1 is not prime.', runtime: {kind: 'docker', container: name, root: '/app'}}),
    settings, output, {model});
  assert.equal(result.status, 'validated', result.reason + '\n' + JSON.stringify(result.matrix));
  assert.equal((await original.read('prime.cjs')).toString(), buggy);
  assert.equal((await original.exec('bash agent-tests/run-tests.sh', 5000)).code, 1);
  await cleanupResources(path.join(output, 'workspaces/resources.json'));
  factory = await DockerFactory.create(name, '/app', 'none');
  const timed = await factory.fork('timeout-check');
  const killed = await timed.exec('sleep 60 & wait', 150);
  assert.equal(killed.timedOut, true);
  const inspection = await command(['docker', 'inspect', timed.id]); assert.notEqual(inspection.code, 0);
  factory.resourceArgs.push('--entrypoint', '/testhelix-missing-entrypoint');
  await assert.rejects(factory.fork('failed-start'), /docker run failed/);
  const leftovers = await command(['docker', 'ps', '-aq', '--filter', `label=testhelix.owner=${factory.owner}`]);
  assert.equal(leftovers.code, 0); assert.equal(leftovers.stdout.trim(), '', 'a failed start must not leak a created container');
  await factory.close();
  const snapshot = await command(['docker', 'image', 'inspect', factory.image]); assert.notEqual(snapshot.code, 0);
  const inspected = await command(['docker', 'image', 'inspect', image, '--format', '{{json .RepoDigests}}']);
  const evidence = {kind: 'scripted-model Docker integration, not a benchmark/model score', status: result.status,
    selected: result.selected.length, witnesses: result.witnesses.length, modelCalls: model.calls, usage: result.usage,
    elapsedMs: result.elapsedMs, image, imageDigests: JSON.parse(inspected.stdout),
    testsOnlyPublication: true, timeoutContainerRemoved: true, failedStartContainerRemoved: true, snapshotRemoved: true};
  await atomicJSON(path.join(output, 'smoke-evidence.json'), evidence);
  process.stdout.write(JSON.stringify(evidence, null, 2) + '\n');
} finally {
  await factory?.close();
  await cleanupResources(path.join(output, 'workspaces/resources.json'));
  await command(['docker', 'rm', '-f', name]);
}
