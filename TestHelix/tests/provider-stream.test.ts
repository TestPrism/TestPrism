import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {SDKModel} from '../src/model.js';
import {configSchema} from '../src/types.js';

async function fixture(t: any, terminal = true, nativeState = false) {
  const requests: any[] = [];
  const server = http.createServer(async (req,res) => {
    let body=''; for await(const chunk of req) body+=chunk; requests.push(JSON.parse(body));
    res.writeHead(200, {'content-type':'text/event-stream'});
    const emit=(data:any)=>res.write(`data: ${JSON.stringify(data)}\n\n`);
    const call={type:'function_call',id:'fc_1',call_id:'call_1',name:'read_file',arguments:'```json\n{"path":"public.cjs"}\n```',status:'completed'};
    emit({type:'response.created',sequence_number:0,response:{id:'resp_1',created_at:1,model:'gpt-5.5'}});
    if(nativeState) {
      const reasoning={id:'rs_1',type:'reasoning',summary:[],encrypted_content:null};
      emit({type:'response.output_item.added',sequence_number:1,output_index:1,item:reasoning});
      emit({type:'response.reasoning_summary_part.added',sequence_number:2,output_index:1,item_id:'rs_1',summary_index:0,part:{type:'summary_text',text:''}});
      emit({type:'response.reasoning_summary_text.delta',sequence_number:3,output_index:1,item_id:'rs_1',summary_index:0,delta:'Public observation.'});
      emit({type:'response.output_item.done',sequence_number:4,output_index:1,item:{...reasoning,summary:[{type:'summary_text',text:'Public observation.'}],encrypted_content:'opaque-stream-signature'}});
      const message={id:'msg_1',type:'message',role:'assistant',phase:'commentary',status:'completed',content:[]};
      emit({type:'response.output_item.added',sequence_number:5,output_index:2,item:message});
      emit({type:'response.content_part.added',sequence_number:6,output_index:2,item_id:'msg_1',content_index:0,part:{type:'output_text',text:'',annotations:[]}});
      emit({type:'response.output_text.delta',sequence_number:7,output_index:2,item_id:'msg_1',content_index:0,delta:'Inspecting.'});
      emit({type:'response.output_item.done',sequence_number:8,output_index:2,item:{...message,content:[{type:'output_text',text:'Inspecting.',annotations:[]}]}});
    }
    emit({type:'response.output_item.added',sequence_number:1,output_index:0,item:{...call,arguments:''}});
    emit({type:'response.function_call_arguments.delta',sequence_number:2,output_index:0,item_id:'fc_1',delta:call.arguments});
    emit({type:'response.output_item.done',sequence_number:3,output_index:0,item:call});
    if(terminal) emit({type:'response.completed',sequence_number:4,response:{id:'resp_1',created_at:1,model:'gpt-5.5',status:'completed',
      output:[call],usage:{input_tokens:100,output_tokens:20,total_tokens:120,input_tokens_details:{cached_tokens:40},output_tokens_details:{reasoning_tokens:5}}}});
    res.end();
  });
  await new Promise<void>(r=>server.listen(0,'127.0.0.1',r));t.after(()=>new Promise<void>(r=>server.close(()=>r())));
  process.env.TESTHELIX_STREAM_FIXTURE_KEY='fixture';t.after(()=>delete process.env.TESTHELIX_STREAM_FIXTURE_KEY);
  const config=configSchema.parse({model:{provider:'openai-responses',name:'gpt-5.5',baseURL:`http://127.0.0.1:${(server.address() as any).port}/v1`,apiKeyEnv:'TESTHELIX_STREAM_FIXTURE_KEY',streaming:true}});
  return {requests,model:new SDKModel(config)};
}
const request=()=>({system:'Exact system contract',messages:[{role:'user' as const,content:'Read the public file'}],
  tools:{read_file:{description:'Read',schema:{type:'object',properties:{path:{type:'string'}},required:['path'],additionalProperties:false}}},
  signal:AbortSignal.timeout(3000),maxOutputTokens:512});
test('native SSE uses SDK parsing, normalizes final calls once, and retains authoritative cache usage',async t=>{
  const f=await fixture(t);const result=await f.model.complete(request());
  assert.equal(f.requests.length,1);assert.equal(f.requests[0].stream,true);assert.equal(f.requests[0].instructions,'Exact system contract');
  assert.deepEqual(result.calls.map(c=>({name:c.name,input:c.input,invalid:c.invalid})),[{name:'read_file',input:{path:'public.cjs'},invalid:undefined}]);
  assert.equal(result.protocol?.length,1);assert.deepEqual(result.usage,{input:100,output:20,cached:40,estimated:false});
  assert.equal(result.messages.filter(m=>m.role==='tool').length,0);
  assert.match(JSON.stringify(result.messages),/public.cjs/);assert.doesNotMatch(JSON.stringify(result.messages),/```/);
});
test('an SSE body ending after a tool call without a terminal response cannot return executable calls',async t=>{
  const f=await fixture(t,false);await assert.rejects(f.model.complete(request()),/without a complete response/);
});

test('SSE preserves encrypted reasoning and assistant phase through the next native request', async t => {
  const f=await fixture(t,true,true);const initial=request();const result=await f.model.complete(initial);
  assert.match(JSON.stringify(result.messages),/opaque-stream-signature/);
  await f.model.complete({...request(),messages:[...initial.messages,...result.messages,{role:'tool',content:[{type:'tool-result',toolCallId:'call_1',toolName:'read_file',output:{type:'json',value:{ok:true}}}]}]});
  assert.ok(f.requests[1].input.some((item:any)=>item.type==='reasoning'&&item.encrypted_content==='opaque-stream-signature'));
  assert.ok(f.requests[1].input.some((item:any)=>item.role==='assistant'&&item.phase==='commentary'));
});
