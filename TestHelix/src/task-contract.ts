import { z } from 'zod';
import {publicPath, testPath} from './util.js';
import type {FileArtifact} from './types.js';
import type {Sandbox} from './runtime.js';
import {assertJsonBudget, checkJsonValue, checkSchemaDefinition, CONTRACT_CEILINGS, contractLimitsSchema,
  valueSchemaWire, type ContractLimits} from './contract-schema.js';

/** Host-owned invariants. A model may report these, but cannot change them. */
export const HOST_HARD_CONSTRAINTS = [
  'public-task-authoritative',
  'export-root-agent-tests',
  'runner-required',
  'no-hidden-benchmark-data',
  'bounded-artifacts',
  'source-repairs-internal',
] as const;

const target = z.string().min(1).max(1024).describe('Public workspace-relative file, or stdin/stdout/return for runtime evidence.');
export const constraintWireSchema = z.discriminatedUnion('kind', [
  z.strictObject({kind: z.literal('path'), path: z.string().min(1).max(1024),
    type: z.enum(['file', 'directory']).default('file'), executable: z.boolean().optional()}),
  z.strictObject({kind: z.literal('format'), target, format: z.enum(['utf8', 'json', 'jsonl'])}),
  z.strictObject({kind: z.literal('schema'), target, schema: valueSchemaWire}),
  z.strictObject({kind: z.literal('cardinality'), target, unit: z.enum(['bytes', 'lines', 'items']),
    min: z.number().int().nonnegative().optional(), max: z.number().int().nonnegative().optional()}),
  z.strictObject({kind: z.literal('note'), text: z.string().min(1).max(4000)}),
]);
export type ContractConstraint = z.infer<typeof constraintWireSchema>;
// Only structured clauses are advertised to models. Parsing also supports old saved declarations.
export const outputContractWireSchema = z.strictObject({
  version: z.literal(2).default(2),
  inputs: z.array(constraintWireSchema).max(CONTRACT_CEILINGS.maxClauses).default([]),
  outputs: z.array(constraintWireSchema).max(CONTRACT_CEILINGS.maxClauses).default([]),
  entrypoint: z.string().min(1).max(200).default('agent-tests/run-tests.sh'),
  hardConstraints: z.array(z.enum(HOST_HARD_CONSTRAINTS)).max(HOST_HARD_CONSTRAINTS.length).default([]),
  rationale: z.string().max(1000).default(''),
});
export type OutputContract = z.infer<typeof outputContractWireSchema>;
const legacyContractSchema = z.strictObject({
  inputs: z.array(z.string().min(1).max(500)).max(24).default([]),
  outputs: z.array(z.string().min(1).max(500)).max(24).default([]),
  entrypoint: z.string().min(1).max(200).default('agent-tests/run-tests.sh'),
  hardConstraints: z.array(z.enum(HOST_HARD_CONSTRAINTS)).max(HOST_HARD_CONSTRAINTS.length).default([]),
  rationale: z.string().max(1000).default(''),
});
function boundedDeclaration(value: unknown, limits: ContractLimits): void {
  assertJsonBudget(value, limits);
  const record = value as {inputs?: unknown; outputs?: unknown} | null;
  const clauses = [record?.inputs, record?.outputs].flatMap(items => Array.isArray(items) ? items : []);
  if (clauses.length > limits.maxClauses) throw new Error('Contract exceeds clause limit');
  for (const clause of clauses) {
    if (clause?.kind === 'schema') {
      assertJsonBudget(clause.schema, {...limits, maxBytes: limits.maxSchemaBytes});
    }
  }
}
export const outputContractSchema = z.preprocess((value, ctx) => {
  try {
    boundedDeclaration(value, CONTRACT_CEILINGS);
    const record = value as {version?: unknown; inputs?: unknown; outputs?: unknown} | null;
    const clauses = [record?.inputs, record?.outputs].flatMap(items => Array.isArray(items) ? items : []);
    if (record?.version === undefined && clauses.some(item => typeof item === 'string')) {
      const old = legacyContractSchema.parse(value);
      return {...old, version: 2, inputs: old.inputs.map(text => ({kind: 'note', text})),
        outputs: old.outputs.map(path => ({kind: 'path', path, type: path.endsWith('/') ? 'directory' : 'file'}))};
    }
    return value;
  } catch (error) {
    if (error instanceof z.ZodError) for (const issue of error.issues.slice(0, 3))
      ctx.addIssue({code: 'custom', path: issue.path, message: issue.message});
    else ctx.addIssue({code: 'custom', message: (error as Error).message});
    return z.NEVER;
  }
}, outputContractWireSchema).superRefine((value, ctx) => {
  for (const side of ['inputs', 'outputs'] as const) value[side].forEach((clause, index) => {
    try {
      if (clause.kind === 'schema') checkSchemaDefinition(clause.schema);
      if (clause.kind === 'cardinality' && (clause.min === undefined && clause.max === undefined
          || clause.min !== undefined && clause.max !== undefined && clause.min > clause.max))
        throw new Error('Cardinality needs min or max, with min <= max');
    } catch (error) {ctx.addIssue({code: 'custom', path: [side, index], message: (error as Error).message});}
  });
});

export type TaskContractMode = 'tests-only' | 'source-repair' | 'mixed' | 'unspecified';
export interface InferredTaskContract {
  mode: TaskContractMode;
  requiredOutputs: string[];
  inputConstraints: string[];
  hardConstraints: typeof HOST_HARD_CONSTRAINTS[number][];
  evidence: string[];
}
export interface ContractValidation {
  compatible: boolean;
  errors: string[];
  warnings: string[];
  inferred: InferredTaskContract;
  declared?: OutputContract;
  effective: OutputContract;
  origin: 'model' | 'host-default';
  checks: Array<{side: 'inputs' | 'outputs'; index: number; kind: ContractConstraint['kind'];
    status: 'verified' | 'failed' | 'unverified'; detail: string}>;
}

const clip = (value: string, limit = 300) => value.trim().replace(/\s+/g, ' ').slice(0, limit);
const lines = (instruction: string) => instruction.split(/\r?\n/).map(line => clip(line)).filter(Boolean);

/** Infer only bounded, auditable facts from the public task text. */
export function inferTaskContract(instruction: string): InferredTaskContract {
  const text = instruction.trim();
  const lower = text.toLowerCase();
  const allLines = lines(text);
  const outputs = new Set<string>();
  const evidence: string[] = [];
  for (const match of text.matchAll(/\b(?:fixed|corrected|patched)[-_][A-Za-z0-9_.-]+\.(?:py|js|ts|java|go|rs|c|cpp|rb|php)\b/gi)) outputs.add(match[0]!);
  for (const match of text.matchAll(/\b(?:fixed|corrected)\s+[A-Za-z0-9_.-]+\.(?:py|js|ts|java|go|rs|c|cpp)\b/gi)) outputs.add(match[0]!);
  const sourceRepair = /\b(fixed[-_][\w.-]+\.(?:py|js|ts|java|go|rs|c|cpp|rb|php)|corrected\s+(?:program|version|file)|source\s+(?:repair|modification|change)|modify\s+(?:the\s+)?source|change\s+exactly\s+(?:one|1)\s+line)\b/i.test(text);
  const testOutput = /agent-tests(?:\/|\\)|run-tests\.sh|regression\s+(?:test|suite)|test\s+cases?/i.test(text);
  if (/agent-tests(?:\/|\\)/i.test(text)) outputs.add('agent-tests/');
  if (/run-tests\.sh/i.test(text)) outputs.add('agent-tests/run-tests.sh');
  const mode: TaskContractMode = sourceRepair && testOutput ? 'mixed' : sourceRepair ? 'source-repair' : testOutput ? 'tests-only' : 'unspecified';
  const inputConstraints: string[] = [];
  for (const line of allLines) {
    if (/\b(?:input|format|constraint|requirement|data|argument|parameter|return)\b/i.test(line)) inputConstraints.push(line);
    if (inputConstraints.length >= 16) break;
  }
  for (const line of allLines) if (/\b(fixed[-_][\w.-]+|run-tests\.sh|agent-tests(?:\/|\\))\b/i.test(line)) evidence.push(line);
  if (sourceRepair) evidence.push('Public instruction contains a source-repair requirement.');
  if (testOutput) evidence.push('Public instruction contains a test-artifact or runner requirement.');
  const hard = [...HOST_HARD_CONSTRAINTS];
  if (!lower) return {mode: 'unspecified', requiredOutputs: [], inputConstraints: [], hardConstraints: hard, evidence: []};
  return {mode, requiredOutputs: [...outputs].slice(0, 24), inputConstraints, hardConstraints: hard, evidence: evidence.slice(0, 16)};
}

/** Validate a model declaration against host rules and the public task contract. */
export function validateOutputContract(declared: unknown, inferred: InferredTaskContract,
  protocol: 'tests-only' | 'internal' = 'tests-only', rawLimits: Partial<ContractLimits> = {}): ContractValidation {
  const limits = contractLimitsSchema.parse(rawLimits);
  const errors: string[] = [], warnings: string[] = [];
  let parsed: OutputContract | undefined;
  if (declared !== undefined) {
    try {boundedDeclaration(declared, limits); parsed = outputContractSchema.parse(declared);}
    catch (error) {
      const detail = error instanceof z.ZodError ? error.issues.slice(0, 3).map(issue => `${issue.path.join('.')}: ${issue.message}`).join('; ')
        : (error as Error).message;
      errors.push(`contract declaration is invalid: ${detail}`);
    }
  }
  if (protocol === 'tests-only' && (inferred.mode === 'source-repair' || inferred.mode === 'mixed'))
    errors.push(`public task requires source artifacts (${inferred.requiredOutputs.join(', ') || 'source changes'}), but this adapter exports agent-tests/ only`);
  if (parsed) {
    if (parsed.entrypoint !== 'agent-tests/run-tests.sh') errors.push(`contract.entrypoint must be agent-tests/run-tests.sh, got ${parsed.entrypoint}`);
    for (const side of ['inputs', 'outputs'] as const) for (const clause of parsed[side]) {
      const location = clause.kind === 'path' ? clause.path : 'target' in clause ? clause.target : undefined;
      if (!location || clause.kind !== 'path' && channels.has(location)) continue;
      try {
        const file = publicPath(location);
        if (side === 'outputs' && !(clause.kind === 'path' && clause.type === 'directory' && file.replace(/\/$/, '') === 'agent-tests'))
          testPath(file);
      } catch (error) {errors.push(`contract.${side} path rejected: ${(error as Error).message}`);}
    }
    for (const required of inferred.requiredOutputs) if (required !== 'agent-tests/' && required !== 'agent-tests/run-tests.sh'
        && !parsed.outputs.some(clause => clause.kind === 'path' && clause.path === required))
      warnings.push(`declared contract does not mention public required output ${required}`);
    for (const required of HOST_HARD_CONSTRAINTS) if (!parsed.hardConstraints.includes(required))
      warnings.push(`declared contract omitted host hard constraint ${required}`);
  }
  const effective: OutputContract = {...(parsed ?? outputContractWireSchema.parse({})), hardConstraints: [...HOST_HARD_CONSTRAINTS]};
  // Host defaults remain effective even for an empty/omitted model declaration.
  effective.outputs = [...effective.outputs];
  if (!effective.outputs.some(clause => clause.kind === 'path' && clause.path === 'agent-tests/run-tests.sh' && clause.type === 'file'))
    effective.outputs.push({kind: 'path', path: 'agent-tests/run-tests.sh', type: 'file'});
  return {compatible: errors.length === 0, errors, warnings, inferred, effective,
    origin: declared === undefined ? 'host-default' : 'model', checks: [], ...(parsed ? {declared: parsed} : {})};
}

const channels = new Set(['stdin', 'stdout', 'return']);
const decode = (data: Buffer) => new TextDecoder('utf-8', {fatal: true}).decode(data);
function parsedDocument(data: Buffer, limits: ContractLimits): unknown {
  let value: unknown;
  try {value = JSON.parse(decode(data));} catch {throw new Error('Expected a complete UTF-8 JSON document');}
  assertJsonBudget(value, {maxBytes: limits.maxCheckBytes, maxDepth: 32, maxNodes: 32768});
  return value;
}

/** Verify files against immutable public inputs and the exact captured output bundle.
 * Notes and runtime channels stay explicitly unverified; no model-supplied code runs here.
 */
export async function verifyContractArtifacts(report: ContractValidation, baseline: Sandbox, tests: readonly FileArtifact[],
  rawLimits: Partial<ContractLimits> = {}, artifactByteBudget = Infinity): Promise<ContractValidation> {
  const limits = contractLimitsSchema.parse(rawLimits);
  limits.maxCheckBytes = Math.min(limits.maxCheckBytes, artifactByteBudget);
  const result: ContractValidation = {...report, checks: [], errors: [...report.errors], warnings: [...report.warnings]};
  if (!result.compatible) return result;
  const outputs = new Map(tests.map(file => [file.path, file]));
  const cache = new Map<string, Buffer>();
  let checkedBytes = 0;
  const read = async (side: 'inputs' | 'outputs', location: string): Promise<Buffer> => {
    const file = publicPath(location), key = side + ':' + file;
    if (cache.has(key)) return cache.get(key)!;
    const artifact = outputs.get(file);
    const data = side === 'outputs'
      ? artifact && !artifact.deleted ? Buffer.from(artifact.data, 'base64') : undefined
      : await baseline.read(file, limits.maxCheckBytes - checkedBytes);
    if (!data) throw new Error('Required exported file is missing');
    checkedBytes += data.length;
    if (checkedBytes > limits.maxCheckBytes) throw new Error('Contract file checks exceed byte budget');
    cache.set(key, data); return data;
  };
  for (const side of ['inputs', 'outputs'] as const) for (const [index, clause] of result.effective[side].entries()) {
    if (clause.kind === 'note' || 'target' in clause && channels.has(clause.target)) {
      result.checks.push({side, index, kind: clause.kind, status: 'unverified',
        detail: clause.kind === 'note' ? 'Natural-language claim needs public evidence and semantic review'
          : 'Runtime channel needs execution evidence; artifact capture cannot verify it'});
      continue;
    }
    try {
      if (clause.kind === 'path') {
        const file = publicPath(clause.path).replace(/\/$/, '');
        if (clause.type === 'directory') {
          const files = side === 'outputs' ? tests.map(item => item.path) : await baseline.list(file);
          if (!files.some(item => item.startsWith(file + '/'))) throw new Error('Required directory has no visible files');
          if (clause.executable !== undefined) throw new Error('Executable checks apply to files only');
        } else {
          await read(side, file);
          if (clause.executable !== undefined) {
            const mode = side === 'outputs' ? outputs.get(file)!.mode : await baseline.mode(file);
            if (Boolean(mode & 0o111) !== clause.executable) throw new Error('File executable permission does not match');
          }
        }
      } else {
        const data = await read(side, clause.target);
        if (clause.kind === 'format') {
          if (clause.format === 'json') parsedDocument(data, limits);
          else if (clause.format === 'utf8') decode(data);
          else {
            const records = decode(data).split(/\r?\n/);
            if (records.at(-1) === '') records.pop();
            if (!records.length || records.length > 32768) throw new Error('JSONL needs 1..32768 nonempty records');
            for (const record of records) parsedDocument(Buffer.from(record), limits);
          }
        } else if (clause.kind === 'schema') {
          const error = checkJsonValue(parsedDocument(data, limits), clause.schema);
          if (error) throw new Error(error);
        } else {
          const document = clause.unit === 'items' ? parsedDocument(data, limits) : undefined;
          if (clause.unit === 'items' && !Array.isArray(document)) throw new Error('Item cardinality requires a JSON array');
          const text = clause.unit === 'lines' ? decode(data) : '';
          const count = clause.unit === 'bytes' ? data.length : clause.unit === 'items' ? (document as unknown[]).length
            : text ? text.split(/\r?\n/).length - (text.endsWith('\n') ? 1 : 0) : 0;
          if (clause.min !== undefined && count < clause.min || clause.max !== undefined && count > clause.max)
            throw new Error(`${clause.unit} cardinality is outside declared range`);
        }
      }
      result.checks.push({side, index, kind: clause.kind, status: 'verified', detail: 'Checked public baseline input or captured output bytes'});
    } catch (error) {
      const detail = (error as Error).message.slice(0, 500);
      result.checks.push({side, index, kind: clause.kind, status: 'failed', detail});
      result.errors.push(`contract.${side}[${index}]: ${detail}`);
    }
  }
  result.compatible = result.errors.length === 0;
  return result;
}
