import { z } from 'zod';
import { applyPatch } from 'diff';
import picomatch from 'picomatch';
import { parseAgentPatch, applyUniqueEdits } from './patch.js';
import type { Sandbox } from './runtime.js';
import type { Store } from './store.js';
import type { ToolSpec } from './types.js';
import { WorkingMemory } from './memory.js';
import { clip, hash, publicPath, shellQuote, testPath } from './util.js';
import { builtinSkills, skillIds, type SkillId } from './skills.js';
import { prepareTestHarness, type HarnessInput } from './harness.js';
import { reviewSchema, reviewWireSchema } from './review.js';
import {writeTestSuite, type SuiteInput} from './test-suite.js';
import {classify} from './validation.js';
import {PiWorkspaceTools} from './pi-tools.js';
import {readSkillReference, skillReferences} from './skills.js';
import {SkillExecution, proceduresForSkill, type ProcedureHooks} from './skill-execution.js';
import {applyV4APatch} from './v4a-patch.js';
import {outputContractSchema, outputContractWireSchema} from './task-contract.js';

const text = z.string();
const filePath = text.min(1).max(1024);
const positive = z.number().int().positive();
const offset = z.number().int().nonnegative();
const editInput = z.strictObject({path: filePath, old: text.min(1).max(1_000_000),
  replacement: text.max(1_000_000), expectedHash: text.regex(/^[a-f0-9]{64}$/).optional()});
const schema = (description: string, input: z.ZodType, wire = input): ToolSpec => {
  const {$schema: _, ...json} = z.toJSONSchema(wire, {target: 'draft-7'});
  return {description, schema: json, parse: value => input.parse(value)};
};
const webQuery = z.strictObject({query: text.min(2).max(500), limit: z.number().int().min(1).max(8).optional()});
const githubQuery = z.strictObject({query: text.min(2).max(500), kind: z.enum(['code', 'repositories', 'issues']).default('code'), limit: z.number().int().min(1).max(10).optional()});
const submitInput = z.strictObject({
    sourcePaths: z.array(filePath).max(40).optional().describe('Deprecated and ignored; omit.'),
    deletedSourcePaths: z.array(filePath).max(40).optional().describe('Deprecated and ignored; omit.'),
    replayScript: filePath.optional().describe('Captured internal Bash repair script for command/state changes or public-source edits outside the workspace. Save it inside the workspace, outside agent-tests/. Host replays it fresh BEFORE installing frozen tests; tests only observe results.'),
    requirements: z.array(text.min(1).max(500)).min(1).max(20), rationale: text.max(2000), contract: outputContractSchema.optional()});
export const workspaceTools: Record<string, ToolSpec> = {
  prepare_test_harness: schema('Prepare a portable Python 3.6+ unittest or isolated Django harness from fixed generic resources and execute a setup control. Inspect the existing project runner first. Preserves existing files; no task assertions or downloads. Add cases/test_*.py afterwards.', z.strictObject({
    framework: z.enum(['unittest', 'django']), python: text.min(1).max(500).regex(/^[A-Za-z0-9_./-]+$/).optional(),
    projectApps: z.array(text.regex(/^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$/)).max(12).optional(),
    modulePaths: z.array(filePath).max(8).optional()})),
  load_skill: schema('Load a curated skill by ID, or a listed references/*.md resource on demand. Guidance is not evidence. Only fixed host-owned packages are readable.', z.strictObject({id: z.enum(skillIds), resource: text.max(200).optional()})),
  list_files: schema('List public files with pagination. pattern accepts a literal substring or a glob (*, **, ?, braces). Slashless globs match basenames; other globs match workspace paths or paths relative to directory. Narrow directory for large repositories.', z.strictObject({
    directory: filePath.optional(), pattern: text.max(200).optional(), offset: offset.optional(), limit: positive.max(350).optional()})),
  read_file: schema('Read numbered lines. Follow nextStartLine/nextStartColumn when truncated. refresh bypasses the version-checked cache.', z.strictObject({
    path: filePath, startLine: positive.optional(), endLine: positive.optional(), startColumn: offset.optional(), refresh: z.boolean().optional()})),
  search: schema('Search literal public code; use directory and offset to narrow/page matches. No matches is a successful result.', z.strictObject({
    query: text.min(1).max(2000), directory: filePath.optional(), offset: offset.optional()})),
  web_search: schema('Search public web documentation from the host network. Docker task containers remain offline; results are bounded and may be unavailable offline.', webQuery),
  github_search: schema('Search public GitHub code and repositories through the GitHub API. Optional GITHUB_TOKEN raises rate limits; token is never returned. Docker task containers remain offline.', githubQuery),
  network_status: schema('Report sandbox network isolation and host search availability without accessing the task container network.', z.strictObject({})),
  write_test_suite: schema('Write a complete group of files under agent-tests/ and immediately run bash agent-tests/run-tests.sh. Include the runner if missing. Validates all destinations, rolls back failed writes, preserves files on test failure. Supply your own task assertions; no source edits.', z.strictObject({
    files: z.array(z.strictObject({path: filePath, content: text.max(1_000_000)})).min(1).max(40),
    timeoutMs: positive.min(100).max(300000).optional()})),
  write_file: schema('Write complete UTF-8 file contents. Prefer edit_file or apply_patch for small changes to existing files.', z.strictObject({
    path: filePath, content: text.max(1_000_000)})),
  // Pi checks the current file for one unique match. Avoid asking the model to
  // transcribe opaque hashes; explicit legacy preconditions remain enforced.
  edit_file: {...schema('Pi edit: replace one unique match in the current file, preserving BOM and line endings. Uses exact matching first, with Pi whitespace normalization fallback.',
    editInput.omit({expectedHash: true})), parse: value => editInput.parse(value)},
  apply_patch: schema('Apply a standard unified diff (--- a/path, +++ b/path, @@ -line,count +line,count @@). Supports add/delete via /dev/null. Counts are recounted. Stale context may be recovered only when the exact removed lines identify a unique contiguous replacement. No whitespace fuzzing. All files are checked before writing. For new files, write_file is simpler.', z.strictObject({patch: text.min(1).max(1_000_000)})),
  exec: schema('Run a bounded Bash command in the isolated public task workspace. Batch related checks; avoid hidden benchmark data. Killed on timeout.', z.strictObject({
    command: text.min(1).max(64000), timeoutMs: positive.min(100).max(300000).optional()})),
  read_artifact: schema('Read this session\'s saved result/argument or host-granted execution evidence by artifact ID, with character pagination.', z.strictObject({
    id: text.regex(/^[a-f0-9]{64}$/), offset: offset.optional()})),
  view_image: schema('Inspect a public PNG/JPEG/WebP task image when visual evidence matters.', z.strictObject({path: filePath})),
  request_replan: schema('Request a budgeted planning revision only when your own execution evidence disproves a key plan premise. Continue preserving runnable artifacts; this does not extend the task budget.',
    z.strictObject({reason: text.min(1).max(600), artifact: text.regex(/^[a-f0-9]{64}$/)})),
  terminal: schema('Use a persistent native task terminal for interactive or long-running programs. Start a named session, send a line, read its output, or interrupt it. Read output before sending the next dependent input.',
    z.strictObject({action: z.enum(['start', 'send', 'read', 'interrupt']), name: text.regex(/^[a-zA-Z0-9_-]{1,32}$/), text: text.max(16000).default('')})),
  update_notes: schema('Save bounded working memory. Prefer entries with stable IDs, observed paths or artifact evidence. File changes mark facts stale; do not copy logs or code.', z.strictObject({
    notes: text.max(3000).optional(),
    entries: z.array(z.strictObject({id: text.min(1).max(60), kind: z.enum(['fact', 'decision', 'next', 'requirement']), text: text.min(1).max(600),
      paths: z.array(filePath).max(6).optional(), artifacts: z.array(text.regex(/^[a-f0-9]{64}$/)).max(3).optional()})).max(16).optional(),
    remove: z.array(text.min(1).max(60)).max(16).optional(),
  }).refine(value => value.notes !== undefined || value.entries !== undefined || value.remove !== undefined, 'Supply notes or entries')),
  submit_candidate: schema('Export agent-tests/ and capture public source changes for fresh internal replay. Provide requirements, rationale and a structured contract: path, format, schema, cardinality or note clauses. Inputs refer to immutable public baseline files; outputs to captured tests. Runtime channels and notes remain unverified. Unknown kinds/schema keywords are rejected; use note for unsupported semantics. Missing contract uses host defaults; declarations cannot change host rules.',
    submitInput, submitInput.extend({contract: outputContractWireSchema.optional()})),
};
export const reviewTool = {...schema('Submit source-blind findings with severity and evidence. Cite files by path + startLine/endLine and the numbered original task by taskLines. The host extracts exact text; do not transcribe quotes. Each finding needs a verified excerpt. Blocking unsupported-assertion findings need falseRejection with a concrete compliant behavior, control assessment and public basis; task omission alone is insufficient. Missing evidence requests clarification within the same budget. Optional improvements are suggestions. Host derives acceptance.', reviewWireSchema),
  parse: (value: unknown) => reviewSchema.parse(value)};
export const readTools = new Set(['list_files', 'read_file', 'search', 'web_search', 'github_search', 'network_status', 'read_artifact', 'view_image', 'load_skill']);
export function validateTool(spec: ToolSpec, raw: unknown): unknown { return spec.parse ? spec.parse(raw) : raw; }

/** Budget the JSON encoding, including escaped quotes/newlines, rather than raw text. */
function jsonPrefix(value: string, capacity: number): string {
  let low = 0, high = Math.min(value.length, Math.max(0, capacity));
  while (low < high) {
    const middle = Math.ceil((low + high) / 2);
    if (JSON.stringify(value.slice(0, middle)).length - 2 <= capacity) low = middle;
    else high = middle - 1;
  }
  if (low < value.length && /[\uD800-\uDBFF]/.test(value[low - 1] ?? '') && /[\uDC00-\uDFFF]/.test(value[low] ?? '')) low--;
  return value.slice(0, low);
}
function textPage(text: string, start: number, field: string, metadata: Record<string, unknown>, limit: number): Record<string, unknown> {
  const capacity = limit - JSON.stringify({...metadata, [field]: '', nextOffset: text.length}).length;
  if (capacity < 8) throw new Error('Tool output limit is too small for pagination metadata');
  const chunk = jsonPrefix(text.slice(start), capacity), end = start + chunk.length;
  return {...metadata, [field]: chunk, ...(end < text.length ? {nextOffset: end} : {})};
}
async function hostJson(url: string, headers: Record<string, string>, timeoutMs = 8000): Promise<any> {
  const response = await fetch(url, {headers, signal: AbortSignal.timeout(timeoutMs)});
  const body = await response.text();
  if (body.length > 2_000_000) throw new Error('Host search response exceeds 2 MB');
  if (!response.ok) throw new Error(`Host search HTTP ${response.status}: ${clip(body, 400)}`);
  return JSON.parse(body);
}

export class Toolbox {
  private readonly pi: PiWorkspaceTools;
  readonly procedures: SkillExecution;
  workspaceRevision = 0;
  readonly memory = new WorkingMemory();
  readonly revisions = new Map<string, string>();
  private cache = new Map<string, {version: string; data: Buffer}>();
  get notes(): string { return this.memory.render(); }
  constructor(readonly sandbox: Sandbox, readonly store: Store, readonly limit: number,
    readonly timeoutMs: number, readonly readOnly = false, readonly skills: readonly SkillId[] = [], readonly skillResourcesEnabled = true, readonly maxArtifactBytes = 4_000_000,
    readonly networkMode: 'none' | 'inherit' = 'none') {
      this.pi = new PiWorkspaceTools(sandbox);
      this.procedures = new SkillExecution(sandbox, store, skills, skillResourcesEnabled && !readOnly);
    }
  private workspacePath(value: string, directory = false): string {
    const root = this.sandbox.root.replace(/\/$/, '');
    const relative = value.startsWith(root + '/') ? value.slice(root.length + 1)
      : value === root ? '.' : value;
    // Retain publicPath's traversal/private-file checks and Sandbox's symlink
    // containment. Only strip the exact current workspace prefix.
    return directory && (relative === '.' || relative === '') ? '.' : publicPath(relative);
  }
  private invalidate(file?: string): void {
    this.workspaceRevision++;
    this.procedures.invalidate();
    this.cache.clear(); this.memory.invalidate(file);
    if (file) this.revisions.delete(file); else this.revisions.clear();
  }
  private async read(file: string, refresh = false): Promise<Buffer> {
    const cached = this.cache.get(file);
    let version = cached && !refresh ? await this.sandbox.fingerprint?.(file) : undefined;
    if (!refresh && version && cached?.version === version) return cached.data;
    let data: Buffer;
    if (this.sandbox.readVersioned) ({data, version} = await this.sandbox.readVersioned(file));
    else {
      version = await this.sandbox.fingerprint?.(file);
      data = await this.sandbox.read(file);
      if (version !== await this.sandbox.fingerprint?.(file)) version = undefined;
    }
    if (data.length > 8_000_000) throw new Error('File exceeds 8 MB read limit; use a bounded project command');
    const revision = hash(data);
    if (this.revisions.has(file) && this.revisions.get(file) !== revision) this.memory.invalidate(file);
    this.revisions.set(file, revision);
    if (version) {
      this.cache.delete(file); this.cache.set(file, {version, data});
      while ([...this.cache.values()].reduce((sum, entry) => sum + entry.data.length, 0) > 8_000_000 || this.cache.size > 32)
        this.cache.delete(this.cache.keys().next().value!);
    }
    return data;
  }
  async execute(name: string, raw: unknown, signal: AbortSignal, spec = workspaceTools[name]): Promise<Record<string, unknown>> {
    signal.throwIfAborted();
    if (!Object.hasOwn(workspaceTools, name)) throw new Error(`Unknown tool: ${name}`);
    const args = validateTool(spec!, raw) as Record<string, any>;
    if (this.readOnly && !readTools.has(name) && name !== 'update_notes') throw new Error('Review tools are read-only');
    if (typeof args.path === 'string') args.path = this.workspacePath(args.path);
    if (typeof args.directory === 'string') args.directory = this.workspacePath(args.directory, true);
    if (name === 'write_test_suite') args.files = args.files.map((file: {path: string; content: string}) =>
      ({...file, path: this.workspacePath(file.path)}));
    if (name === 'prepare_test_harness') Object.assign(args, {python: args.python ?? 'python3',
      projectApps: [...new Set(args.projectApps ?? [])], modulePaths: [...new Set((args.modulePaths ?? []).map((file: string) => this.workspacePath(file)))]});
    return this.procedures.run(name, args, signal, hooks => this.executeWorkspace(name, args, signal, hooks));
  }
  private async executeWorkspace(name: string, args: Record<string, any>, signal: AbortSignal, hooks?: ProcedureHooks): Promise<Record<string, unknown>> {
    const file = () => publicPath(args.path);
    switch (name) {
      case 'prepare_test_harness': {
        if (!this.skills.includes('framework-tests') || !this.skillResourcesEnabled) throw new Error('Framework resources are not enabled for this session');
        this.invalidate();
        const result = await prepareTestHarness(this.sandbox, args as unknown as HarnessInput, this.timeoutMs, signal, hooks);
        await this.store.event('skills.harness', {workspace: this.sandbox.id, ...result});
        return result;
      }
      case 'load_skill': {
        const id = args.id as SkillId;
        if (!this.skills.includes(id)) throw new Error('Skill is not enabled for this session');
        if (args.resource && !this.skillResourcesEnabled) throw new Error('Skill resources are disabled');
        const skill = builtinSkills[id];
        const body = args.resource ? readSkillReference(id, args.resource) : skill.text;
        await this.store.event('skills.loaded', {workspace: this.sandbox.id, id, resource: args.resource, digest: skill.digest});
        return {ok: true, id, resource: args.resource, digest: skill.digest, text: body,
          references: this.skillResourcesEnabled ? skillReferences(id) : [],
          procedures: this.skillResourcesEnabled ? proceduresForSkill(id).map(({id, tool, purpose}) => ({id, tool, purpose})) : [], provenance: 'curated-generic-method'};
      }
      case 'list_files': {
        const directory = args.directory && args.directory !== '.' ? publicPath(args.directory).replace(/\/$/, '') : '';
        let files = await this.sandbox.list(directory);
        if (args.pattern) {
          const pattern = args.pattern.replace(/^\.\//, '');
          if (picomatch.scan(pattern, {nonegate: true}).isGlob) {
            // Match only the already contained public listing. Glob syntax never
            // becomes a filesystem traversal or shell command.
            const matches = picomatch(pattern, {dot: true, nonegate: true, basename: !pattern.includes('/')});
            files = files.filter(file => matches(file) || (directory && matches(file.slice(directory.length + 1))));
          } else files = files.filter(file => file.includes(pattern));
        }
        const start = args.offset ?? 0;
        let end = Math.min(files.length, start + (args.limit ?? 200));
        const page = () => ({ok: true, count: files.length, files: files.slice(start, end), ...(end < files.length ? {nextOffset: end} : {})});
        while (end > start && JSON.stringify(page()).length > this.limit) end--;
        if (end === start && start < files.length) throw new Error('A file path exceeds the output limit; increase toolOutputChars');
        return page();
      }
      case 'read_file': {
        const data = await this.read(file(), args.refresh), contents = data.toString('utf8');
        if (contents.includes('\0')) throw new Error('Binary file; use view_image for images');
        const lines = contents.split('\n'), start = args.startLine ?? 1;
        let end = Math.min(lines.length, args.endLine ?? start + 199, start + 399);
        if (start > lines.length || end < start) throw new Error(`Requested lines out of range (file has ${lines.length} lines)`);
        const selected = await this.pi.readLines(file(), start, end - start + 1, async () => data, signal);
        if (selected) end = Math.min(end, start + selected.length - 1);
        const column = args.startColumn ?? 0;
        if (column > lines[start - 1]!.length) throw new Error('startColumn exceeds line length');
        const metadata = {ok: true, path: file(), hash: this.revisions.get(file()), startLine: start, totalLines: lines.length};
        const capacity = this.limit - JSON.stringify({...metadata, content: '', nextStartLine: lines.length, nextStartColumn: contents.length}).length;
        let used = 0;
        let content = '', nextStartLine: number | undefined, nextStartColumn: number | undefined;
        for (let line = start; line <= end; line++) {
          const from = line === start ? column : 0, prefix = `${line}${from ? `:${from}` : ''}: `;
          const available = capacity - used - (JSON.stringify(prefix + '\n').length - 2);
          if (available < 8) {
            if (!content) throw new Error('Tool output limit is too small for file pagination metadata');
            nextStartLine = line; nextStartColumn = from || undefined; break;
          }
          const remaining = (selected?.[line - start] ?? lines[line - 1]!).slice(from);
          const chunk = jsonPrefix(remaining, available), rendered = prefix + chunk + '\n';
          content += rendered; used += JSON.stringify(rendered).length - 2;
          if (remaining.length > chunk.length) { nextStartLine = line; nextStartColumn = from + chunk.length; break; }
          nextStartLine = line < lines.length ? line + 1 : undefined;
        }
        return {...metadata, content, ...(nextStartLine ? {nextStartLine, ...(nextStartColumn ? {nextStartColumn} : {})} : {})};
      }
      case 'search': {
        const directory = args.directory && args.directory !== '.' ? publicPath(args.directory) : '.';
        const excluded = ['.git', 'node_modules', '.venv', '__pycache__', '.patch-generation-state', 'patch-sets', '.cache'];
        const flags = excluded.map(x => `--exclude-dir=${shellQuote(x)}`).join(' ');
        const globs = [...excluded, 'solution.sh'].map(x => `-g ${shellQuote('!' + x)}`).join(' ');
        // grep -r does not follow nested symlinks; -R would escape the public tree.
        if (directory !== '.') await this.sandbox.list(directory); // Validate directory containment before shell traversal.
        const search = `if command -v rg >/dev/null 2>&1; then rg -n -F --hidden ${globs} -- ${shellQuote(args.query)} ${shellQuote(directory)}; else grep -r -n -I -F ${flags} --exclude=solution.sh -- ${shellQuote(args.query)} ${shellQuote(directory)}; fi`;
        const result = await this.sandbox.exec(search, Math.min(this.timeoutMs, 20000), signal);
        const start = args.offset ?? 0, matches = result.stdout;
        const artifact = await this.store.artifact(matches);
        return textPage(matches, start, 'matches', {ok: !result.timedOut && [0, 1].includes(result.code!), artifact,
          ...(result.truncated ? {truncated: true, hint: 'Narrow directory/query; the process output limit was reached'} : {}),
          ...(result.stderr ? {error: clip(result.stderr, Math.floor(this.limit / 4))} : {})}, this.limit);
      }
      case 'network_status': {
        const probe = await this.sandbox.exec("if command -v curl >/dev/null 2>&1; then curl -fsS --max-time 3 -o /dev/null -w '%{http_code}' https://api.github.com; elif command -v wget >/dev/null 2>&1; then wget -q --timeout=3 --spider https://api.github.com && printf 200 || printf 000; else printf unavailable; fi", 5000, signal);
        return {ok: true, sandboxNetwork: this.networkMode, sandboxReachable: probe.code === 0 && /^(2|3)/.test(probe.stdout), probe: clip(probe.stdout, 32), hostSearch: 'enabled', githubToken: Boolean(process.env.GITHUB_TOKEN)};
      }
      case 'web_search': {
        const q = encodeURIComponent(args.query as string), limit = args.limit ?? 5;
        try {
          const response = await fetch(`https://html.duckduckgo.com/html/?q=${q}`, {headers: {'User-Agent': 'TestHelix/0.2'}, signal: AbortSignal.timeout(8000)});
          const body = await response.text();
          if (!response.ok) throw new Error(`Host search HTTP ${response.status}`);
          const results = [...body.matchAll(/result__a[^>]*href="([^"]+)"[^>]*>(.*?)<\/a>/g)].slice(0, limit).map(match => ({url: match[1], title: (match[2] ?? '').replace(/<[^>]+>/g, '')}));
          return {ok: true, query: args.query, results};
        } catch (error) { return {ok: false, query: args.query, offline: true, error: error instanceof Error ? error.message : String(error)}; }
      }
      case 'github_search': {
        const q = encodeURIComponent(args.query as string), limit = args.limit ?? 5;
        try {
          const headers: Record<string, string> = {'Accept': 'application/vnd.github+json', 'User-Agent': 'TestHelix/0.2'};
          if (process.env.GITHUB_TOKEN) headers.Authorization = `Bearer ${process.env.GITHUB_TOKEN}`;
          const kind = args.kind ?? 'code';
          const data = await hostJson(`https://api.github.com/search/${kind}?q=${q}&per_page=${limit}`, headers);
          const items = Array.isArray(data.items) ? data.items.slice(0, limit).map((item: any) => ({name: item.name, path: item.path, title: item.title, url: item.html_url, repository: item.repository?.full_name ?? item.full_name, description: item.description})) : [];
          return {ok: true, query: args.query, total: data.total_count, items};
        } catch (error) { return {ok: false, query: args.query, offline: true, error: error instanceof Error ? error.message : String(error)}; }
      }
      case 'write_test_suite': {
        await hooks?.stage('write-and-run');
        const paths = (args as unknown as SuiteInput).files.map(file => testPath(file.path));
        for (const file of paths) this.invalidate(file);
        return writeTestSuite(this.sandbox, args as unknown as SuiteInput, this.timeoutMs, this.maxArtifactBytes, signal);
      }
      case 'write_file': {
        const data = Buffer.from(args.content); if (data.length > 1_000_000) throw new Error('File too large for one write');
        this.invalidate(file());
        signal.throwIfAborted();
        await this.sandbox.write(file(), data, file().endsWith('.sh') ? 0o755 : undefined);
        return {ok: true, path: file(), bytes: data.length, hash: hash(data)};
      }
      case 'edit_file': {
        const original = await this.read(file(), true);
        if (args.expectedHash && args.expectedHash !== hash(original)) throw new Error('Stale file hash; read current file before editing');
        this.invalidate(file()); await this.pi.editFile(file(), args.old, args.replacement, signal);
        return {ok: true, path: file(), hash: hash(await this.read(file(), true))};
      }
      case 'apply_patch': {
        if (!args.operation) return this.patch(args.patch);
        this.invalidate(publicPath(args.operation.path));
        return applyV4APatch(this.sandbox, args, signal);
      }
      case 'exec': {
        if (!args.command.trim()) throw new Error('Command must be non-empty');
        this.invalidate();
        const result = await this.sandbox.exec(args.command, Math.min(this.timeoutMs, args.timeoutMs ?? this.timeoutMs), signal);
        return {ok: result.code === 0 && !result.timedOut, ...result, status: classify(result.code, result.stdout + '\n' + result.stderr, result.timedOut, result.truncated)};
      }
      case 'read_artifact': {
        const contents = await this.store.readArtifact(args.id), start = args.offset ?? 0;
        return textPage(contents, start, 'content', {ok: true, id: args.id, offset: start, totalChars: contents.length}, this.limit);
      }
      case 'update_notes': {
        for (const entry of args.entries ?? []) {
          entry.paths = entry.paths?.map((file: string) => publicPath(file));
          for (const artifact of entry.artifacts ?? []) await this.store.readArtifact(artifact);
        }
        this.memory.update(args.entries ?? [], this.revisions, args.remove);
        if (args.notes !== undefined) this.memory.setNotes(args.notes);
        await this.store.save(`memory/${hash(this.sandbox.id)}.json`, this.memory.snapshot());
        return {ok: true, saved: true, digest: this.memory.digest};
      }
      case 'view_image': {
        const data = await this.read(file()); if (data.length > 4_000_000) throw new Error('Image exceeds 4 MB');
        const type = data.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10])) ? 'png'
          : data[0] === 255 && data[1] === 216 && data[2] === 255 ? 'jpeg'
          : data.toString('ascii', 0, 4) === 'RIFF' && data.toString('ascii', 8, 12) === 'WEBP' ? 'webp' : undefined;
        if (!type) throw new Error('Invalid PNG/JPEG/WebP file signature');
        return {ok: true, image: data.toString('base64'), mediaType: `image/${type}`, path: file()};
      }
      case 'terminal': {
        if (!this.sandbox.terminal) throw new Error('Persistent terminal requires the native TestPrism runtime');
        this.invalidate(); // A running interactive process can mutate files even during a read.
        return this.sandbox.terminal(String(args.action), String(args.name), String(args.text), signal);
      }
      default: throw new Error(`Tool ${name} requires the agent host`);
    }
  }
  private async patch(input: string): Promise<Record<string, unknown>> {
    const {patches, recountedHunks} = parseAgentPatch(input);
    if (!patches.length || patches.length > 8) throw new Error('Supply a unified diff for 1–8 files');
    const changes: Array<{file: string; before?: Buffer; after?: Buffer; mode: number}> = [];
    const contextRecovered: string[] = [];
    for (const patch of patches) {
      const old = patch.oldFileName, next = patch.newFileName;
      if (patch.isBinary || patch.isRename || patch.isCopy || !patch.hunks.length)
        throw new Error('Only textual add/delete/update patches with hunks are supported');
      if (!old || !next) throw new Error('Patch requires --- and +++ file headers');
      const file = publicPath((next === '/dev/null' ? old : next).replace(/^[ab]\//, ''));
      if (old !== '/dev/null' && next !== '/dev/null' && old.replace(/^a\//, '') !== next.replace(/^b\//, '')) throw new Error('Renames are unsupported; use separate add and delete patches');
      if (changes.some(change => change.file === file)) throw new Error('Duplicate patch path');
      let before: Buffer | undefined;
      try { before = await this.sandbox.read(file); } catch (error) {
        if (old !== '/dev/null' || (error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      }
      if (old === '/dev/null' && before) throw new Error(`New file already exists: ${file}`);
      if (before && before.length > 1_000_000) throw new Error('Patch target exceeds 1 MB');
      let after = applyPatch(before?.toString('utf8') ?? '', patch, {fuzzFactor: 0, autoConvertLineEndings: false});
      if (after === false && before) {
        after = applyUniqueEdits(before.toString('utf8'), patch);
        if (after !== false) contextRecovered.push(file);
      }
      if (after === false) throw new Error(`Patch context does not match ${file}; read the current file`);
      if (Buffer.byteLength(after) > 1_000_000 || (next === '/dev/null' && after !== '')) throw new Error('Invalid deletion or oversized patch result');
      changes.push({file, before, after: next === '/dev/null' ? undefined : Buffer.from(after),
        mode: before ? await this.sandbox.mode(file) : file.endsWith('.sh') ? 0o755 : 0o644});
    }
    const applied: typeof changes = [];
    try {
      for (const change of changes) {
        this.invalidate(change.file); applied.push(change);
        if (change.after) await this.sandbox.write(change.file, change.after, change.mode); else await this.sandbox.remove(change.file);
      }
    } catch (error) {
      const failures = [];
      for (const change of applied.reverse()) {
        try { if (change.before) await this.sandbox.write(change.file, change.before, change.mode); else await this.sandbox.remove(change.file); }
        catch { failures.push(change.file); }
      }
      if (failures.length) throw new Error(`Patch failed and rollback incomplete: ${failures.join(', ')}. Inspect files before continuing.`);
      throw error;
    }
    return {ok: true, recountedHunks, contextRecovered, files: changes.map(change => ({path: change.file, deleted: !change.after, hash: change.after ? hash(change.after) : undefined}))};
  }
  async observe(result: Record<string, unknown>): Promise<Record<string, unknown>> {
    const serialized = JSON.stringify(result);
    if (serialized.length <= this.limit) return result;
    const artifact = await this.store.artifact(serialized);
    return {ok: result.ok, artifact, chars: serialized.length, preview: clip(serialized, Math.max(128, this.limit - 200))};
  }
}
