import test from 'node:test';
import assert from 'node:assert/strict';
import net from 'node:net';
import http from 'node:http';
import {channel} from 'node:diagnostics_channel';
import {Agent, EnvHttpProxyAgent, fetch as undiciFetch} from 'undici';
import {retryPlan, withTransportEvidence} from '../src/retry.js';
import {SDKModel} from '../src/model.js';
import {Budget} from '../src/budget.js';
import {config, fixture} from './helpers.js';
import path from 'node:path';

async function listen(t: any, server: net.Server) {
  const sockets = new Set<net.Socket>();
  server.on('connection', socket => { sockets.add(socket); socket.on('close', () => sockets.delete(socket)); });
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(async () => { for (const socket of sockets) socket.destroy(); await new Promise<void>(resolve => server.close(() => resolve())); });
  return `127.0.0.1:${(server.address() as net.AddressInfo).port}`;
}
async function failure(work: () => Promise<unknown>) {
  try { await work(); } catch (error) { return error; }
  assert.fail('Expected transport failure');
}
const plan = (error: unknown) => retryPlan(error, 0, 1, 1000);

test('actual SDK pre-TLS failure refunds tokens, preserves call charge and allows protected retry', async t => {
  const endpoint = await listen(t, net.createServer(socket => socket.once('data', () => socket.destroy())));
  const f = await fixture(); t.after(f.cleanup);
  process.env.TESTHELIX_TRANSPORT_TEST_KEY = 'local-fixture'; t.after(() => delete process.env.TESTHELIX_TRANSPORT_TEST_KEY);
  const c = config(); Object.assign(c.model, {provider: 'openai-responses', baseURL: `https://${endpoint}/v1`, apiKeyEnv: 'TESTHELIX_TRANSPORT_TEST_KEY'});
  c.scheduler.enabled = false;
  Object.assign(c.budget, {maxInputTokens: 600, maxCalls: 3});
  const budget = await Budget.open(c, path.join(f.root, 'budget.json'), 'transport', false);
  const floor = {input: 200, output: 128, calls: 1};
  const reservation = await budget.reserve(300, 128, floor);
  const error = await failure(() => new SDKModel(c).complete({system: 'Local fixture', messages: [{role: 'user', content: 'test'}],
    tools: {}, maxOutputTokens: 128, signal: AbortSignal.timeout(3000)}));
  assert.equal(plan(error).retry, true);
  assert.equal(plan(error).rejected, true);
  await budget.settle(reservation, plan(error).rejected ? {input: 0, output: 0, cached: 0} : undefined);
  assert.equal(budget.snapshot().calls, 1);
  assert.equal(budget.remainingInputTokens, 600);
  await budget.reserve(300, 128, floor);
  assert.equal(retryPlan(error, 1, 1, 1000).retry, false);
  for (const name of ['undici:request:create', 'undici:client:sendHeaders', 'undici:client:connectError'])
    assert.equal(channel(name).hasSubscribers, false, name);
});

test('proxy tunnel pre-TLS failure is local proof, concurrent sent request remains uncertain', async t => {
  const proxy = http.createServer();
  proxy.on('connect', (_req, socket) => {
    socket.write('HTTP/1.1 200 Connection Established\r\n\r\n');
    socket.once('data', () => socket.destroy());
  });
  const proxyAddress = await listen(t, proxy);
  const dispatcher = new EnvHttpProxyAgent({httpsProxy: `http://${proxyAddress}`, noProxy: ''});
  t.after(() => dispatcher.close());
  let received = false, warmSocket: net.Socket | undefined;
  const target = await listen(t, http.createServer(async (req, res) => {
    if (req.url === '/warm') { warmSocket = req.socket; res.end('ready'); return; }
    for await (const _chunk of req) { /* drain the actual request */ }
    assert.equal(req.socket, warmSocket, 'fixture reused the established connection');
    received = true; req.socket.destroy();
  }));
  const direct = new Agent({connections: 1}); t.after(() => direct.close());
  await (await withTransportEvidence(() => undiciFetch(`http://${target}/warm`, {dispatcher: direct}))).text();
  const [tls, sent] = await Promise.all([
    failure(() => withTransportEvidence(() => fetch('https://fixture.invalid/v1', {dispatcher, method: 'POST', body: 'model request', signal: AbortSignal.timeout(3000)} as RequestInit))),
    failure(() => withTransportEvidence(() => undiciFetch(`http://${target}`, {dispatcher: direct, method: 'POST', body: 'model request', signal: AbortSignal.timeout(3000)}))),
  ]);
  assert.equal(plan(tls).rejected, true);
  assert.equal(plan(tls).retry, true);
  assert.equal(received, true);
  assert.equal(plan(sent).rejected, false);
});

test('redirect after dispatch and gateway TLS error text cannot refund unknown usage', async t => {
  const tls = await listen(t, net.createServer(socket => socket.once('data', () => socket.destroy())));
  const refusedServer = net.createServer();
  const refused = await listen(t, refusedServer);
  await new Promise<void>(resolve => refusedServer.close(() => resolve()));
  const address = await listen(t, http.createServer((req, res) => {
    if (req.url === '/redirect') { res.writeHead(307, {location: `https://${tls}`}); res.end(); }
    else if (req.url === '/refused') { res.writeHead(307, {location: `https://${refused}`}); res.end(); }
    else { res.writeHead(502); res.end('Client network socket disconnected before secure TLS connection was established'); }
  }));
  const redirected = await failure(() => withTransportEvidence(() => fetch(`http://${address}/redirect`, {method: 'POST', body: 'request', signal: AbortSignal.timeout(3000)})));
  assert.equal(plan(redirected).rejected, false);
  const refusedRedirect = await failure(() => withTransportEvidence(() => fetch(`http://${address}/refused`, {method: 'POST', body: 'request', signal: AbortSignal.timeout(3000)})));
  assert.equal(plan(refusedRedirect).rejected, false, 'post-dispatch connection refusal overrides generic pre-connect code');
  const response = await withTransportEvidence(() => fetch(`http://${address}/gateway`));
  const error = Object.assign(new Error(await response.text()), {statusCode: response.status, cause: {code: 'ECONNRESET'}});
  assert.equal(plan(error).rejected, false);
  const synthetic = Object.assign(new Error('Client network socket disconnected before secure TLS connection was established'), {code: 'ECONNRESET'});
  assert.equal(plan(await failure(() => withTransportEvidence(async () => { throw synthetic; }))).rejected, false);
  for (const name of ['undici:request:create', 'undici:client:sendHeaders', 'undici:client:connectError'])
    assert.equal(channel(name).hasSubscribers, false, name);
});
