export const skillContexts = ['general', 'algorithm', 'framework', 'stateful', 'performance', 'cli', 'visual'] as const;
export type SkillContext = typeof skillContexts[number];
export const capabilityTags = ['repository', 'algorithm', 'terminal', 'performance', 'visual', 'application', 'security'] as const;
export const words = (text: string): string[] => text.toLowerCase().match(/[\p{L}\p{N}]+/gu) ?? [];
export function phraseMatches(surface: string, phrase: string): boolean {
  const needle = words(phrase).join(' ');
  return !!needle && ` ${words(surface).join(' ')} `.includes(` ${needle} `);
}

/** Downweight benchmark delivery instructions, never remove them from the model's contract. */
export function taskDomain(instruction: string): {domain: string; delivery: string; examples: string} {
  const domain: string[] = [], delivery: string[] = [], examples: string[] = [];
  let deliveryLevel = 0;
  let fence = '';
  for (const line of instruction.split('\n')) {
    const marker = /^\s*(`{3,}|~{3,})/.exec(line)?.[1];
    if (marker && (!fence || (marker[0] === fence[0] && marker.length >= fence.length))) {
      (deliveryLevel ? delivery : examples).push(line); fence = fence ? '' : marker; continue;
    }
    if (fence) { (deliveryLevel ? delivery : examples).push(line); continue; }
    const heading = /^(#{1,6})\s+(.+?)\s*#*\s*$/.exec(line);
    if (heading) {
      const level = heading[1]!.length;
      if (deliveryLevel && level <= deliveryLevel) deliveryLevel = 0;
      if (/^(?:test output location(?: & entry point)?|before you write tests|test submission|submission instructions|evaluation instructions)$/i.test(heading[2]!.trim())) deliveryLevel = level;
    }
    (deliveryLevel ? delivery : domain).push(line);
  }
  return {domain: domain.join('\n'), delivery: delivery.join('\n'), examples: examples.join('\n')};
}
/** Descriptive public-text labels for retrieval and experiment strata. These do
 * not select a runtime procedure or impose a task-specific acceptance rule. */
export function explainSkillContext(instruction: string, _source = '') {
  const {domain, delivery, examples} = taskDomain(instruction);
  const terms: Record<Exclude<SkillContext, 'general'>, string[]> = {
    algorithm: ['algorithm', 'integer', 'graph', 'sort', 'matrix', 'numerical', 'floating point', 'sequence', 'array', 'parser', 'equation', 'polynomial', 'number theory', 'algebraic'],
    framework: ['django', 'pytest', 'pylint', 'django db', 'djangoproject', 'plugin', 'framework', 'import', 'registration', 'migration', 'schema', 'queryset', 'subquery', 'orm'],
    stateful: ['transaction', 'rollback', 'database', 'cache', 'concurrent', 'lifecycle', 'session', 'state machine'],
    performance: ['performance', 'optimize', 'optimise', 'complexity', 'latency', 'memory usage', 'allocation', 'benchmark'],
    cli: ['command line', 'stdout', 'stderr', 'exit code', 'shell', 'filesystem', 'directory', 'command line interface'],
    visual: ['screenshot', 'layout', 'image', 'pixel', 'browser', 'pdf', 'css', 'canvas', 'renderer'],
  };
  const scores = Object.entries(terms).map(([context, triggers]) => {
    const matched = triggers.filter(term => phraseMatches(domain, term));
    const exampleOnly = triggers.filter(term => !matched.includes(term) && phraseMatches(examples, term));
    const deliveryOnly = triggers.filter(term => !matched.includes(term) && phraseMatches(delivery, term));
    // A named framework is more informative than generic import/directory words.
    const anchor = context === 'framework' && phraseMatches(domain, 'django') ? 2 : 0;
    const substantive = matched.length + exampleOnly.length * .15 + anchor;
    return {context: context as SkillContext, matched, exampleOnly, deliveryOnly, anchor, substantive,
      score: substantive + deliveryOnly.length * .05};
  });
  scores.sort((a, b) => b.score - a.score || skillContexts.indexOf(a.context) - skillContexts.indexOf(b.context));
  const selected = scores.find(score => score.substantive > 0);
  return {context: selected?.context ?? 'general' as SkillContext, scores};
}
export function skillContext(instruction: string, source = ''): SkillContext { return explainSkillContext(instruction, source).context; }

/** Multiple descriptive capability tags from public text, independent of the dataset source. */
export function capabilitiesFor(instruction: string, source = ''): string[] {
  const mapping: Record<string, string> = {algorithm: 'algorithm', framework: 'repository', stateful: 'application',
    performance: 'performance', cli: 'terminal', visual: 'visual'};
  const tags = explainSkillContext(instruction, source).scores.filter(score => score.substantive >= 2).map(score => mapping[score.context]!);
  return [...new Set(tags.length ? tags : ['repository'])];
}
