import { promises as fs, constants } from 'node:fs';
import path from 'node:path';
import type { Config, Execution, FileArtifact, Task } from './types.js';
import { atomicJSON, command, containedPath, errorText, id, mapLimit, publicPath, readJSON, shellQuote, testPath } from './util.js';

export interface Sandbox {
  id: string; root: string;
  read(file: string, maxBytes?: number): Promise<Buffer>;
  fingerprint?(file: string): Promise<string>;
  readVersioned?(file: string): Promise<{data: Buffer; version?: string}>;
  mode(file: string): Promise<number>;
  write(file: string, data: Buffer, mode?: number): Promise<void>;
  remove(file: string): Promise<void>;
  list(prefix?: string): Promise<string[]>;
  exec(script: string, timeoutMs: number, signal?: AbortSignal): Promise<Execution>;
  terminal?(action: string, name: string, text: string, signal: AbortSignal): Promise<Record<string, unknown>>;
  close(): Promise<void>;
}
export interface SandboxFactory {
  fork(label: string): Promise<Sandbox>;
  publish(files: FileArtifact[]): Promise<void>;
  close(): Promise<void>;
}
export async function applyFiles(sandbox: Sandbox, files: readonly FileArtifact[]): Promise<void> {
  for (const file of files) {
    if (file.deleted) await sandbox.remove(file.path);
    else await sandbox.write(file.path, Buffer.from(file.data, 'base64'), file.mode);
  }
}
export async function publishTests(target: Sandbox, files: FileArtifact[]): Promise<void> {
  for (const file of files) { testPath(file.path); if (file.deleted) throw new Error('A submission cannot contain deleted test files'); }
  if (!files.length) { await target.remove('agent-tests'); return; }
  const staging = id('.testhelix-publish'), backup = id('.testhelix-backup');
  try {
    await applyFiles(target, files.map(file => ({...file, path: `${staging}/${file.path}`})));
    const swap = await target.exec(`if [ -e agent-tests ] || [ -L agent-tests ]; then mv -- agent-tests ${shellQuote(backup)} || exit 1; fi\nif mv -- ${shellQuote(staging + '/agent-tests')} agent-tests; then rm -rf -- ${shellQuote(backup)}; else if [ -e ${shellQuote(backup)} ] || [ -L ${shellQuote(backup)} ]; then mv -- ${shellQuote(backup)} agent-tests; fi; exit 1; fi`, 10000);
    if (swap.code !== 0 || swap.timedOut) throw new Error(`Submission swap failed: ${swap.stderr}`);
  } finally { await target.remove(staging); }
}
const excluded = new Set(['.git', 'node_modules', '.venv', '__pycache__', '.pytest_cache', '.cache', '.testhelix-tmp', '.patch-generation-state', 'patch-sets']);

export class LocalSandbox implements Sandbox {
  readonly id: string;
  constructor(readonly root: string, private owned = true) { this.id = root; }
  async read(file: string, maxBytes?: number): Promise<Buffer> {
    const target = await containedPath(this.root, file);
    if (maxBytes === undefined) return fs.readFile(target);
    if (!Number.isSafeInteger(maxBytes) || maxBytes < 0) throw new Error('Invalid read byte limit');
    const handle = await fs.open(target, constants.O_RDONLY | constants.O_NONBLOCK);
    try {
      const stat = await handle.stat();
      if (!stat.isFile() || stat.size > maxBytes) throw new Error('File exceeds bounded read limit or is not a regular file');
      const data = Buffer.alloc(Math.min(maxBytes + 1, stat.size + 1));
      let size = 0;
      while (size < data.length) {
        const {bytesRead} = await handle.read(data, size, data.length - size, null);
        if (!bytesRead) break;
        size += bytesRead;
      }
      if (size !== stat.size) throw new Error('File changed during bounded read');
      return data.subarray(0, size);
    } finally {await handle.close();}
  }
  async fingerprint(file: string): Promise<string> {
    const stat = await fs.stat(await containedPath(this.root, file), {bigint: true});
    if (!stat.isFile()) throw new Error('Expected a regular file');
    if (stat.size > 8_000_000n) throw new Error('File exceeds the 8 MB read limit; use a bounded project command');
    return `${stat.dev}:${stat.ino}:${stat.size}:${stat.mtimeNs}:${stat.ctimeNs}`;
  }
  async mode(file: string): Promise<number> { return (await fs.stat(await containedPath(this.root, file))).mode & 0o777; }
  async write(file: string, data: Buffer, mode?: number): Promise<void> {
    const target = await containedPath(this.root, file);
    mode ??= await this.mode(file).catch(error => { if (error.code === 'ENOENT') return 0o644; throw error; });
    await fs.mkdir(path.dirname(target), {recursive: true});
    await fs.writeFile(target, data, {mode}); await fs.chmod(target, mode & 0o777);
  }
  async remove(file: string): Promise<void> { await fs.rm(await containedPath(this.root, file), {force: true, recursive: true}); }
  async list(prefix = ''): Promise<string[]> {
    const base = prefix ? await containedPath(this.root, prefix) : this.root;
    const output: string[] = [];
    const walk = async (directory: string) => {
      const entries = await fs.readdir(directory, {withFileTypes: true}).catch(error => {
        if (error.code === 'ENOENT') return []; throw error;
      });
      for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
        if (excluded.has(entry.name) || entry.name === 'solution.sh') continue;
        const absolute = path.join(directory, entry.name);
        if (entry.isDirectory()) await walk(absolute);
        else if (entry.isFile()) output.push(path.relative(this.root, absolute).split(path.sep).join('/'));
      }
    };
    await walk(base); return output;
  }
  exec(script: string, timeoutMs: number, signal?: AbortSignal): Promise<Execution> {
    const environment: NodeJS.ProcessEnv = {
      PATH: process.env.PATH, LANG: 'C.UTF-8', HOME: this.root,
      TMPDIR: path.join(this.root, '.testhelix-tmp'), PYTHONPATH: this.root,
    };
    return fs.mkdir(environment.TMPDIR!, {recursive: true}).then(() => command(['bash', '-lc', script],
      {cwd: this.root, timeoutMs, signal, env: environment}));
  }
  async close(): Promise<void> { if (this.owned) await fs.rm(this.root, {recursive: true, force: true}); }
}

interface LocalLease {version: 1; original: string; owner: string; ready: boolean; children: string[]}
async function realLocation(value: string): Promise<string> {
  const suffix: string[] = [];
  for (let current = path.resolve(value);;) {
    try {return path.join(await fs.realpath(current), ...suffix);}
    catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'ENOENT') throw error;
      suffix.unshift(path.basename(current)); current = path.dirname(current);
    }
  }
}
export class LocalFactory implements SandboxFactory {
  private sandboxes = new Set<Sandbox>();
  private writes: Promise<void> = Promise.resolve();
  private closed = false;
  private constructor(readonly original: string, readonly baseline: string, readonly runs: string, private lease: LocalLease) {}
  static async create(root: string, runs: string): Promise<LocalFactory> {
    root = await fs.realpath(path.resolve(root)); runs = await realLocation(runs);
    if (runs === root || runs.startsWith(root + path.sep))
      throw new Error('Run artifacts must be outside the task workspace');
    await fs.mkdir(runs, {recursive: true});
    const journal = path.join(runs, 'local-resources.json');
    const previous = await readJSON<LocalLease>(journal);
    const lease: LocalLease = previous ?? {version: 1, original: root, owner: id('local'), ready: false, children: []};
    if (lease.version !== 1 || lease.original !== root || !/^local-[a-z0-9-]+$/.test(lease.owner) || typeof lease.ready !== 'boolean'
      || !Array.isArray(lease.children) || lease.children.some(name => typeof name !== 'string' || !/^[a-zA-Z0-9-]+$/.test(name) || !name.startsWith(lease.owner + '-child-')))
      throw new Error('Invalid local workspace ownership record');
    const baseline = path.join(runs, `${lease.owner}-baseline`);
    // A crashed run leaves an immutable ready snapshot and a list of owned
    // branches. Reuse that snapshot; never overlay current files onto it.
    await mapLimit(lease.children, 8, name => fs.rm(path.join(runs, name), {recursive: true, force: true}));
    lease.children = [];
    if (lease.ready) {
      const stat = await fs.lstat(baseline);
      if (!stat.isDirectory() || stat.isSymbolicLink()) throw new Error('Local snapshot is missing or replaced');
    } else await fs.rm(baseline, {recursive: true, force: true});
    await atomicJSON(journal, lease);
    try {
      if (!lease.ready) {
        await fs.cp(root, baseline, {recursive: true, mode: constants.COPYFILE_FICLONE,
          filter: source => !['.git', 'agent-tests', '.testhelix-tmp'].includes(path.basename(source))});
        lease.ready = true; await atomicJSON(journal, lease);
      }
      return new LocalFactory(root, baseline, runs, lease);
    } catch (error) {await fs.rm(baseline, {recursive: true, force: true}); await fs.rm(journal, {force: true}); throw error;}
  }
  async fork(label: string): Promise<Sandbox> {
    if (this.closed) throw new Error('Local factory is closed');
    const name = id(`${this.lease.owner}-child-${label.replace(/[^a-z0-9-]/gi, '-').slice(0, 40)}`);
    const root = path.join(this.runs, name);
    const sandbox = new LocalSandbox(root); this.sandboxes.add(sandbox);
    this.lease.children.push(name);
    const snapshot = structuredClone(this.lease);
    this.writes = this.writes.then(() => atomicJSON(path.join(this.runs, 'local-resources.json'), snapshot));
    try {await this.writes; await fs.cp(this.baseline, root, {recursive: true, mode: constants.COPYFILE_FICLONE}); return sandbox;}
    catch (error) {await sandbox.close(); throw error;}
  }
  async publish(files: FileArtifact[]): Promise<void> {
    const target = new LocalSandbox(this.original, false);
    await publishTests(target, files);
  }
  async close(): Promise<void> {
    if (this.closed) return;
    await this.writes.catch(() => {});
    const removed = await Promise.allSettled([...this.sandboxes].map(s => s.close()));
    const failures = removed.filter((result): result is PromiseRejectedResult => result.status === 'rejected');
    if (failures.length) throw new AggregateError(failures.map(result => result.reason), 'Local branches could not be removed; ownership journal retained');
    await fs.rm(this.baseline, {recursive: true, force: true});
    await fs.rm(path.join(this.runs, 'local-resources.json'), {force: true});
    this.closed = true;
  }
}

async function docker(args: string[], options: Parameters<typeof command>[1] = {}): Promise<Execution> {
  const result = await command(['docker', ...args], options);
  if (result.code !== 0 || result.timedOut) throw Object.assign(new Error(`docker ${args[0]} failed (exit ${result.code}${result.timedOut ? ', timed out' : ''}): ${errorText(result.stderr || result.stdout || (result.code === 44 ? 'Public path does not exist' : 'Command produced no diagnostic output')).slice(-1500)}`),
    {code: result.code === 44 ? 'ENOENT' : undefined});
  return result;
}
export class DockerSandbox implements Sandbox {
  readonly id: string;
  private alive = true;
  private removed = false;
  constructor(readonly container: string, readonly root: string, private owned = true) { this.id = container; }
  private guarded(file: string): {target: string; check: string} {
    const target = path.posix.join(this.root, publicPath(file));
    // Check existing ancestors as well as the file, rejecting symlink escapes.
    const check = `p=${shellQuote(target)}; while [ ! -e "$p" ] && [ ! -L "$p" ]; do p=$(dirname "$p"); done; real=$(readlink -f "$p") || exit 76; base=$(readlink -f ${shellQuote(this.root)}) || exit 76; case "$real" in "$base"|"$base"/*) ;; *) exit 76;; esac; rel="\${real#"$base"}"; case "$rel/" in */.git/*|*/.patch-generation-state/*|*/patch-sets/*|*/solution.sh/*) exit 76;; esac`;
    return {target, check};
  }
  private async target(file: string): Promise<string> {
    const {target, check} = this.guarded(file);
    await docker(['exec', this.container, 'sh', '-c', check]); return target;
  }
  async fingerprint(file: string): Promise<string> {
    const {target, check} = this.guarded(file);
    return (await docker(['exec', this.container, 'sh', '-c', `${check}\n[ -f ${shellQuote(target)} ] && stat -c '%d:%i:%s:%y:%z' -- ${shellQuote(target)}`])).stdout.trim();
  }
  async read(file: string, maxBytes?: number): Promise<Buffer> {
    return (await this.readVersioned(file, maxBytes)).data;
  }
  async readVersioned(file: string, maxBytes = 8000000): Promise<{data: Buffer; version?: string}> {
    if (!Number.isSafeInteger(maxBytes) || maxBytes < 0) throw new Error('Invalid read byte limit');
    maxBytes = Math.min(maxBytes, 8000000);
    const {target, check} = this.guarded(file);
    // base64 keeps arbitrary binary bytes intact through the process logger.
    const version = `stat -c '%d:%i:%s:%y:%z' -- ${shellQuote(target)}`;
    const result = await docker(['exec', this.container, 'sh', '-c', `${check}\n[ -e ${shellQuote(target)} ] || exit 44\n[ -f ${shellQuote(target)} ] && [ "$(stat -c %s -- ${shellQuote(target)})" -le ${maxBytes} ] || exit 65\nbefore=$(${version}) || exit 1\nprintf '%s\\n' "$before"\nhead -c ${maxBytes + 1} < ${shellQuote(target)} | base64 || exit 1\nprintf '\\n'\n${version}`], {maxBytes: Math.ceil((maxBytes + 1) * 1.4) + 4096});
    if (result.truncated) throw new Error('File exceeds the read limit');
    const lines = result.stdout.trimEnd().split('\n'), before = lines.shift(), after = lines.pop();
    const data = Buffer.from(lines.join('\n'), 'base64');
    if (data.length > maxBytes) throw new Error('File exceeds bounded read limit');
    if (data.length !== Number(before?.split(':')[2])) throw new Error('File changed during bounded read');
    return {data, version: before && before === after ? before : undefined};
  }
  async mode(file: string): Promise<number> {
    const {target, check} = this.guarded(file);
    const result = await docker(['exec', this.container, 'sh', '-c', `${check}\n[ -e ${shellQuote(target)} ] || [ -L ${shellQuote(target)} ] || exit 44\nstat -c %a -- ${shellQuote(target)}`]);
    return parseInt(result.stdout.trim(), 8) & 0o777;
  }
  async write(file: string, data: Buffer, mode?: number): Promise<void> {
    const {target, check} = this.guarded(file);
    await docker(['exec', '-i', this.container, 'sh', '-c',
      `${check}\nmkdir -p ${shellQuote(path.posix.dirname(target))} && cat > ${shellQuote(target)}${mode === undefined ? '' : ` && chmod ${(mode & 0o777).toString(8)} ${shellQuote(target)}`}`], {input: data});
  }
  async remove(file: string): Promise<void> {
    const {target, check} = this.guarded(file);
    await docker(['exec', this.container, 'sh', '-c', `${check}\nrm -rf -- ${shellQuote(target)}`]);
  }
  async list(prefix = ''): Promise<string[]> {
    const base = prefix ? await this.target(prefix) : this.root;
    const ignores = [...excluded].map(x => `-name ${shellQuote(x)}`).join(' -o ');
    const result = await docker(['exec', this.container, 'sh', '-c',
      `cd ${shellQuote(this.root)} && if [ -d ${shellQuote(base)} ]; then find ${shellQuote(prefix || '.')} \\( ${ignores} \\) -prune -o -type f ! -name solution.sh -print; fi`], {maxBytes: 2_000_000});
    if (result.truncated) throw new Error('File listing too large; use a narrower directory');
    return result.stdout.split('\n').map(x => x.replace(/^\.\//, '')).filter(Boolean).sort();
  }
  async exec(script: string, timeoutMs: number, signal?: AbortSignal): Promise<Execution> {
    if (!this.alive) throw new Error('Sandbox was stopped after a previous timeout');
    return command(['docker', 'exec', '-w', this.root, this.container, 'bash', '-lc',
      `export PYTHONPATH=${shellQuote(this.root)}\${PYTHONPATH:+:\${PYTHONPATH}}\n${script}`], {
      timeoutMs, signal, onTimeout: async () => { if (this.owned) await this.close(); },
    });
  }
  async close(): Promise<void> {
    if (this.owned && !this.removed) {
      this.alive = false;
      const result = await command(['docker', 'rm', '-f', this.container], {timeoutMs: 15000});
      if (result.timedOut || (result.code !== 0 && !/No such container/i.test(result.stderr)))
        throw new Error(`Could not remove sandbox ${this.container}: ${result.stderr}`);
      this.removed = true;
    }
  }
}

export class DockerFactory implements SandboxFactory {
  private sandboxes = new Set<Sandbox>();
  private children: string[] = [];
  private writes: Promise<void> = Promise.resolve();
  private closed = false;
  private constructor(readonly original: string, readonly root: string, readonly image: string, readonly network: string,
    readonly owner: string, readonly resourcesFile?: string, readonly resourceArgs: string[] = []) {}
  static async create(container: string, root: string, network: Config['execution']['network'], resourcesFile?: string): Promise<DockerFactory> {
    const inspection = JSON.parse((await docker(['inspect', container])).stdout)[0];
    if (!path.posix.isAbsolute(root)) throw new Error('Docker workdir must be absolute');
    if (inspection.Mounts?.some((mount: {Destination: string}) => root === mount.Destination || root.startsWith(mount.Destination + '/') || mount.Destination.startsWith(root + '/')))
      throw new Error('Workspace intersects a mounted volume; a Docker commit would omit task state. Use an image-backed task workspace.');
    const owner = id('owner'), image = `testhelix-snapshot:${owner}`;
    if (resourcesFile) {
      await cleanupResources(resourcesFile);
      await atomicJSON(resourcesFile, {owner, image, containers: []});
    }
    await docker(['commit', '--change', `LABEL testhelix.owner=${owner}`, container, image], {timeoutMs: 120000});
    const networks = Object.keys(inspection.NetworkSettings?.Networks ?? {});
    const inherited = networks.find(x => !['host', 'none'].includes(x)) ?? 'none';
    const limits: string[] = [], host = inspection.HostConfig ?? {};
    if (host.Memory > 0) limits.push('--memory', String(host.Memory));
    if (host.NanoCpus > 0) limits.push('--cpus', String(host.NanoCpus / 1e9));
    if (host.PidsLimit > 0) limits.push('--pids-limit', String(host.PidsLimit));
    if (host.ShmSize > 0) limits.push('--shm-size', String(host.ShmSize));
    return new DockerFactory(container, root, image, network === 'inherit' ? inherited : 'none', owner, resourcesFile, limits);
  }
  async fork(label: string): Promise<Sandbox> {
    if (this.closed) throw new Error('Docker factory is closed');
    const name = id(`testhelix-${label.replace(/[^a-z0-9-]/gi, '-').slice(0, 35)}`);
    this.children.push(name);
    if (this.resourcesFile) {
      const lease = {owner: this.owner, image: this.image, containers: [...this.children]};
      this.writes = this.writes.then(() => atomicJSON(this.resourcesFile!, lease)); await this.writes;
    }
    try {
      const container = (await docker(['run', '-d', '--name', name, '--label', 'testhelix.owned=true',
        '--label', `testhelix.owner=${this.owner}`,
        '--network', this.network, '--workdir', this.root, ...this.resourceArgs, this.image])).stdout.trim();
      const sandbox = new DockerSandbox(container, this.root); this.sandboxes.add(sandbox);
      await sandbox.remove('agent-tests');
      return sandbox;
    } catch (error) {
      // docker run may create a stopped container before reporting start failure.
      // The recorded name exists before the command and is guarded by ownership.
      await removeOwnedContainer(name, this.owner);
      throw error;
    }
  }
  async publish(files: FileArtifact[]): Promise<void> {
    const target = new DockerSandbox(this.original, this.root, false);
    await publishTests(target, files);
  }
  async close(): Promise<void> {
    if (this.closed) return;
    await this.writes.catch(() => {}); // A failed journal cannot bypass in-memory cleanup.
    await Promise.allSettled([...this.sandboxes].map(s => s.close()));
    const cleanup = await Promise.allSettled(this.children.map(name => removeOwnedContainer(name, this.owner)));
    const failed = cleanup.filter((result): result is PromiseRejectedResult => result.status === 'rejected');
    if (failed.length) throw new AggregateError(failed.map(result => result.reason), 'Owned Docker resources could not be removed; ownership journal retained');
    await removeOwnedImage(this.image, this.owner);
    this.closed = true;
  }
}
async function removeOwnedContainer(name: string, owner: string): Promise<void> {
  const inspection = await command(['docker', 'inspect', '--format', '{{index .Config.Labels "testhelix.owner"}}', name], {timeoutMs: 10000});
  if (!inspection.timedOut && inspection.code !== 0 && /No such (?:object|container)/i.test(inspection.stderr)) return;
  if (inspection.timedOut || inspection.code !== 0) throw new Error(`Could not inspect owned container ${name}: ${inspection.stderr}`);
  if (inspection.stdout.trim() !== owner) throw new Error(`Container ownership changed: ${name}`);
  await docker(['rm', '-f', name], {timeoutMs: 15000});
}
async function removeOwnedImage(image: string, owner: string): Promise<void> {
  const inspection = await command(['docker', 'image', 'inspect', '--format', '{{index .Config.Labels "testhelix.owner"}}', image], {timeoutMs: 10000});
  if (!inspection.timedOut && inspection.code !== 0 && /No such (?:object|image)/i.test(inspection.stderr)) return;
  if (inspection.timedOut || inspection.code !== 0) throw new Error(`Could not inspect snapshot ${image}: ${inspection.stderr}`);
  if (inspection.stdout.trim() !== owner) throw new Error(`Snapshot ownership changed: ${image}`);
  await docker(['image', 'rm', image], {timeoutMs: 30000});
}
export async function createFactory(task: Task, config: Config, runs: string, remainingMs = () => config.budget.wallMs): Promise<SandboxFactory> {
  if (task.runtime.kind === 'testprism') return new (await import('./native-runtime.js')).NativeFactory(task.runtime, remainingMs);
  return task.runtime.kind === 'local' ? LocalFactory.create(task.runtime.root, runs)
    : DockerFactory.create(task.runtime.container, task.runtime.root, config.execution.network, path.join(runs, 'resources.json'));
}
export async function cleanupResources(resourcesFile: string): Promise<void> {
  const lease = await readJSON<{owner: string; image: string; containers: string[]}>(resourcesFile);
  if (!lease) return;
  if (!/^owner-[a-z0-9-]+$/.test(lease.owner) || lease.image !== `testhelix-snapshot:${lease.owner}`)
    throw new Error('Invalid resource ownership record');
  for (const name of lease.containers) {
    if (!/^testhelix-[a-z0-9-]+$/i.test(name)) throw new Error('Invalid owned container name');
  }
  await mapLimit(lease.containers, 4, name => removeOwnedContainer(name, lease.owner));
  await removeOwnedImage(lease.image, lease.owner);
}
