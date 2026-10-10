import { parsePatch } from 'diff';

const hunkHeader = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;

/** Let the diff library own parsing/application. Repair only redundant line-count
 * metadata when model-authored payloads are otherwise well-formed. File names,
 * positions, context, whitespace and edit payloads are never inferred or changed. */
export function parseAgentPatch(input: string): {patches: ReturnType<typeof parsePatch>; recountedHunks: number} {
  try { return {patches: parsePatch(input), recountedHunks: 0}; }
  catch (originalError) {
    const lines = input.split('\n');
    const fileBoundary = (i: number) => lines[i]?.startsWith('--- ') && lines[i + 1]?.startsWith('+++ ')
      && hunkHeader.test(lines[i + 2] ?? '');
    const boundary = (i: number) => hunkHeader.test(lines[i] ?? '') || fileBoundary(i)
      || /^(?:diff --git |Index: |diff -r )/.test(lines[i] ?? '');
    let recountedHunks = 0;
    for (let i = 0; i < lines.length; i++) {
      const header = hunkHeader.exec(lines[i]!);
      if (!header) continue;
      let oldLines = 0, newLines = 0, end = i + 1, sawPayload = false;
      for (; end < lines.length && !boundary(end); end++) {
        const line = lines[end]!;
        if (line === '' && (end === lines.length - 1 || boundary(end + 1))) break;
        if (line === '\\ No newline at end of file' && sawPayload) continue;
        if (!/^[ +\-]/.test(line)) throw originalError;
        if (line[0] !== '+') oldLines++;
        if (line[0] !== '-') newLines++;
        sawPayload = true;
      }
      if (!sawPayload) throw originalError;
      const declaredOld = Number(header[2] ?? 1), declaredNew = Number(header[4] ?? 1);
      if (declaredOld !== oldLines || declaredNew !== newLines) {
        lines[i] = `@@ -${header[1]},${oldLines} +${header[3]},${newLines} @@${header[5]}`;
        recountedHunks++;
      }
      i = end - 1;
    }
    if (!recountedHunks) throw originalError;
    return {patches: parsePatch(lines.join('\n')), recountedHunks};
  }
}

/** A stale context wrapper can be lowered to edit_file semantics when each hunk
 * expresses one unambiguous, exact line replacement. Never guess insertions,
 * trim whitespace, alter replacement text, or choose between duplicate matches. */
export function applyUniqueEdits(source: string, patch: ReturnType<typeof parsePatch>[number]): string | false {
  if (!patch.hunks.length || patch.hunks.length > 64 || patch.oldFileName === '/dev/null' || patch.newFileName === '/dev/null') return false;
  const edits: Array<{start: number; end: number; replacement: string}> = [];
  for (const hunk of patch.hunks) {
    if (hunk.lines.some(line => line.startsWith('\\'))) return false;
    const first = hunk.lines.findIndex(line => /^[+-]/.test(line));
    const last = hunk.lines.findLastIndex(line => /^[+-]/.test(line));
    if (first < 0) return false;
    const group = hunk.lines.slice(first, last + 1);
    if (group.some(line => !/^[+-]/.test(line))) return false;
    const removed = group.filter(line => line[0] === '-').map(line => line.slice(1));
    const added = group.filter(line => line[0] === '+').map(line => line.slice(1));
    if (!removed.length) return false;
    const old = removed.join('\n') + '\n', replacement = added.length ? added.join('\n') + '\n' : '';
    let start = -1, cursor = 0;
    for (;;) {
      const found = source.indexOf(old, cursor);
      if (found < 0) break;
      if (found === 0 || source[found - 1] === '\n') {
        if (start !== -1) return false;
        start = found;
      }
      cursor = found + 1;
    }
    if (start < 0 || (edits.length && start < edits.at(-1)!.end)) return false;
    edits.push({start, end: start + old.length, replacement});
  }
  let result = source;
  for (const edit of edits.reverse()) result = result.slice(0, edit.start) + edit.replacement + result.slice(edit.end);
  return result;
}
