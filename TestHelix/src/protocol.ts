import { createScanner, parseTree, printParseErrorCode, SyntaxKind, type Node, type ParseError } from 'jsonc-parser';
import type { ToolSpec } from './types.js';

const maxInputChars = 8_000_000;
const maxDepth = 32;
const maxTokens = 32_768;
type Schema = Record<string, unknown>;
const record = (value: unknown): value is Schema => value !== null && typeof value === 'object' && !Array.isArray(value);

/** Parse complete JSON/JSONC only. The parser's recovery tree is NEVER accepted on error. */
function parseComplete(text: string): Node {
  const scanner = createScanner(text, true);
  let depth = 0, tokens = 0;
  for (let token = scanner.scan(); token !== SyntaxKind.EOF; token = scanner.scan()) {
    if (++tokens > maxTokens) throw new Error('Tool arguments contain too many JSON tokens');
    if (token === SyntaxKind.OpenBraceToken || token === SyntaxKind.OpenBracketToken) {
      if (++depth > maxDepth) throw new Error('Tool arguments exceed the JSON nesting limit');
    } else if (token === SyntaxKind.CloseBraceToken || token === SyntaxKind.CloseBracketToken) depth = Math.max(0, depth - 1);
  }
  const errors: ParseError[] = [];
  const root = parseTree(text, errors, {allowTrailingComma: true, disallowComments: false, allowEmptyContent: false});
  if (errors.length || !root) {
    const error = errors[0];
    throw new Error(error ? `Incomplete or invalid JSON: ${printParseErrorCode(error.error)} at offset ${error.offset}` : 'Expected JSON object');
  }
  return root;
}

function valueOf(node: Node): unknown {
  if (node.type === 'object') {
    const entries: Array<[string, unknown]> = [], keys = new Set<string>();
    for (const property of node.children ?? []) {
      const key = property.children![0]!.value as string;
      // JSON.parse would silently use the last value, including escaped duplicates.
      if (keys.has(key)) throw new Error('Duplicate JSON object key; specify each field once');
      if (['__proto__', 'constructor', 'prototype'].includes(key)) throw new Error('Reserved JSON object key');
      keys.add(key); entries.push([key, valueOf(property.children![1]!)]);
    }
    return Object.fromEntries(entries);
  }
  if (node.type === 'array') return (node.children ?? []).map(valueOf);
  if (node.type === 'number' && (!Number.isFinite(node.value) || (Number.isInteger(node.value) && !Number.isSafeInteger(node.value))))
    throw new Error('JSON number must be finite and integers must be exactly representable');
  return node.value;
}

/** Only unambiguous scalar types declared by the schema may change. Text stays text. */
function normalizeTypes(value: unknown, schema: Schema, changes: Set<string>): unknown {
  if (typeof value === 'string') {
    if (schema.type === 'boolean' && (value === 'true' || value === 'false')) {
      changes.add('boolean-string'); return value === 'true';
    }
    if (schema.type === 'integer' && /^(?:0|-?[1-9]\d*)$/.test(value) && Number.isSafeInteger(Number(value))) {
      changes.add('integer-string'); return Number(value);
    }
    return value;
  }
  if (Array.isArray(value) && schema.type === 'array' && record(schema.items)) {
    const items = schema.items;
    return value.map(item => normalizeTypes(item, items, changes));
  }
  if (record(value) && schema.type === 'object' && record(schema.properties)) {
    const properties = schema.properties;
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key,
      Object.hasOwn(properties, key) && record(properties[key]) ? normalizeTypes(item, properties[key], changes) : item]));
  }
  return value;
}

function validate(spec: ToolSpec, input: unknown): void {
  if (!spec.parse) return; // Some injected test tools have only wire schemas; production tools all have parsers.
  try { spec.parse(input); }
  catch (error) {
    const issues = record(error) && Array.isArray(error.issues) ? error.issues : undefined;
    const detail = issues ? issues.slice(0, 3).map(issue => `${issue.path?.join('.') || '$'}: ${issue.message}`).join('; ')
      : 'Input does not satisfy this tool schema';
    throw new Error(`Invalid tool arguments: ${detail.slice(0, 400)}`);
  }
}

export interface NormalizedToolInput { input: string; changes: string[] }

/** Deterministic boundary normalization, with no LLM calls and no inferred code or missing fields. */
export function normalizeToolInput(raw: string, spec: ToolSpec): NormalizedToolInput {
  if (raw.length > maxInputChars) throw new Error('Tool arguments exceed the 8 million character limit');
  const changes = new Set<string>();
  let text = raw.trim();
  const fence = /^```(?:json|jsonc)?\r?\n([\s\S]*)\r?\n```$/i.exec(text);
  if (fence) { text = fence[1]!.trim(); changes.add('json-fence'); }
  // Preserve the SDK's empty-argument convention for tools with no required fields.
  if (!text && !fence && raw.trim() === '') { text = '{}'; changes.add('empty-object'); }
  let root = parseComplete(text);
  if (root.type === 'string') {
    text = root.value as string;
    root = parseComplete(text); // At most one extra encoding layer, and still a complete object.
    changes.add('encoded-object');
  }
  if (root.type !== 'object') throw new Error('Tool arguments must be a JSON object');
  const parsed = valueOf(root);
  try { JSON.parse(text); } catch { changes.add('jsonc'); }
  const input = normalizeTypes(parsed, spec.schema, changes);
  validate(spec, input); // Do not drop unknown keys, guess required fields, or conceal range violations.
  return {input: JSON.stringify(input), changes: [...changes]};
}
