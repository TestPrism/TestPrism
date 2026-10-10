import {promises as fs} from 'node:fs';
import path from 'node:path';
import {randomUUID} from 'node:crypto';
import {setTimeout as delay} from 'node:timers/promises';
import lockfile from 'proper-lockfile';
import {z} from 'zod';
import type {Config} from './types.js';
import {atomicJSON, hash} from './util.js';

const failure = z.object({until:z.number().finite(), observedAt:z.number().finite(),
  reason:z.enum(['rate-limit','unavailable','authentication','upstream']), strikes:z.number().int().min(1)});
const stateSchema = z.object({version:z.literal(1), rate:failure.optional(), blocked:z.record(z.string(),failure),
  probe:z.object({id:z.string(), until:z.number().finite()}).optional()});
type State = z.infer<typeof stateSchema>;
export class ProviderUnavailable extends Error {
  readonly requestSent = false;
  constructor(readonly reason: string, readonly retryAt: number) {
    super(`Provider admission paused (${reason}); retry after ${new Date(retryAt).toISOString()}`);
    this.name = 'ProviderUnavailable';
  }
}

function location(config: Config): string {
  // Share account rate limits across roles/models, but invalidate on credential rotation.
  return path.join(path.resolve(config.scheduler.directory), 'health-' + hash({provider:config.model.provider,
    endpoint:new URL(config.model.baseURL).href.replace(/\/+$/,''), credential:process.env[config.model.apiKeyEnv],
    headers:Object.fromEntries(Object.entries(config.model.headersEnv).map(([key, variable])=>[key,process.env[variable]]))}).slice(0,24) + '.json');
}
async function state<T>(config: Config, signal: AbortSignal, action:(state:State)=>T): Promise<T> {
  const file = location(config);
  await fs.mkdir(path.dirname(file), {recursive:true,mode:0o700});
  let release: (()=>Promise<void>) | undefined;
  while (!release) {
    signal.throwIfAborted();
    try {release = await lockfile.lock(file,{realpath:false,retries:0,stale:60000,update:10000});}
    catch(error) {if((error as NodeJS.ErrnoException).code!=='ELOCKED')throw error; await delay(30,undefined,{signal});}
  }
  try {
    const raw = await fs.readFile(file,'utf8').catch(error=>{if(error.code==='ENOENT')return '';throw error;});
    const value:State = raw ? stateSchema.parse(JSON.parse(raw)) : {version:1,blocked:{}};
    const previous=JSON.stringify(value);
    const result = action(value);
    if(!raw || JSON.stringify(value)!==previous)await atomicJSON(file,value);
    return result;
  } finally {await release();}
}
const modelKey = (config:Config)=>hash(config.model.name);
const roleKey = (config:Config, planning:boolean)=>hash({model:config.model.name,effort:planning?'xhigh':config.model.providerOptions.openai?.reasoningEffort});

/** Shared cooldowns are checked before acquiring API slots; only one recovery probe is admitted. */
export async function providerAdmission(config:Config, planning:boolean, signal:AbortSignal, claim=false):Promise<string|undefined> {
  try {return await admission(config,planning,signal,claim);}
  catch(error) {throw Object.assign(error instanceof Error ? error : new Error(String(error)),{requestSent:false});}
}
async function admission(config:Config, planning:boolean, signal:AbortSignal, claim:boolean):Promise<string|undefined> {
  if(!config.scheduler.enabled)return;
  for (;;) {
    const result = await state(config,signal,value=>{
      const now=Date.now(), relevant=['account',modelKey(config),roleKey(config,planning)].map(key=>value.blocked[key]).filter(x=>!!x);
      const blocked=relevant.find(item=>item.until>now);
      if(blocked)throw new ProviderUnavailable(blocked.reason,blocked.until);
      const until=Math.max(value.rate?.until??0,value.probe?.until??0);
      if(until>now)return {wait:Math.min(250,until-now)};
      if(claim && (value.rate || relevant.length)) {
        const id=randomUUID(); value.probe={id,until:now+config.model.timeoutMs+15000};
        return {probe:id};
      }
      return {};
    });
    if(!result.wait)return result.probe;
    await delay(result.wait,undefined,{signal});
  }
}

/** No task content or raw provider error body is persisted in the shared circuit. */
export async function healthyFetch(config:Config, planning:boolean, signal:AbortSignal, work:()=>Promise<Response>):Promise<Response> {
  if(!config.scheduler.enabled)return work();
  const probe=await providerAdmission(config,planning,signal,true), startedAt=Date.now();
  let response:Response|undefined;
  try {
    response=await work();
    const body=[400,404,422].includes(response.status) ? (await response.clone().text()).slice(0,6000) : '';
    await state(config,AbortSignal.timeout(5000),value=>{
      const now=Date.now(), status=response!.status;
      const remember=(key:string,reason:'unavailable'|'authentication')=>{
        value.blocked[key]={reason,observedAt:now,until:Math.max(value.blocked[key]?.until??0,now+config.scheduler.providerUnavailableMs),
          strikes:(value.blocked[key]?.strikes??0)+1};
      };
      if(status===401 || status===403)remember('account','authentication');
      else if(/(?:reasoning|effort).{0,100}(?:not supported|unsupported)|unsupported.{0,100}(?:reasoning|effort)/is.test(body))remember(roleKey(config,planning),'unavailable');
      else if(/model.{0,200}(?:not supported|unsupported|not found|does not exist)|(?:unsupported|unknown|invalid).{0,30}model/is.test(body))remember(modelKey(config),'unavailable');
      else if(status===429 || status>=500) {
        const strikes=(value.rate?.strikes??0)+1, rawMs=response!.headers.get('retry-after-ms'), raw=response!.headers.get('retry-after');
        let wait=rawMs!==null ? Number(rawMs) : raw!==null ? (Number.isFinite(Number(raw)) ? Number(raw)*1000 : Date.parse(raw)-now) : NaN;
        if(!Number.isFinite(wait)||wait<0)wait=Math.min(60000,config.scheduler.providerCooldownMs*2**Math.min(6,strikes-1))*(1+Math.random()*.2);
        value.rate={reason:status===429?'rate-limit':'upstream',observedAt:now,
          until:Math.max(value.rate?.until??0,Math.min(8640000000000000,now+wait)),strikes};
      } else if(response!.ok) {
        // Equal timestamps can belong to a newer failure in the same millisecond.
        // Leave that circuit intact; a later recovery probe can clear it safely.
        if(value.rate && value.rate.observedAt<startedAt)delete value.rate;
        for(const key of ['account',modelKey(config),roleKey(config,planning)])
          if(value.blocked[key] && value.blocked[key]!.observedAt<startedAt)delete value.blocked[key];
      }
      if(value.probe?.id===probe)delete value.probe;
    });
    return response;
  } catch (error) {
    // Initial TLS/socket failures need the same shared circuit as failed probes.
    // Caller cancellation is not evidence that the provider is unhealthy.
    if (!response && !signal.aborted) await state(config, AbortSignal.timeout(5000), value => {
      const now = Date.now(), strikes = (value.rate?.strikes ?? 0) + 1;
      value.rate = {reason: 'upstream', observedAt: now, strikes,
        until: Math.max(value.rate?.until ?? 0, now + Math.min(60000,
          config.scheduler.providerCooldownMs * 2 ** Math.min(6, strikes - 1)))};
    });
    throw error;
  } finally {
    if(probe)await state(config,AbortSignal.timeout(5000),value=>{
      if(value.probe?.id===probe) {
        delete value.probe;
      }
    });
  }
}
