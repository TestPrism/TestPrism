import assert from 'node:assert/strict';
import path from 'node:path';
import { DockerSandbox } from '../src/runtime.js';
import { prepareTestHarness } from '../src/harness.js';
import {Store} from '../src/store.js';
import {Toolbox} from '../src/tools.js';
import {skillIds} from '../src/skills.js';
import { atomicJSON, command, id, implementationDigest } from '../src/util.js';

// Public baseline image only. The generated control contains no task assertions.
const image = process.env.TESTHELIX_HARNESS_IMAGE ?? 'swebench/sweb.eval.x86_64.django_1776_django-11087:latest';
const name = id('testhelix-skill-control'), output = path.resolve(process.argv[2] ?? `.runs/skill-control-${Date.now()}`);
const environment = '/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin';
try {
  const start = await command(['docker', 'run', '-d', '--name', name, '--label', 'testhelix.fixture=skill-control', '--network', 'none',
    '-e', `PATH=${environment}`, '--workdir', '/testbed', '--entrypoint', 'sh', image, '-c', 'while :; do sleep 3600; done'], {timeoutMs: 120000});
  assert.equal(start.code, 0, start.stderr);
  const sandbox = new DockerSandbox(name, '/testbed', false);
  const store = new Store(output).forSession('harness-control'); await store.init();
  const makeTools = () => new Toolbox(sandbox, store, 10000, 30000, false, skillIds);
  let tools = makeTools(); tools.procedures.setActive(['framework-tests']);
  for (const app of ['fixture_parent', 'fixture_child'])
    await sandbox.write(`agent-tests/project_models/${app}/__init__.py`, Buffer.from(''));
  await sandbox.write('agent-tests/project_models/fixture_parent/models.py', Buffer.from('from django.db import models\nclass Record(models.Model):\n    value = models.IntegerField()\n'));
  await sandbox.write('agent-tests/project_models/fixture_child/models.py', Buffer.from('from django.db import models\nfrom fixture_parent.models import Record\nclass Derived(Record):\n    label = models.CharField(max_length=30)\n'));
  const input = {framework: 'django', projectApps: ['fixture_parent', 'fixture_child'], modulePaths: ['agent-tests/project_models']};
  const prepared = await tools.execute('prepare_test_harness', input, AbortSignal.timeout(60000));
  assert.equal(prepared.controlPassed, true, JSON.stringify(prepared));
  const saved = tools.procedures.snapshot();
  assert.equal(saved.receipts[0]!.activation, 'active-method');
  saved.receipts[0]!.status = 'running'; saved.receipts[0]!.stage = 'install';
  await store.save('sessions/harness-control/skill-execution.json', saved);
  const missing = saved.receipts[0]!.plannedFiles[0]!.path; await sandbox.remove(missing);
  tools = makeTools(); await tools.procedures.initialize();
  assert.equal(tools.procedures.snapshot().receipts[0]!.status, 'interrupted');
  const recovered = await tools.execute('prepare_test_harness', input, AbortSignal.timeout(60000));
  assert.equal(recovered.controlPassed, true); assert.equal(recovered.reusedFiles, (prepared.files as unknown[]).length - 1);
  const empty = await sandbox.exec('bash agent-tests/run-tests.sh', 10000);
  assert.notEqual(empty.code, 0); assert.match(empty.stdout + empty.stderr, /No task tests found/);
  await sandbox.write('agent-tests/cases/test_fixture.py', Buffer.from(`from django.test import TestCase\nfrom evo_harness_app.models import HarnessParent\n\nclass Example(TestCase):\n    def test_roundtrip(self):\n        item = HarnessParent.objects.create(value=17)\n        self.assertEqual(HarnessParent.objects.get(pk=item.pk).value, 17)\n`));
  const pass = await sandbox.exec('bash agent-tests/run-tests.sh', 10000); assert.equal(pass.code, 0, pass.stderr);
  await sandbox.write('agent-tests/cases/test_inheritance.py', Buffer.from('from django.test import TestCase\nfrom fixture_child.models import Derived\nfrom fixture_parent.models import Record\nclass InheritanceControl(TestCase):\n    def test_parent_and_child_tables(self):\n        obj = Derived.objects.create(value=17, label="control")\n        self.assertEqual(Record.objects.get(pk=obj.pk).value, 17)\n        self.assertEqual(Derived.objects.get(pk=obj.pk).label, "control")\n'));
  const inheritance = await sandbox.exec('bash agent-tests/run-tests.sh', 10000); assert.equal(inheritance.code, 0, inheritance.stderr);
  await sandbox.write('agent-tests/cases/test_fixture.py', Buffer.from(`from django.test import TestCase\n\nclass Example(TestCase):\n    def test_failure(self):\n        self.assertEqual(17, 18)\n`));
  const fail = await sandbox.exec('bash agent-tests/run-tests.sh', 10000); assert.notEqual(fail.code, 0); assert.match(fail.stderr, /AssertionError/);
  const inspect = await command(['docker', 'inspect', name, '--format', '{{.Image}}']);
  const changed = Buffer.from('#!/bin/sh\nexit 17\n'); await sandbox.write('agent-tests/run-tests.sh', changed, 0o755);
  const conflict = await tools.execute('prepare_test_harness', input, AbortSignal.timeout(30000));
  assert.equal(conflict.stage, 'conflict'); assert.deepEqual(await sandbox.read('agent-tests/run-tests.sh'), changed);
  const evidence = {kind: 'generic skill resource integration; no model calls or benchmark score', implementationDigest: await implementationDigest(),
    image, imageId: inspect.stdout.trim(), runtime: prepared.runtime, skillDigest: prepared.skillDigest,
    control: prepared.control, recovered, receipts: tools.procedures.snapshot(), empty, pass, inheritance, fail,
    ownedEditsPreserved: true, conflictsPreserved: (await prepareTestHarness(sandbox, {framework: 'django'}, 30000, AbortSignal.timeout(30000))).stage === 'conflict'};
  assert.equal(evidence.conflictsPreserved, true);
  await atomicJSON(path.join(output, 'evidence.json'), evidence);
  process.stdout.write(JSON.stringify({output, runtime: prepared.runtime, controlPassed: true, emptyRejected: true, failurePropagated: true}) + '\n');
} finally { await command(['docker', 'rm', '-f', name]); }
