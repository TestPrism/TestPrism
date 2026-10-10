import test from 'node:test';
import assert from 'node:assert/strict';
import type {ModelMessage} from 'ai';
import {fixture} from './helpers.js';
import {LocalSandbox} from '../src/runtime.js';
import {Store} from '../src/store.js';
import {prepareTestHarness, harnessUsageCard} from '../src/harness.js';
import {SkillSession} from '../src/skill-session.js';
import {skillIds, skillResources} from '../src/skills.js';
import {skillContext} from '../src/skill-context.js';
import {projectContext} from '../src/context.js';
import {command} from '../src/util.js';
import {Toolbox} from '../src/tools.js';

test('method routing recognizes ORM and mathematical contracts without task names or delivery keyword leakage', () => {
  assert.equal(skillContext('Negating a queryset with a correlated subquery must preserve behavior.'), 'framework');
  assert.equal(skillContext('Solve the algebraic equation with independently checked results.'), 'algorithm');
  assert.equal(skillContext('A polynomial transformation must preserve its mathematical identity.'), 'algorithm');
  assert.equal(skillContext('The filesystem root must remain unchanged.'), 'cli');
  assert.equal(skillContext('Preserve the documented result.\n## Test Output Location & Entry Point\nUse an ORM subquery and a polynomial equation as directory names.'), 'general');
});

test('preparation details remain recoverable by receipt after context compaction', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  const store = new Store(f.out); await store.init();
  const prepared = await prepareTestHarness(sandbox, {framework: 'unittest'}, 5000, AbortSignal.timeout(10000));
  assert.equal(prepared.controlPassed, true);
  const state = new SkillSession(skillIds, false, {instruction: 'Framework behavior.', source: 'local', routes: []});
  const artifact = await store.artifact(JSON.stringify(prepared));
  state.observe('prepare_test_harness', {framework: 'unittest'}, prepared, artifact);
  const messages: ModelMessage[] = [{role: 'user', content: 'Original public task: preserve documented behavior.'},
    {role: 'assistant', content: 'Prepare the runtime.'}, {role: 'user', content: 'TRANSIENT_SETUP_PAYLOAD ' + JSON.stringify(prepared)}];
  for (let i = 0; i < 12; i++) messages.push({role: 'assistant', content: `Inspect interface ${i}.`},
    {role: 'user', content: 'Unrelated repository evidence. '.repeat(80)});
  const active = state.render();
  const projected = await projectContext(messages, '', {maxTokens: 2800, recentTurns: 1, toolOutputChars: 1000}, store,
    {tail: [{role: 'user', content: active.text}]});
  assert.doesNotMatch(JSON.stringify(projected), /TRANSIENT_SETUP_PAYLOAD/);
  assert.ok(active.text.includes(artifact));
  const receipt = JSON.parse(await store.readArtifact(artifact));
  assert.equal(receipt.framework, 'unittest');
  assert.match(JSON.stringify(receipt.usage), /agent-tests\/cases\/test_\*\.py/);
  assert.doesNotMatch(active.text, /HarnessChild|import evo_harness_app.models/);
  assert.ok(active.chars <= 4400);
});

test('the retained Django fixture card agrees with the actual installed template fields', async () => {
  const models = skillResources('framework-tests').find(file => file.path === 'assets/evo_harness_app/models.py')!;
  const parsed = await command(['python3', '-B', '-c',
    'import ast,json,sys\ntree=ast.parse(sys.stdin.read())\nprint(json.dumps({c.name:[n.targets[0].id for n in c.body if isinstance(n,ast.Assign)] for c in tree.body if isinstance(c,ast.ClassDef)}))'],
    {input: models.data, timeoutMs: 5000});
  assert.equal(parsed.code, 0, parsed.stderr);
  const classes = JSON.parse(parsed.stdout) as Record<string, string[]>;
  const card = harnessUsageCard('django');
  for (const [name, fields] of Object.entries(classes)) {
    assert.ok(card.includes(name), `Missing fixture name ${name}`);
    for (const field of fields) assert.ok(card.includes(field), `Missing fixture field ${name}.${field}`);
  }
  assert.equal(classes.HarnessChild!.includes('value'), false);
  assert.match(card, /HarnessChild has parent \(FK\), no value field/);
  assert.match(card, /agent-tests\/evo_harness_app\/models.py/);
  assert.match(card, /initial template; recheck edited files/);
});

test('skill state keeps preparation receipts without promoting setup to current task evidence', () => {
  const task = {instruction: 'Django framework behavior.', source: 'local', routes: []};
  const state = new SkillSession(skillIds, false, task);
  state.observe('prepare_test_harness', {framework: 'django'},
    {ok: true, stage: 'control', controlPassed: true}, 'b'.repeat(64));
  assert.match(state.render().reason, /generic control passed: true/);
  assert.match(state.render().reason, /Task coverage remains unverified/);
  assert.doesNotMatch(state.render().text, /import evo_harness_app.models/);
  const restored = new SkillSession(skillIds, false, task); restored.restore(state.snapshot());
  assert.equal(restored.snapshot().setup, undefined);
  assert.doesNotMatch(restored.render().reason, /control passed: true/);
  state.observe('write_file', {path: 'agent-tests/run-tests.sh'}, {ok: true}, 'c'.repeat(64));
  assert.equal(state.snapshot().setup, undefined);
  state.observe('prepare_test_harness', {framework: 'unittest'}, {ok: false, stage: 'probe'}, 'f'.repeat(64));
  assert.equal(state.snapshot().setup?.controlPassed, false);
  assert.match(state.render().reason, /unsuccessful execution/);
});

test('host correction activates failure triage without inventing an own-execution symptom', () => {
  const task = {instruction: 'Preserve the documented interface.', source: 'local', routes: []};
  const state = new SkillSession(skillIds, false, task); state.feedback();
  assert.ok(state.render().ids.includes('failure-triage'));
  assert.equal(state.snapshot().failure, undefined);
  assert.doesNotMatch(state.render().reason, /Own execution symptom/);
  const control = new SkillSession(skillIds, false, {...task, routes: [{context: 'general', skill: null}]}); control.feedback();
  assert.deepEqual(control.render().ids, ['artifact-workflow']);
});

test('skill references load only on demand from fixed enabled packages, and resource-free controls cannot access them', async t => {
  const f = await fixture(); t.after(f.cleanup); const store = new Store(f.out); await store.init();
  const sandbox = new LocalSandbox(f.workspace, false), signal = AbortSignal.timeout(5000);
  const tools = new Toolbox(sandbox, store, 10000, 3000, false, ['framework-tests']);
  const entry = await tools.execute('load_skill', {id: 'framework-tests'}, signal);
  assert.ok(Array.isArray(entry.references) && entry.references.includes('references/django-models.md'));
  assert.doesNotMatch(String(entry.text), /schema_editor.create_model/);
  const detail = await tools.execute('load_skill', {id: 'framework-tests', resource: 'references/django-models.md'}, signal);
  assert.match(String(detail.text), /schema_editor.create_model/);
  for (const resource of ['../../.env.local', 'assets/evo_harness_app/models.py', 'references/missing.md'])
    await assert.rejects(tools.execute('load_skill', {id: 'framework-tests', resource}, signal), /Unknown curated/);
  await assert.rejects(new Toolbox(sandbox, store, 10000, 3000, false, ['framework-tests'], false)
    .execute('load_skill', {id: 'framework-tests', resource: 'references/django-models.md'}, signal), /disabled/);
});
