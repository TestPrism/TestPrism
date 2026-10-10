import path from 'node:path';
import { promises as fs } from 'node:fs';
import { loadConfig } from '../src/config.js';
import { skillAblationProfiles } from '../src/skill-ablation.js';
import { atomicJSON, implementationDigest } from '../src/util.js';

const [configPath, outputPath] = process.argv.slice(2);
if (!configPath || !outputPath) throw new Error('Usage: npx tsx scripts/plan-skill-ablation.ts <config.json> <new-output-directory>');
const output = path.resolve(outputPath);
await fs.mkdir(output, {recursive: false});
const profiles = skillAblationProfiles(await loadConfig(configPath));
for (const profile of profiles) await atomicJSON(path.join(output, profile.id + '.json'), profile.config);
await atomicJSON(path.join(output, 'plan.json'), {kind: 'unexecuted paired development ablation', implementationDigest: await implementationDigest(),
  profiles: profiles.map(({id, configDigest}) => ({id, configDigest, configFile: id + '.json'})),
  orders: [profiles.map(p => p.id), profiles.map(p => p.id).reverse()],
  constraints: ['Same development task IDs, model, budget and repeats across all profiles.',
    'Freeze generated tests before independent evaluation. Never adapt on hardest500.',
    'Setup controls and diagnostic F2P are intermediate signals; promotion requires official valid/invalid patch-set scoring.'],
  modelCalls: 0, successRate: null, eligibleForPromotion: false});
process.stdout.write(output + '\n');
