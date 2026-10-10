import { z } from 'zod';
import type { Review } from './types.js';
import type { Sandbox } from './runtime.js';
import { errorText, publicPath, hash } from './util.js';
import type { FailureEvidence } from './execution-evidence.js';

export const taskLines = z.strictObject({startLine: z.number().int().positive(), endLine: z.number().int().positive().optional()})
  .refine(value => value.endLine === undefined || value.endLine >= value.startLine, 'End line must follow start line');
export function numberedPublicTask(instruction: string): string {
  return instruction.split('\n').map((line, index) => `${index + 1}| ${line}`).join('\n');
}
export function publicTaskExcerpt(instruction: string | undefined, range: z.infer<typeof taskLines>, minChars = 8): string {
  taskLines.parse(range);
  if (instruction === undefined) throw new Error('Original public task is unavailable');
  const lines = instruction.split('\n'), end = range.endLine ?? range.startLine;
  if (end > lines.length) throw new Error('Task evidence line range outside the original public task');
  const excerpt = lines.slice(range.startLine - 1, end).join('\n');
  if (excerpt.trim().length < minChars) throw new Error(`Task evidence needs a substantive excerpt of at least ${minChars} characters`);
  return excerpt;
}

const citation = z.strictObject({path: z.string().min(1).max(1024), quote: z.string().min(8).max(800).optional(),
  excerptHash: z.string().regex(/^[a-f0-9]{64}$/).optional(), excerptChars: z.number().int().positive().optional(),
  startLine: z.number().int().positive().optional(), endLine: z.number().int().positive().optional()})
  .refine(value => value.quote !== undefined || value.startLine !== undefined, 'Supply quote or startLine')
  .refine(value => value.endLine === undefined || (value.startLine !== undefined && value.endLine >= value.startLine
    ), 'End line must follow start line');

export const reviewFindingSchema = z.strictObject({
  severity: z.enum(['blocking', 'suggestion']),
  kind: z.enum(['setup', 'unsupported-assertion', 'missing-requirement', 'improvement']),
  explanation: z.string().min(1).max(1000),
  requirement: z.string().min(1).max(800),
  evidence: z.array(citation).min(1).max(3),
  falseRejection: z.strictObject({
    allowedBehavior: z.string().min(12).max(800).describe('Concrete public-contract-compliant behavior that this assertion would reject.'),
    whyAllowed: z.string().min(12).max(1000).describe('Explain why that behavior is allowed, considering established compatibility, not only omission from the task.'),
    controlAssessment: z.string().min(12).max(800).describe('Assess whether this assertion is a justified positive/preservation control and why it still rejects valid behavior.'),
    publicBasis: z.union([
      z.strictObject({taskLines, taskQuote: z.string().min(8).max(800).optional(), excerptHash: z.string().regex(/^[a-f0-9]{64}$/).optional()}),
      z.strictObject({taskQuote: z.string().min(8).max(800)}),
      z.strictObject({repository: citation}),
    ]).describe('Original task line range or unchanged public repository citation; generated tests cannot justify this claim.'),
  }).optional().describe('Required by the host for a blocking unsupported-assertion finding. Missing evidence requests clarification, never automatic acceptance.'),
});
export type ReviewFinding = z.infer<typeof reviewFindingSchema>;
const executionCheckSchema = z.strictObject({
  id: z.string().regex(/^[a-f0-9]{16}$/),
  cause: z.enum(['behavior', 'setup', 'incidental', 'unclear']),
  explanation: z.string().min(12).max(800),
  evidence: z.array(citation).min(1).max(2),
  taskLines: taskLines.optional().describe('For behavior: line range in the numbered original public task supporting this attribution. Host extracts the exact text.'),
  taskQuote: z.string().min(8).max(800).optional().describe('For behavior: exact public task excerpt explaining why this exception is caused by the missing required behavior.'),
  taskExcerptHash: z.string().regex(/^[a-f0-9]{64}$/).optional(),
});
export type ReviewExecutionCheck = z.infer<typeof executionCheckSchema>;
export const reviewSchema = z.strictObject({
  findings: z.array(reviewFindingSchema).max(20), coveredRequirements: z.array(z.string().min(1).max(1000)).max(30),
  executionChecks: z.array(executionCheckSchema).max(6).optional().describe('Before acceptance, attribute each host-listed baseline exception/no-work ID using test or public file evidence. A setup cause blocks; unclear requests clarification. Incidental requires evidence that the symptom does not prevent the target observation.'),
});
// Current requests use coordinates, so copying Markdown/code escapes is not part
// of the review task. The full reader still accepts sealed historical quotes.
const fileLines = z.strictObject({path: z.string().min(1).max(1024), ...taskLines.shape})
  .refine(value => value.endLine === undefined || value.endLine >= value.startLine, 'End line must follow start line');
export const reviewWireSchema = reviewSchema.extend({
  findings: z.array(reviewFindingSchema.extend({
    evidence: z.array(fileLines).min(1).max(3),
    falseRejection: reviewFindingSchema.shape.falseRejection.unwrap().extend({
      publicBasis: z.union([z.strictObject({taskLines}), z.strictObject({repository: fileLines})]),
    }).optional(),
  })).max(20),
  executionChecks: z.array(executionCheckSchema.omit({taskQuote: true, taskExcerptHash: true}).extend({evidence: z.array(fileLines).min(1).max(2)})).max(6).optional(),
});
// Read old fixtures/checkpoints conservatively. Only the evidence-based schema is
// advertised to current models; old concerns remain blocking, never reclassified.
const legacyReview = z.strictObject({accepted: z.boolean(), concerns: z.array(z.string()).max(20),
  missingRequirements: z.array(z.string()).max(20), coveredRequirements: z.array(z.string()).max(30)});
export const parseReview = (input: unknown) => z.union([reviewSchema, legacyReview]).parse(input);

export class ReviewEvidenceRequired extends Error {
  constructor(readonly review: Review) {
    super(review.concerns.join('\n'));
    this.name = 'ReviewEvidenceRequired';
  }
}

export async function finalizeReview(input: unknown, sandbox: Sandbox, publicInstruction?: string, executionFailures: readonly FailureEvidence[] = []): Promise<Review> {
  const parsed = parseReview(input);
  if (!('findings' in parsed)) {
    if (parsed.accepted && executionFailures.some(failure => failure.needsAttribution))
      throw new ReviewEvidenceRequired({...parsed, accepted: false, resolution: 'needs-evidence', concerns: ['Baseline exceptions require executionChecks before acceptance.']});
    return {...parsed, accepted: parsed.accepted && !parsed.concerns.length && !parsed.missingRequirements.length};
  }
  const contents = new Map<string, string>();
  const taskExcerpt = (range: z.infer<typeof taskLines>) => publicTaskExcerpt(publicInstruction, range);
  const verifyCitation = async (evidence: z.infer<typeof citation>) => {
    const file = publicPath(evidence.path);
    if (!contents.has(file)) {
      const data = await sandbox.read(file);
      if (data.length > 8_000_000) throw new Error('Review evidence file exceeds 8 MB');
      contents.set(file, data.toString('utf8'));
    }
    const content = contents.get(file)!;
    let quote = evidence.quote;
    let excerptHash: string | undefined, excerptChars: number | undefined;
    if (evidence.startLine !== undefined) {
      const lines = content.split('\n'), end = evidence.endLine ?? evidence.startLine;
      if (end > lines.length) throw new Error(`Review evidence line range outside ${file}`);
      const excerpt = lines.slice(evidence.startLine - 1, end).join('\n');
      if (quote !== undefined && !excerpt.includes(quote)) throw new Error(`Review evidence quote not found in the specified lines of ${file}`);
      excerptHash = hash(excerpt); excerptChars = excerpt.length;
      if (evidence.excerptHash !== undefined && evidence.excerptHash !== excerptHash) throw new Error(`Stale evidence digest in ${file}`);
      if (evidence.excerptChars !== undefined && evidence.excerptChars !== excerptChars) throw new Error(`Stale evidence length in ${file}`);
      // Range correctness and display size are different concerns. Bind the
      // COMPLETE range by digest; present a short exact substring without asking
      // the model to retry a valid citation merely because it spans many lines.
      quote ??= excerpt.trimStart().slice(0, 800);
    }
    if (!quote || quote.trim().length < 8 || quote.length > 800 || !content.includes(quote))
      throw new Error(`Review evidence quote not found in ${file}; use startLine/endLine or an exact excerpt of 8–800 characters`);
    return {...evidence, path: file, quote, ...(excerptHash ? {excerptHash, excerptChars} : {})};
  };
  const findings: ReviewFinding[] = [], discardedEvidence: NonNullable<Review['discardedEvidence']> = [];
  for (const [index, finding] of parsed.findings.entries()) {
    if (finding.severity === 'blocking' && finding.kind === 'improvement') throw new Error('An optional improvement must be a suggestion');
    const verified: ReviewFinding['evidence'] = [], errors: string[] = [];
    for (const evidence of finding.evidence) {
      try {
        verified.push(await verifyCitation(evidence));
      } catch (error) {
        const message = errorText(error); errors.push(message);
        discardedEvidence.push({finding: index, path: evidence.path, error: message});
      }
    }
    // The contract requires at least one verified excerpt per finding, not a
    // perfectly copied optional second citation. Never drop an unverified
    // finding and reinterpret the remainder as an accepted review.
    if (!verified.length) throw new Error(`No verified evidence for finding ${index + 1}: ${errors.join('; ')}`);
    findings.push({...finding, evidence: verified});
  }
  const clarification: string[] = [];
  const executionChecks: ReviewExecutionCheck[] = [];
  // Rejection already prevents publication. Acceptance must explain the actual
  // failure evidence, rather than approving the intended meaning of the tests.
  if (!findings.some(finding => finding.severity === 'blocking')) {
    const required = executionFailures.filter(failure => failure.needsAttribution);
    for (const failure of required) {
      const matching = parsed.executionChecks?.filter(check => check.id === failure.id) ?? [];
      try {
        if (matching.length !== 1) throw new Error('supply exactly one executionChecks entry for this ID');
        const check = matching[0]!;
        const evidence = await Promise.all(check.evidence.map(verifyCitation));
        if (check.cause === 'unclear') throw new Error(check.explanation);
        const taskQuote = check.taskLines ? taskExcerpt(check.taskLines) : check.taskQuote;
        if (check.taskLines && check.taskQuote !== undefined && !taskQuote?.includes(check.taskQuote))
          throw new Error('Task quote conflicts with the cited task lines');
        if (check.taskExcerptHash !== undefined && check.taskExcerptHash !== hash(taskQuote ?? '')) throw new Error('Stale public task evidence digest');
        if (check.cause === 'behavior' && (!taskQuote || !publicInstruction?.includes(taskQuote)))
          throw new Error('behavior attribution needs taskLines and evidence that the fixture reaches that required behavior');
        executionChecks.push({...check, evidence, ...(taskQuote ? {taskQuote: taskQuote.trimStart().slice(0, 800), taskExcerptHash: hash(taskQuote)} : {})});
        if (check.cause === 'setup') findings.push({severity: 'blocking', kind: 'setup',
          explanation: check.explanation, requirement: 'Test preparation must reach the public behavior under test.', evidence});
      } catch (error) { clarification.push(`Baseline failure ${failure.id} (${failure.exception}) needs attribution: ${errorText(error)}`); }
    }
  }
  for (const [index, finding] of findings.entries()) {
    if (finding.severity !== 'blocking' || finding.kind !== 'unsupported-assertion') continue;
    try {
      const proof = finding.falseRejection;
      if (!proof) throw new Error('supply falseRejection: allowedBehavior, whyAllowed, controlAssessment and publicBasis. Task omission alone does not establish incompatibility; assess justified controls.');
      if ('taskLines' in proof.publicBasis) {
        const excerpt = taskExcerpt(proof.publicBasis.taskLines);
        if (proof.publicBasis.excerptHash !== undefined && proof.publicBasis.excerptHash !== hash(excerpt)) throw new Error('Stale public task evidence digest');
        if (proof.publicBasis.taskQuote !== undefined && !excerpt.includes(proof.publicBasis.taskQuote)) throw new Error('Task quote conflicts with the cited task lines');
        proof.publicBasis = {taskLines: proof.publicBasis.taskLines, taskQuote: excerpt.trimStart().slice(0, 800), excerptHash: hash(excerpt)};
      } else if ('taskQuote' in proof.publicBasis) {
        if (!publicInstruction || !publicInstruction.includes(proof.publicBasis.taskQuote) || proof.publicBasis.taskQuote.trim().length < 8)
          throw new Error('publicBasis.taskQuote must be an exact excerpt of the original public task');
      } else {
        const file = publicPath(proof.publicBasis.repository.path);
        if (file === 'agent-tests' || file.startsWith('agent-tests/')) throw new Error('publicBasis cannot cite generated tests as the public contract');
        proof.publicBasis = {repository: await verifyCitation(proof.publicBasis.repository)};
      }
    } catch (error) { clarification.push(`Finding ${index + 1} needs evidence: ${errorText(error)}`); }
  }
  if (clarification.length && !executionChecks.some(check => check.cause === 'setup')) throw new ReviewEvidenceRequired({accepted: false, resolution: 'needs-evidence', findings,
    concerns: clarification, missingRequirements: [], coveredRequirements: parsed.coveredRequirements,
    ...(discardedEvidence.length ? {discardedEvidence} : {})});
  const blockers = findings.filter(finding => finding.severity === 'blocking');
  const describe = (finding: ReviewFinding) => `${finding.explanation} Requirement: ${finding.requirement} Evidence: ${finding.evidence.map(e => e.path).join(', ')}`;
  return {accepted: !blockers.length, resolution: 'complete', findings, ...(executionChecks.length ? {executionChecks} : {}), ...(discardedEvidence.length ? {discardedEvidence} : {}),
    concerns: blockers.filter(finding => finding.kind !== 'missing-requirement').map(describe),
    missingRequirements: blockers.filter(finding => finding.kind === 'missing-requirement').map(describe),
    coveredRequirements: parsed.coveredRequirements};
}
