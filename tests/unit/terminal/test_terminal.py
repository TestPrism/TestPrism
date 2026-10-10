from unittest.mock import Mock

import pytest
from docker.models.containers import Container

from testprism.terminal.terminal import Terminal
from testprism.terminal.tmux_session import (
    TmuxSendKeysError,
    TmuxSession,
    TmuxSessionStartupError,
)


@pytest.fixture
def mock_container():
    container = Mock(spec=Container)
    container.exec_run.return_value = Mock(exit_code=0, output=b"mock output")
    return container


@pytest.fixture
def tmux_session(mock_container):
    return TmuxSession(session_name="test-session", container=mock_container)


def test_prepare_keys_single_string(tmux_session):
    """Test preparing a single string key."""
    result, is_blocking = tmux_session._prepare_keys("ls", block=False)
    assert result == ["ls"]
    assert is_blocking is False


def test_prepare_keys_list(tmux_session):
    """Test preparing a list of keys."""
    result, is_blocking = tmux_session._prepare_keys(["cd", "dir"], block=False)
    assert result == ["cd", "dir"]
    assert is_blocking is False


def test_prepare_keys_non_blocking_with_enter(tmux_session):
    """Test non-blocking keys ending with Enter."""
    result, is_blocking = tmux_session._prepare_keys(["ls", "Enter"], block=False)
    assert result == ["ls", "Enter"]
    assert is_blocking is False


def test_prepare_keys_blocking_with_enter(tmux_session):
    """Test blocking keys ending with Enter."""
    result, is_blocking = tmux_session._prepare_keys(["ls", "Enter"], block=True)
    assert result == ["ls", tmux_session._TMUX_COMPLETION_COMMAND, "Enter"]
    assert is_blocking is True


def test_prepare_keys_blocking_with_newline(tmux_session):
    """Test blocking keys ending with newline."""
    result, is_blocking = tmux_session._prepare_keys(["echo 'hello'\n"], block=True)
    assert result == ["echo 'hello'", tmux_session._TMUX_COMPLETION_COMMAND, "Enter"]
    assert is_blocking is True


def test_prepare_keys_multiple_enters(tmux_session):
    """Test handling multiple Enter keys in blocking mode."""
    result, is_blocking = tmux_session._prepare_keys(
        ["ls", "Enter", "Enter"], block=True
    )
    assert result == ["ls", "; tmux wait -S done", "Enter"]
    assert is_blocking is True


def test_prepare_keys_only_newlines(tmux_session):
    """Test handling input with only newlines in blocking mode."""
    result, is_blocking = tmux_session._prepare_keys(["\n", "\r\n"], block=True)
    assert result == [tmux_session._TMUX_COMPLETION_COMMAND, "Enter"]
    assert is_blocking is True


def test_prepare_keys_empty_list(tmux_session):
    """Test preparing an empty list of keys."""
    result, is_blocking = tmux_session._prepare_keys([], block=True)
    assert result == []
    assert is_blocking is False


def test_prepare_keys_blocking_without_enter(tmux_session):
    """Test preparing a blocking command without an Enter key."""
    result, is_blocking = tmux_session._prepare_keys(["ls"], block=True)
    assert result == ["ls"]
    assert is_blocking is False


def test_prepare_keys_mixed_newlines_and_content(tmux_session):
    """Test handling mixed content with newlines in blocking mode."""
    result, is_blocking = tmux_session._prepare_keys(
        ["echo 'hello'\n", "world\n"], block=True
    )
    assert result == ["echo 'hello'\n", "world", "; tmux wait -S done", "Enter"]
    assert is_blocking is True


def test_prepare_keys_carriage_return(tmux_session):
    """Test handling carriage returns in blocking mode."""
    result, is_blocking = tmux_session._prepare_keys(["echo 'hello'\r"], block=True)
    assert result == ["echo 'hello'", "; tmux wait -S done", "Enter"]
    assert is_blocking is True


def test_blocking_send_keys_failure_raises_immediately(tmux_session, mock_container):
    mock_container.reset_mock()
    mock_container.exec_run.return_value = Mock(
        exit_code=1, output=b"send-keys failed badly"
    )

    with pytest.raises(TmuxSendKeysError) as exc_info:
        tmux_session.send_keys(["echo 'hello'", "Enter"], block=True)

    assert "send-keys failed badly" in str(exc_info.value)
    assert mock_container.exec_run.call_count == 1


def test_non_blocking_send_keys_failure_raises(tmux_session, mock_container):
    mock_container.reset_mock()
    mock_container.exec_run.return_value = Mock(
        exit_code=1, output=b"non-blocking send failure"
    )

    with pytest.raises(TmuxSendKeysError) as exc_info:
        tmux_session.send_keys(["echo 'hello'", "Enter"], block=False)

    assert "non-blocking send failure" in str(exc_info.value)


def test_start_waits_for_recording_shell_before_sending_clear(
    tmux_session, monkeypatch
):
    exec_run = Mock(return_value=Mock(exit_code=0, output=b""))
    send_keys = Mock()
    monkeypatch.setattr(tmux_session, "_exec_run", exec_run)
    monkeypatch.setattr(tmux_session, "is_session_alive", Mock(return_value=True))
    monkeypatch.setattr(tmux_session, "send_keys", send_keys)

    tmux_session.start()

    recording_keys = send_keys.call_args_list[0].kwargs["keys"]
    assert "asciinema rec --stdin -c " in recording_keys[0]
    assert f"tmux wait -S {tmux_session._recording_ready_channel}" in recording_keys[0]
    assert recording_keys[1] == "Enter"
    exec_run.assert_any_call(
        [
            "timeout",
            f"{tmux_session._RECORDING_STARTUP_TIMEOUT_SEC}s",
            "tmux",
            "wait",
            tmux_session._recording_ready_channel,
        ]
    )
    assert send_keys.call_args_list[1].kwargs["keys"] == ["clear", "Enter"]


def test_start_fails_if_recording_shell_never_becomes_ready(tmux_session, monkeypatch):
    exec_run = Mock(
        side_effect=[
            Mock(exit_code=0, output=b""),
            Mock(exit_code=124, output=b"recording startup timed out"),
        ]
    )
    send_keys = Mock()
    monkeypatch.setattr(tmux_session, "_exec_run", exec_run)
    monkeypatch.setattr(tmux_session, "is_session_alive", Mock(return_value=True))
    monkeypatch.setattr(tmux_session, "send_keys", send_keys)

    with pytest.raises(TmuxSessionStartupError, match="recording shell"):
        tmux_session.start()

    assert send_keys.call_count == 1


def test_terminal_stop_continues_when_recording_teardown_fails():
    terminal = object.__new__(Terminal)
    session = Mock()
    session._session_name = "stopped-session"
    session.stop.side_effect = TmuxSendKeysError("tmux session is gone")
    terminal._sessions = {"stopped-session": session}
    terminal._compose_manager = Mock()
    terminal._livestreamer = None
    terminal._logger = Mock()

    terminal.stop()

    terminal._compose_manager.stop.assert_called_once_with()
    terminal._logger.warning.assert_called_once()
    assert terminal._sessions == {}
