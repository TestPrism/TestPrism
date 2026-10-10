import test from 'node:test';
import assert from 'node:assert/strict';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { fixture, config, completion } from './helpers.js';
import { LocalSandbox } from '../src/runtime.js';
import { Toolbox, workspaceTools } from '../src/tools.js';
import { Store } from '../src/store.js';
import { SkillSession } from '../src/skill-session.js';
import { skillIds, builtinSkills } from '../src/skills.js';
import { taskDomain, skillContext } from '../src/skill-context.js';
import { finalizeReview } from '../src/review.js';
import { resourceDigests, hash } from '../src/util.js';
import { AgentSession } from '../src/agent.js';
import { Budget } from '../src/budget.js';
import { configSchema, type Model } from '../src/types.js';
import { skillAblationProfiles } from '../src/skill-ablation.js';
import { applyGenome, genesis } from '../src/genome.js';
import { classify } from '../src/validation.js';

test('delivery boilerplate cannot turn a framework task into CLI; substantive CLI requirements still count', () => {
  const delivery = '\n## Test Output Location & Entry Point\nUse a shell command line in a directory. Capture stdout, stderr and exit code.\n### Runner\nfilesystem command line interface\n## Before You Write Tests\nInspect directory.';
  assert.equal(skillContext('## Background\nDjango model registration import.\n## Required observable outcome\nPreserve framework behavior.' + delivery), 'framework');
  assert.equal(skillContext('## Background\nRepair behavior.' + delivery), 'general');
  assert.equal(skillContext('Django database behavior.\n```text\nexecute_from_command_line\nstackwalk_stdout stackwalk_stderr\n# stdout stderr command line\n```' + delivery), 'framework');
  assert.equal(skillContext('## Required observable outcome\nThe command line must send results to stdout, errors to stderr and set exit code.' + delivery), 'cli');
  const nested = taskDomain('## Before You Write Tests\n### Setup\nshell\n## Expected behavior\narray algorithm');
  assert.match(nested.domain, /array algorithm/); assert.doesNotMatch(nested.domain, /shell/);
});

test('fixed Python harness runs controls, rejects empty suites, propagates failures and preserves existing work', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const sandbox = new LocalSandbox(f.workspace, false), store = new Store(f.out); await store.init();
  const tools = new Toolbox(sandbox, store, 8000, 10000, false, ['framework-tests']);
  const prepared = await tools.execute('prepare_test_harness', {framework: 'unittest'}, AbortSignal.timeout(20000));
  assert.equal(prepared.controlPassed, true, JSON.stringify(prepared));
  assert.match(JSON.stringify(prepared.runtime), /python/);
  assert.match(JSON.stringify(prepared.usage), /TestCase subclass/);
  const empty = await sandbox.exec('bash agent-tests/run-tests.sh', 3000);
  assert.notEqual(empty.code, 0); assert.match(empty.stderr, /No task tests found/);
  assert.equal(classify(empty.code, empty.stdout + empty.stderr), 'error', 'empty control harness must never qualify as a baseline regression');
  await sandbox.write('agent-tests/cases/test_contract.py', Buffer.from('import unittest\nclass Contract(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(3 + 4, 7)\n'));
  const passing = await sandbox.exec('bash agent-tests/run-tests.sh', 3000); assert.equal(passing.code, 0, passing.stderr);
  await sandbox.write('agent-tests/cases/test_contract.py', Buffer.from('import unittest\nclass Contract(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(3 + 4, 8)\n'));
  const failing = await sandbox.exec('bash agent-tests/run-tests.sh', 3000); assert.notEqual(failing.code, 0); assert.match(failing.stderr, /AssertionError/);
  const before = await sandbox.read('agent-tests/run-tests.sh');
  const again = await tools.execute('prepare_test_harness', {framework: 'unittest'}, AbortSignal.timeout(10000));
  assert.equal(again.stage, 'control'); assert.ok(Number(again.reusedFiles) > 0);
  assert.deepEqual(await sandbox.read('agent-tests/run-tests.sh'), before);
  await sandbox.write('agent-tests/run-tests.sh', Buffer.from('#!/bin/sh\nexit 17\n'));
  const changed = await tools.execute('prepare_test_harness', {framework: 'unittest'}, AbortSignal.timeout(10000));
  assert.equal(changed.stage, 'conflict'); assert.match((await sandbox.read('agent-tests/run-tests.sh')).toString(), /exit 17/);
  const denied = new Toolbox(sandbox, store, 3000, 3000, false, ['framework-tests'], false);
  await assert.rejects(denied.execute('prepare_test_harness', {framework: 'unittest'}, AbortSignal.timeout(1000)), /not enabled/);
  const reviewer = new Toolbox(sandbox, store, 3000, 3000, true, ['framework-tests']);
  await assert.rejects(reviewer.execute('prepare_test_harness', {framework: 'unittest'}, AbortSignal.timeout(1000)), /read-only/);
  assert.throws(() => workspaceTools.prepare_test_harness!.parse!({framework: 'unittest', python: 'python3;touch sentinel'}));
});

test('active skills change on own execution evidence without accumulating failure prompts or bypassing abstention', () => {
  const task = {instruction: 'Django framework registration', source: 'local', routes: []};
  const state = new SkillSession(skillIds, false, task);
  assert.deepEqual(state.render().ids, ['artifact-workflow']);
  state.observe('prepare_test_harness', {framework: 'django'}, {ok: true, runtime: {python: '3.6.13', django: '2.2'}, controlPassed: true}, 'a'.repeat(64));
  assert.deepEqual(state.render().ids, ['artifact-workflow'], 'setup control must retain the workflow for writing actual cases');
  const failed = {ok: true, code: 1, stderr: 'ModuleNotFoundError: local_fixture'};
  state.observe('exec', {command: 'bash agent-tests/run-tests.sh'}, failed, 'b'.repeat(64));
  assert.ok(state.render().ids.includes('failure-triage'));
  const size = state.render().chars;
  for (let i = 0; i < 20; i++) state.observe('exec', {command: 'bash agent-tests/run-tests.sh'}, failed, 'b'.repeat(64));
  assert.equal(state.render().chars, size);
  state.observe('exec', {command: 'bash agent-tests/run-tests.sh'}, {ok: true, code: 0, status: 'pass'}, 'c'.repeat(64));
  assert.ok(!state.render().ids.includes('failure-triage'));
  state.observe('exec', {command: 'bash agent-tests/run-tests.sh'}, {ok: true, code: 1, stderr: 'No task tests found'}, 'c'.repeat(64));
  assert.equal(state.render().phase, 'repair'); assert.ok(state.render().ids.includes('failure-triage'));
  assert.doesNotMatch(state.render().reason, /Write cases|No task cases/);
  const control = new SkillSession(skillIds, false, {...task, routes: [{context: 'framework', skill: null}]});
  control.observe('exec', {command: 'bash agent-tests/run-tests.sh'}, failed, 'b'.repeat(64));
  assert.deepEqual(control.render().ids, ['artifact-workflow']);
  const disabled = new SkillSession([], false, task);
  disabled.observe('exec', {command: 'bash agent-tests/run-tests.sh'}, failed, 'b'.repeat(64));
  assert.equal(disabled.render().text, '');
});

test('classified failure preserves generation and triage without deciding its cause or bypassing feedback', () => {
  const task = {instruction: 'Framework behavior', source: 'local', routes: []};
  const state = new SkillSession(skillIds, false, task);
  state.observe('write_test_suite', {}, {written: true, execution: {code: 1, status: 'fail', timedOut: false}}, 'observed-failure');
  const rendered = state.render();
  assert.equal(rendered.phase, 'tests');
  assert.ok(rendered.ids.includes('failure-triage'));
  assert.match(rendered.reason, /unattributed.*baseline.*oracle.*setup/);
  assert.match(rendered.reason, /correctness is unverified/);
  assert.match(rendered.reason, /observed-failure/);
  // A new API may be absent on baseline. Routing must not reinterpret exceptions.
  state.observe('exec', {command: 'bash agent-tests/run-tests.sh'},
    {code: 1, status: 'fail', stderr: 'ModuleNotFoundError', diagnostics: {failures: [{exception: 'ImportError'}]}}, 'unresolved-api');
  assert.equal(state.render().phase, 'tests');
  state.feedback(); assert.equal(state.render().phase, 'repair');
  assert.ok(state.render().ids.includes('failure-triage'));
  const abstained = new SkillSession(skillIds, false, {...task, routes: [{context: state.offer.context, skill: null}]});
  abstained.observe('write_test_suite', {}, {written: true, execution: {code: 1, status: 'fail'}}, 'failure');
  assert.deepEqual(abstained.render().ids, ['artifact-workflow']);
});

test('execution errors, zero tests, timeout, truncation and unclassified results remain repair observations', () => {
  for (const execution of [
    {code: 1, status: 'error', stderr: 'error collecting: missing import'},
    {code: 0, status: classify(0, 'Ran 0 tests in 0.001s\nOK')},
    {code: 1, status: 'fail', timedOut: true},
    {code: 0, status: 'pass', truncated: true},
    {code: 1, status: 'fail', truncated: true},
    {code: 1, stderr: 'ModuleNotFoundError'},
    {code: 0},
  ]) {
    const state = new SkillSession(skillIds, false, {instruction: 'Public behavior', source: 'local', routes: []});
    state.observe('write_test_suite', {}, {written: true, execution}, 'unresolved');
    assert.equal(state.render().phase, 'repair', JSON.stringify(execution));
    assert.ok(state.render().ids.includes('failure-triage'));
  }
});

test('skill telemetry distinguishes requested bodies from component filtering and bounded review omission', () => {
  const task = {instruction: 'Public behavior', source: 'local', routes: []};
  const review = new SkillSession(skillIds, true, task).render();
  assert.deepEqual(review.requested, ['contract-audit', 'control-evidence', 'observable-oracles']);
  assert.deepEqual(review.ids, ['contract-audit', 'control-evidence']);
  assert.ok(review.omitted.some(item => item.id === 'observable-oracles' && item.reason === 'context-limit'));
  assert.ok(review.chars <= 4400);
  const routed = new SkillSession(skillIds, false, {...task, routes: [{context: 'general', skill: 'observable-oracles'}]});
  assert.ok(routed.render().omitted.some(item => item.id === 'observable-oracles' && item.reason === 'component'));
  routed.observe('load_skill', {id: 'observable-oracles'}, {ok: true}, 'manual');
  assert.equal(routed.render().ids[0], 'observable-oracles');
  assert.ok(!routed.render().omitted.some(item => item.id === 'observable-oracles'));
  assert.ok(routed.render().chars <= 4400);
});

test('a routed project method remains injected alongside triage with a full artifact hash', () => {
  const state = new SkillSession(skillIds, false, {instruction: 'Django framework behavior', source: 'local',
    routes: [{context: 'framework', skill: 'framework-tests'}]});
  state.observe('write_test_suite', {}, {written: true, execution: {code: 1, status: 'fail', timedOut: false}}, 'a'.repeat(64));
  const rendered = state.render();
  assert.equal(rendered.phase, 'tests');
  assert.deepEqual(rendered.requested, ['artifact-workflow', 'failure-triage', 'framework-tests']);
  assert.deepEqual(rendered.ids, rendered.requested);
  assert.deepEqual(rendered.omitted, []);
  assert.match(rendered.reason, /correctness is unverified/);
  assert.ok(rendered.text.includes('a'.repeat(64)));
  assert.ok(rendered.chars <= 4400, `${rendered.chars} exceeds the unchanged limit`);
});

test('suggestions cannot veto; blocking findings need real file quotes and legacy rejection remains conservative', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const sandbox = new LocalSandbox(f.workspace, false);
  await sandbox.write('agent-tests/check.py', Buffer.from('assert observed == 42\n'));
  const finding = {severity: 'suggestion', kind: 'improvement', explanation: 'Consider another independently justified boundary.',
    requirement: 'Return the public result.', evidence: [{path: 'agent-tests/check.py', quote: 'assert observed == 42'}]};
  assert.equal((await finalizeReview({findings: [finding], coveredRequirements: ['public result']}, sandbox)).accepted, true);
  const blocker = {...finding, severity: 'blocking', kind: 'unsupported-assertion', explanation: '42 is asserted for every input although the result must vary.'};
  await sandbox.write('contract.txt', Buffer.from('The public result varies with the input.\n'));
  const falseRejection = {allowedBehavior: 'Return 7 for an input whose public result is 7.', whyAllowed: 'The result depends on the input, not a universal constant.',
    controlAssessment: 'This constant is asserted for every input, not a supported contrasting control.', publicBasis: {repository: {path: 'contract.txt', startLine: 1}}};
  const rejected = await finalizeReview({findings: [{...blocker, falseRejection}], coveredRequirements: []}, sandbox);
  assert.equal(rejected.accepted, false); assert.equal(rejected.concerns.length, 1);
  await assert.rejects(finalizeReview({findings: [{...blocker, evidence: [{path: 'agent-tests/check.py', quote: 'invented file content'}]}], coveredRequirements: []}, sandbox), /quote not found/);
  await assert.rejects(finalizeReview({findings: [{...finding, severity: 'blocking'}], coveredRequirements: []}, sandbox), /optional improvement/);
  const legacy = await finalizeReview({accepted: true, concerns: ['Old unresolved concern'], missingRequirements: [], coveredRequirements: []}, sandbox);
  assert.equal(legacy.accepted, false);
});

test('review line citations preserve source escapes and a bad optional citation cannot erase a verified blocker', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const sandbox = new LocalSandbox(f.workspace, false);
  const line = 'const expected = "first\\\\nsecond";';
  await sandbox.write('agent-tests/check.js', Buffer.from('// fixture\n' + line + '\nassert.equal(actual, expected);\n'));
  const finding = {severity: 'blocking', kind: 'unsupported-assertion', explanation: 'The string asserts a literal escape instead of the specified newline.',
    requirement: 'The public result is multiline.', evidence: [{path: 'agent-tests/check.js', startLine: 2, endLine: 3},
      {path: 'agent-tests/check.js', quote: 'a supplementary quote that does not exist'}],
    falseRejection: {allowedBehavior: 'Return actual multiline text with a newline character.', whyAllowed: 'The public interface specifies multiline text, not a literal escape sequence.',
      controlAssessment: 'This compares the required result directly and is not a separate control.', publicBasis: {repository: {path: 'contract.txt', startLine: 1}}}};
  await sandbox.write('contract.txt', Buffer.from('The public result is multiline.\n'));
  const result = await finalizeReview({findings: [finding], coveredRequirements: []}, sandbox);
  assert.equal(result.accepted, false); assert.equal(result.findings?.[0]?.evidence.length, 1);
  assert.equal(result.findings?.[0]?.evidence[0]?.quote, line + '\nassert.equal(actual, expected);');
  assert.equal(result.discardedEvidence?.length, 1);
  await assert.rejects(finalizeReview({findings: [{...finding, evidence: [{path: 'agent-tests/check.js', startLine: 200}]}], coveredRequirements: []}, sandbox), /No verified evidence/);
  await assert.rejects(finalizeReview({findings: [finding, {...finding, evidence: [{path: 'agent-tests/check.js', quote: 'invented second finding'}]}], coveredRequirements: []}, sandbox), /No verified evidence for finding 2/);
  await assert.rejects(finalizeReview({findings: [{...finding, evidence: [{path: 'agent-tests/check.js', startLine: 2, quote: 'const expected = "first\\nsecond";'}]}], coveredRequirements: []}, sandbox), /quote not found/);
});

test('skill resource identity changes with nested scripts and rejects symlink sources', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const root = path.join(f.root, 'resources'); await fs.mkdir(path.join(root, 'assets'), {recursive: true});
  await fs.writeFile(path.join(root, 'SKILL.md'), 'Fixed guidance'); await fs.writeFile(path.join(root, 'assets/run.py'), 'print(1)');
  const before = hash(await resourceDigests(root));
  await fs.writeFile(path.join(root, 'assets/run.py'), 'print(2)');
  assert.notEqual(hash(await resourceDigests(root)), before);
  await fs.symlink(path.join(root, 'SKILL.md'), path.join(root, 'assets/link'));
  await assert.rejects(resourceDigests(root), /symlinks/);
});

test('ablation profiles preserve budgets and model; resources remain frozen across genome application', () => {
  const settings = configSchema.parse({...config(), search: {...config().search, skills: skillIds}});
  const profiles = skillAblationProfiles(settings);
  assert.equal(new Set(profiles.map(p => p.configDigest)).size, 3);
  for (const {config: variant} of profiles) {
    assert.deepEqual(variant.model, settings.model); assert.deepEqual(variant.budget, settings.budget);
    assert.deepEqual(variant.execution, settings.execution); assert.deepEqual(variant.context, settings.context);
    assert.equal(applyGenome(variant, genesis(variant)).search.skillResources, variant.search.skillResources);
  }
  assert.deepEqual(profiles[0]!.config.search.skills, []);
  assert.equal(profiles[1]!.config.search.skillResources, false);
  assert.equal(profiles[2]!.config.search.skillResources, true);
});

test('model sees one current skill region; manual load is compact in canonical history and reviewer cannot prepare harness', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const settings = configSchema.parse({...config(), search: {...config().search, skills: skillIds}});
  const store = new Store(f.out); await store.init(); const sandbox = new LocalSandbox(f.workspace, false);
  const budget = await Budget.open(settings, path.join(f.root, 'budget.json'), 'phase-test', false);
  const history: Parameters<Model['complete']>[0][] = [];
  const model: Model = {complete: async args => {
    history.push(args);
    return history.length === 1 ? completion([['load_skill', {id: 'observable-oracles'}]]) : completion([
      ['write_file', {path: 'agent-tests/run-tests.sh', content: '#!/bin/sh\nexit 1\n'}],
      ['submit_candidate', {sourcePaths: [], requirements: ['unverified fixture'], rationale: 'context test'}],
    ]);
  }};
  const agent = new AgentSession({id: 'phases', role: 'candidate', strategy: '', task: f.task, config: settings, model, sandbox, baseline: sandbox, store, budget});
  await agent.run(2);
  const second = JSON.stringify(history[1]!.messages);
  assert.equal(second.split('Preloaded observable-oracles:').length - 1, 1);
  assert.doesNotMatch(JSON.stringify(agent.messages), /Translate contracts|For each case record requirement/);
  assert.ok(!JSON.stringify(agent.messages).includes(builtinSkills['observable-oracles'].text));
  const reviewModel: Model = {complete: async args => {
    assert.equal('prepare_test_harness' in args.tools, false);
    return completion([['submit_review', {findings: [], coveredRequirements: []}]]);
  }};
  const review = new AgentSession({id: 'review-phase', role: 'reviewer', strategy: '', task: f.task, config: settings, model: reviewModel, sandbox, baseline: sandbox, store, budget});
  await review.run(1); assert.equal(review.review!.accepted, true);
});

test('discovery never installs a framework or loses tools because of task wording or elapsed turns', async t => {
  for (const instruction of ['Django model registration', 'C++ algorithm implementation', 'Update application service state']) {
    const f = await fixture(); t.after(f.cleanup);
    const settings = configSchema.parse({...config(), search: {...config().search, skills: skillIds}});
    const store = new Store(f.out); await store.init(); const sandbox = new LocalSandbox(f.workspace, false);
    const budget = await Budget.open(settings, path.join(f.root, 'budget.json'), instruction, false);
    let calls = 0;
    const model: Model = {complete: async args => {
      calls++;
      if (calls <= 3) {
        assert.ok(args.tools.read_file && args.tools.list_files && args.tools.search);
        assert.deepEqual(await sandbox.list('agent-tests'), [], 'Discovery must not mutate the workspace');
        return completion([[calls === 2 ? 'list_files' : 'read_file', calls === 2 ? {} : {path: 'prime.cjs'}]]);
      }
      return completion([
        ['write_file', {path: 'agent-tests/run-tests.sh', content: '#!/bin/sh\nexit 1\n'}],
        ['submit_candidate', {requirements: ['fixture'], rationale: 'Explicit model artifact action'}],
      ]);
    }};
    const session = new AgentSession({id: 'discovery', role: 'candidate', strategy: '', task: {...f.task, instruction},
      config: settings, model, sandbox, baseline: sandbox, store, budget});
    await session.run(4); assert.ok(session.candidate); assert.equal(calls, 4);
  }
});

test('finalization removes exploratory tools while preserving artifact writes and execution', async t => {
  const f = await fixture(); t.after(f.cleanup);
  const settings = config(), store = new Store(f.out); await store.init(); const sandbox = new LocalSandbox(f.workspace, false);
  const budget = await Budget.open(settings, path.join(f.root, 'final-budget.json'), 'final-tools', false);
  const model: Model = {complete: async args => {
    for (const name of ['read_file', 'list_files', 'search', 'load_skill']) assert.equal(name in args.tools, false);
    assert.ok(args.tools.read_artifact, 'recover scoped evidence by ID even while finalizing');
    for (const name of ['write_file', 'edit_file', 'apply_patch', 'exec', 'submit_candidate']) assert.equal(name in args.tools, true);
    return completion([
      ['write_file', {path: 'agent-tests/run-tests.sh', content: '#!/bin/sh\nexit 1\n'}],
      ['exec', {command: 'bash agent-tests/run-tests.sh'}],
      ['submit_candidate', {sourcePaths: [], requirements: ['fixture'], rationale: 'Finalization remains executable'}],
    ]);
  }};
  const session = new AgentSession({id: 'final-tools', role: 'candidate', strategy: '', task: f.task, config: settings, model, sandbox, baseline: sandbox, store, budget});
  await session.run(1); assert.ok(session.candidate);
});

test('zero-test unittest success cannot serve as execution evidence', () => {
  assert.equal(classify(0, 'Ran 0 tests in 0.000s\n\nOK\n'), 'error');
  assert.equal(classify(0, 'Ran 2 tests in 0.001s\n\nOK\n'), 'pass');
  assert.equal(classify(1, 'Ran 2 tests in 0.001s\n\nFAILED (failures=1)\n'), 'fail');
});
