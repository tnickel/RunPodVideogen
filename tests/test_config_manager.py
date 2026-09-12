# Regressionstests für backend/config_manager.py
# Deckt u. a. P2-9b ab: atomares Schreiben, Fallback bei korrupter Config.

import json

import backend.config_manager as cm


def _use(tmp_path, monkeypatch, cfg_content=None):
    cfg_file = tmp_path / "config.json"
    if cfg_content is not None:
        cfg_file.write_text(cfg_content, encoding="utf-8")
    outputs = tmp_path / "outputs"
    outputs.mkdir(exist_ok=True)
    monkeypatch.setattr(cm, "_CONFIG_PATH", str(cfg_file))
    monkeypatch.setattr(cm, "_OUTPUTS_DIR", str(outputs))
    return cfg_file


def test_load_creates_default_file_when_missing(tmp_path, monkeypatch):
    cfg_file = _use(tmp_path, monkeypatch, cfg_content=None)
    assert not cfg_file.exists()

    cfg = cm.load_config()

    assert cfg_file.exists(), "load_config muss eine fehlende Config mit Defaults anlegen"
    assert cfg["runpod_api_key"] == ""
    # Robust gegen legitime Default-Änderungen (z. B. Commit 192553c: A40-Default)
    assert cfg["gpu_type_id"] == cm.DEFAULT_CONFIG["gpu_type_id"]
    assert cfg["server_host"] == "127.0.0.1", "Server darf nur nach expliziter Config auf LAN lauschen"
    assert "model_preset" not in cfg, "Toter Config-Schlüssel model_preset wurde entfernt"
    assert cfg["active_pod_id"] is None


def test_load_merges_missing_keys_and_persists(tmp_path, monkeypatch):
    cfg_file = _use(tmp_path, monkeypatch, cfg_content=json.dumps({"gpu_type_id": "NVIDIA A40"}))

    cfg = cm.load_config()

    # Fehlende Schlüssel sind mit Defaults ergänzt ...
    assert cfg["gpu_type_id"] == "NVIDIA A40"  # vorhandener Wert bleibt
    assert cfg["auto_shutdown_minutes"] == 30  # fehlender Schlüssel ergänzt
    # ... und sofort persistiert (Regressionsschutz gegen Config-Drift).
    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert on_disk["auto_shutdown_minutes"] == 30


def test_corrupt_config_falls_back_to_defaults_without_crash(tmp_path, monkeypatch):
    cfg_file = _use(tmp_path, monkeypatch, cfg_content="{invalid json,,,")

    cfg = cm.load_config()

    assert cfg["gpu_type_id"] == cm.DEFAULT_CONFIG["gpu_type_id"]
    assert cfg["active_pod_id"] is None, "Korrupte Config darf nie einen Pod-ID-Fake liefern"


def test_update_config_roundtrip(tmp_path, monkeypatch):
    cfg_file = _use(tmp_path, monkeypatch)

    updated = cm.update_config({"account_balance": 5.5, "gpu_type_id": "NVIDIA A40"})

    assert updated["account_balance"] == 5.5
    on_disk = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert on_disk["account_balance"] == 5.5
    assert on_disk["gpu_type_id"] == "NVIDIA A40"


def test_atomic_save_leaves_no_tmp_files_behind(tmp_path, monkeypatch):
    cfg_file = _use(tmp_path, monkeypatch)

    for i in range(5):
        cm.update_config({"account_balance": float(i)})

    leftovers = [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
    assert leftovers == [], f"Temp-Dateien übrig geblieben: {leftovers}"
    assert json.loads(cfg_file.read_text(encoding="utf-8"))["account_balance"] == 4.0
