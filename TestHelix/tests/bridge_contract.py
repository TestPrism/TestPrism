import importlib.util, json, os, pathlib, subprocess, sys, tempfile, types
from enum import Enum

base = types.ModuleType('testprism.agents.base_agent')
class Mode(str, Enum):
    REPLACE = 'replace'
    APPEND = 'append'
class BaseAgent:
    def __init__(self, **kwargs):
        self.system_prompt = kwargs.get('system_prompt', '')
        self.system_prompt_mode = Mode(kwargs.get('system_prompt_mode', 'replace'))
        self.profile_env = kwargs.get('profile_env', {})
class AgentResult:
    def __init__(self, **kwargs): self.__dict__.update(kwargs)
base.BaseAgent, base.AgentResult, base.SystemPromptMode = BaseAgent, AgentResult, Mode
failure = types.ModuleType('testprism.agents.failure_mode')
class Failure(str, Enum):
    NONE='none'
    AGENT_TEST_MISSING='agent_test_missing'
    UNKNOWN_AGENT_ERROR='unknown_agent_error'
    AGENT_TIMEOUT='agent_timeout'
failure.FailureMode=Failure
sys.modules['testprism.agents.base_agent']=base
sys.modules['testprism.agents.failure_mode']=failure
spec=importlib.util.spec_from_file_location('bridge_under_test', sys.argv[1])
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with tempfile.TemporaryDirectory(prefix='testhelix-bridge-') as temporary:
    root=pathlib.Path(temporary); (root/'dist').mkdir()
    (root/'dist/cli.js').write_text('''const fs=require('fs'); const path=require('path');
const args=process.argv.slice(2), out=args[args.indexOf('--out')+1];
if(args[0]==='cleanup') {
  fs.writeFileSync(path.join(path.dirname(out),'cleanup-marker'),'yes');
  process.exit(process.env.FIXTURE_SCENARIO==='cleanup-fail' ? 7 : 0);
}
const task=JSON.parse(fs.readFileSync(args[args.indexOf('--task')+1]));
if(task.systemPrompt!=='EXACT NATIVE PROMPT' || task.systemPromptMode!=='replace' || task.runtime.container!=='fixture-container' || task.runtime.root!=='/testbed' || task.source!=='from-swe-perf' || task.instruction!=='public instruction' || process.env.FIXTURE_PROFILE!=='private') process.exit(19);
fs.mkdirSync(out,{recursive:true});
if(process.env.FIXTURE_SCENARIO==='corrupt-result') {
  fs.writeFileSync(path.join(out,'budget.json'),JSON.stringify({input:777,output:222,pending:{unknown:{}}}));
  fs.writeFileSync(path.join(out,'result.json'),'{broken'); process.exit(0);
}
if(process.env.FIXTURE_SCENARIO==='budget-fail' || process.env.FIXTURE_SCENARIO==='timeout') {
  fs.writeFileSync(path.join(out,'budget.json'),JSON.stringify({input:999,output:333,pending:{unknown:{}}}));
  if(process.env.FIXTURE_SCENARIO==='timeout') setTimeout(()=>{},30000); else process.exit(3);
} else {
  const providerFailed=process.env.FIXTURE_SCENARIO==='provider-failed';
  const empty=process.env.FIXTURE_SCENARIO==='empty' || providerFailed;
  if(providerFailed) fs.writeFileSync(path.join(out,'events.jsonl'),JSON.stringify({type:'model.error',data:{status:429}})+'\\n');
  fs.writeFileSync(path.join(out,'result.json'),JSON.stringify({status:empty?'no_submit':'validated',usage:{input:234,output:56}}));
  if(empty) process.exit(2);
}
''')
    container=types.SimpleNamespace(id='fixture-container', attrs={'Config':{'WorkingDir':'/testbed'}}, reload=lambda: None)
    agent=module.TestHelixAgent(project_root=root, task_id='from-swe-perf/public-task', task_source='from-swe-perf',
                               test_timeout_sec=1.25, system_prompt='EXACT NATIVE PROMPT', profile_env={'FIXTURE_PROFILE':'private'})
    result=agent.perform_task('public instruction', types.SimpleNamespace(container=container), root/'logs')
    assert result.total_input_tokens==234 and result.total_output_tokens==56 and result.failure_mode==Failure.NONE, result.__dict__
    assert agent.native_system_prompt_modes==frozenset({Mode.REPLACE, Mode.APPEND})
    public=json.loads((root/'logs/testhelix-public-task.json').read_text())
    assert public['id']=='from-swe-perf/public-task' and public['testTimeoutMs']==1250
    assert not {'patch_sets', 'reference_patch', 'dataset_path', 'apiKey', 'profile_env'}.intersection(public)
    try:
        agent.perform_task('public instruction', types.SimpleNamespace(container=container), root/'logs')
        raise AssertionError('an existing result must not be silently overwritten')
    except RuntimeError as error:
        assert 'fresh harness trial directory' in str(error)
    for scenario, expected, tokens in [('empty',Failure.AGENT_TEST_MISSING,234),
                                       ('provider-failed',Failure.UNKNOWN_AGENT_ERROR,234),
                                       ('cleanup-fail',Failure.UNKNOWN_AGENT_ERROR,234),
                                       ('budget-fail',Failure.UNKNOWN_AGENT_ERROR,999),
                                       ('corrupt-result',Failure.UNKNOWN_AGENT_ERROR,777),
                                       ('timeout',Failure.AGENT_TIMEOUT,999)]:
        candidate=module.TestHelixAgent(project_root=root, task_source='from-swe-perf', system_prompt='EXACT NATIVE PROMPT',
                                        timeout_sec=.3 if scenario=='timeout' else 5,
                                        profile_env={'FIXTURE_PROFILE':'private','FIXTURE_SCENARIO':scenario})
        result=candidate.perform_task('public instruction',types.SimpleNamespace(container=container),root/scenario)
        assert result.failure_mode==expected and result.total_input_tokens==tokens, (scenario,result.__dict__)
        assert (root/scenario/'cleanup-marker').is_file()
    external=root/'external'; external.mkdir()
    readonly=module.TestHelixAgent(project_root=root, benchmark_root=external)
    try:
        readonly.perform_task('public instruction', types.SimpleNamespace(container=container),external/'logs')
        raise AssertionError('external output must be rejected')
    except ValueError as error:
        assert 'outside' in str(error)
    assert list(external.iterdir())==[]
    for name in ['testhelix-js','testhelix-public-task.json','testhelix-node.log']:
        logs=root/('symlink-'+name); logs.mkdir()
        target=external/name
        if name=='testhelix-js': target.mkdir()
        else: target.write_text('MUST_REMAIN_UNCHANGED')
        (logs/name).symlink_to(target, target_is_directory=name=='testhelix-js')
        try:
            readonly.perform_task('public instruction', types.SimpleNamespace(container=container),logs)
            raise AssertionError('bridge output symlink must be rejected before writes')
        except ValueError as error:
            assert 'symlink' in str(error)
        if name=='testhelix-js': assert list(target.iterdir())==[]
        else: assert target.read_text()=='MUST_REMAIN_UNCHANGED'
print('Native adapter contract: passed (stubbed TestPrism types, real Node subprocess)')
