import { readFileSync, readdirSync } from 'node:fs';
import {loadSkillsFromDir, parseFrontmatter} from '@earendil-works/pi-coding-agent';
import {fileURLToPath} from 'node:url';
import { hash } from './util.js';
import { skillContext, type SkillContext } from './skill-context.js';
import {lessonComponents, type LessonComponent} from './lessons.js';

// Curated, versioned runtime skills. These contain methods only, never benchmark
// answers. No task directory, trajectory or external file is a skill source.
export const skillIds = ['artifact-workflow', 'framework-tests', 'observable-oracles', 'failure-triage',
  'contract-partitions', 'small-input-oracles', 'stateful-sequences', 'performance-controls', 'cli-artifacts', 'visual-contracts', 'contract-audit', 'control-evidence'] as const;
export type SkillId = typeof skillIds[number];
export const reviewerOnlySkills: readonly SkillId[] = ['contract-audit', 'control-evidence'];
export function skillResources(id: SkillId): Array<{path: string; data: Buffer}> {
  const root = new URL(`../skills/testprism/${id}/`, import.meta.url);
  const visit = (prefix: string): Array<{path: string; data: Buffer}> => readdirSync(new URL(prefix, root), {withFileTypes: true})
    .sort((a, b) => a.name.localeCompare(b.name)).flatMap(entry => {
      if (entry.name === '__pycache__' || entry.name.endsWith('.pyc')) return [];
      if (entry.isSymbolicLink()) throw new Error('Host skill resources must not be symlinks');
      const path = prefix + entry.name;
      return entry.isDirectory() ? visit(path + '/') : [{path, data: readFileSync(new URL(path, root))}];
    });
  return visit('');
}
export const builtinSkills: Record<SkillId, {summary: string; text: string; digest: string; components: LessonComponent[]}> = Object.fromEntries(skillIds.map(id => {
  // Resolve only fixed host-owned files, never the benchmark workspace or a model path.
  const resources = skillResources(id); // Reject links before third-party discovery.
  const loaded = loadSkillsFromDir({dir: fileURLToPath(new URL(`../skills/testprism/${id}/`, import.meta.url)), source: 'testhelix-curated'});
  if (loaded.diagnostics.length || loaded.skills.length !== 1 || loaded.skills[0]!.name !== id)
    throw new Error(`Invalid Pi skill package: ${id}`);
  const raw = readFileSync(new URL(`../skills/testprism/${id}/SKILL.md`, import.meta.url), 'utf8');
  const {frontmatter: metadata, body} = parseFrontmatter<{name: string; description: string; metadata?: {components: LessonComponent[]}}>(raw);
  if (metadata.name !== id || typeof metadata.description !== 'string' || metadata.description.length > 160 || !body.trim())
    throw new Error(`Invalid host skill metadata: ${id}`);
  const components = metadata.metadata?.components;
  if (!Array.isArray(components) || !components.length || components.some(value => !lessonComponents.includes(value)))
    throw new Error(`Skill requires valid component ownership: ${id}`);
  return [id, {summary: metadata.description, text: body.trim(), components, digest: hash(resources.map(file => [file.path, hash(file.data)]))}];
})) as Record<SkillId, {summary: string; text: string; digest: string; components: LessonComponent[]}>;
export function skillReferences(id: SkillId): string[] {
  return skillResources(id).filter(file => /^references\/[a-z0-9/_-]+\.md$/.test(file.path)).map(file => file.path);
}
export function readSkillReference(id: SkillId, reference: string): string {
  if (!skillReferences(id).includes(reference))
    throw new Error(`Unknown curated skill reference. Available for ${id}: ${skillReferences(id).join(', ') || '(none)'}`);
  const data = skillResources(id).find(file => file.path === reference)!.data;
  if (data.length > 12000) throw new Error('Skill reference exceeds bounded resource size');
  return data.toString('utf8');
}
export interface SkillRoute { context: SkillContext; skill: SkillId | null }
export const contextSkills: Record<SkillContext, readonly SkillId[]> = {
  general: ['contract-partitions', 'observable-oracles'], algorithm: ['small-input-oracles', 'contract-partitions'],
  framework: ['framework-tests', 'stateful-sequences'], stateful: ['stateful-sequences', 'framework-tests'],
  performance: ['performance-controls', 'small-input-oracles'], cli: ['cli-artifacts', 'framework-tests'],
  visual: ['visual-contracts', 'framework-tests'],
};
export function skillOffer(enabled: readonly SkillId[], reviewer = false,
  task?: {instruction: string; source: string; routes: readonly SkillRoute[]}):
  {text: string; digest: string; ids: SkillId[]; preloaded: SkillId[]; context: SkillContext; catalog: SkillId[]} {
  const ids = [...new Set(enabled)].filter(id => reviewer
    ? ['observable-oracles', 'failure-triage', ...reviewerOnlySkills].includes(id) : !reviewerOnlySkills.includes(id));
  const context = task ? skillContext(task.instruction, task.source) : 'general';
  const route = task?.routes.find(route => route.context === context);
  // Context labels can stratify experiments; only an explicitly configured
  // route preloads a specialist. Otherwise the model chooses from the catalog.
  const chosen = route?.skill;
  const preloaded: SkillId[] = ids.includes('artifact-workflow') ? ['artifact-workflow'] : [];
  if (reviewer) preloaded.push(...reviewerOnlySkills.filter(id => ids.includes(id)));
  if (task && !reviewer && chosen && ids.includes(chosen) && !preloaded.includes(chosen)) preloaded.push(chosen);
  const catalog = ids.filter(id => !preloaded.includes(id));
  if (!ids.length) return {text: '', digest: hash([]), ids, preloaded, context, catalog};
  const prefix = 'Generic method skills; the original public contract has priority. These are guidance, not task evidence.\n';
  const body = () => preloaded.map(id => `Preloaded ${id}:\n${builtinSkills[id].text}\n`).join('');
  const suffix = () => task
    ? `Other enabled methods can be loaded by ID with load_skill alongside independent reads. Relevant catalog:\n${catalog.map(id => `${id}: ${builtinSkills[id].summary}`).join('\n')}`
    : `Other enabled methods can be loaded by ID with load_skill: ${catalog.join(', ')}`;
  // Shrink the index first: a frozen route must actually inject its selected method.
  while ((prefix + body() + suffix()).length > 3200 && catalog.length) catalog.pop();
  return {ids, digest: hash({library: ids.map(id => [id, builtinSkills[id]]), preloaded, catalog, context}), preloaded, context, catalog,
    text: prefix + body() + suffix()};
}
