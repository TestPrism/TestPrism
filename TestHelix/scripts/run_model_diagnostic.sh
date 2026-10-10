#!/usr/bin/env bash
# Development task only. Reads public task.yaml; never mounts benchmark answers.
set -euo pipefail
cd "$(dirname "$0")/.."
diagnostic_out=${1:-".runs/model-diagnostic-$(date +%Y%m%d-%H%M%S)"}
credential_file=${TESTHELIX_ENV_FILE:-.env.local}
# Node fetch must honor the proxy already configured by the caller (explicit 0 is preserved).
if [[ -n "${HTTPS_PROXY:-${https_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}" ]]; then
  export NODE_USE_ENV_PROXY="${NODE_USE_ENV_PROXY:-1}"
fi
diagnostic_config=${TESTHELIX_CONFIG:-configs/experiments/hardest500.gpt55-diagnostic.toml}
test -r "$credential_file" || { echo "Missing credential env file: $credential_file" >&2; exit 1; }
test -r "$diagnostic_config" || { echo "Missing model config: $diagnostic_config" >&2; exit 1; }
test ! -e "$diagnostic_out" || { echo "Use a new output directory: $diagnostic_out" >&2; exit 1; }
mkdir -p "$diagnostic_out"
node --input-type=module - "$diagnostic_out" <<'JS'
import fs from 'node:fs';
import {parse} from 'yaml';
const id = 'from-swe-bench/django__django-11087';
const holdout = fs.readFileSync('TestPrism-main/verified_tasks/hardest_500.json', 'utf8');
if (holdout.includes('django__django-11087')) throw new Error('Diagnostic task overlaps hardest500');
const task = parse(fs.readFileSync('TestPrism-main/verified_tasks_flat_test/from-swe-bench__django__django-11087/task.yaml', 'utf8'));
fs.writeFileSync(process.argv[2] + '/public-task.json', JSON.stringify({id, source: 'from-swe-bench', instruction: task.instruction,
  root: task.workdir, testTimeoutMs: 120000}));
JS
npm run build
diagnostic_image=swebench/sweb.eval.x86_64.django_1776_django-11087:latest
docker image inspect "$diagnostic_image" >/dev/null 2>&1 || docker pull "$diagnostic_image"
diagnostic_container=$(docker run -d --network none --label testhelix.diagnostic=public-development \
  -e PATH=/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  --entrypoint /bin/bash "$diagnostic_image" -c 'sleep infinity')
trap 'docker rm -f "$diagnostic_container" >/dev/null 2>&1 || true' EXIT
docker exec "$diagnostic_container" rm -rf /testbed/.git
node --input-type=module - "$diagnostic_out" "$diagnostic_container" "$diagnostic_image" "$diagnostic_config" <<'JS'
import fs from 'node:fs';
import {execFileSync} from 'node:child_process';
import {createHash} from 'node:crypto';
const {root, ...task} = JSON.parse(fs.readFileSync(process.argv[2] + '/public-task.json', 'utf8'));
fs.writeFileSync(process.argv[2] + '/task.json', JSON.stringify({...task, runtime: {kind: 'docker', container: process.argv[3], root}}, null, 2));
const inspect = JSON.parse(execFileSync('docker', ['inspect', process.argv[3]], {encoding: 'utf8'}))[0];
fs.writeFileSync(process.argv[2] + '/provenance.json', JSON.stringify({schemaVersion: 1, preparedAt: new Date().toISOString(),
  kind: 'public-development-diagnostic', eligibleForEvolutionValidation: false, excludesHardest500: true,
  taskDirectory: 'TestPrism-main/verified_tasks_flat_test/from-swe-bench__django__django-11087',
  image: process.argv[4], imageId: inspect.Image, root,
  env: {PATH: '/opt/miniconda3/envs/testbed/bin:/opt/miniconda3/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'},
  configDigest: createHash('sha256').update(fs.readFileSync(process.argv[5])).digest('hex')}, null, 2));
JS
node --env-file="$credential_file" dist/cli.js run --task "$diagnostic_out/task.json" \
  --config "$diagnostic_config" --out "$diagnostic_out/run"
