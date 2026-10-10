import {AsyncLocalStorage} from 'node:async_hooks';
import {subscribe, unsubscribe} from 'node:diagnostics_channel';

// Use local transport events and object identity, never a gateway's error text.
// Track headers across the whole fetch (including redirects): a later failed
// connection cannot prove that an earlier request did not reach the provider.
const transportScope = new AsyncLocalStorage<{created: boolean; sent: boolean}>();
const requests = new WeakMap<object, {created: boolean; sent: boolean}>();
const connectionErrors = new WeakSet<object>();
const undispatchedErrors = new WeakSet<object>();
const dispatchedErrors = new WeakSet<object>();
const onCreate = (message: unknown) => {
  const {request} = message as {request: object};
  const scope = transportScope.getStore();
  if (scope) { scope.created = true; requests.set(request, scope); }
};
const onSend = (message: unknown) => {
  const {request} = message as {request: {method?: string}};
  // A proxy CONNECT establishes a tunnel; it does not send the model request.
  if (request.method === 'CONNECT') return;
  const scope = requests.get(request);
  if (scope) scope.sent = true;
};
const onConnectError = (message: unknown) => {
  const error = (message as {error: unknown}).error;
  if (error instanceof Error) connectionErrors.add(error);
};
const observers = [['undici:request:create', onCreate], ['undici:client:sendHeaders', onSend],
  ['undici:client:connectError', onConnectError]] as const;
let activeTransports = 0;

/** Missing diagnostics fail closed; only a locally observed connection failure qualifies. */
export async function withTransportEvidence<T>(work: () => Promise<T>): Promise<T> {
  if (activeTransports++ === 0) for (const [name, observer] of observers) subscribe(name, observer);
  return transportScope.run({created: false, sent: false}, async () => {
    try { return await work(); }
    catch (error) {
      const scope = transportScope.getStore()!;
      if (scope.created && error instanceof Error) {
        let cause: unknown = error;
        for (let depth = 0; cause instanceof Error && depth < 5; depth++) {
          if (connectionErrors.has(cause)) {
            // The SDK unwraps fetch TypeErrors. Keep proof on a fresh wrapper,
            // not the shared socket error (which may reject other requests).
            const failure = new Error(`${error.message}: ${cause.message}`, {cause: error});
            failure.name = error.name;
            (scope.sent ? dispatchedErrors : undispatchedErrors).add(failure);
            throw failure;
          }
          cause = cause.cause;
        }
      }
      throw error;
    }
    finally {
      if (--activeTransports === 0) for (const [name, observer] of observers) unsubscribe(name, observer);
    }
  });
}

/** Retry only explicitly transient transport failures; parser/config/abort errors need intervention. */
export function retryPlan(error: unknown, attempt: number, maxRetries: number, maxDelayMs: number, now = Date.now()): {retry: boolean; delayMs: number; status?: number; rejected: boolean} {
  const e = error as {statusCode?: number; responseHeaders?: Record<string, string>; name?: string; code?: string; cause?: unknown; requestSent?: boolean};
  const status = e?.statusCode;
  let network = false, notConnected = false, dispatched = false, cause: unknown = error;
  for (let depth = 0; cause && depth < 5; depth++) {
    const c = cause as {code?: string; cause?: unknown};
    network ||= ['ECONNRESET', 'ECONNREFUSED', 'EPIPE', 'EAI_AGAIN', 'UND_ERR_SOCKET', 'UND_ERR_CONNECT_TIMEOUT'].includes(c.code ?? '');
    // DNS/TCP connection failures occur before an HTTP body can reach the model.
    // Socket resets, response timeouts and 5xx remain uncertain and retain their reservation.
    notConnected ||= ['ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'UND_ERR_CONNECT_TIMEOUT'].includes(c.code ?? '');
    notConnected ||= typeof cause === 'object' && undispatchedErrors.has(cause);
    dispatched ||= typeof cause === 'object' && dispatchedErrors.has(cause);
    cause = c.cause;
  }
  const rejected = e?.requestSent === false || (!status && notConnected && !dispatched)
    || !!status && [400, 401, 403, 404, 405, 409, 413, 415, 422, 429].includes(status);
  const result = {retry: false, delayMs: 0, status, rejected};
  if (attempt >= maxRetries || /Abort|Timeout|Invalid|Validation|JSON|Unsupported|BudgetExceeded/.test(e?.name ?? '')) return result;
  if (!(status === 429 || (status !== undefined && status >= 500 && status <= 599) || (!status && network))) return result;
  const headers = Object.fromEntries(Object.entries(e.responseHeaders ?? {}).map(([key, value]) => [key.toLowerCase(), value]));
  let delay = Math.min(1000 * 2 ** attempt, maxDelayMs);
  if (headers['retry-after-ms']) delay = Number(headers['retry-after-ms']);
  else if (headers['retry-after']) {
    const seconds = Number(headers['retry-after']);
    delay = Number.isFinite(seconds) ? seconds * 1000 : Date.parse(headers['retry-after']) - now;
  }
  if (!Number.isFinite(delay) || delay < 0) delay = Math.min(1000 * 2 ** attempt, maxDelayMs);
  if (delay > maxDelayMs) return result; // Never retry earlier than the provider requests.
  return {...result, retry: true, delayMs: delay};
}
