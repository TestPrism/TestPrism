from contextlib import contextmanager
from pathlib import Path
from typing import Any, Generator

from testprism.terminal.docker_compose_manager import DockerComposeManager
from testprism.terminal.tmux_session import TmuxSendKeysError, TmuxSession
from testprism.utils.livestreamer import Livestreamer
from testprism.utils.logger import logger


class Terminal:
    def __init__(
        self,
        client_container_name: str,
        client_image_name: str,
        docker_compose_path: Path,
        docker_image_name_prefix: str | None = None,
        sessions_logs_path: Path | None = None,
        agent_logs_path: Path | None = None,
        commands_path: Path | None = None,
        no_rebuild: bool = False,
        cleanup: bool = False,
        livestream: bool = False,
        disable_recording: bool = False,
        agent_name: str | None = None,
        agent_kwargs: dict[str, Any] | None = None,
        server_image_name: str | None = None,
    ):
        self._client_container_name = client_container_name
        self._docker_image_name_prefix = docker_image_name_prefix
        self._docker_compose_path = docker_compose_path
        self._sessions_logs_path = sessions_logs_path
        self._agent_logs_path = agent_logs_path
        self._commands_path = commands_path
        self._livestream = livestream
        self._disable_recording = disable_recording

        self._logger = logger.getChild(__name__)
        self._sessions: dict[str, TmuxSession] = {}

        self._compose_manager = DockerComposeManager(
            client_container_name=client_container_name,
            client_image_name=client_image_name,
            docker_image_name_prefix=docker_image_name_prefix,
            docker_compose_path=docker_compose_path,
            no_rebuild=no_rebuild,
            cleanup=cleanup,
            sessions_logs_path=sessions_logs_path,
            agent_logs_path=agent_logs_path,
            agent_name=agent_name,
            agent_kwargs=agent_kwargs,
            server_image_name=server_image_name,
        )

        self._init_livestreamer()
        self.container = None

    def _init_livestreamer(self) -> None:
        self._livestreamer = None

        if not self._livestream:
            return

        if not self._sessions_logs_path:
            raise ValueError("sessions_logs_path is required to livestream.")

        self._livestreamer = Livestreamer()

    def create_session(
        self, session_name: str, is_active_stream: bool = False, as_configured_user=True
    ) -> TmuxSession:
        """Create a new tmux session with the given name."""
        if self.container is None:
            raise ValueError("Container not started. Run start() first.")

        if session_name in self._sessions:
            raise ValueError(f"Session {session_name} already exists")

        if as_configured_user:
            user = self.container.attrs["Config"].get("User", "")
        else:
            user = "root"

        session = TmuxSession(
            session_name=session_name,
            container=self.container,
            commands_path=self._commands_path,
            disable_recording=self._disable_recording,
            user=user,
        )

        self._sessions[session_name] = session

        if is_active_stream:
            self.set_active_stream(session_name)

        session.start()

        return session

    def get_session(self, session_name: str) -> TmuxSession:
        """Get an existing tmux session by name."""
        if session_name not in self._sessions:
            raise ValueError(f"Session {session_name} does not exist")
        return self._sessions[session_name]

    def start(self) -> None:
        self.container = self._compose_manager.start()

    def stop(self) -> None:
        for session in self._sessions.values():
            try:
                session.stop()
            except TmuxSendKeysError as error:
                # A terminal command may have already ended the tmux session.
                # Recording teardown is best-effort and must not hide the
                # Agent's real result or prevent container cleanup.
                self._logger.warning(
                    "Failed to stop tmux recording for session %s: %s",
                    session._session_name,
                    error,
                )

        self._compose_manager.stop()

        if self._livestreamer:
            self._livestreamer.stop()

        self._sessions.clear()

    def copy_to_container(
        self,
        paths: list[Path] | Path,
        container_dir: str | None = None,
        container_filename: str | None = None,
    ):
        self._compose_manager.copy_to_client_container(
            paths=paths,
            container_dir=container_dir,
            container_filename=container_filename,
        )

    def copy_from_container(
        self,
        container_path: str,
        host_dir: Path,
    ) -> None:
        """Extract a file or directory from the container to the host."""
        import io
        import tarfile

        if self.container is None:
            raise ValueError("Container not started. Run start() first.")

        bits, _ = self.container.get_archive(container_path)
        stream = io.BytesIO()
        for chunk in bits:
            stream.write(chunk)
        stream.seek(0)

        host_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=stream) as tar:
            tar.extractall(path=str(host_dir))

    def set_active_stream(self, session_name: str) -> None:
        """Set which session to livestream, or None to stop streaming."""
        if not self._livestreamer or not self._sessions_logs_path:
            return

        if session_name not in self._sessions:
            raise ValueError(f"Session '{session_name}' does not exist")

        print(f"\nSwitching livestream to tmux session '{session_name}'")

        session = self._sessions[session_name]

        self._livestreamer.change_livestream_path(
            self._sessions_logs_path
            / session.logging_path.relative_to(
                DockerComposeManager.CONTAINER_SESSION_LOGS_PATH
            )
        )


@contextmanager
def spin_up_terminal(
    client_container_name: str,
    client_image_name: str,
    docker_compose_path: Path,
    docker_image_name_prefix: str | None = None,
    sessions_logs_path: Path | None = None,
    agent_logs_path: Path | None = None,
    commands_path: Path | None = None,
    no_rebuild: bool = False,
    cleanup: bool = False,
    livestream: bool = False,
    disable_recording: bool = False,
    agent_name: str | None = None,
    agent_kwargs: dict[str, Any] | None = None,
    server_image_name: str | None = None,
) -> Generator[Terminal, None, None]:
    terminal = Terminal(
        client_container_name=client_container_name,
        client_image_name=client_image_name,
        docker_image_name_prefix=docker_image_name_prefix,
        docker_compose_path=docker_compose_path,
        sessions_logs_path=sessions_logs_path,
        agent_logs_path=agent_logs_path,
        commands_path=commands_path,
        no_rebuild=no_rebuild,
        cleanup=cleanup,
        livestream=livestream,
        disable_recording=disable_recording,
        agent_name=agent_name,
        agent_kwargs=agent_kwargs,
        server_image_name=server_image_name,
    )

    try:
        terminal.start()
        yield terminal
    finally:
        terminal.stop()
