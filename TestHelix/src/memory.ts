import { clip, hash } from './util.js';
import { z } from 'zod';

export interface MemoryEntry {
  id: string; kind: 'fact' | 'decision' | 'next' | 'requirement'; text: string;
  paths?: string[]; artifacts?: string[];
}
interface RecordEntry extends MemoryEntry { revisions: Record<string, string>; stale: boolean; sequence: number }
/** Session-local evidence memory. Evolution is the only route to cross-task knowledge. */
export class WorkingMemory {
  private records = new Map<string, RecordEntry>();
  private sequence = 0;
  private legacy = '';
  private legacyStale = false;
  setNotes(notes: string): void { this.legacy = notes; this.legacyStale = false; }
  restore(raw: unknown): void {
    const saved = z.object({legacy: z.string().max(3000), records: z.array(z.object({
      id: z.string().max(60), kind: z.enum(['fact', 'decision', 'next', 'requirement']), text: z.string().max(600),
      paths: z.array(z.string()).max(6).optional(), artifacts: z.array(z.string()).max(3).optional(),
      revisions: z.record(z.string(), z.string()), sequence: z.number().int().nonnegative(),
    })).max(16)}).parse(raw);
    this.legacy = saved.legacy; this.legacyStale = true; this.records.clear();
    for (const entry of saved.records) this.records.set(entry.id, {...entry, stale: true});
    this.sequence = Math.max(0, ...saved.records.map(entry => entry.sequence));
  }
  update(entries: MemoryEntry[], revisions: Map<string, string>, remove: string[] = []): void {
    // Validate all entries before mutating any memory.
    const prepared = entries.map(entry => {
      if (entry.kind === 'fact' && !entry.paths?.length && !entry.artifacts?.length)
        throw new Error('A fact requires a previously observed path or saved evidence artifact');
      const versions = Object.fromEntries((entry.paths ?? []).map(file => {
        const revision = revisions.get(file);
        if (!revision) throw new Error(`Read ${file} before recording it as evidence`);
        return [file, revision];
      }));
      return {...entry, revisions: versions, stale: false, sequence: ++this.sequence};
    });
    for (const id of remove) this.records.delete(id);
    for (const entry of prepared) this.records.set(entry.id, entry);
    while (this.records.size > 16) {
      const oldest = [...this.records.values()].sort((a, b) => Number(b.stale) - Number(a.stale) || a.sequence - b.sequence)[0]!;
      this.records.delete(oldest.id);
    }
  }
  invalidate(file?: string): void {
    for (const entry of this.records.values())
      if (!file || entry.paths?.includes(file)) entry.stale = true;
    this.legacyStale = true;
  }
  render(query = '', limit = 3000): string {
    const terms = new Set(query.toLowerCase().match(/[\p{L}\p{N}_]{3,}/gu) ?? []);
    const score = (entry: RecordEntry) => (entry.stale ? -100 : 0) + (entry.kind === 'next' ? 10 : 0)
      + [...terms].filter(term => entry.text.toLowerCase().includes(term)).length;
    const entries = [...this.records.values()].sort((a, b) => score(b) - score(a) || b.sequence - a.sequence);
    let result = '';
    for (const entry of entries) {
      const text = `[${entry.id} ${entry.kind}${entry.stale ? ' STALE: reverify' : ''}] ${entry.text}`
        + (entry.paths?.length ? ` (files: ${entry.paths.join(', ')})` : '')
        + (entry.artifacts?.length ? ` (evidence: ${entry.artifacts.join(', ')})` : '') + '\n';
      if (result.length + text.length <= limit) result += text;
    }
    if (this.legacy && result.length < limit - 100)
      result += `Working notes${this.legacyStale ? ' (may be stale after workspace changes)' : ' (agent claims; verify)'}:\n${clip(this.legacy, limit - result.length - 100)}`;
    return result.trim();
  }
  snapshot(): unknown { return {schemaVersion: 1, records: [...this.records.values()], legacy: this.legacy, legacyStale: this.legacyStale}; }
  get digest(): string { return hash(this.snapshot()); }
}
