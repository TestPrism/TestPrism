import { clip, hash } from './util.js';

export interface FailureEvidence {
  id: string;
  exception: string;
  message: string;
  frames: Array<{path: string; line: number; operation?: string}>;
  needsAttribution: boolean;
}
export interface ExecutionEvidence { failures: FailureEvidence[]; omitted: number; summary: string[] }

/** Extract symptoms, not a verdict. A missing required behavior may raise an exception. */
export function executionEvidence(output: string): ExecutionEvidence {
  const failures = new Map<string, FailureEvidence>(), summary: string[] = [];
  let frames: FailureEvidence['frames'] = [];
  const add = (exception: string, message: string, needsAttribution: boolean) => {
    const own = frames.find(frame => /(?:^|\/)agent-tests\//.test(frame.path));
    const last = frames.at(-1);
    const locations = [...new Set([own, last].filter((frame): frame is NonNullable<typeof frame> => !!frame))];
    const item = {exception, message: clip(message.trim(), 300), frames: locations, needsAttribution};
    const id = hash(item).slice(0, 16);
    failures.set(id, {id, ...item});
  };
  for (const raw of output.split('\n')) {
    const line = raw.replace(/\x1b\[[0-9;]*m/g, '');
    if (/^\s*Traceback \(most recent call last\):/.test(line)) frames = [];
    const frame = /^\s*File ["']([^"']+)["'], line (\d+)/.exec(line);
    if (frame) { frames.push({path: clip(frame[1]!, 180), line: Number(frame[2])}); continue; }
    const exception = /^\s*(?:E\s+)?((?:[A-Za-z_]\w*\.)*(?:[A-Za-z_]\w*(?:Error|Exception)|Error|Exception|error|SystemExit))(?:\s*\[[^\]]+\])?:\s*(.*)$/.exec(line);
    if (exception) {
      add(exception[1]!, exception[2]!, !/(?:^|\.)AssertionError$/.test(exception[1]!));
      frames = [];
    } else if (frames.length && /^\s{4,}\S/.test(line) && !frames.at(-1)!.operation) {
      frames.at(-1)!.operation = clip(line.trim(), 200);
    }
    if (/^\s*(?:Ran \d+ tests?\b|FAILED \(|\d+ failed\b|=+ .*\b(?:failed|error)\b)/i.test(line)) summary.push(clip(line.trim(), 180));
    // A nested CLI can run no work while the outer unittest runner collects tests.
    // Keep this as an attribution request; an empty-input task may intentionally require it.
    if (/\bno (?:files to lint|files (?:were )?processed|tests (?:were )?collected)\b/i.test(line))
      add('NoWorkObserved', line, true);
  }
  const ordered = [...failures.values()].sort((a, b) => Number(b.needsAttribution) - Number(a.needsAttribution));
  return {failures: ordered.slice(0, 6), omitted: Math.max(0, ordered.length - 6), summary: summary.slice(-3)};
}
