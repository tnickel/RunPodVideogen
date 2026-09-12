import os
import json
import threading

_CONFIG_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "config.json"))
_OUTPUTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "outputs"))
_lock = threading.Lock()

DEFAULT_CONFIG = {
    "runpod_api_key": "",
    "account_email": "",
    "active_pod_id": None,
    "active_pod_status": "OFFLINE",
    "gpu_type_id": "NVIDIA GeForce RTX 4090",
    "cloud_type": "ALL",
    "volume_disk_gb": 75,
    "container_disk_gb": 30,
    "standby_mode": "stop",
    "auto_shutdown_minutes": 30,
    "server_port": 7880,
    "model_preset": "MiniMax H3 FL2VA Pruned 20B (PDD 8-Step)",
    "auto_sync_videos": True,
    "sync_interval_seconds": 15
}

def get_outputs_dir() -> str:
    os.makedirs(_OUTPUTS_DIR, exist_ok=True)
    return _OUTPUTS_DIR

def load_config() -> dict:
    with _lock:
        if not os.path.exists(_CONFIG_PATH):
            save_config_unsafe(DEFAULT_CONFIG)
            return DEFAULT_CONFIG.copy()
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            # Merge missing keys
            updated = False
            for k, v in DEFAULT_CONFIG.items():
                if k not in data:
                    data[k] = v
                    updated = True
            if updated:
                save_config_unsafe(data)
            return data
        except Exception as e:
            print(f"[ConfigManager] Error reading config: {e}")
            return DEFAULT_CONFIG.copy()

def save_config_unsafe(cfg: dict):
    with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)

def update_config(updates: dict) -> dict:
    with _lock:
        cfg = {}
        if os.path.exists(_CONFIG_PATH):
            try:
                with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                    cfg = json.load(f)
            except Exception:
                cfg = DEFAULT_CONFIG.copy()
        else:
            cfg = DEFAULT_CONFIG.copy()
            
        cfg.update(updates)
        save_config_unsafe(cfg)
        return cfg
