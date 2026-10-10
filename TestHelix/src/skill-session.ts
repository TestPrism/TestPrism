import { builtinSkills, skillOffer, reviewerOnlySkills, type SkillId, type SkillRoute } from './skills.js';
import { hash } from './util.js';

type Phase = 'discovery' | 'tests' | 'repair' | 'review';
interface SkillState {phase: Phase; manual?: SkillId; failure?: {kind: string; artifact: string};
  setup?: {artifact: string; controlPassed: boolean}}
type SkillOmission = {id: SkillId; reason: 'disabled' | 'component' | 'skill-limit' | 'context-limit'};
/** Bounded method injection from explicit choices and observed tool outcomes.
 * Runtime observations never prescribe a framework, fixture layout or repair. */
export class SkillSession {
  readonly offer: ReturnType<typeof skillOffer>;
  private state: SkillState;
  private readonly specialist?: SkillId;
  private readonly abstain: boolean;
  constructor(enabled: readonly SkillId[], readonly reviewer: boolean,
    task: {instruction: string; source: string; routes: readonly SkillRoute[]}) {
    this.offer = skillOffer(enabled, reviewer, task);
    this.specialist = this.offer.preloaded.find(id => id !== 'artifact-workflow' && !reviewerOnlySkills.includes(id));
    this.abstain = task.routes.some(route => route.context === this.offer.context && route.skill === null);
    this.state = {phase: reviewer ? 'review' : 'discovery'};
  }
  snapshot(): SkillState { return structuredClone(this.state); }
  restore(state?: SkillState): void {
    if (!state) return;
    this.state = structuredClone(state);
    if (this.state.manual && !this.offer.ids.includes(this.state.manual)) this.state.manual = undefined;
    // A restored workspace has no current execution evidence.
    this.state.setup = undefined; this.state.failure = undefined;
  }
  feedback(): void { if (!this.reviewer) { this.state.phase = 'repair'; this.state.manual = undefined; } }
  observe(name: string, input: Record<string, unknown>, result: Record<string, unknown>, artifact: string): void {
    if (name === 'load_skill' && result.ok && !input.resource && this.offer.ids.includes(input.id as SkillId)) this.state.manual = input.id as SkillId;
    if (this.reviewer) return;
    if (name === 'write_test_suite' && result.written && result.execution) {
      this.state.phase = 'tests';
      this.observe('exec', {command: 'bash agent-tests/run-tests.sh'}, result.execution as Record<string, unknown>, artifact);
      return;
    }
    if (name === 'prepare_test_harness') {
      this.state.setup = {artifact, controlPassed: result.controlPassed === true};
      if (result.controlPassed) { this.state.phase = 'tests'; this.state.failure = undefined; }
    }
    const runner = name === 'exec' && typeof input.command === 'string'
      && /(?:^|[\s/])agent-tests\/run-tests\.sh(?:\s|[;&|"']|$)/.test(input.command);
    if (runner && result.code === 0 && result.status === 'pass' && !result.timedOut && !result.truncated) {
      this.state.phase = 'tests'; this.state.failure = undefined;
    } else if (runner && result.status === 'fail' && !result.timedOut && !result.truncated) {
      // Host classification is an execution observation, not an oracle verdict.
      // Expected baseline failures must not make generation methods unreachable.
      this.state.phase = 'tests';
      this.state.failure = {kind: 'unattributed failed run; baseline, oracle or setup may be responsible; correctness is unverified', artifact};
    } else if (runner || (name === 'prepare_test_harness' && !result.ok)) {
      this.state.phase = 'repair';
      const kind = result.timedOut ? 'timeout' : 'unsuccessful execution';
      this.state.failure = {kind, artifact};
    } else if ((result.ok || name === 'terminal') && ['write_file', 'edit_file', 'apply_patch', 'exec', 'terminal'].includes(name)) {
      this.state.setup = undefined;
      if (this.state.phase === 'discovery' && typeof input.path === 'string' && input.path.startsWith('agent-tests/')) this.state.phase = 'tests';
    }
  }
  render(): {text: string; ids: SkillId[]; requested: SkillId[]; omitted: SkillOmission[];
    phase: Phase; digest: string; reason: string; context: string; chars: number} {
    const requested: SkillId[] = [];
    const {phase, manual, setup, failure} = this.state;
    if (this.reviewer) requested.push('contract-audit', 'control-evidence', 'observable-oracles');
    else {
      requested.push('artifact-workflow');
      if (!this.abstain) {
        if (phase === 'repair' || failure) requested.push('failure-triage');
        if (this.specialist) requested.push(this.specialist);
      }
    }
    if (manual) requested.unshift(manual);
    const component = phase === 'discovery' ? 'preparation' : phase === 'tests' ? 'generation' : phase;
    const requestedIds = [...new Set(requested)], omitted: SkillOmission[] = [];
    let ids: SkillId[] = [];
    for (const id of requestedIds) {
      if (!this.offer.ids.includes(id)) omitted.push({id, reason: 'disabled'});
      // Unattributed evidence still needs triage while generation stays available.
      else if (id !== manual && !(id === 'failure-triage' && failure) && !builtinSkills[id].components.includes(component)) omitted.push({id, reason: 'component'});
      else if (ids.length === 3) omitted.push({id, reason: 'skill-limit'});
      else ids.push(id);
    }
    const reason = failure
      ? `Own execution symptom: ${failure.kind}; evidence artifact ${failure.artifact}. The model must determine its cause.`
      : setup ? `Preparation receipt ${setup.artifact}; generic control passed: ${setup.controlPassed}. Task coverage remains unverified.`
      : `Current phase: ${phase}.`;
    const catalog = this.offer.catalog.filter(id => !ids.includes(id));
    const prefix = `Active generic method skills. The full original public contract takes priority. ${reason}\n`;
    const suffix = () => catalog.length ? '\nOn demand with load_skill: ' + catalog.map(id => `${id}: ${builtinSkills[id].summary}`).join('\n') : '';
    const body = () => ids.map(id => `Preloaded ${id}:\n${builtinSkills[id].text}`).join('\n');
    // The optional discovery index must not evict a method selected for the
    // current execution phase merely because metadata grew by a few bytes.
    while (catalog.length && (prefix + body() + suffix()).length > 4400) catalog.pop();
    while (ids.length > 1 && (prefix + body() + suffix()).length > 4400)
      omitted.push({id: ids.pop()!, reason: 'context-limit'});
    const text = this.offer.ids.length ? prefix + body() + suffix() : '';
    return {text, ids, requested: requestedIds, omitted, phase, reason, context: this.offer.context, chars: text.length,
      digest: hash({ids: ids.map(id => [id, builtinSkills[id].digest]), phase, reason, setup})};
  }
}
