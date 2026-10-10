import type { ModelMessage, ToolModelMessage } from 'ai';
import type { Candidate, Completion, Config, Model, Review, Role, Task, ToolSpec } from './types.js';
import type { Sandbox } from './runtime.js';
import { Budget, BudgetExceeded, type BudgetFloor } from './budget.js';
import {captureCandidate, captureWorkspaceCandidate, type CaptureContext} from './artifacts.js';
import { Store } from './store.js';
import { Toolbox, readTools, reviewTool, validateTool, workspaceTools } from './tools.js';
import { retryPlan } from './retry.js';
import { projectContext, contextSnapshot, restoreContext, contextTokens } from './context.js';
import { dispatchTools } from './tool-dispatch.js';
import { errorText, hash, readJSON, requestSize } from './util.js';
import path from 'node:path';
import { retrieveLessons, type LessonComponent } from './lessons.js';
import {recordLessonRetrieval} from './lesson-replay.js';
import { SkillSession } from './skill-session.js';
import { explainSkillContext, capabilitiesFor } from './skill-context.js';
import { finalizeReview, numberedPublicTask, ReviewEvidenceRequired } from './review.js';
import type { FailureEvidence } from './execution-evidence.js';
import {v4aPatchTool} from './v4a-patch.js';
import type {EnvironmentObservation} from './planner.js';
import {discoverSourceChanges} from './source-state.js';

export const candidatePrompt = `You are TestHelix. The original public task is the acceptance contract.
Produce a regression suite covering the public requirements. Use submit_candidate's contract object to declare path, format, schema, cardinality and note clauses for inputs and outputs. Inputs name public baseline files; outputs name captured agent-tests/ files. For stdin/stdout/return or semantics that cannot be checked from files, use a runtime target or note; these remain unverified. Do not handwrite JSON in prose. Unknown kinds, unsupported schema keywords and malformed fields are rejected; correct the indicated field within the existing budget. The host supplies defaults if contract is omitted and always preserves its hard constraints. A format check does not establish semantic correctness or public-task coverage. Prefer an internal repair control when it is cheap and reliable, but do not sacrifice a well-supported regression just to manufacture a repair. The unchanged baseline must fail for a task-related reason. Derive expectations independently from public evidence, not from the repair. Neither a command exit code nor a host checkpoint proves correctness.
Classify import failures by origin: missing external dependencies, wrong interpreters, zero tests, or tracebacks rooted in agent-tests are setup; a missing module under the project's public package may itself be the behavioral baseline.
Only agent-tests/ at the original working directory is exported; keep that location unchanged even if public source lives elsewhere. Keep test scripts, fixtures and helpers there, with executable agent-tests/run-tests.sh. Installed environments, builds and caches belong outside the export; the runner must reproduce required setup. Preflight the interpreter and imports once; if a dependency is absent, fix the runner's supported environment before judging the source behavior. After a behavioral baseline failure, consider a minimal mutation of the real public source with edit_file/apply_patch/write_file when it is cheap and informative. If a repair is impractical, preserve the regression evidence and continue toward submission instead of spending the remaining budget on a speculative change. Source edits inside the workspace are captured automatically. For command/state changes or public-source edits outside it, save a compact internal Bash repair script inside the workspace, outside agent-tests/, and submit its replayScript path; keep that script at most 500 characters and use a workspace-relative path, never inline its contents. The host replays it before installing the frozen tests in each fresh control environment; external edits alone are not captured.
If you make a source mutation, rerun the unchanged runner and record the result. Before submit, verify agent-tests/run-tests.sh exists and is executable and the baseline regression is reproducible. A source repair is useful supporting evidence, not a prerequisite for submitting a reviewed baseline regression. submit_candidate returning sourceVerified:false is an unverified proposal; preserve useful work when correcting a candidate. The host independently executes and reviews the candidate; your declaration cannot bypass verification. Use only public task evidence and task-provided source; hidden benchmark files, reference solutions and other candidates are unavailable.
Choose the next action from the missing evidence and remaining budget. Skills provide optional methods, not additional acceptance requirements. Batch independent reads, follow artifact references and keep concise evidence in update_notes. Never emit submit_candidate in the same response as writes or exec: write the source/replay first, execute the exact runner in a later response, then submit alone.`;

const reviewerPrompt = `You review independent regression tests against the original public task.
Your workspace contains the unchanged public baseline plus generated tests. You cannot see generated source repairs or any hidden oracle.
Inspect the tests and relevant public interfaces. Decide whether every assertion follows from explicit requirements or clearly established repository behavior.
Flag implementation coupling, hard-coded workspace paths, circular expected values, invented negative behavior, swallowed failures, unbounded generation, fake passing runners, and missing major public requirements.
Structural/performance assertions are permitted when the task explicitly requests them. A newly required file or API may be absent on baseline.
Execution evidence is provided by the host; you are not the execution oracle. Return submit_review findings with severity, the public requirement and file evidence. Cite path plus startLine/endLine from numbered reads; cite the numbered original public task with taskLines. The host extracts exact excerpts; do not recopy source escapes or Markdown. Block only evidenced setup defects, unsupported assertions or missing required behavior. Optional cases and speculative concerns are suggestions; host derives acceptance from blocking findings. An excerpt proves its existence, not your interpretation.
Before acceptance, explain each host-listed baseline exception/no-work ID in executionChecks using the cited fixture and operation. A required API missing on baseline can be a behavior failure; inconsistent fixtures and commands that process no input are different causes. Check a supported control and actual failure output. An unresolved cause cannot be counted as a valid regression.
For a blocking unsupported-assertion finding, supply falseRejection describing a concrete allowed behavior, why it remains allowed given established compatibility, whether the assertion is a justified control, and publicBasis with taskLines or a repository line citation. Omission from the task is not enough. If that claim cannot be established, express the uncertainty as a suggestion; do not invent a counterexample or automatically approve a failing suite.`;
const toolsets = new Map<string, Record<string, ToolSpec>>();
// Keep the emergency closeout request small enough to fit after a long
// exploration transcript. These are the only capabilities needed to preserve
// artifacts, run the portable control, and submit a candidate. Including the
// full workspace surface here can make the tool schema itself consume the
// remaining input reservation, yielding agent_test_missing even when files
// already exist.
const candidateCloseoutTools = new Set([
  'write_test_suite', 'write_file', 'edit_file', 'apply_patch', 'exec',
  'read_artifact', 'submit_candidate', 'update_notes',
]);
function sessionTools(role: Role, skills: readonly string[], resources: boolean, planner = false, terminal = false, v4aPatch = false) {
  const key = `${role}:${[...skills].sort().join(',')}:${resources}:${planner}:${terminal}:${v4aPatch}`;
  const cached = toolsets.get(key);
  if (cached) return cached;
  const tools: Record<string, ToolSpec> = role === 'candidate' ? {...workspaceTools} : Object.fromEntries([
    ...['list_files', 'read_file', 'search', 'read_artifact', 'update_notes', 'view_image', 'load_skill'].map(name => [name, workspaceTools[name]!]),
    ['submit_review', reviewTool],
  ]);
  if (!skills.length) delete tools.load_skill;
  else tools.load_skill = {...workspaceTools.load_skill!, schema: {type: 'object', additionalProperties: false,
    properties: {id: {type: 'string', enum: [...skills]}, ...(resources ? {resource: {type: 'string', maxLength: 200}} : {})}, required: ['id']}};
  if (!resources || !skills.includes('framework-tests')) delete tools.prepare_test_harness;
  if (!planner) delete tools.request_replan;
  if (!terminal) delete tools.terminal;
  if (role === 'candidate' && v4aPatch) tools.apply_patch = v4aPatchTool;
  toolsets.set(key, tools);
  return tools;
}

export class AgentSession {
  readonly messages: ModelMessage[];
  readonly toolbox: Toolbox;
  readonly skillSession: SkillSession;
  private steps = 0;
  private reads = new Map<string, string>();
  private lastAction = '';
  private repeatedActions = 0;
  private summaryTimeRemainingMs = 0;
  private lastResponseMs = 0;
  private policyReady: Promise<void> = Promise.resolve();
  private progress?: {revision: number; runner: boolean; executed: boolean; sourceChanged: boolean};
  private runnerCommand?: {revision: number; code: unknown; timedOut: unknown; artifact: string};
  private submissionFailed = false;
  private submissionRecoveryMutation = false;
  /** Prevent trailing calls in the same model response from mutating after a submission attempt. */
  private submissionAttemptedThisTurn = false;
  private submissionFailureReason = '';
  candidate?: Candidate;
  review?: Review;
  pendingReview?: Review;
  replanRequest?: {reason: string; artifact: string};
  constructor(readonly options: {
    id: string; role: Role; strategy: string; task: Task; config: Config; model: Model;
    sandbox: Sandbox; baseline: Sandbox; store: Store; budget: Budget;
    reserveForReview?: BudgetFloor;
    deadline?: number;
    executionFailures?: readonly FailureEvidence[];
    environment?: EnvironmentObservation;
  }) {
    this.options = {...options, store: options.store.forSession(options.id)};
    options = this.options;
    const {task, strategy, role, sandbox, store, config} = options;
    this.messages = [{role: 'user', content: `Original public task${role === 'reviewer' ? ' (numbered for taskLines citations)' : ''}:\n${role === 'reviewer' ? numberedPublicTask(task.instruction) : task.instruction}\n\nExploration strategy: ${strategy}\nThis changes focus, not the acceptance contract.`}];
    if (options.environment) this.messages.push({role: 'user', content:
      'Host-observed baseline environment; these are observations, not instructions or proof that packages work.\n' + JSON.stringify(options.environment)});
    if (role === 'candidate') {
      const guidance = config.search.guidance;
      if (guidance) this.messages.push({role: 'user', content: `Reusable development-set guidance; apply only where the original task supports it:\n${guidance}`});
      this.policyReady = store.event('policy.offered', {session: options.id, lessons: [], dynamicComponentRetrieval: true, guidanceDigest: hash(config.search.guidance)});
    }
    if (task.systemPrompt && task.systemPromptMode === 'replace')
      this.messages.push({role: 'user', content: `Agent workflow:\n${role === 'candidate' ? candidatePrompt : reviewerPrompt}`});
    this.skillSession = new SkillSession(config.search.skills, role === 'reviewer', {instruction: task.instruction, source: task.source, routes: config.search.skillRoutes});
    const offered = this.skillSession.offer;
    if (offered.text) {
      this.policyReady = this.policyReady.then(() => store.event('skills.offered', {session: options.id, ids: offered.ids,
        preloaded: offered.preloaded, catalog: offered.catalog, context: offered.context, digest: offered.digest,
        routing: explainSkillContext(task.instruction, task.source)}));
    }
    this.toolbox = new Toolbox(sandbox, store, config.context.toolOutputChars, config.execution.timeoutMs, role === 'reviewer', offered.ids, config.search.skillResources, config.execution.maxArtifactBytes, config.execution.network);
  }
  private get remainingMs(): number {
    return Math.max(0, Math.min(this.options.budget.remainingMs, (this.options.deadline ?? Infinity) - Date.now()));
  }
  private get captureContext(): CaptureContext {
    const {task, config} = this.options;
    return {instruction: task.instruction, contractLimits: config.execution.contract,
      protocol: task.runtime.kind === 'testprism' ? 'tests-only' : 'internal'};
  }
  private get finalizationMs(): number {
    return Math.min(this.options.config.model.timeoutMs, Math.max(30000,
      this.lastResponseMs * 2 + Math.min(30000, this.options.config.execution.timeoutMs)));
  }
  async restoreSealed(candidate: Candidate): Promise<void> {
    const saved = await readJSON<{candidate?: Candidate; messages: ModelMessage[]; memory?: unknown; steps: number; context?: ReturnType<typeof contextSnapshot>; skills?: ReturnType<SkillSession['snapshot']>; procedures?: unknown; replanRequest?: {reason: string; artifact: string}}>(
      path.join(this.options.store.root, 'sessions', `${this.options.id}.json`));
    if (!saved?.candidate || saved.candidate.sourceHash !== candidate.sourceHash || saved.candidate.testsHash !== candidate.testsHash
      || JSON.stringify(saved.messages[0]) !== JSON.stringify(this.messages[0])) return;
    // Only a finalized checkpoint is replayable. Unfinished work is not silently treated as sealed source state.
    const calls = saved.messages.flatMap(message => typeof message.content === 'string' ? [] : message.content.filter(part => part.type === 'tool-call'));
    const results = saved.messages.flatMap(message => typeof message.content === 'string' ? [] : message.content.filter(part => part.type === 'tool-result'));
    if (calls.length !== results.length || calls.some(call => !results.some(result => result.toolCallId === call.toolCallId))) return;
    if (saved.memory) this.toolbox.memory.restore(saved.memory);
    this.skillSession.restore(saved.skills);
    this.toolbox.procedures.restore(saved.procedures);
    this.replanRequest = saved.replanRequest;
    this.messages.splice(0, this.messages.length, ...saved.messages); this.steps = saved.steps;
    restoreContext(this.messages, saved.context);
    await this.options.store.event('session.restored', {session: this.options.id, messages: this.messages.length, memoryStale: true});
  }
  async run(maxSteps: number, round = 0, feedback?: string): Promise<void> {
    const previous = this.candidate;
    try { await this.runSteps(maxSteps, round, feedback); }
    catch (error) {
      const {role, sandbox, baseline, config, store, budget, id, strategy} = this.options;
      if (role !== 'candidate' || this.candidate || budget.remainingMs < 1000) throw error;
      try {
        // Missing a final tool call must not discard actual work, including an
        // interrupted correction. Captured source is an unverified hypothesis.
        const declaration = {sourcePaths: [],
          ...(previous?.replayScript ? {replayScript:previous.replayScript} : {}),
          requirements: ['Coverage is unverified; assess the suite against the complete original public contract.'],
          rationale: 'Host captured existing artifacts after generation stopped without a valid submission. Coverage and any source repair remain hypotheses requiring independent execution and review.'};
        const identity = {id: `${id}-r${round}`, lineage: id, round, strategy, sessionId: id};
        let sourceError: string | undefined;
        try { this.candidate = await captureWorkspaceCandidate(sandbox, baseline, declaration, identity, config.execution.maxArtifactBytes, this.captureContext); }
        catch (error) {
          sourceError = errorText(error);
          this.candidate = await captureCandidate(sandbox, baseline, {...declaration,
            rationale: declaration.rationale + ' Source inventory could not be sealed; this is a tests-only candidate.'}, identity, config.execution.maxArtifactBytes, this.captureContext);
        }
        if (previous?.sourceHash === this.candidate.sourceHash && previous.testsHash === this.candidate.testsHash) this.candidate = previous;
        await store.event('candidate.recovered', {session: id, reason: errorText(error), sourceSealed: this.candidate.source.length > 0,
          sourceError, source: this.candidate.source.map(file => ({path: file.path, deleted: file.deleted ?? false})), tests: this.candidate.tests.map(file => file.path)});
        await this.checkpoint();
      } catch (recoveryError) {
        this.candidate = undefined;
        await store.event('candidate.recovery_failed', {session: id, error: errorText(recoveryError)});
        throw error;
      }
    }
  }
  private async runSteps(maxSteps: number, round = 0, feedback?: string): Promise<void> {
    const {role, config, store, budget, id, task} = this.options;
    await this.policyReady;
    await this.toolbox.procedures.initialize();
    if (feedback) { this.messages.push({role: 'user', content: feedback}); this.skillSession.feedback(); }
    this.candidate = undefined; this.review = undefined; this.pendingReview = undefined;
    this.submissionFailed = false; this.submissionRecoveryMutation = false; this.submissionFailureReason = '';
    // Compression is optional bookkeeping. Bound its cumulative wall time so
    // repeated large reads cannot spend the candidate phase summarizing alone.
    this.summaryTimeRemainingMs = this.remainingMs * config.context.compactTimeFraction;
    await store.event('context.budget', {session: id, timeLimitMs: Math.floor(this.summaryTimeRemainingMs),
      timeFraction: config.context.compactTimeFraction, requestTimeoutMs: config.context.compactTimeoutMs,
      maxOutputTokens: config.context.compactMaxOutputTokens});
    const availableTools = sessionTools(role, this.toolbox.skills, config.search.skillResources, config.planner.enabled && id === 'candidate-1',
      !!this.options.sandbox.terminal, ['gpt-5.4', 'gpt-5.5'].includes(config.model.name));
    const finalTool = role === 'candidate' ? 'submit_candidate' : 'submit_review';
    const native = role === 'candidate' ? candidatePrompt : reviewerPrompt;
    const system = task.systemPrompt && task.systemPromptMode === 'replace' ? task.systemPrompt
      : native + (task.systemPrompt ? '\n\n' + task.systemPrompt : '');
    let inputFinalization = false;
    turns: for (let step = 0; step < maxSteps; step++) {
      budget.assertAvailable();
      if (!this.remainingMs) throw new BudgetExceeded('Candidate phase time allowance exhausted');
      this.steps++;
      this.submissionAttemptedThisTurn = false;
      const floor = this.options.reserveForReview;
      const availableCalls = Math.max(0, budget.remainingCalls - (floor?.calls ?? 0));
      const forecast = budget.forecastResponses(floor?.input);
      const tokenPressure = role === 'candidate' && forecast !== undefined && forecast <= 2 * config.budget.concurrency;
      // Two parallel candidates may each need a checkpoint and a final action.
      // Enter artifact finalization before their shared allowance is consumed.
      const finishingCalls = role === 'candidate' ? 2 * config.budget.concurrency : 1;
      const timePressure = this.remainingMs <= this.finalizationMs;
      // Establish a runnable artifact early.  Without this gate a candidate can
      // spend its whole exploratory phase reading source and leave the official
      // scorer with no test file; after two turns the model gets the same
      // write/exec/submit surface used during normal finalization.
      // Keep the candidate in the artifact phase until it has demonstrated the
      // complete portable loop: runner, an observed execution, and a captured
      // public-source/replay change. A runner-only smoke artifact is not useful
      // to the official scorer and was a recurring failure mode in v16.
      let artifactGate = role === 'candidate' && step >= 3
        && !(this.progress?.runner && this.progress.executed);
      const gateAtStart = artifactGate;
      // A rejected publication is itself a finalization state. Keep the
      // session in the bounded recovery surface until the declaration is
      // repaired or accepted; otherwise the next turn silently reopens broad
      // discovery and can strand the same artifacts again.
      let final = inputFinalization || artifactGate || this.submissionFailed || step === maxSteps - 1 || availableCalls <= finishingCalls || this.repeatedActions >= 3 || tokenPressure || timePressure;
      let readySeal = false;
      // Budget finalization is a capability phase, not another request to keep
      // reading. Writes/edits/exec remain available so the model can complete and
      // check real artifacts. A focused inspection can still use bounded exec.
      let checkpoint = '';
      if (role === 'candidate') {
        if (this.progress?.revision !== this.toolbox.workspaceRevision) {
          const files = await this.options.sandbox.list('agent-tests');
          // Source inventory is expensive and is not available in every
          // lightweight sandbox used by discovery tests. It is only needed
          // after a runnable suite has executed; before that, keep the
          // discovery path free of an unrelated host-side scan.
          const source = files.includes('agent-tests/run-tests.sh') && this.runnerCommand
            ? await discoverSourceChanges(this.options.sandbox, this.options.baseline)
            : {sourcePaths: [], deletedSourcePaths: []};
          this.progress = {revision: this.toolbox.workspaceRevision,
            runner: files.includes('agent-tests/run-tests.sh'),
            executed: Boolean(this.runnerCommand && this.runnerCommand.revision === this.toolbox.workspaceRevision),
            sourceChanged: source.sourcePaths.length > 0 || source.deletedSourcePaths.length > 0};
        }
        // Once the complete evidence loop is present, seal immediately. This
        // keeps a successful repair from being lost to another exploratory
        // turn while retaining the writable artifact tool surface.
        artifactGate = step >= 3 && !(this.progress.runner && this.progress.executed);
        // A source/replay change is the meaningful transition into closeout.
        // Runner execution alone must remain compatible with discovery and
        // correction sessions that intentionally submit tests-only evidence;
        // once a repair change has been observed, seal on the next response.
        if (!artifactGate && this.progress.sourceChanged) { final = true; readySeal = true; }
        // Record observations, not a guessed framework, test layout or diagnosis.
        // An arbitrary shell command mentioning the runner is not proof of coverage.
        const execution = this.runnerCommand ? {...this.runnerCommand,
          current: this.runnerCommand.revision === this.toolbox.workspaceRevision} : null;
        checkpoint = `Workspace evidence: ${JSON.stringify({runnerPresent: this.progress.runner,
          execution, sourceChanged: this.progress.sourceChanged})}. `
          + 'Execution is a command receipt; source repair and task coverage remain unverified. Preserve a behavioral baseline; add a source/replay repair control when practical.';
        if (this.submissionFailed) checkpoint += ` Previous submission was rejected: ${this.submissionFailureReason}. ${this.submissionRecoveryMutation ? 'A workspace mutation has been made; rerun the exact runner and retry the declaration.' : 'Repair the rejected artifact before retrying. If a public-source or replay change is practical, add it; otherwise fix the regression suite or runner, rerun the exact command, and resubmit with an honest rationale.'}`;
        await store.event('workflow.checkpoint', {session: id, step: this.steps, runner: this.progress.runner,
          executed: this.progress.executed, sourceChanged: this.progress.sourceChanged,
          execution, tokenPressure, forecast, timePressure, remainingMs: this.remainingMs});
      }
      // Once a runnable artifact has executed, the candidate has entered an
      // irreversible closeout path. The review floor protects later review and
      // correction work, but reserving its input slice here can strand the
      // already-created artifact before submit_candidate (the source of
      // agent_test_missing). Keep call/output reservations intact while
      // allowing this final request to consume otherwise idle input capacity.
      const closeoutFloor: BudgetFloor | undefined = role === 'candidate'
        && final && this.progress?.runner && this.progress.executed
        ? {...(floor ?? {input: 0, output: 0, calls: 0}), input: 0}
        : floor;
      const inputFloor = closeoutFloor?.input ?? 0;
      // Resource exhaustion is the only automatic capability transition. Domain
      // methods cannot force edits or disable discovery after a fraction of turns.
      // Keep discovery available while the artifact checkpoint is being
      // established. Once a final response is actually due, narrow the surface
      // so the model can finish the runnable control and publish it. A runner
      // with an observed execution may still need one final source read.
      // Submission recovery is model-guided. Keep inspection and repair tools
      // available so a rejected artifact can be fixed in-place instead of
      // forcing an unrelated source mutation through a host-side gate.
      const recoveryPending = false;
      const closeoutNames = new Set(candidateCloseoutTools);
      if (role === 'candidate' && final && this.runnerCommand) closeoutNames.add('read_file');
      const tools = role === 'candidate' && final && (recoveryPending || !gateAtStart || inputFinalization || readySeal)
        ? Object.fromEntries(Object.entries(availableTools).filter(([name]) => closeoutNames.has(name))) : availableTools;
      const tail: ModelMessage[] = [{role: 'user', content: final ? (role === 'candidate'
        ? `${artifactGate ? 'Artifact checkpoint: finish the portable regression loop before more exploration.' : tokenPressure || inputFinalization ? 'Input budget is nearly exhausted; plan to finish in this response.' : 'Host turn limit: this is your last response.'} The host has switched to artifact tools: write_test_suite/write/edit/apply_patch/exec/submit. ${artifactGate ? 'Create an executable runner and execute the same behavioral case. If a small source/replay control is practical, add and rerun it; otherwise submit the reviewed baseline regression with an honest rationale. Do not spend this turn on review prose.' : 'Finish essential file writes or execution in this response; submit_candidate must be the only tool call in a later response after the runner passes (never put writes and submit in the SAME tool batch).'} Use exec for a narrowly necessary check. The submit tool only captures existing files. ${checkpoint}`
        : `Host turn limit: submit now with ${finalTool}; no exploration turns remain.`)
        : `Turns left: ${Math.min(maxSteps - step, availableCalls)}. Phase time left: ${Math.ceil(this.remainingMs / 1000)} seconds, including writes and execution. ${this.repeatedActions >= 2 ? 'Repeated actions produced no new work. Change approach or submit usable artifacts.' : 'Prioritize runnable artifacts.'} ${checkpoint}`}];
      const active = this.skillSession.render();
      const component: LessonComponent = active.phase === 'review' ? 'review' : active.phase === 'repair' ? 'repair'
        : active.phase === 'discovery' ? 'preparation' : 'generation';
      const lessonSurface = task.instruction + '\n' + active.reason + '\n' + checkpoint;
      const lessonScope = {component, capabilities: capabilitiesFor(task.instruction, task.source)};
      const methods = retrieveLessons(config.search.lessons, lessonSurface, 1600, lessonScope);
      await recordLessonRetrieval(store, lessonSurface, lessonScope, 1600, methods);
      if (methods.length) {
        tail.unshift({role: 'user', content: `Current ${component} methods; original requirements take priority:\n` + methods.map(x => x.text).join('\n')});
        await store.event('skills.component_methods', {session: id, component, methods: methods.map(x => ({id: x.id, version: x.version ?? hash(x)}))});
      }
      this.toolbox.procedures.setActive(active.ids);
      if (active.text) {
        tail.unshift({role: 'user', content: [active.text, this.toolbox.procedures.render()].filter(Boolean).join('\n')});
        const {text: _, ...telemetry} = active;
        await store.event('skills.active', {session: id, step: this.steps, ...telemetry});
      }
      let projected: ModelMessage[];
      for (;;) {
        try {
          projected = await projectContext(this.messages, this.toolbox.memory.render(task.instruction), config.context, store,
            {system, tools, tail, maxInputReservation: () => Math.max(0, budget.remainingInputTokens - inputFloor), imageTokenReservation: config.model.imageTokenReservation,
              // Preserve the remaining actions for execution and submission;
              // Pi's existing bounded archive fallback needs no model request.
              summarize: final || step >= maxSteps - 2 ? undefined
                : (summarySystem, summaryMessages, maxOutput, continuationInput) => this.complete(summarySystem, summaryMessages, {}, undefined, maxOutput, 'compact', continuationInput)});
          break;
        } catch (error) {
          if (!/exceed remaining input reservation/.test(errorText(error))) throw error;
          if (await budget.waitForSettlement(this.remainingMs)) {
            await store.event('budget.peer_settled', {session: id, stage: 'context'});
            continue;
          }
          // A historical average can overestimate the remaining actions. The
          // actual discovery request may not fit while our existing smaller
          // artifact tool surface still does. Reproject once before discarding
          // the opportunity to write, execute and seal; retain all budget floors.
          if (role === 'candidate' && !final) {
            inputFinalization = true;
            await store.event('workflow.finalization', {session: id, reason: 'input-reservation',
              error: errorText(error), remainingInputTokens: budget.remainingInputTokens,
              protectedInputTokens: inputFloor});
            step--; continue turns;
          }
          throw error;
        }
      }
      // Projection itself may spend wall time. Rebuild the final tool surface
      // and instruction before dispatch, rather than sending stale exploration
      // guidance after a summary used the remaining discovery allowance.
      if (!final && this.remainingMs <= this.finalizationMs) { step--; continue; }
      const modelMessages: ModelMessage[] = [...projected, ...tail];
      // Before spending another exploratory response, prove that the same
      // projection can still fit one writable candidate closeout. This uses
      // the conservative request-size estimator and the existing review floor;
      // it is a look-ahead check, not an extra reservation or a turn quota.
      if (role === 'candidate' && !final) {
        const closeoutTools = Object.fromEntries(Object.entries(availableTools)
          .filter(([name]) => candidateCloseoutTools.has(name)));
        const closeoutTail: ModelMessage[] = [{role: 'user', content:
          `Input budget is nearly exhausted; finish essential file writes or run the suite in this response. The host has switched to artifact tools: write_test_suite/write/edit/apply_patch/exec/submit_candidate. Submit_candidate must be a separate later response after the runner passes. ${checkpoint}`}];
        const closeoutMessages = [...projected, ...closeoutTail];
        const closeoutSize = requestSize({system, messages: closeoutMessages, tools: closeoutTools}, config.model.imageTokenReservation);
        const closeoutInput = closeoutSize.bytes + closeoutSize.imageTokens + closeoutMessages.length * 64;
        const availableInput = Math.max(0, budget.remainingInputTokens - inputFloor);
        if (closeoutInput > availableInput) {
          inputFinalization = true;
          await store.event('workflow.finalization', {session: id, reason: 'lookahead-input-floor',
            closeoutInput, availableInput, remainingInputTokens: budget.remainingInputTokens,
            protectedInputTokens: floor?.input ?? 0, tools: Object.keys(closeoutTools).length});
          step--; continue turns;
        }
      }
      // Candidate finalization may still require writes. A forced submit-only call
      // prevents recovery from a preceding failed edit even when output budget remains.
      const forceCandidateSubmit = role === 'candidate' && final && Boolean(this.progress?.runner)
        && (!this.submissionFailed || this.submissionRecoveryMutation)
        ? 'submit_candidate' : undefined;
      const reply = await this.complete(system, modelMessages, tools,
        final && role === 'reviewer' ? finalTool : forceCandidateSubmit,
        undefined, undefined, 0, closeoutFloor);
      const action = hash(reply.calls.length ? reply.calls.map(call => ({name: call.name, input: call.input})) : 'no-tool');
      this.repeatedActions = action === this.lastAction ? this.repeatedActions + 1 : 1; this.lastAction = action;
      const emitted = reply.messages.flatMap(message => typeof message.content === 'string' ? [] : message.content.filter(part => part.type === 'tool-call'));
      if (new Set(reply.calls.map(call => call.id)).size !== reply.calls.length || emitted.length !== reply.calls.length
        || reply.calls.some(call => !emitted.some(part => part.toolCallId === call.id && part.toolName === call.name)))
        throw new Error('Invalid model tool-call protocol: duplicate IDs or inconsistent assistant messages');
      this.messages.push(...reply.messages);
      await store.event('model.completed', {session: id, role, step: this.steps, usage: reply.usage, finish: reply.finish, tools: reply.calls.map(x => x.name)});
      for (const notice of reply.protocol ?? []) await store.event(`protocol.${notice.status}`, {session: id, step: this.steps, ...notice});
      await this.checkpoint();
      if (!reply.calls.length) {
        this.messages.push({role: 'user', content: `Use the tools to complete the work and call ${finalTool}; prose is not an artifact.`});
        continue;
      }
      const visibleIds = new Set(projected.flatMap(message => typeof message.content === 'string' ? []
        : message.content.filter(part => part.type === 'tool-result').map(part => part.toolCallId)));
      const execute = async (call: Completion['calls'][number], signal: AbortSignal) => {
        let result: Record<string, unknown>;
        const started = performance.now();
        await store.event('tool.started', {session: id, id: call.id, name: call.name});
        try {
          if (call.invalid) throw new Error(call.invalid);
          if (this.candidate || this.review || (role === 'candidate' && this.submissionAttemptedThisTurn))
            throw new Error('The session is finalized; trailing tool was not executed');
          if (!Object.hasOwn(tools, call.name)) throw new Error('Tool is not available in this role');
          const input = validateTool(tools[call.name]!, call.input);
          if (call.name === 'request_replan' && role === 'candidate' && config.planner.enabled) {
            const request = input as {reason: string; artifact: string};
            const evidence = await store.readArtifact(request.artifact);
            this.replanRequest = {...request, reason: request.reason + '\nObserved evidence: ' + evidence.slice(0, 1200)};
            result = {ok: true, pending: true, message: 'Host will consider one bounded planning revision after this candidate phase; preserve and submit usable work.'};
          } else if (call.name === 'submit_candidate' && role === 'candidate') {
            this.submissionAttemptedThisTurn = true;
            this.candidate = await captureWorkspaceCandidate(this.options.sandbox, this.options.baseline, input,
              {id: `${id}-r${round}`, lineage: id, round, strategy: this.options.strategy, sessionId: id}, config.execution.maxArtifactBytes, this.captureContext);
            const report = this.candidate.contractCheck!;
            const contract = {origin: report.origin, verified: report.checks.filter(check => check.status === 'verified').length,
              unverified: report.checks.filter(check => check.status === 'unverified').length,
              artifact: await store.artifact(JSON.stringify(report))};
            await store.event('candidate.contract', {candidate: this.candidate.id, ...contract});
            result = {ok: true, exported: this.candidate.id, source: this.candidate.source.map(file => ({path: file.path, deleted: file.deleted ?? false})), sourceVerified: false, contract};
          } else if (call.name === 'submit_review' && role === 'reviewer') {
            this.review = await finalizeReview(input, this.options.sandbox, task.instruction, this.options.executionFailures);
            this.pendingReview = undefined;
            result = {ok: true, reviewed: true, accepted: this.review.accepted, discardedEvidence: this.review.discardedEvidence};
          } else result = await this.toolbox.execute(call.name, input, signal, tools[call.name]);
        } catch (error) {
          result = {ok: false, error: errorText(error)};
          if (role === 'reviewer' && call.name === 'submit_review' && !this.review) {
            this.pendingReview = error instanceof ReviewEvidenceRequired ? error.review : {
              accepted: false, resolution: 'needs-evidence', concerns: [errorText(error)], missingRequirements: [], coveredRequirements: [],
            };
            result.resolution = 'needs-evidence';
            await store.event('review.needs_evidence', {session: id, concerns: this.pendingReview.concerns});
          }
        }
        if (call.name === 'submit_candidate' && role === 'candidate' && result.ok === false) {
          this.submissionFailed = true;
          this.submissionRecoveryMutation = false;
          this.submissionFailureReason = String(result.error ?? 'submission rejected');
        }
        const mutationInput = (call.input ?? {}) as Record<string, unknown>;
        const sourceMutation = call.name === 'edit_file' || call.name === 'apply_patch'
          || (call.name === 'write_file' && typeof mutationInput.path === 'string'
            && !mutationInput.path.startsWith('agent-tests/'));
        // Rewriting tests does not repair a rejected source/replay export. Only
        // a public-source or replay mutation can reopen a candidate submission.
        const recoveryMutation = sourceMutation;
        if (role === 'candidate' && recoveryMutation && result.ok !== false)
          this.submissionRecoveryMutation = true;
        const full = JSON.stringify(result), artifact = await store.artifact(full);
        const input = (call.input ?? {}) as Record<string, unknown>;
        if (call.name === 'write_test_suite' && result.executed && result.execution) {
          const execution = result.execution as Record<string, unknown>;
          this.runnerCommand = {revision: this.toolbox.workspaceRevision, code: execution.code, timedOut: execution.timedOut, artifact};
        } else if (call.name === 'exec' && typeof input.command === 'string'
          && /(?:^|[\s/])agent-tests\/run-tests\.sh(?:\s|[;&|"']|$)/.test(input.command) && 'code' in result) {
          this.runnerCommand = {revision: this.toolbox.workspaceRevision, code: result.code, timedOut: result.timedOut, artifact};
        }
        await store.event('tool.completed', {session: id, id: call.id, name: call.name, ok: result.ok, artifact, durationMs: Math.round(performance.now() - started)});
        let visible = typeof result.image === 'string'
          ? {ok: true, path: result.path, mediaType: result.mediaType, imageAttached: true}
          : await this.toolbox.observe(result);
        if (call.name === 'load_skill' && result.ok && !result.resource)
          visible = {ok: true, id: result.id, digest: result.digest, references: result.references, procedures: result.procedures, activeNextRequest: true, artifact};
        if (['read_file', 'search', 'list_files'].includes(call.name) && result.ok) {
          const digest = hash({name: call.name, result}), previous = this.reads.get(digest);
          if (full.length > 600 && previous && visibleIds.has(previous)) visible = {ok: true, unchangedSince: previous, artifact};
          else this.reads.set(digest, call.id);
        }
        return {call, result, visible, artifact};
      };
      const completed = await dispatchTools(reply, tools, readTools, execute, AbortSignal.timeout(Math.max(1, this.remainingMs)), config.execution.readConcurrency);
      for (const {call, result, artifact} of completed)
        this.skillSession.observe(call.name, (call.input ?? {}) as Record<string, unknown>, result, artifact);
      const response: ToolModelMessage = {role: 'tool', content: completed.map(({call, visible}) => ({type: 'tool-result', toolCallId: call.id,
        toolName: call.name, output: {type: 'json', value: visible as never}}))};
      this.messages.push(response);
      // Images follow ALL tool results; inserting user messages between tool results breaks Anthropic's protocol.
      for (const {result} of completed) if (typeof result.image === 'string') this.messages.push({role: 'user', content: [
        {type: 'text', text: `Public task image: ${String(result.path)}`},
        {type: 'image', image: result.image, mediaType: String(result.mediaType)},
      ]});
      await this.checkpoint();
      if (this.candidate || this.review) return;
      // A finalization response is the last response we promised the model.
      // Seal the real workspace via run() recovery instead of buying repeated
      // "last" responses; verification still decides whether it is publishable.
      if (final && !gateAtStart && !readySeal && !this.submissionFailed) break;
    }
    throw new Error(`${id} exhausted ${maxSteps} turns without a valid ${finalTool}`);
  }
  private async complete(system: string, messages: ModelMessage[], tools: Parameters<Model['complete']>[0]['tools'], forceTool?: string,
    outputLimit?: number, purpose?: 'compact', continuationInput = 0, floorOverride?: BudgetFloor): Promise<Completion> {
    const {budget, model, config, store, id} = this.options;
    if (purpose === 'compact' && contextTokens({system, messages, tools}, config.context.tokenizer) > config.context.maxTokens)
      throw new Error('Summary evidence exceeds configured context; use archived evidence fallback');
    const floor = floorOverride ?? this.options.reserveForReview;
    // A checkpoint must leave a response to act on it. Enforce this atomically
    // in the shared reservation, including when another candidate is running.
    const protectedFloor = purpose === 'compact' ? {input: (floor?.input ?? 0) + continuationInput,
      output: (floor?.output ?? 0) + Math.min(1024, config.model.maxOutputTokens),
      calls: (floor?.calls ?? 0) + (this.options.role === 'candidate' ? 2 * config.budget.concurrency : 1)} : floor;
    return budget.slot(async () => {
      for (let attempt = 0; ; attempt++) {
        if (!this.remainingMs) throw new BudgetExceeded('Candidate phase time allowance exhausted');
        const summaryTimeout = purpose === 'compact' ? Math.floor(Math.min(config.context.compactTimeoutMs,
          this.summaryTimeRemainingMs, this.remainingMs - this.finalizationMs)) : undefined;
        if (summaryTimeout !== undefined && summaryTimeout <= 0)
          throw new Error('Compaction time allowance exhausted; preserve time for artifact actions');
        const size = requestSize({system, messages, tools}, config.model.imageTokenReservation);
        const inputUpperBound = size.bytes + size.imageTokens + messages.length * 64;
        const outputCap = Math.min(outputLimit ?? Infinity,
          this.options.role === 'reviewer' ? Math.min(config.model.maxOutputTokens, config.model.reviewMaxOutputTokens) : config.model.maxOutputTokens);
        let reservation;
        for (;;) {
          try { reservation = await budget.reserve(inputUpperBound, outputCap, protectedFloor); break; }
          catch (error) {
            if (!(error instanceof BudgetExceeded) || !await budget.waitForSettlement(this.remainingMs)) throw error;
            if (!this.remainingMs) throw new BudgetExceeded('Candidate phase time allowance exhausted');
            await store.event('budget.peer_settled', {session: id, stage: 'reservation'});
          }
        }
        const started = performance.now();
        const wallStarted = Date.now();
        let requestSent = false;
        try {
          const requestTimeout = Math.floor(Math.min(config.model.timeoutMs, this.remainingMs,
            summaryTimeout ?? Infinity, purpose === 'compact' ? this.remainingMs - this.finalizationMs : Infinity));
          if (requestTimeout <= 0) throw new BudgetExceeded('No phase time remains for this model request');
          await store.event('model.requested', {session: id, purpose, model: config.model.name,
            reasoningEffort: config.model.providerOptions.openai?.reasoningEffort, attempt, inputUpperBound, outputCap: reservation.output, messages: messages.length,
            ...(purpose === 'compact' ? {summaryTimeoutMs: requestTimeout, summaryTimeRemainingMs: Math.floor(this.summaryTimeRemainingMs)} : {})});
          requestSent = true;
          const reply = await model.complete({system, messages, tools, forceTool, maxOutputTokens: reservation.output,
            signal: AbortSignal.timeout(requestTimeout)});
          await budget.settle(reservation, reply.usage.estimated ? undefined : reply.usage);
          await store.event('model.latency', {session: id, durationMs: Math.round(performance.now() - started), usage: reply.usage});
          if (!purpose) this.lastResponseMs = Math.max(0, Date.now() - wallStarted);
          if (purpose) await store.event('model.completed', {session: id, role: purpose, purpose, step: this.steps, usage: reply.usage,
            finish: reply.finish, tools: reply.calls.map(call => call.name)});
          return reply;
        } catch (error) {
          // Compaction has a recoverable archive fallback; retrying a failed
          // summary buys no task progress and can consume the remaining actions.
          const plan = retryPlan(error, attempt, purpose === 'compact' ? 0 : config.model.maxRetries, config.model.maxRetryDelayMs);
          await budget.settle(reservation, !requestSent || plan.rejected ? {input: 0, output: 0, cached: 0} : undefined);
          await store.event('model.error', {session: id, purpose, attempt, status: plan.status, retry: plan.retry, delayMs: plan.delayMs, error: errorText(error)});
          if (error instanceof BudgetExceeded || !plan.retry || plan.delayMs >= this.remainingMs) throw error;
          await new Promise(resolve => setTimeout(resolve, plan.delayMs));
        } finally {
          if (purpose === 'compact') this.summaryTimeRemainingMs = Math.max(0,
            this.summaryTimeRemainingMs - Math.max(0, Date.now() - wallStarted));
        }
      }
    });
  }
  private checkpoint(): Promise<void> {
    return this.options.store.save(`sessions/${this.options.id}.json`, {schemaVersion: 1,
      id: this.options.id, steps: this.steps, notes: this.toolbox.notes, memory: this.toolbox.memory.snapshot(), messages: this.messages,
      context: contextSnapshot(this.messages), skills: this.skillSession.snapshot(), procedures: this.toolbox.procedures.snapshot(),
      sandbox: {id: this.options.sandbox.id, root: this.options.sandbox.root}, candidate: this.candidate, review: this.review, pendingReview: this.pendingReview,
      replanRequest: this.replanRequest});
  }
}
