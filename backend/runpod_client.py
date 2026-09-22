import requests
import math
from typing import Dict, Any, Optional, List
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RUNPOD_GRAPHQL_BASE = "https://api.runpod.io/graphql"


def gpu_offer_details(gpu: Dict[str, Any], cloud_type: str) -> Dict[str, Any]:
    """Normalize RunPod's stock separately from its catalog prices.

    See https://docs.runpod.io/sdks/graphql/manage-pods (Check GPU availability).
    A catalog price, or a null lowestPrice, does not prove current availability.
    """
    clouds = ("SECURE", "COMMUNITY") if cloud_type == "ALL" else (cloud_type,)
    stock_map = {"high": "available", "medium": "available", "low": "limited",
                 "none": "unavailable", "out": "unavailable"}

    def positive_price(value):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value) if math.isfinite(value) and value > 0 else None
        return None

    offers = []
    for cloud in clouds:
        prefix = "secure" if cloud == "SECURE" else "community"
        stock = gpu.get(prefix + "Stock") or {}
        offered = gpu.get(prefix + "Cloud") is not False
        availability = stock_map.get(str(stock.get("stockStatus") or "").lower(), "unknown")
        source = "runpod_live" if availability != "unknown" else "unknown"
        if not offered:
            availability, source = "unavailable", "runpod_cloud_catalog"
        live_price = positive_price(stock.get("uninterruptablePrice")) if offered else None
        catalog_price = positive_price(gpu.get(prefix + "Price")) if offered else None
        offers.append({
            "cloud_type": cloud,
            "availability": availability,
            "availability_source": source,
            "hourly_price": live_price if live_price is not None else catalog_price,
            "price_source": "runpod_live" if live_price is not None else
                            "runpod_catalog" if catalog_price is not None else "unknown",
        })

    # An ALL request can deploy in either cloud. An unknown cloud must not turn
    # an explicit no-stock result in the other cloud into "unavailable".
    states = {offer["availability"] for offer in offers}
    availability = next(state for state in ("available", "limited", "unknown", "unavailable")
                        if state in states)
    candidates = [offer for offer in offers if offer["availability"] in ("available", "limited")]
    if not candidates:
        candidates = [offer for offer in offers if offer["availability"] == "unknown"] or offers
    priced = [offer for offer in candidates if offer["hourly_price"] is not None]
    selected = min(priced, key=lambda offer: offer["hourly_price"]) if priced else None
    source = next(offer["availability_source"] for offer in offers
                  if offer["availability"] == availability)
    labels = {"available": "Verfügbar", "limited": "Wenige verfügbar",
              "unavailable": "Nicht verfügbar", "unknown": "Verfügbarkeit unbekannt"}
    label = labels[availability]
    if all(offer["availability_source"] == "runpod_cloud_catalog" for offer in offers):
        label = "In dieser Cloud nicht angeboten"

    return {
        "availability": availability,
        "availability_label": label,
        "availability_source": source,
        "available": True if availability in ("available", "limited") else
                     False if availability == "unavailable" else None,
        "hourly_price": selected["hourly_price"] if selected else None,
        "price_source": selected["price_source"] if selected else "unknown",
        "price_cloud_type": selected["cloud_type"] if selected else None,
        "cloud_type": cloud_type,
    }

_session = requests.Session()
_retry_strategy = Retry(
    total=3,
    backoff_factor=1.0,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET", "POST"),
    raise_on_status=False,
)
_session.mount("https://", HTTPAdapter(max_retries=_retry_strategy))
_session.mount("http://", HTTPAdapter(max_retries=_retry_strategy))

# Dedicated session without automatic retries for mutating operations (prevent duplicate pod creation)
_no_retry_session = requests.Session()

class RunPodClient:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip() if api_key else ""

    def _request(self, query: str, variables: Optional[Dict[str, Any]] = None, mutating: bool = False) -> Dict[str, Any]:
        if not self.api_key:
            raise ValueError("RunPod API Key ist nicht konfiguriert.")

        url = f"{RUNPOD_GRAPHQL_BASE}?api_key={self.api_key}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }
        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        session = _no_retry_session if mutating else _session
        res = session.post(url, json=payload, headers=headers, timeout=25)
        res.raise_for_status()
        res_json = res.json()

        if "errors" in res_json and res_json["errors"]:
            msgs = [e.get("message", "Unknown GraphQL error") for e in res_json["errors"]]
            raise Exception("RunPod API Fehler: " + ", ".join(msgs))

        return res_json.get("data", {})

    def get_myself(self) -> Dict[str, Any]:
        query = """
        query {
          myself {
            id
            email
            pods {
              id
              name
              desiredStatus
              lastStatusChange
              runtime {
                uptimeInSeconds
                ports {
                  ip
                  isIpPublic
                  privatePort
                  publicPort
                  type
                }
              }
            }
          }
        }
        """
        data = self._request(query)
        return data.get("myself", {})

    def get_gpu_types(self) -> List[Dict[str, Any]]:
        # Query both clouds explicitly: secureCloud=false is Community, while
        # omitting this filter is not a reliable substitute for an ALL merge.
        query = """
        query {
          gpuTypes {
            id
            displayName
            memoryInGb
            securePrice
            communityPrice
            secureCloud
            communityCloud
            secureStock: lowestPrice(input: {gpuCount: 1, secureCloud: true}) {
              stockStatus
              uninterruptablePrice
            }
            communityStock: lowestPrice(input: {gpuCount: 1, secureCloud: false}) {
              stockStatus
              uninterruptablePrice
            }
          }
        }
        """
        try:
            data = self._request(query)
            return data.get("gpuTypes", [])
        except Exception:
            # Keep the catalog usable if the stock resolver is temporarily
            # unavailable. Missing stock fields normalize to "unknown".
            return self._get_gpu_catalog()

    def _get_gpu_catalog(self) -> List[Dict[str, Any]]:
        query = """
        query {
          gpuTypes {
            id
            displayName
            memoryInGb
            securePrice
            communityPrice
            secureCloud
            communityCloud
          }
        }
        """
        data = self._request(query)
        return data.get("gpuTypes", [])

    def get_pod(self, pod_id: str) -> Optional[Dict[str, Any]]:
        """Return None only for a confirmed null pod, never for a failed query."""
        query = """
        query($podId: String!) {
          pod(input: {podId: $podId}) {
            id
            name
            desiredStatus
            lastStatusChange
            imageName
            costPerHr
            runtime {
              uptimeInSeconds
              ports {
                ip
                isIpPublic
                privatePort
                publicPort
                type
              }
              gpus {
                id
                gpuUtilPercent
                memoryUtilPercent
              }
            }
          }
        }
        """
        try:
            data = self._request(query, {"podId": pod_id})
        except Exception as e:
            # HTTP errors may include the authenticated URL. Keep the failure
            # distinct from a missing pod without leaking that URL to callers.
            raise RuntimeError(
                f"RunPod-Podstatusabfrage fehlgeschlagen ({type(e).__name__}). "
                "API-Verbindung und Berechtigung prüfen."
            ) from None
        if not isinstance(data, dict) or "pod" not in data:
            raise RuntimeError("RunPod-Podstatusabfrage lieferte keine gültige Pod-Antwort.")
        pod = data["pod"]
        if pod is not None and (not isinstance(pod, dict) or pod.get("id") != pod_id):
            raise RuntimeError("RunPod-Podstatusabfrage lieferte keine passende Pod-Antwort.")
        return pod

    def deploy_pod(
        self,
        name: str,
        gpu_type_id: str,
        volume_in_gb: int = 75,
        container_disk_in_gb: int = 30,
        cloud_type: str = "ALL",
        public_ssh_key: str = "",
        template_id: Optional[str] = None
    ) -> str:
        """
        Deploys an on-demand GPU Pod with PyTorch and mapped ports (22 for SSH, 7860 for Wan2GP WebUI).
        """
        query = """
        mutation($input: PodFindAndDeployOnDemandInput!) {
          podFindAndDeployOnDemand(input: $input) {
            id
            desiredStatus
            imageName
          }
        }
        """
        env_vars = []
        if public_ssh_key:
            env_vars.append({"key": "PUBLIC_KEY", "value": public_ssh_key})
            env_vars.append({"key": "SSH_PUBLIC_KEY", "value": public_ssh_key})

        input_payload = {
            "name": name,
            "gpuTypeId": gpu_type_id,
            "gpuCount": 1,
            "cloudType": cloud_type,
            "volumeInGb": volume_in_gb,
            "containerDiskInGb": container_disk_in_gb,
            "ports": "22/tcp,7860/http",
            "volumeMountPath": "/workspace",
            "env": env_vars
        }

        # Use runpod-torch-v240 (PyTorch 2.4 / CUDA 12.4) by default
        input_payload["templateId"] = template_id if template_id else "runpod-torch-v240"

        data = self._request(query, {"input": input_payload}, mutating=True)
        pod_info = data.get("podFindAndDeployOnDemand", {})
        pod_id = pod_info.get("id")
        if not pod_id:
            raise Exception("RunPod antwortete ohne Pod-ID beim Bereitstellen.")
        return pod_id

    def resume_pod(self, pod_id: str, gpu_count: int = 1) -> bool:
        query = """
        mutation($input: PodResumeInput!) {
          podResume(input: $input) {
            id
            desiredStatus
          }
        }
        """
        try:
            self._request(query, {"input": {"podId": pod_id, "gpuCount": gpu_count}}, mutating=True)
            return True
        except Exception as e:
            print(f"[RunPodClient] Error resuming pod {pod_id}: {e}")
            raise

    def stop_pod(self, pod_id: str) -> bool:
        query = """
        mutation($input: PodStopInput!) {
          podStop(input: $input) {
            id
            desiredStatus
          }
        }
        """
        try:
            self._request(query, {"input": {"podId": pod_id}}, mutating=True)
            return True
        except Exception as e:
            print(f"[RunPodClient] Error stopping pod {pod_id}: {e}")
            raise

    def terminate_pod(self, pod_id: str) -> bool:
        query = """
        mutation($podId: String!) {
          podTerminate(input: {podId: $podId})
        }
        """
        try:
            self._request(query, {"podId": pod_id}, mutating=True)
            return True
        except Exception as e:
            print(f"[RunPodClient] Error terminating pod {pod_id}: {e}")
            raise

    @staticmethod
    def get_proxy_url(pod_id: str, port: int = 7860) -> str:
        """Returns the official RunPod direct proxy URL for an exposed HTTP port."""
        return f"https://{pod_id}-{port}.proxy.runpod.net"
