"""Recover only journaled TestHelix native projects after a killed harness."""
import json
from pathlib import Path
import re


def remove_and_verify(manager):
    """Native best-effort deletion is not a receipt; verify all owned resources."""
    manager._force_remove_compose_project()
    filters = {"label": "com.docker.compose.project=" + manager._client_container_name}
    if (manager._client.containers.list(all=True, filters=filters)
            or manager._client.networks.list(filters=filters) or manager._client.volumes.list(filters=filters)):
        raise RuntimeError("Native resources remain for owned project " + manager._client_container_name)


def cleanup_native(journal, manager_type=None):
    if manager_type is None:
        from testprism.terminal.docker_compose_manager import DockerComposeManager
        manager_type = DockerComposeManager
    journal = Path(journal)
    state = json.loads(journal.read_text())
    owner = state.get("owner", "")
    if state.get("schemaVersion") != 1 or not re.fullmatch(r"ctb-testhelix-[a-f0-9]{16}", owner):
        raise ValueError("Invalid native ownership journal")
    projects = state.get("projects")
    if not isinstance(projects, list) or any(not isinstance(p, str) or not re.fullmatch(re.escape(owner) + r"-[a-f0-9]{10}", p) for p in projects):
        raise ValueError("Native journal includes a project outside its owner")
    for project in projects:
        manager = manager_type(client_container_name=project, client_image_name="unused",
            docker_compose_path=journal.parent / project / "task/docker-compose.yaml", no_rebuild=True, cleanup=False)
        remove_and_verify(manager)
    state["recovered"] = True
    temporary = journal.with_suffix(".cleanup.tmp")
    temporary.write_text(json.dumps(state, indent=2))
    temporary.replace(journal)
    return len(projects)
