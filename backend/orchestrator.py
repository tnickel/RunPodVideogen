import os
import time
import threading
import shlex
from typing import Dict, Any, List, Optional
from collections import deque

from backend.config_manager import load_config, update_config, get_outputs_dir
from backend.runpod_client import RunPodClient
from backend.ssh_client import SSHManager, get_or_create_ssh_keys

class PodOrchestrator:
    def __init__(self):
        self.lock = threading.Lock()
        self.log_history = deque(maxlen=300)
        self.listeners = set()

        self.status = "OFFLINE"
        self.status_detail = "Bereit"
        self.pod_id: Optional[str] = None
        self.pod_ip: Optional[str] = None
        self.pod_ssh_port: Optional[int] = None
        self.proxy_url: Optional[str] = None
        
        self.session_start_time: Optional[float] = None
        self.hourly_rate: float = 0.34
        self.accumulated_cost: float = 0.0
        self.last_activity_time: float = time.time()
        
        self.is_bootstrapping = False
        self._stop_bg_thread = False
        self._email_cache: Optional[str] = None
        self._email_cache_time: float = 0.0
        
        # Load persisted pod if available
        cfg = load_config()
        self.pod_id = cfg.get("active_pod_id")
        self.status = cfg.get("active_pod_status", "OFFLINE")

        # Start background polling/sync worker thread
        self.bg_thread = threading.Thread(target=self._background_loop, daemon=True)
        self.bg_thread.start()

        # Reconcile runtime state if a pod was previously active
        if self.pod_id:
            threading.Thread(target=self._reconcile_startup_state, daemon=True).start()

    def log(self, message: str, level: str = "INFO"):
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] [{level}] {message}"
        with self.lock:
            self.log_history.append(entry)
        
        # Notify WebSocket listeners
        for listener in list(self.listeners):
            try:
                listener(entry)
            except Exception:
                self.listeners.discard(listener)

    def add_listener(self, callback):
        with self.lock:
            self.listeners.add(callback)

    def remove_listener(self, callback):
        with self.lock:
            self.listeners.discard(callback)

    def get_client(self) -> RunPodClient:
        cfg = load_config()
        key = cfg.get("runpod_api_key", "")
        return RunPodClient(key)

    def get_account_email(self) -> str:
        """Returns the account email, cached for 300s to avoid spamming RunPod API on every poll."""
        now = time.time()
        if self._email_cache and (now - self._email_cache_time < 300):
            return self._email_cache
        cfg = load_config()
        cached_email = cfg.get("account_email", "")
        try:
            client = self.get_client()
            user = client.get_myself()
            if user and "email" in user and user["email"]:
                self._email_cache = user["email"]
                self._email_cache_time = now
                if user["email"] != cached_email:
                    update_config({"account_email": user["email"]})
                return user["email"]
        except Exception:
            pass
        return cached_email

    def _reconcile_startup_state(self):
        """Recovers runtime state (IP, SSH port, proxy URL, cost, uptime) after server restart."""
        if not self.pod_id:
            return
        try:
            client = self.get_client()
            pod = client.get_pod(self.pod_id)
            if not pod:
                self.log(f"Persistierter Pod {self.pod_id} existiert auf RunPod nicht mehr. Bereinige Status...", level="WARNING")
                with self.lock:
                    self.pod_id = None
                    self.status = "OFFLINE"
                    self.status_detail = "Bereit"
                update_config({"active_pod_id": None, "active_pod_status": "OFFLINE"})
                return

            desired = pod.get("desiredStatus")
            if desired == "RUNNING":
                runtime = pod.get("runtime") or {}
                ports = runtime.get("ports") or []
                ip = None
                ssh_port = None
                for p in ports:
                    if p.get("privatePort") == 22:
                        ip = p.get("ip")
                        ssh_port = p.get("publicPort")
                        break

                with self.lock:
                    self.status = "RUNNING"
                    self.status_detail = "Wan2GP aktiv & verbunden"
                    self.pod_ip = ip
                    self.pod_ssh_port = int(ssh_port) if ssh_port else None
                    self.proxy_url = RunPodClient.get_proxy_url(self.pod_id, 7860)
                    self.hourly_rate = float(pod.get("costPerHr") or 0.34)
                    uptime = runtime.get("uptimeInSeconds") or 0
                    self.session_start_time = time.time() - uptime
                    self.last_activity_time = time.time()
                update_config({"active_pod_status": "RUNNING"})
                self.log(f"Zustand nach Server-Neustart wiederhergestellt: Pod {self.pod_id} läuft (SSH: {ip}:{ssh_port})")

            elif desired == "EXITED":
                with self.lock:
                    self.status = "STOPPED"
                    self.status_detail = "GPU pausiert (Standby)"
                    self.pod_ip = None
                    self.pod_ssh_port = None
                update_config({"active_pod_status": "STOPPED"})
                self.log(f"Zustand nach Server-Neustart wiederhergestellt: Pod {self.pod_id} ist im Standby.")

            elif desired == "TERMINATED":
                with self.lock:
                    self.pod_id = None
                    self.status = "OFFLINE"
                    self.status_detail = "Bereit"
                update_config({"active_pod_id": None, "active_pod_status": "OFFLINE"})
        except Exception as e:
            self.log(f"Hinweis: Status-Reconciliation nach Start übersprungen ({e})", level="WARNING")

    def start_pod(self) -> Dict[str, Any]:
        with self.lock:
            if self.status in ["RUNNING", "STARTING", "BOOTSTRAPPING"]:
                return {"success": False, "message": f"Pod befindet sich bereits im Status {self.status}"}
            self.status = "STARTING"
            self.status_detail = "Initialisiere Pod..."
            self.last_activity_time = time.time()
            self.session_start_time = None
            self.accumulated_cost = 0.0

        threading.Thread(target=self._deploy_or_resume_worker, daemon=True).start()
        return {"success": True, "message": "Startvorgang eingeleitet"}

    def stop_pod(self) -> Dict[str, Any]:
        """Pauses the pod (GPU stops, storage preserved)."""
        cfg = load_config()
        pod_id = self.pod_id or cfg.get("active_pod_id")
        if not pod_id:
            return {"success": False, "message": "Kein aktiver Pod vorhanden."}

        client = self.get_client()
        self.log(f"Pausiere Pod {pod_id} (GPU wird gestoppt, Volume bleibt erhalten)...")
        try:
            client.stop_pod(pod_id)
            with self.lock:
                self.status = "STOPPED"
                self.status_detail = "Pod pausiert (Speicher erhalten)"
            update_config({"active_pod_status": "STOPPED"})
            self.log("Pod erfolgreich pausiert. GPU-Kosten gestoppt.")
            return {"success": True, "message": "Pod pausiert"}
        except Exception as e:
            self.log(f"Fehler beim Pausieren: {e}", level="ERROR")
            return {"success": False, "message": str(e)}

    def terminate_pod(self) -> Dict[str, Any]:
        """Terminates the pod completely (Storage and GPU destroyed -> 0€)."""
        cfg = load_config()
        pod_id = self.pod_id or cfg.get("active_pod_id")
        if not pod_id:
            return {"success": False, "message": "Kein aktiver Pod vorhanden."}

        client = self.get_client()
        self.log(f"Terminiere Pod {pod_id} vollständig (0 € Standby-Kosten)...", level="WARNING")
        try:
            client.terminate_pod(pod_id)
            with self.lock:
                self.status = "TERMINATED"
                self.status_detail = "Pod gelöscht (0,00 € Standby)"
                self.pod_id = None
                self.pod_ip = None
                self.pod_ssh_port = None
                self.proxy_url = None
            update_config({"active_pod_id": None, "active_pod_status": "TERMINATED"})
            self.log("Pod erfolgreich terminiert. Keine weiteren Kosten.")
            return {"success": True, "message": "Pod terminiert"}
        except Exception as e:
            self.log(f"Fehler beim Terminieren: {e}", level="ERROR")
            return {"success": False, "message": str(e)}

    def _deploy_or_resume_worker(self):
        cfg = load_config()
        client = self.get_client()
        _, pub_key = get_or_create_ssh_keys()
        
        pod_id = self.pod_id or cfg.get("active_pod_id")

        # Check if existing stopped pod can be resumed or if it is already running
        can_resume = False
        already_running = False
        if pod_id:
            try:
                pod_info = client.get_pod(pod_id)
                if pod_info:
                    desired = pod_info.get("desiredStatus")
                    if desired == "EXITED":
                        can_resume = True
                    elif desired == "RUNNING":
                        already_running = True
            except Exception:
                can_resume = False
                already_running = False

        try:
            if already_running:
                self.log(f"Pod {pod_id} existiert und läuft bereits. Verwende ihn weiter...")
            elif can_resume:
                self.log(f"Reaktiviere bestehenden Pod {pod_id}...")
                client.resume_pod(pod_id)
            else:
                gpu_type = cfg.get("gpu_type_id", "NVIDIA GeForce RTX 4090")
                cloud_type = cfg.get("cloud_type", "ALL")
                vol_gb = int(cfg.get("volume_disk_gb", 75))
                cont_gb = int(cfg.get("container_disk_gb", 30))

                self.log(f"Erstelle neuen Pod auf RunPod mit GPU '{gpu_type}' (Volume: {vol_gb} GB)...")
                pod_id = client.deploy_pod(
                    name="wan2gp-videogen-studio",
                    gpu_type_id=gpu_type,
                    volume_in_gb=vol_gb,
                    container_disk_in_gb=cont_gb,
                    cloud_type=cloud_type,
                    public_ssh_key=pub_key
                )
                with self.lock:
                    self.pod_id = pod_id
                update_config({"active_pod_id": pod_id, "active_pod_status": "STARTING"})
                self.log(f"Pod erfolgreich gebucht! Pod-ID: {pod_id}")

            # Wait for RUNNING and SSH port mapping
            self.log("Warte auf Zuweisung von IP und SSH-Port durch RunPod...")
            ip, ssh_port = None, None
            pod_info = None
            for attempt in range(60):
                time.sleep(5)
                pod_info = client.get_pod(pod_id)
                if not pod_info:
                    continue

                status = pod_info.get("desiredStatus")
                runtime = pod_info.get("runtime")
                if status == "RUNNING" and runtime:
                    ports = runtime.get("ports", [])
                    for p in ports:
                        if p.get("privatePort") == 22:
                            ip = p.get("ip")
                            ssh_port = p.get("publicPort")
                            break
                    if ip and ssh_port:
                        break
                self.log(f"Warte auf Container-Initialisierung... ({attempt+1}/60)")

            if not ip or not ssh_port:
                raise Exception("Timeout beim Warten auf öffentliche IP / SSH-Port des Containers.")

            with self.lock:
                self.pod_ip = ip
                self.pod_ssh_port = int(ssh_port)
                self.proxy_url = RunPodClient.get_proxy_url(pod_id, 7860)
                if pod_info and pod_info.get("costPerHr"):
                    try:
                        self.hourly_rate = float(pod_info["costPerHr"])
                    except ValueError:
                        pass
                self.status = "BOOTSTRAPPING"
                self.status_detail = "Richte Wan2GP & Modell-Umgebung ein..."

            self.log(f"Pod läuft! SSH: {ip}:{ssh_port} | Proxy: {self.proxy_url}")
            
            # Bootstrap Wan2GP via SSH
            bootstrap_ok = self._bootstrap_wan2gp(ip, int(ssh_port))
            if not bootstrap_ok:
                with self.lock:
                    self.status = "ERROR"
                    self.status_detail = "Bootstrap fehlgeschlagen - Terminal prüfen"
                update_config({"active_pod_status": "ERROR"})
                return

            with self.lock:
                self.status = "RUNNING"
                self.status_detail = "Wan2GP aktiv & bereit"
                self.session_start_time = time.time()
                self.last_activity_time = time.time()
            update_config({"active_pod_status": "RUNNING"})
            self.log(f"Wan2GP erfolgreich gestartet! Bereit unter {self.proxy_url}")

        except Exception as e:
            err_msg = str(e)
            self.log(f"Fehler beim Starten des Pods: {err_msg}", level="ERROR")
            friendly_hint = err_msg
            if "no longer any instances available" in err_msg.lower():
                friendly_hint = "Die gewählte GPU ist in der Community Cloud gerade ausgebucht. Tipp: Wähle 'NVIDIA A40' (48 GB VRAM, ~$0.35/h) oder schalte auf 'Secure Cloud' um und klicke erneut auf 'Pod Starten'."
                self.log(friendly_hint, level="WARNING")
            with self.lock:
                self.status = "ERROR"
                self.status_detail = friendly_hint
            update_config({"active_pod_status": "ERROR"})

    def _bootstrap_wan2gp(self, ip: str, port: int):
        self.log("Starte SSH-Bootstrap für Wan2GP auf dem Pod...")
        ssh = SSHManager(ip, port, log_callback=self.log)

        # Retry SSH connect for up to 60s
        connected = False
        for attempt in range(12):
            try:
                client = ssh.get_client(timeout=10)
                client.close()
                connected = True
                break
            except Exception:
                self.log(f"Warte auf SSH-Daemon im Container... ({attempt+1}/12)")
                time.sleep(5)

        if not connected:
            raise Exception("Konnte keine SSH-Verbindung zum Pod herstellen.")

        bootstrap_script = """
        set -e
        mkdir -p /workspace/.cache/huggingface
        export HF_HOME=/workspace/.cache/huggingface

        # Clone or update Wan2GP in persistent volume
        if [ ! -d "/workspace/Wan2GP" ]; then
            echo "[BOOTSTRAP] Klone Wan2GP nach /workspace/Wan2GP..."
            cd /workspace
            git clone https://github.com/deepbeepmeep/Wan2GP.git
            cd /workspace/Wan2GP
            pip install --no-cache-dir -r requirements.txt
        else
            echo "[BOOTSTRAP] Wan2GP bereits auf Volume vorhanden."
        fi

        # Triton Compatibility Patch for RunPod PyTorch Container:
        # Wan2GP uses do_not_specialize_on_alignment which is only in Triton 3.2+
        python3 -c "
import triton
p = triton.__file__
with open(p, 'r') as f:
    s = f.read()
if '_compatible_jit' not in s:
    wrapper = '''
_orig_jit = jit
def _compatible_jit(*args, **kwargs):
    kwargs.pop('do_not_specialize_on_alignment', None)
    return _orig_jit(*args, **kwargs)
jit = _compatible_jit
'''
    with open(p, 'a') as f:
        f.write('\\n' + wrapper + '\\n')
" 2>/dev/null || true

        # PyTorch Compatibility Patch for RunPod PyTorch < 2.5:
        # Wan2GP / mmaudio uses torch.nn.Buffer which was introduced in PyTorch 2.5
        python3 -c "
wgp_path = '/workspace/Wan2GP/wgp.py'
try:
    with open(wgp_path, 'r') as f:
        c = f.read()
    if 'class _Buffer' not in c:
        polyfill = '''# --- Compatibility Polyfill for torch.nn.Buffer (PyTorch < 2.5) ---
import torch
import torch.nn as nn
if not hasattr(nn, \\\"Buffer\\\"):
    class _Buffer(torch.Tensor):
        def __new__(cls, data=None, *, persistent=True):
            if data is None: data = torch.empty(0)
            t = data.detach().as_subclass(cls) if isinstance(data, torch.Tensor) else torch.tensor(data).as_subclass(cls)
            t.persistent = persistent
            return t
    nn.Buffer = _Buffer
    _orig_module_setattr = nn.Module.__setattr__
    def _compatible_module_setattr(self, name, value):
        if isinstance(value, _Buffer):
            self.register_buffer(name, value, persistent=getattr(value, \\\"persistent\\\", True))
        else:
            _orig_module_setattr(self, name, value)
    nn.Module.__setattr__ = _compatible_module_setattr
# ------------------------------------------------------------------
'''
        with open(wgp_path, 'w') as f:
            f.write(polyfill + '\\n' + c)
except Exception as e:
    print('Polyfill injection note:', e)
" 2>/dev/null || true

        mkdir -p /workspace/Wan2GP/outputs
        mkdir -p /workspace/Wan2GP/ckpts

        # Check if Wan2GP already running
        if pgrep -f "wgp.py" > /dev/null; then
            echo "[BOOTSTRAP] Wan2GP laeuft bereits."
        else
            echo "[BOOTSTRAP] Starte Wan2GP WebUI auf Port 7860..."
            cd /workspace/Wan2GP
            setsid python wgp.py --server-port 7860 --server-name 0.0.0.0 </dev/null > /workspace/wan2gp.log 2>&1 &
            sleep 4
        fi
        """
        code, out, err = ssh.exec_command(bootstrap_script, timeout=900)
        self.log(f"Bootstrap-Ausgabe:\n{out}")
        if code != 0:
            self.log(f"Bootstrap-Fehler (Code {code}):\n{err}", level="ERROR")
            return False

        # Readiness poll: verify that port 7860 responds
        self.log("Prüfe Erreichbarkeit der Wan2GP WebUI (Port 7860)...")
        for attempt in range(18):
            time.sleep(5)
            chk_code, chk_out, _ = ssh.exec_command("curl -sf -o /dev/null http://127.0.0.1:7860 && echo READY", timeout=10)
            if "READY" in chk_out:
                self.log("Wan2GP WebUI ist erreichbar und bereit!")
                return True
            self.log(f"Warte auf Wan2GP WebUI... ({attempt+1}/18)")

        self.log("Hinweis: Wan2GP braucht eventuell noch etwas Zeit zum Laden der Modellgewichte.", level="WARNING")
        return True

    def _background_loop(self):
        """Monitors session cost, auto-shutdown timer and syncs videos."""
        sync_counter = 0
        while not self._stop_bg_thread:
            time.sleep(2)
            try:
                cfg = load_config()
                now = time.time()

                with self.lock:
                    status = self.status

                if status != "RUNNING":
                    continue

                # Update live cost
                if self.session_start_time:
                    elapsed_hours = (now - self.session_start_time) / 3600.0
                    self.accumulated_cost = elapsed_hours * self.hourly_rate

                # Auto-Shutdown Check
                try:
                    auto_shut = int(cfg.get("auto_shutdown_minutes", 30) or 0)
                except (ValueError, TypeError):
                    auto_shut = 30

                if auto_shut > 0:
                    idle_seconds = now - self.last_activity_time
                    if idle_seconds > (auto_shut * 60):
                        if cfg.get("standby_mode", "stop") == "terminate":
                            self.log("Auto-Shutdown: Inaktivität erreicht! Terminiere Pod (Modus 'Löschen')...", level="WARNING")
                            self.terminate_pod()
                        else:
                            self.log(f"Auto-Shutdown: {auto_shut} Minuten Inaktivität erreicht! Pausiere GPU (Stop Pod)...", level="WARNING")
                            self.stop_pod()

                # Sync Videos every ~15 seconds
                sync_counter += 2
                try:
                    sync_interval = int(cfg.get("sync_interval_seconds", 15) or 15)
                except (ValueError, TypeError):
                    sync_interval = 15

                if sync_counter >= sync_interval and cfg.get("auto_sync_videos", True):
                    sync_counter = 0
                    if self.pod_ip and self.pod_ssh_port:
                        try:
                            ssh = SSHManager(self.pod_ip, self.pod_ssh_port, log_callback=self.log)
                            new_vids = ssh.sync_videos()
                            if new_vids:
                                self.last_activity_time = now
                                self.log(f"Neu synchronisierte Videos: {', '.join(new_vids)}")
                        except Exception as e:
                            pass
            except Exception as e:
                self.log(f"Hintergrund-Dienst Fehler (wird abgefangen): {e}", level="ERROR")
                time.sleep(5)

    def get_state(self) -> Dict[str, Any]:
        cfg = load_config()
        uptime_sec = 0
        if self.session_start_time and self.status == "RUNNING":
            uptime_sec = int(time.time() - self.session_start_time)

        return {
            "status": self.status,
            "status_detail": self.status_detail,
            "pod_id": self.pod_id,
            "pod_ip": self.pod_ip,
            "pod_ssh_port": self.pod_ssh_port,
            "proxy_url": self.proxy_url,
            "uptime_seconds": uptime_sec,
            "hourly_rate": round(self.hourly_rate, 2),
            "accumulated_cost": round(self.accumulated_cost, 4),
            "standby_mode": cfg.get("standby_mode", "stop"),
            "gpu_type_id": cfg.get("gpu_type_id", "NVIDIA GeForce RTX 4090"),
            "auto_shutdown_minutes": cfg.get("auto_shutdown_minutes", 30),
            "recent_logs": list(self.log_history)[-50:]
        }

    def delete_video(self, filename: str) -> bool:
        """Deletes a video file locally and remotely on the pod (if running) to prevent re-sync."""
        safe_name = os.path.basename(filename)
        deleted_anything = False

        # 1. Delete locally
        local_path = os.path.join(get_outputs_dir(), safe_name)
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
                deleted_anything = True
                self.log(f"Video lokal gelöscht: {safe_name}")
            except Exception as e:
                self.log(f"Fehler beim lokalen Löschen von {safe_name}: {e}", level="WARNING")

        # 2. Delete remotely if pod is running
        if self.pod_ip and self.pod_ssh_port and self.status == "RUNNING":
            try:
                ssh = SSHManager(self.pod_ip, self.pod_ssh_port)
                remote_escaped = shlex.quote(f"/workspace/Wan2GP/outputs/{safe_name}")
                code, _, _ = ssh.exec_command(f"rm -f {remote_escaped}")
                if code == 0:
                    deleted_anything = True
                self.log(f"Video auch auf dem Pod gelöscht: {safe_name}")
            except Exception as e:
                self.log(f"Hinweis: Konnte Video auf Pod nicht löschen: {e}", level="WARNING")

        return deleted_anything

