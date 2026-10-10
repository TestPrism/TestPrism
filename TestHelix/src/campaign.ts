import { withRunLock } from './lock.js';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { z } from 'zod';
import { loadConfig, loadTask } from './config.js';
import { runTask } from './pipeline.js';
import { atomicJSON, errorText, hash, implementationDigest, readJSON } from './util.js';
import type { Config, RunResult } from './types.js';

const planSchema = z.object({
  configFile: z.string(), taskFiles: z.array(z.string()).min(1),
  concurrency: z.number().int().min(1).max(16).default(1),
  maxTasks: z.number().int().positive(), maxCalls: z.number().int().positive(),
  maxInputTokens: z.number().int().positive(), maxOutputTokens: z.number().int().positive(),
  maxCostUSD: z.number().positive().optional(), wallMs: z.number().int().positive(),
});
interface Job { taskId: string; index: number; config: Config; status: 'running' | RunResult['status'] | 'error';
  usage: {calls: number; input: number; output: number; costUSD: number}; error?: string }
interface Journal { identity: string; startedAt: number; jobs: Job[] }
/** Admissions reserve whole job budgets before dispatch; settlements release unused capacity. */
export async function runCampaign(planFile: string, outputDir: string, resume: boolean) {
  return withRunLock(outputDir, () => runCampaignUnlocked(planFile, outputDir, resume));
}
async function runCampaignUnlocked(planFile: string, outputDir: string, resume: boolean) {
  planFile = path.resolve(planFile); outputDir = path.resolve(outputDir);
  const plan = planSchema.parse(JSON.parse(await fs.readFile(planFile, 'utf8')));
  const resolve = (file: string) => path.resolve(path.dirname(planFile), file);
  const config = await loadConfig(resolve(plan.configFile));
  if (plan.maxCostUSD && !(config.model.inputPricePerMillion && config.model.outputPricePerMillion))
    throw new Error('Campaign dollar cap requires explicit model prices');
  const tasks = await Promise.all(plan.taskFiles.map(file => loadTask(resolve(file))));
  if (new Set(tasks.map(t => t.id)).size !== tasks.length) throw new Error('Duplicate task IDs in campaign');
  const identity = hash({plan, config, tasks, implementation: await implementationDigest()}), journalFile = path.join(outputDir, 'campaign.json');
  const previous = await readJSON<Journal>(journalFile);
  if (previous && !resume) throw new Error('Campaign exists; use --resume or a new output directory');
  if (previous && previous.identity !== identity) throw new Error('Campaign plan/task/config identity changed');
  const journal: Journal = previous ?? {identity, startedAt: Date.now(), jobs: []};
  await atomicJSON(journalFile, journal);
  const usage = () => journal.jobs.reduce((sum, job) => ({calls: sum.calls + job.usage.calls,
    input: sum.input + job.usage.input, output: sum.output + job.usage.output, costUSD: sum.costUSD + job.usage.costUSD}),
    {calls: 0, input: 0, output: 0, costUSD: 0});
  // Serialize snapshots as well as writes: a slow settlement cannot overwrite a later admission.
  let writes = Promise.resolve();
  const persist = () => {
    const snapshot = structuredClone(journal);
    writes = writes.then(() => atomicJSON(journalFile, snapshot));
    return writes;
  };
  const scheduled = Math.min(tasks.length, plan.maxTasks);
  const pending = Array.from({length: scheduled}, (_, index) => index)
    .filter(index => !journal.jobs.some(j => j.index === index && j.status !== 'running'));
  const active = new Map<number, Promise<void>>();
  let stopReason = 'All scheduled tasks finished';
  const execute = async (job: Job) => {
    const jobDir = path.join(outputDir, `task-${String(job.index + 1).padStart(4, '0')}`);
    process.stderr.write(`[${job.index + 1}/${scheduled}] ${job.taskId}\n`);
    try {
      const result = await runTask(tasks[job.index]!, job.config, jobDir, {resume});
      job.status = result.status; job.usage = {...result.usage, costUSD: result.usage.costUSD ?? 0};
    } catch (error) {
      job.status = 'error'; job.error = errorText(error);
      const spent = await readJSON<{calls: number; input: number; output: number}>(path.join(jobDir, 'budget.json')).catch(() => undefined);
      if (spent) job.usage = {calls: spent.calls, input: spent.input, output: spent.output,
        costUSD: (spent.input * job.config.model.inputPricePerMillion + spent.output * job.config.model.outputPricePerMillion) / 1e6};
      // Without a readable ledger retain the full reservation; unknown spend is not zero.
    }
    await persist();
    process.stderr.write(`[${job.index + 1}/${scheduled}] ${job.taskId}: ${job.status}\n`);
  };
  try {
  while (pending.length || active.size) {
    while (pending.length && active.size < plan.concurrency) {
      const elapsed = Date.now() - journal.startedAt;
      if (elapsed >= plan.wallMs) { stopReason = 'Campaign wall budget exhausted'; break; }
      const index = pending[0]!, task = tasks[index]!;
      let job = journal.jobs.find(j => j.index === index);
      if (!job) {
        const used = usage(), remaining = {calls: plan.maxCalls - used.calls, input: plan.maxInputTokens - used.input,
          output: plan.maxOutputTokens - used.output, dollars: plan.maxCostUSD === undefined ? Infinity : plan.maxCostUSD - used.costUSD};
        if (remaining.calls < 1 || remaining.input < 1000 || remaining.output < 128 || remaining.dollars <= 0) {
          stopReason = 'Campaign aggregate budget exhausted'; break;
        }
        const limited = structuredClone(config);
        limited.budget = {...limited.budget, maxCalls: Math.min(limited.budget.maxCalls, remaining.calls),
          maxInputTokens: Math.min(limited.budget.maxInputTokens, remaining.input),
          maxOutputTokens: Math.min(limited.budget.maxOutputTokens, remaining.output),
          wallMs: Math.min(limited.budget.wallMs, plan.wallMs - elapsed)};
        if (Number.isFinite(remaining.dollars)) limited.budget.maxCostUSD = Math.min(limited.budget.maxCostUSD ?? Infinity, remaining.dollars);
        job = {taskId: task.id, index, config: limited, status: 'running', usage: {
          calls: limited.budget.maxCalls, input: limited.budget.maxInputTokens, output: limited.budget.maxOutputTokens,
          costUSD: limited.budget.maxCostUSD ?? (limited.budget.maxInputTokens * limited.model.inputPricePerMillion
            + limited.budget.maxOutputTokens * limited.model.outputPricePerMillion) / 1e6}};
        journal.jobs.push(job);
        await persist(); // No request may precede its durable reservation.
      }
      pending.shift();
      active.set(index, execute(job));
      stopReason = 'All scheduled tasks finished';
    }
    if (!active.size) break;
    const completed = await Promise.race([...active].map(([index, running]) => running.then(() => index)));
    active.delete(completed);
    // A full reservation can temporarily exhaust the aggregate cap. Retry after settlement.
  }
  } finally {
    await Promise.allSettled(active.values());
  }
  await writes;
  const result = {identity, stopReason, concurrency: plan.concurrency, requested: tasks.length, scheduled,
    finished: journal.jobs.filter(j => j.status !== 'running').length,
    usage: {...usage(), costUSD: config.model.inputPricePerMillion && config.model.outputPricePerMillion ? usage().costUSD : null},
    statuses: Object.fromEntries(['validated', 'provisional', 'no_submit', 'error', 'running'].map(status =>
      [status, journal.jobs.filter(j => j.status === status).length])), elapsedMs: Date.now() - journal.startedAt};
  await atomicJSON(path.join(outputDir, 'summary.json'), result); return result;
}
