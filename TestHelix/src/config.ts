import { promises as fs } from 'node:fs';
import path from 'node:path';
import {parse as parseTOML} from 'smol-toml';
import { configSchema, taskSchema, type Config, type Task } from './types.js';

function interpolate(value: unknown): unknown {
  if (typeof value === 'string') return value.replace(/\$\{([A-Z_][A-Z0-9_]*)\}/g, (_, name: string) => {
    const resolved = process.env[name];
    if (resolved === undefined) throw new Error(`Missing configuration environment variable: ${name}`);
    return resolved;
  });
  if (Array.isArray(value)) return value.map(interpolate);
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, interpolate(v)]));
  return value;
}
export async function loadConfig(file: string): Promise<Config> {
  const config = configSchema.parse(interpolate(await loadDocument(file)));
  config.scheduler.directory = path.resolve(path.dirname(file), config.scheduler.directory);
  return config;
}
/** Input syntax only; all formats pass through the same schema and defaults. */
export async function loadDocument(file: string): Promise<unknown> {
  const text = await fs.readFile(file, 'utf8');
  return path.extname(file).toLowerCase() === '.toml' ? parseTOML(text) : JSON.parse(text);
}
export async function loadTask(file: string): Promise<Task> {
  const task = taskSchema.parse(JSON.parse(await fs.readFile(file, 'utf8')));
  if (task.runtime.kind === 'local') task.runtime.root = path.resolve(path.dirname(file), task.runtime.root);
  return task;
}
