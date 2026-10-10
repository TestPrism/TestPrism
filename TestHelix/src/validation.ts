import type { Candidate, Config, FileArtifact, Observation, Status } from './types.js';
import { applyFiles, type Sandbox, type SandboxFactory } from './runtime.js';
import { Store } from './store.js';
import { Budget } from './budget.js';
import { clip, errorText, hash, id, mapLimit, publicPath, readJSON, shellQuote } from './util.js';
import {replayPath} from './artifacts.js';
import path from 'node:path';
import { executionEvidence, type ExecutionEvidence } from './execution-evidence.js';

export function classify(code: number | null, output: string, timedOut = false, truncated = false): Status {
  if (timedOut) return 'timeout';
  // Missing log bytes may hide an empty run or setup error. Re-run with bounded
  // reporting before treating this execution as acceptance evidence.
  if (truncated) return 'error';
  if (code === null || code === 126 || code === 127) return 'error';
  // unittest reports an empty discovery as successful exit 0. It is no evidence.
  if (/^\s*Ran 0 tests?\b|^\s*(?:no tests ran|collected 0 items)\b/im.test(output)) return 'error';
  if (code === 0) return 'pass';
  // A runner pointing at an absent generated test/helper is a broken suite,
  // not evidence that the public implementation violates its contract.
  if (/(?:can't open file|cannot find module|no such file or directory)[^\n]*[/\\]agent-tests[/\\]/i.test(output)
    || /(?:^|\n)[^\n]*agent-tests[/\\][^\n]*:\s*(?:no such file or directory|not found)(?:\s|$)/i.test(output)) return 'error';
  if (/agent-tests[^\n]*(?:syntax error|SyntaxError|IndentationError)/i.test(output)) return 'error';
  let reportedFile = '';
  for (const line of output.split('\n')) {
    const location = /^\s*File ["']([^"']+)["'], line \d+/.exec(line)
      ?? /^\s*((?:\/?[^\s:]+\/)*[^\s:]+\.(?:[cm]?js|tsx?|py)):\d+(?::\d+)?\s*$/.exec(line);
    if (location) reportedFile = location[1]!;
    if (/^\s*(?:SyntaxError|IndentationError|TabError)\b/.test(line) && /(?:^|[/\\])agent-tests[/\\]/.test(reportedFile)) return 'error';
  }
  if (/no tests ran|no task tests found|collected 0 items|error collecting|command not found/i.test(output)) return 'error';
  return 'fail';
}
/** Normalize reporting locations only; never rewrite temporary paths in assertion values. */
export function normalizeReportedTempPaths(output: string): string {
  return output.split('\n').map(line => {
    if (/^\s*rootdir: \/tmp\/\S+\s*$/.test(line)) return 'rootdir: /tmp/<temporary>';
    const normalize = (file: string) => '/tmp/<temporary>/' + file.slice(file.lastIndexOf('/') + 1);
    // Replace the reporting path only. A suffix can contain actual assertion
    // values, even when the line starts with a pytest/traceback location.
    return line.replace(/^(\s*File ")(\/tmp\/[^\"]+)(", line \d+)/,
      (_all, prefix: string, file: string, suffix: string) => prefix + normalize(file) + suffix)
      .replace(/^(\s*)((?:\.\.\/tmp|\/tmp)\/[^\s:]+)(?=:\d+|[ \t]+[.FEsxX]+(?:\s|$))/,
        (_all, prefix: string, file: string) => prefix + normalize(file));
  }).join('\n');
}
/** Loopback pool endpoints are runtime fixture metadata. Preserve assertion
 * values, non-local endpoints, error classes and the actual failure message. */
export function normalizeReportedLoopbackPorts(output: string): string {
  return output.split('\n').map(line => {
    if (line.includes('AssertionError') || /^\s*(?:E\s+)?assert\b/.test(line) || !/\b\w*(?:Error|Exception):/.test(line)) return line;
    return line.replace(/(\b(?:HTTP|HTTPS|SOCKSHTTP|SOCKSHTTPS)ConnectionPool\(host=(['"])(?:localhost|127\.0\.0\.1|::1)\2,\s*port=)\d+(\))/g,
      '$1<local-port>$3');
  }).join('\n');
}
export function failureSignature(status: Status, output: string, root: string): string {
  if (status === 'pass') return 'pass';
  return hash(normalizedFailure(output, root).trim());
}
/** Assertion values can vary while the same predicate fails (e.g. elapsed time).
 * Keep diagnostic hashes intact. Located test assertions use their predicate;
 * accompanying exceptions retain their exact parsed diagnostics and attribution
 * requirement. Incomplete or unlocated evidence falls back to the raw hash.
 */
export function repeatFailureIdentity(diagnostics: ExecutionEvidence, root: string, testFiles: ReadonlySet<string>): string | undefined {
  if (!diagnostics.failures.length || diagnostics.omitted) return;
  const prefix = root.replace(/\/+$/, '') + '/';
  const sites: string[] = [];
  let locatedAssertion = false;
  for (const failure of diagnostics.failures) {
    const frames = failure.frames.filter(frame => {
      const file = frame.path.startsWith(prefix) ? frame.path.slice(prefix.length) : frame.path;
      return file.startsWith('agent-tests/');
    });
    if (!frames.length || failure.message.length >= 300
      || failure.frames.some(frame => frame.path.length >= 180 || (frame.operation?.length ?? 0) >= 200)) return;
    try {
      const locations = [];
      for (const frame of frames) {
        const file = publicPath(frame.path.startsWith(prefix) ? frame.path.slice(prefix.length) : frame.path);
        if (!testFiles.has(file) || !frame.operation) return;
        locations.push([file, frame.line, frame.operation]);
      }
      // A production assertion reached through a test call is not a located
      // test predicate. Its message remains significant, like other exceptions.
      if (!failure.needsAttribution && /(?:^|\.)AssertionError$/.test(failure.exception)
        && /^(?:assert\b|(?:[\w.]+\.)?assert\w*\s*\()/.test(frames.at(-1)?.operation ?? '')) {
        locatedAssertion = true;
        sites.push(JSON.stringify([failure.exception, locations]));
      } else {
        sites.push(JSON.stringify({...failure,id:undefined,frames:failure.frames.map(frame=>({...frame,
          path:frame.path.startsWith(prefix) ? frame.path.slice(prefix.length) : frame.path}))}));
      }
    } catch { return; }
  }
  return locatedAssertion ? hash(sites.sort()) : undefined;
}
/** Compare qualified failures across fresh validations using the same rule as
 * within a repeated validation. Diagnostic hashes remain available for audit. */
export function sameFailure(left: Observation, right: Observation, root: string, tests: readonly FileArtifact[]): boolean {
  if (left.status !== 'fail' || right.status !== 'fail') return false;
  if (left.signature === right.signature) return true;
  if (!left.diagnostics || !right.diagnostics) return false;
  const files = new Set(tests.map(file => file.path));
  const before = repeatFailureIdentity(left.diagnostics, root, files);
  return !!before && before === repeatFailureIdentity(right.diagnostics, root, files);
}
function normalizedFailure(output: string, root: string): string {
  const workspace = root.replace(/\/+$/, '');
  const normalize = (file: string) => workspace && (file === workspace || file.startsWith(workspace + '/'))
    ? '<workspace>' + file.slice(workspace.length) : file;
  const locations = output.split('\n').map(line => line
    .replace(/^(\s*File ")([^\"]+)(", line \d+)/,
      (_all, prefix: string, file: string, suffix: string) => prefix + normalize(file) + suffix)
    .replace(/^(\s*)(\S+?)(?=:\d+(?::\d+)?(?:\s|:|$)|[ \t]+[.FEsxX]+\s*\[)/,
      (_all, prefix: string, file: string) => prefix + normalize(file))
    .replace(/^(\s+at (?:.*?\()?)([^\s()]+)(:\d+:\d+\)?\s*)$/,
      (_all, prefix: string, file: string, suffix: string) => prefix + normalize(file) + suffix)
    .replace(/^(\s*rootdir: )(\S+)(\s*)$/,
      (_all, prefix: string, file: string, suffix: string) => prefix + normalize(file) + suffix)
    .replace(/^(\s*Ran \d+ tests? in )\d+(?:\.\d+)?s(\s*)$/, '$1<duration>$2')
    .replace(/^(\s*(?:=+\s*)?\d+ (?:failed|passed|skipped|deselected|xfailed|xpassed|warnings?|errors?)(?:, \d+ (?:failed|passed|skipped|deselected|xfailed|xpassed|warnings?|errors?))* in )\d+(?:\.\d+)?s(?: \(\d+:\d+(?::\d+)?\))?(\s*=*\s*)$/,
      '$1<duration>$2')).join('\n');
  // Hex values, arbitrary durations, run IDs and paths in assertions remain
  // significant. Unknown output formats are conservatively left unchanged.
  const normalized = normalizeReportedLoopbackPorts(normalizeReportedTempPaths(locations));
  return normalized;
}
export class Validator {
  private inFlight = new Map<string, Promise<Observation>>();
  constructor(readonly factory: SandboxFactory, readonly config: Config, readonly store: Store,
    readonly budget: Budget, readonly identity: string) {}
  async evaluate(tests: FileArtifact[], source: FileArtifact[], testId: string, sourceId: string, repeat = 1,
    options: {fresh?: boolean; replayScript?: string} = {}): Promise<Observation> {
    if (!Number.isInteger(repeat) || repeat < 1 || repeat > 5) throw new Error('Invalid repeat count');
    const key = hash({tests, source, repeat, replayScript:options.replayScript, identity: this.identity, timeout: this.config.execution.testTimeoutMs});
    // Final attestations neither join exploratory work nor overwrite its cache.
    // Each invocation gets a distinct audit record, including after a resume.
    if (options.fresh) return this.evaluateFresh(hash({key, attempt: id('final')}), tests, source, testId, sourceId, repeat, true, options.replayScript);
    let pending = this.inFlight.get(key);
    if (!pending) {
      pending = this.evaluateFresh(key, tests, source, testId, sourceId, repeat, false, options.replayScript);
      this.inFlight.set(key, pending);
      pending.finally(() => this.inFlight.delete(key)).catch(() => {});
    } else await this.store.event('validation.deduplicated', {testId, sourceId});
    return {...await pending, testId, sourceId};
  }
  private async evaluateFresh(key: string, tests: FileArtifact[], source: FileArtifact[], testId: string, sourceId: string, repeat: number, fresh = false, replayScript?: string): Promise<Observation> {
    const cached = fresh ? undefined : await readJSON<Observation>(path.join(this.store.root, 'cells', `${key}.json`));
    if (cached && !['timeout', 'error'].includes(cached.status)) {
      await this.store.event('validation.cache_hit', {testId, sourceId});
      return {...cached, testId, sourceId};
    }
    const runs: Observation[] = [];
    const normalized: string[] = [];
    const assertionSites: Array<string | undefined> = [];
    const testFiles = new Set(tests.map(file => file.path));
    for (let index = 0; index < repeat; index++) {
      if (!this.budget.remainingMs) return {testId, sourceId, status: 'timeout', signature: 'budget', durationMs: 0, output: 'Run wall budget exhausted'};
      let sandbox: Sandbox | undefined;
      let preparationArtifact: string | undefined;
      try {
        sandbox = await this.factory.fork('cell');
        const script = replayPath(source, replayScript, sandbox.root);
        await applyFiles(sandbox, source);
        if (script) {
          const execution = await sandbox.exec('bash -e -- ' + shellQuote(script), Math.max(1, Math.min(this.config.execution.timeoutMs, this.budget.remainingMs)));
          preparationArtifact = await this.store.artifact(JSON.stringify({phase:'source-replay',...execution}));
          if (execution.code !== 0 || execution.timedOut || execution.truncated)
            throw new Error('Source replay failed before test execution; inspect the attached execution artifact');
          // A repair cannot supply its own passing runner. Install the exact
          // frozen tests only after the state transition is complete.
          await sandbox.remove('agent-tests');
        }
        await applyFiles(sandbox, tests);
        const syntax = await sandbox.exec('bash -n agent-tests/run-tests.sh', Math.min(10000, this.budget.remainingMs));
        if (syntax.code !== 0 || syntax.timedOut || syntax.truncated) throw new Error(`Generated runner syntax check failed: ${syntax.stderr}`);
        if (!this.budget.remainingMs) throw new Error('Run wall budget exhausted during validation preparation');
        const execution = await sandbox.exec('bash agent-tests/run-tests.sh', Math.max(1, Math.min(this.config.execution.testTimeoutMs, this.budget.remainingMs)));
        const output = execution.stdout + '\n' + execution.stderr + (execution.truncated
          ? '\n[Host: process output exceeded its capture limit; reduce runner logging and rerun.]' : '');
        const status = classify(execution.code, output, execution.timedOut, execution.truncated);
        const artifact = await this.store.artifact(output);
        normalized.push(normalizedFailure(output, sandbox.root));
        const diagnostics = executionEvidence(output);
        assertionSites.push(status === 'fail' ? repeatFailureIdentity(diagnostics, sandbox.root, testFiles) : undefined);
        runs.push({testId, sourceId, status, signature: failureSignature(status, output, sandbox.root),
          durationMs: execution.durationMs, output: clip(output, 3000), artifact, diagnostics});
      } catch (error) {
        assertionSites.push(undefined);
        normalized.push(errorText(error));
        runs.push({testId, sourceId, status: 'error', signature: hash(errorText(error)), durationMs: 0, output: errorText(error),
          ...(preparationArtifact ? {artifact:preparationArtifact} : {})});
      } finally { await sandbox?.close(); }
    }
    const result = {...runs[0]!, durationMs: runs.reduce((sum, run) => sum + run.durationMs, 0)};
    if (runs.some(run => run.status !== result.status || (result.status === 'fail' && run.signature !== result.signature))) {
      const statusChange = runs.some(run => run.status !== result.status);
      const sameAssertionSites = !statusChange && !!assertionSites[0] && assertionSites.every(site => site === assertionSites[0]);
      const changed = runs.findIndex(run => run.signature !== result.signature);
      const before = normalized[0]!.split('\n'), after = normalized[Math.max(0, changed)]!.split('\n');
      let line = 0;
      while (line < Math.max(before.length, after.length) && before[line] === after[line]) line++;
      result.reproducibility = {kind: statusChange ? 'status-change' : 'diagnostic-change',
        ...(sameAssertionSites ? {sameAssertionSites: true} : {}),
        runs: runs.map(({status, signature, artifact}) => ({status, signature, artifact})),
        ...(line < Math.max(before.length, after.length) ? {firstDifference: {line: line + 1,
          before: clip(before[line] ?? '<end of output>', 400), after: clip(after[line] ?? '<end of output>', 400)}} : {})};
      if (!sameAssertionSites) {
        result.status = 'error'; result.output = 'Unstable results across fresh repeated runs:\n' + runs.map(run => run.output).join('\n');
        result.signature = 'unstable';
      }
      // Preserve every full repeat, not just the first run's clipped prefix.
      // Diagnostic variation stays visible, including when the assertion sites agree.
      result.artifact = await this.store.artifact(JSON.stringify(result.reproducibility));
    }
    await this.store.save(`cells/${key}.json`, result);
    await this.store.event('validation.cell', {...result, fresh});
    return result;
  }
  async matrix(candidates: Candidate[]): Promise<Observation[]> {
    // Baseline/self gates determine which tests and repairs can affect selection.
    // Do not spend fresh sandboxes on cross-cells that selection must reject.
    const qualification = candidates.flatMap(test => [
      {test, source: undefined as Candidate | undefined},
      ...(test.source.length ? [{test, source: test}] : []),
    ]);
    const matrix = await mapLimit(qualification, this.config.execution.concurrency, ({test, source}) => this.evaluate(
      test.tests, source?.source ?? [], test.id, source?.id ?? 'baseline',
      this.config.execution.repeat,
      {replayScript:source?.replayScript},
    ));
    // A failed repair disqualifies that witness, not its independently justified
    // tests. Other qualified repairs may still accept those tests.
    const eligible = candidates.filter(candidate => matrix.some(cell => cell.testId === candidate.id && cell.sourceId === 'baseline' && cell.status === 'fail'));
    const sources = eligible.filter(candidate => candidate.source.length && matrix.some(cell => cell.testId === candidate.id && cell.sourceId === candidate.id && cell.status === 'pass'));
    const known = new Map<string, Observation>();
    for (const candidate of eligible) for (const cell of matrix.filter(cell => cell.testId === candidate.id && ['pass', 'fail'].includes(cell.status))) {
      const source = candidates.find(source => source.id === cell.sourceId);
      known.set(hash({tests: candidate.tests, source: source?.source ?? [], replayScript:source?.replayScript}), cell);
    }
    const jobs = eligible.flatMap(test => sources.filter(source => source.id !== test.id).map(source => ({test, source})));
    matrix.push(...await mapLimit(jobs, this.config.execution.concurrency, async ({test, source}) => {
      const cached = known.get(hash({tests: test.tests, source: source.source, replayScript:source.replayScript}));
      return cached ? {...cached, testId: test.id, sourceId: source.id}
        : this.evaluate(test.tests, source.source, test.id, source.id, 1, {replayScript:source.replayScript});
    }));
    await this.store.event('validation.matrix', {candidates: candidates.length, eligible: eligible.length, cells: matrix.length,
      skipped: candidates.length * (1 + candidates.filter(candidate => candidate.source.length).length) - matrix.length});
    return matrix;
  }
}
