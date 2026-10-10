from pathlib import Path

from testprism.handlers.trial_handler import TrialHandler


def _write_task_fixture(task_dir: Path, compose_text: str) -> None:
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "task.yaml").write_text("instruction: demo\ndifficulty: easy\n")
    (task_dir / "docker-compose.yaml").write_text(compose_text)


def test_trial_handler_omits_server_image_when_compose_has_only_client(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "demo-task"
    _write_task_fixture(
        task_dir,
        """\
services:
  client:
    image: demo-client
""",
    )

    handler = TrialHandler("trial-1", task_dir, task_id="from-demo/no-server")

    assert handler.has_server_service is False
    assert handler.compose_service_names == {"client"}
    assert handler.server_image_name is None


def test_trial_handler_exposes_server_image_when_server_service_exists(
    tmp_path: Path,
) -> None:
    task_dir = tmp_path / "demo-task"
    _write_task_fixture(
        task_dir,
        """\
services:
  client:
    image: demo-client
  server:
    image: demo-server
""",
    )

    handler = TrialHandler("trial-1", task_dir, task_id="from-demo/with-server")

    assert handler.has_server_service is True
    assert handler.compose_service_names == {"client", "server"}
    assert handler.server_image_name == f"{handler.docker_image_name_prefix}__server"
