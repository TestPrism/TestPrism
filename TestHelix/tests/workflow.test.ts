import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import net from 'node:net';
import {spawn} from 'node:child_process';
import {setTimeout as delay} from 'node:timers/promises';
import {stringify} from 'smol-toml';
import {loadConfig} from '../src/config.js';
import {configSchema, type Model} from '../src/types.js';
import {withAPISlot} from '../src/api-scheduler.js';
import {nativeRequest, NativeFactory} from '../src/native-runtime.js';
import {planTask, planBrief, type TaskPlan} from '../src/planner.js';
import {LocalSandbox} from '../src/runtime.js';
import {AgentSession} from '../src/agent.js';
import {Budget} from '../src/budget.js';
import {Store} from '../src/store.js';
import {hash} from '../src/util.js';
import {command} from '../src/util.js';
import {fileURLToPath} from 'node:url';
import http from 'node:http';
import {SDKModel} from '../src/model.js';
import {failureLabels} from '../src/component-feedback.js';
import {screeningSamples} from '../src/evolution-screen.js';
import {retrieveLessons, updateLesson} from '../src/lessons.js';
import {config, completion, fixture, repairs, suites} from './helpers.js';

test('shell commands support Bash syntax and preserve a failed pipeline status', async t => {
  const f = await fixture(); t.after(f.cleanup); const sandbox = new LocalSandbox(f.workspace, false);
  const result = await sandbox.exec('set -euo pipefail; values=(observed); printf "%s" "${values[0]}"; false | cat', 3000);
  assert.equal(result.stdout, 'observed'); assert.equal(result.code, 1); assert.equal(result.timedOut, false);
});

test('a passing runner checkpoint can retrieve a generation lesson before artifact submission', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.search.lessons = [{id:'close-observed-run',component:'generation',capabilities:['repository'],
    triggers:['code 0'],text:'OBSERVED_COMPLETION_METHOD: seal the existing artifacts.'}];
  const working = path.join(f.root,'working'); await fs.cp(f.workspace,working,{recursive:true});
  const store = new Store(f.out); await store.init();
  const budget = await Budget.open(settings,path.join(f.out,'budget.json'),'checkpoint-retrieval',false);
  let calls = 0;
  const model: Model = {complete:async args => {
    if (++calls === 1) return completion([
      ['write_file',{path:'prime.cjs',content:repairs[0]}],
      ['write_test_suite',{files:[{path:'agent-tests/run-tests.sh',content:'node agent-tests/check.cjs\n'},
        {path:'agent-tests/check.cjs',content:suites[0]}]}],
    ]);
    assert.match(JSON.stringify(args.messages),/OBSERVED_COMPLETION_METHOD/);
    return completion([['submit_candidate',{requirements:['primality'],rationale:'Public regression and observed repair control'}]]);
  }};
  const session = new AgentSession({id:'candidate-1',role:'candidate',strategy:'fixture',task:f.task,config:settings,model,
    sandbox:new LocalSandbox(working,false),baseline:new LocalSandbox(f.workspace,false),store,budget});
  await session.run(3); assert.ok(session.candidate); assert.equal(calls,2);
});

test('a write-and-run classified failure actually retrieves a generation lesson before repair and submission', async t => {
  const f = await fixture(); t.after(f.cleanup); const settings = config();
  settings.search.skills = ['artifact-workflow', 'failure-triage'];
  settings.search.lessons = updateLesson([], {operation: 'upsert', id: 'attribute-observed-failure', component: 'generation',
    triggers: ['code 1'], when: 'The runner reports an unresolved failure',
    action: 'ATTRIBUTION_METHOD: check public expectations before changing source or assertions.',
    verify: 'The same suite is rerun after correction.', rationale: 'Execution failure alone is not oracle evidence.'}).lessons;
  const working = path.join(f.root, 'working'); await fs.cp(f.workspace, working, {recursive: true});
  const store = new Store(f.out); await store.init();
  const budget = await Budget.open(settings, path.join(f.out, 'budget.json'), 'failure-method', false);
  let calls = 0;
  const model: Model = {complete: async args => {
    if (++calls === 1) return completion([['write_test_suite', {files: [
      {path: 'agent-tests/run-tests.sh', content: 'node agent-tests/check.cjs\n'},
      {path: 'agent-tests/check.cjs', content: suites[0]},
    ]}]]);
    assert.match(JSON.stringify(args.messages), /ATTRIBUTION_METHOD/);
    assert.match(JSON.stringify(args.messages), /correctness is unverified/);
    assert.match(JSON.stringify(args.messages), /Preloaded failure-triage/);
    return completion([
      ['write_file', {path: 'prime.cjs', content: repairs[0]}],
      ['exec', {command: 'bash agent-tests/run-tests.sh'}],
      ['submit_candidate', {requirements: ['primality'], rationale: 'Observed baseline failure and passing local repair'}],
    ]);
  }};
  const session = new AgentSession({id: 'candidate-1', role: 'candidate', strategy: 'fixture', task: f.task, config: settings, model,
    sandbox: new LocalSandbox(working, false), baseline: new LocalSandbox(f.workspace, false), store, budget});
  await session.run(3); assert.ok(session.candidate); assert.equal(calls, 2);
  const events = (await fs.readFile(path.join(f.out, 'events.jsonl'), 'utf8')).trim().split('\n').map(JSON.parse);
  const exposure = events.filter(event => event.type === 'skills.component_methods');
  assert.equal(exposure.length, 1);
  assert.equal(exposure[0].data.component, 'generation');
  assert.deepEqual(exposure[0].data.methods, settings.search.lessons.map(({id, version}) => ({id, version})));
  const active = events.filter(event => event.type === 'skills.active').at(-1).data;
  assert.equal(active.phase, 'tests'); assert.ok(active.requested.includes('failure-triage'));
  assert.deepEqual(active.omitted, []);
});

function workflow() {
  const c = config();
  Object.assign(c.model, {name:'gpt-5.4', provider:'openai-responses', providerOptions:{openai:{reasoningEffort:'medium'}}});
  c.planner.enabled = true;
  return configSchema.parse(c);
}

test('JSON and TOML share defaults, relative scheduler path and role constraints', async t => {
  const f = await fixture(); t.after(f.cleanup); const value = workflow();
  await fs.writeFile(path.join(f.root,'config.json'), JSON.stringify(value));
  await fs.writeFile(path.join(f.root,'config.toml'), stringify(value));
  const json = await loadConfig(path.join(f.root,'config.json'));
  assert.deepEqual(await loadConfig(path.join(f.root,'config.toml')), json);
  assert.ok(path.isAbsolute(json.scheduler.directory));
  assert.equal(configSchema.parse({...value, model:{...value.model, name:'gpt-5.5'}}).model.name,'gpt-5.5');
  assert.throws(() => configSchema.parse({...value, model:{...value.model, name:'unsupported-model'}}));
  assert.throws(() => configSchema.parse({...value, model:{...value.model, providerOptions:{openai:{reasoningEffort:'xhigh'}}}}));
});

test('planner preserves original contract, accounts every revision and caps at two across resumes', async t => {
  const f = await fixture(); t.after(f.cleanup); const c = workflow(), store = new Store(f.out); await store.init();
  const budget = await Budget.open(c, path.join(f.out,'budget.json'), hash(c), false);
  const plan: TaskPlan = {capabilities:['algorithm'],requirements:[{requirement:'Nonnegative integer primality',quote:f.task.instruction}],
    preparation:['Probe existing Node runner'],branches:[{focus:'Boundary cases',verification:'Independent divisibility oracle'}],unknowns:[],completion:'Reproducible contract cases'};
  let calls = 0;
  const model: Model = {complete: async args => {calls++; assert.equal(args.forceTool,'submit_plan');
    assert.match(JSON.stringify(args.messages), /1\| Fix isPrime/);
    assert.doesNotMatch(JSON.stringify(args.tools.submit_plan!.schema), /"quote"/);
    return completion([['submit_plan', {...plan, requirements: [{requirement: plan.requirements[0]!.requirement, taskLines: {startLine: 1}}]}]]);}};
  const sandbox = new LocalSandbox(f.workspace, false);
  assert.deepEqual(await planTask(f.task,c,sandbox,budget,store,model), plan);
  await planTask(f.task,c,sandbox,budget,store,model); assert.equal(calls,1);
  await planTask(f.task,c,sandbox,budget,store,model,'Observed runner uses a different interface.');
  await planTask(f.task,c,sandbox,budget,store,model,'Another request cannot bypass the cap.');
  assert.equal(calls,2); assert.equal(budget.snapshot().calls,2); assert.equal(budget.snapshot().output,180);
  assert.doesNotMatch(planBrief(plan, 0), /Reproducible contract cases|1 is not prime/);
  assert.match(planBrief(plan, 0), /Boundary cases/);
});

test('planner rejects invalid task ranges without inventing citations and charges returned responses', async t => {
  for (const taskLines of [{startLine: 0}, {startLine: 2, endLine: 1}, {startLine: 99}, {startLine: 2}]) {
    const f = await fixture(); t.after(f.cleanup); const c = workflow(), store = new Store(f.out); await store.init();
    f.task.instruction += '\n';
    const budget = await Budget.open(c, path.join(f.out, 'budget.json'), hash(c), false);
    const model: Model = {complete: async () => completion([['submit_plan', {capabilities: ['algorithm'],
      requirements: [{requirement: 'Public behavior', taskLines}], preparation: [],
      branches: [{focus: 'Complete contract', verification: 'Independent oracle'}], unknowns: [], completion: 'done'}]])};
    assert.equal(await planTask(f.task, c, new LocalSandbox(f.workspace, false), budget, store, model), undefined);
    assert.equal(budget.snapshot().calls, 1); assert.equal(budget.snapshot().output, 90);
    assert.equal(JSON.parse(await fs.readFile(path.join(f.out, 'planner.json'), 'utf8')).plan, undefined);
  }
});

test('planner rejects fabricated quotes and falls back without consuming execution reserve', async t => {
  const f=await fixture(); t.after(f.cleanup); const c=workflow(), store=new Store(f.out); await store.init();
  const budget=await Budget.open(c,path.join(f.out,'budget.json'),hash(c),false);
  const model: Model={complete:async()=>completion([['submit_plan',{capabilities:['algorithm'],requirements:[{requirement:'fabricated',quote:'Negative values must throw'}],
    preparation:[],branches:[{focus:'Wrong assumption',verification:'none'}],unknowns:[],completion:'done'}]])};
  assert.equal(await planTask(f.task,c,new LocalSandbox(f.workspace,false),budget,store,model),undefined);
  assert.equal(budget.snapshot().calls,1); assert.ok(budget.remainingCalls>=8);
});

test('confirmed planner API rejection counts its call but refunds unsent inference tokens', async t => {
  const f=await fixture(); t.after(f.cleanup); const c=workflow(), store=new Store(f.out); await store.init();
  const budget=await Budget.open(c,path.join(f.out,'budget.json'),hash(c),false);
  const model: Model={complete:async()=>{throw Object.assign(new Error('rate limited'),{statusCode:429});}};
  assert.equal(await planTask(f.task,c,new LocalSandbox(f.workspace,false),budget,store,model),undefined);
  assert.deepEqual([budget.snapshot().calls,budget.snapshot().input,budget.snapshot().output],[1,0,0]);
});

test('SDK wire requests pin execution to medium and planner to xhigh, with Responses storage disabled', async t => {
  const bodies: any[]=[];
  const server=http.createServer(async (req,res)=>{
    let raw=''; for await(const chunk of req) raw+=chunk;
    bodies.push(JSON.parse(raw)); assert.equal(req.url,'/responses');
    res.setHeader('content-type','application/json');
    res.end(JSON.stringify({id:'resp-fixture',created_at:1,model:'gpt-5.4',status:'completed',output:[{type:'message',id:'msg-fixture',role:'assistant',status:'completed',
      content:[{type:'output_text',text:'OK',annotations:[]}]}],usage:{input_tokens:10,output_tokens:2,input_tokens_details:{cached_tokens:0},output_tokens_details:{reasoning_tokens:0}}}));
  });
  await new Promise<void>(r=>server.listen(0,'127.0.0.1',r)); t.after(()=>new Promise<void>(r=>server.close(()=>r())));
  const c=workflow(); c.model.baseURL=`http://127.0.0.1:${(server.address() as net.AddressInfo).port}`; c.model.apiKeyEnv='TESTHELIX_WORKFLOW_TEST_KEY';
  process.env.TESTHELIX_WORKFLOW_TEST_KEY='fixture'; t.after(()=>delete process.env.TESTHELIX_WORKFLOW_TEST_KEY);
  for(const model of ['gpt-5.4','gpt-5.5']) {
    c.model.name=model;
    for(const role of ['execution','planner'] as const) await new SDKModel(c,role).complete({system:'Return OK',messages:[{role:'user',content:'OK'}],tools:{},maxOutputTokens:1024,signal:AbortSignal.timeout(3000)});
  }
  assert.deepEqual(bodies.map(b=>[b.model,b.reasoning.effort,b.store]),[['gpt-5.4','medium',false],['gpt-5.4','xhigh',false],['gpt-5.5','medium',false],['gpt-5.5','xhigh',false]]);
});

test('reflection exports generic failure mechanisms without task literals or solution fragments',()=>{
  const labels=failureLabels('ModuleNotFoundError at secret_task/path.py. Assertion expected 872391. Negative mutant return False. Review citation exceeds taskLines.');
  assert.deepEqual(labels,['framework-preparation','review-citation']);
  assert.doesNotMatch(JSON.stringify(labels),/secret_task|872391|False/);
});

test('shared API leases enforce global and planner concurrency across six Node processes', async t => {
  const f=await fixture(); t.after(f.cleanup); const c=config();
  Object.assign(c.scheduler,{enabled:true,directory:path.join(f.root,'slots'),apiConcurrency:2,planningConcurrency:1});
  const file=path.join(f.root,'config.json'), log=path.join(f.root,'events.jsonl'); await fs.writeFile(file,JSON.stringify(c));
  const worker=path.join(f.root,'worker.mjs');
  await fs.writeFile(worker,`import fs from 'node:fs/promises';
    import {withAPISlot} from ${JSON.stringify(new URL('../src/api-scheduler.ts',import.meta.url).href)};
    const config=JSON.parse(await fs.readFile(process.argv[2],'utf8')), planning=process.argv[4]==='true';
    await withAPISlot(config,planning,AbortSignal.timeout(15000),async()=>{
      await fs.appendFile(process.argv[3],JSON.stringify({delta:1,planning})+'\\n');
      await new Promise(r=>setTimeout(r,350));
      await fs.appendFile(process.argv[3],JSON.stringify({delta:-1,planning})+'\\n');
    });`);
  await Promise.all(Array.from({length:6},(_,i)=>new Promise<void>((resolve,reject)=>{
    const child=spawn(process.execPath,['--import','tsx',worker,file,log,String(i<3)],{stdio:['ignore','ignore','pipe']});
    let stderr=''; child.stderr.on('data',x=>stderr+=x); child.on('error',reject);
    child.on('close',code=>code===0?resolve():reject(new Error(stderr)));
  })));
  let active=0, planners=0, peak=0;
  for(const line of (await fs.readFile(log,'utf8')).trim().split('\n')) {
    const e=JSON.parse(line); active+=e.delta; planners+=e.planning?e.delta:0; peak=Math.max(peak,active);
    assert.ok(active<=2&&planners<=1&&active>=0&&planners>=0);
  }
  assert.equal(active,0); assert.equal(peak,2);
  c.scheduler.apiConcurrency=3;
  await assert.rejects(withAPISlot(c,false,AbortSignal.timeout(1000),async()=>{}), error=>(error as any).requestSent===false);
});

test('queued cancellation never dispatches and does not strand a slot', async t => {
  const f=await fixture(); t.after(f.cleanup); const c=config();
  Object.assign(c.scheduler,{enabled:true,directory:path.join(f.root,'slots'),apiConcurrency:1});
  let entered!:()=>void, release!:()=>void;
  const started=new Promise<void>(r=>entered=r), finish=new Promise<void>(r=>release=r);
  const holder=withAPISlot(c,false,AbortSignal.timeout(3000),async()=>{entered(); await finish;});
  await started;
  await assert.rejects(withAPISlot(c,false,AbortSignal.timeout(30),async()=>assert.fail('must not dispatch')),e=>(e as any).requestSent===false);
  release(); await holder;
  assert.equal(await withAPISlot(c,true,AbortSignal.timeout(1000),async()=>42),42);
});

test('native transport preserves Unicode split across socket chunks and rejects premature close', async t => {
  const f=await fixture(); t.after(f.cleanup); const socket=path.join(f.root,'control.sock'); let broken=false;
  const server=net.createServer(connection=>connection.once('data',async()=>{
    if(broken) {connection.end();return;}
    const body=Buffer.from(JSON.stringify({ok:true,result:{text:'中文测试'}})+'\n');
    for(const byte of body) {connection.write(Buffer.from([byte])); await delay(1);}
    connection.end();
  }));
  await new Promise<void>(r=>server.listen(socket,r)); t.after(()=>new Promise<void>(r=>server.close(()=>r())));
  assert.deepEqual(await nativeRequest(socket,'exec',{},AbortSignal.timeout(5000)),{text:'中文测试'});
  broken=true; await assert.rejects(nativeRequest(socket,'exec',{},AbortSignal.timeout(1000)),/closed before/);
});

test('native factory carries remaining deadlines and cancels the exact fork operation when its reply is lost', async t=>{
  const f=await fixture();t.after(f.cleanup);const socket=path.join(f.root,'control.sock');
  const requests:Array<{method:string;params:Record<string,any>}>=[];let loseReply=false,remaining=321;
  const server=net.createServer(connection=>{
    let data='';connection.setEncoding('utf8');connection.on('data',chunk=>{
      data+=chunk;if(!data.endsWith('\n'))return;
      const request=JSON.parse(data);requests.push(request);
      if(loseReply&&request.method==='fork'){connection.end();return;}
      const result=request.method==='fork'?{id:'owned',container:'fixture',root:'/app'}
        :request.method==='exec'?{code:0,stdout:'ok',stderr:'',timedOut:false}:{};
      connection.end(JSON.stringify({ok:true,result})+'\n');
    });
  });
  await new Promise<void>(r=>server.listen(socket,r));t.after(()=>new Promise<void>(r=>server.close(()=>r())));
  const factory=new NativeFactory({kind:'testprism',socket,container:'original',root:'/app'},()=>remaining);
  const sandbox=await factory.fork('first');
  assert.match(requests[0]!.params.operationId,/^[a-f0-9]{32}$/);assert.equal(requests[0]!.params.timeoutMs,321);
  remaining=77;await sandbox.exec('true',5000);assert.equal(requests.at(-1)!.params.timeoutMs,77);
  await sandbox.terminal('read','shell','',AbortSignal.timeout(1000));assert.equal(requests.at(-1)!.params.timeoutMs,77);
  loseReply=true;await assert.rejects(factory.fork('lost'),/closed before/);
  const failed=requests.at(-2)!,cancel=requests.at(-1)!;
  assert.equal(failed.method,'fork');assert.deepEqual(cancel,{method:'cancel',params:{operationId:failed.params.operationId}});
  assert.notEqual(failed.params.operationId,requests[0]!.params.operationId);
  remaining=0;const count=requests.length;await assert.rejects(factory.fork('expired'),/exhausted/);assert.equal(requests.length,count);
});

test('component lessons are versioned, bounded and retrieved only in their component and capability', () => {
  const {lessons}=updateLesson([],{operation:'upsert',id:'runner-control',component:'preparation',capabilities:['repository'],
    triggers:['test runner'],when:'Runner setup fails',action:'Probe the existing test runner',verify:'A generic control executes',rationale:'Environment failures precede assertions'});
  assert.match(lessons[0]!.version!,/^[a-f0-9]{64}$/);
  assert.equal(retrieveLessons(lessons,'test runner',1600,{component:'preparation',capabilities:['repository']}).length,1);
  assert.equal(retrieveLessons(lessons,'test runner',1600,{component:'review',capabilities:['repository']}).length,0);
  assert.equal(retrieveLessons(lessons,'test runner',1600,{component:'preparation',capabilities:['visual']}).length,0);
});

test('training screening stratifies sources, is deterministic, and never duplicates tasks', () => {
  const samples=Array.from({length:30},(_,i)=>({key:`s${i%3}/t${i}`,source:`s${i%3}`}));
  const selected=screeningSamples(samples,6,'seed'); assert.deepEqual(selected,screeningSamples([...samples].reverse(),6,'seed'));
  assert.equal(new Set(selected.map(s=>s.key)).size,6); assert.equal(new Set(selected.map(s=>s.source)).size,3);
  assert.notDeepEqual(selected,screeningSamples(samples,6,'another'));
});

test('native lifecycle and staging survive shutdown races, cleanup faults and invalid ownership', async () => {
  const result = await command(['python3','-B',fileURLToPath(new URL('./native_workflow_contract.py',import.meta.url))], {timeoutMs:30000,maxBytes:12000});
  assert.equal(result.code,0,result.stdout+result.stderr);
});
