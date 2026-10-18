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

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

import backend.config_manager as cm
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
    """Liefert RunPod nie einen SSH-Port, gilt der Host als defekt. Bei einem
    BESTANDS-Pod (hier: wiederverwendet) dürfen Daten und Pod-ID erhalten
    bleiben: Der Pod wird datenerhaltend pausiert, nicht terminiert, und es
    wird kein Ersatz gebucht."""
    fake = FakeRunPodClient()
    patch_orchestrator(monkeypatch, fake)
    fake.pods["pod123"] = {"desiredStatus": "RUNNING", "runtime": {"ports": []}}

    o = PodOrchestrator()
    o.pod_id = "pod123"
    o._deploy_or_resume_worker()

    assert o.status == "STOPPED"
    assert "Bestands-Pod" in o.status_detail
    assert o.pod_id == "pod123", "Pod-ID eines Bestands-Pods muss erhalten bleiben"
    assert fake.stop_calls == ["pod123"], "Bestands-Pod datenerhaltend pausieren"
    assert fake.terminate_calls == [], "Bestands-Pod darf NIEMALS automatisch terminiert werden"
    assert fake.deploy_calls == [], "Keine Ersatzbuchung für Bestands-Pods"
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


@pytest.mark.parametrize("standby_mode", ["stop", "terminate"])
def test_auto_shutdown_applies_to_error_pods(orch_env, monkeypatch, standby_mode):
    """P2-8: Der lokale ERROR-Status darf einen möglicherweise weiterlaufenden
    (kostenpflichtigen) Pod nicht vom Inaktivitäts-Abschalttimer ausnehmen -
    ein Setupfehler darf nicht bedeuten, dass der Pod unbegrenzt weiterläuft."""
    fake = FakeRunPodClient()
    fake.pods["pod123"] = dict(RUNNING_POD)
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)
    orch_env.write_cfg(auto_shutdown_minutes=1, standby_mode=standby_mode)

    o = PodOrchestrator()
    o.pod_id = "pod123"
    o.status = "ERROR"
    o.status_detail = "Bootstrap fehlgeschlagen - Terminal prüfen"
    o.session_start_time = None
    o.last_activity_time = time.time() - 120

    calls = fake.stop_calls if standby_mode == "stop" else fake.terminate_calls
    assert wait_for(lambda: bool(calls)), "Laufender ERROR-Pod muss vom Auto-Shutdown erfasst werden"
    assert calls == ["pod123"]
    o._stop_bg_thread = True


def test_confirmed_exited_error_pod_survives_terminate_idle_timer(orch_env, monkeypatch):
    """Ein alter ERROR-Eintrag darf einen nachweislich pausierten Pod nicht löschen."""
    background_loop = PodOrchestrator._background_loop
    monkeypatch.setattr(PodOrchestrator, "_background_loop", lambda self: None)
    fake = FakeRunPodClient()
    fake.pods["pod123"] = {"desiredStatus": "EXITED"}
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    orch_env.write_cfg(auto_shutdown_minutes=1, standby_mode="terminate")
    o = PodOrchestrator()
    o.bg_thread.join(timeout=1)
    o.pod_id, o.status = "pod123", "ERROR"
    o.status_detail = "Preflight fehlgeschlagen: CUDA-Test"
    o.startup_progress.update(phase="error", detail="CUDA-Test", active=False)
    o.accumulated_cost = 0.25
    o.session_start_time = time.time() - 3600
    o.hourly_rate, o._hourly_rate_pod_id = 0.25, "pod123"
    o.last_activity_time = time.time() - 120

    def one_tick(_seconds):
        o._stop_bg_thread = True

    monkeypatch.setattr(orch.time, "sleep", one_tick)
    background_loop(o)

    assert o.status == "STOPPED"
    assert o.pod_id == "pod123"
    assert fake.stop_calls == fake.terminate_calls == []
    assert "CUDA-Test" in o.status_detail
    assert o.startup_progress["phase"] == "error"
    assert o.session_start_time is None and o.hourly_rate is None
    assert o.accumulated_cost == 0
    assert read_cfg(orch_env)["spent_total"] == 0.25
    assert read_cfg(orch_env)["active_pod_status"] == "STOPPED"


@pytest.mark.parametrize("component", ["ssh", "cuda", "network"])
def test_paused_preflight_failure_survives_later_terminate_idle_timer(orch_env, monkeypatch, component):
    """Der erfolgreiche datenerhaltende Stop bleibt auch nach dem Idle-Limit erhalten."""
    background_loop = PodOrchestrator._background_loop
    monkeypatch.setattr(PodOrchestrator, "_background_loop", lambda self: None)
    fake = FakeRunPodClient()
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    orch_env.write_cfg(auto_shutdown_minutes=1, standby_mode="terminate")
    o = PodOrchestrator()
    o.bg_thread.join(timeout=1)
    o.pod_id, o.status = "pod123", "BOOTSTRAPPING"
    cm.update_config({"active_pod_id": "pod123"})
    o.accumulated_cost = 0.25
    o.hourly_rate, o._hourly_rate_pod_id = 0.25, "pod123"
    o.session_start_time = time.time() - 3600
    o._handle_preflight_failure(o._operation_id, orch.PodPreflightError(component, "Prüfung gescheitert"))
    assert o.status == "STOPPED"
    assert o.startup_progress["phase"] == "error"
    assert component in o.status_detail
    assert o.session_start_time is None and o.accumulated_cost == 0
    assert read_cfg(orch_env)["spent_total"] == 0.25

    o.last_activity_time = time.time() - 120

    def one_tick(_seconds):
        o._stop_bg_thread = True

    monkeypatch.setattr(orch.time, "sleep", one_tick)
    background_loop(o)

    assert o.pod_id == "pod123"
    assert fake.stop_calls == ["pod123"]
    assert fake.terminate_calls == fake.deploy_calls == []
    assert read_cfg(orch_env)["active_pod_status"] == "STOPPED"


def test_hanging_sftp_sync_never_blocks_auto_shutdown(orch_env, monkeypatch):
    """P1-4-Abnahme: Ein hängenbleibender SFTP-Transfer darf den Auto-Shutdown
    (Kostenschutz) nicht blockieren - der Sync läuft dafür in einem eigenen
    Thread und wird nicht gestapelt."""
    fake = FakeRunPodClient()
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)
    orch_env.write_cfg(auto_shutdown_minutes=1, standby_mode="stop", sync_interval_seconds=1)

    sync_gate = threading.Event()
    sync_started = threading.Event()
    sync_instances = []

    class _HangingSSHManager:
        def __init__(self, ip, port, log_callback=None):
            sync_instances.append(self)

        def sync_videos(self, *args, **kwargs):
            sync_started.set()
            # Simuliert einen steckenden SFTP-Transfer (länger als der Test).
            sync_gate.wait(timeout=10)
            return []

    monkeypatch.setattr(orch, "SSHManager", _HangingSSHManager)

    o = PodOrchestrator()
    o.pod_id = "pod123"
    o.status = "RUNNING"
    o.session_start_time = None
    o.last_activity_time = time.time() - 120
    o.pod_ip = "192.0.2.10"
    o.pod_ssh_port = 22093

    try:
        assert sync_started.wait(timeout=5), "Sync muss angestoßen worden sein"
        # Der Kostenschutz muss trotz blockiertem Sync auslösen können.
        assert wait_for(lambda: bool(fake.stop_calls), timeout=5), \
            "Auto-Shutdown muss unabhängig vom hängenden Sync greifen"
        assert fake.stop_calls == ["pod123"]
        # Kein Sync-Stau: trotz vieler Loop-Ticks läuft nur EIN Transfer.
        assert len(sync_instances) == 1, "Hängender Sync darf keinen Thread-Stau erzeugen"
    finally:
        sync_gate.set()  # Testende: Sync-Thread kontrolliert freigeben
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
    remote_digest = hashlib.sha256(b"data").hexdigest()

    def __init__(self, host, port, log_callback=None):
        pass

    def delete_verified_file(self, remote_path, expected_digest):
        FakeSSHManager.commands.append((remote_path, expected_digest))
        return FakeSSHManager.remote_digest == expected_digest


def test_delete_video_quotes_filename_shell_safe(orch_env, monkeypatch):
    monkeypatch.setattr(orch, "SSHManager", FakeSSHManager)
    FakeSSHManager.commands = []

    o = PodOrchestrator()
    o.status = "RUNNING"
    o.pod_ip = "192.0.2.10"
    o.pod_ssh_port = 22093
    evil_name = "it's ;tricky.mp4"
    (orch_env.outputs / evil_name).write_bytes(b"data")
    monkeypatch.setattr(orch, "get_verified_identity", lambda local_name, local_path: {
        "remote": evil_name, "sha256": FakeSSHManager.remote_digest, "size": 4,
    }, raising=False)

    assert o.delete_video(evil_name) is True
    assert not (orch_env.outputs / evil_name).exists()

    expected_path = "/workspace/Wan2GP/outputs/it's ;tricky.mp4"
    assert FakeSSHManager.commands == [(expected_path, FakeSSHManager.remote_digest)]
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
    monkeypatch.setattr(orch, "get_verified_identity", lambda local_name, local_path: {
        "remote": "bild:final*.mp4", "sha256": FakeSSHManager.remote_digest, "size": 4,
    }, raising=False)

    o = PodOrchestrator()
    o.status = "RUNNING"
    o.pod_ip = "192.0.2.10"
    o.pod_ssh_port = 22093

    assert o.delete_video("bild_final_.mp4") is True

    assert FakeSSHManager.commands == [
        ("/workspace/Wan2GP/outputs/bild:final*.mp4", FakeSSHManager.remote_digest),
    ]
    assert "bild_final_.mp4" not in load_name_map(), "Mapping-Eintrag muss nach dem Löschen verschwinden"
    o._stop_bg_thread = True


def test_delete_video_never_trusts_ambiguous_legacy_mapping(orch_env, monkeypatch):
    """Gleiche Dateigröße beweist die Herkunft einer alten Zuordnung nicht."""
    monkeypatch.setattr(orch, "SSHManager", FakeSSHManager)
    monkeypatch.setattr(orch, "get_verified_identity", lambda local_name, local_path: None, raising=False)
    FakeSSHManager.commands = []
    from backend.ssh_client import save_name_map
    save_name_map({"clip_a.mp4": "clip?a.mp4"})
    (orch_env.outputs / "clip_a.mp4").write_bytes(b"AAAA")
    o = PodOrchestrator()
    o.status = "RUNNING"
    o.pod_ip, o.pod_ssh_port = "192.0.2.10", 22093

    assert o.delete_video("clip_a.mp4") is True
    assert not (orch_env.outputs / "clip_a.mp4").exists()
    assert FakeSSHManager.commands == [], "Ohne Provenienz bleibt das fremde Remote-Video unangetastet"
    o._stop_bg_thread = True


def test_delete_video_preserves_replaced_remote_content(orch_env, monkeypatch):
    """Selbst ein verifizierter Download berechtigt nicht zum Löschen neuer Remote-Inhalte."""
    monkeypatch.setattr(orch, "SSHManager", FakeSSHManager)
    FakeSSHManager.commands = []
    monkeypatch.setattr(FakeSSHManager, "remote_digest", hashlib.sha256(b"BBBB").hexdigest())
    monkeypatch.setattr(orch, "get_verified_identity", lambda local_name, local_path: {
        "remote": "clip:a.mp4", "sha256": hashlib.sha256(b"AAAA").hexdigest(), "size": 4,
    }, raising=False)
    (orch_env.outputs / "clip_a.mp4").write_bytes(b"AAAA")
    o = PodOrchestrator()
    o.status = "RUNNING"
    o.pod_ip, o.pod_ssh_port = "192.0.2.10", 22093

    assert o.delete_video("clip_a.mp4") is True
    assert not (orch_env.outputs / "clip_a.mp4").exists()
    assert FakeSSHManager.commands == [
        ("/workspace/Wan2GP/outputs/clip:a.mp4", hashlib.sha256(b"AAAA").hexdigest()),
    ]
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
    # Netzwerk-Preflight deterministisch ausgeschaltet: die FakeSSH-Routen
    # erwarten nur CUDA-/Bootstrap-/Probe-Kommandos. Die eigenen Net-Tests
    # mocken _preflight_network_speed bzw. load_config selbst.
    monkeypatch.setattr(orch, "load_config", lambda: {"preflight_min_net_speed_mb": 0})
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

    with pytest.raises(orch.PodPreflightError) as exc_info:
        o._bootstrap_wan2gp("192.0.2.10", 22093)
    assert exc_info.value.component == "cuda"

    assert len(ssh.calls) == 1
    assert ssh.calls[0][0].startswith("python3 - <<'VIDEOGEN_CUDA'")
    assert ssh.stream_completed is False
    assert o.startup_progress["phase"] == "ssh"
    assert "GPU-Zugriff" in o.startup_progress["label"]
    assert "Input/output error" in o.startup_progress["detail"]
    assert any("CUDA-Prüfung fehlgeschlagen" in line for line in o.log_history)


# ------------------------------------------------- Preflight: Netzwerk & Retry

def test_network_preflight_rejects_slow_host_before_installation(webui_probe, monkeypatch):
    """Ein Host mit lahmer Leitung wird VOR Clone/pip erkannt und als
    PodPreflightError('network') gemeldet - der Worker kann ihn dann sofort
    terminieren, statt stundenlang Geld für tote Modell-Downloads zu zahlen."""
    o, ssh = webui_probe
    monkeypatch.setattr(orch, "load_config", lambda: {"preflight_min_net_speed_mb": 5})
    monkeypatch.setattr(o, "_preflight_network_speed", lambda _ssh: 300_000.0)

    with pytest.raises(orch.PodPreflightError) as exc_info:
        o._bootstrap_wan2gp("192.0.2.10", 22093)
    assert exc_info.value.component == "network"

    assert not any("set -e" in command for command, _ in ssh.calls), \
        "Installation darf auf einem langsamen Host nicht erst beginnen"
    assert any("Netzwerk-Preflight fehlgeschlagen" in line for line in o.log_history)
    assert "MB/s" in o.startup_progress["detail"]


def test_network_preflight_passes_fast_host(webui_probe, monkeypatch):
    o, ssh = webui_probe
    ssh.probe_results = [(0, "READY\n", "")]
    monkeypatch.setattr(orch, "load_config", lambda: {"preflight_min_net_speed_mb": 5})
    monkeypatch.setattr(o, "_preflight_network_speed", lambda _ssh: 45_000_000.0)

    assert o._bootstrap_wan2gp("192.0.2.10", 22093) is True
    assert any("Netzwerk-Preflight ok" in line for line in o.log_history)


def test_network_preflight_skips_host_when_unmeasurable(webui_probe, monkeypatch):
    """Kann die Rate nicht gemessen werden, darf ein funktionierender Pod
    deshalb NICHT verworfen werden (fail-open)."""
    o, ssh = webui_probe
    ssh.probe_results = [(0, "READY\n", "")]
    monkeypatch.setattr(orch, "load_config", lambda: {"preflight_min_net_speed_mb": 5})
    monkeypatch.setattr(o, "_preflight_network_speed", lambda _ssh: None)

    assert o._bootstrap_wan2gp("192.0.2.10", 22093) is True
    assert not any("Netzwerk-Preflight fehlgeschlagen" in line for line in o.log_history)


def test_preflight_network_speed_parses_curl_rate():
    o = PodOrchestrator.__new__(PodOrchestrator)

    class _SSH:
        def __init__(self, out):
            self.out = out

        def exec_command(self, command, timeout):
            assert "VG_NET_SPEED_RC=" in command and "huggingface.co" in command
            assert "-r 0-" in command, "Range-Request nötig, sonst misst man Handshakes"
            assert "--fail" in command, "HTTP-Fehler dürfen keine gültige Geschwindigkeitsmessung liefern"
            return 0, self.out, ""

    # Erfolgreiche, schnelle und langsame Messungen sind echte Werte
    assert o._preflight_network_speed(_SSH("VG_NET_SPEED_RC=0 VG_NET_SPEED=42130000\n")) == pytest.approx(42_130_000.0)
    assert o._preflight_network_speed(_SSH("VG_NET_SPEED_RC=0 VG_NET_SPEED=300000\n")) == pytest.approx(300_000.0)

    # Messfehler jeglicher Art -> None (kein Pod-Verwurf): Curl-Fehler (rc!=0,
    # z. B. DNS/TLS/Timeout), leere Ausgabe, ungültige/nicht endliche Werte.
    assert o._preflight_network_speed(_SSH("VG_NET_SPEED_RC=28 VG_NET_SPEED=150.5\n")) is None
    assert o._preflight_network_speed(_SSH("VG_NET_SPEED_RC=6 VG_NET_SPEED=\n")) is None
    assert o._preflight_network_speed(_SSH("VG_NET_SPEED_RC=0 VG_NET_SPEED=\n")) is None
    assert o._preflight_network_speed(_SSH("VG_NET_SPEED_RC=0 VG_NET_SPEED=nan\n")) is None
    assert o._preflight_network_speed(_SSH("VG_NET_SPEED_RC=0 VG_NET_SPEED=abc\n")) is None
    assert o._preflight_network_speed(_SSH("garbage ohne token\n")) is None


def test_preflight_network_rejects_real_local_http_503(monkeypatch):
    """Den tatsächlichen Curl-Befehl gegen HTTP 503 auf Loopback ausführen."""
    if os.name == "nt":
        shell = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "Git/bin/bash.exe"
        shell = str(shell) if shell.is_file() else None
    else:
        shell = shutil.which("sh")
    if not shell:
        pytest.skip("Eine POSIX-Shell wird für die lokale Curl-Integration benötigt")

    requests = []

    class UnavailableHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            body = b"Service Unavailable"
            self.send_response(503)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), UnavailableHandler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    o = PodOrchestrator.__new__(PodOrchestrator)
    monkeypatch.setattr(o, "NET_PREFLIGHT_URL", f"http://127.0.0.1:{server.server_port}/model")
    captured = []

    class LocalShellSSH:
        def exec_command(self, command, timeout):
            result = subprocess.run([shell, "-c", command], capture_output=True, text=True,
                                    timeout=timeout, check=False)
            captured.append(result.stdout)
            return result.returncode, result.stdout, result.stderr

    try:
        assert o._preflight_network_speed(LocalShellSSH()) is None
        assert requests == ["/model"], "Curl muss den lokalen Testserver tatsächlich ansprechen"
        assert "VG_NET_SPEED_RC=22" in captured[0], "HTTP 503 muss ein Curl-Fehler bleiben"
    finally:
        server.shutdown()
        server.server_close()
        serving.join(timeout=2)


def _patch_preflight_worker(monkeypatch, fake, bootstrap_fn):
    """Worker-Test-Rig: eindeutige Pod-IDs, direkte Bootstrap-Kontrolle."""
    def deploy(self, **kwargs):
        self.deploy_calls.append(kwargs)
        pod_id = f"newpod-{len(self.deploy_calls)}"
        self.pods[pod_id] = RUNNING_POD
        return pod_id

    monkeypatch.setattr(FakeRunPodClient, "deploy_pod", deploy)
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(PodOrchestrator, "_bootstrap_wan2gp", bootstrap_fn)
    monkeypatch.setattr(orch, "get_or_create_ssh_keys", lambda: ("PRIV", "PUB"))
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)


def test_preflight_failure_terminates_pod_and_rebooks(orch_env, monkeypatch):
    """Kernverhalten: Preflight-Fehler terminiert den defekten Pod SOFORT
    (sonst brennt er weiter Kosten) und bucht automatisch einen Ersatz."""
    fake = FakeRunPodClient()
    attempts = {"n": 0}
    boots = []

    def bootstrap(self, ip, port):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise orch.PodPreflightError("network", "Download-Rate 0.3 MB/s zu HuggingFace")
        boots.append((ip, port))
        return True

    _patch_preflight_worker(monkeypatch, fake, bootstrap)

    o = PodOrchestrator()
    o._preflight_retries = 0
    o._deploy_or_resume_worker()

    assert len(fake.deploy_calls) == 2, "Nach Preflight-Fehler muss genau einmal neu gebucht werden"
    assert fake.terminate_calls == ["newpod-1"], "Der defekte Pod muss terminiert werden"
    assert o.status == "RUNNING"
    assert read_cfg(orch_env)["active_pod_id"] == "newpod-2"
    assert boots == [("192.0.2.10", 22093)], "Bootstrap läuft auf dem ERSATZ-Pod"
    o._stop_bg_thread = True


def test_preflight_retries_exhausted_sets_secure_hint(orch_env, monkeypatch):
    fake = FakeRunPodClient()

    def always_broken(self, ip, port):
        raise orch.PodPreflightError("cuda", "/dev/nvidia-uvm EIO, cuInit 999")

    _patch_preflight_worker(monkeypatch, fake, always_broken)

    o = PodOrchestrator()
    o._preflight_retries = 0
    o._deploy_or_resume_worker()

    assert len(fake.deploy_calls) == 3, "Initial + preflight_max_retries (2) Neuversuche"
    assert fake.terminate_calls == ["newpod-1", "newpod-2", "newpod-3"], \
        "Jeder Montagshost muss terminiert worden sein (keine Kostenlecks)"
    assert o.status == "ERROR"
    assert "Secure Cloud" in o.status_detail
    assert read_cfg(orch_env)["active_pod_status"] == "ERROR"
    o._stop_bg_thread = True


@pytest.mark.parametrize("component,message", [
    ("ssh", "SSH-Daemon antwortet nicht"),
    ("cuda", "/dev/nvidia-uvm EIO, cuInit 999"),
    ("network", "Download-Rate 0.4 MB/s zu HuggingFace"),
])
def test_preflight_failure_preserves_resumed_pod(orch_env, monkeypatch, component, message):
    """P1-2-Abnahme: Ein aus EXITED aufgeweckter Bestands-Pod wird bei SSH-,
    CUDA- und Netzwerk-Preflightfehlern NICHT terminiert. Pod-ID und Config
    bleiben erhalten, der Pod wird datenerhaltend pausiert."""
    fake = FakeRunPodClient()

    def pod_state(_fake):
        # Deterministisch statt Aufruf-Sequenz: Der Reconcile-Hintergrundthread
        # fragt get_pod ebenfalls ab und wuerde sonst den EXITED-Zustand konsumieren.
        return {"desiredStatus": "EXITED"} if not fake.resume_calls else RUNNING_POD

    fake.pods["pod123"] = pod_state

    def failing_bootstrap(self, ip, port):
        raise orch.PodPreflightError(component, message)

    _patch_preflight_worker(monkeypatch, fake, failing_bootstrap)

    orch_env.write_cfg(active_pod_id="pod123")
    o = PodOrchestrator()
    o.pod_id = "pod123"
    o._preflight_retries = 0
    o._deploy_or_resume_worker()

    assert fake.resume_calls == ["pod123"]
    assert fake.terminate_calls == [], "Bestands-Pod mit Volume darf nie terminiert werden"
    assert fake.deploy_calls == [], "Keine Ersatzbuchung für Bestands-Pods"
    assert fake.stop_calls == ["pod123"], "Stattdessen datenerhaltend pausieren"
    assert o.status == "STOPPED"
    assert o.pod_id == "pod123", "Pod-ID muss erhalten bleiben"
    assert "Bestands-Pod" in o.status_detail
    assert read_cfg(orch_env)["active_pod_id"] == "pod123"
    o._stop_bg_thread = True


def test_stale_booking_result_is_cleaned_up_as_orphan(orch_env, monkeypatch):
    """P1-3-Abnahme: Kehrt deploy_pod() zurück, nachdem die Operation bereits
    ersetzt wurde, darf die neue Pod-ID nicht einfach fallen gelassen werden:
    Der frisch erstellte Pod wird terminiert; der Zustand der laufenden
    neueren Operation bleibt unberührt."""
    fake = FakeRunPodClient()
    deploy_started = threading.Event()
    release_deploy = threading.Event()

    def slow_deploy(self, **kwargs):
        fake.deploy_calls.append(kwargs)
        deploy_started.set()
        assert release_deploy.wait(timeout=5), "Test muss das Deploy freigeben"
        return "latepod-1"

    monkeypatch.setattr(FakeRunPodClient, "deploy_pod", slow_deploy)
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(PodOrchestrator, "_bootstrap_wan2gp", lambda self, ip, port: True)
    monkeypatch.setattr(orch, "get_or_create_ssh_keys", lambda: ("PRIV", "PUB"))
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)

    o = PodOrchestrator()
    t = threading.Thread(target=o._deploy_or_resume_worker, daemon=True)
    t.start()
    assert deploy_started.wait(timeout=5), "Worker muss das Deploy erreicht haben"
    # Operation ablösen, während die Buchung noch läuft (z. B. Terminate-Klick).
    with o.lock:
        o._operation_id += 1
        newer_pod = "newerpod-9"
        o.pod_id = newer_pod
    release_deploy.set()
    t.join(timeout=5)
    assert not t.is_alive()

    assert fake.terminate_calls == ["latepod-1"], "Verspätet gebuchter Pod muss aufgeräumt werden"
    assert o.pod_id == newer_pod, "Der neuere Pod-Zustand darf nicht überschrieben werden"
    assert read_cfg(orch_env).get("active_pod_id") != "latepod-1"
    o._stop_bg_thread = True


def test_uncleanable_orphan_pod_is_persisted(orch_env, monkeypatch):
    """P1-3-Abnahme: Schlägt auch das Aufräumen fehl, muss die verwaiste
    Pod-ID dauerhaft erfasst und gemeldet werden (manuelle Bereinigung)."""
    fake = FakeRunPodClient()
    deploy_started = threading.Event()
    release_deploy = threading.Event()

    def slow_deploy(self, **kwargs):
        fake.deploy_calls.append(kwargs)
        deploy_started.set()
        assert release_deploy.wait(timeout=5)
        return "latepod-2"

    def failing_terminate(self, pod_id):
        fake.terminate_calls.append(pod_id)
        raise RuntimeError("RunPod API 503")

    monkeypatch.setattr(FakeRunPodClient, "deploy_pod", slow_deploy)
    monkeypatch.setattr(FakeRunPodClient, "terminate_pod", failing_terminate)
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(PodOrchestrator, "_bootstrap_wan2gp", lambda self, ip, port: True)
    monkeypatch.setattr(orch, "get_or_create_ssh_keys", lambda: ("PRIV", "PUB"))
    monkeypatch.setattr(orch.time, "sleep", fast_sleep)

    o = PodOrchestrator()
    t = threading.Thread(target=o._deploy_or_resume_worker, daemon=True)
    t.start()
    assert deploy_started.wait(timeout=5)
    with o.lock:
        o._operation_id += 1
    release_deploy.set()
    t.join(timeout=5)
    assert not t.is_alive()

    assert fake.terminate_calls == ["latepod-2"], "Aufräumversuch musste stattfinden"
    assert read_cfg(orch_env).get("orphan_pod_ids") == ["latepod-2"], \
        "Nicht aufräumbare Pod-ID muss für manuelle Bereinigung persistiert werden"
    assert any("latepod-2" in line and "ERROR" in line for line in o.log_history)
    o._stop_bg_thread = True


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
    assert r"parse_public_url\|\--public-url\|shared/authentication/__init__.py" in source or \
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


# ------------------------------------------------- GPU-Ausweichbuchung

class _FallbackGpuClient:
    """deploy_pod failt fuer die erste Karte (ausgebucht), bucht die zweite."""

    def __init__(self, primary, fallback):
        self._primary = primary
        self._fallback = fallback
        self.attempts = []

    def get_gpu_types(self):
        return [
            {"id": self._primary, "displayName": "P", "memoryInGb": 48, "communityPrice": 0.30},
            {"id": self._fallback, "displayName": "F", "memoryInGb": 48, "communityPrice": 0.35},
        ]

    def get_pod(self, pod_id):
        return None  # Worker bleibt in der Poll-Schleife haengen (Test beendet sich vorher)

    def deploy_pod(self, name, gpu_type_id, **kwargs):
        self.attempts.append(gpu_type_id)
        if gpu_type_id == self._primary:
            raise Exception("Error generating run: No longer any instances available")
        return "newpod123"


def test_start_pod_falls_back_to_next_gpu_when_out_of_stock(api_env, monkeypatch):
    srv = api_env["server"]
    orch = srv.orchestrator
    fake = _FallbackGpuClient("NVIDIA L40", "NVIDIA A40")
    monkeypatch.setattr(orch, "get_client", lambda: fake)
    # Gewaehlte Karte explizit setzen: die api_env-Config enthaelt sonst keine
    # GPU und der Worker wuerde den Default (A40) direkt buchen.
    api_env["cfg_file"].write_text(json.dumps(
        {"runpod_api_key": "", "gpu_type_id": "NVIDIA L40", "cloud_type": "ALL"}), encoding="utf-8")
    srv.orchestrator.status = "OFFLINE"
    srv.orchestrator.gpu_out_of_stock = {}

    res = srv.orchestrator.start_pod()
    assert res["success"] is True
    assert wait_for(lambda: "NVIDIA A40" == fake.attempts[-1] if fake.attempts else False, timeout=10)

    cfg = json.loads(api_env["cfg_file"].read_text(encoding="utf-8"))
    assert cfg["gpu_type_id"] == "NVIDIA A40"
    assert cfg["active_pod_id"] == "newpod123"
    # Die vergriffene Karte ist gelernt
    assert "NVIDIA L40" in orch.gpu_out_of_stock

    # Aufräumen für Folgetests
    orch.status = "OFFLINE"
    orch.pod_id = None
    orch.gpu_out_of_stock = {}
    orch._operation_id += 1


def test_start_pod_no_fallback_on_unrelated_error(api_env, monkeypatch):
    srv = api_env["server"]
    orch = srv.orchestrator
    fake = _FallbackGpuClient("NVIDIA L40", "NVIDIA A40")

    def bad_deploy(name, gpu_type_id, **kw):
        fake.attempts.append(gpu_type_id)
        raise Exception("SSH connection refused")
    monkeypatch.setattr(fake, "deploy_pod", bad_deploy)
    monkeypatch.setattr(orch, "get_client", lambda: fake)
    api_env["cfg_file"].write_text(json.dumps(
        {"runpod_api_key": "", "gpu_type_id": "NVIDIA L40", "cloud_type": "ALL"}), encoding="utf-8")
    srv.orchestrator.status = "OFFLINE"
    srv.orchestrator.gpu_out_of_stock = {}

    srv.orchestrator.start_pod()
    assert wait_for(lambda: srv.orchestrator.status == "ERROR", timeout=10)
    # Nur die gewaehlte Karte versucht, kein Ausweichversuch
    assert fake.attempts == ["NVIDIA L40"]

    srv.orchestrator.status = "OFFLINE"
    srv.orchestrator._operation_id += 1


# ------------------------------------------------- Kostenverbuchung

def test_stop_pod_books_session_cost_permanently(api_env, monkeypatch):
    srv = api_env["server"]
    orch = srv.orchestrator

    class _OkClient:
        def stop_pod(self, pod_id):
            return True
    monkeypatch.setattr(orch, "get_client", lambda: _OkClient())

    cm.update_config({"spent_total": 0.0})
    orch.accumulated_cost = 0.5
    orch.pod_id = "podX"
    orch.status = "RUNNING"
    orch.session_start_time = time.time() - 60

    res = srv.orchestrator.stop_pod()
    assert res["success"] is True

    cfg = json.loads(api_env["cfg_file"].read_text(encoding="utf-8"))
    assert cfg["spent_total"] == 0.5
    assert orch.accumulated_cost == 0.0

    # Aufräumen
    orch.status = "OFFLINE"
    orch.pod_id = None
    cm.update_config({"spent_total": 0.0})


def test_lifecycle_methods_with_costs_do_not_deadlock(api_env, monkeypatch):
    """P1-1-Regression: stop/terminate/start mit positiven Sessionkosten müssen
    zurückkehren (kein Lock-Reentry-Deadlock in _book_session_cost -> log),
    genau einmal verbuchen und den Statuszugriff danach freigeben. Der Aufruf
    läuft in einem begrenzt gewarteten Thread, damit eine Regression die Suite
    blockiert meldet statt sie aufzuhängen."""
    srv = api_env["server"]
    orch = srv.orchestrator

    class _OkClient:
        def stop_pod(self, pod_id):
            return True

        def terminate_pod(self, pod_id):
            return True
    monkeypatch.setattr(orch, "get_client", lambda: _OkClient())
    monkeypatch.setattr(orch, "_deploy_or_resume_worker", lambda *a, **k: None)

    def run_bounded(fn, timeout=5.0):
        box = {}

        def runner():
            try:
                box["res"] = fn()
            except Exception as e:  # noqa: BLE001 - Regressionstest
                box["err"] = e

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        t.join(timeout)
        assert not t.is_alive(), "Lifecycle-Methode hängt (Deadlock-Regression)"
        if "err" in box:
            raise box["err"]
        return box.get("res")

    for method in ("stop_pod", "terminate_pod"):
        cm.update_config({"spent_total": 0.0})
        orch.accumulated_cost = 0.25
        orch.pod_id = "podX"
        orch.status = "RUNNING"
        orch.session_start_time = time.time() - 60

        res = run_bounded(getattr(orch, method))
        assert res["success"] is True

        cfg = json.loads(api_env["cfg_file"].read_text(encoding="utf-8"))
        assert cfg["spent_total"] == 0.25, "Kosten müssen genau einmal verbucht werden"
        assert orch.accumulated_cost == 0.0
        state = orch.get_state()
        assert state["status"] in ("STOPPED", "TERMINATED"), "Statusabfrage muss nach dem Aufruf funktionieren"

    cm.update_config({"spent_total": 0.0})
    orch.accumulated_cost = 0.25
    orch.status = "OFFLINE"
    res = run_bounded(orch.start_pod)
    assert res["success"] is True
    cfg = json.loads(api_env["cfg_file"].read_text(encoding="utf-8"))
    assert cfg["spent_total"] == 0.25

    # Aufräumen
    orch.status = "OFFLINE"
    orch.pod_id = None
    cm.update_config({"spent_total": 0.0})


def test_balance_subtracts_permanent_spend(client, api_env):
    srv = api_env["server"]
    cm.update_config({"account_balance": 10.0, "spent_total": 2.0})
    srv.orchestrator.accumulated_cost = 1.0

    res = client.get("/api/status")

    assert res.status_code == 200
    assert res.json()["account"]["balance"] == 7.0

    srv.orchestrator.accumulated_cost = 0.0
    cm.update_config({"spent_total": 0.0})


def test_manual_balance_entry_stays_consistent(client, api_env):
    """P2-11: Manuelle Eingabe des WAHREN Kontostands. Die Basis absorbiert
    historische (spent_total) und laufende (accumulated) Kosten, damit die
    Eingabe exakt erscheint und nichts doppelt abgezogen wird - die Historie
    bleibt dabei erhalten (kein Reset mehr)."""
    srv = api_env["server"]
    cm.update_config({"account_balance": 10.0, "spent_total": 3.0})
    srv.orchestrator.accumulated_cost = 2.0

    res = client.get("/api/account/refresh", params={"set_balance": 15.0})

    assert res.status_code == 200
    assert res.json()["account"]["balance"] == 15.0
    cfg = json.loads(api_env["cfg_file"].read_text(encoding="utf-8"))
    assert cfg["spent_total"] == 3.0, "Historie bleibt erhalten"
    assert cfg["account_balance"] == 20.0, "Basis = Eingabe + spent_total + accumulated"

    # Nächster Statuspoll: gleicher Stand, kein Doppelabzug der laufenden Kosten
    body = client.get("/api/status").json()
    assert body["account"]["balance"] == 15.0

    # Stop bucht accumulated in spent_total - Anzeige bleibt bei der Eingabe,
    # zieht aber künftige Kosten korrekt ab (kein Doppelpost).
    srv.orchestrator.status = "OFFLINE"  # Kostenverbuchung ohne echten Stop-Call
    srv.orchestrator._book_session_cost()
    body = client.get("/api/status").json()
    assert body["account"]["balance"] == 15.0, "Nach Verbuchung der laufenden Kosten darf sich die Anzeige nicht verschieben"
    cfg = json.loads(api_env["cfg_file"].read_text(encoding="utf-8"))
    assert cfg["spent_total"] == 5.0

    srv.orchestrator.accumulated_cost = 0.0
    cm.update_config({"account_balance": 10.0, "spent_total": 0.0})
