import type { Candidate, Observation, Review } from './types.js';
import { clip, hash } from './util.js';

export interface Selection { tests: Candidate[]; witnesses: Candidate[]; provisional?: Candidate; ranked: Candidate[]; feedback: Record<string, string> }

/** Correct executable defects and reviewed counterexamples, not disagreement alone. */
export function correctionCandidates(candidates: Candidate[], matrix: Observation[], reviews: Record<string, Review>): Candidate[] {
  const status = (test: Candidate, source: string) => matrix.find(cell => cell.testId === test.id && cell.sourceId === source)?.status;
  const priority = (candidate: Candidate) => {
    if (reviews[candidate.testsHash]?.accepted === false || status(candidate, 'baseline') !== 'fail'
      || !candidate.source.length || status(candidate, candidate.id) !== 'pass') return 2;
    return candidates.some(test => test.lineage !== candidate.lineage && reviews[test.testsHash]?.accepted === true
      && status(test, 'baseline') === 'fail' && status(test, candidate.id) === 'fail') ? 1 : 0;
  };
  return candidates.map(candidate => ({candidate, priority: priority(candidate)})).filter(item => item.priority > 0)
    .sort((a, b) => b.priority - a.priority || a.candidate.lineage.localeCompare(b.candidate.lineage)).map(item => item.candidate);
}
export function select(candidates: Candidate[], matrix: Observation[], minSupport: number, reviews: Record<string, Review> = {}): Selection {
  const cell = (test: string, source: string) => matrix.find(item => item.testId === test && item.sourceId === source);
  const sourceHashes = new Set<string>();
  const sources = candidates.filter(candidate => {
    if (!candidate.source.length || sourceHashes.has(candidate.sourceHash)
      || cell(candidate.id, 'baseline')?.status !== 'fail' || cell(candidate.id, candidate.id)?.status !== 'pass') return false;
    sourceHashes.add(candidate.sourceHash); return true;
  });
  const feedback: Record<string, string> = {};
  for (const candidate of candidates) {
    const review = reviews[candidate.testsHash];
    const attributed = new Set(review?.accepted && review.resolution === 'complete'
      ? review.executionChecks?.filter(check => check.cause === 'behavior' || check.cause === 'incidental').map(check => check.id) : []);
    const related = matrix.filter(item => item.testId === candidate.id || item.sourceId === candidate.id);
    const evidence = new Map<string, {status: string; preview: string; artifact?: string; diagnostics?: Observation['diagnostics']}>();
    const summary = related.map(item => {
      const key = hash({status: item.status, signature: item.signature}).slice(0, 12);
      if (item.status !== 'pass' && !evidence.has(key) && evidence.size < 4)
        evidence.set(key, {status: item.status, preview: clip(item.output, 650), artifact: item.artifact, diagnostics: item.diagnostics});
      return {test: item.testId, repair: item.sourceId, status: item.status,
        ...(item.status !== 'pass' ? {evidence: key, artifact: item.artifact} : {})};
    });
    feedback[candidate.lineage] = JSON.stringify({
      instruction: 'Correct failures against the original task. Agreement can repeat a shared mistake; other repairs are hypotheses. A peer failure alone does not justify weakening a test. Fix your source when a publicly justified peer test exposes a defect. Unrun cross-cells provide no evidence; consult artifacts for full output.',
      sourceHypothesis: {paths: candidate.source.map(file => file.path), replayScript:candidate.replayScript,
        selfStatus: cell(candidate.id, candidate.id)?.status ?? 'unrun'},
      reproducibility: cell(candidate.id, 'baseline')?.reproducibility,
      nextStep: cell(candidate.id, 'baseline')?.status === 'error'
        && cell(candidate.id, 'baseline')?.output.startsWith('Unstable results across fresh repeated runs:')
        ? 'Compare the recorded repeats before changing production code. Distinguish changing behavior from random fixture paths or diagnostic text. Use deterministic sandbox-local fixtures or stable failure messages while preserving the exact behavioral assertions. Reproduce the same failure across fresh runs; do not suppress failures or weaken the predicate to obtain agreement.'
        : cell(candidate.id, 'baseline')?.status !== 'fail'
        ? 'Establish a runnable regression that fails for the intended public behavior on original source; fix setup errors separately.'
        : cell(candidate.id, 'baseline')?.diagnostics?.failures.some(failure => failure.needsAttribution && !attributed.has(failure.id))
          ? 'Attribute unresolved baseline exceptions using the public contract, existing execution results and review evidence. They may be the intended regression or a setup defect. Correct the evidenced artifact, rerun the affected tests and submit; an expected baseline exception alone does not require changing the fixture.'
        : !candidate.source.length
          ? 'Your tests reproduce a baseline failure, but there is no captured source repair. Implement and run a source hypothesis now. Preserve justified assertions; do not repeat setup or unchanged baseline runs.'
          : cell(candidate.id, candidate.id)?.status !== 'pass'
            ? 'The source hypothesis and tests disagree. Attribute the first actionable failure to test setup, an unsupported assertion, or an incomplete repair using the public task and repository contract. Correct the responsible artifact and rerun controls. Agreement alone is not correctness; neither repeated repair failures nor a passing repair justify changing an assertion.'
            : 'Your source hypothesis passes its own tests. Address evidenced review findings or disagreements with independent repairs against the public contract.',
      peerResults: summary, evidence: Object.fromEntries(evidence),
      // The contract remains pinned. Hand off decisions and actionable findings,
      // not duplicate task quotes and large evidence excerpts from the review.
      review: review ? clip(JSON.stringify({accepted: review.accepted, resolution: review.resolution,
        findings: review.findings?.filter(finding => finding.severity === 'blocking').map(finding => ({
          kind: finding.kind, explanation: finding.explanation, requirement: finding.requirement,
          evidence: finding.evidence?.map(item => ({path: item.path, startLine: item.startLine, endLine: item.endLine})),
        })), executionChecks: review.executionChecks?.map(check => ({id: check.id, cause: check.cause, explanation: check.explanation})),
        concerns: review.concerns, missingRequirements: review.missingRequirements}), 3000) : undefined,
    });
  }
  const valid = candidates.filter(candidate => cell(candidate.id, 'baseline')?.status === 'fail'
    && reviews[candidate.testsHash]?.accepted !== false);
  const accepted: Candidate[] = [];
  const independent = (source: Candidate, test: Candidate) => source.lineage !== test.lineage && source.sourceHash !== test.sourceHash;
  let witnesses = sources;
  const duplicateTests = new Set<string>();
  const ranked = valid.map(candidate => ({candidate,
    support: sources.filter(source => independent(source, candidate) && cell(candidate.id, source.id)?.status === 'pass').length,
    ownPass: cell(candidate.id, candidate.id)?.status === 'pass' ? 1 : 0,
  // Coverage declarations are model claims, not measured coverage. Repeating
  // them must not buy review priority. Keep ties deterministic across resumes.
  })).sort((a, b) => b.support - a.support || b.ownPass - a.ownPass
    || a.candidate.tests.reduce((sum, file) => sum + file.data.length, 0) - b.candidate.tests.reduce((sum, file) => sum + file.data.length, 0)
    || a.candidate.id.localeCompare(b.candidate.id));
  for (const {candidate, support} of ranked) {
    if (support < minSupport || duplicateTests.has(candidate.testsHash)) continue;
    const common = witnesses.filter(source => cell(candidate.id, source.id)?.status === 'pass');
    if (common.filter(source => independent(source, candidate)).length < minSupport) continue;
    // Every already selected test must retain independent support after intersection.
    if (accepted.some(test => common.filter(source => independent(source, test)).length < minSupport)) continue;
    accepted.push(candidate); witnesses = common; duplicateTests.add(candidate.testsHash);
  }
  return {tests: accepted, witnesses: accepted.length ? witnesses : [], provisional: ranked[0]?.candidate,
    ranked: ranked.map(item => item.candidate), feedback};
}
