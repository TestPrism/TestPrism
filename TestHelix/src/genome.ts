import { z } from 'zod';
import { configSchema, type Config } from './types.js';
import { hash } from './util.js';
import { lessonLibrarySchema, validateLessonDelta } from './lessons.js';
import { skillRoutesSchema } from './skill-policy.js';

export const genesSchema = z.object({
  guidance: z.string().max(2400),
  lessons: lessonLibrarySchema,
  skillRoutes: skillRoutesSchema.default([]),
  initialCandidates: z.number().int().min(1).max(4), maxCandidates: z.number().int().min(1).max(4),
  maxRounds: z.number().int().min(1).max(3), agentSteps: z.number().int().min(2).max(20),
  repairSteps: z.number().int().min(2).max(10),
  contextTokens: z.number().int().min(2000).max(64000),
  toolOutputChars: z.number().int().min(512).max(20000),
  reviewMaxOutputTokens: z.number().int().min(128).max(16384),
}).strict().refine(value => value.initialCandidates <= value.maxCandidates, 'initialCandidates exceeds maxCandidates');
export type Genes = z.infer<typeof genesSchema>;
export type Gene = keyof Genes;
export const policyGenes = Object.keys(genesSchema.shape) as [Gene, ...Gene[]];
export interface Genome { id: string; generation: number; genes: Genes; parents: string[];
  mutation?: {component: Gene; rationale: string; evidence: string[]; operator: 'reflection' | 'catalog' | 'crossover'; arm?: string} }
export function genesis(config: Config): Genome {
  const {guidance, lessons, initialCandidates, maxCandidates, maxRounds, agentSteps, repairSteps} = config.search;
  const genes = genesSchema.parse({guidance, lessons, skillRoutes: config.search.skillRoutes, initialCandidates, maxCandidates, maxRounds, agentSteps, repairSteps,
    contextTokens: config.context.maxTokens,
    toolOutputChars: config.context.toolOutputChars, reviewMaxOutputTokens: config.model.reviewMaxOutputTokens});
  return {id: `genome-${hash(genes).slice(0, 24)}`, generation: 0, genes, parents: []};
}
export function child(parent: Genome, raw: unknown, mutation: NonNullable<Genome['mutation']>,
  forbiddenTaskIds: string[], donor?: Genome): Genome {
  const genes = genesSchema.parse(raw);
  const changed = (Object.keys(genes) as Gene[]).filter(key => hash(genes[key]) !== hash(parent.genes[key]));
  if (changed.length !== 1 || changed[0] !== mutation.component) throw new Error('A mutation must change exactly its declared policy gene');
  if (!mutation.rationale.trim() || !mutation.evidence.length) throw new Error('Mutation requires rationale and training evidence');
  if (mutation.component === 'lessons') validateLessonDelta(parent.genes.lessons, genes.lessons);
  const text = JSON.stringify({guidance: genes.guidance, lessons: genes.lessons});
  if (/```|BEGIN (?:RSA |OPENSSH )?PRIVATE KEY|\bsk-[a-zA-Z0-9_-]{12,}/.test(text)) throw new Error('Policy must contain reusable prose, not code blocks or secrets');
  const normalized = text.toLowerCase().replace(/[^a-z0-9]/g, '');
  if (forbiddenTaskIds.some(id => [id, id.split('/').at(-1)!].some(part => part.length > 4 && normalized.includes(part.toLowerCase().replace(/[^a-z0-9]/g, '')))))
    throw new Error('Policy contains a training/validation/holdout task identifier');
  if (new Set(genes.lessons.map(lesson => lesson.id)).size !== genes.lessons.length) throw new Error('Duplicate lesson IDs');
  return {id: `genome-${hash(genes).slice(0, 24)}`, generation: parent.generation + 1, genes,
    parents: donor ? [parent.id, donor.id] : [parent.id], mutation};
}
export function applyGenome(config: Config, genome: Genome): Config {
  const {contextTokens, toolOutputChars, reviewMaxOutputTokens, ...search} = genesSchema.parse(genome.genes);
  return configSchema.parse({...config, search: {...config.search, ...search},
    context: {...config.context, maxTokens: contextTokens, toolOutputChars},
    model: {...config.model, reviewMaxOutputTokens}});
}
export function random(seed: string): () => number {
  let state = parseInt(hash(seed).slice(0, 8), 16) || 1;
  return () => { state ^= state << 13; state ^= state >>> 17; state ^= state << 5; return (state >>> 0) / 4294967296; };
}
