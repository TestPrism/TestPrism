#!/usr/bin/env python3
"""Restore missing ``tb__...__client`` images from Docker build cache.

The prepull script warms Docker's build cache under ``testprism-prepull-*``
tags, while the harness runs with ``tb__<task_id>__client`` tags. If a run with
cleanup enabled removed the harness tags, this script rebuilds those harness
tags. By default it resolves Docker Compose first, then uses the prepull-style
``docker build --network=host`` path so Dockerfile ``RUN`` steps can reach a
host-local proxy.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from testprism.terminal.docker_compose_manager import DockerComposeManager


@dataclass(frozen=True)
class RestoreJob:
    task_id: str
    image_tag: str
    task_dir: Path
    compose_path: Path


@dataclass(frozen=True)
class BuildSpec:
    service: str
    context: Path
    dockerfile: Path
    image: str
    build_args: dict[str, str]
    target: str | None = None


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-_") or "x"


def _image_exists(image_tag: str) -> bool:
    return (
        subprocess.run(
            ["docker", "image", "inspect", image_tag],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def _host_build_proxy(cli_proxy: str | None) -> str | None:
    return (
        cli_proxy
        or os.environ.get("TESTPRISM_DOCKER_BUILD_PROXY")
        or os.environ.get("TESTPRISM_BUILD_PROXY")
        or os.environ.get("TESTPRISM_RUNTIME_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
    )


def _host_build_proxy_args(build_proxy: str | None) -> dict[str, str]:
    if not build_proxy:
        return {}
    return {
        "HTTP_PROXY": build_proxy,
        "HTTPS_PROXY": build_proxy,
        "http_proxy": build_proxy,
        "https_proxy": build_proxy,
        "NO_PROXY": "localhost,127.0.0.0/8,::1",
        "no_proxy": "localhost,127.0.0.0/8,::1",
    }


def _configure_direct_build_env(env: dict[str, str], build_proxy: str | None) -> None:
    for key in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "FTP_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "ftp_proxy",
        "all_proxy",
        "no_proxy",
    ):
        env.pop(key, None)
    env.update(_host_build_proxy_args(build_proxy))
    env["DOCKER_BUILDKIT"] = env.get("DOCKER_BUILDKIT", "0")
    env["COMPOSE_DOCKER_CLI_BUILD"] = env.get("COMPOSE_DOCKER_CLI_BUILD", "1")


def _read_missing_report(path: Path, taskset: Path) -> list[RestoreJob]:
    jobs: list[RestoreJob] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row in reader:
            task_id = (row.get("task_id") or "").strip()
            image_tag = (row.get("image_tag") or "").strip()
            if not task_id or not image_tag:
                continue

            task_link = taskset / task_id
            task_dir = task_link.resolve()
            compose_path = task_dir / "docker-compose.yaml"
            if not compose_path.exists():
                compose_path = task_dir / "docker-compose.yml"
            if not compose_path.exists():
                raise FileNotFoundError(f"No compose file for {task_id}: {task_dir}")

            jobs.append(
                RestoreJob(
                    task_id=task_id,
                    image_tag=image_tag,
                    task_dir=task_dir,
                    compose_path=compose_path,
                )
            )
    return jobs


def _resolve_build_specs(
    manager: DockerComposeManager,
    job: RestoreJob,
) -> list[BuildSpec]:
    proc = subprocess.run(
        [
            *manager.get_docker_compose_command([]),
            "config",
            "--format",
            "json",
        ],
        cwd=str(job.task_dir),
        env=manager.env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        raise RuntimeError(f"docker compose config failed: {message}")

    config = json.loads(proc.stdout)
    services = config.get("services")
    if not isinstance(services, dict) or not services:
        raise RuntimeError("docker compose config did not contain services")

    specs: list[BuildSpec] = []
    for service_name, service in services.items():
        if not isinstance(service, dict):
            continue
        build = service.get("build")
        if not build:
            continue

        if isinstance(build, str):
            context = Path(build)
            dockerfile = context / "Dockerfile"
            build_args: dict[str, str] = {}
            target = None
        elif isinstance(build, dict):
            context = Path(str(build.get("context") or job.task_dir))
            dockerfile_value = str(build.get("dockerfile") or "Dockerfile")
            dockerfile = Path(dockerfile_value)
            if not dockerfile.is_absolute():
                dockerfile = context / dockerfile

            args_value = build.get("args") or {}
            if isinstance(args_value, dict):
                build_args = {str(k): str(v) for k, v in args_value.items()}
            elif isinstance(args_value, list):
                build_args = {}
                for item in args_value:
                    key, sep, value = str(item).partition("=")
                    if key:
                        build_args[key] = value if sep else manager.env.get(key, "")
            else:
                build_args = {}
            target = str(build["target"]) if build.get("target") else None
        else:
            continue

        image = str(
            service.get("image")
            or f"restore-{_slug(job.task_id)}-{_slug(str(service_name))}:latest"
        )
        specs.append(
            BuildSpec(
                service=str(service_name),
                context=context,
                dockerfile=dockerfile,
                image=image,
                build_args=build_args,
                target=target,
            )
        )

    if not specs:
        raise RuntimeError("docker compose config did not contain buildable services")
    return specs


def _build_direct_commands(
    manager: DockerComposeManager,
    job: RestoreJob,
    *,
    build_proxy: str | None,
    network_host: bool,
    pull_base: bool,
) -> list[list[str]]:
    specs = _resolve_build_specs(manager, job)
    commands: list[list[str]] = []
    proxy_args = _host_build_proxy_args(build_proxy)
    for spec in specs:
        cmd = ["docker", "build"]
        if network_host:
            cmd.append("--network=host")
        if pull_base:
            cmd.append("--pull")
        if spec.target:
            cmd.extend(["--target", spec.target])

        build_args = dict(spec.build_args)
        build_args.update(proxy_args)
        for key, value in sorted(build_args.items()):
            cmd.extend(["--build-arg", f"{key}={value}"])

        cmd.extend(
            ["-f", str(spec.dockerfile), "-t", spec.image, str(spec.context)]
        )
        commands.append(cmd)

    return commands


def _restore_one(
    job: RestoreJob,
    output_dir: Path,
    force: bool,
    *,
    build_network_host: bool,
    build_proxy: str | None,
    pull_base: bool,
    timeout: int,
) -> tuple[str, str]:
    log_path = output_dir / f"{_slug(job.task_id)}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    if not force and _image_exists(job.image_tag):
        log_path.write_text(
            f"[skip] already exists: {job.image_tag}\n",
            encoding="utf-8",
        )
        return job.task_id, "skipped"

    manager = DockerComposeManager(
        client_container_name=f"restore-{_slug(job.task_id)}",
        client_image_name=job.image_tag,
        docker_image_name_prefix=f"tb__{_slug(job.task_id)}",
        docker_compose_path=job.compose_path,
        no_rebuild=False,
        cleanup=False,
        sessions_logs_path=output_dir / job.task_id / "sessions",
        agent_logs_path=output_dir / job.task_id / "agent-logs",
    )
    _configure_direct_build_env(manager.env, build_proxy)

    try:
        manager._prepare_runtime_assets()
        if build_network_host:
            commands = _build_direct_commands(
                manager,
                job,
                build_proxy=build_proxy,
                network_host=True,
                pull_base=pull_base,
            )
        else:
            cmd = manager.get_docker_compose_command(["build"])
            if pull_base:
                cmd.append("--pull")
            commands = [cmd]

        start = time.monotonic()
        header = (
            f"# task: {job.task_id}\n"
            f"# image: {job.image_tag}\n"
            f"# compose: {job.compose_path}\n"
            f"# cwd: {job.task_dir}\n"
            f"# build_network_host: {build_network_host}\n"
            f"# build_proxy: {build_proxy or ''}\n"
            f"# cmd: {' && '.join(' '.join(cmd) for cmd in commands)}\n"
            f"# started_at: {datetime.now(timezone.utc).isoformat()}\n"
            f"{'=' * 72}\n"
        )
        with log_path.open("w", encoding="utf-8") as log:
            log.write(header)
            log.flush()
            rc = 0
            deadline = time.monotonic() + timeout
            for cmd in commands:
                log.write(f"\n$ {' '.join(cmd)}\n")
                log.flush()
                remaining = max(1, int(deadline - time.monotonic()))
                try:
                    proc = subprocess.run(
                        cmd,
                        cwd=str(job.task_dir),
                        env=manager.env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        timeout=remaining,
                    )
                    rc = proc.returncode
                except subprocess.TimeoutExpired:
                    log.write(f"\n[timeout] after {timeout}s\n")
                    rc = -1
                if rc != 0:
                    break
            duration = time.monotonic() - start
            log.write(f"\n# duration_sec: {duration:.2f}\n")
        if rc != 0:
            raise RuntimeError(f"docker build failed with exit code {rc}")
        with log_path.open("a", encoding="utf-8") as log:
            log.write("\n[ok] restored\n")
        return job.task_id, "ok"
    except Exception as exc:
        with log_path.open("a", encoding="utf-8") as log:
            log.write(f"\n[failed] {type(exc).__name__}: {exc}\n")
        return job.task_id, "failed"
    finally:
        manager._cleanup_temp_files()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Rebuild missing harness tb__...__client images from cache."
    )
    parser.add_argument(
        "--missing-report",
        type=Path,
        default=Path(
            "/tmp/testprism_missing_harness_client_images_20260520-000358.tsv"
        ),
        help="TSV with task_id and image_tag columns.",
    )
    parser.add_argument(
        "--taskset",
        type=Path,
        default=(
            REPO_ROOT
            / "runs/_cloudgpt_pool/cloudgpt-pool-20260520-000358/taskset"
        ),
        help="Flattened taskset directory used by the run.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "runs/_restore_tb_client_images",
        help="Directory for restore logs.",
    )
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument(
        "--timeout",
        type=int,
        default=DockerComposeManager.BUILD_TIMEOUT_SEC,
        help="Per-task build timeout in seconds.",
    )
    parser.add_argument("--limit", type=int, default=0, help="0 means no limit.")
    parser.add_argument("--force", action="store_true")
    parser.add_argument(
        "--no-build-network-host",
        action="store_true",
        help="Use docker compose build instead of direct docker build --network=host.",
    )
    parser.add_argument(
        "--build-proxy",
        default=None,
        help=(
            "Proxy URL to inject into Dockerfile RUN steps, e.g. "
            "http://127.0.0.1:7890. Defaults to TESTPRISM_* proxy env vars, "
            "then HTTP(S)_PROXY, then DockerComposeManager's default."
        ),
    )
    parser.add_argument(
        "--pull",
        action="store_true",
        help="Pass --pull to docker build, forcing base image registry checks.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    jobs = _read_missing_report(args.missing_report, args.taskset)
    if args.limit:
        jobs = jobs[: args.limit]

    print(f"restore jobs: {len(jobs)}")
    print(f"logs: {args.output_dir}")
    build_network_host = not args.no_build_network_host
    build_proxy = _host_build_proxy(args.build_proxy)
    print(
        f"build_network_host={build_network_host}  "
        f"build_proxy={build_proxy or ''}  pull_base={args.pull}"
    )
    if not jobs:
        return 0

    counts = {"ok": 0, "skipped": 0, "failed": 0}
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [
            executor.submit(
                _restore_one,
                job,
                args.output_dir,
                args.force,
                build_network_host=build_network_host,
                build_proxy=build_proxy,
                pull_base=args.pull,
                timeout=args.timeout,
            )
            for job in jobs
        ]
        for future in as_completed(futures):
            task_id, status = future.result()
            counts[status] += 1
            print(f"[{status}] {task_id}")

    print(
        "summary: "
        + ", ".join(f"{key}={value}" for key, value in sorted(counts.items()))
    )
    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
