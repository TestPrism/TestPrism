import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import path from 'node:path';
import {runTask} from '../src/pipeline.js';
import {Validator} from '../src/validation.js';
import {Budget} from '../src/budget.js';
import {Store} from '../src/store.js';
import {LocalFactory,LocalSandbox,publishTests,type SandboxFactory} from '../src/runtime.js';
import {captureWorkspaceCandidate, replayPath} from '../src/artifacts.js';
import type {FileArtifact,Model} from '../src/types.js';
import {config,fixture,FixtureModel,completion} from './helpers.js';

const file=(path:string,content:string):FileArtifact=>({path,data:Buffer.from(content).toString('base64'),mode:0o644});
const check="const assert=require('node:assert/strict'),fs=require('node:fs');const p='.testhelix-tmp/completed';assert.ok(fs.existsSync(p),'required completed state missing');assert.equal(fs.readFileSync(p,'utf8'),'completed');\n";
const tests=[file('agent-tests/run-tests.sh','node agent-tests/check.cjs\n'),file('agent-tests/check.cjs',check)];

test('independent command controls replay before cross-validation and never enter published tests',async t=>{
  const f=await fixture();t.after(f.cleanup);const settings=config();
  f.task.instruction='Complete the operation by writing completed to .testhelix-tmp/completed. Tests must observe this state without performing the operation.';
  const reviewer=new FixtureModel();
  const model:Model={complete:async args=>{
    if('submit_review' in args.tools)return reviewer.complete(args);
    const second=JSON.stringify(args.messages).includes('Independently solve');
    return completion([
      ['write_file',{path:'internal-repair.sh',content:'mkdir -p .testhelix-tmp\n'+(second?'printf completed':'printf %s completed')+' > .testhelix-tmp/completed\n'}],
      ...tests.map(item=>['write_file',{path:item.path,content:Buffer.from(item.data,'base64').toString()}] as [string,unknown]),
      ['submit_candidate',{requirements:['observable completed state'],rationale:'Independent command replay control',replayScript:'internal-repair.sh'}],
    ]);
  }};
  const result=await runTask(f.task,settings,f.out,{model});
  assert.equal(result.status,'validated');assert.equal(result.candidates.length,2);
  assert.ok(result.candidates.every(c=>c.replayScript==='internal-repair.sh'));
  assert.notEqual(result.candidates[0]!.sourceHash,result.candidates[1]!.sourceHash);
  assert.ok(result.matrix.some(cell=>cell.sourceId==='baseline' && cell.status==='fail'));
  assert.ok(result.matrix.filter(cell=>cell.sourceId!=='baseline').every(cell=>cell.status==='pass'));
  await assert.rejects(fs.stat(path.join(f.workspace,'internal-repair.sh')));
  await assert.rejects(fs.stat(path.join(f.workspace,'.testhelix-tmp/completed')));
  assert.equal(await fs.readFile(path.join(f.workspace,'agent-tests/check.cjs'),'utf8'),check);
});

test('replay selection is part of cache identity and a repair cannot replace the frozen tests',async t=>{
  const f=await fixture();t.after(f.cleanup);const settings=config();
  const factory=await LocalFactory.create(f.workspace,path.join(f.root,'branches'));t.after(()=>factory.close());
  const store=new Store(f.out);await store.init();const budget=await Budget.open(settings,path.join(f.out,'budget.json'),'replay',false);
  const validator=new Validator(factory,settings,store,budget,'replay');
  const quoted="repair with ' quote.sh";
  const source=[file(quoted,'mkdir -p .testhelix-tmp\nprintf completed > .testhelix-tmp/completed\n'),
    file('fake.sh','mkdir -p agent-tests\nprintf "exit 0\\n" > agent-tests/run-tests.sh\n'),file('failed.sh','exit 19\n')];
  assert.equal((await validator.evaluate(tests,source,'t','good',2,{replayScript:quoted})).status,'pass');
  assert.equal((await validator.evaluate(tests,source,'t','fake',2,{replayScript:'fake.sh'})).status,'fail');
  const failed=await validator.evaluate(tests,source,'t','failed',1,{replayScript:'failed.sh'});
  assert.equal(failed.status,'error');assert.match(await store.readArtifact(failed.artifact!),/"code":19/);
  assert.equal(budget.snapshot().calls,0);
});

test('replay capture rejects uncaptured, test, private, deleted, binary and syntactically invalid scripts',async t=>{
  const f=await fixture();t.after(f.cleanup);
  const source=[file('control.sh','exit 0\n'),file('agent-tests/control.sh','exit 0\n'),{...file('gone.sh','exit 0\n'),deleted:true},file('binary.sh','\0')];
  for(const name of ['missing.sh','agent-tests/control.sh','../control.sh','.git/control.sh','gone.sh','binary.sh'])
    assert.throws(()=>replayPath(source,name));
  assert.equal(replayPath(source,f.workspace+'/control.sh',f.workspace),'control.sh');
  const factory=await LocalFactory.create(f.workspace,path.join(f.root,'branches'));t.after(()=>factory.close());
  const baseline=await factory.fork('baseline'),sandbox=await factory.fork('candidate');
  await sandbox.write('agent-tests/run-tests.sh',Buffer.from('exit 1\n'));
  await sandbox.write('control.sh',Buffer.from('if then\n'));
  await assert.rejects(captureWorkspaceCandidate(sandbox,baseline,{requirements:['state'],rationale:'fixture',replayScript:'control.sh'},
    {id:'c',lineage:'c',round:0,strategy:'fixture',sessionId:'c'},10000),/Replay script syntax invalid/);
});

test('captured replay repairs external public source while tests retain the official artifact root',async t=>{
  const f=await fixture();t.after(f.cleanup);const settings=config();
  const publicSource=path.join(f.root,'public-source');
  await fs.mkdir(publicSource);
  await fs.writeFile(path.join(publicSource,'value.cjs'),'module.exports = 0;\n');
  // Model the image-backed layout: each fresh environment owns both the official
  // working directory and a separate public checkout. No host source is edited.
  const factory:SandboxFactory={
    fork:async()=>{
      const environment=await fs.mkdtemp(path.join(f.root,'external-replay-'));
      const workspace=path.join(environment,'official');
      await fs.mkdir(workspace);
      await fs.cp(publicSource,path.join(environment,'public-source'),{recursive:true});
      return new LocalSandbox(workspace,false);
    },
    publish:files=>publishTests(new LocalSandbox(f.workspace,false),files),
    close:async()=>{},
  };
  const baseline=await factory.fork('baseline'),candidate=await factory.fork('candidate');
  const runner='node agent-tests/check.cjs\n';
  const assertion="const assert=require('node:assert/strict');assert.equal(require('../../public-source/value.cjs'),1);\n";
  await candidate.write('agent-tests/run-tests.sh',Buffer.from(runner),0o755);
  await candidate.write('agent-tests/check.cjs',Buffer.from(assertion));
  await candidate.write('internal-repair.sh',Buffer.from("printf 'module.exports = 1;\\n' > ../public-source/value.cjs\n"));
  assert.equal((await candidate.exec('bash internal-repair.sh',10000)).code,0);
  const captured=await captureWorkspaceCandidate(candidate,baseline,{requirements:['public result equals one'],
    rationale:'Replay separate public checkout without moving exported artifacts',replayScript:'internal-repair.sh'},
    {id:'external',lineage:'external',round:0,strategy:'fixture',sessionId:'external'},10000);
  assert.deepEqual(captured.source.map(item=>item.path),['internal-repair.sh']);
  const store=new Store(f.out);await store.init();
  const budget=await Budget.open(settings,path.join(f.out,'budget.json'),'external-replay',false);
  const validator=new Validator(factory,settings,store,budget,'external-replay');
  assert.equal((await validator.evaluate(captured.tests,[],'t','baseline',1)).status,'fail');
  assert.equal((await validator.evaluate(captured.tests,captured.source,'t','repair',2,
    {replayScript:captured.replayScript})).status,'pass');
  assert.equal((await validator.evaluate(captured.tests,[],'t','fresh-baseline',1,{fresh:true})).status,'fail');
  await factory.publish(captured.tests);
  assert.equal(await fs.readFile(path.join(f.workspace,'agent-tests/check.cjs'),'utf8'),assertion);
  assert.equal(await fs.readFile(path.join(publicSource,'value.cjs'),'utf8'),'module.exports = 0;\n');
  await assert.rejects(fs.stat(path.join(f.workspace,'internal-repair.sh')));
  assert.equal(budget.snapshot().calls,0);
});
