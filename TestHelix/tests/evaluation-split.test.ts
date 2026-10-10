import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {command} from '../src/util.js';
import {fixture} from './helpers.js';

test('host evaluation rejects implicit or contradictory hardest sampling before accessing task references', async t => {
  const f = await fixture(); t.after(f.cleanup);
  // No task, source image, or reference exists here. A split mismatch must fail first.
  const base = {taskDirectory: f.root, imageId: 'sha256:' + '0'.repeat(64), root: '/testbed',
    eligibleForEvolutionValidation: false};
  for (const [split, excludes, flag, expected] of [
    ['hardest500', false, false, /explicit matching flag/],
    ['development', true, true, /explicit matching flag/],
    ['hardest500', true, true, /Inconsistent diagnostic split/],
    [undefined, false, false, /Inconsistent diagnostic split/],
  ] as const) {
    await fs.writeFile(path.join(f.root, 'provenance.json'), JSON.stringify({...base, sampleSplit: split, excludesHardest500: excludes}));
    const result = await command([process.execPath, '--import', 'tsx', 'scripts/evaluate_model_diagnostic.ts',
      '--diagnostic', f.root, ...(flag ? ['--hardest-sample'] : [])], {timeoutMs: 10000});
    assert.equal(result.code, 1);
    assert.match(result.stderr, expected);
    await assert.rejects(fs.stat(path.join(f.root, 'evaluation')), /ENOENT/);
  }
});
