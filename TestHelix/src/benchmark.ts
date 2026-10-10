import { promises as fs } from 'node:fs';
import path from 'node:path';
import { parse } from 'yaml';
import { z } from 'zod';
import { command, hash, mapLimit, relativePath } from './util.js';

const manifestSchema = z.object({total_selected: z.number().int().positive(), sources: z.record(z.string(),
  z.object({task_ids: z.array(z.string()), selected_count: z.number().int().optional()}))});
export interface TaskEntry {
  key: string; source: string; id: string; directory: string | null;
  location: 'selected' | 'flat-fallback' | 'missing'; instructionDigest?: string;
  workdir?: string; hasDockerfile: boolean; hasCompose: boolean; issues: string[];
}
export interface Inventory {
  schemaVersion: 1; benchmarkRoot: string; manifestPath: string; manifestDigest: string;
  expected: number; available: number; selectedLayout: number; flatFallback: number; missing: number;
  officialCLIAvailable: boolean; harnessAvailable: boolean; unavailableAgentImports: string[]; issues: string[]; tasks: TaskEntry[];
}
export async function exists(file: string): Promise<boolean> { return fs.access(file).then(() => true, () => false); }
export async function unavailableAgentImports(benchmarkRoot: string): Promise<string[]> {
  const factory = await fs.readFile(path.join(benchmarkRoot, 'testprism/agents/agent_factory.py'), 'utf8')
    .catch(error => { if (error.code === 'ENOENT') return ''; throw error; });
  if (!factory) return ['testprism.agents.agent_factory'];
  // The supplied factory uses unconditional top-level from-imports. Report all
  // missing local modules without executing benchmark code or importing agents.
  const modules = [...new Set([...factory.matchAll(/^from\s+(testprism\.[\w.]+)\s+import\b/gm)].map(match => match[1]!))];
  const availability = await Promise.all(modules.map(async name => ({name,
    available: await exists(path.join(benchmarkRoot, name.replaceAll('.', '/') + '.py'))
      || await exists(path.join(benchmarkRoot, name.replaceAll('.', '/'), '__init__.py'))})));
  return availability.filter(module => !module.available).map(module => module.name).sort();
}
export async function inventory(benchmarkRoot: string, manifestPath?: string): Promise<Inventory> {
  benchmarkRoot = path.resolve(benchmarkRoot);
  manifestPath = path.resolve(manifestPath ?? path.join(benchmarkRoot, 'verified_tasks/test-hardest-500/manifest.json'));
  const raw = await fs.readFile(manifestPath, 'utf8'), manifest = manifestSchema.parse(JSON.parse(raw));
  const keys = new Set<string>();
  const requested = Object.entries(manifest.sources).flatMap(([source, data]) => {
    relativePath(source);
    if (data.selected_count !== undefined && data.selected_count !== data.task_ids.length) throw new Error(`Invalid manifest count: ${source}`);
    return data.task_ids.map(id => {
      relativePath(id); const key = `${source}/${id}`;
      if (keys.has(key)) throw new Error(`Duplicate manifest task: ${key}`);
      keys.add(key); return {source, id, key};
    });
  });
  if (requested.length !== manifest.total_selected) throw new Error('Manifest total does not equal unique task count');
  const tasks = await mapLimit(requested, 16, async entry => {
    const selected = path.join(path.dirname(manifestPath!), entry.source, entry.id);
    const flat = path.join(benchmarkRoot, 'verified_tasks_flat_test', `${entry.source}__${entry.id}`);
    const location = await exists(path.join(selected, 'task.yaml')) ? 'selected'
      : await exists(path.join(flat, 'task.yaml')) ? 'flat-fallback' : 'missing';
    const directory = location === 'selected' ? selected : location === 'flat-fallback' ? flat : null;
    const item: TaskEntry = {...entry, location, directory, hasDockerfile: false, hasCompose: false, issues: []};
    if (!directory) { item.issues.push('Missing public task.yaml'); return item; }
    try {
      const task = parse(await fs.readFile(path.join(directory, 'task.yaml'), 'utf8'));
      if (typeof task.instruction !== 'string' || !task.instruction.trim()) throw new Error('Missing public instruction');
      item.instructionDigest = hash(task.instruction); item.workdir = typeof task.workdir === 'string' ? task.workdir : undefined;
      item.hasDockerfile = await exists(path.join(directory, 'Dockerfile'));
      item.hasCompose = await exists(path.join(directory, 'docker-compose.yaml'));
      if (!item.hasDockerfile && !item.hasCompose) item.issues.push('No Dockerfile or docker-compose.yaml');
    } catch (error) { item.issues.push(String(error)); }
    return item;
  });
  const officialCLIAvailable = await exists(path.join(benchmarkRoot, 'testprism/cli/tb/main.py'));
  const harnessAvailable = await exists(path.join(benchmarkRoot, 'testprism/harness/testbench_harness.py'));
  const missing = tasks.filter(item => item.location === 'missing').length;
  const flatFallback = tasks.filter(item => item.location === 'flat-fallback').length;
  const issues: string[] = [];
  if (missing) issues.push(`${missing} selected public tasks are missing in both layouts`);
  if (flatFallback) issues.push(`${flatFallback} tasks use the flat fallback; selected-layout instruction equivalence has not been established`);
  if (!officialCLIAvailable) issues.push('Official CLI entry point testprism/cli/tb/main.py is missing');
  if (!harnessAvailable) issues.push('Official TestBench harness is missing');
  const missingImports = await unavailableAgentImports(benchmarkRoot);
  if (missingImports.length) issues.push(`AgentFactory has ${missingImports.length} unavailable local imports; a complete benchmark checkout is required`);
  return {schemaVersion: 1, benchmarkRoot, manifestPath, manifestDigest: hash(raw), expected: requested.length,
    available: requested.length - missing, selectedLayout: requested.length - missing - flatFallback,
    flatFallback, missing, officialCLIAvailable, harnessAvailable, unavailableAgentImports: missingImports, issues, tasks};
}
export async function doctor(benchmarkRoot?: string) {
  const docker = await command(['docker', 'version', '--format', '{{.Server.Version}}'], {timeoutMs: 10000}).catch(error =>
    ({code: 1, stdout: '', stderr: String(error), timedOut: false}));
  const data = benchmarkRoot ? await inventory(benchmarkRoot) : undefined;
  return {node: process.version, nodeSupported: Number(process.versions.node.split('.')[0]) >= 22,
    docker: docker.code === 0 ? docker.stdout.trim() : null, dockerError: docker.code === 0 ? undefined : docker.stderr,
    ...(data ? {benchmark: {...data, tasks: undefined}} : {})};
}
