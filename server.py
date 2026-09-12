import os
import sys
import asyncio
import subprocess
from typing import Dict, Any, List
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from backend.config_manager import load_config, update_config, get_outputs_dir
from backend.runpod_client import RunPodClient
from backend.orchestrator import PodOrchestrator

app = FastAPI(title="RunPod VideoGen Studio", version="1.0.0")
orchestrator = PodOrchestrator()

# Mount outputs directory for direct streaming if needed
outputs_dir = get_outputs_dir()

# WebSocket active connections
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: str):
        for connection in list(self.active_connections):
            try:
                await connection.send_text(message)
            except Exception:
                self.disconnect(connection)

ws_manager = ConnectionManager()

# Hook orchestrator logs to WebSocket
def forward_log_to_ws(entry: str):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            asyncio.run_coroutine_threadsafe(ws_manager.broadcast(entry), loop)
    except Exception:
        pass

orchestrator.add_listener(forward_log_to_ws)

class ConfigUpdateModel(BaseModel):
    runpod_api_key: str | None = None
    gpu_type_id: str | None = None
    cloud_type: str | None = None
    volume_disk_gb: int | None = None
    standby_mode: str | None = None
    auto_shutdown_minutes: int | None = None
    auto_sync_videos: bool | None = None

class VideoDeleteModel(BaseModel):
    filename: str
    account_balance: float | None = None

# Routes
@app.get("/api/status")
def get_status():
    state = orchestrator.get_state()
    cfg = load_config()
    client = orchestrator.get_client()
    
    account_info = {
        "email": cfg.get("account_email", ""),
        "balance": float(cfg.get("account_balance", 18.74))
    }
    try:
        user = client.get_myself()
        if user and "email" in user:
            account_info["email"] = user.get("email", account_info["email"])
    except Exception:
        pass

    return {
        **state,
        "account": account_info,
        "config": {
            "gpu_type_id": cfg.get("gpu_type_id"),
            "cloud_type": cfg.get("cloud_type"),
            "volume_disk_gb": cfg.get("volume_disk_gb"),
            "standby_mode": cfg.get("standby_mode"),
            "auto_shutdown_minutes": cfg.get("auto_shutdown_minutes"),
            "auto_sync_videos": cfg.get("auto_sync_videos")
        }
    }

@app.get("/api/gpus")
def get_gpus():
    client = orchestrator.get_client()
    try:
        all_gpus = client.get_gpu_types()
        # Filter down to GPUs relevant for 20B / Video models (>= 24GB VRAM)
        filtered = []
        for g in all_gpus:
            vram = g.get("memoryInGb", 0)
            if vram >= 24:
                filtered.append({
                    "id": g["id"],
                    "displayName": g["displayName"],
                    "vram": vram,
                    "communityPrice": g.get("communityPrice"),
                    "securePrice": g.get("securePrice")
                })
        # Sort by price
        filtered.sort(key=lambda x: (x.get("communityPrice") or 99))
        return {"gpus": filtered}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/pod/start")
def start_pod():
    res = orchestrator.start_pod()
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.post("/api/pod/stop")
def stop_pod():
    res = orchestrator.stop_pod()
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.post("/api/pod/terminate")
def terminate_pod():
    res = orchestrator.terminate_pod()
    if not res["success"]:
        raise HTTPException(status_code=400, detail=res["message"])
    return res

@app.get("/api/videos")
def list_videos():
    folder = get_outputs_dir()
    files = []
    for f in os.listdir(folder):
        if f.lower().endswith((".mp4", ".png", ".jpg")):
            path = os.path.join(folder, f)
            stat = os.stat(path)
            files.append({
                "filename": f,
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "modified": stat.st_mtime,
                "url": f"/api/videos/file/{f}"
            })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return {"videos": files}

@app.get("/api/videos/file/{filename}")
def get_video_file(filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(get_outputs_dir(), safe_name)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="Datei nicht gefunden")
    return FileResponse(path)

@app.post("/api/videos/open-folder")
def open_folder():
    folder = get_outputs_dir()
    try:
        if sys.platform == "win32":
            os.startfile(folder)
        else:
            subprocess.Popen(["xdg-open", folder])
        return {"success": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/account/refresh")
def refresh_account(set_balance: float | None = None):
    cfg = load_config()
    client = orchestrator.get_client()
    state = orchestrator.get_state()
    
    email = cfg.get("account_email", "")
    try:
        user_info = client.get_myself()
        if user_info and "email" in user_info:
            email = user_info["email"]
            update_config({"account_email": email})
    except Exception:
        pass

    if set_balance is not None:
        update_config({"account_balance": round(set_balance, 2)})
        balance = round(set_balance, 2)
    else:
        base_balance = float(cfg.get("account_balance", 18.74))
        accumulated = float(state.get("accumulated_cost", 0.0))
        balance = max(0.0, round(base_balance - accumulated, 2))

    return {
        "success": True,
        "account": {
            "email": email,
            "balance": balance
        }
    }

@app.post("/api/videos/delete")
def delete_video_endpoint(model: VideoDeleteModel):
    safe_name = os.path.basename(model.filename)
    success = orchestrator.delete_video(safe_name)
    return {"success": success, "filename": safe_name}


@app.get("/api/config")
def get_config_endpoint():
    cfg = load_config()
    # Mask API key partially for security
    key = cfg.get("runpod_api_key", "")
    masked = key[:6] + "..." + key[-4:] if len(key) > 10 else key
    return {**cfg, "runpod_api_key_masked": masked}

@app.post("/api/config")
def update_config_endpoint(model: ConfigUpdateModel):
    updates = {k: v for k, v in model.model_dump().items() if v is not None}
    updated = update_config(updates)
    orchestrator.log(f"Einstellungen aktualisiert: {list(updates.keys())}")
    return {"success": True, "config": updated}

@app.websocket("/ws/logs")
async def websocket_logs(websocket: WebSocket):
    await ws_manager.connect(websocket)
    # Send recent logs immediately
    state = orchestrator.get_state()
    for entry in state["recent_logs"]:
        await websocket.send_text(entry)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        ws_manager.disconnect(websocket)

# Serve Frontend static files
static_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "static"))
os.makedirs(static_dir, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
def serve_index():
    index_file = os.path.join(static_dir, "index.html")
    if os.path.exists(index_file):
        return FileResponse(index_file)
    return HTMLResponse("<h1>RunPod VideoGen Studio lädt...</h1>")

if __name__ == "__main__":
    import uvicorn
    cfg = load_config()
    port = int(cfg.get("server_port", 7880))
    print(f"=======================================================")
    print(f"  RunPod VideoGen Studio startet auf Port {port}")
    print(f"  Öffne im Browser: http://localhost:{port}")
    print(f"=======================================================")
    uvicorn.run(app, host="0.0.0.0", port=port)
