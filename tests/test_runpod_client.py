# Regressionstests für backend/runpod_client.py
# Deckt u. a. P2-13 ab: Mutationen (deploy/resume/stop/terminate) dürfen kein
# automatisches Retry verwenden, damit ein verlorener Response nicht zu
# doppelt gebuchten Pods führt.

import pytest
import traceback

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


def test_get_pod_raises_on_graphql_error_instead_of_claiming_not_found(sessions):
    retry, _ = sessions
    retry.response = FakeResponse(errors=[{"message": "not found"}])
    client = rpc.RunPodClient("rpa_testkey")

    with pytest.raises(RuntimeError, match="Podstatusabfrage fehlgeschlagen"):
        client.get_pod("p1")


def test_get_pod_returns_none_for_explicit_null_only(sessions):
    retry, _ = sessions
    retry.response = FakeResponse(data={"pod": None})

    assert rpc.RunPodClient("rpa_testkey").get_pod("p1") is None


def test_get_pod_transport_failure_does_not_expose_api_key(sessions, monkeypatch, capsys):
    retry, _ = sessions
    key = "rpa_PRIVATE_TEST_VALUE"

    def fail(*_args, **_kwargs):
        raise rpc.requests.ConnectionError(f"Connection failed: https://api.runpod.io/graphql?api_key={key}")

    monkeypatch.setattr(retry, "post", fail)
    with pytest.raises(RuntimeError, match="ConnectionError") as caught:
        rpc.RunPodClient(key).get_pod("p1")

    assert key not in str(caught.value)
    assert key not in "".join(traceback.format_exception(caught.value))
    assert key not in capsys.readouterr().out


@pytest.mark.parametrize("data", [None, {}, {"pod": "invalid"}, {"pod": {}}, {"pod": {"id": "other-pod"}}])
def test_get_pod_malformed_response_is_not_treated_as_missing(monkeypatch, data):
    client = rpc.RunPodClient("rpa_testkey")
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: data)

    with pytest.raises(RuntimeError, match="Pod-Antwort"):
        client.get_pod("p1")


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


def test_gpu_catalog_queries_both_clouds_for_live_stock(sessions):
    retry, no_retry = sessions
    retry.response = FakeResponse(data={"gpuTypes": [{"id": "NVIDIA A40"}]})

    assert rpc.RunPodClient("testkey").get_gpu_types() == [{"id": "NVIDIA A40"}]
    query = retry.posts[0]["json"]["query"]
    assert "secureStock: lowestPrice(input: {gpuCount: 1, secureCloud: true})" in query
    assert "communityStock: lowestPrice(input: {gpuCount: 1, secureCloud: false})" in query
    assert "stockStatus" in query
    assert not no_retry.posts


def test_gpu_catalog_falls_back_without_inventing_stock(monkeypatch):
    client = rpc.RunPodClient("testkey")
    queries = []

    def request(query):
        queries.append(query)
        if "lowestPrice" in query:
            raise RuntimeError("stock resolver unavailable")
        return {"gpuTypes": [{"id": "NVIDIA A40", "communityPrice": 0.35}]}

    monkeypatch.setattr(client, "_request", request)
    gpu = client.get_gpu_types()[0]
    offer = rpc.gpu_offer_details(gpu, "COMMUNITY")

    assert len(queries) == 2
    assert offer["availability"] == "unknown"
    assert offer["available"] is None
    assert offer["hourly_price"] == 0.35
    assert offer["price_source"] == "runpod_catalog"


@pytest.mark.parametrize("stock,expected,available", [
    ("High", "available", True), ("Medium", "available", True),
    ("Low", "limited", True), ("None", "unavailable", False),
    ("Out", "unavailable", False), (None, "unknown", None),
    ("unexpected", "unknown", None),
])
def test_stock_mapping_does_not_guess_unknown_values(stock, expected, available):
    offer = rpc.gpu_offer_details({
        "communityPrice": 0.35, "communityStock": {"stockStatus": stock},
    }, "COMMUNITY")
    assert offer["availability"] == expected
    assert offer["available"] is available


def test_cloud_not_offered_ignores_misleading_catalog_price():
    offer = rpc.gpu_offer_details({
        "communityCloud": False, "communityPrice": 0.35,
        "secureCloud": True, "securePrice": 0.49,
    }, "COMMUNITY")
    assert offer["availability"] == "unavailable"
    assert offer["availability_source"] == "runpod_cloud_catalog"
    assert offer["hourly_price"] is None


def test_secure_price_does_not_fall_back_to_cheaper_community():
    offer = rpc.gpu_offer_details({
        "communityPrice": 0.35, "securePrice": 0.49,
        "secureStock": {"stockStatus": "Low", "uninterruptablePrice": 0.55},
    }, "SECURE")
    assert offer["hourly_price"] == 0.55
    assert offer["price_cloud_type"] == "SECURE"
    assert offer["price_source"] == "runpod_live"


def test_all_clouds_prefers_available_offer_over_lower_out_of_stock_price():
    offer = rpc.gpu_offer_details({
        "communityPrice": 0.35, "securePrice": 0.49,
        "communityStock": {"stockStatus": "None", "uninterruptablePrice": 0.35},
        "secureStock": {"stockStatus": "Low", "uninterruptablePrice": 0.49},
    }, "ALL")
    assert offer["availability"] == "limited"
    assert offer["hourly_price"] == 0.49
    assert offer["price_cloud_type"] == "SECURE"


def test_all_clouds_reports_unknown_when_only_one_cloud_has_no_stock():
    offer = rpc.gpu_offer_details({
        "communityPrice": 0.35, "securePrice": 0.49,
        "communityStock": {"stockStatus": "None"},
    }, "ALL")
    assert offer["availability"] == "unknown"


@pytest.mark.parametrize("price", [0, -1, None, float("nan"), float("inf"), "0.35", True])
def test_invalid_prices_are_not_usable(price):
    offer = rpc.gpu_offer_details({"communityPrice": price}, "COMMUNITY")
    assert offer["hourly_price"] is None
    assert offer["price_source"] == "unknown"


# ---------------------------------------------------------------- P1-6

class _ExplodingSession:
    """Simuliert requests-Ausnahmen, die die URL (inkl. ?api_key=...) enthalten."""

    def __init__(self, exc_type):
        self._exc_type = exc_type

    def post(self, url, **kwargs):
        raise self._exc_type(f"503 Server Error: Service Unavailable for url: {url}")


@pytest.mark.parametrize("mutating", [False, True])
def test_request_errors_never_contain_the_api_key(monkeypatch, mutating):
    """P1-6: requests bettet die ?api_key=-URL in HTTP-Fehler ein. Die zentrale
    Aufbereitung muss den Key aus ALLEN Ausnahmen entfernen (lesbar für Logs
    und API-Antworten), ohne die Fehlerart zu verändern."""
    import requests as requests_mod
    key = "testkey_ABCDEFGHIJKLMNOP"
    client = rpc.RunPodClient(key)
    boom = _ExplodingSession(requests_mod.HTTPError)
    monkeypatch.setattr(rpc, "_session", boom)
    monkeypatch.setattr(rpc, "_no_retry_session", boom)

    with pytest.raises(requests_mod.HTTPError) as exc_info:
        client._request("query { myself { id } }", mutating=mutating)

    assert key not in str(exc_info.value), "API-Key darf im Fehlertext nicht vorkommen"
    assert "***API-KEY***" in str(exc_info.value), "Ursache muss erkennbar bleiben"
    assert key not in "".join(traceback.format_exception(exc_info.value))
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None


def test_request_connection_errors_are_sanitized_too(monkeypatch):
    import requests as requests_mod
    key = "testkey_ABCDEFGHIJKLMNOP"
    client = rpc.RunPodClient(key)

    class _TimeoutSession:
        def post(self, url, **kwargs):
            raise requests_mod.ConnectionError(
                f"HTTPSConnectionPool(host='api.runpod.io', url={url}): Max retries exceeded")

    monkeypatch.setattr(rpc, "_session", _TimeoutSession())
    monkeypatch.setattr(rpc, "_no_retry_session", _TimeoutSession())

    with pytest.raises(requests_mod.ConnectionError) as exc_info:
        client._request("query { myself { id } }")

    assert key not in str(exc_info.value)
    assert "api.runpod.io" in str(exc_info.value), "Ursache muss erkennbar bleiben"
    assert key not in "".join(traceback.format_exception(exc_info.value))
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
