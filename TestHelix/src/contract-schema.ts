import {z} from 'zod';

// These ceilings bound host work; lower limits can be configured per run.
export const CONTRACT_CEILINGS = Object.freeze({maxClauses: 64, maxBytes: 65536, maxDepth: 16,
  maxNodes: 4096, maxSchemaBytes: 8192, maxCheckBytes: 4000000});
export const contractLimitsSchema = z.strictObject({
  maxClauses: z.number().int().min(1).max(CONTRACT_CEILINGS.maxClauses).default(32),
  maxBytes: z.number().int().min(1024).max(CONTRACT_CEILINGS.maxBytes).default(32768),
  maxDepth: z.number().int().min(4).max(CONTRACT_CEILINGS.maxDepth).default(12),
  maxNodes: z.number().int().min(32).max(CONTRACT_CEILINGS.maxNodes).default(2048),
  maxSchemaBytes: z.number().int().min(256).max(CONTRACT_CEILINGS.maxSchemaBytes).default(8192),
  maxCheckBytes: z.number().int().min(256).max(CONTRACT_CEILINGS.maxCheckBytes).default(1000000),
});
export type ContractLimits = z.infer<typeof contractLimitsSchema>;

/** Bound data before recursive schema parsing/JSON encoding, including direct tool calls. */
export function assertJsonBudget(value: unknown, limits: {maxBytes: number; maxDepth: number; maxNodes: number}): void {
  const pending = [{value, depth: 0}], seen = new WeakSet<object>();
  let nodes = 0, bytes = 0;
  while (pending.length) {
    const {value: item, depth} = pending.pop()!;
    if (++nodes > limits.maxNodes) throw new Error('Contract JSON exceeds node limit');
    if (depth > limits.maxDepth) throw new Error('Contract JSON exceeds depth limit');
    if (typeof item === 'string') bytes += Buffer.byteLength(item);
    else if (item !== null && typeof item === 'object') {
      if (seen.has(item)) throw new Error('Contract must be an acyclic JSON tree');
      seen.add(item);
      if (Array.isArray(item) && item.length > limits.maxNodes) throw new Error('Contract JSON exceeds node limit');
      if (!Array.isArray(item) && ![Object.prototype, null].includes(Object.getPrototypeOf(item)))
        throw new Error('Contract must contain plain JSON objects');
      const keys = Object.keys(item);
      if (keys.length + nodes > limits.maxNodes) throw new Error('Contract JSON exceeds node limit');
      for (const key of keys) {
        if (['__proto__', 'constructor', 'prototype'].includes(key)) throw new Error('Reserved contract JSON key');
        const property = Object.getOwnPropertyDescriptor(item, key)!;
        if (!('value' in property)) throw new Error('Contract JSON cannot contain accessors');
        bytes += Buffer.byteLength(key);
        pending.push({value: property.value, depth: depth + 1});
      }
    } else if (item !== null && typeof item !== 'boolean' && !(typeof item === 'number' && Number.isFinite(item)
        && (!Number.isInteger(item) || Number.isSafeInteger(item))))
      throw new Error('Contract must contain JSON values');
    if (bytes > limits.maxBytes) throw new Error('Contract JSON exceeds byte limit');
  }
  if (Buffer.byteLength(JSON.stringify(value)) > limits.maxBytes) throw new Error('Contract JSON exceeds byte limit');
}

type Scalar = string | number | boolean | null;
export interface ValueSchema {
  type: 'object' | 'array' | 'string' | 'number' | 'integer' | 'boolean' | 'null';
  properties?: Record<string, ValueSchema>; required?: string[]; additionalProperties?: boolean;
  items?: ValueSchema; enum?: Scalar[];
  minimum?: number; maximum?: number; minLength?: number; maxLength?: number; minItems?: number; maxItems?: number;
}
const count = z.number().int().nonnegative();
/** An explicit JSON Schema subset: unsupported keywords fail instead of being ignored. */
export const valueSchemaWire: z.ZodType<ValueSchema> = z.lazy(() => z.strictObject({
  type: z.enum(['object', 'array', 'string', 'number', 'integer', 'boolean', 'null']),
  properties: z.record(z.string().min(1).max(200), valueSchemaWire).optional(),
  required: z.array(z.string().min(1).max(200)).max(64).optional(), additionalProperties: z.boolean().optional(),
  items: valueSchemaWire.optional(), enum: z.array(z.union([z.string(), z.number(), z.boolean(), z.null()])).min(1).max(64).optional(),
  minimum: z.number().optional(), maximum: z.number().optional(), minLength: count.optional(), maxLength: count.optional(),
  minItems: count.optional(), maxItems: count.optional(),
}));

export function checkSchemaDefinition(schema: ValueSchema): void {
  const fields: Record<string, string[]> = {properties: ['object'], required: ['object'], additionalProperties: ['object'],
    items: ['array'], minItems: ['array'], maxItems: ['array'], minLength: ['string'], maxLength: ['string'],
    minimum: ['number', 'integer'], maximum: ['number', 'integer']};
  for (const [key, types] of Object.entries(fields)) if (Object.hasOwn(schema, key) && !types.includes(schema.type))
    throw new Error(`Schema ${key} is incompatible with type ${schema.type}`);
  for (const [min, max] of [[schema.minimum, schema.maximum], [schema.minLength, schema.maxLength], [schema.minItems, schema.maxItems]])
    if (min !== undefined && max !== undefined && min > max) throw new Error('Schema minimum exceeds maximum');
  for (const child of Object.values(schema.properties ?? {})) checkSchemaDefinition(child);
  if (schema.items) checkSchemaDefinition(schema.items);
}

/** Validate one bounded parsed document. Return no data values in diagnostics. */
export function checkJsonValue(value: unknown, schema: ValueSchema, location = '$'): string | undefined {
  const kind = value === null ? 'null' : Array.isArray(value) ? 'array' : typeof value;
  if (schema.type === 'integer' ? typeof value !== 'number' || !Number.isInteger(value) : kind !== schema.type)
    return `${location}: expected ${schema.type}`;
  if (schema.enum && !schema.enum.some(item => item === value)) return `${location}: value is outside enum`;
  if (typeof value === 'number' && (!Number.isFinite(value) || schema.minimum !== undefined && value < schema.minimum
      || schema.maximum !== undefined && value > schema.maximum)) return `${location}: number is outside range`;
  if (typeof value === 'string') {
    const size = [...value].length;
    if (schema.minLength !== undefined && size < schema.minLength || schema.maxLength !== undefined && size > schema.maxLength)
      return `${location}: string length is outside range`;
  }
  if (Array.isArray(value)) {
    if (schema.minItems !== undefined && value.length < schema.minItems || schema.maxItems !== undefined && value.length > schema.maxItems)
      return `${location}: array length is outside range`;
    if (schema.items) for (let i = 0; i < value.length; i++) {
      const error = checkJsonValue(value[i], schema.items, `${location}[${i}]`);
      if (error) return error;
    }
  } else if (value !== null && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    for (const key of schema.required ?? []) if (!Object.hasOwn(record, key)) return `${location}.${key}: required field is missing`;
    for (const key of Object.keys(record)) {
      const property = schema.properties && Object.hasOwn(schema.properties, key) ? schema.properties[key] : undefined;
      if (!property && schema.additionalProperties === false) return `${location}: additional field is not allowed`;
      const error = property ? checkJsonValue(record[key], property, `${location}.${key}`) : undefined;
      if (error) return error;
    }
  }
}
