# Regressionstests für die HTTP-API (server.py) via FastAPI TestClient.
#
# Festschreibung der kritischen Bugfixes:
#   P0-2  /api/config darf den RunPod-API-Key nie im Klartext ausliefern
#   P1-5  Balance-Formel (base - session costs) + persistierbare Balance
#   P2-11b Video-URLs müssen URL-encodet sein
#   P3-20 Delete-Endpoint meldet 404, wenn nichts löschbar war
# Zusätzlich: Path-Traversal-Schutz und WebSocket-Log-Replay.

import json
import urllib.parse
from types import SimpleNamespace

import pytest

import backend.config_manager as cm


# ------------------------------------------------------------------ P0-2

def test_config_endpoint_never_leaks_cleartext_api_key(client):
    # Bewusst KEIN rpa_-Präfix: Der Pre-Commit-Hook blockt rpa_+15 Zeichen (auch Fake-Keys).
    fake_key = "testkey_ABCDEFGHIJKLMNOP"
    cm.update_config({"runpod_api_key": fake_key})

    res = client.get("/api/config")

    assert res.status_code == 200
    body = res.json()
    assert "runpod_api_key" not in body, "Klartext-Key-Feld darf nicht im JSON sein"
    assert fake_key not in res.text, "Key darf nirgends im Body auftauchen"
    assert body["runpod_api_key_masked"] == "testke...MNOP"  # erste 6 + letzte 4 Zeichen


def test_config_endpoint_masks_short_keys(client):
    cm.update_config({"runpod_api_key": "abc"})

    body = client.get("/api/config").json()

    assert "runpod_api_key" not in body
    assert body["runpod_api_key_masked"] == "***"


# ------------------------------------------------------------------ P1-5

def test_post_config_never_leaks_cleartext_api_key(client, api_env):
    """P1-5: GET maskiert den Key - POST darf ihn genauso wenig ausliefern
    (auch nicht bei leerem JSON oder einer gewöhnlichen Einstellungsänderung)."""
    fake_key = "testkey_ABCDEFGHIJKLMNOP"
    cm.update_config({"runpod_api_key": fake_key})

    for payload in ({}, {"volume_disk_gb": 99}):
        res = client.post("/api/config", json=payload)
        assert res.status_code == 200
        assert "runpod_api_key" not in res.json()["config"], "Klartext-Key-Feld darf nicht im JSON sein"
        assert fake_key not in res.text, "Key darf nirgends in der POST-Antwort auftauchen"
        assert res.json()["config"]["runpod_api_key_masked"] == "testke...MNOP"


def test_post_config_never_touches_balance(client, api_env):
    """P2-11: Das Speichern anderer Einstellungen darf das Guthaben nicht
    verändern - der Wert im Dialog ist ein berechneter Restbetrag, kein neuer
    Basisstand. Manuelle Korrektur nur über /api/account/refresh?set_balance."""
    cm.update_config({"account_balance": 20.0, "spent_total": 5.0})

    res = client.post("/api/config", json={"account_balance": 15.0, "volume_disk_gb": 80})

    assert res.status_code == 200
    on_disk = json.loads(api_env["cfg_file"].read_text(encoding="utf-8"))
    assert on_disk["account_balance"] == 20.0, "Basis darf durch Routine-Saves nicht verändert werden"
    assert on_disk["spent_total"] == 5.0
    assert on_disk["volume_disk_gb"] == 80


def test_post_config_without_key_preserves_existing_key(client, api_env):
    """Settings-Speichern ohne neue Key-Eingabe darf den vorhandenen Key nicht löschen."""
    cm.update_config({"runpod_api_key": "testkey_SECRET123456"})

    res = client.post("/api/config", json={"volume_disk_gb": 30})

    assert res.status_code == 200
    on_disk = json.loads(api_env["cfg_file"].read_text(encoding="utf-8"))
    assert on_disk["runpod_api_key"] == "testkey_SECRET123456"


def test_status_balance_subtracts_session_costs(client):
    """Regression P1-5c: angezeigtes Guthaben = Basis - Session-Kosten."""
    cm.update_config({"account_balance": 10.0})
    import server as srv_mod  # das Singleton nutzt dieselbe isolierte Config
    srv_mod.orchestrator.accumulated_cost = 2.5

    body = client.get("/api/status").json()

    assert body["account"]["balance"] == 7.5


def test_status_email_without_network(client):
    """Ohne API-Key darf /api/status kein Netzwerk machen und nicht crashen."""
    body = client.get("/api/status").json()
    assert "account" in body
    assert isinstance(body["account"]["email"], str)


def test_balance_rebase_observes_session_booked_after_first_config_read(client, api_env, monkeypatch):
    srv = api_env["server"]
    cm.update_config({"account_balance": 20.0, "spent_total": 3.0})
    srv.orchestrator.accumulated_cost = 2.0

    def client_after_stop():
        # Deterministic interleaving: stop books the session after the endpoint
        # first reads its email/config, before it obtains an accounting snapshot.
        with srv.orchestrator.lock:
            srv.orchestrator._book_session_cost()
        return SimpleNamespace(get_myself=lambda: {})

    monkeypatch.setattr(srv.orchestrator, "get_client", client_after_stop)
    response = client.get("/api/account/refresh", params={"set_balance": 15})

    assert response.status_code == 200
    assert response.json()["account"]["balance"] == 15
    assert cm.load_config()["account_balance"] == 20
    assert cm.load_config()["spent_total"] == 5
    assert client.get("/api/status").json()["account"]["balance"] == 15


def test_balance_rebase_reads_current_costs_after_account_request(client, api_env, monkeypatch):
    srv = api_env["server"]
    cm.update_config({"account_balance": 20.0, "spent_total": 3.0})
    srv.orchestrator.accumulated_cost = 2.0

    def slow_account_request():
        with srv.orchestrator.lock:
            srv.orchestrator.accumulated_cost = 4.0
        return {}

    monkeypatch.setattr(srv.orchestrator, "get_client", lambda: SimpleNamespace(get_myself=slow_account_request))
    response = client.get("/api/account/refresh", params={"set_balance": 15})

    assert response.status_code == 200
    assert cm.load_config()["account_balance"] == 22
    assert client.get("/api/status").json()["account"]["balance"] == 15


@pytest.mark.parametrize("amount", ["nan", "inf", "-inf", "-1"])
def test_balance_rebase_rejects_invalid_amount_before_network(client, api_env, monkeypatch, amount):
    def unexpected_network():
        raise AssertionError("Invalid balance must be rejected before creating a client")

    monkeypatch.setattr(api_env["server"].orchestrator, "get_client", unexpected_network)
    assert client.get("/api/account/refresh", params={"set_balance": amount}).status_code == 400


# ------------------------------------------------------------------ P2-11b

def test_videos_listing_returns_quoted_urls(client, api_env):
    fname = "mein video #1 & co (final).mp4"
    (api_env["outputs"] / fname).write_bytes(b"video-bytes")

    res = client.get("/api/videos")

    assert res.status_code == 200
    entry = next(v for v in res.json()["videos"] if v["filename"] == fname)
    assert entry["url"] == "/api/videos/file/" + urllib.parse.quote(fname)
    assert " " not in entry["url"]
    assert "#" not in entry["url"]

    # Die encodete URL muss die Datei tatsächlich ausliefern:
    assert client.get(entry["url"]).status_code == 200


def test_video_file_endpoint_blocks_path_traversal(client):
    assert client.get("/api/videos/file/..%5Cconfig.json").status_code == 404
    assert client.get("/api/videos/file/nichtvorhanden.mp4").status_code == 404


# ------------------------------------------------------------------ P3-20

def test_delete_unknown_video_returns_404(client):
    res = client.post("/api/videos/delete", json={"filename": "gibtsnicht.mp4"})
    assert res.status_code == 404


def test_delete_existing_video_returns_200(client, api_env):
    fname = "loeschmich.mp4"
    (api_env["outputs"] / fname).write_bytes(b"data")

    res = client.post("/api/videos/delete", json={"filename": fname})

    assert res.status_code == 200
    assert not (api_env["outputs"] / fname).exists()


# ------------------------------------------------------------------ WebSocket

def test_ws_logs_replays_recent_history(client):
    client.get("/api/status")  # sicherstellen, dass App läuft
    import server as srv_mod
    srv_mod.orchestrator.log("REGTEST_MARKER_42", level="INFO")

    with client.websocket_connect("/ws/logs") as ws:
        found = False
        for _ in range(80):
            msg = ws.receive_text()
            if "REGTEST_MARKER_42" in msg:
                found = True
                break
        assert found, "Beim Verbinden müssen die recent_logs replayspielt werden"


def test_pod_start_rejected_in_invalid_state(client):
    """Start-Endpoint lehnt ab, wenn bereits ein Pod aktiv ist (Guard-Test)."""
    import server as srv_mod
    srv_mod.orchestrator.status = "RUNNING"

    res = client.post("/api/pod/start")

    assert res.status_code == 400


# ------------------------------------------------------- GPU-Verfügbarkeit

class _FakeGpuClient:
    """Ersetzt RunPodClient in /api/gpus (kein Netzwerkzugriff im Test)."""

    def __init__(self, gpus):
        self._gpus = gpus

    def get_gpu_types(self):
        return self._gpus


def test_gpus_endpoint_marks_learned_out_of_stock(client, api_env, monkeypatch):
    """Vergangene Buchungsfehler sind erkennbar, ohne andere GPUs grün zu raten."""
    srv = api_env["server"]
    gpus = [
        {"id": "NVIDIA A40", "displayName": "A40", "memoryInGb": 48,
         "communityPrice": 0.35, "securePrice": 0.49},
        {"id": "NVIDIA GeForce RTX 4090", "displayName": "RTX 4090", "memoryInGb": 24,
         "communityPrice": 0.34, "securePrice": 0.74},
    ]
    monkeypatch.setattr(srv.orchestrator, "get_client", lambda: _FakeGpuClient(gpus))
    srv.orchestrator.mark_gpu_unavailable("NVIDIA A40")

    res = client.get("/api/gpus")

    assert res.status_code == 200
    gpus_out = res.json()["gpus"]
    by_id = {g["id"]: g for g in gpus_out}
    assert by_id["NVIDIA A40"]["available"] is False
    assert by_id["NVIDIA A40"]["availability_source"] == "recent_booking_failure"
    assert by_id["NVIDIA GeForce RTX 4090"]["available"] is None
    assert by_id["NVIDIA GeForce RTX 4090"]["availability"] == "unknown"
    # Vergriffene Karten werden ans Ende sortiert
    assert gpus_out[-1]["id"] == "NVIDIA A40"


def test_gpu_availability_reset_endpoint(client, api_env):
    srv = api_env["server"]
    srv.orchestrator.mark_gpu_unavailable("NVIDIA A40")

    res = client.post("/api/gpus/availability/reset")

    assert res.status_code == 200
    assert res.json()["success"] is True
    assert srv.orchestrator.get_gpu_availability() == {}


def test_gpus_uses_cloud_query_and_reports_metadata(client, api_env, monkeypatch):
    srv = api_env["server"]
    cm.update_config({"cloud_type": "COMMUNITY"})
    gpus = [{"id": "NVIDIA A40", "displayName": "A40", "memoryInGb": 48,
             "communityPrice": 0.35, "securePrice": 0.49,
             "communityStock": {"stockStatus": "Low", "uninterruptablePrice": 0.35},
             "secureStock": {"stockStatus": "High", "uninterruptablePrice": 0.49}}]
    monkeypatch.setattr(srv.orchestrator, "get_client", lambda: _FakeGpuClient(gpus))

    data = client.get("/api/gpus?cloud_type=SECURE").json()
    gpu = data["gpus"][0]
    assert data["cloud_type"] == gpu["cloud_type"] == "SECURE"
    assert data["checked_at"].endswith("+00:00")
    assert data["availability_notice"]
    assert gpu["hourly_price"] == 0.49
    assert gpu["availability"] == "available"
    assert client.get("/api/gpus").json()["gpus"][0]["availability"] == "limited"
    assert cm.load_config()["cloud_type"] == "COMMUNITY"


def test_gpus_live_stock_takes_precedence_over_old_booking_failure(client, api_env, monkeypatch):
    srv = api_env["server"]
    gpus = [{"id": "NVIDIA A40", "displayName": "A40", "memoryInGb": 48,
             "secureStock": {"stockStatus": "High", "uninterruptablePrice": 0.49}}]
    monkeypatch.setattr(srv.orchestrator, "get_client", lambda: _FakeGpuClient(gpus))
    srv.orchestrator.mark_gpu_unavailable("NVIDIA A40")

    gpu = client.get("/api/gpus").json()["gpus"][0]
    assert gpu["availability"] == "available"
    assert gpu["availability_source"] == "runpod_live"
    # Reading fresh stock must not silently reset the learned error cache.
    assert "NVIDIA A40" in srv.orchestrator.get_gpu_availability()


def test_gpus_hide_incompatible_amd_and_low_memory_cards(client, api_env, monkeypatch):
    srv = api_env["server"]
    gpus = [{"id": "AMD Instinct MI300X OAM", "displayName": "MI300X", "memoryInGb": 192},
            {"id": "NVIDIA RTX A4000", "displayName": "A4000", "memoryInGb": 16},
            {"id": "NVIDIA A40", "displayName": "A40", "memoryInGb": 48}]
    monkeypatch.setattr(srv.orchestrator, "get_client", lambda: _FakeGpuClient(gpus))
    assert [gpu["id"] for gpu in client.get("/api/gpus").json()["gpus"]] == ["NVIDIA A40"]


def test_gpus_reject_invalid_cloud_without_network(client):
    assert client.get("/api/gpus?cloud_type=invalid").status_code == 400


def test_gpus_does_not_apply_booking_failure_to_another_cloud(client, api_env, monkeypatch):
    srv = api_env["server"]
    cm.update_config({"cloud_type": "COMMUNITY"})
    srv.orchestrator.mark_gpu_unavailable("NVIDIA A40")
    gpus = [{"id": "NVIDIA A40", "displayName": "A40", "memoryInGb": 48}]
    monkeypatch.setattr(srv.orchestrator, "get_client", lambda: _FakeGpuClient(gpus))
    gpu = client.get("/api/gpus?cloud_type=SECURE").json()["gpus"][0]
    assert gpu["availability"] == "unknown"


def test_config_cloud_change_resets_old_booking_failures(client, api_env):
    srv = api_env["server"]
    cm.update_config({"cloud_type": "COMMUNITY"})
    srv.orchestrator.mark_gpu_unavailable("NVIDIA A40")

    response = client.post("/api/config", json={"cloud_type": "SECURE"})

    assert response.status_code == 200
    assert cm.load_config()["cloud_type"] == "SECURE"
    assert srv.orchestrator.get_gpu_availability() == {}


def test_config_save_without_cloud_change_preserves_booking_failures(client, api_env):
    srv = api_env["server"]
    cm.update_config({"cloud_type": "COMMUNITY"})
    srv.orchestrator.mark_gpu_unavailable("NVIDIA A40")

    for update in ({"cloud_type": "COMMUNITY"}, {"gpu_type_id": "NVIDIA RTX A6000"},
                   {"cloud_type": "community"}):
        response = client.post("/api/config", json=update)
        assert response.status_code == 200
        assert "NVIDIA A40" in srv.orchestrator.get_gpu_availability()
