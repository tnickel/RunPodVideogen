"""Offline regressions for command output draining; no SSH or user config access."""

from collections import deque
from types import SimpleNamespace

import pytest
import paramiko
import threading
import time

import backend.ssh_client as ssh_module
from backend.ssh_client import SSHManager


class FakeChannel:
    def __init__(self, stdout=(), stderr=(), *, finished=True, exit_code=0):
        self.stdout = deque(stdout)
        self.stderr = deque(stderr)
        self.finished = finished
        self.exit_code = exit_code
        self.reads = []
        self.exit_reads = 0

    def recv_ready(self):
        return bool(self.stdout)

    def recv_stderr_ready(self):
        return bool(self.stderr)

    def recv(self, size):
        self.reads.append("stdout")
        return self.stdout.popleft()

    def recv_stderr(self, size):
        self.reads.append("stderr")
        return self.stderr.popleft()

    def exit_status_ready(self):
        # Deliberately signal completion even when data remains buffered.
        return self.finished

    def recv_exit_status(self):
        assert not self.stdout and not self.stderr, "Exit read before draining output"
        self.exit_reads += 1
        return self.exit_code


class FakeClient:
    def __init__(self, channel, error=None):
        self.channel = channel
        self.error = error
        self.closed = 0
        self.command = None

    def exec_command(self, cmd, timeout):
        self.command = (cmd, timeout)
        if self.error:
            raise self.error
        return None, SimpleNamespace(channel=self.channel), None

    def close(self):
        self.closed += 1


def manager_for(channel, error=None):
    # Avoid creating keys or making connections in SSHManager.__init__.
    manager = object.__new__(SSHManager)
    client = FakeClient(channel, error=error)
    manager.get_client = lambda **kwargs: client
    return manager, client


@pytest.fixture
def clock(monkeypatch):
    current = [100.0]
    monkeypatch.setattr(ssh_module.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(ssh_module.time, "sleep", lambda seconds: current.__setitem__(0, current[0] + seconds))
    return current


def test_stream_interleaves_utf8_crlf_and_flushes_trailing_lines():
    channel = FakeChannel(
        stdout=[b"Start \xe2", b"\x82\xac\r", b"\n50%\rDone"],
        stderr=[b"warn\n", b"bad\xe2", b"\x82\xac\nlast"],
        exit_code=7,
    )
    manager, client = manager_for(channel)
    lines = []

    result = manager.exec_command_stream("bootstrap", on_line=lines.append)

    assert result == (7, "Start €\r\n50%\rDone", "warn\nbad€\nlast")
    assert lines == ["warn", "Start €", "50%", "bad€", "Done", "last"]
    assert channel.reads == ["stdout", "stderr"] * 3
    assert channel.exit_reads == 1
    assert client.closed == 1


def test_stream_preserves_blank_lines_and_replaces_incomplete_utf8():
    channel = FakeChannel(stdout=[b"\r", b"\n", b"\n\r", b"\nend\xe2"])
    manager, client = manager_for(channel)
    lines = []

    assert manager.exec_command_stream("test", on_line=lines.append) == (0, "\r\n\n\r\nend�", "")
    assert lines == ["", "", "", "end�"]
    assert client.closed == 1


def test_stream_bounds_tails_and_unterminated_lines(monkeypatch):
    monkeypatch.setattr(ssh_module, "_STREAM_TAIL_CHARS", 12)
    manager, client = manager_for(FakeChannel(
        stdout=[b"prefix\n", b"a" * 100, b"finish"],
        stderr=[b"b" * 100, b"error\n"],
    ))
    lines = []

    code, stdout, stderr = manager.exec_command_stream("large-output", on_line=lines.append)

    assert code == 0
    assert stdout == "a" * 6 + "finish"
    assert stderr == "b" * 6 + "error\n"
    assert max(map(len, lines)) <= 12
    assert lines[0] == "prefix"
    assert client.closed == 1


def test_stream_timeout_is_absolute_and_closes_client(clock):
    channel = FakeChannel(finished=False)
    manager, client = manager_for(channel)

    with pytest.raises(TimeoutError, match="exceeded 2 seconds"):
        manager.exec_command_stream("never-finishes", timeout=2)

    assert clock[0] == pytest.approx(102.0)
    assert channel.exit_reads == 0
    assert client.closed == 1


def test_active_output_does_not_extend_deadline(clock):
    channel = FakeChannel(stdout=[b"still working\n"] * 20, finished=False)
    manager, client = manager_for(channel)

    def on_line(_):
        clock[0] += 0.4

    with pytest.raises(TimeoutError):
        manager.exec_command_stream("busy-forever", timeout=1, on_line=on_line)

    assert len(channel.stdout) == 17
    assert client.closed == 1


def test_final_line_callback_also_observes_deadline(clock):
    manager, client = manager_for(FakeChannel(stdout=[b"last line"]))

    def on_line(_):
        clock[0] += 2

    with pytest.raises(TimeoutError):
        manager.exec_command_stream("test", timeout=1, on_line=on_line)

    assert client.closed == 1


def test_stream_cancel_during_output_closes_client():
    channel = FakeChannel(stdout=[b"first\n", b"second\n"], finished=False)
    manager, client = manager_for(channel)
    lines = []

    with pytest.raises(InterruptedError, match="cancelled"):
        manager.exec_command_stream("test", on_line=lines.append, cancelled=lambda: bool(lines))

    assert lines == ["first"]
    assert client.closed == 1
    assert channel.exit_reads == 0


def test_cancel_before_connect_does_not_open_connection():
    manager, client = manager_for(FakeChannel())

    with pytest.raises(InterruptedError):
        manager.exec_command_stream("test", cancelled=lambda: True)

    assert client.command is None
    assert client.closed == 0


@pytest.mark.parametrize("failure_source", ["command", "callback"])
def test_stream_closes_client_if_command_or_callback_raises(failure_source):
    error = RuntimeError("expected failure")
    manager, client = manager_for(
        FakeChannel(stdout=[b"line\n"]),
        error=error if failure_source == "command" else None,
    )

    def broken_callback(_):
        raise error

    with pytest.raises(RuntimeError, match="expected failure"):
        manager.exec_command_stream("test", on_line=broken_callback)

    assert client.closed == 1


def test_connection_setup_counts_toward_deadline(clock):
    manager, client = manager_for(FakeChannel())

    def connect(**kwargs):
        clock[0] += 5
        return client

    manager.get_client = connect
    with pytest.raises(TimeoutError):
        manager.exec_command_stream("test", timeout=2)

    assert client.command is None
    assert client.closed == 1


def test_nonpositive_timeout_is_rejected_without_connection():
    manager, client = manager_for(FakeChannel())
    with pytest.raises(ValueError, match="positive"):
        manager.exec_command_stream("test", timeout=0)
    assert client.command is None


def test_failed_connection_closes_allocated_ssh_client(monkeypatch):
    manager = object.__new__(SSHManager)
    manager.host = "test.invalid"
    manager.port = 22
    manager.priv_key_str = "test-key"
    client = FakeClient(FakeChannel())
    client.set_missing_host_key_policy = lambda policy: None

    def fail_connect(**kwargs):
        assert kwargs["banner_timeout"] == 2
        assert kwargs["auth_timeout"] == 2
        raise TimeoutError("connect timeout")

    client.connect = fail_connect
    monkeypatch.setattr(ssh_module.paramiko, "SSHClient", lambda: client)
    monkeypatch.setattr(ssh_module.paramiko.RSAKey, "from_private_key", lambda key: object())

    with pytest.raises(TimeoutError, match="connect timeout"):
        manager.exec_command_stream("test", timeout=2)

    assert client.closed == 1


class _NoAckTransport:
    """Real Paramiko Channel, but no network, worker or request ACK."""

    def get_log_channel(self):
        return "test.offline.channel"

    def _send_user_message(self, message):
        pass

    def get_exception(self):
        return None


class _NoAckClient:
    def __init__(self):
        self.channel = paramiko.Channel(1)
        self.channel._set_transport(_NoAckTransport())
        self.channel.active = True
        self.closed = False

    def exec_command(self, command, timeout):
        self.channel.settimeout(timeout)
        # Paramiko ignores that channel timeout in this ACK wait. The deadline
        # must close the connection/channel from the watchdog.
        self.channel.exec_command(command)
        return None, SimpleNamespace(channel=self.channel), None

    def open_sftp(self):
        self.channel.invoke_subsystem("sftp")
        raise AssertionError("No subsystem ACK should arrive in this test")

    def close(self):
        self.closed = True
        self.channel.close()


def test_real_channel_missing_exec_ack_observes_deadline_without_worker_leak():
    manager = object.__new__(SSHManager)
    client = _NoAckClient()
    manager.get_client = lambda **kwargs: client
    before = {thread.ident for thread in threading.enumerate()}
    start = time.monotonic()

    with pytest.raises(TimeoutError, match="exceeded"):
        manager.exec_command_stream("never acknowledged", timeout=0.1)

    assert time.monotonic() - start < 1.5
    assert client.closed and client.channel.closed
    assert {thread.ident for thread in threading.enumerate()} <= before


def test_sftp_missing_subsystem_ack_is_bounded(monkeypatch, orch_env):
    manager = object.__new__(SSHManager)
    client = _NoAckClient()
    manager.get_client = lambda **kwargs: client
    manager.log_callback = lambda message: None
    monkeypatch.setattr(ssh_module, "SFTP_IO_TIMEOUT_SECONDS", 0.1)
    before = {thread.ident for thread in threading.enumerate()}
    start = time.monotonic()

    assert manager.sync_videos() == []

    assert time.monotonic() - start < 1.5
    assert client.closed and client.channel.closed
    assert {thread.ident for thread in threading.enumerate()} <= before


def test_sftp_download_deadline_aborts_blocked_receive_and_removes_partial(monkeypatch, orch_env):
    manager = object.__new__(SSHManager)
    client = _NoAckClient()
    manager.get_client = lambda **kwargs: client
    manager.log_callback = lambda message: None
    monkeypatch.setattr(ssh_module, "SFTP_TRANSFER_TIMEOUT_SECONDS", 0.1)

    class BlockingSFTP:
        def get_channel(self):
            return client.channel

        def listdir_attr(self, remote):
            return [SimpleNamespace(filename="blocked.mp4", st_size=10, st_mtime=time.time() - 60)]

        def get(self, remote, local):
            with open(local, "wb") as stream:
                stream.write(b"partial")
            # Actual Paramiko blocking buffered read, no fabricated sleep loop.
            client.channel.recv(32_768)

        def close(self):
            pass

    client.open_sftp = lambda: BlockingSFTP()
    start = time.monotonic()

    assert manager.sync_videos() == []

    assert time.monotonic() - start < 1.5
    assert client.closed and client.channel.closed
    assert list(orch_env.outputs.iterdir()) == []
