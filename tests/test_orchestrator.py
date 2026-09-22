# Regressionstests für backend/orchestrator.py
#
# Festschreibung der kritischen Bugfixes:
#   P0-1  Resume-Entscheidung anhand echter RunPod-desiredStatus-Werte
#         (EXITED -> Resume, RUNNING -> Wiederverwendung, sonst Deploy)
#   P1-6  Reconciliation nach Server-Restart (IP/SSH/Proxy/Kosten)
#   P1-8  Auto-Shutdown respektiert standby_mode (stop vs. terminate)
#   P2-9a Background-Loop überlebt ungültige Config-Werte
#   P2-12 E-Mail-Cache verhindert API-Spam
#   P2-11c/P3-20 Löschen: sicheres Shell-Quoting + korrekter Rückgabewert

import json
import shlex
import threading
import time
from collections import deque
from types import SimpleNamespace

import pytest

import backend.orchestrator as orch
from backend.orchestrator import PodOrchestrator
from tests.conftest import wait_for

REAL_SLEEP = time.sleep


def fast_sleep(_seconds):
    """Ersetzt time.sleep im Test: kürzt Warteschleifen auf 10 ms."""
    REAL_SLEEP(0.01)


RUNNING_POD = {
    "id": "pod123",
    "desiredStatus": "RUNNING",
    "costPerHr": 0.49,
    "runtime": {
        "uptimeInSeconds": 600,
        "ports": [{"privatePort": 22, "ip": "192.0.2.10", "publicPort": 22093}],
    },
}


class FakeRunPodClient:
    def __init__(self):
        self.pods = {}  # pod_id -> dict oder callable(fake) -> dict
        self.resume_calls = []
        self.stop_calls = []
        self.terminate_calls = []
        self.deploy_calls = []
        self.get_myself_calls = 0
        self.email = None

    def get_pod(self, pod_id):
        pod = self.pods.get(pod_id)
        if callable(pod):
            pod = pod(self)
        return pod

    def get_myself(self):
        self.get_myself_calls += 1
        if self.email is None:
            raise RuntimeError("Im Testbetrieb ist kein Netzwerk erlaubt")
        return {"email": self.email}

    def resume_pod(self, pod_id, gpu_count=1):
        self.resume_calls.append(pod_id)
        return True

    def stop_pod(self, pod_id):
        self.stop_calls.append(pod_id)
        return True

    def terminate_pod(self, pod_id):
        self.terminate_calls.append(pod_id)
        return True

    def deploy_pod(self, **kwargs):
        self.deploy_calls.append(kwargs)
        return "newpod-1"


def patch_orchestrator(monkeypatch, fake):
    """Mocket Netzwerk/SSH ab und beschleunigt Warteschleifen."""
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(orch, "get_or_create_ssh_keys", lambda: ("PRIV", "PUB"))
    boots = []
    monkeypatch.setattr(
        PodOrchestrator, "_bootstrap_wan2gp",
        lambda self, ip, port: boots.append((ip, port)) or True,
    )
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)
    return boots


def read_cfg(orch_env):
    return json.loads(orch_env.cfg_file.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- P0-1: Resume

def test_worker_resumes_exited_pod_instead_of_deploying(orch_env, monkeypatch):
    """Kernregression P0-1: STOPPTER Pod (desiredStatus EXITED) wird resumed,
    NICHT neu deployed. Der alte Bug hat jeden 'Aufwecken'-Klick einen neuen
    Pod gekauft und den alten als Kostenquelle verlassen."""
    fake = FakeRunPodClient()
    boots = patch_orchestrator(monkeypatch, fake)
    calls = {"n": 0}

    def pod_sequence(_fake):
        calls["n"] += 1
        return {"desiredStatus": "EXITED"} if calls["n"] == 1 else RUNNING_POD

    fake.pods["pod123"] = pod_sequence

    o = PodOrchestrator()
    o.pod_id = "pod123"
    o._deploy_or_resume_worker()

    assert fake.resume_calls == ["pod123"], "EXITED-Pod muss per podResume geweckt werden"
    assert fake.deploy_calls == [], "Es darf KEIN zweiter Pod deployed werden"
    assert o.status == "RUNNING"
    assert boots == [("192.0.2.10", 22093)]
    assert read_cfg(orch_env)["active_pod_status"] == "RUNNING"
    o._stop_bg_thread = True


def test_worker_reuses_already_running_pod(orch_env, monkeypatch):
    """P0-1: Läuft der persistierte Pod bereits (z. B. nach ERROR oder Restart),
    wird er wiederverwendet - weder Resume noch Deploy."""
    fake = FakeRunPodClient()
    patch_orchestrator(monkeypatch, fake)
    fake.pods["pod123"] = dict(RUNNING_POD)

    o = PodOrchestrator()
    o.pod_id = "pod123"
    o._deploy_or_resume_worker()

    assert fake.resume_calls == []
    assert fake.deploy_calls == []
    assert o.status == "RUNNING"
    assert o.proxy_url == "https://pod123-7860.proxy.runpod.net"
    o._stop_bg_thread = True


def test_worker_deploys_new_pod_when_none_exists(orch_env, monkeypatch):
    fake = FakeRunPodClient()
    patch_orchestrator(monkeypatch, fake)
    fake.pods["newpod-1"] = dict(RUNNING_POD)

    o = PodOrchestrator()
    assert o.pod_id is None
    o._deploy_or_resume_worker()

    assert len(fake.deploy_calls) == 1
    kwargs = fake.deploy_calls[0]
    assert kwargs["gpu_type_id"] == "NVIDIA A40"       # aus der Config gelesen
    assert kwargs["cloud_type"] == "ALL"
    assert kwargs["public_ssh_key"] == "PUB"
    assert o.pod_id == "newpod-1"
    assert read_cfg(orch_env)["active_pod_id"] == "newpod-1"
    assert o.status == "RUNNING"
    o._stop_bg_thread = True


def test_worker_timeout_sets_error_state(orch_env, monkeypatch):
    """Liefert RunPod nie einen SSH-Port, muss der Status ERROR werden
    (und pod_id bleibt erhalten, damit der Löschen-Button eingreifen kann)."""
    fake = FakeRunPodClient()
    patch_orchestrator(monkeypatch, fake)
    fake.pods["pod123"] = {"desiredStatus": "RUNNING", "runtime": {"ports": []}}

    o = PodOrchestrator()
    o.pod_id = "pod123"
    o._deploy_or_resume_worker()

    assert o.status == "ERROR"
    assert "Timeout" in o.status_detail
    assert o.pod_id == "pod123"
    o._stop_bg_thread = True


def test_start_pod_rejected_while_running(orch_env, monkeypatch):
    fake = FakeRunPodClient()
    patch_orchestrator(monkeypatch, fake)
    o = PodOrchestrator()
    o.status = "RUNNING"

    res = o.start_pod()

    assert res["success"] is False
    o._stop_bg_thread = True


# ------------------------------------------------------- P1-6: Reconciliation

def test_reconcile_restores_running_pod_after_restart(orch_env, monkeypatch):
    fake = FakeRunPodClient()
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    # Dieser Test prüft die Zustandserholung; WebUI-/SSH-Prüfungen haben eigene
    # Tests und dürfen hier keinen echten Hintergrund-Verbindungsversuch starten.
    monkeypatch.setattr(PodOrchestrator, "_ensure_webui_after_reconcile", lambda self, ip, port: None)
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)
    fake.pods["pod123"] = dict(RUNNING_POD)
    orch_env.write_cfg(active_pod_id="pod123", active_pod_status="RUNNING")

    o = PodOrchestrator()  # __init__ startet die Reconciliation

    assert wait_for(lambda: o.pod_ip == "192.0.2.10"), "IP muss wiederhergestellt werden"
    assert o.pod_ssh_port == 22093
    assert o.proxy_url == "https://pod123-7860.proxy.runpod.net"
    assert o.hourly_rate == pytest.approx(0.49)
    assert o.session_start_time is not None
    assert o.status == "RUNNING"
    o._stop_bg_thread = True


def test_reconcile_marks_exited_pod_as_stopped(orch_env, monkeypatch):
    fake = FakeRunPodClient()
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)
    fake.pods["pod123"] = {"desiredStatus": "EXITED"}
    orch_env.write_cfg(active_pod_id="pod123", active_pod_status="RUNNING")

    o = PodOrchestrator()

    assert wait_for(lambda: o.status == "STOPPED")
    assert o.pod_id == "pod123"  # Standby-Pod bleibt referenziert (Resume-fähig)
    o._stop_bg_thread = True


def test_reconcile_cleans_up_vanished_pod(orch_env, monkeypatch):
    fake = FakeRunPodClient()
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)
    fake.pods["pod123"] = None  # Pod existiert auf RunPod nicht mehr
    orch_env.write_cfg(active_pod_id="pod123", active_pod_status="RUNNING")

    o = PodOrchestrator()

    # Auf die persistierte Bereinigung warten (update_config läuft nach pod_id=None)
    assert wait_for(lambda: read_cfg(orch_env)["active_pod_id"] is None)
    assert o.pod_id is None
    assert o.status == "OFFLINE"
    o._stop_bg_thread = True


# ------------------------------------------- P2-9a / P1-8: Background-Loop

def test_background_loop_survives_invalid_config_values(orch_env, monkeypatch):
    """Ein kaputter Config-Wert darf den Kostenschutz-Watchdog NIEMALS still töten."""
    fake = FakeRunPodClient()
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    orch_env.write_cfg(auto_shutdown_minutes="keine-zahl", sync_interval_seconds="auch-nicht")

    o = PodOrchestrator()
    o.status = "RUNNING"
    o.session_start_time = time.time()
    o.last_activity_time = time.time()

    REAL_SLEEP(5.0)  # echte Loop-Takte (2 s) abwarten

    assert o.bg_thread.is_alive(), "Background-Loop muss nach Config-Fehlern weiterlaufen"
    o._stop_bg_thread = True


def test_auto_shutdown_terminates_in_terminate_mode(orch_env, monkeypatch):
    fake = FakeRunPodClient()
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)
    orch_env.write_cfg(auto_shutdown_minutes=1, standby_mode="terminate")

    o = PodOrchestrator()
    o.pod_id = "pod123"
    o.status = "RUNNING"
    o.session_start_time = None
    o.last_activity_time = time.time() - 120  # 2 Minuten idle > 1 Minute Limit

    assert wait_for(lambda: bool(fake.terminate_calls)), "Auto-Shutdown muss terminieren"
    assert fake.terminate_calls == ["pod123"]
    assert fake.stop_calls == []
    o._stop_bg_thread = True


def test_auto_shutdown_stops_in_default_mode(orch_env, monkeypatch):
    fake = FakeRunPodClient()
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)
    orch_env.write_cfg(auto_shutdown_minutes=1, standby_mode="stop")

    o = PodOrchestrator()
    o.pod_id = "pod123"
    o.status = "RUNNING"
    o.session_start_time = None
    o.last_activity_time = time.time() - 120

    assert wait_for(lambda: bool(fake.stop_calls)), "Auto-Shutdown muss stoppen (pausieren)"
    assert fake.stop_calls == ["pod123"]
    assert fake.terminate_calls == []
    assert o.status == "STOPPED"
    o._stop_bg_thread = True


def test_auto_shutdown_disabled_never_stops(orch_env, monkeypatch):
    fake = FakeRunPodClient()
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)
    orch_env.write_cfg(auto_shutdown_minutes=0)

    o = PodOrchestrator()
    o.pod_id = "pod123"
    o.status = "RUNNING"
    o.session_start_time = None
    o.last_activity_time = time.time() - 99999

    REAL_SLEEP(1.0)
    assert fake.stop_calls == [] and fake.terminate_calls == []
    o._stop_bg_thread = True


# ---------------------------------------------------------------- P2-12: Cache

def test_account_email_is_cached(orch_env, monkeypatch):
    fake = FakeRunPodClient()
    fake.email = "tester@example.com"
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)

    o = PodOrchestrator()
    assert o.get_account_email() == "tester@example.com"
    assert o.get_account_email() == "tester@example.com"
    assert fake.get_myself_calls == 1, "Zweiter Aufruf muss aus dem Cache kommen (kein API-Poll)"
    assert read_cfg(orch_env)["account_email"] == "tester@example.com"
    o._stop_bg_thread = True


def test_account_email_falls_back_to_config_on_error(orch_env, monkeypatch):
    fake = FakeRunPodClient()  # email=None -> get_myself wirft
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)
    orch_env.write_cfg(account_email="fallback@example.com")

    o = PodOrchestrator()
    assert o.get_account_email() == "fallback@example.com"
    o._stop_bg_thread = True


# --------------------------------------------- P2-11c / P3-20: Video-Löschen

class FakeSSHManager:
    commands = []

    def __init__(self, host, port, log_callback=None):
        pass

    def exec_command(self, cmd, timeout=60):
        FakeSSHManager.commands.append(cmd)
        return (0, "", "")


def test_delete_video_quotes_filename_shell_safe(orch_env, monkeypatch):
    monkeypatch.setattr(orch, "SSHManager", FakeSSHManager)
    FakeSSHManager.commands = []

    o = PodOrchestrator()
    o.status = "RUNNING"
    o.pod_ip = "192.0.2.10"
    o.pod_ssh_port = 22093
    evil_name = "it's ;tricky.mp4"
    (orch_env.outputs / evil_name).write_bytes(b"data")

    assert o.delete_video(evil_name) is True
    assert not (orch_env.outputs / evil_name).exists()

    cmd = FakeSSHManager.commands[-1]
    expected_path = "/workspace/Wan2GP/outputs/it's ;tricky.mp4"
    assert shlex.split(cmd) == ["rm", "-f", expected_path], (
        f"Shell-Zeile muss parsesicher sein, war: {cmd}"
    )
    o._stop_bg_thread = True


def test_delete_video_returns_false_when_nothing_deleted(orch_env, monkeypatch):
    monkeypatch.setattr(orch, "SSHManager", FakeSSHManager)

    o = PodOrchestrator()
    o.status = "OFFLINE"  # kein Remote-Versuch
    o.pod_ip = None

    assert o.delete_video("gibtsnicht.mp4") is False
    o._stop_bg_thread = True


def test_delete_video_uses_original_remote_name_via_mapping(orch_env, monkeypatch):
    """Wurde ein Dateiname für Windows sanitiziert, muss das Remote-Löschen den
    ORIGINAL-Namen treffen - sonst bleibt die Datei auf dem Pod und wird erneut
    synchronisiert."""
    monkeypatch.setattr(orch, "SSHManager", FakeSSHManager)
    FakeSSHManager.commands = []
    from backend.ssh_client import save_name_map, load_name_map
    save_name_map({'bild_final_.mp4': 'bild:final*.mp4'})
    (orch_env.outputs / 'bild_final_.mp4').write_bytes(b"data")

    o = PodOrchestrator()
    o.status = "RUNNING"
    o.pod_ip = "192.0.2.10"
    o.pod_ssh_port = 22093

    assert o.delete_video("bild_final_.mp4") is True

    cmd = FakeSSHManager.commands[-1]
    assert shlex.split(cmd) == ["rm", "-f", "/workspace/Wan2GP/outputs/bild:final*.mp4"], (
        f"Remote-Pfad muss den Original-Namen nutzen, war: {cmd}"
    )
    assert "bild_final_.mp4" not in load_name_map(), "Mapping-Eintrag muss nach dem Löschen verschwinden"
    o._stop_bg_thread = True


# ------------------------------------------------------- GPU-Verfügbarkeit

def test_out_of_stock_error_detection():
    """Erkennt RunPods 'No longer any instances available' und Varianten."""
    assert PodOrchestrator.is_out_of_stock_error(
        "Error generating run: No longer any 'NVIDIA RTX 4090' instances available")
    assert PodOrchestrator.is_out_of_stock_error("Diese GPU ist ausgebucht")
    assert PodOrchestrator.is_out_of_stock_error("This GPU is SOLD OUT")
    assert not PodOrchestrator.is_out_of_stock_error("SSH connection failed")


def test_gpu_availability_marks_expire(api_env, monkeypatch):
    srv = api_env["server"]
    srv.orchestrator.mark_gpu_unavailable("NVIDIA A40")
    assert srv.orchestrator.get_gpu_availability() == {"NVIDIA A40": False}

    # TTL auf "bereits abgelaufen" setzen -> Markierung fällt automatisch weg
    monkeypatch.setattr(PodOrchestrator, "GPU_OUT_OF_STOCK_TTL_SECONDS", -1)
    assert srv.orchestrator.get_gpu_availability() == {}


def test_bootstrap_webui_check_is_port_based():
    """Regression: Der Bootstrap prüfte per pgrep auf 'wgp.py' - das matchet über
    den bash -c-Wrapper die SSH-Shell selbst und startete wgp.py nie neu."""
    import inspect

    source = inspect.getsource(PodOrchestrator._bootstrap_wan2gp)
    assert "http://127.0.0.1:7860" in source
    assert 'pgrep -f "wgp.py"' not in source


@pytest.fixture()
def webui_probe(monkeypatch):
    """Prüft Start/Readiness ohne Threads, Config, SSH-Schlüssel oder Netzwerk."""
    o = PodOrchestrator.__new__(PodOrchestrator)
    o.lock = threading.Lock()
    o.log_history = deque()
    o.listeners = set()
    o.status_detail = "Prüfe WebUI"
    o.proxy_url = "https://pod123-7860.proxy.runpod.net"
    o.startup_progress = {
        "phase": "cancelled", "label": "Kein Start aktiv", "detail": "",
        "started_at": None, "updated_at": None, "active": False,
    }

    class FakeSSH:
        def __init__(self):
            self.calls = []
            self.probe_results = [(7, "", "Connection refused")]
            self.bootstrap_result = (0, "Bootstrap ausgeführt", "")
            self.cuda_result = (0, "CUDA_READY\n", "")
            self.log_result = (0, "RuntimeError: model import failed\n", "")
            self.stream_completed = False
            self.after_stream_line = None

        def get_client(self, **kwargs):
            return SimpleNamespace(close=lambda: None)

        def exec_command(self, command, timeout):
            self.calls.append((command, timeout))
            if command.startswith("python3 - <<'VIDEOGEN_CUDA'"):
                assert timeout == 30
                return self.cuda_result
            if command.startswith("tail "):
                if isinstance(self.log_result, Exception):
                    raise self.log_result
                return self.log_result
            if "set -e" in command:
                return self.bootstrap_result
            assert command.startswith("curl ")
            assert shlex.split(command)[shlex.split(command).index("-m") + 1] == "5"
            if len(self.probe_results) > 1:
                return self.probe_results.pop(0)
            return self.probe_results[0]

        def exec_command_stream(self, command, timeout, on_line=None, cancelled=None):
            result = self.exec_command(command, timeout)
            for line in (result[1] + "\n" + result[2]).splitlines():
                if cancelled and cancelled():
                    return -1, result[1], result[2]
                if on_line:
                    on_line(line)
                if self.after_stream_line:
                    self.after_stream_line(line)
            self.stream_completed = True
            return result

    ssh = FakeSSH()
    monkeypatch.setattr(orch, "SSHManager", lambda *args, **kwargs: ssh)
    # Nicht das globale time-Modul verändern: andere Tests nutzen echte Uhren.
    monkeypatch.setattr(orch, "time", SimpleNamespace(
        sleep=lambda _: None, strftime=time.strftime, time=time.time,
    ))
    return o, ssh


@pytest.mark.parametrize("probe_result", [
    (7, "", "Connection refused"),
    (7, "READY\n", "curl failed"),
    (0, "NOT READY\n", ""),
])
def test_bootstrap_readiness_failure_returns_false_and_reads_log(webui_probe, probe_result):
    o, ssh = webui_probe
    ssh.probe_results = [probe_result]

    assert o._bootstrap_wan2gp("192.0.2.10", 22093) is False
    assert sum(command.startswith("curl ") for command, _ in ssh.calls) == 18
    assert ssh.calls[-1] == ("tail -n 80 /workspace/wan2gp.log", 15)
    assert any("RuntimeError: model import failed" in entry for entry in o.log_history)
    assert not any("erreichbar und bereit" in entry for entry in o.log_history)


def test_bootstrap_retries_until_http_success(webui_probe):
    o, ssh = webui_probe
    ssh.probe_results = [(7, "", "Connection refused"), (0, "READY\n", "")]

    assert o._bootstrap_wan2gp("192.0.2.10", 22093) is True
    assert sum(command.startswith("curl ") for command, _ in ssh.calls) == 2
    assert not any(command.startswith("tail ") for command, _ in ssh.calls)


def test_bootstrap_failure_is_preserved_when_log_cannot_be_read(webui_probe):
    o, ssh = webui_probe
    ssh.log_result = TimeoutError("SSH log timeout")

    assert o._bootstrap_wan2gp("192.0.2.10", 22093) is False
    assert any("SSH log timeout" in entry for entry in o.log_history)


def test_bootstrap_command_failure_reads_log_without_readiness_poll(webui_probe):
    o, ssh = webui_probe
    ssh.bootstrap_result = (1, "", "pip failed")

    assert o._bootstrap_wan2gp("192.0.2.10", 22093) is False
    assert not any(command.startswith("curl ") for command, _ in ssh.calls)
    assert any("RuntimeError: model import failed" in entry for entry in o.log_history)


def test_cuda_failure_stops_before_package_installation_and_reports_cause(webui_probe):
    o, ssh = webui_probe
    ssh.cuda_result = (1, "", "CUDA_ERROR_UNKNOWN: /dev/nvidia-uvm: Input/output error")

    assert o._bootstrap_wan2gp("192.0.2.10", 22093) is False

    assert len(ssh.calls) == 1
    assert ssh.calls[0][0].startswith("python3 - <<'VIDEOGEN_CUDA'")
    assert ssh.stream_completed is False
    assert o.startup_progress["phase"] == "ssh"
    assert "GPU-Zugriff" in o.startup_progress["label"]
    assert "Input/output error" in o.startup_progress["detail"]
    assert any("CUDA-Prüfung fehlgeschlagen" in line for line in o.log_history)


@pytest.mark.parametrize("exit_code, restart_expected", [(0, False), (7, True)])
def test_reconcile_readiness_requires_successful_exit_code(webui_probe, monkeypatch, exit_code, restart_expected):
    o, ssh = webui_probe
    ssh.probe_results = [(exit_code, "READY\n", "")]
    restarts = []
    monkeypatch.setattr(o, "_bootstrap_wan2gp", lambda ip, port: restarts.append((ip, port)) or False)

    o._ensure_webui_after_reconcile("192.0.2.10", 22093)

    assert bool(restarts) is restart_expected
    if restart_expected:
        assert "fehlgeschlagen" in o.status_detail
        assert o.status == "ERROR"
        assert o.startup_progress["phase"] == "error"
    else:
        assert o.status_detail == "Wan2GP aktiv & verbunden"
        assert o.status == "RUNNING"
        assert o.startup_progress["phase"] == "ready"
    assert o.startup_progress["active"] is False


def test_bootstrap_stream_updates_progress_before_remote_command_finishes(webui_probe):
    o, ssh = webui_probe
    ssh.bootstrap_result = (
        0,
        "[VIDEOGEN_PHASE] install\nCollecting mmgp==3.8.0\n"
        "[VIDEOGEN_PHASE] launch\n[BOOTSTRAP] Starte Wan2GP WebUI\n",
        "",
    )
    ssh.probe_results = [(0, "READY\n", "")]
    observed = []

    def observe(line):
        if line.startswith("Collecting ") or line.startswith("[BOOTSTRAP]"):
            observed.append((dict(o.startup_progress), ssh.stream_completed))

    ssh.after_stream_line = observe

    assert o._bootstrap_wan2gp("192.0.2.10", 22093) is True

    assert len(observed) == 2
    install, launch = observed
    assert install[0]["phase"] == "install"
    assert install[0]["detail"] == "Collecting mmgp==3.8.0"
    assert launch[0]["phase"] == "launch"
    assert launch[0]["detail"] == "[BOOTSTRAP] Starte Wan2GP WebUI"
    assert all(progress["active"] for progress, _ in observed)
    assert all(completed is False for _, completed in observed)
    assert install[0]["started_at"] == launch[0]["started_at"]


def test_late_stream_lines_cannot_restore_cancelled_startup_progress(webui_probe, monkeypatch):
    o, ssh = webui_probe
    o._operation_id = 1
    o.pod_id = "pod123"
    o.status = "BOOTSTRAPPING"
    cancelled_progress = []

    def stream_then_abort(command, timeout, on_line, cancelled):
        on_line("Collecting mmgp==3.8.0")
        o._operation_id += 1
        o.status = "TERMINATED"
        o.status_detail = "Pod gelöscht"
        o._set_startup_progress("cancelled", "Start abgebrochen", "Pod gelöscht", o._operation_id)
        cancelled_progress.append(dict(o.startup_progress))
        assert cancelled() is True
        # Ein bereits gepufferter Callback kommt nach dem Abbruch noch an.
        on_line("[VIDEOGEN_PHASE] launch")
        on_line("[BOOTSTRAP] Starte Wan2GP WebUI")
        return -1, "", ""

    monkeypatch.setattr(ssh, "exec_command_stream", stream_then_abort)

    assert o._bootstrap_wan2gp("192.0.2.10", 22093) is False

    assert o.status == "TERMINATED"
    assert o.status_detail == "Pod gelöscht"
    assert o.startup_progress == cancelled_progress[0]
    assert o.startup_progress["active"] is False
    assert not any("Starte Wan2GP WebUI" in line for line in o.log_history)
    assert len(ssh.calls) == 1
    assert ssh.calls[0][0].startswith("python3 - <<'VIDEOGEN_CUDA'")
    assert not any(command.startswith(("curl ", "tail ")) for command, _ in ssh.calls), (
        "Nach Abbruch keine weitere HTTP-Prüfung oder Log-Abfrage"
    )


def test_bootstrap_prefers_upstream_public_url_flag():
    """Regression: Neue Wan2GP-Versionen (Upstream-RunPod-Fix, Sept 2026) bringen
    --public-url mit. Der Bootstrap muss das Flag nutzen, statt am veraenderten
    same_origin-Source den Patch verweigern zu lassen und komplett zu sterben."""
    import inspect

    source = inspect.getsource(PodOrchestrator._bootstrap_wan2gp)
    # Neuer Pfad: Flag erkennen und an wgp.py durchreichen
    assert "--public-url" in source
    assert "$PUBLIC_URL_ARG" in source
    assert "parse_public_url\|\--public-url\|shared/authentication/__init__.py" in source or \
           "shared/authentication/__init__.py" in source
    # Alter Pfad bleibt fuer aeltere Wan2GP-Versionen erhalten
    assert "patch_wan2gp_proxy" in source


def test_bootstrap_upgrades_old_torch_and_guards_triton_patch():
    """Regression: comfy-kitchen 0.2.35 (Wan2GP-Pin) braucht torch >= 2.5 und
    triton >= 3.2. Der Bootstrap muss alten Containern (torch 2.4/triton 3.0)
    ein Upgrade verpassen und den Triton-Wrapper fuer triton >= 3.2 sperren,
    sonst wirft wgp.py beim Import infer_schema-Fehler."""
    import inspect

    source = inspect.getsource(PodOrchestrator._bootstrap_wan2gp)
    assert "torch==2.6.0" in source
    assert ">= (2, 5)" in source
    assert ">= (3, 2)" in source
    # Der Triton-Wrapper steht innerhalb der Versionsabfrage (nicht mehr bedingungslos)
    triton_guard_pos = source.find(">= (3, 2)")
    wrapper_pos = source.find("_compatible_jit")
    assert 0 < triton_guard_pos < wrapper_pos
