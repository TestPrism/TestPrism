import test from 'node:test';
import assert from 'node:assert/strict';
import {collectExecutionFeedback, reflectionFeedback} from '../src/evolution-feedback.js';
import {replayLessonMutation} from '../src/lesson-replay.js';
import {evolutionArms} from '../src/evolution-bandit.js';
import {child, genesis} from '../src/genome.js';
import {updateLesson} from '../src/lessons.js';
import {config} from './helpers.js';
import {hash} from '../src/util.js';

test('structured execution feedback keeps failure mechanisms while excluding task literals', () => {
  const feedback = collectExecutionFeedback([
    JSON.stringify({type: 'policy.retrieval', data: {component: 'generation', capabilities: [], maxChars: 1600,
      surface: 'serialization PRIVATE-TASK-42', complete: true, offered: [{id: 'lesson-a', version: 'a'.repeat(64)}]}}),
    JSON.stringify({type: 'workflow.checkpoint', data: {runner: true, executed: false, sourceChanged: false, tokenPressure: true, timePressure: false}}),
    JSON.stringify({type: 'model.error', data: 'context overflow in PRIVATE-TASK-42'}),
  ].join('\n'), {status: 'no_submit', reason: 'context overflow PRIVATE-TASK-42', taskId: 'PRIVATE-TASK-42'});
  assert.deepEqual(feedback.summary.observedFailureClasses, ['context-overflow', 'missing-submission']);
  assert.equal(feedback.summary.components.generation?.offered, 1);
  assert.equal(feedback.summary.milestones.runnerObserved, true);
  assert.doesNotMatch(JSON.stringify(feedback.summary), /PRIVATE-TASK-42|serialization/);
});

test('provider incidents are retained as diagnostics but do not create a capability priority', () => {
  const baseline = genesis(config());
  const execution = collectExecutionFeedback(JSON.stringify({type: 'model.error', data: 'HTTP 503 model unavailable'}),
    {status: 'no_submit', reason: 'provider unavailable', taskId: 's/a'});
  const evaluation: any = {genomeId: baseline.id, split: 'train', seed: 0, taskKeys: ['s/a'], trials: [],
    usage: {input: 1, output: 1, cached: 0, calls: 1}, durationMs: 1, evidenceDigest: hash('feedback'),
    execution: [{taskKey: 's/a', ...execution}]};
  const summary = reflectionFeedback([evaluation]);
  assert.equal(summary.providerAffectedTasks, 1);
  assert.deepEqual(summary.componentPriorities, {});
});

test('lesson replay distinguishes a real prompt intervention from an unreachable trigger', () => {
  const parent = genesis(config());
  const update = {operation: 'upsert' as const, id: 'serialization-anchor', component: 'generation' as const,
    triggers: ['serialization'], when: 'A representation may have equivalent encodings', action: 'Check more than one documented encoding',
    verify: 'Each accepted encoding has an independent observable oracle', rationale: 'Training evidence'};
  const next = child(parent, {...parent.genes, lessons: updateLesson(parent.genes.lessons, update).lessons},
    {component: 'lessons', rationale: 'Training evidence', evidence: ['train'], operator: 'reflection'}, []);
  const base = {genomeId: parent.id, split: 'train' as const, seed: 0, taskKeys: ['s/a'], trials: [],
    usage: {input: 1, output: 1, cached: 0, calls: 1}, durationMs: 1, evidenceDigest: 'base', execution: [{taskKey: 's/a',
      summary: {componentEvents: {}, observedFailureClasses: [], components: {}, milestones: {runnerObserved: false,
        runnerExecuted: false, sourceChangeObserved: false, tokenPressure: false, timePressure: false}, status: 'validated'},
      retrieval: [{component: 'generation' as const, capabilities: [], surface: 'serialization floating point', complete: true, maxChars: 1600}]}]};
  assert.equal(replayLessonMutation(parent, next, [base]).status, 'intervention_observed');
  const missed = structuredClone(base); missed.execution[0].retrieval[0].surface = 'database transactions';
  assert.equal(replayLessonMutation(parent, next, [missed]).status, 'no_observed_intervention');
});

test('mutation focus can isolate one lesson component for a small experiment', () => {
  const parent = genesis(config());
  const arms = evolutionArms(parent, [], [{key: 'a', source: 'local'}, {key: 'b', source: 'local'}],
    {components: ['lessons'], lessonComponents: ['review']});
  assert.deepEqual(arms.map(arm => arm.id), ['lessons:review']);
});
