import os
import time
import threading
import shlex
import math
from typing import Dict, Any, List, Optional
from collections import deque
from pathlib import Path

from backend.config_manager import load_config, update_config, get_outputs_dir
from backend.runpod_client import RunPodClient
from backend.ssh_client import SSHManager, get_or_create_ssh_keys, get_verified_identity, remove_name_mapping


class PodPreflightError(Exception):
    """Der zugewiesene Pod-Host ist vor dem Setup als unbrauchbar erkannt
    (CUDA-Treiber, SSH oder Netzwerk-Geschwindigkeit).

    Der Start-Worker terminiert so einen Pod sofort - RunPod berechnet ihn
    sonst weiter, obwohl er nichts leisten kann - und bucht automatisch neu.
    `component` ist "cuda", "ssh" oder "network" (nur für Logs/UI)."""

    def __init__(self, component: str, message: str):
        super().__init__(message)
        self.component = component


class PodOrchestrator:
    # Wie lange eine per fehlgeschlagener Buchung erkannte "ausgebucht"-Markierung gilt.
    # Fallback, wenn RunPods Live-Abfrage keinen Bestand liefert. Frische
    # Bestandsdaten haben Vorrang; alte Buchungsfehler verfallen automatisch.
    GPU_OUT_OF_STOCK_TTL_SECONDS = 15 * 60

    # Netzwerk-Preflight: 20 MB Range-Request gegen eine bekannte, öffentliche
    # HuggingFace-Datei. Die Modell-Downloads (30+ GB) dominieren die Setup-Zeit;
    # ein Host mit toter/lahmer Leitung ist vorher billig zu erkennen.
    NET_PREFLIGHT_URL = "https://huggingface.co/openai-community/gpt2/resolve/main/model.safetensors"
    NET_PREFLIGHT_BYTES = 20 * 1024 * 1024

    def __init__(self):
        # Reentrant: Lifecycle-Methoden (start/stop/terminate) buchen unter
        # gehaltenem Lock die Session-Kosten (_book_session_cost -> log());
        # ein einfacher Lock würde dort gegenseitig blockieren.
        self.lock = threading.RLock()
        self.log_history = deque(maxlen=300)
        self.listeners = set()
        self.gpu_out_of_stock: Dict[str, float] = {}

        self.status = "OFFLINE"
        self.status_detail = "Bereit"
        self.pod_id: Optional[str] = None
        self.pod_ip: Optional[str] = None
        self.pod_ssh_port: Optional[int] = None
        self.proxy_url: Optional[str] = None
        
        self.session_start_time: Optional[float] = None
        self.hourly_rate: Optional[float] = None
        self._hourly_rate_pod_id: Optional[str] = None
        self._operation_id = 0
        self._sync_in_progress = False
        self.startup_progress = {
            "phase": "cancelled", "label": "Kein Start aktiv", "detail": "",
            "started_at": None, "updated_at": None, "active": False,
        }
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
        if not self.pod_id and self.status not in ("OFFLINE", "TERMINATED"):
            # Older startup workers could persist ERROR after a pod was deleted.
            self.status = "OFFLINE"
            update_config({"active_pod_status": "OFFLINE"})

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

    def _operation_current(self, operation_id, pod_id=None):
        with self.lock:
            return (operation_id == self._operation_id
                    and (pod_id is None or pod_id == self.pod_id))

    def _set_startup_progress(self, phase, label, detail="", operation_id=None):
        with self.lock:
            if operation_id is not None and operation_id != self._operation_id:
                return
            active = phase in ("booking", "container", "ssh", "install", "launch")
            previous = getattr(self, "startup_progress", {})
            now = time.time()
            self.startup_progress = {
                "phase": phase, "label": label, "detail": detail[-500:],
                "started_at": previous.get("started_at") or (now if active else None),
                "updated_at": now, "active": active,
            }
            # Fortschritt ist Aktivität: schützt laufende Installationen vor dem
            # Inaktivitäts-Auto-Shutdown (auch für spätere ERROR-Zustände).
            if active and hasattr(self, "last_activity_time"):
                self.last_activity_time = now

    def _refresh_pod_rate(self, pod_id, pod_info, operation_id=None):
        """Only accept a finite rate reported for this pod and this operation."""
        value = pod_info.get("costPerHr")
        try:
            rate = float(value) if value is not None and not isinstance(value, bool) else None
        except (ValueError, TypeError, OverflowError):
            rate = None
        if rate is None or not math.isfinite(rate) or rate < 0:
            return False
        with self.lock:
            if (not pod_id or self.pod_id != pod_id
                    or (operation_id is not None and operation_id != self._operation_id)):
                return False
            self.hourly_rate = rate
            self._hourly_rate_pod_id = pod_id
        return True

    @staticmethod
    def is_out_of_stock_error(message: str) -> bool:
        m = (message or "").lower()
        return ("instances available" in m
                or "sold out" in m
                or "ausgebucht" in m)

    def mark_gpu_unavailable(self, gpu_type_id: str):
        if not gpu_type_id:
            return
        with self.lock:
            self.gpu_out_of_stock[gpu_type_id] = time.time()
        ttl_min = self.GPU_OUT_OF_STOCK_TTL_SECONDS // 60
        self.log(f"'{gpu_type_id}' ist aktuell ausgebucht (Markierung läuft nach {ttl_min} Min. ab).", level="WARNING")

    def get_gpu_availability(self) -> Dict[str, bool]:
        """Returns {gpu_type_id: False} for GPUs with a live out-of-stock mark."""
        now = time.time()
        with self.lock:
            expired = [g for g, ts in self.gpu_out_of_stock.items()
                       if now - ts > self.GPU_OUT_OF_STOCK_TTL_SECONDS]
            for g in expired:
                del self.gpu_out_of_stock[g]
            return {g: False for g in self.gpu_out_of_stock}

    def reset_gpu_availability(self):
        with self.lock:
            self.gpu_out_of_stock.clear()
        self.log("Verfügbarkeits-Markierungen zurückgesetzt.")

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
        with self.lock:
            pod_id, operation_id = self.pod_id, self._operation_id
        if not pod_id:
            return
        try:
            client = self.get_client()
            pod = client.get_pod(pod_id)
            if not self._operation_current(operation_id, pod_id):
                return
            if not pod:
                self.log(f"Persistierter Pod {self.pod_id} existiert auf RunPod nicht mehr. Bereinige Status...", level="WARNING")
                with self.lock:
                    if operation_id != self._operation_id or self.pod_id != pod_id:
                        return
                    self.pod_id = None
                    self.hourly_rate = None
                    self._hourly_rate_pod_id = None
                    self.status = "OFFLINE"
                    self.status_detail = "Bereit"
                    update_config({"active_pod_id": None, "active_pod_status": "OFFLINE"})
                return

            desired = pod.get("desiredStatus")
            if desired == "RUNNING":
                self._refresh_pod_rate(pod_id, pod, operation_id)
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
                    if operation_id != self._operation_id or self.pod_id != pod_id:
                        return
                    self.status = "RUNNING"
                    self.status_detail = "Prüfe Wan2GP-WebUI im Pod..."
                    self.pod_ip = ip
                    self.pod_ssh_port = int(ssh_port) if ssh_port else None
                    self.proxy_url = RunPodClient.get_proxy_url(self.pod_id, 7860)
                    uptime = runtime.get("uptimeInSeconds") or 0
                    self.session_start_time = time.time() - uptime
                    self.last_activity_time = time.time()
                    update_config({"active_pod_status": "RUNNING"})
                self.log(f"Zustand nach Server-Neustart wiederhergestellt: Pod {self.pod_id} läuft (SSH: {ip}:{ssh_port})")

                # wgp.py überlebt keinen Container-Neustart - nach einem Server-
                # Neustart muss die WebUI explizit geprüft und ggf. neu gestartet
                # werden, sonst meldet die App fälschlich "aktiv & verbunden".
                if ip and ssh_port and self._operation_current(operation_id, pod_id):
                    self._ensure_webui_after_reconcile(ip, int(ssh_port))
                elif self._operation_current(operation_id, pod_id):
                    # A dashboard restart during provisioning must keep waiting
                    # instead of claiming that a not-yet-reachable WebUI is ready.
                    with self.lock:
                        if operation_id != self._operation_id or self.pod_id != pod_id:
                            return
                        self.status = "STARTING"
                        self.status_detail = "Warte auf Container-Initialisierung..."
                    self._deploy_or_resume_worker(operation_id)

            elif desired == "EXITED":
                with self.lock:
                    if operation_id != self._operation_id or self.pod_id != pod_id:
                        return
                    self.status = "STOPPED"
                    self.status_detail = "GPU pausiert (Standby)"
                    self.pod_ip = None
                    self.pod_ssh_port = None
                    self.hourly_rate = None
                    self._hourly_rate_pod_id = None
                    update_config({"active_pod_status": "STOPPED"})
                self.log(f"Zustand nach Server-Neustart wiederhergestellt: Pod {self.pod_id} ist im Standby.")

            elif desired == "TERMINATED":
                with self.lock:
                    if operation_id != self._operation_id or self.pod_id != pod_id:
                        return
                    self.pod_id = None
                    self.hourly_rate = None
                    self._hourly_rate_pod_id = None
                    self.status = "OFFLINE"
                    self.status_detail = "Bereit"
                    update_config({"active_pod_id": None, "active_pod_status": "OFFLINE"})
        except Exception as e:
            with self.lock:
                if operation_id != self._operation_id or self.pod_id != pod_id:
                    return
                self.status = "ERROR"
                self.status_detail = "Pod-Status konnte nicht geprüft werden - API-Verbindung prüfen und erneut starten."
                self.startup_progress.update(
                    phase="error", label="Pod-Status prüfen", detail=self.status_detail,
                    active=False, updated_at=time.time(),
                )
                update_config({"active_pod_status": "ERROR"})
            self.log(f"Status-Reconciliation fehlgeschlagen ({type(e).__name__}); vorhandener Pod bleibt gespeichert.", level="WARNING")

    def _book_session_cost(self):
        """Bucht die aufgelaufenen Session-Kosten endgültig (spent_total, persistent).

        Ohne diese Verbuchung würde das Guthaben beim nächsten Session-Start die
        ausgegebene GPU-Zeit vergessen (accumulated_cost wird dort zurückgesetzt).
        """
        accumulated = float(self.accumulated_cost or 0.0)
        if accumulated <= 0:
            return
        cfg = load_config()
        spent = float(cfg.get("spent_total", 0.0) or 0.0) + accumulated
        update_config({"spent_total": round(spent, 4)})
        self.accumulated_cost = 0.0
        self.log(f"Session-Kosten verbucht: ${accumulated:.4f} (gesamt ausgegeben: ${spent:.2f})")

    def start_pod(self) -> Dict[str, Any]:
        with self.lock:
            if self.status in ["RUNNING", "STARTING", "BOOTSTRAPPING"]:
                return {"success": False, "message": f"Pod befindet sich bereits im Status {self.status}"}
            # Restkosten eines evtl. unsanft beendeten Sessions auch verbuchen
            self._book_session_cost()
            self.status = "STARTING"
            self._operation_id += 1
            self._preflight_retries = 0
            self._fresh_pod_op_id = None
            operation_id = self._operation_id
            self.status_detail = "Initialisiere Pod..."
            self.last_activity_time = time.time()
            self.session_start_time = None
            self.accumulated_cost = 0.0
            self.hourly_rate = None
            self._hourly_rate_pod_id = None
            self.pod_ip = None
            self.pod_ssh_port = None
            self.proxy_url = None
            now = time.time()
            self.startup_progress = {
                "phase": "booking", "label": "GPU wird angefragt",
                "detail": "RunPod weist eine passende Instanz zu.",
                "started_at": now, "updated_at": now, "active": True,
            }

        threading.Thread(target=self._deploy_or_resume_worker, args=(operation_id,), daemon=True).start()
        return {"success": True, "message": "Startvorgang eingeleitet"}

    def stop_pod(self) -> Dict[str, Any]:
        """Pauses the pod (GPU stops, storage preserved)."""
        cfg = load_config()
        pod_id = self.pod_id or cfg.get("active_pod_id")
        if not pod_id:
            return {"success": False, "message": "Kein aktiver Pod vorhanden."}

        with self.lock:
            self._operation_id += 1
            operation_id = self._operation_id
        client = self.get_client()
        self.log(f"Pausiere Pod {pod_id} (GPU wird gestoppt, Volume bleibt erhalten)...")
        try:
            client.stop_pod(pod_id)
            with self.lock:
                if operation_id != self._operation_id or self.pod_id != pod_id:
                    return {"success": True, "message": "Pause ausgeführt; neueren Zustand beibehalten"}
                self.status = "STOPPED"
                self.status_detail = "Pod pausiert (Speicher erhalten)"
                self._book_session_cost()
                self.session_start_time = None
                self.hourly_rate = None
                self._hourly_rate_pod_id = None
                self.startup_progress.update(phase="cancelled", label="Start abgebrochen",
                                             detail="Pod wurde pausiert.", active=False, updated_at=time.time())
                update_config({"active_pod_status": "STOPPED"})
            self.log("Pod erfolgreich pausiert. GPU-Kosten gestoppt.")
            return {"success": True, "message": "Pod pausiert"}
        except Exception as e:
            with self.lock:
                if operation_id == self._operation_id:
                    self.status = "ERROR"
                    self.status_detail = "Pausieren fehlgeschlagen - Pod-Status prüfen"
                    self.startup_progress.update(phase="error", label="Pausieren fehlgeschlagen",
                                                 detail=self.status_detail, active=False, updated_at=time.time())
                    update_config({"active_pod_status": "ERROR"})
            self.log(f"Fehler beim Pausieren: {e}", level="ERROR")
            return {"success": False, "message": str(e)}

    def terminate_pod(self) -> Dict[str, Any]:
        """Terminates the pod completely (Storage and GPU destroyed -> 0€)."""
        cfg = load_config()
        pod_id = self.pod_id or cfg.get("active_pod_id")
        if not pod_id:
            return {"success": False, "message": "Kein aktiver Pod vorhanden."}

        with self.lock:
            self._operation_id += 1
            operation_id = self._operation_id
        client = self.get_client()
        self.log(f"Terminiere Pod {pod_id} vollständig (0 € Standby-Kosten)...", level="WARNING")
        try:
            client.terminate_pod(pod_id)
            with self.lock:
                if operation_id != self._operation_id or self.pod_id != pod_id:
                    return {"success": True, "message": "Löschen ausgeführt; neueren Zustand beibehalten"}
                self.status = "TERMINATED"
                self.status_detail = "Pod gelöscht (0,00 € Standby)"
                self._book_session_cost()
                self.pod_id = None
                self.pod_ip = None
                self.pod_ssh_port = None
                self.proxy_url = None
                self.hourly_rate = None
                self._hourly_rate_pod_id = None
                self.session_start_time = None
                self.accumulated_cost = 0.0
                self.startup_progress.update(phase="cancelled", label="Start abgebrochen",
                                             detail="Pod wurde gelöscht.", active=False, updated_at=time.time())
                update_config({"active_pod_id": None, "active_pod_status": "TERMINATED"})
            self.log("Pod erfolgreich terminiert. Keine weiteren Kosten.")
            return {"success": True, "message": "Pod terminiert"}
        except Exception as e:
            with self.lock:
                if operation_id == self._operation_id:
                    self.status = "ERROR"
                    self.status_detail = "Löschen fehlgeschlagen - Pod-Status prüfen"
                    self.startup_progress.update(phase="error", label="Löschen fehlgeschlagen",
                                                 detail=self.status_detail, active=False, updated_at=time.time())
                    update_config({"active_pod_status": "ERROR"})
            self.log(f"Fehler beim Terminieren: {e}", level="ERROR")
            return {"success": False, "message": str(e)}

    def _cleanup_orphan_pod(self, client, pod_id: str):
        """Räumt einen Pod auf, den eine ersetzte/abgebrochene Operation erstellt hat.

        Gelingt das nicht, wird die ID persistent in `orphan_pod_ids` vermerkt
        und laut gemeldet, damit sie manuell bei RunPod gelöscht werden kann."""
        try:
            client.terminate_pod(pod_id)
            self.log(f"Verwaisten Pod {pod_id} (Operation wurde abgelöst) terminiert.", level="WARNING")
            return
        except Exception as e:
            self.log(f"ACHTUNG: Verwaister Pod {pod_id} konnte nicht terminiert werden "
                     f"({e}). Bitte in RunPod manuell löschen - er kostet sonst weiter Geld!", level="ERROR")
        try:
            cfg = load_config()
            orphans = list(cfg.get("orphan_pod_ids", []) or [])
            if pod_id not in orphans:
                orphans.append(pod_id)
                update_config({"orphan_pod_ids": orphans})
        except Exception as e:
            self.log(f"Verwaiste Pod-ID {pod_id} konnte nicht persistiert werden: {e}", level="ERROR")

    def _preflight_network_speed(self, ssh) -> Optional[float]:
        """Misst die Download-Rate (Bytes/s) zu HuggingFace per 20-MB-Range-Request.

        Rückgabe None = Messung nicht möglich (SSH-Fehler, Curl-Fehler wie
        DNS/TLS/Timeout, unlesbare oder ungültige Ausgabe); der Host wird dann
        NICHT verworfen, damit ein Messproblem keinen funktionierenden Pod
        kostet. Nur eine erfolgreich durchgeführte Messung liefert einen Wert."""
        # Curl-Exitcode und Messwert getrennt einsammeln: Wuerde der Exitcode
        # (wie bei einem echo-Wrapper) verloren gehen, erschiene z. B. ein
        # Timeout-Resttransfer als "erfolgreiche Messung" mit Mini-Rate.
        command = (
            'speed=$(curl --fail -sL -o /dev/null -w "%{speed_download}" '
            f"--max-time 25 -r 0-{self.NET_PREFLIGHT_BYTES} {shlex.quote(self.NET_PREFLIGHT_URL)} 2>/dev/null); "
            'rc=$?; echo "VG_NET_SPEED_RC=$rc VG_NET_SPEED=$speed"'
        )
        try:
            code, out, _err = ssh.exec_command(command, timeout=35)
        except Exception as e:
            self.log(f"Netzwerk-Preflight nicht ausführbar ({type(e).__name__}); Prüfung übersprungen.", level="WARNING")
            return None
        if code != 0:
            self.log(f"Netzwerk-Preflight mit Exit-Code {code} abgebrochen; Prüfung übersprungen.", level="WARNING")
            return None
        rc, raw = None, None
        for token in out.split():
            if token.startswith("VG_NET_SPEED_RC="):
                try:
                    rc = int(token.split("=", 1)[1])
                except ValueError:
                    rc = None
            elif token.startswith("VG_NET_SPEED="):
                raw = token.split("=", 1)[1].strip().strip('"').strip("'")
        if rc is None or rc != 0:
            return None
        try:
            value = float(raw) if raw else None
        except ValueError:
            return None
        if value is None or not math.isfinite(value) or value < 0:
            return None
        return value

    def _handle_preflight_failure(self, operation_id: int, err: PodPreflightError):
        """Reaktion auf einen im Preflight erkannten defekten Host.

        Frisch gebuchte Pods: sofort terminieren (RunPod berechnet ERROR-Pods
        weiter) und automatisch neu buchen - maximal preflight_max_retries Mal.
        Bestands-Pods (aufgeweckt aus Standby oder wiederverwendet): Volume mit
        den Modellen und Pod-ID unangetastet lassen, nur datenerhaltend pausieren
        und den Fehler anzeigen - ein transienter SSH-/Netzfehler darf niemals
        den gespeicherten Pod samt Daten löschen."""
        detail = str(err)[-500:]
        self.log(f"Preflight fehlgeschlagen ({err.component}): {detail}", level="ERROR")

        with self.lock:
            if operation_id != self._operation_id:
                return
            pod_id = self.pod_id or None
            fresh_pod = pod_id is not None and getattr(self, "_fresh_pod_op_id", None) == operation_id
        client = self.get_client()

        if pod_id and not fresh_pod:
            stopped = False
            try:
                client.stop_pod(pod_id)
                stopped = True
                self.log(f"Bestands-Pod {pod_id} datenerhaltend pausiert (keine GPU-Kosten, "
                         "Volume bleibt erhalten).", level="WARNING")
            except Exception as se:
                self.log(f"Bestands-Pod {pod_id} konnte nicht pausiert werden: {se}", level="ERROR")
            hint = (f"Preflight fehlgeschlagen ({err.component}), Bestands-Pod bleibt erhalten"
                    f"{' und ist pausiert' if stopped else ''}: {detail} "
                    "Pod ggf. später erneut aufwecken oder manuell prüfen.")
            with self.lock:
                if operation_id != self._operation_id or self.pod_id != pod_id:
                    return
                self.status = "STOPPED" if stopped else "ERROR"
                if stopped:
                    self._book_session_cost()
                    self.session_start_time = None
                    self.hourly_rate = None
                    self._hourly_rate_pod_id = None
                self.status_detail = hint
                self.startup_progress.update(phase="error", label="Preflight – fehlgeschlagen",
                                             detail=hint[-500:], active=False, updated_at=time.time())
                update_config({"active_pod_status": self.status})
            self.log(hint, level="ERROR")
            return

        if pod_id:
            try:
                client.terminate_pod(pod_id)
                self.log(f"Defekten Pod {pod_id} automatisch terminiert - keine weiteren Kosten.", level="WARNING")
            except Exception as te:
                # Ohne Terminieren läuft der Pod weiter; dann den Nutzer
                # entscheiden lassen statt blind neu zu buchen (Doppelkosten).
                self.log(f"Pod {pod_id} konnte NICHT automatisch terminiert werden: {te}", level="ERROR")
                with self.lock:
                    if operation_id != self._operation_id:
                        return
                    self.status = "ERROR"
                    self.status_detail = ("Preflight fehlgeschlagen und Pod konnte nicht automatisch "
                                          "gelöscht werden - bitte in RunPod prüfen.")
                    self.startup_progress.update(phase="error", label="Preflight – fehlgeschlagen",
                                                 detail=self.status_detail, active=False, updated_at=time.time())
                    update_config({"active_pod_status": "ERROR"})
                return
            with self.lock:
                if operation_id != self._operation_id:
                    return
                self.pod_id = None
                self.pod_ip = None
                self.pod_ssh_port = None
                self.proxy_url = None
            update_config({"active_pod_id": None})

        self._preflight_retries = getattr(self, "_preflight_retries", 0) + 1
        max_retries = int(load_config().get("preflight_max_retries", 2))
        if self._preflight_retries > max_retries:
            hint = (f"{self._preflight_retries - 1} Pods nacheinander mit defekter Hardware oder "
                    "Netzwerk. Tipp: Secure Cloud in den Einstellungen wählen (zuverlässige Hosts, "
                    "etwas teurer) oder später erneut versuchen.")
            self.log(hint, level="ERROR")
            with self.lock:
                if operation_id != self._operation_id:
                    return
                self.status = "ERROR"
                self.status_detail = hint
                self.startup_progress.update(phase="error", label="Preflight – fehlgeschlagen",
                                             detail=hint, active=False, updated_at=time.time())
                update_config({"active_pod_id": None, "active_pod_status": "ERROR"})
            return

        attempt = self._preflight_retries
        with self.lock:
            if operation_id != self._operation_id:
                return
            self.status_detail = f"Host defekt ({err.component}) - buche neu (Versuch {attempt + 1})..."
            self.startup_progress.update(phase="booking", label=f"Neuer Buchungsversuch ({attempt + 1})",
                                         detail=detail, active=True, updated_at=time.time())
            update_config({"active_pod_status": "STARTING"})
        self.log(f"Buche Ersatz-Pod (Versuch {attempt + 1} von {max_retries + 1})...", level="WARNING")
        self._deploy_or_resume_worker(operation_id)

    def _deploy_or_resume_worker(self, operation_id=None):
        if operation_id is None:
            with self.lock:
                operation_id = self._operation_id
        if not self._operation_current(operation_id):
            return
        cfg = load_config()
        client = self.get_client()
        _, pub_key = get_or_create_ssh_keys()
        
        pod_id = self.pod_id or cfg.get("active_pod_id")

        # Check if existing stopped pod can be resumed or if it is already running
        can_resume = False
        already_running = False
        if pod_id:
            existing_pod_error = None
            try:
                pod_info = client.get_pod(pod_id)
                if not self._operation_current(operation_id):
                    return
                if pod_info is not None:
                    desired = pod_info.get("desiredStatus")
                    if desired == "EXITED":
                        can_resume = True
                    elif desired == "RUNNING":
                        already_running = True
                    else:
                        existing_pod_error = (
                            "Vorhandener Pod ist weder eindeutig RUNNING noch EXITED. "
                            "RunPod-Status prüfen; es wurde kein neuer Pod gebucht."
                        )
            except Exception as e:
                # An unavailable status endpoint is not proof that the pod is
                # gone. Never buy a replacement while the existing pod is unknown.
                # Network exception text can contain the API key in its URL.
                existing_pod_error = (
                    f"Status des vorhandenen Pods konnte nicht abgefragt werden ({type(e).__name__}). "
                    "API-Verbindung prüfen; es wurde kein neuer Pod gebucht."
                )
            if existing_pod_error:
                with self.lock:
                    if operation_id != self._operation_id:
                        return
                    self.status = "ERROR"
                    self.status_detail = existing_pod_error
                    self.startup_progress.update(
                        phase="error", label="Vorhandenen Pod prüfen",
                        detail=existing_pod_error, active=False, updated_at=time.time(),
                    )
                    update_config({"active_pod_status": "ERROR"})
                self.log(existing_pod_error, level="ERROR")
                return

        try:
            if not self._operation_current(operation_id):
                return
            if already_running:
                self.log(f"Pod {pod_id} existiert und läuft bereits. Verwende ihn weiter...")
                with self.lock:
                    # Bestands-Pod: Preflight-Fehler dürfen ihn nie terminieren.
                    self._fresh_pod_op_id = None
            elif can_resume:
                self.log(f"Reaktiviere bestehenden Pod {pod_id}...")
                client.resume_pod(pod_id)
                with self.lock:
                    self._fresh_pod_op_id = None
            else:
                gpu_type = cfg.get("gpu_type_id", "NVIDIA A40")
                cloud_type = cfg.get("cloud_type", "ALL")
                vol_gb = int(cfg.get("volume_disk_gb", 75))
                cont_gb = int(cfg.get("container_disk_gb", 30))

                # RunPod stellt keinen Bestand bereit - der Buchungsversuch ist
                # die einzige Pruefung. Bei "ausgebucht" automatisch die naechste
                # guenstige Karte versuchen, statt den Start fehlschlagen zu lassen.
                candidates = [gpu_type]
                seen = {gpu_type}
                try:
                    unavailable = self.get_gpu_availability()
                    others = [g for g in client.get_gpu_types()
                              if g.get("memoryInGb", 0) >= 24 and g["id"] not in seen
                              and g["id"] not in unavailable]
                    others.sort(key=lambda g: g.get("communityPrice") if g.get("communityPrice") is not None else 99)
                    for g in others[:2]:
                        candidates.append(g["id"])
                        seen.add(g["id"])
                except Exception as e:
                    self.log(f"GPU-Liste fuer Ausweichkandidaten nicht abrufbar ({type(e).__name__}).", level="WARNING")

                deployed = False
                last_err: Optional[Exception] = None
                for cand in candidates:
                    if not self._operation_current(operation_id):
                        return
                    if cand != gpu_type:
                        self.log(f"GPU '{gpu_type}' ist ausgebucht - weiche automatisch aus auf '{cand}'...", level="WARNING")
                    try:
                        self.log(f"Erstelle neuen Pod auf RunPod mit GPU '{cand}' (Volume: {vol_gb} GB)...")
                        new_pod_id = client.deploy_pod(
                            name="wan2gp-videogen-studio",
                            gpu_type_id=cand,
                            volume_in_gb=vol_gb,
                            container_disk_in_gb=cont_gb,
                            cloud_type=cloud_type,
                            public_ssh_key=pub_key
                        )
                        with self.lock:
                            if operation_id != self._operation_id:
                                # P1: Die Buchung lief durch, die Operation wurde
                                # inzwischen ersetzt. Der neue Pod gehört niemandem
                                # mehr und würde unbemerkt Kosten erzeugen - jetzt
                                # gezielt aufräumen, den aktuelleren Zustand nicht
                                # anfassen.
                                stale_pod_id = new_pod_id
                            else:
                                stale_pod_id = None
                                self.pod_id = new_pod_id
                                # Frisch gebucht: Nur solche Pods darf der Preflight
                                # automatisch verwerfen und ersetzen.
                                self._fresh_pod_op_id = operation_id
                        if stale_pod_id:
                            self._cleanup_orphan_pod(client, stale_pod_id)
                            return
                        pod_id = new_pod_id
                        update_config({"active_pod_id": pod_id, "active_pod_status": "STARTING",
                                       "gpu_type_id": cand})
                        self.log(f"Pod erfolgreich gebucht! Pod-ID: {pod_id} (GPU: {cand})")
                        deployed = True
                        break
                    except Exception as e:
                        last_err = e
                        if self.is_out_of_stock_error(str(e)):
                            self.mark_gpu_unavailable(cand)
                            continue
                        raise
                if not deployed:
                    raise last_err or Exception("Keine GPU konnte gebucht werden.")

            if not self._operation_current(operation_id, pod_id):
                return
            # Wait for RUNNING and SSH port mapping
            self._set_startup_progress("container", "Container wird bereitgestellt",
                                       "Warte auf öffentliche IP und SSH-Port von RunPod.", operation_id)
            self.log("Warte auf Zuweisung von IP und SSH-Port durch RunPod...")
            ip, ssh_port = None, None
            pod_info = None
            for attempt in range(60):
                if not self._operation_current(operation_id, pod_id):
                    return
                # Fetch the booked rate immediately; SSH allocation can take minutes.
                pod_info = client.get_pod(pod_id)
                if not self._operation_current(operation_id, pod_id):
                    return
                if pod_info:
                    self._refresh_pod_rate(pod_id, pod_info, operation_id)
                if not pod_info:
                    time.sleep(5)
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
                if (attempt + 1) % 6 == 0:
                    self.log(f"Warte auf Container-Initialisierung... ({attempt+1}/60)")
                self._set_startup_progress("container", "Container wird bereitgestellt",
                                           f"RunPod antwortet; SSH ist noch nicht bereit (Abfrage {attempt + 1}).", operation_id)
                time.sleep(5)

            if not ip or not ssh_port:
                raise PodPreflightError(
                    "ssh", "Timeout beim Warten auf öffentliche IP / SSH-Port des Containers "
                           "(Host startet den Container nicht).")

            with self.lock:
                if operation_id != self._operation_id or self.pod_id != pod_id:
                    return
                self.pod_ip = ip
                self.pod_ssh_port = int(ssh_port)
                self.proxy_url = RunPodClient.get_proxy_url(pod_id, 7860)
                self.status = "BOOTSTRAPPING"
                self.status_detail = "Richte Wan2GP & Modell-Umgebung ein..."

            self.log(f"Pod läuft! SSH: {ip}:{ssh_port} | Proxy: {self.proxy_url}")
            
            # Bootstrap Wan2GP via SSH
            bootstrap_ok = self._bootstrap_wan2gp(ip, int(ssh_port))
            if not self._operation_current(operation_id, pod_id):
                return
            if not bootstrap_ok:
                with self.lock:
                    if operation_id != self._operation_id or self.pod_id != pod_id:
                        return
                    self.status = "ERROR"
                    self.status_detail = "Bootstrap fehlgeschlagen - Terminal prüfen"
                    failed_label = self.startup_progress.get("label", "Einrichtung")
                    last_detail = self.startup_progress.get("detail") or self.status_detail
                    self.startup_progress.update(phase="error", label=f"{failed_label} – fehlgeschlagen",
                                                 detail=last_detail, active=False, updated_at=time.time())
                    update_config({"active_pod_status": "ERROR"})
                return

            with self.lock:
                if operation_id != self._operation_id or self.pod_id != pod_id:
                    return
                self.status = "RUNNING"
                self.status_detail = "Wan2GP aktiv & bereit"
                self.session_start_time = time.time()
                self.last_activity_time = time.time()
                self.startup_progress.update(phase="ready", label="WanGP-WebUI bereit",
                                             detail="WebUI und Browser-Verbindung erfolgreich geprüft.",
                                             active=False, updated_at=time.time())
                update_config({"active_pod_status": "RUNNING"})
            self.log(f"Wan2GP erfolgreich gestartet! Bereit unter {self.proxy_url}")

        except PodPreflightError as e:
            if not self._operation_current(operation_id):
                return
            self._handle_preflight_failure(operation_id, e)
        except Exception as e:
            if not self._operation_current(operation_id):
                return
            err_msg = str(e)
            self.log(f"Fehler beim Starten des Pods: {err_msg}", level="ERROR")
            friendly_hint = err_msg
            if self.is_out_of_stock_error(err_msg):
                self.mark_gpu_unavailable(cfg.get("gpu_type_id", ""))
                friendly_hint = "Die gewählte GPU ist gerade ausgebucht. Vergleiche aktuelle Preise und Verfügbarkeit in der GPU-Auswahl."
                self.log(friendly_hint, level="WARNING")
            with self.lock:
                if operation_id != self._operation_id:
                    return
                self.status = "ERROR"
                self.status_detail = friendly_hint
                failed_label = self.startup_progress.get("label", "Start")
                self.startup_progress.update(phase="error", label=f"{failed_label} – fehlgeschlagen",
                                             detail=friendly_hint[-500:], active=False, updated_at=time.time())
                update_config({"active_pod_status": "ERROR"})

    def _webui_probe_command(self):
        command = "curl -sf -o /dev/null -m 5 http://127.0.0.1:7860"
        if self.proxy_url:
            # An empty queue request must fail validation (422), not the
            # browser origin check (403). No job can be created without data.
            command += (
                " && [ \"$(curl -s -o /dev/null -m 5 -w '%{http_code}'"
                " -H 'Content-Type: application/json'"
                f" -H {shlex.quote('Origin: ' + self.proxy_url)}"
                " -X POST --data '{}' http://127.0.0.1:7860/gradio_api/queue/join)\" = 422 ]"
            )
        return command + " && echo READY"

    def _ensure_webui_after_reconcile(self, ip: str, ssh_port: int):
        """Prüft nach einem Server-Neustart, ob die Wan2GP-WebUI im Pod antwortet,
        und startet andernfalls den Bootstrap (Patches + wgp.py) neu."""
        operation_id = getattr(self, "_operation_id", None)
        pod_id = getattr(self, "pod_id", None)
        def cancelled():
            return operation_id is not None and not self._operation_current(operation_id, pod_id)

        try:
            ssh = SSHManager(ip, ssh_port, log_callback=self.log)
            for attempt in range(6):
                if cancelled():
                    return
                try:
                    ssh.get_client(timeout=10).close()
                    break
                except Exception:
                    time.sleep(5)

            code, out, _ = ssh.exec_command(self._webui_probe_command(), timeout=15)
            if cancelled():
                return
            if code == 0 and "READY" in out.splitlines():
                with self.lock:
                    if operation_id is not None and (operation_id != self._operation_id or self.pod_id != pod_id):
                        return
                    self.status_detail = "Wan2GP aktiv & verbunden"
                    self.status = "RUNNING"
                    if hasattr(self, "startup_progress"):
                        self.startup_progress.update(phase="ready", label="WanGP-WebUI bereit", active=False, updated_at=time.time())
                self.log("Wan2GP-WebUI antwortet - nichts zu tun.")
                return

            with self.lock:
                if operation_id is not None and (operation_id != self._operation_id or self.pod_id != pod_id):
                    return
                self.status = "BOOTSTRAPPING"
                self.status_detail = "WanGP wird wiederhergestellt..."
            self.log("Wan2GP-WebUI antwortet nicht - starte Wan2GP im Pod neu...", level="WARNING")
            bootstrap_ok = self._bootstrap_wan2gp(ip, ssh_port)
            if cancelled():
                return
            if bootstrap_ok:
                with self.lock:
                    if operation_id is not None and (operation_id != self._operation_id or self.pod_id != pod_id):
                        return
                    self.status_detail = "Wan2GP aktiv & bereit"
                    self.status = "RUNNING"
                    if hasattr(self, "startup_progress"):
                        self.startup_progress.update(phase="ready", label="WanGP-WebUI bereit", active=False, updated_at=time.time())
                    update_config({"active_pod_status": "RUNNING"})
                self.log(f"Wan2GP-WebUI wiederhergestellt: {self.proxy_url}")
            else:
                with self.lock:
                    if operation_id is not None and (operation_id != self._operation_id or self.pod_id != pod_id):
                        return
                    self.status_detail = "WebUI-Restart fehlgeschlagen - Logs prüfen"
                    self.status = "ERROR"
                    if hasattr(self, "startup_progress"):
                        failed_label = self.startup_progress.get("label", "WebUI")
                        self.startup_progress.update(phase="error", label=f"{failed_label} – fehlgeschlagen", active=False, updated_at=time.time())
                    update_config({"active_pod_status": "ERROR"})
        except Exception as e:
            if cancelled():
                return
            with self.lock:
                if operation_id is not None and (operation_id != self._operation_id or self.pod_id != pod_id):
                    return
                self.status = "ERROR"
                self.status_detail = "WebUI-Prüfung fehlgeschlagen - Logs prüfen"
                if hasattr(self, "startup_progress"):
                    self.startup_progress.update(phase="error", label="WebUI-Prüfung fehlgeschlagen", active=False, updated_at=time.time())
                update_config({"active_pod_status": "ERROR"})
            self.log(f"Hinweis: WebUI-Prüfung nach Reconcile fehlgeschlagen ({e})", level="WARNING")

    def _log_wan2gp_tail(self, ssh):
        """Holt bei Startfehlern die tatsächliche Wan2GP-Ausgabe vom Pod."""
        try:
            code, out, err = ssh.exec_command(
                "tail -n 80 /workspace/wan2gp.log", timeout=15)
            if code == 0 and out.strip():
                self.log(f"Letzte Wan2GP-Logzeilen:\n{out[-12000:]}", level="ERROR")
            else:
                self.log(f"Wan2GP-Log nicht verfügbar: {err.strip() or 'Log ist leer'}", level="WARNING")
        except Exception as e:
            self.log(f"Wan2GP-Log konnte nicht gelesen werden: {e}", level="WARNING")

    def _bootstrap_wan2gp(self, ip: str, port: int):
        operation_id = getattr(self, "_operation_id", None)
        pod_id = getattr(self, "pod_id", None)
        def cancelled():
            return operation_id is not None and not self._operation_current(operation_id, pod_id)

        if cancelled():
            return False
        self._set_startup_progress("ssh", "SSH-Verbindung wird aufgebaut",
                                   "Warte auf den SSH-Dienst im Container.", operation_id)
        self.log("Starte SSH-Bootstrap für Wan2GP auf dem Pod...")
        ssh = SSHManager(ip, port, log_callback=self.log)

        # Retry SSH connect for up to 60s
        connected = False
        for attempt in range(12):
            if cancelled():
                return False
            try:
                client = ssh.get_client(timeout=10)
                client.close()
                connected = True
                break
            except Exception:
                self.log(f"Warte auf SSH-Daemon im Container... ({attempt+1}/12)")
                time.sleep(5)

        if not connected:
            raise PodPreflightError("ssh", "Konnte keine SSH-Verbindung zum Pod herstellen "
                                           "(SSH-Daemon im Container antwortet nicht).")
        if cancelled():
            return False
        self._set_startup_progress("ssh", "GPU-Zugriff wird geprüft",
                                   "Prüfe den CUDA-Treiber vor der Installation.", operation_id)
        cuda_check = (Path(__file__).resolve().parents[1] / "scripts" / "check_pod_cuda.py").read_text(encoding="utf-8")
        cuda_command = "python3 - <<'VIDEOGEN_CUDA'\n" + cuda_check + "\nVIDEOGEN_CUDA\n"
        cuda_code, cuda_out, cuda_err = ssh.exec_command(cuda_command, timeout=30)
        if cancelled():
            return False
        if cuda_code != 0:
            detail = (cuda_err.strip() or cuda_out.strip() or "CUDA-Prüfung ohne Rückmeldung fehlgeschlagen.")[-500:]
            self._set_startup_progress("ssh", "GPU-Zugriff prüfen", detail, operation_id)
            self.log(f"CUDA-Prüfung fehlgeschlagen: {detail}", level="ERROR")
            raise PodPreflightError("cuda", detail)
        self.log(cuda_out.strip() or "CUDA-Prüfung erfolgreich.")

        # Hosts mit lahmer/toter Leitung früh aussortieren: Die Modell-Downloads
        # sind 30+ GB; ein 100-KB/s-Host würde stundenlang Geld verbrennen.
        min_net_mb = float(load_config().get("preflight_min_net_speed_mb", 5) or 0)
        if min_net_mb > 0:
            self._set_startup_progress("ssh", "Netzwerk-Geschwindigkeit wird geprüft",
                                       "Teste Download-Rate zu HuggingFace für die Modell-Downloads.", operation_id)
            if cancelled():
                return False
            net_speed = self._preflight_network_speed(ssh)
            if net_speed is not None and net_speed < min_net_mb * 1_000_000:
                detail = (f"Download-Rate zu HuggingFace nur {net_speed / 1e6:.2f} MB/s "
                          f"(Minimum {min_net_mb:.0f} MB/s) - Host-Netzwerk zu langsam für Modell-Downloads.")
                self._set_startup_progress("ssh", "Netzwerk prüfen", detail, operation_id)
                self.log(f"Netzwerk-Preflight fehlgeschlagen: {detail}", level="ERROR")
                raise PodPreflightError("network", detail)
            if net_speed is not None:
                self.log(f"Netzwerk-Preflight ok: {net_speed / 1e6:.1f} MB/s zu HuggingFace.")

        self._set_startup_progress("install", "WanGP und Pakete werden installiert",
                                   "Installation wird im Pod gestartet.", operation_id)

        # The RunPod proxy rewrites Host to its internal address. Wan2GP-Versionen
        # ab dem Upstream-RunPod-Fix (Sept 2026) bringen dafuer ein --public-url
        # Flag mit; aeltere Versionen erhalten den exakten Public-Origin weiter
        # ueber unseren Source-Patch.
        public_origin = self.proxy_url or RunPodClient.get_proxy_url(self.pod_id, 7860)
        proxy_patcher = (Path(__file__).resolve().parents[1] / "scripts" / "patch_wan2gp_proxy.py").read_text(encoding="utf-8")
        proxy_patch_command = (
            f"python3 - --public-origin {shlex.quote(public_origin)} <<'RUNPOD_PROXY_PATCH'\n"
            + proxy_patcher + "\nRUNPOD_PROXY_PATCH\n"
        )
        origin_fix_block = (
            "PUBLIC_URL_ARG=\"\"\n"
            "if grep -q '\"--public-url\"' /workspace/Wan2GP/shared/authentication/__init__.py 2>/dev/null; then\n"
            "    echo \"[BOOTSTRAP] Wan2GP unterstuetzt --public-url (upstream RunPod-Fix); kein Source-Patch noetig.\"\n"
            f"    PUBLIC_URL_ARG=\"--public-url {public_origin}\"\n"
            "else\n"
            "    echo \"[BOOTSTRAP] Alte Wan2GP-Version: Origin-Pruefung wird gepatcht.\"\n"
            + proxy_patch_command +
            "fi\n"
        )

        bootstrap_script = """
        set -e
        mkdir -p /workspace/.cache/huggingface
        export HF_HOME=/workspace/.cache/huggingface

        # Clone or update Wan2GP in persistent volume
        if [ ! -d "/workspace/Wan2GP" ]; then
            echo "[VIDEOGEN_PHASE] install"
            echo "[BOOTSTRAP] Klone Wan2GP nach /workspace/Wan2GP..."
            cd /workspace
            git clone https://github.com/deepbeepmeep/Wan2GP.git
        else
            echo "[BOOTSTRAP] Wan2GP bereits auf Volume vorhanden."
        fi

        cd /workspace/Wan2GP
        export PYTHONUNBUFFERED=1
        export PIP_PROGRESS_BAR=off
        echo "[BOOTSTRAP] Prüfe und installiere Python-Pakete; dieser Schritt kann mehrere Minuten dauern..."
        python3 -m pip install --no-cache-dir -r requirements.txt

        # Torch- und Triton-Stand pruefen: Wan2GP / comfy-kitchen (0.2.35)
        # benoetigen torch >= 2.5 (custom_op-Schema mit list[int] und
        # String-Annotationen) bzw. triton >= 3.2. Der RunPod-Container
        # runpod-torch-v240 liefert torch 2.4.1 / triton 3.0 -> einmalig heben.
        if ! python3 -c "import torch; raise SystemExit(0 if tuple(map(int, torch.__version__.split('+')[0].split('.')[:2])) >= (2, 5) else 1)"; then
            echo "[BOOTSTRAP] Torch zu alt fuer Wan2GP-Abhaengigkeiten - upgrade auf 2.6.0 (cu124)..."
            python3 -m pip install --no-cache-dir "torch==2.6.0" "torchvision==0.21.0" "torchaudio==2.6.0"
        fi
        python3 -c "import torch, triton; print('[BOOTSTRAP] torch', torch.__version__, '| triton', triton.__version__, '| CUDA:', torch.cuda.is_available())"

        echo "[VIDEOGEN_PHASE] launch"

        # Triton Compatibility Patch NUR fuer triton < 3.2 (RunPod-Altcontainer):
        # Wan2GP uses do_not_specialize_on_alignment which is only in Triton 3.2+.
        # Ab triton 3.2 wuerde der Wrapper das kwarg wegwerfen und ist daher gesperrt.
        if ! python3 -c "import triton; raise SystemExit(0 if tuple(map(int, triton.__version__.split('.')[:2])) >= (3, 2) else 1)"; then
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
        fi

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

        # RUNPOD_ORIGIN_FIX

        # Check if Wan2GP already running - bewusst über den Port statt pgrep:
        # Ein pgrep auf den Skriptnamen matchet über den bash -c-Wrapper des
        # SSH-Kommandos die Shell selbst und meldet fälschlich "läuft bereits".
        if curl -sf -o /dev/null -m 3 http://127.0.0.1:7860; then
            echo "[BOOTSTRAP] Wan2GP laeuft bereits."
        else
            echo "[BOOTSTRAP] Starte Wan2GP WebUI auf Port 7860..."
            cd /workspace/Wan2GP
            setsid python -u wgp.py --server-port 7860 --server-name 0.0.0.0 $PUBLIC_URL_ARG </dev/null >> /workspace/wan2gp.log 2>&1 &
            sleep 4
        fi
        """
        bootstrap_script = bootstrap_script.replace("        # RUNPOD_ORIGIN_FIX\n", origin_fix_block, 1)
        progress_phase = "install"
        def on_install_line(line):
            nonlocal progress_phase
            if cancelled():
                return
            if line.startswith("[VIDEOGEN_PHASE] "):
                next_phase = line.split(" ", 1)[1].strip()
                if next_phase in ("install", "launch"):
                    progress_phase = next_phase
                return
            if not line.strip():
                return
            self.log(line)
            label = "WanGP wird gestartet" if progress_phase == "launch" else "WanGP und Pakete werden installiert"
            self._set_startup_progress(progress_phase, label, line, operation_id)

        code, out, err = ssh.exec_command_stream(bootstrap_script, timeout=900,
                                                on_line=on_install_line, cancelled=cancelled)
        if cancelled():
            return False
        if code != 0:
            self.log(f"Bootstrap-Fehler (Code {code}):\n{err}", level="ERROR")
            self._log_wan2gp_tail(ssh)
            return False

        # Verify HTTP readiness and that this pod's browser origin is accepted.
        self._set_startup_progress("launch", "WebUI wird geprüft",
                                   "Warte auf HTTP-Antwort und gültige Browser-Verbindung.", operation_id)
        self.log("Prüfe Wan2GP-WebUI und Browser-Origin (Port 7860)...")
        for attempt in range(18):
            time.sleep(5)
            if cancelled():
                return False
            chk_code, chk_out, _ = ssh.exec_command(self._webui_probe_command(), timeout=15)
            if cancelled():
                return False
            if chk_code == 0 and "READY" in chk_out.splitlines():
                self.log("Wan2GP WebUI ist erreichbar und bereit!")
                return True
            self.log(f"Warte auf Wan2GP WebUI... ({attempt+1}/18)")
            self._set_startup_progress("launch", "WebUI wird geprüft",
                                       f"WebUI antwortet noch nicht (Prüfung {attempt + 1} von 18).", operation_id)

        self.log("Wan2GP-WebUI antwortet nach Ablauf der Startprüfung nicht. Start fehlgeschlagen; Logs prüfen.", level="ERROR")
        self._log_wan2gp_tail(ssh)
        return False

    def _kick_video_sync(self):
        """Startet den Video-Sync als eigenen Daemon-Thread.

        Ein hängender SFTP-Transfer darf den Kostenschutz im Hintergrund-Loop
        (Auto-Shutdown, Kostenaktualisierung) nie blockieren. Der Sync läuft
        maximal einmal gleichzeitig, damit es keinen Thread-Stau gibt."""
        with self.lock:
            if self._sync_in_progress:
                return
            ip, port = self.pod_ip, self.pod_ssh_port
            if not ip or not port:
                return
            self._sync_in_progress = True

        def run():
            try:
                ssh = SSHManager(ip, port, log_callback=self.log)
                new_vids = ssh.sync_videos()
                if new_vids:
                    self.last_activity_time = time.time()
                    self.log(f"Neu synchronisierte Videos: {', '.join(new_vids)}")
            except Exception as e:
                self.log(f"Video-Sync fehlgeschlagen ({type(e).__name__}): {e}", level="WARNING")
            finally:
                with self.lock:
                    self._sync_in_progress = False

        threading.Thread(target=run, daemon=True, name="video-sync").start()

    def _background_loop(self):
        """Monitors session cost, auto-shutdown timer and kicks video syncs."""
        sync_counter = 0
        last_rate_poll = 0
        while not self._stop_bg_thread:
            time.sleep(2)
            try:
                cfg = load_config()
                now = time.time()

                with self.lock:
                    status = self.status
                    pod_id, operation_id = self.pod_id, self._operation_id

                if pod_id and status in ("RUNNING", "BOOTSTRAPPING", "ERROR") and now - last_rate_poll >= 30:
                    last_rate_poll = now
                    try:
                        pod = self.get_client().get_pod(pod_id)
                        if pod:
                            # ERROR beschreibt die Anwendung, nicht den Cloud-
                            # Zustand. Ein nachweislich pausierter Pod darf nie
                            # allein wegen eines alten Idle-Timers gelöscht werden.
                            if pod.get("desiredStatus") == "EXITED":
                                with self.lock:
                                    if operation_id != self._operation_id or pod_id != self.pod_id:
                                        continue
                                    self.status = "STOPPED"
                                    self._book_session_cost()
                                    self.session_start_time = None
                                    self.hourly_rate = None
                                    self._hourly_rate_pod_id = None
                                    self.status_detail = (f"Pod pausiert (Speicher erhalten). {self.status_detail}"
                                                          if status == "ERROR" else "Pod pausiert (Speicher erhalten)")
                                    update_config({"active_pod_status": "STOPPED"})
                                continue
                            self._refresh_pod_rate(pod_id, pod, operation_id)
                    except Exception as e:
                        # Pricing is advisory; its API must not block the idle
                        # shutdown check or forget an otherwise running pod.
                        self.log(f"Pod-Preis konnte nicht aktualisiert werden ({type(e).__name__}); letzter bestätigter Preis bleibt erhalten.", level="WARNING")
                if not self._operation_current(operation_id, pod_id):
                    continue

                if status not in ("RUNNING", "ERROR"):
                    continue

                # Update live cost
                if status == "RUNNING":
                    with self.lock:
                        if self.session_start_time and self.hourly_rate is not None:
                            elapsed_hours = (now - self.session_start_time) / 3600.0
                            self.accumulated_cost = elapsed_hours * self.hourly_rate

                # Auto-Shutdown Check - auch für ERROR-Pods: Der lokale Fehlerstatus
                # sagt nichts darüber, ob der Pod bei RunPod noch läuft und Kosten
                # macht. (Laufende Installationen sind über last_activity_time
                # geschützt, das der Bootstrap-Fortschritt aktuell hält.)
                try:
                    auto_shut = int(cfg.get("auto_shutdown_minutes", 30) or 0)
                except (ValueError, TypeError):
                    auto_shut = 30

                if auto_shut > 0 and pod_id:
                    idle_seconds = now - self.last_activity_time
                    if idle_seconds > (auto_shut * 60):
                        if cfg.get("standby_mode", "stop") == "terminate":
                            self.log("Auto-Shutdown: Inaktivität erreicht! Terminiere Pod (Modus 'Löschen')...", level="WARNING")
                            self.terminate_pod()
                        else:
                            self.log(f"Auto-Shutdown: {auto_shut} Minuten Inaktivität erreicht! Pausiere GPU (Stop Pod)...", level="WARNING")
                            self.stop_pod()

                # Sync Videos every ~15 seconds (eigener Thread, blockiert nie
                # den Kostenschutz oben)
                if status == "RUNNING":
                    sync_counter += 2
                    try:
                        sync_interval = int(cfg.get("sync_interval_seconds", 15) or 15)
                    except (ValueError, TypeError):
                        sync_interval = 15

                    if sync_counter >= sync_interval and cfg.get("auto_sync_videos", True):
                        sync_counter = 0
                        self._kick_video_sync()
            except Exception as e:
                self.log(f"Hintergrund-Dienst Fehler (wird abgefangen): {e}", level="ERROR")
                time.sleep(5)

    def get_state(self) -> Dict[str, Any]:
        cfg = load_config()
        with self.lock:
            uptime_sec = 0
            if self.session_start_time and self.status == "RUNNING":
                uptime_sec = int(time.time() - self.session_start_time)
            rate_known = (self.hourly_rate is not None and self.pod_id is not None
                          and self._hourly_rate_pod_id == self.pod_id)
            return {
                "status": self.status,
                "status_detail": self.status_detail,
                "pod_id": self.pod_id,
                "pod_ip": self.pod_ip,
                "pod_ssh_port": self.pod_ssh_port,
                "proxy_url": self.proxy_url,
                "uptime_seconds": uptime_sec,
                "hourly_rate": round(self.hourly_rate, 6) if rate_known else None,
                "hourly_rate_source": "runpod" if rate_known else "unknown",
                "startup_progress": dict(self.startup_progress),
                "accumulated_cost": round(self.accumulated_cost, 4),
                "standby_mode": cfg.get("standby_mode", "stop"),
                "gpu_type_id": cfg.get("gpu_type_id", "NVIDIA A40"),
                "auto_shutdown_minutes": cfg.get("auto_shutdown_minutes", 30),
                "recent_logs": list(self.log_history)[-50:]
            }

    def delete_video(self, filename: str) -> bool:
        """Delete locally; delete remotely only with matching content provenance.

        Legacy name maps and equal file sizes cannot establish identity. Require
        a verified download plus the current remote SHA256 before removing it.
        """
        safe_name = os.path.basename(filename)
        deleted_anything = False
        local_path = os.path.join(get_outputs_dir(), safe_name)
        identity = get_verified_identity(safe_name, local_path)
        with self.lock:
            ip, port, status = self.pod_ip, self.pod_ssh_port, self.status

        if os.path.exists(local_path):
            try:
                os.remove(local_path)
                deleted_anything = True
                self.log(f"Video lokal gelöscht: {safe_name}")
            except Exception as e:
                self.log(f"Fehler beim lokalen Löschen von {safe_name}: {e}", level="WARNING")
                return False
        if not os.path.exists(local_path):
            try:
                remove_name_mapping(safe_name)
            except OSError as e:
                self.log(f"Video gelöscht, Zuordnung konnte nicht bereinigt werden: {e}", level="WARNING")

        if deleted_anything and identity and ip and port and status == "RUNNING":
            original_name = identity["remote"]
            try:
                ssh = SSHManager(ip, port, log_callback=self.log)
                if ssh.delete_verified_file(f"/workspace/Wan2GP/outputs/{original_name}", identity["sha256"]):
                    self.log(f"Video auch auf dem Pod gelöscht: {original_name}")
                else:
                    self.log(f"Remote-Inhalt von '{original_name}' ist nicht bestätigt oder wurde verändert; "
                             "Datei auf dem Pod bleibt erhalten.", level="WARNING")
            except Exception as e:
                self.log(f"Hinweis: Konnte Video auf Pod nicht löschen: {e}", level="WARNING")
        elif deleted_anything and not identity:
            self.log(f"Keine verifizierte Remote-Zuordnung für '{safe_name}'; nur lokal gelöscht.", level="WARNING")
        return deleted_anything
