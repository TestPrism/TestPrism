import { z } from 'zod';
import { capabilityTags, phraseMatches, words } from './skill-context.js';
import {hash} from './util.js';

export const lessonComponents = ['planning', 'preparation', 'generation', 'repair', 'review'] as const;
export type LessonComponent = typeof lessonComponents[number];

export const lessonSchema = z.strictObject({id: z.string().min(1).max(80),
  triggers: z.array(z.string().min(2).max(80)).min(1).max(8)
    .describe('Short literal words or phrases matched against public instructions and the execution checkpoint, not full applicability sentences.'), text: z.string().min(1).max(1000),
  component: z.enum(lessonComponents).optional(), capabilities: z.array(z.enum(capabilityTags)).max(7).optional(),
  version: z.string().regex(/^[a-f0-9]{64}$/).optional()});
export type Lesson = z.infer<typeof lessonSchema>;
export function lessonDigest(lesson: Omit<Lesson, 'version'>): string {
  return hash({id: lesson.id, triggers: lesson.triggers, text: lesson.text,
    component: lesson.component ?? 'generation', capabilities: lesson.capabilities ?? []});
}
export const lessonLibrarySchema = z.array(lessonSchema).max(20).superRefine((lessons, context) => {
  for (const component of lessonComponents)
    if (lessons.filter(lesson => lesson.component === component).length > 4)
      context.addIssue({code:'custom', message:`At most four methods per component: ${component}`});
  if (lessons.filter(lesson => !lesson.component).length > 6)
    context.addIssue({code:'custom', message:'At most six legacy methods; add component ownership for new methods'});
  for (const lesson of lessons) if (lesson.version) {
    const {version, ...content} = lesson;
    if (lessonDigest(content) !== version) context.addIssue({code:'custom', message:'Lesson version digest does not match its content'});
  }
});
export const lessonUpdateSchema = z.discriminatedUnion('operation', [
  z.strictObject({operation: z.literal('upsert'), id: z.string().min(1).max(80), triggers: lessonSchema.shape.triggers,
    component: z.enum(lessonComponents).optional(), capabilities: lessonSchema.shape.capabilities,
    when: z.string().min(1).max(200), action: z.string().min(1).max(400), verify: z.string().min(1).max(250),
    rationale: z.string().min(1).max(2000)}),
  z.strictObject({operation: z.literal('remove'), id: z.string().min(1).max(80), rationale: z.string().min(1).max(2000)}),
]);
export function updateLesson(before: readonly Lesson[], raw: unknown): {lessons: Lesson[]; rationale: string} {
  const update = lessonUpdateSchema.parse(raw);
  const lessons = before.filter(lesson => lesson.id !== update.id);
  if (update.operation === 'upsert') {
    const item = {id: update.id, triggers: update.triggers, component: update.component ?? before.find(x => x.id === update.id)?.component ?? 'generation',
      capabilities: update.capabilities ?? before.find(x => x.id === update.id)?.capabilities ?? [], text: `When: ${update.when}\nAction: ${update.action}\nVerify: ${update.verify}`};
    lessons.push({...item, version: lessonDigest(item)});
  }
  lessonLibrarySchema.parse(lessons); validateLessonDelta(before, lessons);
  return {lessons, rationale: update.rationale};
}
const generic = new Set(['bug', 'fix', 'implement', 'function', 'test', 'tests', 'code', 'task']);
const terms = (lesson: Lesson) => [...new Set(lesson.triggers.map(trigger => words(trigger).join(' ')))]
  .filter(trigger => trigger.length >= 2 && !trigger.split(' ').every(word => generic.has(word)));
export function retrieveLessons(lessons: readonly Lesson[], surface: string, maxChars = 1600,
  scope?: {component: LessonComponent; capabilities: string[]}): Lesson[] {
  const eligible = lessons.filter(lesson => !scope || ((lesson.component ? lesson.component === scope.component : !['planning', 'review'].includes(scope.component))
    && (!lesson.capabilities?.length || lesson.capabilities.some(tag => scope.capabilities.includes(tag)))));
  const ranked = eligible.map(lesson => ({lesson, score: terms(lesson).filter(trigger => phraseMatches(surface, trigger))
    .reduce((sum, trigger) => sum + Math.log(1 + lessons.length / lessons.filter(other => terms(other)
      .some(term => words(term).join(' ') === words(trigger).join(' '))).length), 0)}))
    .filter(item => item.score > 0).sort((a, b) => b.score - a.score || a.lesson.text.length - b.lesson.text.length || a.lesson.id.localeCompare(b.lesson.id));
  const selected: Lesson[] = []; let size = 0;
  for (const {lesson} of ranked) {
    if (size + lesson.text.length > maxChars || selected.some(other => overlap(other.text, lesson.text) > .75)) continue;
    selected.push(lesson); size += lesson.text.length;
    if (selected.length === 2) break;
  }
  return selected;
}
function overlap(a: string, b: string): number {
  const left = new Set(words(a)), right = new Set(words(b));
  return [...left].filter(word => right.has(word)).length / Math.max(1, Math.min(left.size, right.size));
}
export function validateLessonDelta(before: readonly Lesson[], after: readonly Lesson[]): void {
  if (new Set(after.map(lesson => lesson.id)).size !== after.length) throw new Error('Duplicate lesson IDs');
  const all = new Set([...before, ...after].map(lesson => lesson.id));
  const changes = [...all].filter(id => JSON.stringify(before.find(lesson => lesson.id === id)) !== JSON.stringify(after.find(lesson => lesson.id === id)));
  if (changes.length !== 1) throw new Error('Change one lesson at a time; preserve unrelated experience');
  const updated = after.find(lesson => lesson.id === changes[0]);
  if (updated && !terms(updated).length) throw new Error('Lesson needs specific applicability triggers, not generic bug/fix/function tags');
  if (updated && after.some(other => other.id !== updated.id && overlap(other.text, updated.text) > .85))
    throw new Error('Lesson duplicates existing guidance; refine the existing item');
}
