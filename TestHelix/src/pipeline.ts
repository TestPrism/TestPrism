import { promises as fs } from 'node:fs';
import path from 'node:path';
import { AgentSession } from './agent.js';
import { mergeTests } from './artifacts.js';
import { Budget, type BudgetFloor } from './budget.js';
import { SDKModel } from './model.js';
import { applyFiles, createFactory, LocalSandbox, publishTests, type Sandbox, type SandboxFactory } from './runtime.js';
import { correctionCandidates, select } from './selection.js';
import { Store } from './store.js';
import type { Candidate, Config, Model, Observation, Review, RunResult, Task } from './types.js';
import { errorText, hash, implementationDigest, mapLimit, readJSON } from './util.js';
import { Validator, sameFailure } from './validation.js';
import { withRunLock } from './lock.js';
import {reviewSelection} from './review-stage.js';
import {planTask, planBrief, observeTaskEnvironment} from './planner.js';
import {providerAdmission} from './provider-health.js';
import {failureLabels} from './component-feedback.js';
import {hypothesisDifference} from './differential-stage.js';
import {SearchBudget, addFloor} from './search-budget.js';

const strategies = [
  'Write a small public-behavior regression early, then develop a minimal source repair and expand the suite with an independent small-input oracle where possible.',
  'Independently solve the complete task. Derive a small oracle or boundary table from public examples, mathematics or documented behavior before implementing the repair. Check one ordinary supported case as well as the regression. Choose the repair independently of the test oracle. Inspect uncertain expected values against the original contract; do not inherit another solver\'s plan or assumptions.',
  'Challenge likely incomplete implementations. Cover explicit requirements overlooked by narrow tests and avoid adding rules the task does not state.',
  'Look for alternative correct representations and algorithms. Produce portable semantic tests that still reject plausible near misses.',
];
interface Checkpoint { identity: string; candidates: Candidate[]; reviews: Record<string, Review>; nextIndex: number; round: number; attemptedCorrections?: string[]; differentialAudited?: boolean }

type RunOptions = {resume?: boolean; model?: Model; plannerModel?: Model; factory?: SandboxFactory};
export async function runTask(task: Task, config: Config, outputDir: string, options: RunOptions = {}): Promise<RunResult> {
  return withRunLock(outputDir, () => runTaskUnlocked(task, config, outputDir, options));
}
async function runTaskUnlocked(task: Task, config: Config, outputDir: string, options: RunOptions): Promise<RunResult> {
  outputDir = path.resolve(outputDir);
  const store = new Store(outputDir); await store.init();
  const taskDigest = hash(task), configDigest = hash(config), implementation = await implementationDigest();
  const identity = hash({taskDigest, configDigest, implementation});
  const finished = options.resume ? await readJSON<RunResult>(path.join(outputDir, 'result.json')) : undefined;
  if (finished) {
    if (finished.taskDigest !== taskDigest || finished.configDigest !== configDigest || finished.implementationDigest !== implementation)
      throw new Error('Completed run identity mismatch');
    return finished;
  }
  const checkpoint = options.resume ? await readJSON<Checkpoint>(path.join(outputDir, 'checkpoint.json')) : undefined;
  if (checkpoint && checkpoint.identity !== identity) throw new Error('Checkpoint task/configuration mismatch');
  if (!options.resume && await readJSON(path.join(outputDir, 'budget.json'))) throw new Error('Run directory already exists; use --resume or a new directory');
  const budget = await Budget.open(config, path.join(outputDir, 'budget.json'), identity, !!options.resume);
  const model = options.model ?? new SDKModel(config);
  if(!options.model)try {
    await providerAdmission(config,false,AbortSignal.timeout(Math.max(1,Math.min(config.model.timeoutMs,budget.remainingMs))));
  } catch(error) {
    await store.event('run.admission_failed', {requestSent:false, failureClasses:failureLabels(errorText(error))});
    throw error;
  }
  const factory = options.factory ?? await createFactory(task, config, path.join(outputDir, 'workspaces'), () => budget.remainingMs);
  let baseline: Sandbox | undefined, finalResult: RunResult | undefined;
  try {
    const candidates = checkpoint?.candidates ?? [], reviews = checkpoint?.reviews ?? {};
    const sessions = new Map<string, AgentSession>();
    const attemptedCorrections = new Set(checkpoint?.attemptedCorrections ?? []);
    let differentialAudited = checkpoint?.differentialAudited ?? false;
    let nextIndex = checkpoint?.nextIndex ?? 0, matrix: Observation[] = [], reason = 'No candidate passed executable verification';
    let result: RunResult | undefined;
    const save = (round: number) => store.save('checkpoint.json', {identity, candidates, reviews, nextIndex, round,
      attemptedCorrections: [...attemptedCorrections], differentialAudited});
    await store.save('run.json', {schemaVersion: 1, taskDigest, configDigest, implementationDigest: implementation, task: {id: task.id, source: task.source}, config});
    await store.event('run.started', {task: task.id, resumed: !!checkpoint});
    baseline = await factory.fork('baseline-reference');
    const environment = await observeTaskEnvironment(task, config, baseline, budget, store);
    const plannerModel = config.planner.enabled ? options.plannerModel ?? options.model ?? new SDKModel(config, 'planner') : undefined;
    let taskPlan = plannerModel ? await planTask(task, config, baseline, budget, store, plannerModel) : undefined;
    const validator = new Validator(factory, {...config, execution: {...config.execution,
      testTimeoutMs: Math.min(config.execution.testTimeoutMs, task.testTimeoutMs ?? Infinity)}}, store, budget, identity);
    const sameBaseline = (candidate: Candidate, observation: Observation) => observation.status === 'fail'
      && matrix.some(cell => cell.testId === candidate.id && cell.sourceId === 'baseline'
        && sameFailure(cell, observation, baseline!.root, candidate.tests));
    const staged = config.search.schedule === 'staged';
    const allocation = new SearchBudget(config, budget);
    const qualified = (candidate: Candidate) => candidate.source.length > 0
      && matrix.some(cell => cell.testId === candidate.id && cell.sourceId === 'baseline' && cell.status === 'fail')
      && matrix.some(cell => cell.testId === candidate.id && cell.sourceId === candidate.id && cell.status === 'pass');
    const branchStrategy = (index: number) => index === 0 && taskPlan ? planBrief(taskPlan, 0) : strategies[index % strategies.length]!;
    // Reuse the existing atomic budget floor. Serial branches reserve a fair
    // share for unstarted peers, including summaries/retries, before any review.
    const runBranches = async <T>(items: T[], stage: string, maxTurns: number,
      work: (item: T, turns: number, floor: BudgetFloor, deadline?: number) => Promise<void>,
      admit?: (wave: T[]) => Promise<void>) => {
      const review = allocation.review(items.length);
      const correction = ((stage === 'initial' && items.length > 1) || stage === 'differential')
        ? allocation.correction() : {calls: 0, input: 0, output: 0};
      const tail = addFloor(review, correction);
      for (let offset = 0; offset < items.length; offset += config.budget.concurrency) {
        const wave = items.slice(offset, offset + config.budget.concurrency), remaining = items.length - offset;
        const {floor, turns, deadline} = allocation.wave(remaining, wave.length, maxTurns, tail);
        await store.event('budget.allocation', {stage, branches: wave.length, remainingBranches: remaining, turns,
          reviewReserve: review.calls, correctionReserve: correction.calls, reserveForReview: floor, deadline});
        if (turns < 1 || budget.remainingMs < 1000) break;
        await admit?.(wave);
        await mapLimit(wave, config.budget.concurrency, item => work(item, turns, floor, deadline));
      }
    };
    const generate = async (index: number, turns = config.search.agentSteps, reserveForReview = allocation.review(),
      deadline = allocation.wave(1, 1, turns, reserveForReview).deadline) => {
      const sessionId = `candidate-${index + 1}`;
      try {
        const sandbox = await factory.fork(sessionId);
        const session = new AgentSession({id: sessionId, role: 'candidate', strategy: branchStrategy(index), task,
          config, model, sandbox, baseline: baseline!, store, budget, reserveForReview, deadline, environment});
        sessions.set(sessionId, session);
        await session.run(turns);
        if (session.candidate) candidates.push(session.candidate);
      } catch (error) { reason = errorText(error); await store.event('candidate.failed', {session: sessionId, error: reason}); }
    };
    const sessionFor = async (parent: Candidate) => {
      let session = sessions.get(parent.sessionId);
      if (!session) {
        const sandbox = await factory.fork(`resume-${parent.id}`);
        await applyFiles(sandbox, parent.source); await applyFiles(sandbox, parent.tests);
        session = new AgentSession({id: parent.sessionId, role: 'candidate', strategy: parent.strategy, task, config, model,
          sandbox, baseline: baseline!, store, budget, environment});
        await session.restoreSealed(parent);
        sessions.set(parent.sessionId, session);
      }
      return session;
    };
    if (nextIndex < config.search.initialCandidates) {
      const indices = Array.from({length: config.search.initialCandidates - nextIndex}, (_, i) => i + nextIndex);
      nextIndex = config.search.initialCandidates;
      await runBranches(indices, 'initial', config.search.agentSteps, generate); await save(0);
    }
    for (let round = checkpoint?.round ?? 0; round < config.search.maxRounds; round++) {
      candidates.sort((a, b) => a.lineage.localeCompare(b.lineage));
      matrix = await validator.matrix(candidates);
      // Review gets bounded resources; a still-unqualified primary keeps the
      // rest for correction. There is no speculative peer reservation to release.
      const reserve = round + 1 < config.search.maxRounds ? allocation.correction() : undefined;
      let selection = config.search.review ? await reviewSelection({candidates, matrix, reviews, task, config, model,
        factory, baseline, budget, store, round, reserve, environment, checkpoint: () => save(round)})
        : select(candidates, matrix, config.search.minIndependentSupport, reviews);
      // A semantic review validates the test contract, not the candidate's
      // first repair.  If the baseline-failing suite is reviewed but its own
      // repair is missing or failed, still spend the bounded peer slot on an
      // independent repair.  The old `qualified` gate stranded precisely
      // these useful regressions: no peer could rescue a bad first hypothesis,
      // and the official scorer saw no selected tests.
      const reviewReadyBaseline = candidates.some(candidate =>
        matrix.some(cell => cell.testId === candidate.id && cell.sourceId === 'baseline' && cell.status === 'fail')
        && reviews[candidate.testsHash]?.accepted === true);
      if (staged && nextIndex < config.search.maxCandidates && reviewReadyBaseline) {
        const index = nextIndex++;
        if (allocation.canChallenge(config.search.challengeSteps)) {
          const floor = addFloor(allocation.review(), allocation.correction());
          const turns = config.search.challengeSteps;
          await store.event('challenge.started', {round, turns, primary: candidates[0]?.id, reserve: floor});
          await generate(index, turns, floor, Date.now() + Math.floor(budget.remainingMs * .7));
          matrix = await validator.matrix(candidates);
          selection = await reviewSelection({candidates, matrix, reviews, task, config, model, factory, baseline, budget, store,
            round, environment, reserve: round + 1 < config.search.maxRounds ? allocation.correction() : undefined, checkpoint: () => save(round)});
        } else await store.event('challenge.skipped', {reason: 'Insufficient allowance for an independent attempt and review',
          remaining: {calls: budget.remainingCalls, input: budget.remainingInputTokens, output: budget.remainingOutputTokens, ms: budget.remainingMs}});
      }
      if (staged && config.search.differentialSteps > 0 && !differentialAudited && candidates.length === 2
        && candidates.every(candidate => qualified(candidate) && reviews[candidate.testsHash]?.accepted === true)
        && candidates[0]!.sourceHash !== candidates[1]!.sourceHash
        && matrix.filter(cell => cell.sourceId !== 'baseline').every(cell => cell.status === 'pass')) {
        differentialAudited = true; await save(round);
        if (round + 1 < config.search.maxRounds && allocation.canChallenge(config.search.differentialSteps)) {
          const parent = candidates[0]!, difference = await hypothesisDifference(parent, candidates[1]!, baseline);
          let revised: Candidate | undefined;
          await store.event('differential.started', {candidate: parent.id, peer: candidates[1]!.id, maxTurns: config.search.differentialSteps});
          await runBranches([parent], 'differential', config.search.differentialSteps, async (_parent, turns, floor, deadline) => {
            try {
              const session = await sessionFor(parent);
              session.options.reserveForReview = floor;
              session.options.deadline = deadline ?? Date.now() + Math.floor(budget.remainingMs * .5);
              await session.run(turns, Math.max(round, parent.round) + 1, `Differential coverage challenge: the existing suites agree, but the two repairs below are untrusted hypotheses. Find a small supported input where their branches may behave differently. Derive the expected result from the original task and public documentation, never from either repair. Check interactions between changed branches and ordinary supported inputs. Add a discriminating regression if justified, run it on your existing source, and submit. Preserve existing justified assertions; do not copy the peer repair or invent requirements. If no justified distinction is found, submit the existing artifacts. Any new test will receive separate source-blind review.\n${difference}`);
              revised = session.candidate;
            } catch (error) {await store.event('differential.failed', {candidate: parent.id, error: errorText(error)});}
          });
          // Keep the sealed primary in the checkpoint until the added coverage
          // has executable and semantic evidence. Interrupted or invalid audits
          // must not discard previously qualified work.
          let accepted = false;
          if (revised) {
            const proposed = candidates.map(candidate => candidate === parent ? revised! : candidate);
            const proposedMatrix = await validator.matrix(proposed);
            if (proposedMatrix.some(cell => cell.testId === revised!.id && cell.sourceId === 'baseline' && cell.status === 'fail')) {
              const proposedSelection = await reviewSelection({candidates: proposed, matrix: proposedMatrix, reviews, task,
                config, model, factory, baseline, budget, store, round, environment,
                reserve: allocation.correction(), checkpoint: () => save(round)});
              if (reviews[revised.testsHash]?.accepted === true) {
                candidates.splice(candidates.indexOf(parent), 1, revised);
                matrix = proposedMatrix; selection = proposedSelection; accepted = true;
              }
            }
          }
          if (!accepted) {
            await store.event('differential.rejected', {candidate: revised?.id,
              reason: 'No reproducible, semantically approved coverage; retain the sealed primary'});
            await sessions.get(parent.sessionId)?.options.sandbox.close(); sessions.delete(parent.sessionId);
          }
        } else await store.event('differential.skipped', {reason: 'Insufficient budget for a coverage challenge plus review/correction'});
      }
      await save(round);
      const reviewed = !config.search.review || selection.tests.every(candidate => reviews[candidate.testsHash]?.accepted === true);
      const correctionKey = (candidate: Candidate) => hash({lineage: candidate.lineage, source: candidate.sourceHash,
        tests: candidate.testsHash, review: reviews[candidate.testsHash],
        failures: matrix.filter(cell => cell.status !== 'pass' && (cell.testId === candidate.id || cell.sourceId === candidate.id))
          .map(cell => ({tests: candidates.find(test => test.id === cell.testId)?.testsHash,
            source: candidates.find(source => source.id === cell.sourceId)?.sourceHash ?? 'baseline',
            status: cell.status, signature: cell.signature}))});
      const actionable = correctionCandidates(candidates, matrix, reviews);
      const repair = actionable.filter(candidate => !attemptedCorrections.has(correctionKey(candidate)));
      for (const candidate of actionable.filter(candidate => !repair.includes(candidate)))
        await store.event('correction.skipped', {candidate: candidate.id, reason: 'Unchanged artifacts and failure evidence already received a correction attempt'});
      const canCorrect = round + 1 < config.search.maxRounds && budget.remainingCalls >= 2 && budget.remainingMs >= 1000;
      const unresolvedCounterexample = selection.tests.length > 0 && selection.witnesses.some(source =>
        candidates.some(test => !selection.tests.includes(test) && reviews[test.testsHash]?.accepted === true
          && matrix.some(cell => cell.testId === test.id && cell.sourceId === 'baseline' && cell.status === 'fail')
          && matrix.some(cell => cell.testId === test.id && cell.sourceId === source.id && cell.status === 'fail')));
      const counterexample = canCorrect && unresolvedCounterexample && repair.some(source => selection.witnesses.includes(source)
        && candidates.some(test => !selection.tests.includes(test) && reviews[test.testsHash]?.accepted === true
          && matrix.some(cell => cell.testId === test.id && cell.sourceId === source.id && cell.status === 'fail')));
      if (counterexample) await store.event('selection.counterexample', {reason: 'A reviewed test challenges a prospective witness; attempt bounded correction before exporting the weaker subset'});
      if (selection.tests.length && reviewed && !unresolvedCounterexample) {
        const tests = mergeTests(selection.tests);
        const baselineResult = await validator.evaluate(tests, [], 'merged', 'baseline', config.execution.repeat, {fresh: true});
        const baselineValid = baselineResult.status === 'fail'
          && (selection.tests.length > 1 || sameBaseline(selection.tests[0]!, baselineResult));
        const witnesses = baselineValid ? await mapLimit(selection.witnesses, config.execution.concurrency, candidate => validator.evaluate(
          tests, candidate.source, 'merged', candidate.id, config.execution.repeat, {fresh: true, replayScript:candidate.replayScript})) : [];
        matrix.push(baselineResult, ...witnesses);
        if (baselineValid && witnesses.length && witnesses.every(x => x.status === 'pass')) {
          result = await finish('validated', 'Independent repairs accept the merged suite, baseline fails; '
            + (config.search.review ? 'semantic review passed' : 'semantic review disabled by configuration'), selection.tests, selection.witnesses);
          break;
        }
        reason = 'Merged tests interfered or failed final fresh validation';
        await store.event('merge.rejected', {baseline: baselineResult.status, witnesses: witnesses.map(x => x.status)});
      }
      if (round + 1 >= config.search.maxRounds || budget.remainingCalls < 2 || budget.remainingMs < 1000) break;
      const replanning = sessions.get('candidate-1')?.replanRequest;
      if (plannerModel && replanning) {
        taskPlan = await planTask(task, config, baseline, budget, store, plannerModel, JSON.stringify(replanning));
        const session = sessions.get('candidate-1')!; session.replanRequest = undefined;
        if (taskPlan) session.messages.push({role: 'user', content: planBrief(taskPlan, 0)});
      }
      await save(round);
      await runBranches(repair, 'repair', config.search.repairSteps, async (parent, repairTurns, reserveForReview, deadline) => {
        try {
          const session = await sessionFor(parent);
          session.options.reserveForReview = reserveForReview;
          session.options.deadline = deadline;
          // Grant only the host execution evidence referenced by this correction.
          // Private candidate/reviewer archives stay inside their session scopes.
          const artifacts = new Set(matrix.filter(item => (item.testId === parent.id || item.sourceId === parent.id)
            && item.status !== 'pass').flatMap(item => [item.artifact, ...(item.reproducibility?.runs.map(run => run.artifact) ?? [])]
              .filter((artifact): artifact is string => !!artifact)));
          for (const artifact of artifacts) await session.options.store.artifact(await store.readArtifact(artifact));
          // Share a reproducible test only after independent generation and
          // semantic approval. Never share the peer's repair or private notes.
          const counterexamples = [];
          for (const test of candidates.filter(test => test.lineage !== parent.lineage && reviews[test.testsHash]?.accepted === true
            && matrix.some(cell => cell.testId === test.id && cell.sourceId === parent.id && cell.status === 'fail'))) {
            const artifact = await session.options.store.artifact(JSON.stringify({testId: test.id, testsHash: test.testsHash,
              files: test.tests.map(file => {
                const bytes = Buffer.from(file.data, 'base64'), content = bytes.toString('utf8');
                const utf8 = Buffer.from(content, 'utf8').equals(bytes);
                return {path: file.path, content: utf8 ? content : file.data, encoding: utf8 ? 'utf8' : 'base64', mode: file.mode};
              })}));
            counterexamples.push({testId: test.id, artifact});
          }
          await session.run(repairTurns, Math.max(round, parent.round) + 1,
            `Your previous source and tests are still present. Repair the specific failures below.\n${selection.feedback[parent.lineage]}`
            + (counterexamples.length ? `\nReviewed counterexample suites (read_artifact; preserve your own tests, replay separately): ${JSON.stringify(counterexamples)}` : ''));
          if (session.candidate) candidates.splice(candidates.indexOf(parent), 1, session.candidate);
        } catch (error) { await store.event('correction.failed', {parent: parent.id, error: errorText(error)}); }
      }, async wave => {
        // Persist only admitted attempts, once before parallel work. A zero-turn
        // allocation must remain eligible if a protected allowance is released.
        for (const parent of wave) attemptedCorrections.add(correctionKey(parent));
        await save(round);
      });
      if (!staged && nextIndex < config.search.maxCandidates && budget.remainingCalls >= 4 && budget.remainingMs > 1000) {
        const index = nextIndex++; await generate(index, Math.max(1, Math.min(config.search.agentSteps, budget.remainingCalls - (config.search.review ? 2 : 0))));
      }
      await save(round + 1);
    }
    if (!result) {
      const selection = select(candidates, matrix, config.search.minIndependentSupport, reviews);
      const diagnostic = config.search.provisionalPolicy === 'baseline-only';
      const requireRepairControl = config.search.provisionalPolicy === 'reviewed-positive';
      const controls = candidates.filter(candidate => candidate.source.length
        && matrix.some(cell => cell.testId === candidate.id && cell.sourceId === 'baseline' && cell.status === 'fail')
        && matrix.some(cell => cell.testId === candidate.id && cell.sourceId === candidate.id && cell.status === 'pass'));
      const choices = selection.ranked.map(candidate => ({candidate, control: controls.find(control =>
        matrix.some(cell => cell.testId === candidate.id && cell.sourceId === control.id && cell.status === 'pass'))}))
        .filter(({candidate, control}) => diagnostic || (reviews[candidate.testsHash]?.accepted === true
          && (!requireRepairControl || Boolean(control))));
      // Prefer a reviewed regression that exposes a witnessed near miss if a
      // conflict could not be repaired. Keep the honest provisional status.
      const challenges = (candidate: Candidate) => new Set(controls.filter(source => source.lineage !== candidate.lineage
        && matrix.some(cell => cell.testId === candidate.id && cell.sourceId === source.id && cell.status === 'fail'))
        .map(source => source.sourceHash)).size;
      choices.sort((a, b) => challenges(b.candidate) - challenges(a.candidate));
      if (config.search.allowProvisional && selection.provisional && !choices.length)
        reason = 'Provisional publication requires an accepted semantic review and a passing source repair control';
      for (const {candidate, control} of config.search.allowProvisional ? choices : []) {
        const baselineResult = await validator.evaluate(candidate.tests, [], candidate.id, 'baseline', config.execution.repeat, {fresh: true});
        const stable = sameBaseline(candidate, baselineResult);
        matrix = matrix.filter(cell => cell.testId !== candidate.id || cell.sourceId !== 'baseline');
        matrix.push(baselineResult);
        if (!stable) {
          reason = 'Final fresh baseline validation did not reproduce the qualified failure';
          await store.event('provisional.rejected', {candidate: candidate.id, reason});
          continue;
        }
        if (!diagnostic && requireRepairControl && control) {
          const positive = await validator.evaluate(candidate.tests, control.source, candidate.id, control.id, config.execution.repeat,
            {fresh: true, replayScript:control.replayScript});
          matrix = matrix.filter(cell => cell.testId !== candidate.id || cell.sourceId !== control.id);
          matrix.push(positive);
          if (positive.status !== 'pass') {
            reason = 'Final fresh source repair control did not pass the provisional suite';
            await store.event('provisional.rejected', {candidate: candidate.id, control: control.id, reason});
            continue;
          }
        }
        result = await finish('provisional', diagnostic
          ? 'DIAGNOSTIC baseline-only export: stable baseline failure without required semantic approval or passing repair control; correctness is unconfirmed'
          : requireRepairControl
            ? 'Semantic review accepted; fresh baseline fails and a fresh repair control passes, but independent support is insufficient; benchmark correctness is unconfirmed'
            : 'Semantic review accepted; fresh baseline fails. Source repair control was optional under reviewed-baseline; independent support is insufficient and benchmark correctness is unconfirmed', [candidate], []);
        break;
      }
      result ??= await finish('no_submit', reason, [], []);
    }
    finalResult = result;

    async function finish(status: RunResult['status'], why: string, selected: Candidate[], witnesses: Candidate[]): Promise<RunResult> {
      const files = mergeTests(selected);
      const testsDir = files.length ? path.join(outputDir, 'submission', 'agent-tests') : null;
      const submission = path.join(outputDir, 'submission');
      await fs.mkdir(submission, {recursive: true});
      await publishTests(new LocalSandbox(submission, false), files);
      await factory.publish(files);
      return {schemaVersion: 1, taskId: task.id, status, reason: why, testsDir, selected: selected.map(x => x.id), witnesses: witnesses.map(x => x.id),
        candidates: [...candidates], matrix, reviews, usage: budget.snapshot(), elapsedMs: budget.elapsedMs, taskDigest, configDigest,
        implementationDigest: implementation};
    }
  } finally {
    try { await baseline?.close(); }
    finally { await factory.close(); }
  }
  if (!finalResult) throw new Error('Run ended without a result');
  finalResult.elapsedMs = budget.elapsedMs;
  await store.save('result.json', finalResult); await store.event('run.completed', {status: finalResult.status, usage: finalResult.usage});
  return finalResult;
}
