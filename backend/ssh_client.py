import os
import io
import codecs
import hashlib
import json
import time
import re
import shlex
import tempfile
import threading
import paramiko
from typing import Tuple, List, Optional, Callable
from backend.config_manager import get_outputs_dir

_SSH_KEY_DIR = os.path.join(os.path.expanduser("~"), ".ssh", "runpod_videogen")
_SSH_PRIV_FILE = os.path.join(_SSH_KEY_DIR, "id_rsa")
_SSH_PUB_FILE = os.path.join(_SSH_KEY_DIR, "id_rsa.pub")

NAME_MAP_FILENAME = ".name_map.json"
VERIFIED_NAME_MAP_FILENAME = ".name_map_verified.json"
_NAME_MAP_LOCK = threading.RLock()
_STREAM_TAIL_CHARS = 200_000
# Zeitlimit für einzelne SFTP-Netzoperationen (Verzeichnisabfrage, Lesezugriff).
# Begrenzt blockierende Transfers; langsam fließende Downloads laufen weiter.
SFTP_IO_TIMEOUT_SECONDS = 30.0
SFTP_TRANSFER_TIMEOUT_SECONDS = 15 * 60.0

# Executed as one process on the pod. A renamed inode is verified/deleted;
# another file created at the original path is never touched by deletion or
# restoration. No shell interpolation occurs inside the quoted heredoc.
_VERIFIED_DELETE_SCRIPT = r'''
import hashlib
import json
import os
import stat
import sys
import tempfile


def preserve(quarantine, original):
    try:
        os.link(quarantine, original, follow_symlinks=False)
    except OSError:
        # An occupied original path must never be replaced. Keep the old bytes
        # under a visible media name instead, allowing normal synchronization.
        extension = os.path.splitext(original)[1]
        if extension.lower() not in (".mp4", ".png", ".jpg"):
            extension = ".backup"
        backup = None
        try:
            descriptor, backup = tempfile.mkstemp(
                prefix="preserved-", suffix=extension,
                dir=os.path.dirname(original) or ".")
            os.close(descriptor)
            os.replace(quarantine, backup)
            return [backup]
        except OSError:
            if backup is not None:
                try:
                    os.unlink(backup)
                except OSError:
                    pass
            return [quarantine]
    else:
        try:
            os.unlink(quarantine)
            return [original]
        except OSError:
            # The restored link and the quarantine both still contain data.
            return [original, quarantine]


def delete_verified(original, expected):
    quarantine = None
    moved = False
    try:
        descriptor, quarantine = tempfile.mkstemp(
            prefix=".runpod-delete-", suffix=".quarantine",
            dir=os.path.dirname(original) or ".")
        os.close(descriptor)
        os.replace(original, quarantine)
        moved = True
        if not stat.S_ISREG(os.lstat(quarantine).st_mode):
            raise ValueError("Ziel ist keine regulaere Datei")
        descriptor = os.open(quarantine, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            digest = hashlib.sha256()
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("Datei wurde waehrend der Pruefung geschrieben")
        if digest.hexdigest() != expected:
            raise ValueError("SHA256 stimmt nicht ueberein")
        os.unlink(quarantine)
        return {"deleted": True}
    except Exception as error:
        if moved:
            retained = preserve(quarantine, original)
        else:
            retained = [original]
            if quarantine is not None:
                try:
                    os.unlink(quarantine)
                except OSError:
                    pass
        return {"deleted": False, "reason": str(error), "retained": retained}


print(json.dumps(delete_verified(sys.argv[1], sys.argv[2]), ensure_ascii=True))
'''


def _windows_safe_name(fname: str) -> str:
    """Ersetzt Windows-verbotene Zeichen; ohne Kollisionsbehandlung."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', fname).rstrip(' .')


def sanitize_remote_name(fname: str, taken_lower: set) -> str:
    """Windows-sicherer, kollisionsfreier lokaler Name für einen Remote-Namen.

    `taken_lower` enthält die bereits belegten lokalen Namen (kleingeschrieben,
    Windows unterscheidet keine Groß-/Kleinschreibung). Kollidiert der
    bereinigte Name mit einem anderen Original (z. B. clip:a.mp4 und clip?a.mp4
    -> beide clip_a.mp4), wird ein stabiler Hash-Suffix vom ORIGINAL-Namen
    angehängt - deterministisch über Syncs hinweg, keine Zufallsnamen."""
    safe = _windows_safe_name(fname)
    if not safe:
        return ""
    if safe.lower() in taken_lower:
        digest = hashlib.sha1(fname.encode("utf-8", errors="replace")).hexdigest()[:8]
        stem, dot, ext = safe.rpartition(".")
        if not stem:
            stem, ext = ext, ""
        safe = f"{stem}__{digest}.{ext}" if ext else f"{safe}__{digest}"
        if safe.lower() in taken_lower:  # extrem unwahrscheinlich, aber sicher
            return ""
    return safe


def _name_map_path() -> str:
    return os.path.join(get_outputs_dir(), NAME_MAP_FILENAME)


def load_name_map() -> dict:
    """Liefert die Zuordnung {lokaler (sanitizierter) Dateiname: Remote-Originalname}."""
    try:
        with open(_name_map_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_name_map(mapping: dict) -> None:
    with _NAME_MAP_LOCK:
        _save_json(_name_map_path(), mapping)


def _save_json(path: str, data: dict) -> None:
    """Publish complete metadata, never a partly written JSON document."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=os.path.dirname(path),
                                         prefix=".mapping-", suffix=".tmp", delete=False) as stream:
            temporary = stream.name
            json.dump(data, stream, indent=2, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.remove(temporary)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_verified_map() -> dict:
    try:
        with open(os.path.join(get_outputs_dir(), VERIFIED_NAME_MAP_FILENAME), encoding="utf-8") as stream:
            data = json.load(stream)
        if data.get("version") == 1 and isinstance(data.get("entries"), dict):
            return data["entries"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def get_verified_identity(local_name: str, local_path: str) -> Optional[dict]:
    """Return {remote, sha256, size} only for the current proven local bytes.

    Legacy .name_map.json entries alone are never a deletion authorization.
    Callers must also compare the current remote SHA256 before deleting remote
    data, because the remote file may have changed since its download.
    """
    with _NAME_MAP_LOCK:
        entry = _load_verified_map().get(local_name)
        if not isinstance(entry, dict) or load_name_map().get(local_name) != entry.get("remote"):
            return None
        remote = entry.get("remote")
        if not isinstance(remote, str) or not remote or "/" in remote or "\x00" in remote:
            return None
        try:
            if (entry.get("size") == os.path.getsize(local_path)
                    and entry.get("sha256") == _sha256_file(local_path)):
                return {key: entry[key] for key in ("remote", "sha256", "size")} | {
                    "remote_mtime": entry.get("remote_mtime")}
        except OSError:
            pass
    return None


def _record_download_identity(local_name: str, remote: str, local_path: str,
                              digest: str, remote_mtime: float) -> bool:
    with _NAME_MAP_LOCK:
        try:
            size = os.path.getsize(local_path)
            if _sha256_file(local_path) != digest:
                return False
        except OSError:
            return False
        mapping = load_name_map()
        verified = _load_verified_map()
        mapping[local_name] = remote
        verified[local_name] = {"remote": remote, "sha256": digest, "size": size,
                                "remote_mtime": remote_mtime}
        # A crash between these writes can only remove trust, never create it:
        # get_verified_identity requires both stores to agree.
        save_name_map(mapping)
        _save_json(os.path.join(get_outputs_dir(), VERIFIED_NAME_MAP_FILENAME),
                   {"version": 1, "entries": verified})
        return True


def remove_name_mapping(local_name: str) -> None:
    """Remove both stores under the same lock used for download publication."""
    with _NAME_MAP_LOCK:
        mapping = load_name_map()
        verified = _load_verified_map()
        mapping.pop(local_name, None)
        verified.pop(local_name, None)
        save_name_map(mapping)
        _save_json(os.path.join(get_outputs_dir(), VERIFIED_NAME_MAP_FILENAME),
                   {"version": 1, "entries": verified})


def _deadline_call(client, callback, deadline: float, timeout: float):
    """Interrupt Paramiko's ACK waits by closing their owning connection.

    Channel.exec_command/invoke_subsystem wait on an Event without observing
    Channel.settimeout. No command worker is created: the calling thread does
    the operation; a short-lived timer only closes the connection at expiry.
    Closing a Paramiko transport wakes those ACK waits.
    """
    expired = threading.Event()
    finished = threading.Event()

    def expire():
        expired.set()
        # During connect() the Transport may not have been assigned yet. Keep
        # the deadline active until the operation exits so a just-created
        # transport cannot begin another full handshake after an early close.
        while not finished.is_set():
            client.close()
            finished.wait(0.02)

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(f"SSH operation exceeded {timeout:g} seconds")
    timer = threading.Timer(remaining, expire)
    timer.daemon = True
    timer.start()
    try:
        try:
            result = callback()
        except Exception:
            if expired.is_set() or time.monotonic() >= deadline:
                raise TimeoutError(f"SSH operation exceeded {timeout:g} seconds") from None
            raise
        if expired.is_set() or time.monotonic() >= deadline:
            raise TimeoutError(f"SSH operation exceeded {timeout:g} seconds")
        return result
    finally:
        finished.set()
        timer.cancel()
        timer.join(timeout=1)

def get_or_create_ssh_keys() -> Tuple[str, str]:
    """
    Returns (private_key_pem, public_key_openssh).
    Persists key in ~/.ssh/runpod_videogen/.
    """
    if os.path.exists(_SSH_PRIV_FILE) and os.path.exists(_SSH_PUB_FILE):
        try:
            with open(_SSH_PRIV_FILE, "r", encoding="utf-8") as f:
                priv = f.read()
            with open(_SSH_PUB_FILE, "r", encoding="utf-8") as f:
                pub = f.read().strip()
            if priv and pub:
                return priv, pub
        except Exception as e:
            print(f"[SSH] Warning loading existing keys: {e}")

    os.makedirs(_SSH_KEY_DIR, exist_ok=True)
    key_obj = paramiko.RSAKey.generate(2048)
    sio = io.StringIO()
    key_obj.write_private_key(sio)
    priv_str = sio.getvalue()
    pub_str = f"ssh-rsa {key_obj.get_base64()} autogenerated-runpod-videogen"

    with open(_SSH_PRIV_FILE, "w", encoding="utf-8") as f:
        f.write(priv_str)
    with open(_SSH_PUB_FILE, "w", encoding="utf-8") as f:
        f.write(pub_str)

    return priv_str, pub_str

class SSHManager:
    def __init__(self, host: str, port: int, log_callback: Optional[Callable[[str], None]] = None):
        self.host = host
        self.port = port
        self.log_callback = log_callback
        self.priv_key_str, self.pub_key_str = get_or_create_ssh_keys()

    def _log(self, msg: str):
        if self.log_callback:
            self.log_callback(msg)
        else:
            print(f"[SSHManager] {msg}")

    def get_client(self, timeout: int = 15) -> paramiko.SSHClient:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            pkey = paramiko.RSAKey.from_private_key(io.StringIO(self.priv_key_str))
            _deadline_call(client, lambda: client.connect(
                    hostname=self.host,
                    port=self.port,
                    username="root",
                    pkey=pkey,
                    timeout=timeout,
                    banner_timeout=min(15, timeout),
                    auth_timeout=min(15, timeout),
                ), time.monotonic() + timeout, timeout)
        except Exception:
            client.close()
            raise
        return client

    def exec_command(self, cmd: str, timeout: int = 60) -> Tuple[int, str, str]:
        """Runs a command and returns (exit_code, stdout, stderr).

        Baut auf exec_command_stream auf: Beide Streams werden laufend geleert
        (kein vollgelaufenes SSH-Fenster) und das Zeitlimit gilt für die
        gesamte Ausführung. Läuft es ab, wird der Kanal geschlossen und
        (-1, "", Hinweis) zurückgegeben statt unbegrenzt im
        recv_exit_status() zu hängen."""
        try:
            return self.exec_command_stream(cmd, timeout=timeout)
        except (TimeoutError, InterruptedError) as e:
            return -1, "", f"SSH-Zeitlimit überschritten ({type(e).__name__}): {e}"

    def delete_verified_file(self, remote_path: str, sha256: str) -> bool:
        """Delete only the quarantined remote inode matching the local hash.

        The single remote operation atomically moves the pathname aside first.
        On mismatch/error it restores without overwriting any replacement, or
        keeps a visible backup with the media extension and reports its path.
        """
        if (not isinstance(remote_path, str) or not remote_path or "\x00" in remote_path
                or not isinstance(sha256, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", sha256)):
            self._log("Remote-Löschen verweigert: ungültiger Pfad oder SHA256.")
            return False
        command = (f"python3 - {shlex.quote(remote_path)} {shlex.quote(sha256.lower())} "
                   "<<'RUNPOD_VERIFIED_DELETE_PY'\n" + _VERIFIED_DELETE_SCRIPT
                   + "\nRUNPOD_VERIFIED_DELETE_PY")
        code, output, error = self.exec_command(command, timeout=120)
        try:
            result = json.loads(output.strip()) if code == 0 else None
        except (ValueError, TypeError):
            result = None
        if isinstance(result, dict) and result.get("deleted") is True:
            return True
        if isinstance(result, dict):
            retained = ", ".join(str(path) for path in result.get("retained", []))
            self._log(f"Remote-Datei erhalten: {result.get('reason', 'Prüfung fehlgeschlagen')}; "
                      f"gespeichert unter: {retained}")
        else:
            self._log(f"Remote-Löschen nicht bestätigt ({code}): {error or output}; "
                      "ggf. .runpod-delete-*.quarantine im Ausgabeordner prüfen.")
        return False

    def exec_command_stream(
        self,
        cmd: str,
        timeout: float = 60,
        on_line: Optional[Callable[[str], None]] = None,
        cancelled: Optional[Callable[[], bool]] = None,
    ) -> Tuple[int, str, str]:
        """Run a command while draining both streams and reporting complete lines.

        CR, LF and CRLF delimit progress lines; split UTF-8 characters are decoded
        incrementally. The returned stdout/stderr and any unfinished single line
        retain at most 200,000 characters each. A trailing line without a newline
        is reported on successful completion as well.

        ``timeout`` sets an absolute deadline, not an inactivity timeout. Expiry
        raises TimeoutError; a true ``cancelled()`` raises InterruptedError.
        Callback failures propagate. In every case the SSH client is closed.
        Closing this channel is not a guarantee that a remote process is killed.
        """
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        deadline = time.monotonic() + timeout
        client = None
        streams = {
            name: {
                "decoder": codecs.getincrementaldecoder("utf-8")(errors="replace"),
                "tail": "", "line": "", "last_cr": False,
            }
            for name in ("stdout", "stderr")
        }

        def check_running():
            if cancelled is not None and cancelled():
                raise InterruptedError("SSH command cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError(f"SSH command exceeded {timeout:g} seconds")

        def consume(name, data, final=False):
            stream = streams[name]
            text = stream["decoder"].decode(data, final=final)
            stream["tail"] = (stream["tail"] + text)[-_STREAM_TAIL_CHARS:]
            if text:
                # A CRLF pair may be split across network packets.
                if stream["last_cr"] and text.startswith("\n"):
                    text = text[1:]
                stream["last_cr"] = text.endswith("\r")
                start = 0
                for match in re.finditer(r"\r\n|\r|\n", text):
                    line = (stream["line"] + text[start:match.start()])[-_STREAM_TAIL_CHARS:]
                    if on_line is not None:
                        on_line(line)
                    stream["line"] = ""
                    start = match.end()
                stream["line"] = (stream["line"] + text[start:])[-_STREAM_TAIL_CHARS:]
            if final and stream["line"]:
                if on_line is not None:
                    on_line(stream["line"])
                stream["line"] = ""

        try:
            check_running()
            client = self.get_client(timeout=min(15, timeout))
            check_running()
            _, stdout, _ = _deadline_call(
                client,
                lambda: client.exec_command(cmd, timeout=max(0.001, deadline - time.monotonic())),
                deadline, timeout,
            )
            channel = stdout.channel
            while True:
                check_running()
                received = False
                # Alternate streams so a busy stdout cannot starve stderr (or
                # cancellation). Never wait for exit before draining the window.
                if channel.recv_ready():
                    consume("stdout", channel.recv(32_768))
                    received = True
                check_running()
                if channel.recv_stderr_ready():
                    consume("stderr", channel.recv_stderr(32_768))
                    received = True
                check_running()
                if (channel.exit_status_ready()
                        and not channel.recv_ready()
                        and not channel.recv_stderr_ready()):
                    consume("stdout", b"", final=True)
                    check_running()
                    consume("stderr", b"", final=True)
                    check_running()
                    return channel.recv_exit_status(), streams["stdout"]["tail"], streams["stderr"]["tail"]
                if not received:
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
        finally:
            if client is not None:
                client.close()

    def sync_videos(self, remote_dir: str = "/workspace/Wan2GP/outputs") -> List[str]:
        """
        Scans remote_dir for .mp4 files and downloads newly completed files
        to local outputs/ folder. Returns list of new filenames downloaded.

        SFTP-Zugriffe sind über einen Kanal-Timeout begrenzt; abgebrochene
        Downloads hinterlassen keine fertige Zieldatei (.part wird entfernt).
        Lokale Namen sind kollisionsfrei: Das Mapping wird erst nach
        erfolgreichem Download geschrieben.
        """
        local_dir = get_outputs_dir()
        downloaded = []

        try:
            client = self.get_client(timeout=10)
        except Exception as e:
            self._log(f"SFTP-Verbindung fehlgeschlagen: {e}")
            return []

        try:
            sftp = _deadline_call(client, client.open_sftp,
                                  time.monotonic() + SFTP_IO_TIMEOUT_SECONDS,
                                  SFTP_IO_TIMEOUT_SECONDS)
            # Also protect subsystem ACK above, before a usable SFTP channel
            # exists. Every subsequent socket read observes this timeout.
            sftp.get_channel().settimeout(SFTP_IO_TIMEOUT_SECONDS)
            try:
                files = _deadline_call(client, lambda: sftp.listdir_attr(remote_dir),
                                       time.monotonic() + SFTP_IO_TIMEOUT_SECONDS,
                                       SFTP_IO_TIMEOUT_SECONDS)
            except IOError:
                # remote dir doesn't exist yet
                return []

            # Bereits belegte lokale Namen: existierende Dateien plus aktuell
            # gültiges Mapping (Windows: ohne Groß-/Kleinschreibung).
            local_files = set(os.listdir(local_dir))
            taken_lower = {n.lower() for n in local_files}
            name_map = load_name_map()
            taken_lower.update(n.lower() for n in name_map)

            for attr in files:
                fname = attr.filename
                if "/" in fname or "\x00" in fname:
                    continue
                if not (fname.lower().endswith(".mp4") or fname.lower().endswith(".png") or fname.lower().endswith(".jpg")):
                    continue

                # Ignore files modified in the last 15 seconds (might still be writing)
                if time.time() - attr.st_mtime < 15:
                    continue

                raw_name = _windows_safe_name(fname)
                if not raw_name:
                    continue
                # Only a checksum-backed association may skip a download.
                # Legacy mappings and matching names/sizes prove no ownership.
                candidates = [name for name, original in name_map.items()
                              if original == fname and name == os.path.basename(name)
                              and "/" not in name and "\\" not in name]
                if (raw_name == fname and raw_name in local_files and raw_name not in candidates
                        and name_map.get(raw_name) in (None, fname)):
                    candidates.append(raw_name)
                known_complete = False
                for candidate in candidates:
                    identity = get_verified_identity(candidate, os.path.join(local_dir, candidate))
                    if (identity and identity["remote"] == fname and identity["size"] == attr.st_size
                            and identity["remote_mtime"] == attr.st_mtime):
                        known_complete = True
                        break
                if known_complete:
                    continue

                remote_path = f"{remote_dir}/{fname}"
                fd, part_path = tempfile.mkstemp(dir=local_dir, prefix=".sync-", suffix=".part")
                os.close(fd)
                self._log(f"Lade Datei herunter: {fname} ({attr.st_size / (1024*1024):.2f} MB)...")

                try:
                    _deadline_call(client, lambda: sftp.get(remote_path, part_path),
                                   time.monotonic() + SFTP_TRANSFER_TIMEOUT_SECONDS,
                                   SFTP_TRANSFER_TIMEOUT_SECONDS)
                    if os.path.getsize(part_path) != attr.st_size:
                        raise IOError("Remote-Dateigröße änderte sich während des Downloads")
                    digest = _sha256_file(part_path)
                    matched = None
                    for candidate in candidates:
                        local_path = os.path.join(local_dir, candidate)
                        try:
                            if (os.path.getsize(local_path) == attr.st_size
                                    and _sha256_file(local_path) == digest):
                                matched = candidate
                                break
                        except OSError:
                            pass
                    if matched is not None:
                        # Migration requires identical bytes, never size alone.
                        _record_download_identity(matched, fname,
                                                  os.path.join(local_dir, matched), digest, attr.st_mtime)
                        name_map = load_name_map()
                        continue

                    taken_lower.update(n.lower() for n in os.listdir(local_dir))
                    safe_local_name = sanitize_remote_name(fname, taken_lower)
                    if not safe_local_name:
                        # A previous version of this remote already uses the
                        # stable hash name. Preserve it and find another free
                        # name; do not overwrite an old or user-modified video.
                        stem, ext = os.path.splitext(raw_name)
                        index = 2
                        while f"{stem}__{index}{ext}".lower() in taken_lower:
                            index += 1
                        safe_local_name = f"{stem}__{index}{ext}"
                    local_path = os.path.join(local_dir, safe_local_name)
                    # Same-directory hardlink publishes a complete download
                    # atomically and refuses to overwrite even if another
                    # operation claimed the name since the inventory snapshot.
                    os.link(part_path, local_path)
                    taken_lower.add(safe_local_name.lower())
                    downloaded.append(safe_local_name)
                    self._log(f"Download abgeschlossen: {safe_local_name}")
                    _record_download_identity(safe_local_name, fname, local_path, digest, attr.st_mtime)
                    name_map = load_name_map()
                except Exception as dl_err:
                    self._log(f"Fehler beim Download von {fname}: {dl_err}")
                finally:
                    if os.path.exists(part_path):
                        try:
                            os.remove(part_path)
                        except Exception:
                            pass

            sftp.close()
        except Exception as e:
            self._log(f"SFTP Sync Fehler: {e}")
        finally:
            client.close()

        return downloaded
