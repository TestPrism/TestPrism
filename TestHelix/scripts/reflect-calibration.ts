import {promises as fs} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {z} from 'zod';
import {loadConfig} from '../src/config.js';
import {Budget, type Reservation} from '../src/budget.js';
import {SDKModel} from '../src/model.js';
import {genesis, applyGenome} from '../src/genome.js';
import {reflectionSystem, lessonReflectionTools, reflectedLesson} from '../src/evolution-reflection.js';
import {collectExecutionFeedback} from '../src/evolution-feedback.js';
import {strictScore, trialSchema} from '../src/report.js';
import {atomicJSON, hash, readJSON, requestSize} from '../src/util.js';
import {withRunLock} from '../src/lock.js';
import {configSchema, type Model, type Completion} from '../src/types.js';

// Explicit files only: never traverse benchmark directories, task source or scorer artifacts.
const manifestSchema = z.strictObject({config: z.string(), output: z.string(),
  calibration: z.array(z.strictObject({officialResults: z.string(), agentDirectory: z.string()})).min(1),
});
export function publicFeedback(events: string, run: any) {
  return collectExecutionFeedback(events, run).summary;
}

/** Prepare by default. --execute is an explicit dispatch switch; no promotion or evaluation occurs here.
 * Usage: npx tsx scripts/reflect-calibration.ts manifest.json [--execute]
 * Paths in manifest are relative to the manifest. Reuse the same output for all resumes.
 */
export async function reflectCalibration(manifestFile: string, execute = false, injectedModel?: Model) {
  const manifest = manifestSchema.parse(await readJSON(manifestFile));
  const resolve = (file: string) => path.resolve(path.dirname(manifestFile), file);
  const directory = resolve(manifest.output);
  return withRunLock(directory, async () => {
    const base = await loadConfig(resolve(manifest.config)), parent = genesis(base);
    if (!base.scheduler.enabled || base.scheduler.apiConcurrency > 3) throw new Error('Shared API scheduler must be enabled with concurrency at most 3');
    const reflectionConfig = configSchema.parse({...base,
      model: {...base.model, name: 'gpt-5.5', maxRetries: 0,
        providerOptions: {...base.model.providerOptions, openai: {...base.model.providerOptions.openai, reasoningEffort: 'medium'}}},
      budget: {...base.budget, maxCalls: 2, concurrency: 1}});
    if (reflectionConfig.model.provider !== 'openai-responses') throw new Error('Calibration reflection requires the configured OpenAI Responses transport');
    const scores: ReturnType<typeof strictScore>[] = [], feedback = [], forbidden: string[] = [], sourceDigests = [];
    for (const input of manifest.calibration) {
      const officialText = await fs.readFile(resolve(input.officialResults), 'utf8');
      const official = z.object({results: z.array(trialSchema.extend({
        trial_ended_at: z.string().min(1).refine(value => Number.isFinite(Date.parse(value)), 'Official trial has not ended'),
      })).length(1)}).parse(JSON.parse(officialText));
      const agentDirectory = resolve(input.agentDirectory);
      if (path.basename(agentDirectory) !== 'testhelix-js') throw new Error('Expected explicit public testhelix-js log directory');
      const events = await fs.readFile(path.join(agentDirectory, 'events.jsonl'), 'utf8');
      const runText = await fs.readFile(path.join(agentDirectory, 'result.json'), 'utf8');
      const run = z.object({taskId: z.string().min(1)}).passthrough().parse(JSON.parse(runText));
      for (const trial of official.results) {
        if (trial.task_id !== run.taskId || (trial.task_key !== undefined && trial.task_key !== run.taskId))
          throw new Error('Official result and public agent run task identity mismatch');
        const key = trial.task_key ?? trial.task_id;
        if (forbidden.includes(key)) throw new Error('Duplicate calibration task; use one fixed trial per task');
        forbidden.push(key); scores.push(strictScore(trial));
      }
      feedback.push(publicFeedback(events, run));
      sourceDigests.push({official: hash(officialText), events: hash(events), publicResult: hash(runText)});
    }
    const evidence = {tasks: scores.length, strictSuccesses: scores.filter(score => score.resolved).length,
      baselineDetected: scores.filter(score => score.baselineDetected).length,
      unscorableOrIncompleteOfficialResults: scores.filter(score => !score.schemaOK).length,
      validAcceptance: scores.reduce((sum, score) => sum + score.validAcceptance, 0) / scores.length,
      invalidRejection: scores.reduce((sum, score) => sum + score.invalidRejection, 0) / scores.length,
      patchErrors: scores.reduce((sum, score) => sum + score.patchErrors, 0), trainingExecutionFeedback: feedback};
    const evidenceDigest = hash({evidence, sourceDigests}), tools = lessonReflectionTools();
    const system = reflectionSystem + ' Generate one unpromoted lesson candidate only. This calibration is adaptive development evidence; an independent host will run official paired evaluation. No score or release decision is made here.';
    // No raw trial, task identity, path, logs, assertion, source, patch or reference answer enters this message.
    const messages = [{role: 'user' as const, content: JSON.stringify({parent: {lessons: parent.genes.lessons}, evidence, evidenceDigest})}];
    const request = {system, messages, tools, forceTool: 'submit_lesson', maxOutputTokens: Math.min(2048, base.model.maxOutputTokens)};
    const identity = hash({request, reflectionConfig, parent: parent.id, evidenceDigest});
    const saved = await readJSON<{identity: string}>(path.join(directory, 'request.json'));
    if (saved && saved.identity !== identity) throw new Error('Frozen reflection inputs changed; refusing resume');
    await atomicJSON(path.join(directory, 'request.json'), {identity, model: reflectionConfig.model.name, reasoningEffort: 'medium', request});
    await atomicJSON(path.join(directory, 'evidence.json'), {evidenceDigest, sourceDigests, evidence, purpose: 'calibration-only; not promotion'});
    if (!execute) return {status: 'prepared', directory, evidenceDigest};
    const budget = await Budget.open(reflectionConfig, path.join(directory, 'budget.json'), identity, true);
    for (let attempt = 0; attempt < 2; attempt++) {
      const file = path.join(directory, `attempt-${attempt}.json`);
      let checkpoint = await readJSON<{reservation: Reservation; reply?: Completion; rejected?: boolean}>(file);
      if (!checkpoint) {
        const reservation = await budget.reserve(requestSize({system, messages, tools}).bytes + 64, request.maxOutputTokens);
        checkpoint = {reservation};
        await atomicJSON(file, checkpoint); // Unknown dispatch is never automatically repeated after a crash.
        try {
          checkpoint.reply = await (injectedModel ?? new SDKModel(reflectionConfig)).complete({...request,
            maxOutputTokens: reservation.output,
            signal: AbortSignal.timeout(Math.max(1, Math.min(base.model.timeoutMs, budget.remainingMs)))});
          await atomicJSON(file, checkpoint); // Seal provider response before idempotent budget settlement.
        } catch (error) {
          await budget.settle(reservation);
          await atomicJSON(path.join(directory, 'usage.json'), budget.snapshot());
          throw error;
        }
      }
      if (!checkpoint.reply) throw new Error('An earlier dispatch has uncertain completion; refusing automatic paid retry');
      await budget.settle(checkpoint.reservation, checkpoint.reply.usage.estimated ? undefined : checkpoint.reply.usage);
      await atomicJSON(path.join(directory, 'usage.json'), budget.snapshot());
      let candidate, candidateConfig;
      try {
        const call = checkpoint.reply.calls.find(call => call.name === 'submit_lesson' && !call.invalid);
        candidate = reflectedLesson(parent, call?.input, [evidenceDigest], forbidden);
        candidateConfig = applyGenome(base, candidate);
      } catch (error) {
        await atomicJSON(file, {...checkpoint, rejected: true});
        // Keep raw diagnostic text local; no task-specific rejection details are sent into a retry.
        await atomicJSON(path.join(directory, `rejection-${attempt}.json`), {error: error instanceof Error ? error.message : String(error)});
        if (attempt === 1) throw error;
        continue;
      }
      // Persistence failures propagate immediately; a valid sealed reply never incurs a paid retry.
      await atomicJSON(path.join(directory, 'candidate.json'), {status: 'unpromoted', promoted: false, identity, evidenceDigest, candidate,
        evaluation: 'Requires separate frozen official paired evaluation; calibration is not held-out evidence'});
      await atomicJSON(path.join(directory, 'candidate-config.json'), candidateConfig);
      return {status: 'unpromoted', directory, evidenceDigest, candidate};
    }
    throw new Error('No valid lesson proposal');
  });
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  const args = process.argv.slice(2);
  if (!args[0] || args.some((arg, i) => i > 0 && arg !== '--execute')) throw new Error('Usage: reflect-calibration.ts manifest.json [--execute]');
  console.log(JSON.stringify(await reflectCalibration(path.resolve(args[0]), args.includes('--execute')), null, 2));
}
