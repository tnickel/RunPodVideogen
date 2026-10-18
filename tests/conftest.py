# Gemeinsame Fixtures für die Test-Suite.
#
# Wichtig: Der FastAPI-Server (server.py) erzeugt beim Import ein
# PodOrchestrator-Singleton mit Background-Threads. Damit Tests keine echte
# RunPod-API ansprechen und nie die echte config.json anfassen, wird
# _CONFIG_PATH/_OUTPUTS_DIR VOR dem Import von `server` auf ein
# Session-Tempverzeichnis umgebogen.

import sys
import json
import pathlib
import tempfile
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# Schutz gilt bereits vor Test-Imports und auch nach monkeypatch.undo(). Die
# ursprünglichen Produktivpfade werden in diesem pytest-Prozess nie restauriert:
# selbst ein entkommener Daemon darf nur in temporäre Testdaten schreiben.
_isolated_test_state = tempfile.TemporaryDirectory(prefix="runpod-videogen-pytest-")
import backend.config_manager as _isolated_config_manager

_isolated_config_manager._CONFIG_PATH = str(pathlib.Path(_isolated_test_state.name) / "config.json")
_isolated_config_manager._OUTPUTS_DIR = str(pathlib.Path(_isolated_test_state.name) / "outputs")


@pytest.fixture(scope="session")
def api_env(tmp_path_factory):
    """Isolierte Umgebung (Config + outputs) für die API-Endpoint-Tests."""
    tmp = tmp_path_factory.mktemp("api_env")
    cfg_file = tmp / "config.json"
    cfg_file.write_text(json.dumps({"runpod_api_key": ""}), encoding="utf-8")
    outputs = tmp / "outputs"
    outputs.mkdir()

    import backend.config_manager as cm
    # Bewusst ohne monkeypatch (session-weit): muss den Import von server überdauern.
    cm._CONFIG_PATH = str(cfg_file)
    cm._OUTPUTS_DIR = str(outputs)

    import server  # Singleton entsteht hiermit gegen die isolierte Config

    # Background-Loop im Testbetrag deterministisch ruhig halten.
    server.orchestrator._stop_bg_thread = True
    server.orchestrator.bg_thread.join(timeout=3)
    assert not server.orchestrator.bg_thread.is_alive()
    return {"cfg_file": cfg_file, "outputs": outputs, "server": server}


@pytest.fixture()
def client(api_env):
    """Frischer TestClient + saubere Ausgangslage pro Test."""
    from fastapi.testclient import TestClient

    srv = api_env["server"]
    api_env["cfg_file"].write_text(json.dumps({"runpod_api_key": ""}), encoding="utf-8")
    srv.orchestrator.accumulated_cost = 0.0
    srv.orchestrator.status = "OFFLINE"
    srv.orchestrator.status_detail = "Bereit"
    srv.orchestrator.pod_ip = None
    srv.orchestrator.pod_ssh_port = None
    srv.orchestrator.session_start_time = None
    srv.orchestrator.gpu_out_of_stock = {}

    with TestClient(srv.app) as c:
        yield c

    srv.orchestrator.accumulated_cost = 0.0
    srv.orchestrator.status = "OFFLINE"
    srv.orchestrator.pod_ip = None
    srv.orchestrator.pod_ssh_port = None
    srv.orchestrator.session_start_time = None
    srv.orchestrator.gpu_out_of_stock = {}


def wait_for(cond, timeout=12.0, interval=0.05):
    """Wartet, bis cond() wahr wird (für Thread-basierte Logik)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(interval)
    return False


@pytest.fixture()
def orch_env(tmp_path, monkeypatch):
    """Eigene Temp-Config + Temp-outputs für Orchestrator-Instanz-Tests."""
    import backend.config_manager as cm
    import backend.orchestrator as orch
    import threading
    import types

    threads = []

    def tracked_thread(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        owner = getattr(kwargs.get("target"), "__self__", None)
        threads.append((thread, owner))
        return thread

    # Nur das Modul des Orchestrators ersetzen, nicht threading.Thread global.
    monkeypatch.setattr(orch, "threading", types.SimpleNamespace(
        Lock=threading.Lock, RLock=threading.RLock, Thread=tracked_thread))

    cfg_file = tmp_path / "config.json"
    outputs = tmp_path / "outputs"
    outputs.mkdir()

    def write_cfg(**updates):
        base = {
            "runpod_api_key": "",
            "account_email": "",
            "account_balance": 18.74,
            "active_pod_id": None,
            "active_pod_status": "OFFLINE",
            "gpu_type_id": "NVIDIA A40",
            "cloud_type": "ALL",
            "volume_disk_gb": 75,
            "container_disk_gb": 30,
            "standby_mode": "stop",
            "auto_shutdown_minutes": 30,
            "auto_sync_videos": True,
            "sync_interval_seconds": 15,
        }
        base.update(updates)
        cfg_file.write_text(json.dumps(base), encoding="utf-8")

    write_cfg()
    monkeypatch.setattr(cm, "_CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(cm, "_OUTPUTS_DIR", str(outputs))
    try:
        yield types.SimpleNamespace(write_cfg=write_cfg, cfg_file=cfg_file, outputs=outputs)
    finally:
        # Vor Rücksetzen der Config-Pfade müssen auch Reconcile-Threads fertig
        # sein; ein Stop-Flag allein verhindert ihren letzten Loop-Takt nicht.
        for _, owner in threads:
            if owner is not None:
                owner._stop_bg_thread = True
        for thread, _ in threads:
            thread.join(timeout=3)
            assert not thread.is_alive(), "Orchestrator-Thread läuft über die Testgrenze hinaus"
