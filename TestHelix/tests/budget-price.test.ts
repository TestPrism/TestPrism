import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import {Budget} from '../src/budget.js';
import {config, fixture} from './helpers.js';

test('cache discounts apply only to settled usage and survive resume without discounting unknown requests', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = config();
  Object.assign(c.model, {inputPricePerMillion: 2.5, cachedInputPricePerMillion: .25, outputPricePerMillion: 15});
  const file = path.join(f.root, 'prices.json'), budget = await Budget.open(c, file, 'priced', false);
  const first = await budget.reserve(10000, 1000);
  assert.equal(budget.snapshot().costUSD, .04);
  await budget.settle(first, {input: 10000, cached: 8000, output: 1000});
  assert.equal(budget.snapshot().costUSD, .022);
  await budget.reserve(10000, 1000);
  const resumed = await Budget.open(c, file, 'priced', true);
  assert.equal(resumed.snapshot().estimated, true);
  assert.equal(resumed.snapshot().costUSD, .062);
});

test('a dollar ceiling cannot assume a cache hit before the provider confirms one', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = config();
  Object.assign(c.model, {inputPricePerMillion: 2.5, cachedInputPricePerMillion: .25, outputPricePerMillion: 15});
  c.budget.maxCostUSD = .02;
  const budget = await Budget.open(c, path.join(f.root, 'ceiling.json'), 'ceiling', false);
  await assert.rejects(budget.reserve(10000, 1000), /budget exhausted/);
  assert.equal(budget.snapshot().calls, 0);
});
