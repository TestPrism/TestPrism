import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeToolInput } from '../src/protocol.js';
import { reviewTool, workspaceTools } from '../src/tools.js';

const normalize = (name: string, raw: string) => normalizeToolInput(raw, workspaceTools[name]!);

test('normalizes complete fenced JSONC and schema-declared scalar strings without changing text', () => {
  const result = normalize('read_file', '```json\n{ // public file\n"path":"123", "startLine":"12", "refresh":"false",}\n```');
  assert.deepEqual(JSON.parse(result.input), {path: '123', startLine: 12, refresh: false});
  assert.deepEqual(new Set(result.changes), new Set(['json-fence', 'jsonc', 'integer-string', 'boolean-string']));
  const double = normalize('read_file', JSON.stringify('{"path":"x", "endLine":"20"}'));
  assert.deepEqual(JSON.parse(double.input), {path: 'x', endLine: 20});
  assert.deepEqual(double.changes, ['encoded-object', 'integer-string']);
  assert.deepEqual(JSON.parse(normalize('list_files', '').input), {});
});

test('normalization preserves all code, commands, patches, paths and escape sequences verbatim', () => {
  const payloads = [
    'const text = "https://host/path//x"; /* comment */\r\n',
    'const x = {"a":1,};\n// trailing comma inside code\n',
    '```json\n{"run": "false"}\n```',
    String.raw`\\server\file\n\u1234 $HOME $(touch x)`,
    'print("中文 😀 \\\" ")\n\t\u0000',
    '\"; rm -rf agent-tests; #',
  ];
  for (const payload of payloads) {
    for (const [name, input] of [
      ['write_file', {path: '01/false/😀.txt', content: payload}],
      ['edit_file', {path: 'x', old: payload, replacement: payload}],
      ['exec', {command: payload, timeoutMs: '100'}],
      ['apply_patch', {patch: payload}],
    ] as const) {
      const raw = JSON.stringify(input).slice(0, -1) + ',}';
      const expected = name === 'exec' ? {...input, timeoutMs: 100} : input;
      assert.deepEqual(JSON.parse(normalize(name, raw).input), expected);
    }
  }
});

test('rejects truncated, ambiguous and invented JSON instead of guessing a runnable call', () => {
  for (const raw of [
    '{"command":"echo done"', '{"command":"echo done",', '{"command":"echo done}',
    '{command:"echo done"}', "{'command':'echo done'}", '{"command":"echo done" "timeoutMs":100}',
    '{"command":"echo done",,}', '{"command":"echo done"} trailing explanation',
    'Here is the JSON: {"command":"echo done"}', '```json\n{"command":"echo done"}',
    '{"command":"echo done"} {"command":"echo other"}', '{"command":"echo done"} /* unfinished',
    '[{"command":"echo done"}]', 'null', JSON.stringify(JSON.stringify('{"command":"echo done"}')),
    '{"command":"echo done","command":"echo other"}',
    String.raw`{"command":"echo done","\u0063ommand":"echo other"}`,
    '{"command":"echo done","__proto__":{"polluted":true}}',
  ]) assert.throws(() => normalize('exec', raw), undefined, raw);
  assert.equal(({} as {polluted?: unknown}).polluted, undefined);
});

test('schema remains strict for missing, unknown, out-of-range and ambiguous typed fields', () => {
  for (const raw of [
    '{"command":123}', '{"timeoutMs":100}', '{"command":"x","extra":1}',
    ...['"0100"', '"+100"', '" 100 "', '"1e2"', '"100.0"', '"NaN"', '"Infinity"',
      '"9007199254740993"', '1e999', '9007199254740993', '"99"', '"300001"', 'null', 'true']
      .map(timeout => `{"command":"x","timeoutMs":${timeout}}`),
  ]) assert.throws(() => normalize('exec', raw), undefined, raw);
  for (const refresh of ['"False"', '"0"', '"yes"', '0', 'null'])
    assert.throws(() => normalize('read_file', `{"path":"x","refresh":${refresh}}`));
  assert.throws(() => normalize('submit_candidate', '{"sourcePaths":"x","requirements":["x"],"rationale":"x"}'));
  assert.doesNotThrow(() => normalize('submit_candidate', '{"requirements":["x"],"rationale":"x"}'));
  assert.throws(() => normalize('submit_candidate', '{"rationale":"x"}'));
  assert.throws(() => normalize('update_notes', '{}'), /Supply notes or entries/);
  assert.throws(() => normalize('update_notes', '{"entries":[{"id":"a","id":"b","kind":"fact","text":"x"}]}'), /Duplicate/);
  // The low-level executor's strict validator does not gain coercion.
  assert.throws(() => workspaceTools.exec!.parse!({command: 'x', timeoutMs: '100'}));
  const review = normalizeToolInput('{"findings":[],"coveredRequirements":[]}', reviewTool);
  assert.deepEqual(JSON.parse(review.input).findings, []);
  assert.throws(() => normalizeToolInput('{"findings":[],"coveredRequirements":[],"accepted":"false"}', reviewTool));
});

test('bounds JSON nesting and token count while accepting large strings and remaining idempotent', () => {
  assert.throws(() => normalize('update_notes', '{"entries":' + '['.repeat(40) + '0' + ']'.repeat(40) + '}'), /nesting/);
  assert.throws(() => normalize('update_notes', '{"entries":[' + '0,'.repeat(20_000) + '0]}'), /too many/);
  assert.throws(() => normalize('write_file', ' '.repeat(8_000_001)), /character limit/);
  const content = '😀'.repeat(450_000);
  const once = normalize('write_file', JSON.stringify({path: 'x', content}));
  assert.equal(JSON.parse(once.input).content, content);
  assert.deepEqual(normalize('write_file', once.input), once);
  const repaired = normalize('read_file', '{"path":"x", "refresh":"false",}');
  assert.deepEqual(normalize('read_file', repaired.input), {input: repaired.input, changes: []});
});
