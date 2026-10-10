import type { Sandbox } from './runtime.js';
import { builtinSkills, skillResources } from './skills.js';
import { hash, shellQuote, publicPath } from './util.js';
import type {ProcedureHooks} from './skill-execution.js';

export interface HarnessInput { framework: 'unittest' | 'django'; python?: string; projectApps?: string[]; modulePaths?: string[] }
/** Initial host template facts, not evidence that the current task suite works. */
export function harnessUsageCard(framework: HarnessInput['framework']): string {
  const common = 'Prepared harness (initial template; recheck edited files): TestCase subclasses with test_* methods in agent-tests/cases/test_*.py; run bash agent-tests/run-tests.sh. ';
  return common + (framework === 'django'
    ? 'Django DB cases: django.test.TestCase. Fixtures: agent-tests/evo_harness_app/models.py; import evo_harness_app.models. Initially HarnessParent.value is an integer; HarnessChild has parent (FK), no value field. '
    : 'unittest mode does not configure Django apps or database tables. ORM cases need Django setup. ')
    + 'Setup control is not task coverage.';
}
/** Fixed host resources only. Never uses a task directory as a template source. */
export async function prepareTestHarness(sandbox: Sandbox, input: HarnessInput, timeoutMs: number, signal: AbortSignal, hooks?: ProcedureHooks) {
  await hooks?.stage('probe');
  const projectApps = [...new Set(input.projectApps ?? [])], modulePaths = [...new Set(input.modulePaths ?? [])].map(publicPath);
  if (projectApps.length > 12 || projectApps.some(app => !/^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$/.test(app)) || modulePaths.length > 8)
    throw new Error('Invalid harness app/module configuration');
  if (input.framework !== 'django' && projectApps.length) throw new Error('projectApps requires Django mode');
  for (const directory of modulePaths) {
    const check = await sandbox.exec(`test -d ${shellQuote(directory)} && test ! -L ${shellQuote(directory)}`, Math.min(timeoutMs, 5000), signal);
    if (check.code !== 0 || check.timedOut) throw new Error(`Harness module path is not a public directory: ${directory}`);
    await sandbox.list(directory); // Containment, including symlinked ancestors.
  }
  const python = input.python ?? 'python3';
  const probeCode = `import sys, json\nassert sys.version_info >= (3, 6), 'Harness requires Python 3.6+'\ninfo = {'python': sys.version.split()[0], 'executable': sys.executable}\n${input.framework === 'django' ? "import django\ninfo['django'] = django.get_version()\ninfo['djangoPath'] = django.__file__\n" : ''}print('TESTHELIX_RUNTIME=' + json.dumps(info))`;
  const probe = await sandbox.exec(`${shellQuote(python)} -c ${shellQuote(probeCode)}`, Math.min(timeoutMs, 15000), signal);
  const match = /^TESTHELIX_RUNTIME=(.+)$/m.exec(probe.stdout);
  if (probe.code !== 0 || probe.timedOut || !match) return {ok: false, stage: 'probe', probe};
  const runtime = JSON.parse(match[1]!) as Record<string, string>;
  const files = skillResources('framework-tests').filter(file => file.path.startsWith('assets/')
    && (input.framework === 'django' || !file.path.startsWith('assets/evo_harness_app/')))
    .map(file => ({path: 'agent-tests/' + file.path.slice('assets/'.length), data: file.data, mode: 0o644}));
  files.push({path: 'agent-tests/_evo_harness/runtime.py', data: Buffer.from(`MODE = ${JSON.stringify(input.framework)}\nPROJECT_APPS = ${JSON.stringify(projectApps)}\nMODULE_PATHS = ${JSON.stringify(modulePaths)}\n`), mode: 0o644},
    {path: 'agent-tests/run-tests.sh', data: Buffer.from(`#!/bin/sh\nset -eu\ncd -- "$(dirname -- "$0")/.."\nexport PYTHONDONTWRITEBYTECODE=1\nexec ${shellQuote(python)} agent-tests/_evo_harness/run.py "$@"\n`), mode: 0o755});
  const manifest = files.map(file => ({path: file.path, digest: hash(file.data), mode: file.mode}));
  const mayResume = !!hooks?.resumeFiles && hash(hooks.resumeFiles) === hash(manifest);
  await hooks?.stage('preflight');
  const ancestors = ['agent-tests', 'agent-tests/_evo_harness', 'agent-tests/evo_harness_app', 'agent-tests/cases'];
  const links = await sandbox.exec(`for target in ${[...ancestors, ...files.map(file => file.path)].map(shellQuote).join(' ')}; do\nif [ -L "$target" ]; then printf '%s\\n' "$target"; exit 43; fi\ndone`, Math.min(timeoutMs, 10000), signal);
  if (links.code !== 0 || links.timedOut) return {ok: false, stage: 'conflict', runtime,
    guidance: 'Harness destinations must not contain symlinks. Existing paths were preserved.', check: links};
  // Check every destination before mutating. An existing runner belongs to the
  // model/project; return an actionable conflict rather than quietly replacing it.
  const conflicts: string[] = [], reused = new Set<string>();
  for (const file of files) {
    try {
      const mode = await sandbox.mode(file.path);
      if (mayResume && mode === file.mode && hash(await sandbox.read(file.path)) === hash(file.data)) reused.add(file.path);
      else conflicts.push(file.path);
    }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error; }
  }
  if (conflicts.length) return {ok: false, stage: 'conflict', runtime, conflicts,
    guidance: 'Existing or edited files were preserved. Inspect the current runner/settings and fix the specific failure; do not rebuild or overwrite working fixtures.'};
  // Persist ownership before the first write, after rejecting pre-existing
  // unowned files. An interrupted install may resume only matching byte/modes.
  await hooks?.stage('install', manifest);
  const written: string[] = [];
  try {
    for (const file of files) {
      signal.throwIfAborted(); if (reused.has(file.path)) continue;
      written.push(file.path); await sandbox.write(file.path, file.data, file.mode);
    }
  } catch (error) {
    for (const file of written.reverse()) await sandbox.remove(file);
    throw error;
  }
  await hooks?.stage('control');
  const control = await sandbox.exec('bash agent-tests/run-tests.sh --control', Math.min(timeoutMs, 30000), signal);
  const passed = control.code === 0 && !control.timedOut && control.stdout.includes('TESTHELIX_HARNESS_CONTROL=');
  return {ok: passed, stage: 'control', framework: input.framework, runtime, projectApps, modulePaths, controlPassed: passed, control,
    reusedFiles: reused.size, verification: 'fresh-setup-control',
    usage: {
      card: harnessUsageCard(input.framework),
      casePath: 'agent-tests/cases/test_*.py',
      discovery: 'unittest discovery: define a TestCase subclass with test_* methods; plain functions are not collected',
      baseClass: input.framework === 'django' ? 'from django.test import TestCase (database managed by runner)' : 'from unittest import TestCase',
      imports: 'Project root and agent-tests are already on sys.path; import the public project directly',
      ...(input.framework === 'django' ? {fixtures: 'Define task-derived models in agent-tests/evo_harness_app/models.py and import from evo_harness_app.models; the app is registered and SQLite schema/cleanup are automatic'} : {}),
      run: 'bash agent-tests/run-tests.sh',
      controlOnly: 'bash agent-tests/run-tests.sh --control only checks framework setup, not task coverage',
    },
    skillDigest: builtinSkills['framework-tests'].digest,
    files: manifest,
    guidance: 'Control checks setup only. Add task-derived cases/test_*.py, then run bash agent-tests/run-tests.sh. The normal runner fails when there are no task tests. Django fixtures must be registered in evo_harness_app/models.py; adapt settings only to public requirements.'};
}
