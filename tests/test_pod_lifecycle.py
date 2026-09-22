"""Preis- und Abbruchregressionen für Podwechsel, ohne Netzwerk oder Wartezeiten."""

import time
from types import SimpleNamespace

import pytest

import backend.orchestrator as orch
from backend.orchestrator import PodOrchestrator
from tests.test_orchestrator import FakeRunPodClient, read_cfg


def running_pod(pod_id, rate):
    return {
        "id": pod_id,
        "desiredStatus": "RUNNING",
        "costPerHr": rate,
        "runtime": {
            "ports": [{"privatePort": 22, "ip": "192.0.2.10", "publicPort": 22093}],
        },
    }


@pytest.fixture()
def lifecycle(orch_env, monkeypatch):
    """Worker gezielt fortsetzen, um veraltete IO-Antworten deterministisch zu prüfen."""
    fake = FakeRunPodClient()
    monkeypatch.setattr(PodOrchestrator, "get_client", lambda self: fake)
    monkeypatch.setattr(PodOrchestrator, "_background_loop", lambda self: None)
    monkeypatch.setattr(orch, "get_or_create_ssh_keys", lambda: ("TEST-PRIVATE", "TEST-PUBLIC"))
    monkeypatch.setattr(orch, "time", SimpleNamespace(
        time=time.time, strftime=time.strftime, sleep=lambda _: None,
    ))
    monkeypatch.setattr(PodOrchestrator, "_bootstrap_wan2gp", lambda *args, **kwargs: True)

    def deploy(**kwargs):
        fake.deploy_calls.append(kwargs)
        return f"newpod-{len(fake.deploy_calls)}"

    monkeypatch.setattr(fake, "deploy_pod", deploy)
    orchestrator = PodOrchestrator()
    orchestrator.bg_thread.join(timeout=1)
    assert not orchestrator.bg_thread.is_alive()
    pending = []

    class DeferredThread:
        def __init__(self, target, args=(), kwargs=None, **_options):
            self.target, self.args, self.kwargs = target, args, kwargs or {}

        def start(self):
            pending.append(lambda: self.target(*self.args, **self.kwargs))

    monkeypatch.setattr(orch.threading, "Thread", DeferredThread)
    return SimpleNamespace(o=orchestrator, fake=fake, pending=pending)


def test_start_does_not_display_previous_pods_price(lifecycle):
    o = lifecycle.o
    o.hourly_rate = 0.497
    o._hourly_rate_pod_id = "deleted-a40"

    assert o.start_pod()["success"] is True

    state = o.get_state()
    assert state["status"] == "STARTING"
    assert state["hourly_rate"] is None
    assert state["hourly_rate_source"] == "unknown"
    assert state["startup_progress"]["phase"] == "booking"
    assert state["startup_progress"]["active"] is True
    assert len(lifecycle.pending) == 1


def test_first_poll_updates_price_before_ssh_runtime_exists(lifecycle):
    o, fake = lifecycle.o, lifecycle.fake
    observations = []

    def provision(_client):
        if not observations:
            observations.append("first-poll")
            return {"id": "newpod-1", "desiredStatus": "RUNNING", "costPerHr": 0.79, "runtime": None}
        # Diese Abfrage beginnt erst, nachdem die erste Antwort verarbeitet wurde.
        observations.append(o.get_state())
        return running_pod("newpod-1", 0.79)

    fake.pods["newpod-1"] = provision
    o.start_pod()
    lifecycle.pending.pop(0)()

    assert observations[1]["status"] == "STARTING"
    assert observations[1]["pod_ip"] is None
    assert observations[1]["hourly_rate"] == pytest.approx(0.79)
    assert observations[1]["hourly_rate_source"] == "runpod"
    assert o.status == "RUNNING"
    assert o.get_state()["startup_progress"]["phase"] == "ready"
    assert o.get_state()["startup_progress"]["active"] is False


@pytest.mark.parametrize("action, expected_status", [
    ("terminate_pod", "TERMINATED"),
    ("stop_pod", "STOPPED"),
])
@pytest.mark.parametrize("bootstrap_raises", [False, True])
def test_cancel_during_bootstrap_cannot_restore_error_or_running(
    lifecycle, orch_env, monkeypatch, action, expected_status, bootstrap_raises,
):
    o, fake = lifecycle.o, lifecycle.fake
    fake.pods["newpod-1"] = running_pod("newpod-1", 0.79)

    def bootstrap(*_args, **_kwargs):
        assert getattr(o, action)()["success"] is True
        if bootstrap_raises:
            raise ConnectionError("SSH-Verbindung nach Abbruch geschlossen")
        return False

    monkeypatch.setattr(o, "_bootstrap_wan2gp", bootstrap)
    o.start_pod()
    lifecycle.pending.pop(0)()

    assert o.status == expected_status
    assert read_cfg(orch_env)["active_pod_status"] == expected_status
    assert "fehlgeschlagen" not in o.status_detail.lower()
    assert o.get_state()["startup_progress"]["phase"] == "cancelled"
    assert o.get_state()["startup_progress"]["active"] is False
    if action == "terminate_pod":
        assert o.pod_id is None
        assert read_cfg(orch_env)["active_pod_id"] is None
        assert o.get_state()["hourly_rate"] != 0.79
    else:
        assert o.pod_id == "newpod-1"


def test_old_poll_cannot_overwrite_replacement_pods_state_and_price(lifecycle, orch_env):
    o, fake = lifecycle.o, lifecycle.fake
    fake.pods["newpod-2"] = running_pod("newpod-2", 1.23)

    def late_old_response(_client):
        # Während die erste RunPod-Abfrage unterwegs ist, löscht der Benutzer
        # den Pod und startet einen neuen. Dessen Worker beendet zuerst.
        assert o.terminate_pod()["success"] is True
        assert o.start_pod()["success"] is True
        lifecycle.pending.pop(0)()
        return running_pod("newpod-1", 0.79)

    fake.pods["newpod-1"] = late_old_response
    o.start_pod()
    lifecycle.pending.pop(0)()

    state = o.get_state()
    assert fake.terminate_calls == ["newpod-1"]
    assert len(fake.deploy_calls) == 2
    assert state["status"] == "RUNNING"
    assert state["pod_id"] == "newpod-2"
    assert state["proxy_url"] == "https://newpod-2-7860.proxy.runpod.net"
    assert state["hourly_rate"] == pytest.approx(1.23)
    assert state["hourly_rate_source"] == "runpod"
    cfg = read_cfg(orch_env)
    assert cfg["active_pod_id"] == "newpod-2"
    assert cfg["active_pod_status"] == "RUNNING"


@pytest.mark.parametrize("action", ["stop_pod", "terminate_pod"])
def test_failed_cancel_during_setup_preserves_pod_and_allows_retry(
    lifecycle, orch_env, monkeypatch, action,
):
    o, fake = lifecycle.o, lifecycle.fake
    fake.pods["newpod-1"] = running_pod("newpod-1", 0.79)

    def api_failure(_pod_id):
        raise RuntimeError("RunPod-Steuerung vorübergehend nicht erreichbar")

    monkeypatch.setattr(fake, action, api_failure)

    def bootstrap(*_args, **_kwargs):
        result = getattr(o, action)()
        assert result["success"] is False
        assert "vorübergehend nicht erreichbar" in result["message"]
        return False

    monkeypatch.setattr(o, "_bootstrap_wan2gp", bootstrap)
    o.start_pod()
    lifecycle.pending.pop(0)()

    assert o.status == "ERROR", "Ein abgebrochener Worker darf STARTING/BOOTSTRAPPING nicht blockieren"
    assert o.pod_id == "newpod-1", "Fehlgeschlagenes Stop/Delete darf den echten Pod nicht vergessen"
    assert "fehlgeschlagen" in o.status_detail.lower()
    cfg = read_cfg(orch_env)
    assert cfg["active_pod_id"] == "newpod-1"
    assert cfg["active_pod_status"] == "ERROR"
    assert o.start_pod()["success"] is True
    assert len(lifecycle.pending) == 1


@pytest.mark.parametrize("raw_rate", [0, "0", 0.79, "0.79"])
def test_provider_price_accepts_zero_and_numeric_strings(lifecycle, raw_rate):
    o = lifecycle.o
    o.pod_id = "newpod-1"
    o.status = "STARTING"

    assert o._refresh_pod_rate("newpod-1", {"costPerHr": raw_rate}, o._operation_id) is True

    state = o.get_state()
    assert state["hourly_rate"] == pytest.approx(float(raw_rate))
    assert state["hourly_rate_source"] == "runpod"


@pytest.mark.parametrize("pod_info", [
    {}, {"costPerHr": None}, {"costPerHr": -1}, {"costPerHr": "unbekannt"},
    {"costPerHr": "nan"}, {"costPerHr": "inf"}, {"costPerHr": "-inf"},
])
def test_missing_or_invalid_price_remains_unknown(lifecycle, pod_info):
    o = lifecycle.o
    o.pod_id = "newpod-1"
    o.status = "STARTING"

    assert o._refresh_pod_rate("newpod-1", pod_info, o._operation_id) is False

    state = o.get_state()
    assert state["hourly_rate"] is None
    assert state["hourly_rate_source"] == "unknown"


def test_price_from_previous_operation_is_ignored_even_for_same_pod(lifecycle):
    o = lifecycle.o
    o.pod_id = "resumed-pod"
    o.status = "STARTING"
    old_operation = o._operation_id
    o._operation_id += 1
    assert o._refresh_pod_rate("resumed-pod", {"costPerHr": 0.79}, o._operation_id) is True

    assert o._refresh_pod_rate("resumed-pod", {"costPerHr": 0.497}, old_operation) is False
    assert o.get_state()["hourly_rate"] == pytest.approx(0.79)


def test_reconcile_ready_result_cannot_overwrite_delete_detail(lifecycle, orch_env, monkeypatch):
    """Delete zwischen Abbruchprüfung und Schreiblock muss auch das Detail schützen."""
    o = lifecycle.o
    o.pod_id = "newpod-1"
    o.status = "RUNNING"
    o.proxy_url = "https://newpod-1-7860.proxy.runpod.net"
    state = {"probe_returned": False}
    original_current = o._operation_current

    def current_then_delete(operation_id, pod_id=None):
        was_current = original_current(operation_id, pod_id)
        if was_current and state["probe_returned"]:
            state["probe_returned"] = False
            assert o.terminate_pod()["success"] is True
        return was_current

    def successful_probe(*_args, **_kwargs):
        state["probe_returned"] = True
        return 0, "READY\n", ""

    ssh = SimpleNamespace(
        get_client=lambda **_kwargs: SimpleNamespace(close=lambda: None),
        exec_command=successful_probe,
    )
    monkeypatch.setattr(orch, "SSHManager", lambda *_args, **_kwargs: ssh)
    monkeypatch.setattr(o, "_operation_current", current_then_delete)

    o._ensure_webui_after_reconcile("192.0.2.10", 22093)

    assert o.status == "TERMINATED"
    assert "gelöscht" in o.status_detail.lower()
    assert "aktiv" not in o.status_detail.lower()
    assert read_cfg(orch_env)["active_pod_status"] == "TERMINATED"


def test_existing_pod_lookup_failure_never_buys_replacement(lifecycle, orch_env):
    o, fake = lifecycle.o, lifecycle.fake
    orch_env.write_cfg(active_pod_id="existing-pod", active_pod_status="STOPPED")
    o.pod_id, o.status = "existing-pod", "STOPPED"

    def unavailable(_client):
        raise ConnectionError("https://example.invalid/?api_key=TEST-PRIVATE-VALUE")

    fake.pods["existing-pod"] = unavailable
    o.start_pod()
    lifecycle.pending.pop(0)()

    assert fake.deploy_calls == []
    assert fake.resume_calls == []
    assert o.pod_id == "existing-pod"
    assert o.status == "ERROR"
    assert "kein neuer Pod gebucht" in o.status_detail
    assert "TEST-PRIVATE-VALUE" not in o.status_detail
    assert not any("TEST-PRIVATE-VALUE" in line for line in o.log_history)
    assert o.startup_progress["phase"] == "error"
    assert o.startup_progress["active"] is False
    assert read_cfg(orch_env)["active_pod_id"] == "existing-pod"
    assert read_cfg(orch_env)["active_pod_status"] == "ERROR"


@pytest.mark.parametrize("pod_info", [
    {"id": "existing-pod", "desiredStatus": "STARTING"},
    {"id": "existing-pod", "desiredStatus": "TERMINATED"},
    {"id": "existing-pod", "desiredStatus": None},
    {},
])
def test_existing_pod_with_unsupported_status_never_buys_replacement(lifecycle, orch_env, pod_info):
    o, fake = lifecycle.o, lifecycle.fake
    orch_env.write_cfg(active_pod_id="existing-pod", active_pod_status="STOPPED")
    o.pod_id, o.status = "existing-pod", "STOPPED"
    fake.pods["existing-pod"] = pod_info

    o.start_pod()
    lifecycle.pending.pop(0)()

    assert fake.deploy_calls == []
    assert fake.resume_calls == []
    assert o.pod_id == "existing-pod"
    assert o.status == "ERROR"
    assert "RunPod-Status prüfen" in o.status_detail
    assert o.startup_progress["active"] is False
    assert read_cfg(orch_env)["active_pod_id"] == "existing-pod"


def test_confirmed_missing_existing_pod_can_still_be_replaced(lifecycle, orch_env):
    o, fake = lifecycle.o, lifecycle.fake
    orch_env.write_cfg(active_pod_id="missing-pod", active_pod_status="STOPPED")
    o.pod_id, o.status = "missing-pod", "STOPPED"
    fake.pods["missing-pod"] = None
    fake.pods["newpod-1"] = running_pod("newpod-1", 0.79)

    o.start_pod()
    lifecycle.pending.pop(0)()

    assert len(fake.deploy_calls) == 1
    assert o.pod_id == "newpod-1"
    assert o.status == "RUNNING"
    assert read_cfg(orch_env)["active_pod_id"] == "newpod-1"


def test_late_lookup_failure_preserves_successful_delete(lifecycle, orch_env):
    o, fake = lifecycle.o, lifecycle.fake
    orch_env.write_cfg(active_pod_id="existing-pod", active_pod_status="STOPPED")
    o.pod_id, o.status = "existing-pod", "STOPPED"

    def failed_after_delete(_client):
        assert o.terminate_pod()["success"] is True
        raise ConnectionError("Statusabfrage inzwischen abgebrochen")

    fake.pods["existing-pod"] = failed_after_delete
    o.start_pod()
    lifecycle.pending.pop(0)()

    assert fake.deploy_calls == []
    assert o.status == "TERMINATED"
    assert o.pod_id is None
    assert o.startup_progress["phase"] == "cancelled"
    assert read_cfg(orch_env)["active_pod_id"] is None
    assert read_cfg(orch_env)["active_pod_status"] == "TERMINATED"
