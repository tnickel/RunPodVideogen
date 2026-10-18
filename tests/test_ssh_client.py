# Regressionstests für backend/ssh_client.py
# Deckt u. a. P1-7 (kein Download frisch geschriebener Dateien) und
# P2-11a (Windows-Sanitizing von Dateinamen) sowie die .part-Logik ab.

import time
from types import SimpleNamespace

import pytest

from backend.ssh_client import SSHManager


class Attr:
    """Attrappen-Objekt mit den von sync_videos genutzten SFTP-Attributen."""
    def __init__(self, name, size=10, mtime=None):
        self.filename = name
        self.st_size = size
        self.st_mtime = mtime if mtime is not None else time.time() - 3600


class FakeSFTP:
    def __init__(self, files, fail_names=()):
        self.files = list(files)
        self.fail = set(fail_names)
        self.get_calls = []
        self.timeout = None

    def get_channel(self):
        return SimpleNamespace(settimeout=lambda value: setattr(self, "timeout", value))

    def listdir_attr(self, remote_dir):
        return self.files

    def get(self, remote, local):
        self.get_calls.append(remote)
        fname = remote.rsplit("/", 1)[-1]
        if fname in self.fail:
            raise IOError("Download simuliert fehlgeschlagen")
        with open(local, "wb") as f:
            f.write(b"x" * 10)

    def close(self):
        pass


class FakeSSHClient:
    def __init__(self, sftp):
        self._sftp = sftp

    def open_sftp(self):
        return self._sftp

    def close(self):
        pass


def _make_manager(monkeypatch, sftp):
    monkeypatch.setattr(SSHManager, "get_client", lambda self, timeout=15: FakeSSHClient(sftp))
    monkeypatch.setattr("backend.ssh_client.get_or_create_ssh_keys", lambda: ("PRIV", "PUB"))
    return SSHManager("10.0.0.1", 22, log_callback=lambda msg: None)


def test_sync_sanitizes_windows_forbidden_characters(tmp_path, monkeypatch, orch_env):
    sftp = FakeSFTP([Attr('bild:final*neu?.mp4')])
    mgr = _make_manager(monkeypatch, sftp)

    downloaded = mgr.sync_videos()

    # ':', '*', '?' müssen ersetzt werden -> Datei ist auf Windows anlegbar
    assert downloaded == ['bild_final_neu_.mp4']
    assert (orch_env.outputs / 'bild_final_neu_.mp4').exists()
    # Remote-Pfad nutzt weiterhin den ORIGINAL-Namen
    assert any("bild:final*neu?.mp4" in r for r in sftp.get_calls)


def test_sync_skips_freshly_written_files(tmp_path, monkeypatch, orch_env):
    """P1-7-Regression: Dateien < 15 s alt dürfen nicht geladen werden (korrupte Downloads)."""
    sftp = FakeSFTP([Attr('rendering_now.mp4', mtime=time.time() - 3)])
    mgr = _make_manager(monkeypatch, sftp)

    downloaded = mgr.sync_videos()

    assert downloaded == []
    assert sftp.get_calls == []
    assert not (orch_env.outputs / 'rendering_now.mp4').exists()


def test_sync_skips_file_with_identical_size(tmp_path, monkeypatch, orch_env):
    (orch_env.outputs / 'done.mp4').write_bytes(b"x" * 10)
    sftp = FakeSFTP([Attr('done.mp4', size=10)])
    mgr = _make_manager(monkeypatch, sftp)

    downloaded = mgr.sync_videos()

    assert downloaded == []
    assert sftp.get_calls == ["/workspace/Wan2GP/outputs/done.mp4"]
    from backend.ssh_client import get_verified_identity
    assert get_verified_identity("done.mp4", str(orch_env.outputs / "done.mp4")) is not None


def test_sync_downloads_changed_file(tmp_path, monkeypatch, orch_env):
    (orch_env.outputs / 'grew.mp4').write_bytes(b"x" * 3)
    sftp = FakeSFTP([Attr('grew.mp4', size=10)])
    mgr = _make_manager(monkeypatch, sftp)

    downloaded = mgr.sync_videos()

    assert len(downloaded) == 1 and downloaded[0] != 'grew.mp4'
    assert (orch_env.outputs / 'grew.mp4').read_bytes() == b"x" * 3
    assert (orch_env.outputs / downloaded[0]).stat().st_size == 10


def test_sync_failed_download_leaves_no_part_file(tmp_path, monkeypatch, orch_env):
    sftp = FakeSFTP([Attr('broken.mp4')], fail_names={'broken.mp4'})
    mgr = _make_manager(monkeypatch, sftp)

    downloaded = mgr.sync_videos()

    assert downloaded == []
    assert not (orch_env.outputs / 'broken.mp4').exists()
    assert not (orch_env.outputs / 'broken.mp4.part').exists(), ".part-Reste müssen aufgeräumt werden"
    assert list(orch_env.outputs.iterdir()) == []


def test_sync_only_considers_media_files(tmp_path, monkeypatch, orch_env):
    sftp = FakeSFTP([Attr('notes.txt'), Attr('clip.mp4'), Attr('still.png'), Attr('meta.json')])
    mgr = _make_manager(monkeypatch, sftp)

    downloaded = mgr.sync_videos()

    assert sorted(downloaded) == ['clip.mp4', 'still.png']


def test_sync_persists_name_mapping(tmp_path, monkeypatch, orch_env):
    """Sanitizing-Mapping: {lokal sanitisiert -> Remote-Original} wird persistiert,
    damit das Remote-Löschen später den Original-Namen trifft."""
    sftp = FakeSFTP([Attr('render:final?.mp4')])
    mgr = _make_manager(monkeypatch, sftp)

    downloaded = mgr.sync_videos()

    assert downloaded == ['render_final_.mp4']
    from backend.ssh_client import load_name_map
    assert load_name_map() == {'render_final_.mp4': 'render:final?.mp4'}


def test_sync_records_provenance_for_identical_names_too(tmp_path, monkeypatch, orch_env):
    """Neu: Das Mapping dokumentiert die Herkunft JEDER erfolgreichen Downloads
    (auch identischer Namen). Erst damit ist der Besitz eines lokalen Namens
    später eindeutig - die Grundlage der Kollisionsauflösung."""
    sftp = FakeSFTP([Attr('normal.mp4')])
    mgr = _make_manager(monkeypatch, sftp)

    mgr.sync_videos()

    from backend.ssh_client import load_name_map
    assert load_name_map() == {'normal.mp4': 'normal.mp4'}


def test_sync_missing_remote_dir_returns_empty(tmp_path, monkeypatch, orch_env):
    class NoDirSFTP(FakeSFTP):
        def listdir_attr(self, remote_dir):
            raise IOError("no such directory")

    mgr = _make_manager(monkeypatch, NoDirSFTP([]))
    assert mgr.sync_videos() == []


# ---------------------------------------------------------------- P2-9

class _PerFileSFTP(FakeSFTP):
    """Schreibt pro Remote-Name unterscheidbare Inhalte (für Kollisionstests)."""

    def __init__(self, files):
        super().__init__(files)
        for attr in self.files:
            attr.st_size = len(attr.filename.encode("utf-8")) * 3

    def get(self, remote, local):
        self.get_calls.append(remote)
        fname = remote.rsplit("/", 1)[-1]
        with open(local, "wb") as f:
            f.write(fname.encode("utf-8") * 3)


def test_sync_keeps_both_files_on_same_sanitized_name(tmp_path, monkeypatch, orch_env):
    """P2-9-Abnahme: clip:a.mp4 und clip?a.mp4 sanitizen beide zu clip_a.mp4.
    Beide Remote-Originals müssen getrennt, mit korrektem Mapping und ohne
    Überschreiben lokal ankommen (gleiche UND unterschiedliche Größe)."""
    sftp = _PerFileSFTP([Attr('clip:a.mp4', size=27), Attr('clip?a.mp4', size=27)])
    mgr = _make_manager(monkeypatch, sftp)

    downloaded = mgr.sync_videos()

    assert len(downloaded) == 2, "Beide Videos müssen getrennt ankommen"
    assert len(set(downloaded)) == 2, "Lokale Namen müssen eindeutig sein"
    files = sorted(p.name for p in orch_env.outputs.iterdir() if p.suffix == ".mp4")
    assert len(files) == 2
    # Beide Inhalte bleiben erhalten - kein Überschreiben
    contents = {(orch_env.outputs / n).read_bytes() for n in files}
    assert contents == {b"clip:a.mp4" * 3, b"clip?a.mp4" * 3}
    # Mapping zeigt jeder lokale Name auf SEIN Remote-Original
    from backend.ssh_client import load_name_map
    mapping = load_name_map()
    assert sorted(mapping.values()) == ["clip:a.mp4", "clip?a.mp4"]
    for local_name, remote_name in mapping.items():
        assert (orch_env.outputs / local_name).read_bytes() == remote_name.encode("utf-8") * 3


def test_sync_handles_windows_case_insensitive_collisions(tmp_path, monkeypatch, orch_env):
    """P2-9: Windows unterscheidet keine Groß-/Kleinschreibung - 'Clip A.mp4'
    und 'clip a.mp4' dürfen nicht dieselbe lokale Datei belegen."""
    (orch_env.outputs / "Clip A.mp4").write_bytes(b"alt")
    sftp = _PerFileSFTP([Attr("clip a.mp4", size=27)])
    mgr = _make_manager(monkeypatch, sftp)

    downloaded = mgr.sync_videos()

    assert len(downloaded) == 1
    assert (orch_env.outputs / "Clip A.mp4").read_bytes() == b"alt", "Bestand bleibt unangetastet"
    new_files = [p for p in orch_env.outputs.iterdir() if p.name != "Clip A.mp4" and p.suffix == ".mp4"]
    assert len(new_files) == 1, "Neue Datei braucht einen eigenen, eindeutigen Namen"


def test_failed_download_writes_no_mapping_entry(tmp_path, monkeypatch, orch_env):
    """P2-9: Mapping wird erst nach ERFOLGREICHEM Download geschrieben - nie auf
    eine Datei zeigen, die lokal nicht existiert (Löschen würde sonst ins Leere
    oder auf ein falsches Original zielen)."""
    sftp = FakeSFTP([Attr('render:broken?.mp4')], fail_names={'render:broken?.mp4'})
    mgr = _make_manager(monkeypatch, sftp)

    downloaded = mgr.sync_videos()

    assert downloaded == []
    from backend.ssh_client import load_name_map
    assert load_name_map() == {}


class _BytesSFTP(FakeSFTP):
    def __init__(self, contents):
        self.contents = contents
        super().__init__([Attr(name, size=len(data)) for name, data in contents.items()])

    def get(self, remote, local):
        self.get_calls.append(remote)
        with open(local, "wb") as stream:
            stream.write(self.contents[remote.rsplit("/", 1)[-1]])


@pytest.mark.parametrize("second_content", [b"B" * 10, b"B" * 17, b"A" * 10])
def test_later_raw_name_collision_preserves_both_downloads(monkeypatch, orch_env, second_content):
    """A later raw filename must not steal an earlier sanitized filename."""
    from backend.ssh_client import get_verified_identity, load_name_map
    first = _BytesSFTP({"clip:a.mp4": b"A" * 10})
    manager = _make_manager(monkeypatch, first)
    assert manager.sync_videos() == ["clip_a.mp4"]

    second = _BytesSFTP({"clip:a.mp4": b"A" * 10, "clip_a.mp4": second_content})
    second.files[0].st_mtime = first.files[0].st_mtime
    manager = _make_manager(monkeypatch, second)
    added = manager.sync_videos()

    assert len(added) == 1 and added[0].lower() != "clip_a.mp4"
    assert (orch_env.outputs / "clip_a.mp4").read_bytes() == b"A" * 10
    assert (orch_env.outputs / added[0]).read_bytes() == second_content
    assert load_name_map()["clip_a.mp4"] == "clip:a.mp4"
    assert get_verified_identity(added[0], str(orch_env.outputs / added[0]))["remote"] == "clip_a.mp4"
    second.get_calls.clear()
    assert manager.sync_videos() == []
    assert second.get_calls == []


def test_legacy_wrong_mapping_requires_downloaded_content_match(monkeypatch, orch_env):
    from backend.ssh_client import get_verified_identity, save_name_map
    local = orch_env.outputs / "clip_a.mp4"
    local.write_bytes(b"A" * 10)
    save_name_map({"clip_a.mp4": "clip?a.mp4"})
    assert get_verified_identity(local.name, str(local)) is None
    manager = _make_manager(monkeypatch, _BytesSFTP({"clip?a.mp4": b"B" * 10}))

    added = manager.sync_videos()

    assert len(added) == 1 and added[0] != local.name
    assert local.read_bytes() == b"A" * 10
    assert get_verified_identity(local.name, str(local)) is None
    assert get_verified_identity(added[0], str(orch_env.outputs / added[0]))["remote"] == "clip?a.mp4"


def test_legacy_mapping_is_migrated_only_after_equal_content_download(monkeypatch, orch_env):
    from backend.ssh_client import get_verified_identity, remove_name_mapping, save_name_map
    local = orch_env.outputs / "clip_a.mp4"
    local.write_bytes(b"A" * 10)
    save_name_map({local.name: "clip:a.mp4"})
    sftp = _BytesSFTP({"clip:a.mp4": b"A" * 10})
    manager = _make_manager(monkeypatch, sftp)

    assert manager.sync_videos() == []
    assert len(sftp.get_calls) == 1
    assert get_verified_identity(local.name, str(local))["remote"] == "clip:a.mp4"
    local.write_bytes(b"B" * 10)
    assert get_verified_identity(local.name, str(local)) is None
    local.write_bytes(b"A" * 10)
    assert get_verified_identity(local.name, str(local)) is not None
    remove_name_mapping(local.name)
    assert get_verified_identity(local.name, str(local)) is None


def test_same_size_local_edit_is_preserved_when_remote_returns(monkeypatch, orch_env):
    manager = _make_manager(monkeypatch, _BytesSFTP({"normal.mp4": b"A" * 10}))
    assert manager.sync_videos() == ["normal.mp4"]
    (orch_env.outputs / "normal.mp4").write_bytes(b"B" * 10)

    added = manager.sync_videos()

    assert len(added) == 1 and added[0] != "normal.mp4"
    assert (orch_env.outputs / "normal.mp4").read_bytes() == b"B" * 10
    assert (orch_env.outputs / added[0]).read_bytes() == b"A" * 10


def test_changed_remote_mtime_with_same_size_downloads_new_version(monkeypatch, orch_env):
    first = _BytesSFTP({"normal.mp4": b"A" * 10})
    manager = _make_manager(monkeypatch, first)
    assert manager.sync_videos() == ["normal.mp4"]
    second = _BytesSFTP({"normal.mp4": b"B" * 10})
    second.files[0].st_mtime = first.files[0].st_mtime + 30
    manager = _make_manager(monkeypatch, second)

    added = manager.sync_videos()

    assert len(added) == 1 and added[0] != "normal.mp4"
    assert (orch_env.outputs / "normal.mp4").read_bytes() == b"A" * 10
    assert (orch_env.outputs / added[0]).read_bytes() == b"B" * 10
    assert manager.sync_videos() == []


def test_sftp_channel_reads_receive_timeout(monkeypatch, orch_env):
    from backend.ssh_client import SFTP_IO_TIMEOUT_SECONDS
    sftp = FakeSFTP([])
    manager = _make_manager(monkeypatch, sftp)
    assert manager.sync_videos() == []
    assert sftp.timeout == SFTP_IO_TIMEOUT_SECONDS


def test_truncated_download_is_not_published(monkeypatch, orch_env):
    manager = _make_manager(monkeypatch, FakeSFTP([Attr("short.mp4", size=12)]))
    assert manager.sync_videos() == []
    assert list(orch_env.outputs.iterdir()) == []


# ---------------------------------------------------------------- P2-10

class _EndlessChannel:
    """Befehl beendet sich nie und produziert keine Ausgabe (Deadlock-Falle)."""

    def recv_ready(self):
        return False

    def recv(self, n):
        return b""

    def recv_stderr_ready(self):
        return False

    def recv_stderr(self, n):
        return b""

    def exit_status_ready(self):
        return False

    def recv_exit_status(self):
        return 0


class _BigOutputChannel:
    """Viel stdout + stderr, danach sauberes Ende - darf das SSH-Fenster nicht blocken."""

    def __init__(self):
        # Bleibt unter der 200k-Tail-Grenze von exec_command_stream
        self.out_chunks = [b"O" * 60000] * 3 + [b"stdout-END\n"]
        self.err_chunks = [b"E" * 60000] * 2 + [b"stderr-END\n"]
        self._exit = None

    def recv_ready(self):
        return bool(self.out_chunks)

    def recv(self, n):
        return self.out_chunks.pop(0) if self.out_chunks else b""

    def recv_stderr_ready(self):
        return bool(self.err_chunks)

    def recv_stderr(self, n):
        return self.err_chunks.pop(0) if self.err_chunks else b""

    def exit_status_ready(self):
        return not self.out_chunks and not self.err_chunks

    def recv_exit_status(self):
        return 7


class _FakeExecClient:
    def __init__(self, channel):
        self._channel = channel

    def exec_command(self, cmd, timeout=None):
        import types
        stdout = types.SimpleNamespace(channel=self._channel)
        return (None, stdout, None)

    def close(self):
        pass


def _exec_manager(monkeypatch, channel):
    monkeypatch.setattr(SSHManager, "get_client", lambda self, timeout=15: _FakeExecClient(channel))
    monkeypatch.setattr("backend.ssh_client.get_or_create_ssh_keys", lambda: ("PRIV", "PUB"))
    return SSHManager("10.0.0.1", 22, log_callback=lambda msg: None)


def test_exec_command_returns_within_timeout_on_endless_command(monkeypatch):
    """P2-10: recv_exit_status() ohne Frist hing früher für immer. Jetzt muss
    der Aufrufer innerhalb des Zeitlimits mit einem definierten Ergebnis
    zurückkommen."""
    import time as time_mod
    mgr = _exec_manager(monkeypatch, _EndlessChannel())
    started = time_mod.monotonic()

    code, out, err = mgr.exec_command("sleep infinity", timeout=1)

    assert time_mod.monotonic() - started < 5, "Aufrufer muss im Zeitlimit zurückkehren"
    assert code == -1
    assert "SSH-Zeitlimit" in err


def test_exec_command_drains_large_outputs_and_returns_exit_code(monkeypatch):
    """P2-10: Große stdout-/stderr-Mengen dürfen das SSH-Fenster nicht füllen -
    laufendes Leeren, vollständige Ausgaben, korrekter Exit-Code."""
    mgr = _exec_manager(monkeypatch, _BigOutputChannel())

    code, out, err = mgr.exec_command("big output", timeout=10)

    assert code == 7
    assert out.endswith("stdout-END\n")
    assert out.count("O") >= 3 * 60000
    assert err.endswith("stderr-END\n")
    assert err.count("E") >= 2 * 60000


def _local_delete_manager(monkeypatch):
    """Execute exactly the remote Python payload locally; never open SSH."""
    import contextlib
    import io
    import shlex
    import sys
    from backend.ssh_client import _VERIFIED_DELETE_SCRIPT

    manager = object.__new__(SSHManager)
    messages = []
    manager.log_callback = messages.append
    commands = []

    def execute(command, timeout):
        commands.append(command)
        header, script = command.split(" <<'RUNPOD_VERIFIED_DELETE_PY'\n", 1)
        script = script.rsplit("\nRUNPOD_VERIFIED_DELETE_PY", 1)[0]
        assert script == _VERIFIED_DELETE_SCRIPT
        argv = shlex.split(header)
        assert argv[:2] == ["python3", "-"]
        output = io.StringIO()
        with monkeypatch.context() as context:
            context.setattr(sys, "argv", argv[1:])
            with contextlib.redirect_stdout(output):
                exec(compile(script, "<remote-verified-delete>", "exec"), {"__name__": "__main__"})
        return 0, output.getvalue(), ""

    manager.exec_command = execute
    return manager, messages, commands


def test_verified_delete_removes_only_matching_file(tmp_path, monkeypatch):
    import hashlib
    original = tmp_path / "video.mp4"
    original.write_bytes(b"matching video")
    manager, messages, commands = _local_delete_manager(monkeypatch)

    assert manager.delete_verified_file(str(original), hashlib.sha256(b"matching video").hexdigest())

    assert not original.exists()
    assert list(tmp_path.iterdir()) == []
    assert len(commands) == 1 and messages == []


@pytest.mark.parametrize("matches", [True, False])
def test_verified_delete_preserves_replacement_created_after_quarantine(tmp_path, monkeypatch, matches):
    import hashlib
    import os
    original = tmp_path / "video.mp4"
    old_content, new_content = b"old generation", b"new generation"
    original.write_bytes(old_content)
    replace = os.replace

    def replace_then_create(source, target):
        replace(source, target)
        if str(source) == str(original):
            # Reproduce a new render taking the same pathname immediately
            # after the atomic move; it must never be removed or overwritten.
            original.write_bytes(new_content)

    monkeypatch.setattr(os, "replace", replace_then_create)
    manager, messages, commands = _local_delete_manager(monkeypatch)
    expected = old_content if matches else b"some different local video"

    assert manager.delete_verified_file(str(original), hashlib.sha256(expected).hexdigest()) is matches

    assert original.read_bytes() == new_content
    assert len(commands) == 1
    backups = [path for path in tmp_path.iterdir() if path != original]
    if matches:
        assert backups == []
    else:
        assert len(backups) == 1
        assert backups[0].suffix == ".mp4"
        assert not backups[0].name.startswith(".")
        assert backups[0].read_bytes() == old_content
        assert any(str(backups[0]) in message for message in messages)


def test_verified_delete_mismatch_restores_original_without_data_loss(tmp_path, monkeypatch):
    import hashlib
    original = tmp_path / "video.mp4"
    original.write_bytes(b"remote original")
    manager, messages, _ = _local_delete_manager(monkeypatch)

    assert not manager.delete_verified_file(str(original), hashlib.sha256(b"other local data").hexdigest())

    assert original.read_bytes() == b"remote original"
    assert list(tmp_path.iterdir()) == [original]
    assert messages


@pytest.mark.parametrize("failure", ["hash", "unlink"])
def test_verified_delete_preserves_data_on_hash_or_unlink_error(tmp_path, monkeypatch, failure):
    import hashlib
    import os
    original = tmp_path / "video.mp4"
    content = b"valuable bytes"
    original.write_bytes(content)
    expected = hashlib.sha256(content).hexdigest()
    if failure == "hash":
        def fail_hash():
            raise OSError("simulated hash/read failure")
        monkeypatch.setattr(hashlib, "sha256", fail_hash)
    else:
        unlink = os.unlink
        failures = [0]

        def fail_first_quarantine_unlink(path):
            if str(path).endswith(".quarantine") and failures[0] == 0:
                failures[0] += 1
                raise OSError("simulated unlink failure")
            return unlink(path)
        monkeypatch.setattr(os, "unlink", fail_first_quarantine_unlink)
    manager, messages, _ = _local_delete_manager(monkeypatch)

    assert not manager.delete_verified_file(str(original), expected)

    assert original.read_bytes() == content
    assert list(tmp_path.iterdir()) == [original]
    assert messages


def test_verified_delete_does_not_follow_symlink(tmp_path, monkeypatch):
    import hashlib
    target = tmp_path / "valuable.mp4"
    target.write_bytes(b"valuable bytes")
    original = tmp_path / "link.mp4"
    try:
        original.symlink_to(target)
    except OSError:
        pytest.skip("Creating symlinks is unavailable on this platform")
    manager, messages, _ = _local_delete_manager(monkeypatch)

    assert not manager.delete_verified_file(str(original), hashlib.sha256(b"valuable bytes").hexdigest())

    assert target.read_bytes() == b"valuable bytes"
    assert original.is_symlink()
    assert messages


@pytest.mark.parametrize("backup_move_fails", [False, True])
def test_verified_delete_preserves_quarantine_when_restoration_fails(tmp_path, monkeypatch, backup_move_fails):
    import hashlib
    import os
    original = tmp_path / "video.mp4"
    content = b"remote data must survive"
    original.write_bytes(content)
    replace = os.replace

    def refuse_restoring_link(*args, **kwargs):
        raise PermissionError("simulated no-clobber restoration failure")

    def maybe_refuse_backup(source, target):
        if backup_move_fails and str(source).endswith(".quarantine"):
            raise PermissionError("simulated backup rename failure")
        return replace(source, target)

    monkeypatch.setattr(os, "link", refuse_restoring_link)
    monkeypatch.setattr(os, "replace", maybe_refuse_backup)
    manager, messages, _ = _local_delete_manager(monkeypatch)

    assert not manager.delete_verified_file(str(original), hashlib.sha256(b"mismatch").hexdigest())

    remaining = list(tmp_path.iterdir())
    assert len(remaining) == 1
    assert remaining[0].read_bytes() == content
    assert remaining[0].suffix == (".quarantine" if backup_move_fails else ".mp4")
    assert any(str(remaining[0]) in message for message in messages)


@pytest.mark.parametrize("remote_path", ["/outputs/bob's clip.mp4", "/outputs/clip; touch hacked.mp4",
                                         "/outputs/clip\n$(touch hacked).mp4"])
def test_verified_delete_quotes_remote_path_and_heredoc(monkeypatch, remote_path):
    import shlex
    manager = object.__new__(SSHManager)
    manager.log_callback = lambda message: None
    commands = []

    def execute(command, timeout):
        commands.append(command)
        header, _ = command.split(" <<'RUNPOD_VERIFIED_DELETE_PY'\n", 1)
        assert shlex.split(header) == ["python3", "-", remote_path, "a" * 64]
        return 0, '{"deleted": true}', ""

    manager.exec_command = execute
    assert manager.delete_verified_file(remote_path, "a" * 64)
    assert len(commands) == 1
