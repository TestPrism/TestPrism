import {random} from './genome.js';
import {fitness, type Evaluation, type Sample} from './evolution-metrics.js';

/** Rotate sources deterministically, then sample within each source without replacement. */
export function screeningSamples(samples: Sample[], count: number, seed: string): Sample[] {
  const rng = random(seed), groups = new Map<string, Sample[]>();
  for (const sample of [...samples].sort((a, b) => a.key.localeCompare(b.key)))
    groups.set(sample.source, [...(groups.get(sample.source) ?? []), sample]);
  const shuffle = <T>(items: T[]) => {
    for (let i = items.length - 1; i > 0; i--) {const j = Math.floor(rng() * (i + 1)); [items[i], items[j]] = [items[j]!, items[i]!];}
    return items;
  };
  const buckets = shuffle([...groups.values()].map(shuffle)), result: Sample[] = [];
  while (result.length < Math.min(count, samples.length))
    for (const bucket of buckets) if (bucket.length && result.length < count) result.push(bucket.pop()!);
  return result.sort((a, b) => a.key.localeCompare(b.key));
}

/** Cheap training triage only. Passing never constitutes promotion or replaces full evaluation. */
export function passesScreen(parent: Evaluation, child: Evaluation): boolean {
  if (parent.split !== 'train' || child.split !== 'train' || parent.seed !== child.seed
    || JSON.stringify([...parent.taskKeys].sort()) !== JSON.stringify([...child.taskKeys].sort()))
    throw new Error('Screening requires a paired training batch');
  const a = fitness([parent]), b = fitness([child]);
  return b.strict >= a.strict && b.valid >= a.valid && b.invalid >= a.invalid && b.errors <= a.errors
    && Object.keys(a.bySource).every(source => (b.bySource[source] ?? 0) >= a.bySource[source]!);
}
