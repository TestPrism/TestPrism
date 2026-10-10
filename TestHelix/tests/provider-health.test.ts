import test from 'node:test';
import assert from 'node:assert/strict';
import {promises as fs} from 'node:fs';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {healthyFetch,providerAdmission} from '../src/provider-health.js';
import {retryPlan} from '../src/retry.js';
import {config,fixture} from './helpers.js';
import {completion} from './helpers.js';
import {probeProvider} from '../src/provider-probe.js';
import {runTask} from '../src/pipeline.js';

async function setup(t:any) {
  const f=await fixture();t.after(f.cleanup);const c=config();
  Object.assign(c.scheduler,{enabled:true,directory:path.join(f.root,'health'),providerCooldownMs:50,providerUnavailableMs:150});
  return {f,c};
}
test('shared Retry-After blocks other roles before dispatch and queued cancellation refunds tokens',async t=>{
  const {c}=await setup(t);
  await healthyFetch(c,false,AbortSignal.timeout(2000),async()=>new Response('limited',{status:429,headers:{'retry-after-ms':'200'}}));
  let sent=false;
  await assert.rejects(healthyFetch(c,true,AbortSignal.timeout(30),async()=>{sent=true;return new Response('ok');}),e=>retryPlan(e,0,1,100).rejected);
  assert.equal(sent,false);
  const start=Date.now();
  await healthyFetch(c,true,AbortSignal.timeout(2000),async()=>new Response('ok'));
  assert.ok(Date.now()-start>=100);
});
test('an initial transport failure opens the shared circuit and recovery cannot erase a newer failure', async t => {
  const {c} = await setup(t); c.scheduler.providerCooldownMs = 250;
  await assert.rejects(healthyFetch(c, false, AbortSignal.timeout(2000), async () => {
    throw Object.assign(new Error('private TLS diagnostic'), {code: 'ECONNRESET'});
  }), /private TLS/);
  let sent = false;
  await assert.rejects(healthyFetch(c, true, AbortSignal.timeout(40), async () => {
    sent = true; return new Response('ok');
  }), error => retryPlan(error, 0, 1, 100).rejected);
  assert.equal(sent, false);
  const file = (await fs.readdir(c.scheduler.directory)).find(file => file.endsWith('.json'))!;
  const recorded = await fs.readFile(path.join(c.scheduler.directory, file), 'utf8');
  assert.doesNotMatch(recorded, /private TLS/);
  assert.equal(JSON.parse(recorded).rate.strikes, 1);
  await healthyFetch(c, false, AbortSignal.timeout(2000), async () => new Response('ok'));
  assert.equal(JSON.parse(await fs.readFile(path.join(c.scheduler.directory, file), 'utf8')).rate, undefined);
});
test('unavailable model pauses other roles, permits one recovery probe, and does not store raw errors',async t=>{
  const {c}=await setup(t);
  await healthyFetch(c,true,AbortSignal.timeout(2000),async()=>new Response('model fixture is not supported: private-task-literal',{status:400}));
  await assert.rejects(providerAdmission(c,false,AbortSignal.timeout(1000)),/admission paused/);
  const files=await fs.readdir(c.scheduler.directory);
  assert.doesNotMatch(await fs.readFile(path.join(c.scheduler.directory,files.find(f=>f.endsWith('.json'))!),'utf8'),/private-task-literal/);
  await delay(170);
  let active=0,peak=0,first=true;
  await Promise.all(Array.from({length:3},()=>healthyFetch(c,false,AbortSignal.timeout(3000),async()=>{
    active++;peak=Math.max(active,peak);
    if(first){first=false;await delay(120);assert.equal(active,1);}
    active--;return new Response('ok');
  })));
  assert.ok(peak>=1);
});
test('a stale successful in-flight response cannot clear a newer rate-limit event even in the same millisecond',async t=>{
  const {c}=await setup(t);let entered!:()=>void,release!:()=>void;
  const now=Date.now();t.mock.method(Date,'now',()=>now);
  const started=new Promise<void>(r=>entered=r),finished=new Promise<void>(r=>release=r);
  const old=healthyFetch(c,false,AbortSignal.timeout(3000),async()=>{entered();await finished;return new Response('ok');});
  await started;
  await healthyFetch(c,false,AbortSignal.timeout(2000),async()=>new Response('',{status:429,headers:{'retry-after':'30'}}));
  release();await old;
  await assert.rejects(providerAdmission(c,false,AbortSignal.timeout(50)),e=>(e as any).requestSent===false);
});
test('reasoning-effort rejection is scoped to that role rather than banning the execution model',async t=>{
  const {c}=await setup(t);
  await healthyFetch(c,true,AbortSignal.timeout(2000),async()=>new Response('reasoning effort xhigh is not supported',{status:400}));
  await assert.rejects(providerAdmission(c,true,AbortSignal.timeout(1000)),/paused/);
  await providerAdmission(c,false,AbortSignal.timeout(1000));
});
test('provider task admission failure records infrastructure evidence before opening any sandbox and spends no calls',async t=>{
  const {f,c}=await setup(t);c.scheduler.providerUnavailableMs=10000;
  c.model.apiKeyEnv='TESTHELIX_ADMISSION_TEST_KEY';process.env.TESTHELIX_ADMISSION_TEST_KEY='fixture';t.after(()=>delete process.env.TESTHELIX_ADMISSION_TEST_KEY);
  await healthyFetch(c,false,AbortSignal.timeout(2000),async()=>new Response('model fixture is not supported',{status:400}));
  await assert.rejects(runTask(f.task,c,f.out,{factory:{fork:async()=>assert.fail('must not start sandbox'),publish:async()=>{},close:async()=>{}}}),/paused/);
  const events=await fs.readFile(path.join(f.out,'events.jsonl'),'utf8');
  assert.match(events,/run.admission_failed/);assert.match(events,/provider-unavailable/);
  const budget=JSON.parse(await fs.readFile(path.join(f.out,'budget.json'),'utf8'));
  assert.deepEqual([budget.calls,budget.input,budget.output],[0,0,0]);
});
test('provider probe checks actual tool results for each role and accounts its separate budget',async t=>{
  const {f,c}=await setup(t); c.planner.enabled=true;
  const result=await probeProvider(c,path.join(f.root,'probe'),()=>({complete:async args=>{
    const nonce=String(args.messages[0]!.content).slice('nonce: '.length);
    return completion([['report_probe',{nonce}]]);
  }}));
  assert.equal(result.ready,true);assert.equal(result.usage.calls,2);assert.equal(result.usage.output,180);
  const rejected=await probeProvider(c,path.join(f.root,'rejected'),()=>({complete:async()=>{throw Object.assign(new Error('rate limit'),{statusCode:429});}}));
  assert.equal(rejected.ready,false);assert.equal(rejected.usage.calls,1);assert.equal(rejected.usage.output,0);
});
