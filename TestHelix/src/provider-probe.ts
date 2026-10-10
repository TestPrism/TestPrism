import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {Budget} from './budget.js';
import {SDKModel} from './model.js';
import type {Config, Model} from './types.js';
import {atomicJSON, hash} from './util.js';
import {withRunLock} from './lock.js';
import {retryPlan} from './retry.js';
import {failureLabels} from './component-feedback.js';

/** Explicit, separately budgeted, task-independent live tool-call admission test. */
export async function probeProvider(config:Config, outputDir:string,
  modelFactory=(role:'execution'|'planner'):Model=>new SDKModel(config,role)) {
  return withRunLock(outputDir,async()=>{
    const roles:Array<'execution'|'planner'>=config.planner.enabled?['execution','planner']:['execution'];
    const limited=structuredClone(config);
    limited.budget={...limited.budget,maxCalls:roles.length,maxInputTokens:8192*roles.length,
      maxOutputTokens:2048*roles.length,wallMs:Math.min(config.budget.wallMs,config.model.timeoutMs*roles.length+10000)};
    const identity=hash({config,kind:'provider-tool-probe-v1'});
    const budget=await Budget.open(limited,path.join(outputDir,'budget.json'),identity,false);
    const results:Array<Record<string,unknown>>=[];
    for(const role of roles) {
      const nonce=randomUUID();
      const reservation=await budget.reserve(4096,2048);
      const result:Record<string,unknown>={role,model:config.model.name,effort:role==='planner'?'xhigh':config.model.providerOptions.openai?.reasoningEffort};
      try {
        const reply=await modelFactory(role).complete({system:'Call report_probe exactly once, copying the nonce. No other action is needed.',
          messages:[{role:'user',content:`nonce: ${nonce}`}],tools:{report_probe:{description:'Report the nonce received in the prompt.',
            schema:{type:'object',properties:{nonce:{type:'string'}},required:['nonce'],additionalProperties:false}}},
          forceTool:'report_probe',maxOutputTokens:reservation.output,signal:AbortSignal.timeout(Math.max(1,Math.min(config.model.timeoutMs,budget.remainingMs)))});
        await budget.settle(reservation,reply.usage.estimated?undefined:reply.usage);
        result.ok=reply.calls.length===1&&reply.calls[0]!.name==='report_probe'&&!reply.calls[0]!.invalid
          && (reply.calls[0]!.input as {nonce?:unknown})?.nonce===nonce;
        result.responseReceived=true;
        result.finish=reply.finish;
        if(!result.ok)result.failureClasses=['provider-tool-protocol'];
      } catch(error) {
        const retry=retryPlan(error,0,0,0);
        await budget.settle(reservation,retry.rejected?{input:0,output:0,cached:0}:undefined);
        Object.assign(result,{ok:false,statusCode:retry.status,requestSent:(error as {requestSent?:boolean})?.requestSent!==false,
          failureClasses:failureLabels(error instanceof Error?error.message:String(error))});
      }
      results.push(result);
      await atomicJSON(path.join(outputDir,'probe.json'),{identity,ready:results.length===roles.length&&results.every(r=>r.ok),
        scope:'task-independent provider tool call; not a task quality evaluation',results,usage:budget.snapshot()});
      if(!result.ok)break;
    }
    return {ready:results.length===roles.length&&results.every(r=>r.ok),results,usage:budget.snapshot(),
      evidence:path.join(path.resolve(outputDir),'probe.json')};
  });
}
