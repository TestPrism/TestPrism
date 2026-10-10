/** Reusable failure labels only: never export task literals, paths, assertions or solutions. */
const rules = [
    ['provider-rate-limit', /rate.?limit|\b429\b/i],
    ['provider-model-unavailable', /model.*(?:not supported|not found|does not exist)|unsupported model/i],
    ['provider-unavailable', /admission paused \(unavailable\)/i],
    ['provider-upstream', /admission paused \(upstream\)|(?:HTTP|status(?:Code)?)\D{0,8}(?:500|502|503|504)\b|gateway timeout|bad gateway|service unavailable/i],
    ['provider-authentication', /authentication|unauthorized|invalid api key/i],
    ['context-overflow', /context.*(?:exceed|overflow|large)|input.*budget/i],
    ['framework-preparation', /ModuleNotFoundError|ImportError|ImproperlyConfigured|framework.*(?:setup|fail)/i],
    ['missing-task-cases', /No task tests|no.*task.*cases|empty.*harness/i],
    ['missing-submission', /no_submit|no.*candidate|missing.*runner/i],
    ['review-citation', /citation|quote|taskLines|line.*(?:range|bound)/i],
    ['contract-overconstraint', /overconstrain|undocumented|valid.*(?:reject|implementation)/i],
    ['nondeterministic-execution', /unstable|nondetermin|flaky/i],
    ['execution-timeout', /timed.?out|timeout/i],
  ] as const;
export type FailureClass = typeof rules[number][0];
export const failureClasses = rules.map(([label]) => label) as [FailureClass, ...FailureClass[]];
export function failureLabels(text: string): FailureClass[] {
  return rules.filter(([, pattern]) => pattern.test(text)).map(([label]) => label);
}
