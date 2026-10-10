import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import {command} from '../src/util.js';
import {config} from './helpers.js';

test('SDK requests retain opt-in proxy routing after Pi imports, honor NO_PROXY and cancel through the proxy', async t => {
  const sockets = new Set<net.Socket>(), tunnels: string[] = [], paths: string[] = [];
  const target = http.createServer(async (request, response) => {
    for await (const _ of request) { /* consume request before responding */ }
    paths.push(request.url!);
    if (request.url?.startsWith('/blocked/')) return;
    response.setHeader('content-type', 'application/json');
    response.end(JSON.stringify({id: 'resp-proxy', created_at: 1, model: 'gpt-5.4', status: 'completed',
      output: [{type: 'message', id: 'msg-proxy', role: 'assistant', status: 'completed',
        content: [{type: 'output_text', text: 'OK', annotations: []}]}],
      usage: {input_tokens: 10, output_tokens: 2}}));
  });
  const proxy = http.createServer((_request, response) => {response.writeHead(500).end();});
  for (const server of [target, proxy]) {
    server.on('connection', socket => {sockets.add(socket); socket.on('close', () => sockets.delete(socket));});
    await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
    t.after(() => new Promise<void>(resolve => {for (const socket of sockets) socket.destroy(); server.close(() => resolve());}));
  }
  const targetPort = (target.address() as net.AddressInfo).port;
  proxy.on('connect', (request, downstream, head) => {
    tunnels.push(request.url!);
    const upstream = net.connect(targetPort, '127.0.0.1', () => {
      downstream.write('HTTP/1.1 200 Connection Established\r\n\r\n');
      if (head.length) upstream.write(head);
      downstream.pipe(upstream); upstream.pipe(downstream);
    });
    sockets.add(upstream); upstream.on('close', () => {sockets.delete(upstream); downstream.destroy();});
    downstream.on('close', () => upstream.destroy());
    upstream.on('error', () => downstream.destroy()); downstream.on('error', () => upstream.destroy());
  });
  const settings = config(); Object.assign(settings.model, {provider: 'openai-responses', name: 'gpt-5.4',
    baseURL: 'http://model.proxy.invalid', apiKeyEnv: 'TESTHELIX_PROXY_TEST_KEY'});
  const program = `import assert from 'node:assert/strict';
    await import(${JSON.stringify(new URL('../src/pi-runtime.ts', import.meta.url).href)});
    const {SDKModel} = await import(${JSON.stringify(new URL('../src/model.ts', import.meta.url).href)});
    const config = ${JSON.stringify(settings)};
    const args = {system:'Return OK',messages:[{role:'user',content:'OK'}],tools:{},maxOutputTokens:64};
    const proxied = new SDKModel(config);
    assert.equal((await proxied.complete({...args,signal:AbortSignal.timeout(3000)})).text,'OK');
    config.model.baseURL = 'http://127.0.0.1:${targetPort}';
    assert.equal((await new SDKModel(config).complete({...args,signal:AbortSignal.timeout(3000)})).text,'OK');
    config.model.baseURL = 'http://model.proxy.invalid/blocked';
    await assert.rejects(new SDKModel(config).complete({...args,signal:AbortSignal.timeout(500)}));
    console.log('proxy, bypass and cancellation passed');`;
  const proxyURL = `http://127.0.0.1:${(proxy.address() as net.AddressInfo).port}`;
  const result = await command([process.execPath, '--import', 'tsx', '--input-type=module', '-e', program], {
    timeoutMs: 15000, maxBytes: 3000, env: {...process.env, NODE_USE_ENV_PROXY: '1', TESTHELIX_PROXY_TEST_KEY: 'fixture',
      HTTP_PROXY: proxyURL, HTTPS_PROXY: proxyURL, http_proxy: proxyURL, https_proxy: proxyURL,
      NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost'},
  });
  assert.equal(result.code, 0, result.stdout + result.stderr);
  assert.ok(tunnels.length >= 1); assert.ok(tunnels.every(authority => authority === 'model.proxy.invalid:80'));
  assert.deepEqual(paths, ['/responses', '/responses', '/blocked/responses']);
});
