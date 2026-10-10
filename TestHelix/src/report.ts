import { z } from 'zod';
import type { Inventory } from './benchmark.js';
import { hash } from './util.js';

// This module consumes frozen evaluation results. It is never imported by the agent.
const status = z.enum(['passed', 'failed']).nullable();
const patch = z.object({patch_id: z.string(), label: z.enum(['oracle_valid', 'oracle_invalid']),
  observed_status: status, failure_mode: z.string(), matched_expectation: z.boolean().optional()});
export const trialSchema = z.object({
  task_id: z.string(), task_key: z.string().optional(), baseline_detected: z.boolean().nullish().transform(v => v ?? false),
  patch_results: z.array(patch).default([]), total_input_tokens: z.number().nonnegative().nullish(),
  total_output_tokens: z.number().nonnegative().nullish(), failure_mode: z.string().default('none'),
  is_resolved: z.boolean().nullish(), patch_set_digest: z.string().optional(),
});
export type Trial = z.infer<typeof trialSchema>;
export function strictScore(trial: Trial) {
  const valid = trial.patch_results.filter(p => p.label === 'oracle_valid');
  const invalid = trial.patch_results.filter(p => p.label === 'oracle_invalid');
  const correct = (p: z.infer<typeof patch>) => p.failure_mode === 'none'
    && p.observed_status === (p.label === 'oracle_valid' ? 'passed' : 'failed');
  const errors = trial.patch_results.filter(p => p.failure_mode !== 'none' || p.observed_status === null).length;
  const schemaOK = valid.length === 5 && invalid.length === 5 && new Set(trial.patch_results.map(p => p.patch_id)).size === 10;
  const validAcceptance = valid.length ? valid.filter(correct).length / valid.length : 0;
  const invalidRejection = invalid.length ? invalid.filter(correct).length / invalid.length : 0;
  const resolved = schemaOK && trial.baseline_detected && validAcceptance === 1 && invalidRejection === 1 && errors === 0;
  return {resolved, schemaOK, baselineDetected: trial.baseline_detected, validAcceptance, invalidRejection, patchErrors: errors};
}
export function wilson(success: number, total: number): [number, number] {
  if (!total) return [0, 1];
  const z = 1.959963984540054, p = success / total, den = 1 + z * z / total;
  const center = (p + z * z / (2 * total)) / den;
  const margin = z * Math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / den;
  return [Math.max(0, center - margin), Math.min(1, center + margin)];
}
export function summarize(trials: Trial[], inventory: Inventory) {
  const records = new Map<string, Trial>(), expected = new Set(inventory.tasks.map(t => t.key));
  for (const trial of trials) {
    let key = trial.task_key ?? trial.task_id;
    if (!expected.has(key)) {
      const matches = inventory.tasks.filter(t => t.id === key || `${t.source}__${t.id}` === key);
      if (matches.length !== 1) throw new Error(`Unknown or ambiguous evaluation task: ${key}`);
      key = matches[0]!.key;
    }
    if (records.has(key)) throw new Error(`Duplicate trial for ${key}; choose a fixed seed/attempt, do not cherry-pick`);
    records.set(key, trial);
  }
  const rows = inventory.tasks.map(task => {
    const trial = records.get(task.key);
    return {key: task.key, source: task.source, present: !!trial,
      ...(trial ? strictScore(trial) : {resolved: false, schemaOK: false, baselineDetected: false, validAcceptance: 0, invalidRejection: 0, patchErrors: 0}),
      inputTokens: trial?.total_input_tokens ?? 0, outputTokens: trial?.total_output_tokens ?? 0,
      declaredScoreMismatch: trial?.is_resolved != null && trial.is_resolved !== strictScore(trial).resolved};
  });
  const aggregate = (subset: typeof rows) => ({expected: subset.length, evaluated: subset.filter(r => r.present).length,
    resolved: subset.filter(r => r.resolved).length, strictAccuracy: subset.filter(r => r.resolved).length / subset.length,
    confidence95: wilson(subset.filter(r => r.resolved).length, subset.length),
    baselineDetection: subset.filter(r => r.baselineDetected).length / subset.length,
    validAcceptance: subset.reduce((s, r) => s + r.validAcceptance, 0) / subset.length,
    invalidRejection: subset.reduce((s, r) => s + r.invalidRejection, 0) / subset.length,
    inputTokens: subset.reduce((s, r) => s + r.inputTokens, 0), outputTokens: subset.reduce((s, r) => s + r.outputTokens, 0)});
  return {schemaVersion: 1, manifestDigest: inventory.manifestDigest, resultsDigest: hash(trials),
    ...aggregate(rows), complete: records.size === inventory.expected && rows.every(r => r.schemaOK),
    declaredScoreMismatches: rows.filter(r => r.declaredScoreMismatch).length,
    missingTokenAccounting: trials.filter(t => t.total_input_tokens == null || t.total_output_tokens == null).length,
    patchSetIdentityAvailable: trials.length > 0 && trials.every(t => !!t.patch_set_digest),
    bySource: Object.fromEntries([...new Set(rows.map(r => r.source))].map(source => [source, aggregate(rows.filter(r => r.source === source))])), rows};
}
