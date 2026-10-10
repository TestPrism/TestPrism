import { z } from 'zod';
import { skillContexts } from './skill-context.js';
import { skillIds, reviewerOnlySkills } from './skills.js';

export const skillRoutesSchema = z.array(z.strictObject({context: z.enum(skillContexts), skill: z.enum(skillIds).refine((id): boolean => !reviewerOnlySkills.includes(id), 'Reviewer-only skills cannot be candidate routes').nullable()}))
  .max(skillContexts.length).refine(routes => new Set(routes.map(route => route.context)).size === routes.length, 'Duplicate skill route contexts');
