import { configSchema, type Config } from './types.js';
import { hash } from './util.js';

/** Separate frozen profiles, not an online holdout learner. Pair tasks/model/budgets externally. */
export function skillAblationProfiles(base: Config) {
  if (!base.search.skills.length) throw new Error('The base ablation profile must enable curated skills');
  return ([
    {id: 'no-skills', search: {...base.search, skills: [], skillRoutes: [], skillResources: false}},
    {id: 'methods', search: {...base.search, skillResources: false}},
    {id: 'methods-resources', search: {...base.search, skillResources: true}},
  ] as const).map(variant => {
    const config = configSchema.parse({...base, search: variant.search});
    return {id: variant.id, config, configDigest: hash(config)};
  });
}
