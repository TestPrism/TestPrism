import type { Config, Usage } from './types.js';
import { atomicJSON, id, readJSON } from './util.js';
import pLimit from 'p-limit';

export interface Reservation { id: string; input: number; output: number; calls: number }
export interface BudgetFloor { input: number; output: number; calls: number }
interface State { identity: string; startedAt: number; calls: number; input: number; output: number; cached: number; estimated: boolean;
  pending: Record<string, Reservation>; settled: string[] }
export class BudgetExceeded extends Error { constructor(message: string) { super(message); this.name = 'BudgetExceeded'; } }
export class Budget {
  private state: State;
  private writes: Promise<void> = Promise.resolve();
  private readonly limit;
  private liveReservations = new Set<string>();
  private settlementWaiters = new Set<() => void>();
  private constructor(readonly config: Config, readonly file: string, identity: string) {
    this.limit = pLimit(config.budget.concurrency);
    this.state = {identity, startedAt: Date.now(), calls: 0, input: 0, output: 0, cached: 0, estimated: false, pending: {}, settled: []};
  }
  static async open(config: Config, file: string, identity: string, resume: boolean): Promise<Budget> {
    const budget = new Budget(config, file, identity);
    const state = resume ? await readJSON<State>(file) : undefined;
    if (state) {
      if (state.identity !== identity) throw new Error('Cannot resume a budget for different task/configuration');
      for (const name of ['calls', 'input', 'output', 'cached', 'startedAt'] as const)
        if (!Number.isSafeInteger(state[name]) || state[name] < 0) throw new Error('Invalid budget checkpoint');
      if (!state.pending || typeof state.pending !== 'object' || Array.isArray(state.pending)
        || !Array.isArray(state.settled) || !state.settled.every(id => typeof id === 'string')
        || new Set(state.settled).size !== state.settled.length || typeof state.estimated !== 'boolean'
        || state.cached > state.input) throw new Error('Invalid budget checkpoint');
      const pending = {calls: 0, input: 0, output: 0};
      for (const [key, reservation] of Object.entries(state.pending)) {
        if (!reservation || reservation.id !== key || state.settled.includes(key)
          || ![reservation.calls, reservation.input, reservation.output].every(value => Number.isSafeInteger(value) && value >= 0)
          || reservation.calls < 1) throw new Error('Invalid budget checkpoint');
        pending.calls += reservation.calls; pending.input += reservation.input; pending.output += reservation.output;
      }
      if (pending.calls > state.calls || pending.input > state.input - state.cached || pending.output > state.output)
        throw new Error('Invalid budget checkpoint');
      if (Object.keys(state.pending).length) state.estimated = true;
      budget.state = state;
    }
    await budget.persist();
    return budget;
  }
  get remainingMs(): number { return Math.max(0, this.config.budget.wallMs - (Date.now() - this.state.startedAt)); }
  get remainingCalls(): number { return Math.max(0, this.config.budget.maxCalls - this.state.calls); }
  get remainingInputTokens(): number { return Math.max(0, this.config.budget.maxInputTokens - this.state.input); }
  get remainingOutputTokens(): number { return Math.max(0, this.config.budget.maxOutputTokens - this.state.output); }
  get elapsedMs(): number { return Date.now() - this.state.startedAt; }
  /** An advisory forecast uses settled usage, excluding temporary upper bounds. */
  get expectedRemainingResponses(): number | undefined {
    return this.forecastResponses();
  }
  forecastResponses(protectedInput = 0): number | undefined {
    const pending = Object.values(this.state.pending);
    const calls = this.state.calls - pending.reduce((sum, item) => sum + item.calls, 0);
    const input = this.state.input - pending.reduce((sum, item) => sum + item.input, 0);
    return calls > 0 && input > 0 ? Math.max(0, Math.floor((this.config.budget.maxInputTokens - protectedInput - input) / (input / calls))) : undefined;
  }
  async waitForSettlement(maxWaitMs = this.remainingMs): Promise<boolean> {
    // A resumed, uncertain reservation has no live request to settle. Never wait
    // indefinitely for it or release its conservatively accounted spend.
    if (!this.liveReservations.size || !this.remainingMs || maxWaitMs <= 0) return false;
    return new Promise(resolve => {
      const finish = (settled: boolean) => { clearTimeout(timer); this.settlementWaiters.delete(wake); resolve(settled); };
      const wake = () => finish(true);
      const timer = setTimeout(() => finish(false), Math.min(this.remainingMs, maxWaitMs));
      this.settlementWaiters.add(wake);
    });
  }
  assertAvailable(): void {
    if (!this.remainingMs || !this.remainingCalls) throw new BudgetExceeded('Run model-call or wall-time budget exhausted');
  }
  async slot<T>(fn: () => Promise<T>): Promise<T> {
    return this.limit(() => { this.assertAvailable(); return fn(); });
  }
  async reserve(inputUpperBound: number, requestedOutput: number, floor: BudgetFloor = {input: 0, output: 0, calls: 0}): Promise<Reservation> {
    this.assertAvailable();
    if (!Object.values(floor).every(value => Number.isSafeInteger(value) && value >= 0)) throw new Error('Invalid protected budget');
    if (this.remainingCalls <= floor.calls) throw new BudgetExceeded('Call budget is protected for later work');
    if (inputUpperBound > this.remainingInputTokens - floor.input)
      throw new BudgetExceeded('Insufficient input-token budget after protected work reserve');
    let output = Math.min(requestedOutput, this.remainingOutputTokens - floor.output);
    if (this.config.budget.maxCostUSD !== undefined && this.config.model.outputPricePerMillion > 0) {
      const availableUSD = this.config.budget.maxCostUSD - this.cost(this.state.input + inputUpperBound + floor.input, this.state.output + floor.output);
      output = Math.min(output, Math.floor(availableUSD * 1e6 / this.config.model.outputPricePerMillion));
    }
    if (output < 128) throw new BudgetExceeded('Run output-token budget exhausted');
    return this.reserveWork({input: inputUpperBound, output, calls: 1});
  }
  async reserveWork(work: {input: number; output: number; calls: number}): Promise<Reservation> {
    this.assertAvailable();
    if (!Object.values(work).every(value => Number.isSafeInteger(value) && value >= 0) || work.calls < 1)
      throw new Error('Invalid work reservation');
    if (work.calls > this.remainingCalls) throw new BudgetExceeded('Insufficient call budget for evaluation batch');
    const {input: inputUpperBound, output} = work;
    if (this.state.output + output > this.config.budget.maxOutputTokens) throw new BudgetExceeded('Insufficient output budget for evaluation batch');
    if (this.state.input + inputUpperBound > this.config.budget.maxInputTokens)
      throw new BudgetExceeded('Insufficient input-token budget for this request');
    const cost = this.cost(this.state.input + inputUpperBound, this.state.output + output);
    if (this.config.budget.maxCostUSD !== undefined && cost > this.config.budget.maxCostUSD)
      throw new BudgetExceeded('Run dollar budget exhausted');
    const reservation = {id: id('reservation'), ...work};
    this.state.calls += work.calls;
    this.state.input += inputUpperBound;
    this.state.output += output;
    this.state.pending[reservation.id] = reservation;
    this.liveReservations.add(reservation.id);
    try { await this.persist(); }
    catch (error) {this.releaseLive(reservation.id); throw error;}
    return reservation;
  }
  async settle(reservation: Reservation, usage?: Usage & {calls?: number}): Promise<void> {
    if (this.state.settled.includes(reservation.id)) return;
    const original = this.state.pending[reservation.id];
    if (!original || JSON.stringify(original) !== JSON.stringify(reservation)) throw new Error('Unknown or changed budget reservation');
    if (usage) {
      if (![usage.input, usage.output, usage.cached, usage.calls ?? reservation.calls].every(value => Number.isSafeInteger(value) && value >= 0)
        || usage.cached > usage.input)
        throw new Error('Invalid usage accounting');
      this.state.input += usage.input - reservation.input;
      this.state.output += usage.output - reservation.output;
      this.state.cached += usage.cached;
      this.state.calls += (usage.calls ?? reservation.calls) - reservation.calls;
      this.state.estimated ||= !!usage.estimated;
    } else this.state.estimated = true;
    delete this.state.pending[reservation.id]; this.state.settled.push(reservation.id);
    try { await this.persist(); }
    finally { this.releaseLive(reservation.id); }
  }
  private releaseLive(reservation: string): void {
    this.liveReservations.delete(reservation);
    for (const wake of [...this.settlementWaiters]) wake();
  }
  snapshot(): Usage & {calls: number; costUSD: number | null} {
    const {input, output, cached, calls, estimated} = this.state;
    return {input, output, cached, calls, estimated,
      costUSD: this.config.model.inputPricePerMillion && this.config.model.outputPricePerMillion ? this.cost(input, output) : null};
  }
  private cost(input: number, output: number): number {
    const {inputPricePerMillion, outputPricePerMillion, cachedInputPricePerMillion = inputPricePerMillion} = this.config.model;
    // Only provider-confirmed cache hits receive a discount. Pending/unknown
    // input remains priced as uncached, including during reservation admission.
    const cached = Math.min(input, this.state.cached);
    return ((input - cached) * inputPricePerMillion + cached * cachedInputPricePerMillion + output * outputPricePerMillion) / 1e6;
  }
  private persist(): Promise<void> {
    const snapshot = structuredClone(this.state);
    this.writes = this.writes.then(() => atomicJSON(this.file, snapshot));
    return this.writes;
  }
}
