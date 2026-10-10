import {AgentSession} from './agent.js';
import type {Budget, BudgetFloor} from './budget.js';
import {executionEvidence} from './execution-evidence.js';
import {applyFiles, type Sandbox, type SandboxFactory} from './runtime.js';
import {select} from './selection.js';
import type {Store} from './store.js';
import type {Candidate, Config, Model, Observation, Review, Task} from './types.js';
import {errorText, hash, mapLimit} from './util.js';
import type {EnvironmentObservation} from './planner.js';

function crossSymptoms(candidate: Candidate, candidates: Candidate[], matrix: Observation[]) {
  // Content-based evidence: source IDs, raw exception messages and repair code
  // never affect the review prompt or cause duplicate audits of the same symptom.
  const testIds = new Set(candidates.filter(test => test.testsHash === candidate.testsHash).map(test => test.id));
  const symptoms = matrix.filter(item => testIds.has(item.testId) && item.sourceId !== 'baseline'
    && item.sourceId !== item.testId && item.status === 'fail').slice(0, 2).flatMap(item =>
      (item.diagnostics ?? executionEvidence(item.output)).failures.filter(failure => failure.needsAttribution).slice(0, 3)
        .map(failure => ({exception: failure.exception.slice(0, 80), testFrames: failure.frames.flatMap(frame => {
          const file = candidate.tests.find(file => frame.path === file.path || frame.path.endsWith('/' + file.path));
          return file ? [{path: file.path, line: frame.line}] : [];
        }).slice(0, 2)}))).filter(symptom => symptom.testFrames.length);
  return [...new Map(symptoms.map(symptom => [JSON.stringify(symptom), symptom])).entries()]
    .sort(([a], [b]) => a.localeCompare(b)).map(([, symptom]) => symptom);
}

/** Re-select after rejections, but never reopen the same test content within a
 * round. A failed review cannot cause an unbounded loop or duplicate spending. */
export async function reviewSelection(options: {
  candidates: Candidate[]; matrix: Observation[]; reviews: Record<string, Review>;
  task: Task; config: Config; model: Model; factory: SandboxFactory; baseline: Sandbox;
  budget: Budget; store: Store; round: number; checkpoint: () => Promise<void>;
  reserve?: BudgetFloor;
  environment?: EnvironmentObservation;
}) {
  const {candidates, matrix, reviews, task, config, model, factory, baseline, budget, store, round} = options;
  const reserve = options.reserve ?? {calls: 0, input: 0, output: 0};
  const cross = new Map(candidates.map(candidate => [candidate.testsHash, crossSymptoms(candidate, candidates, matrix)]));
  for (const candidate of candidates) {
    const symptoms = cross.get(candidate.testsHash)!, prior = reviews[candidate.testsHash];
    if (symptoms.length && prior?.accepted && prior.crossEvidenceHash !== hash(symptoms)) {
      reviews[candidate.testsHash] = {...prior, accepted: false, resolution: 'needs-evidence',
        concerns: [...prior.concerns, 'New cross-control test exceptions require public API attribution.']};
      await store.event('review.evidence_changed', {candidate: candidate.id, evidenceHash: hash(symptoms)});
    }
    const cell = matrix.find(cell => cell.testId === candidate.id && cell.sourceId === 'baseline');
    if (cell?.status !== 'fail') continue;
    cell.diagnostics ??= executionEvidence(cell.output);
    const unresolved = cell.diagnostics.failures.filter(failure => failure.needsAttribution);
    if (unresolved.length && !reviews[candidate.testsHash]) {
      reviews[candidate.testsHash] = {accepted: false, resolution: 'needs-evidence',
        concerns: ['Host detected baseline exceptions/no-work symptoms requiring execution attribution: ' + unresolved.map(failure => failure.id).join(', ')],
        missingRequirements: [], coveredRequirements: []};
      await store.event('review.execution_pending', {candidate: candidate.id, failures: unresolved.map(failure => failure.id)});
    }
  }
  const attempted = new Set<string>();
  let selection = select(candidates, matrix, config.search.minIndependentSupport, reviews);
  while (budget.remainingCalls > reserve.calls && budget.remainingMs >= 1000
    && budget.remainingInputTokens > reserve.input && budget.remainingOutputTokens - reserve.output >= 128) {
    const targets = [...(selection.tests.length ? selection.tests : selection.provisional ? [selection.provisional] : []),
      // A good test may expose an incomplete peer repair. Review its public
      // basis even when that disagreement prevents independent support.
      ...candidates.filter(candidate => matrix.some(cell => cell.testId === candidate.id && cell.sourceId === 'baseline' && cell.status === 'fail'))];
    const pending = [...new Map(targets.filter(candidate => !attempted.has(candidate.testsHash)
      && (!reviews[candidate.testsHash] || reviews[candidate.testsHash]?.resolution === 'needs-evidence'))
      .map(candidate => [candidate.testsHash, candidate])).values()].slice(0, budget.remainingCalls - reserve.calls);
    if (!pending.length) break;
    const turns = Math.min(4, Math.floor((budget.remainingCalls - reserve.calls) / pending.length));
    pending.forEach(candidate => attempted.add(candidate.testsHash));
    try {
      await mapLimit(pending, config.budget.concurrency, async candidate => {
        if (budget.remainingCalls < 1 || budget.remainingMs < 1000) return;
        let sandbox: Sandbox | undefined, reviewer: AgentSession | undefined;
        try {
          sandbox = await factory.fork(`review-${candidate.id}`);
          await applyFiles(sandbox, candidate.tests);
          const prior = reviews[candidate.testsHash]?.resolution === 'needs-evidence' ? reviews[candidate.testsHash] : undefined;
          const cell = matrix.find(cell => cell.testId === candidate.id && cell.sourceId === 'baseline');
          // A baseline exception can hide a later fixture defect. Other repair
          // controls expose useful symptoms, but neither their source excerpts
          // nor arbitrary exception messages belong in this source-blind review.
          const symptoms = cross.get(candidate.testsHash)!;
          reviewer = new AgentSession({id: `review-${candidate.id}${prior ? `-evidence-${round}` : ''}`, role: 'reviewer', strategy: 'Audit the tests without seeing generated repairs',
            task, config, model, sandbox, baseline, store, budget, reserveForReview: reserve,
            executionFailures: cell?.diagnostics?.failures, environment: options.environment});
          // Only original-source execution is granted to this private archive.
          if (cell?.artifact) await reviewer.options.store.artifact(await store.readArtifact(cell.artifact));
          let preview = '';
          if (config.search.schedule === 'staged') {
            // Numbered, bounded reads use the existing toolbox and citation
            // verifier. Save a cold read round without granting repair access.
            for (const file of candidate.tests.slice(0, 4)) {
              const room = Math.min(10000, config.context.toolOutputChars) - preview.length;
              if (room < 256 || budget.remainingMs < 1000) break;
              const read = await reviewer.toolbox.execute('read_file', {path: file.path, startLine: 1, endLine: 100},
                AbortSignal.timeout(Math.max(1, Math.min(10000, budget.remainingMs))));
              preview += `\n${JSON.stringify(read).slice(0, room)}`;
            }
          }
          await reviewer.run(turns, round, `Generated test files: ${candidate.tests.map(x => x.path).join(', ')}\nClaimed coverage: ${candidate.requirements.join('; ')}\nOriginal-source execution: ${cell?.status ?? 'unrun'}\nOriginal-source failure evidence: ${JSON.stringify({diagnostics: cell?.diagnostics, artifact: cell?.artifact})}${prior ? '\nUnresolved prior review; reassess against public evidence, not a binding rejection: ' + JSON.stringify(prior) : ''}`
            + (symptoms.length ? `\nCross-control exception symptoms (bounded; repairs are untrusted hypotheses): ${JSON.stringify(symptoms).slice(0, 3000)}\nInspect the cited test operations and original public APIs: an earlier baseline failure may mask an invalid fixture or unsupported access that fails after the target bug is repaired. These symptoms do not prove either the test or repair correct. Repair code, exception messages and source frames are withheld.` : '')
            + (preview ? `\nHost-read test previews (may be truncated; use read_file for omitted lines):${preview}` : ''));
          if (reviewer.review) reviews[candidate.testsHash] = {...reviewer.review, crossEvidenceHash: hash(symptoms)};
        } catch (error) {
          await store.event('review.failed', {candidate: candidate.id, error: errorText(error)});
        } finally {
          if (!reviewer?.review && reviewer?.pendingReview) reviews[candidate.testsHash] = reviewer.pendingReview;
          await sandbox?.close();
        }
      });
    } finally {
      // mapLimit drains active siblings before this snapshot or factory cleanup.
      await options.checkpoint();
    }
    selection = select(candidates, matrix, config.search.minIndependentSupport, reviews);
  }
  return selection;
}
