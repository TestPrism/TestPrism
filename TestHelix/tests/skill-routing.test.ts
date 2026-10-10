import test from 'node:test';
import assert from 'node:assert/strict';
import { builtinSkills, skillIds, skillOffer, type SkillRoute } from '../src/skills.js';
import { skillContext } from '../src/skill-context.js';
import { retrieveLessons, updateLesson, validateLessonDelta } from '../src/lessons.js';
import { child, genesis, applyGenome } from '../src/genome.js';
import { config } from './helpers.js';
import { configSchema } from '../src/types.js';
import { evolutionArms, selectEvolutionArm } from '../src/evolution-bandit.js';

test('context labels do not force specialist injection; all enabled methods remain discoverable', () => {
  const cases = [
    ['integer graph algorithm', 'algorithm', 'small-input-oracles'],
    ['django registration migration schema', 'framework', 'framework-tests'],
    ['transaction cache rollback database', 'stateful', 'stateful-sequences'],
    ['optimize performance memory usage', 'performance', 'performance-controls'],
    ['command line stdout stderr exit code', 'cli', 'cli-artifacts'],
    ['screenshot render image css', 'visual', 'visual-contracts'],
    ['repair compatibility', 'general', 'contract-partitions'],
  ];
  for (const [instruction, context, specialist] of cases) {
    const task = {instruction: instruction!, source: 'local', routes: []};
    const offer = skillOffer(skillIds, false, task);
    assert.equal(offer.context, context); assert.deepEqual(offer.preloaded, ['artifact-workflow']);
    assert.ok(offer.text.length <= 3200); assert.equal(offer.catalog.length, skillIds.length - 3);
    assert.ok(offer.catalog.includes(specialist as keyof typeof builtinSkills));
    assert.ok(!offer.text.includes(builtinSkills[specialist as keyof typeof builtinSkills].text));
    assert.deepEqual(offer, skillOffer(skillIds, false, task));
    assert.equal(offer.ids.length, skillIds.length - 2, 'candidate methods remain discoverable; audit and control review are reviewer-only');
  }
  assert.equal(skillContext('reshape metadata', 'local'), 'general', 'word fragments must not trigger shape, data or graph skills');
  assert.equal(skillContext('', 'from-swe-perf'), 'general');
  assert.equal(skillContext('', 'from-swe-perf/specific-task-id'), 'general', 'source prior must not inspect task IDs');
});

test('text rendering alone does not request visual methods; public framework namespaces remain discoverable', () => {
  assert.equal(skillContext('Render the interpolated validation error message as plain text.'), 'general');
  assert.equal(skillContext('Validation errors must render the input. See https://docs.djangoproject.com/en/ref/validators/'), 'framework');
  assert.equal(skillContext('Render the pixels correctly on a canvas.'), 'visual');
  assert.equal(skillContext('Use the public django/db/models manager methods.'), 'framework');
});

test('frozen route overrides, no-specialist control and reviewer isolation affect actual preloaded content', () => {
  const task = {instruction: 'integer algorithm', source: 'local', routes: [{context: 'algorithm', skill: null}] as SkillRoute[]};
  const control = skillOffer(skillIds, false, task);
  assert.deepEqual(control.preloaded, ['artifact-workflow']); assert.ok(!control.text.includes(builtinSkills['small-input-oracles'].text));
  task.routes[0]!.skill = 'contract-partitions';
  const changed = skillOffer(skillIds, false, task);
  assert.deepEqual(changed.preloaded, ['artifact-workflow', 'contract-partitions']);
  assert.notEqual(changed.digest, control.digest);
  assert.deepEqual(skillOffer(skillIds, true, task).ids, ['observable-oracles', 'failure-triage', 'contract-audit', 'control-evidence']);
  assert.deepEqual(skillOffer(skillIds, true, task).preloaded, ['contract-audit', 'control-evidence']);
  assert.equal(skillOffer([], false, task).text, '');
  assert.throws(() => configSchema.parse({...config(), search: {skills: [], skillRoutes: task.routes}}), /enabled/);
  assert.throws(() => configSchema.parse({...config(), search: {skills: skillIds, skillRoutes: [task.routes[0], task.routes[0]]}}), /Duplicate/);
  const settings = configSchema.parse({...config(), search: {...config().search, skills: skillIds}}), baseline = genesis(settings);
  const variant = child(baseline, {...baseline.genes, skillRoutes: task.routes},
    {component: 'skillRoutes', evidence: ['train'], rationale: 'Compare a preload policy', operator: 'catalog'}, []);
  const deployed = applyGenome(settings, variant);
  assert.deepEqual(deployed.search.skills, settings.search.skills); assert.deepEqual(deployed.budget, settings.budget);
  assert.deepEqual(deployed.search.skillRoutes, task.routes);
});

test('lesson retrieval rejects broad and substring matches, diversifies and keeps whole bounded lessons', () => {
  const lessons = [
    {id: 'generic', triggers: ['bug', 'fix'], text: 'GENERIC SHOULD NOT LOAD'},
    {id: 'substring', triggers: ['sql'], text: 'SUBSTRING SHOULD NOT LOAD'},
    {id: 'parser', triggers: ['serialization'], text: 'Check parser round trips against independent anchors rather than paired transformations alone.'},
    {id: 'duplicate', triggers: ['serialization'], text: 'Check parser round trips against independent anchors rather than paired transformations alone. Again.'},
    {id: 'numeric', triggers: ['floating point'], text: 'Inspect precision before choosing a numeric tolerance and verify non-finite results explicitly.'},
  ];
  const selected = retrieveLessons(lessons, 'Fix a serialization bug involving floating point in postgresql');
  assert.deepEqual(new Set(selected.map(lesson => lesson.id)), new Set(['parser', 'numeric']));
  assert.deepEqual(retrieveLessons(lessons, 'fix bug'), []);
  assert.equal(retrieveLessons(lessons, 'serialization floating point', 100).length, 1);
  assert.equal(retrieveLessons(lessons, 'serialization floating point', 10).length, 0);
});

test('host applies one structured lesson delta, preserves unrelated experience and checks copying/duplication guards', () => {
  const old = [{id: 'keep', triggers: ['transaction'], text: 'Verify committed and rolled back state using independent public reads.'}];
  const result = updateLesson(old, {operation: 'upsert', id: 'new', triggers: ['serialization'],
    when: 'Round-trip tests can hide coupled mistakes.', action: 'Add independently justified anchors.', verify: 'Both the public example and round trip hold.', rationale: 'Training indicates coupled behavior'});
  assert.deepEqual(result.lessons[0], old[0]); assert.equal(old.length, 1);
  assert.match(result.lessons[1]!.text, /When:.*\nAction:.*\nVerify:/);
  assert.throws(() => validateLessonDelta(result.lessons, []), /one lesson/);
  assert.throws(() => updateLesson(old, {operation: 'remove', id: 'absent', rationale: 'Absent'}), /one lesson/);
  assert.throws(() => updateLesson(old, {operation: 'upsert', id: 'new', triggers: ['bug'], when: 'When.', action: 'Action.', verify: 'Verify.', rationale: 'Training'}), /specific/);
  assert.throws(() => validateLessonDelta([], [{id: 'empty', triggers: ['!!!', 'fix bug'], text: 'No usable retrieval condition.'}]), /specific/);
  assert.throws(() => validateLessonDelta(old, [...old, {...old[0]!, id: 'duplicate'}]), /duplicates/);
  const baseline = genesis(config());
  assert.throws(() => child(baseline, {...baseline.genes, guidance: 'Use ProjectBug123 expected answer.'},
    {component: 'guidance', rationale: 'Training', evidence: ['train'], operator: 'reflection'}, ['source/project-bug-123']), /identifier/);
});

test('UCB cold start balances mutation families, includes abstention and avoids one-task specialist buckets', () => {
  const base = genesis(config()), samples = [{key: 's/a', source: 's', context: 'algorithm' as const}, {key: 's/b', source: 's', context: 'algorithm' as const}];
  const arms = evolutionArms(base, skillIds, samples);
  assert.ok(arms.some(arm => arm.id === 'route:algorithm:none'));
  assert.equal(evolutionArms(base, skillIds, samples.slice(0, 1)).some(arm => arm.context), false);
  const first = selectEvolutionArm(arms, {}, [], [], samples); assert.equal(first.arm.component, 'skillRoutes');
  assert.equal(first.statistics[0]!.score, null, 'untried scores serialize explicitly without Infinity');
  const second = selectEvolutionArm(arms, {[first.arm.id]: 1}, [], [], samples); assert.equal(second.arm.component, 'guidance');
  const third = selectEvolutionArm(arms, {[first.arm.id]: 1, guidance: 1}, [], [], samples); assert.equal(third.arm.component, 'lessons');
});
