import requests
from typing import Dict, Any, Optional, List
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

RUNPOD_GRAPHQL_BASE = "https://api.runpod.io/graphql"

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
        query = """
        query {
          gpuTypes {
            id
            displayName
            memoryInGb
            securePrice
            communityPrice
          }
        }
        """
        data = self._request(query)
        return data.get("gpuTypes", [])

    def get_pod(self, pod_id: str) -> Optional[Dict[str, Any]]:
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
            return data.get("pod")
        except Exception as e:
            print(f"[RunPodClient] Error getting pod {pod_id}: {e}")
            return None

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
