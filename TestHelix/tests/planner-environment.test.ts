import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {planTask,observeTaskEnvironment} from '../src/planner.js';
import {LocalSandbox,LocalFactory} from '../src/runtime.js';
import {Budget} from '../src/budget.js';
import {Store} from '../src/store.js';
import type {Model} from '../src/types.js';
import {config,fixture,completion,FixtureModel} from './helpers.js';
import {runTask} from '../src/pipeline.js';

test('planner exposes available project and conda environments without activating them or unbounding context',async t=>{
  const f=await fixture();t.after(f.cleanup);const settings=config();settings.planner.enabled=true;
  const sandbox=new LocalSandbox(f.workspace,false);
  await sandbox.write('.venv/bin/python',Buffer.from('#!/bin/sh\ntouch interpreter-was-executed\n'),0o755);
  await sandbox.write('venv/bin/python',Buffer.from('#!/bin/sh\nexit 0\n'),0o644);
  for(let i=0;i<65;i++)await sandbox.write(`inputs/${i}.txt`,Buffer.from('public fixture'));
  const execute=sandbox.exec.bind(sandbox);
  let probes=0;
  sandbox.exec=(script,timeout,signal)=>{probes++;return execute(
    'conda() { if [ "$*" != "env list --json" ]; then touch unexpected-conda-action; return 1; fi; '+
    'printf \'{"envs":["/available/env"],"padding":"\'; printf \'%05000d\' 0; printf \'"}\\n\'; }\n'+script,
    timeout,signal);};
  const store=new Store(f.out);await store.init();
  const budget=await Budget.open(settings,path.join(f.out,'budget.json'),'environment-probe',false);
  let observed=false;
  const model:Model={complete:async args=>{
    const payload=JSON.parse(args.messages[0]!.content as string);
    assert.ok(payload.environment.output.split('\n').includes('.venv/bin/python'));
    assert.ok(!payload.environment.output.split('\n').includes('venv/bin/python'));
    assert.ok(payload.environment.output.includes('/available/env'));
    assert.ok(payload.environment.output.length<=2400);
    assert.equal(payload.environment.files.length,60);
    assert.equal(payload.environment.truncated,true);
    assert.ok(!Object.hasOwn(args.tools.submit_plan!.schema.properties as object,'environment'));
    observed=true;
    return completion([['submit_plan',{capabilities:['algorithm'],
      requirements:[{requirement:'Prime classification',taskLines:{startLine:1}}],
      preparation:['Verify the selected runtime with a supported operation.'],
      branches:[{focus:'Exercise the public prime contract.',verification:'Baseline fails and repair passes.'}],
      unknowns:[],completion:'Publish tests after validated repair control.'}]]);
  }};
  assert.ok(await planTask(f.task,settings,sandbox,budget,store,model));
  assert.equal(observed,true);
  const retained=await observeTaskEnvironment(f.task,settings,sandbox,budget,store);
  assert.equal(retained?.files.length,60);
  assert.equal(probes,1);
  const checkpoint=JSON.parse(await fs.readFile(path.join(f.out,'environment.json'),'utf8'));
  assert.deepEqual(checkpoint.observation,retained);
  await assert.rejects(observeTaskEnvironment({...f.task,id:'different-task'},settings,sandbox,budget,store),/identity mismatch/);
  await assert.rejects(fs.stat(path.join(f.workspace,'interpreter-was-executed')));
  await assert.rejects(fs.stat(path.join(f.workspace,'unexpected-conda-action')));
});

test('solvers and reviewers receive host facts without leaking the primary plan or candidate repair',async t=>{
  const f=await fixture();t.after(f.cleanup);const settings=config();settings.planner.enabled=true;
  const factory=await LocalFactory.create(f.workspace,path.join(f.root,'branches'));t.after(()=>factory.close());
  const fork=factory.fork.bind(factory);
  factory.fork=async label=>{
    const sandbox=await fork(label),execute=sandbox.exec.bind(sandbox);
    sandbox.exec=(script,timeout,signal)=>execute(
      'conda() { printf \'{"envs":["/installed/observed-runtime"]}\\n\'; }\n'+script,timeout,signal);
    return sandbox;
  };
  let plannerCalls=0;
  const plannerModel:Model={complete:async()=>{plannerCalls++;return completion([['submit_plan',{
    capabilities:['algorithm'],requirements:[{requirement:'Prime classification',taskLines:{startLine:1}}],
    preparation:[],branches:[{focus:'PRIMARY_PLAN_ONLY',verification:'Verify prime behavior.'}],unknowns:[],completion:'Validated tests.'
  }]]);}};
  const model=new FixtureModel();
  const complete=model.complete.bind(model);
  model.complete=async args=>{
    const reply=await complete(args);
    for(const call of reply.calls)if(call.name==='write_file'){
      const input=call.input as {path:string;content:string};
      if(input.path==='prime.cjs')input.content+='\n// PRIVATE_REPAIR_CONTENT\n';
    }
    return reply;
  };
  const result=await runTask(f.task,settings,f.out,{factory,model,plannerModel});
  assert.equal(result.status,'validated');assert.equal(plannerCalls,1);
  const solvers=model.history.filter(call=>'submit_candidate' in call.tools);
  assert.ok(solvers.length>=2);
  for(const call of solvers)assert.ok(JSON.stringify(call.messages).includes('/installed/observed-runtime'));
  const independent=solvers.filter(call=>JSON.stringify(call.messages).includes('Independently solve'));
  assert.ok(independent.length>0);
  for(const call of independent)assert.ok(!JSON.stringify(call.messages).includes('PRIMARY_PLAN_ONLY'));
  const reviewers=model.history.filter(call=>'submit_review' in call.tools);
  assert.ok(reviewers.length>0);
  for(const call of reviewers){
    const context=JSON.stringify(call.messages);
    assert.ok(context.includes('/installed/observed-runtime'));
    assert.ok(!context.includes('PRIMARY_PLAN_ONLY'));
    assert.ok(!context.includes('PRIVATE_REPAIR_CONTENT'));
  }
  const observation=JSON.parse(await fs.readFile(path.join(f.out,'environment.json'),'utf8'));
  assert.ok(!JSON.stringify(observation).includes('PRIMARY_PLAN_ONLY'));
});

test('environment discovery bounds whole escaped paths and rejects oversized cached observations',async t=>{
  const f=await fixture();t.after(f.cleanup);const settings=config(),sandbox=new LocalSandbox(f.workspace,false);
  const prefix=Array.from({length:22},()=>`b${'"'.repeat(40)}`).join('/');
  const first=`${prefix}/a.txt`,second=`${prefix}/b.txt`;
  await sandbox.write('a-short.txt',Buffer.from('public'));
  await sandbox.write(first,Buffer.from('public'));
  await sandbox.write(second,Buffer.from('public'));
  assert.ok(first.length+second.length<2400);
  assert.ok(JSON.stringify([first,second]).length>2400);
  let probes=0;
  sandbox.exec=async()=>{probes++;return {code:0,stdout:'available-runtime\n',stderr:'',timedOut:false,truncated:false,durationMs:1};};
  const store=new Store(f.out);await store.init();
  const budget=await Budget.open(settings,path.join(f.out,'budget.json'),'escaped-environment',false);
  const observed=await observeTaskEnvironment(f.task,settings,sandbox,budget,store);
  assert.deepEqual(observed?.files,['a-short.txt',first]);
  assert.equal(observed?.truncated,true);
  assert.ok(JSON.stringify(observed?.files).length<=2400);
  const saved=JSON.parse(await fs.readFile(path.join(f.out,'environment.json'),'utf8'));
  saved.observation.files=[first,second];
  await store.save('environment.json',saved);
  await assert.rejects(observeTaskEnvironment(f.task,settings,sandbox,budget,store),/serialized context bound/);
  assert.equal(probes,1);
});
