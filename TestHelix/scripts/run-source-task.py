"""Run one explicitly selected training task through existing native components.

This entry point never reads reference solutions or scores. Selection, concurrency,
image preparation and evolution remain responsibilities of the outer controller.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import uuid

sys.dont_write_bytecode = True
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

def write(file, value):
    temporary = file.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(file)

def digest(file): return hashlib.sha256(file.read_bytes()).hexdigest()
def now(): return datetime.now(timezone.utc).isoformat()

def main():
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('selection', 'runtime-copy', 'config', 'out'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--key', required=True)
    parser.add_argument('--bench', type=Path, default=PROJECT/'TestPrism-main')
    parser.add_argument('--agent-project', type=Path, default=PROJECT)
    parser.add_argument('--prepared', type=Path, help='Completed prepare-only directory')
    parser.add_argument('--evaluate', type=Path, help='Frozen host evaluation manifest; runs no model')
    parser.add_argument('--prepare-only', action='store_true', help='Verify an existing prepared image without generation')
    args = parser.parse_args()
    if args.evaluate and not args.prepared:
        parser.error('--evaluate requires --prepared')
    if args.evaluate and args.prepare_only:
        parser.error('--evaluate and --prepare-only are mutually exclusive')
    bench, out = args.bench.resolve(), args.out.resolve()
    from integrations.testprism.bootstrap import verify_runtime
    from integrations.testprism.staging import validate_task_links
    verify_runtime(bench, args.runtime_copy.resolve())
    sys.path.insert(0, str(args.runtime_copy.resolve()))
    import docker
    import yaml
    from testprism.terminal.terminal import Terminal
    from testprism.agents.base_agent import SystemPromptMode
    from testprism.harness.testbench_system_prompt import TESTBENCH_SYSTEM_PROMPT
    from integrations.testprism.agent import TestHelixAgent
    from integrations.testprism.native_bridge import NativeBridge
    from integrations.testprism.cleanup import remove_and_verify

    selection = json.loads(args.selection.read_text())
    selected = [row for row in selection['tasks'] if row['key'] == args.key]
    if len(selected) != 1 or selected[0].get('role') != 'train':
        raise ValueError('Task must occur exactly once in the explicit training selection')
    entry = selected[0]
    task_dir = Path(entry['stagedTask']).resolve()
    for owned in (out, task_dir, args.agent_project.resolve()):
        if not owned.is_relative_to(PROJECT) or owned.is_relative_to(bench):
            raise ValueError('Use project-owned inputs and output outside the benchmark')
    validate_task_links(task_dir)
    public = yaml.safe_load((task_dir/'task.yaml').read_text())
    if hashlib.sha256(public['instruction'].encode()).hexdigest() != entry['instructionDigest']:
        raise ValueError('Selected public instruction changed')
    config = json.loads(args.config.read_text())
    out.mkdir(parents=True, exist_ok=False)
    client = docker.from_env(timeout=120)
    original = None
    preparation = None
    report = {'key': args.key, 'role': 'train', 'startedAt': now(), 'officialScore': None,
              'referenceRead': False, 'selectionDigest': digest(args.selection)}
    try:
        if args.prepared:
            preparation = json.loads((args.prepared/'preparation.json').read_text())
            if not preparation['complete'] or preparation['key'] != args.key or preparation['instructionDigest'] != entry['instructionDigest']:
                raise ValueError('Prepared task does not match the selected public task')
            images, workdir = preparation['images'], preparation['root']
        else:
            build = json.loads((args.selection.parent/'builds'/entry['source']/'status.json').read_text())
            if build['status'] != 'ready' or build['key'] != args.key:
                raise ValueError('Task image preparation has not completed')
            images = {service['name']: service['imageId'] for service in build['services']}
            image = client.images.get(images['client'])
            workdir = public.get('workdir') or image.attrs['Config'].get('WorkingDir') or '/app'
            if not workdir.startswith('/') or workdir == '/':
                raise ValueError('Public workdir must be an absolute non-root directory')
            container = client.containers.run(image.id, ['-c', 'sleep infinity'], entrypoint='/bin/bash',
                detach=True, network_mode='none', labels={'testhelix.campaign': str(args.selection.parent)})
            try:
                script = 'test -d '+shlex.quote(workdir)+'; find '+shlex.quote(workdir)+' -name .git -prune -exec rm -rf {} +'
                script += '; rm -rf -- ' + ' '.join(shlex.quote(workdir.rstrip('/')+'/'+name) for name in
                    ('agent-tests','solution.sh','patch-sets','.patch-generation-state'))
                checked = container.exec_run(['bash','-e','-c',script], user='root')
                if checked.exit_code: raise RuntimeError('Baseline sanitation failed')
                configuration = {key:image.attrs['Config'].get(key) for key in ('Cmd','Env','WorkingDir','User','Volumes')}
                configuration['Entrypoint'] = image.attrs['Config'].get('Entrypoint') or []
                configuration['Labels'] = {**(image.attrs['Config'].get('Labels') or {}), 'testhelix.campaign':str(args.selection.parent)}
                images['client'] = container.commit(conf=configuration).id
            finally: container.remove(force=True)
        staged = out/'task'
        shutil.copytree(task_dir, staged, symlinks=True)
        compose_file = staged/'docker-compose.yaml'
        compose = yaml.safe_load(compose_file.read_text())
        for name, service in compose['services'].items():
            # Pin every auxiliary service, including names other than "server".
            # Client identity stays parameterized so NativeBridge can own each fork.
            if name != 'client': service['image'] = images[name]
        compose_file.write_text(yaml.safe_dump(compose, sort_keys=False))
        owner = 'ctb-testhelix-' + uuid.uuid4().hex[:16]
        name = owner + '-' + uuid.uuid4().hex[:10]
        write(out/'original-resources.json', {'schemaVersion':1,'owner':owner,'projects':[name],'closed':False,'pending':[name],'operations':{}})
        original = Terminal(client_container_name=name, client_image_name=images['client'],
            server_image_name=images.get('server'), docker_image_name_prefix=name,
            docker_compose_path=compose_file, no_rebuild=True, cleanup=False, disable_recording=True,
            sessions_logs_path=out/'original-sessions', agent_logs_path=out/'original-logs')
        original.start()
        session = original.create_session('original', as_configured_user=True)
        test_timeout = min(float(public.get('max_test_timeout_sec',120)),config['execution']['testTimeoutMs']/1000)
        wall = min(config['budget']['wallMs']/1000,float(public['max_agent_timeout_sec'])-110)
        if wall < 30: raise ValueError('Insufficient public task time for the configured cleanup reserve')
        provenance = {'key':args.key,'role':'train','taskDirectory':entry['taskDirectory'],
            'instructionDigest':entry['instructionDigest'],'imageId':images['client'],'images':images,'root':workdir,
            'env':{},'manifestMember':entry['manifestMember'],'selectionDigest':digest(args.selection),
            'eligibleForEvolutionValidation':False,'officialScore':None,'nativeRuntime':True,
            'configDigest':digest(args.config),'agentWallSeconds':wall,'testTimeoutSeconds':test_timeout}
        provenance['launcherDigests'] = {name:digest(PROJECT/name) for name in
            ('scripts/run-source-task.py','integrations/testprism/native_bridge.py','integrations/testprism/native_worker.py')}
        write(out/'provenance.json',provenance)
        if not args.prepared or args.prepare_only:
            with_bridge = NativeBridge(staged,workdir,original.container,out/'preflight-native',timeout_sec=180)
            try:
                fork = with_bridge.fork('preflight',120000)
                check = with_bridge.execute(fork['id'],'pwd; command -v bash; command -v git; command -v tmux',10000)
                if check['code'] != 0 or check['timedOut']: raise RuntimeError('Native environment preflight failed')
                write(out/'preflight-execution.json',check)
            finally: with_bridge.close()
            write(out/'preparation.json',{**provenance,'complete':True,'paidCalls':0,'preparedAt':now()})
            report['status']='prepared'
        elif args.evaluate:
            manifest = args.evaluate.resolve()
            if not manifest.is_relative_to(args.selection.parent.resolve()):
                raise ValueError('Evaluation manifest must be owned by this campaign')
            frozen = json.loads(manifest.read_text())
            if frozen.get('taskId') != args.key or frozen.get('generationsClosed') is not True:
                raise ValueError('Evaluation requires closed generations for the selected task')
            if frozen.get('images') != images:
                raise ValueError('Evaluation must use the exact frozen service images')
            bridge = NativeBridge(staged,workdir,original.container,out/'evaluation-native',timeout_sec=1800)
            try:
                descriptor = out/'evaluation-runtime.json'
                write(descriptor,{'kind':'testprism','container':original.container.id,'root':workdir,'socket':str(bridge.socket)})
                evaluated = subprocess.run(['node','--import','tsx',str(args.selection.parent/'evaluate-native.ts'),
                    str(manifest),str(descriptor),str(out)],cwd=PROJECT,timeout=1800)
                if evaluated.returncode: raise RuntimeError('Host evaluation failed; inspect its partial audit')
                report['status']='evaluated'
            finally:
                report['referenceRead']=(out/'host-evaluation/reference-audit.json').is_file()
                bridge.close()
        else:
            # A new output directory is the only admission path; no implicit paid retry.
            agent = TestHelixAgent(project_root=str(args.agent_project.resolve()),config=str(args.config.resolve()),
                model_name=config['model']['name'],benchmark_root=str(bench),task_id=args.key,task_source=entry['source'],
                workdir=workdir,native_task_path=staged,system_prompt=TESTBENCH_SYSTEM_PROMPT,
                system_prompt_mode=SystemPromptMode.REPLACE,test_timeout_sec=test_timeout,timeout_sec=wall+20,cleanup_timeout_sec=90)
            result = agent.perform_task(public['instruction'],session,out/'logs')
            report['agentResult']=result.model_dump(mode='json')
            descriptor=out/'logs/testhelix-public-task.json'
            if descriptor.is_file(): shutil.copyfile(descriptor,out/'task.json')
            (out/'run').symlink_to('logs/testhelix-js',target_is_directory=True)
            result_file=out/'run/result.json'
            report['resultDigest']=digest(result_file) if result_file.is_file() else None
            report['status']=json.loads(result_file.read_text())['status'] if result_file.is_file() else 'integration-failed'
            write(out/'generation-freeze.json',{'frozenAt':now(),'resultDigest':report['resultDigest'],'referenceRead':False,'role':'train'})
    finally:
        errors=[]
        if original:
            try: original.stop()
            except Exception as error: errors.append(type(error).__name__)
            try: remove_and_verify(original._compose_manager)
            except Exception as error: errors.append(type(error).__name__)
        client.close()
        write(out/'cleanup.json',{'errors':errors,'completedAt':now()})
        if original and not errors:
            write(out/'original-resources.json', {'schemaVersion':1,'owner':owner,'projects':[name],'closed':True,'pending':[],'operations':{}})
        report['finishedAt']=now();write(out/'summary.json',report)

if __name__ == '__main__': main()
