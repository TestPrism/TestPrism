import type {Budget, BudgetFloor} from './budget.js';
import type {Config} from './types.js';

const empty = (): BudgetFloor => ({calls: 0, input: 0, output: 0});
export const addFloor = (a: BudgetFloor, b: BudgetFloor): BudgetFloor =>
  ({calls: a.calls + b.calls, input: a.input + b.input, output: a.output + b.output});

/** Search policy only. The existing Budget owns atomic admission and settlement.
 * Unstarted optional challengers never hold resources needed by the main solve.
 */
export class SearchBudget {
  constructor(private readonly config: Config, private readonly budget: Budget) {}

  review(branches = 1): BudgetFloor {
    const {config, budget} = this;
    const calls = config.search.review ? Math.min(branches * 2, Math.floor(budget.remainingCalls / 3)) : 0;
    return calls ? {calls,
      input: Math.min(Math.floor(budget.remainingInputTokens * .3), config.context.maxTokens * 2),
      output: Math.min(calls * config.model.reviewMaxOutputTokens, Math.floor(budget.remainingOutputTokens / 3)),
    } : empty();
  }

  correction(): BudgetFloor {
    const {config, budget} = this;
    return config.search.maxRounds > 1 ? {
      calls: Math.min(config.search.repairSteps, Math.floor(budget.remainingCalls / 2)),
      input: Math.min(config.context.maxTokens, Math.floor(budget.remainingInputTokens / 3)),
      output: Math.min(config.model.maxOutputTokens, Math.floor(budget.remainingOutputTokens / 3)),
    } : empty();
  }

  wave(remaining: number, size: number, maxTurns: number, tail: BudgetFloor) {
    const {budget} = this, later = remaining - size, floor = {...tail};
    for (const [key, available] of [['calls', budget.remainingCalls], ['input', budget.remainingInputTokens],
      ['output', budget.remainingOutputTokens]] as const)
      floor[key] += Math.floor(Math.max(0, available - tail[key]) * later / remaining);
    return {floor, turns: Math.max(0, Math.min(maxTurns, Math.floor((budget.remainingCalls - floor.calls) / size))),
      // The last solver also needs time to seal artifacts and run independent
      // verification. Sharing calls alone cannot protect that work from a slow
      // final response consuming the entire task deadline.
      deadline: Date.now() + Math.floor(budget.remainingMs * .8 * size / remaining)};
  }

  /** Admit the whole configured attempt plus review and recovery, or skip it.
   * Token estimates are conservative admission hints, not guaranteed completion.
   */
  canChallenge(turns: number): boolean {
    const {config, budget} = this, tail = addFloor(this.review(), this.correction());
    return budget.remainingCalls >= turns + tail.calls
      && budget.remainingInputTokens >= tail.input + config.context.maxTokens * 3
      && budget.remainingOutputTokens >= tail.output + config.model.maxOutputTokens
      && budget.remainingMs >= 30000;
  }
}
