# Regressionstests für backend/runpod_client.py
# Deckt u. a. P2-13 ab: Mutationen (deploy/resume/stop/terminate) dürfen kein
# automatisches Retry verwenden, damit ein verlorener Response nicht zu
# doppelt gebuchten Pods führt.

import pytest

import backend.runpod_client as rpc


class FakeResponse:
    def __init__(self, data=None, errors=None):
        self._data = data or {}
        self._errors = errors

    def raise_for_status(self):
        pass

    def json(self):
        out = {"data": self._data}
        if self._errors:
            out["errors"] = self._errors
        return out


class FakeSession:
    def __init__(self):
        self.posts = []
        self.response = FakeResponse()

    def post(self, url, json=None, headers=None, timeout=None):
        self.posts.append({"url": url, "json": json, "headers": headers})
        return self.response


@pytest.fixture()
def sessions(monkeypatch):
    retry = FakeSession()
    no_retry = FakeSession()
    monkeypatch.setattr(rpc, "_session", retry)
    monkeypatch.setattr(rpc, "_no_retry_session", no_retry)
    return retry, no_retry


def test_missing_api_key_raises_value_error():
    client = rpc.RunPodClient("")
    with pytest.raises(ValueError, match="API Key"):
        client.get_gpu_types()


def test_graphql_errors_raise_exception(sessions):
    retry, _ = sessions
    retry.response = FakeResponse(errors=[{"message": "boom"}, {"message": "kaputt"}])
    client = rpc.RunPodClient("rpa_testkey")
    with pytest.raises(Exception, match=r"RunPod API Fehler: boom, kaputt"):
        client.get_gpu_types()


@pytest.mark.parametrize("mutation", ["stop", "resume", "terminate", "deploy"])
def test_mutations_use_session_without_retry(sessions, mutation):
    retry, no_retry = sessions
    no_retry.response = FakeResponse(
        data={"podStop": {"id": "p1"}, "podResume": {"id": "p1"},
              "podTerminate": True, "podFindAndDeployOnDemand": {"id": "p1"}}
    )
    client = rpc.RunPodClient("rpa_testkey")

    if mutation == "stop":
        client.stop_pod("p1")
    elif mutation == "resume":
        client.resume_pod("p1")
    elif mutation == "terminate":
        client.terminate_pod("p1")
    else:
        client.deploy_pod(name="x", gpu_type_id="A40")

    assert len(no_retry.posts) == 1, f"{mutation} muss ohne Auto-Retry gesendet werden"
    assert len(retry.posts) == 0, f"{mutation} darf NICHT die Retry-Session nutzen (Doppel-Deploy-Risiko)"


def test_queries_use_retry_session(sessions):
    retry, no_retry = sessions
    retry.response = FakeResponse(data={"pod": {"id": "p1", "desiredStatus": "RUNNING"}})
    client = rpc.RunPodClient("rpa_testkey")

    pod = client.get_pod("p1")

    assert pod["id"] == "p1"
    assert len(retry.posts) == 1
    assert len(no_retry.posts) == 0


def test_get_pod_swallows_errors_and_returns_none(sessions):
    retry, _ = sessions
    retry.response = FakeResponse(errors=[{"message": "not found"}])
    client = rpc.RunPodClient("rpa_testkey")

    assert client.get_pod("p1") is None


def test_deploy_pod_payload_structure(sessions):
    _, no_retry = sessions
    no_retry.response = FakeResponse(data={"podFindAndDeployOnDemand": {"id": "newpod-1"}})
    client = rpc.RunPodClient("rpa_testkey")

    pod_id = client.deploy_pod(
        name="studio",
        gpu_type_id="NVIDIA A40",
        volume_in_gb=75,
        container_disk_in_gb=30,
        cloud_type="ALL",
        public_ssh_key="ssh-rsa AAAA...",
    )

    assert pod_id == "newpod-1"
    payload = no_retry.posts[0]["json"]
    inp = payload["variables"]["input"]
    assert inp["gpuTypeId"] == "NVIDIA A40"
    assert inp["volumeInGb"] == 75
    assert inp["ports"] == "22/tcp,7860/http"
    assert inp["templateId"] == "runpod-torch-v240"
    env = {e["key"]: e["value"] for e in inp["env"]}
    assert env["PUBLIC_KEY"] == "ssh-rsa AAAA..."


def test_deploy_pod_without_id_raises(sessions):
    _, no_retry = sessions
    no_retry.response = FakeResponse(data={"podFindAndDeployOnDemand": {}})
    client = rpc.RunPodClient("rpa_testkey")

    with pytest.raises(Exception, match="ohne Pod-ID"):
        client.deploy_pod(name="x", gpu_type_id="A40")


def test_proxy_url_format():
    assert rpc.RunPodClient.get_proxy_url("abc123", 7860) == "https://abc123-7860.proxy.runpod.net"
