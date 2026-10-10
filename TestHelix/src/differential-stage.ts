import {createTwoFilesPatch} from 'diff';
import type {Candidate} from './types.js';
import type {Sandbox} from './runtime.js';
import {comparisonBaseline} from './source-state.js';

/** A bounded view of two hypotheses, never an oracle. Diffing and encoding
 * remain library operations; large/binary/expensive comparisons are explicit. */
export async function hypothesisDifference(first: Candidate, second: Candidate, baseline: Sandbox): Promise<string> {
  const paths = [...new Set([...first.source, ...second.source].map(file => file.path))].sort();
  const parts: string[] = []; let remaining = 8000;
  for (const file of paths.slice(0, 8)) {
    if (remaining < 256) break;
    try {
      const read = async (candidate: Candidate) => {
        const artifact = candidate.source.find(item => item.path === file);
        if (artifact && artifact.data.length > 440000) throw new Error('Source artifact exceeds comparison limit');
        return artifact ? artifact.deleted ? null : {data: Buffer.from(artifact.data, 'base64'), mode: artifact.mode}
          : comparisonBaseline(baseline, file, 160000);
      };
      const firstFile = await read(first), secondFile = await read(second);
      if (!!firstFile !== !!secondFile || firstFile?.mode !== secondFile?.mode) {
        const describe = (value: typeof firstFile) => value ? `present, mode ${value.mode.toString(8)}` : 'absent';
        const state = `File state for ${file}: primary ${describe(firstFile)}; peer ${describe(secondFile)}.`;
        parts.push(state); remaining -= state.length;
      }
      const a = firstFile?.data ?? Buffer.alloc(0), b = secondFile?.data ?? Buffer.alloc(0);
      if (a.equals(b)) continue;
      if (a.length + b.length > 320000 || a.includes(0) || b.includes(0)) {
        parts.push(`Comparison omitted for ${file}: binary or size limit.`); continue;
      }
      const left = a.toString('utf8'), right = b.toString('utf8');
      if (!Buffer.from(left).equals(a) || !Buffer.from(right).equals(b)) {
        parts.push(`Comparison omitted for ${file}: non-UTF-8 content.`); continue;
      }
      const diff = createTwoFilesPatch(`primary/${file}`, `peer/${file}`, left, right, undefined, undefined,
        {context: 3, timeout: 100, maxEditLength: 2000});
      if (!diff) {parts.push(`Comparison omitted for ${file}: diff complexity limit.`); continue;}
      parts.push(diff.slice(0, Math.max(0, remaining)) + (diff.length > remaining ? '\n[Diff truncated]' : ''));
      remaining -= diff.length;
    } catch {
      parts.push(`Comparison unavailable for ${file}; do not infer absent content.`);
    }
  }
  if (paths.length > 8 || remaining < 256) parts.push('Additional differences omitted by the host size limit.');
  return parts.join('\n').slice(0, 8400);
}
