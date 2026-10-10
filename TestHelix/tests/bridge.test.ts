import test from 'node:test';
import assert from 'node:assert/strict';
import { command } from '../src/util.js';

test('native Python bridge forwards exact system channel, Docker identity and token usage through a real Node process', async () => {
  const result = await command(['python3', '-B', 'tests/bridge_contract.py', 'integrations/testprism/agent.py'], {timeoutMs: 10000});
  assert.equal(result.code, 0, result.stdout + result.stderr);
});

test('external integration preflight and launcher preserve benchmark files, scorer methods and frozen selection', async () => {
  const result = await command(['python3', '-B', 'tests/testprism_integration_contract.py'], {timeoutMs: 20000});
  assert.equal(result.code, 0, result.stdout + result.stderr);
});

test('native diagnostic defers optional imports without changing implementations or external files', async () => {
  const result = await command(['python3', '-B', 'tests/native_diagnostic_contract.py'], {timeoutMs: 10000});
  assert.equal(result.code, 0, result.stdout + result.stderr);
});
