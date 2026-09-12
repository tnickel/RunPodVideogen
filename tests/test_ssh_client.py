# Regressionstests für backend/ssh_client.py
# Deckt u. a. P1-7 (kein Download frisch geschriebener Dateien) und
# P2-11a (Windows-Sanitizing von Dateinamen) sowie die .part-Logik ab.

import time

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
    assert sftp.get_calls == []


def test_sync_downloads_changed_file(tmp_path, monkeypatch, orch_env):
    (orch_env.outputs / 'grew.mp4').write_bytes(b"x" * 3)
    sftp = FakeSFTP([Attr('grew.mp4', size=10)])
    mgr = _make_manager(monkeypatch, sftp)

    downloaded = mgr.sync_videos()

    assert downloaded == ['grew.mp4']
    assert (orch_env.outputs / 'grew.mp4').stat().st_size == 10


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


def test_sync_without_special_chars_writes_no_mapping(tmp_path, monkeypatch, orch_env):
    sftp = FakeSFTP([Attr('normal.mp4')])
    mgr = _make_manager(monkeypatch, sftp)

    mgr.sync_videos()

    assert not (orch_env.outputs / '.name_map.json').exists()


def test_sync_missing_remote_dir_returns_empty(tmp_path, monkeypatch, orch_env):
    class NoDirSFTP(FakeSFTP):
        def listdir_attr(self, remote_dir):
            raise IOError("no such directory")

    mgr = _make_manager(monkeypatch, NoDirSFTP([]))
    assert mgr.sync_videos() == []
