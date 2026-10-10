import {z} from 'zod';
import {builtinSkills, skillIds, skillResources, type SkillId} from './skills.js';
import {errorText, hash, readJSON} from './util.js';
import type {Store} from './store.js';
import type {Sandbox} from './runtime.js';
import path from 'node:path';

const fileSchema = z.strictObject({path: z.string(), digest: z.string().regex(/^[a-f0-9]{64}$/), mode: z.number().int().min(0).max(511)});
export type ProcedureFile = z.infer<typeof fileSchema>;
const definitionSchema = z.strictObject({id: z.string().regex(/^[a-z-]+$/),
  tool: z.enum(['prepare_test_harness', 'write_test_suite']), purpose: z.string().min(1).max(240),
  verification: z.enum(['setup-control', 'suite-execution']), recovery: z.enum(['recheck-owned-files', 'inspect-existing-work'])});
const definitions = skillIds.flatMap(skill => {
  const resource = skillResources(skill).find(file => file.path === 'procedures.json');
  if (!resource) return [];
  const parsed = z.strictObject({schemaVersion: z.literal(1), procedures: z.array(definitionSchema).min(1).max(2)}).parse(JSON.parse(resource.data.toString()));
  return parsed.procedures.map(definition => {
    const valid = skill === 'framework-tests' && definition.tool === 'prepare_test_harness' && definition.verification === 'setup-control' && definition.recovery === 'recheck-owned-files'
      || skill === 'artifact-workflow' && definition.tool === 'write_test_suite' && definition.verification === 'suite-execution' && definition.recovery === 'inspect-existing-work';
    if (!valid) throw new Error('Skill procedure must bind an existing host capability with its actual verification');
    return {...definition, skill, key: `${skill}/${definition.id}`};
  });
});
if (new Set(definitions.map(d => d.key)).size !== definitions.length || new Set(definitions.map(d => d.tool)).size !== definitions.length)
  throw new Error('Duplicate skill procedure binding');
export const proceduresForSkill = (id: SkillId) => definitions.filter(def => def.skill === id);
type Definition = typeof definitions[number];
const receiptSchema = z.strictObject({key: z.string(), skill: z.enum(skillIds), skillDigest: z.string(), inputHash: z.string(),
  activation: z.enum(['active-method', 'available-capability']).default('available-capability'),
  attempt: z.number().int().positive(), workspace: z.string(), status: z.enum(['running', 'completed', 'failed', 'interrupted', 'stale']),
  stage: z.string().max(60), plannedFiles: z.array(fileSchema).max(80),
  outcome: z.enum(['setup-control-pass', 'suite-pass', 'suite-fail', 'execution-error']).optional(),
  evidence: z.string().regex(/^[a-f0-9]{64}$/).optional(), previousEvidence: z.string().regex(/^[a-f0-9]{64}$/).optional()});
const snapshotSchema = z.strictObject({schemaVersion: z.literal(1), receipts: z.array(receiptSchema).max(4)});
type Receipt = z.infer<typeof receiptSchema>;
export interface ProcedureHooks {
  resumeFiles?: readonly ProcedureFile[];
  stage: (name: string, files?: readonly ProcedureFile[]) => Promise<void>;
}

/** Domain receipts, not another agent loop. Pi dispatches the existing tools;
 * this binds their observations to versioned skills and scoped durable state. */
export class SkillExecution {
  private receipts = new Map<string, Receipt>();
  private ready?: Promise<void>;
  private fallbackSnapshot?: unknown;
  private readonly checkpoint: string;
  private active = false;
  private activeSkills = new Set<SkillId>();
  constructor(private readonly sandbox: Sandbox, private readonly store: Store, private readonly enabled: readonly SkillId[],
    private readonly resources: boolean) {
    this.checkpoint = `sessions/${store.scope ?? hash(sandbox.id)}/skill-execution.json`;
  }
  initialize(): Promise<void> {
    return this.ready ??= (async () => {
      if (!this.resources || !definitions.some(def => this.enabled.includes(def.skill))) return;
      let raw: unknown;
      try { raw = await readJSON<unknown>(path.join(this.store.root, this.checkpoint)) ?? this.fallbackSnapshot; }
      catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
        await this.event('skills.procedure.restore_rejected', {reason: 'Malformed checkpoint JSON'}); return;
      }
      if (raw === undefined) return;
      const parsed = snapshotSchema.safeParse(raw);
      if (!parsed.success) { await this.event('skills.procedure.restore_rejected', {reason: 'Invalid or incompatible checkpoint'}); return; }
      for (const receipt of parsed.data.receipts) {
        if (!definitions.some(def => def.key === receipt.key && def.skill === receipt.skill) || !this.enabled.includes(receipt.skill)) continue;
        const status = receipt.status === 'running' ? 'interrupted' : receipt.status === 'completed' ? 'stale' : receipt.status;
        this.receipts.set(receipt.key + ':' + receipt.inputHash, {...receipt, status});
      }
      await this.save();
    })();
  }
  restore(snapshot: unknown): void { this.fallbackSnapshot = snapshot; }
  setActive(ids: readonly SkillId[]): void { this.activeSkills = new Set(ids.filter(id => this.enabled.includes(id))); }
  snapshot() { return snapshotSchema.parse({schemaVersion: 1, receipts: [...this.receipts.values()]}); }
  invalidate(): void {
    for (const receipt of this.receipts.values()) if (receipt.status === 'completed') receipt.status = 'stale';
  }
  private save() { return this.store.save(this.checkpoint, this.snapshot()); }
  private event(type: string, data: Record<string, unknown>) {
    return this.store.event(type, {session: this.store.scope, workspace: this.sandbox.id, ...data});
  }
  private definition(tool: string): Definition | undefined {
    return this.resources ? definitions.find(def => def.tool === tool && this.enabled.includes(def.skill)) : undefined;
  }
  async run(tool: string, input: unknown, signal: AbortSignal, execute: (hooks?: ProcedureHooks) => Promise<Record<string, unknown>>): Promise<Record<string, unknown>> {
    const definition = this.definition(tool);
    if (!definition) return execute();
    await this.initialize(); signal.throwIfAborted();
    if (this.active) throw new Error('A mutating skill procedure is already running in this workspace');
    this.active = true;
    try {
      const inputHash = hash(input), recordKey = definition.key + ':' + inputHash;
      const previous = this.receipts.get(recordKey), skillDigest = builtinSkills[definition.skill].digest;
      const resumeFiles = definition.recovery === 'recheck-owned-files' && previous?.inputHash === inputHash
        && previous.skillDigest === skillDigest && previous.plannedFiles.length ? previous.plannedFiles : undefined;
      const receipt: Receipt = {key: definition.key, skill: definition.skill, skillDigest, inputHash,
        activation: this.activeSkills.has(definition.skill) ? 'active-method' : 'available-capability',
        attempt: (previous?.attempt ?? 0) + 1, workspace: this.sandbox.id, status: 'running', stage: 'starting', plannedFiles: resumeFiles ? [...resumeFiles] : [],
        ...(previous?.evidence ? {previousEvidence: previous.evidence} : {})};
      this.receipts.delete(recordKey); this.receipts.set(recordKey, receipt);
      while (this.receipts.size > 4) {
        const key = [...this.receipts.keys()].find(key => key !== recordKey && this.receipts.get(key)!.skill !== 'framework-tests') ?? this.receipts.keys().next().value!;
        this.receipts.delete(key);
      }
      await this.save();
      await this.event('skills.procedure.started', {key: receipt.key, attempt: receipt.attempt, activation: receipt.activation, skillDigest, inputHash, resuming: !!resumeFiles});
      const hooks: ProcedureHooks = {resumeFiles, stage: async (stage, files) => {
        receipt.stage = stage; if (files) receipt.plannedFiles = files.map(file => fileSchema.parse(file));
        await this.save(); await this.event('skills.procedure.stage', {key: receipt.key, attempt: receipt.attempt, stage});
      }};
      let result: Record<string, unknown>;
      try {
        result = await execute(hooks);
        const control = result.control as {code?: number; timedOut?: boolean; stdout?: string} | undefined;
        const execution = result.execution as {code?: number; status?: string; timedOut?: boolean} | undefined;
        receipt.outcome = definition.verification === 'setup-control'
          ? result.ok === true && result.controlPassed === true && control?.code === 0 && !control.timedOut
            && control.stdout?.includes('TESTHELIX_HARNESS_CONTROL=') ? 'setup-control-pass' : 'execution-error'
          : result.written === true && result.executed === true && execution?.code != null && !execution.timedOut
            && ['pass', 'fail'].includes(execution.status ?? '') ? execution.status === 'pass' ? 'suite-pass' : 'suite-fail' : 'execution-error';
        receipt.status = receipt.outcome === 'execution-error' ? 'failed' : 'completed';
        receipt.stage = typeof result.stage === 'string' ? result.stage : 'execution';
      } catch (error) {
        receipt.status = signal.aborted ? 'interrupted' : 'failed'; receipt.outcome = 'execution-error';
        receipt.evidence = await this.store.artifact(JSON.stringify({ok: false, error: errorText(error), stage: receipt.stage}));
        await this.save(); await this.event('skills.procedure.completed', {...receipt, qualityVerified: false});
        throw error;
      }
      receipt.evidence = await this.store.artifact(JSON.stringify(result));
      await this.save(); await this.event('skills.procedure.completed', {...receipt, qualityVerified: false});
      return {...result, procedure: {key: receipt.key, attempt: receipt.attempt, status: receipt.status, outcome: receipt.outcome,
        evidence: receipt.evidence, qualityVerified: false}};
    } finally { this.active = false; }
  }
  render(): string {
    if (!this.resources) return '';
    return [...this.receipts.values()].map(r => `Host procedure ${r.key}: ${r.status}, stage=${r.stage}${r.outcome ? ', observed=' + r.outcome : ''}`
      + `${r.evidence ? ', artifact=' + r.evidence : ''}. ${r.status === 'stale' || r.status === 'interrupted' ? 'Recheck current files/execution; do not rebuild blindly.' : 'This does not certify task coverage or correctness.'}`).join('\n');
  }
}
