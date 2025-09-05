from __future__ import annotations
import os
import sys
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional

# === Ensure SmartInjectGPT is on path ===
SMARTINJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "SmartInjectGPT"))
if SMARTINJECT_ROOT not in sys.path:
    sys.path.insert(0, SMARTINJECT_ROOT)

# === Import Kernel ===
from SmartInjectGPT.backend.kernel.smartinject_kernel import kernel

router = APIRouter()

# === Request Schemas ===
class InjectionRequest(BaseModel):
    tag: str
    response: str

class PromptRequest(BaseModel):
    prompt: str

# === Injection Endpoint ===
@router.post("/inject", tags=["SmartInject"])
def inject_code(req: InjectionRequest):
    try:
        file_map_path = os.path.abspath(
            os.path.join(SMARTINJECT_ROOT, "scripts", "file_map.json")
        )

        if not os.path.exists(file_map_path):
            raise HTTPException(status_code=404, detail="❌ file_map.json not found")

        with open(file_map_path, "r", encoding="utf-8") as f:
            file_map = json.load(f)

        if req.tag not in file_map:
            raise HTTPException(status_code=404, detail=f"❌ Tag '{req.tag}' not found in file_map")

        target_path = os.path.abspath(os.path.join(os.getcwd(), file_map[req.tag]))
        os.makedirs(os.path.dirname(target_path), exist_ok=True)

        with open(target_path, "w", encoding="utf-8") as f:
            f.write(req.response)

        return {"message": f"✅ Code successfully injected to {file_map[req.tag]}"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"💥 Injection failed: {str(e)}")

# === Kernel Endpoints ===
@router.post("/observe", tags=["SmartInjectGPT"])
def kernel_observe(data: PromptRequest):
    if not data.prompt.strip():
        raise HTTPException(status_code=400, detail="⚠️ Prompt cannot be empty")
    kernel.observe(data.prompt)
    return {"message": "🧠 Prompt sent to kernel"}

@router.post("/execute", tags=["SmartInjectGPT"])
def kernel_execute():
    kernel.decide_and_act()
    return {"message": "✅ Kernel executed successfully"}

@router.get("/vision/scan", tags=["SmartInjectGPT"])
def vision_scan():
    entries = kernel.vision.scan_sources()
    kernel.vision.analyze_and_store(entries)
    return {"message": "📷 Vision scan complete", "entries": len(entries)}

@router.get("/vision/report", tags=["SmartInjectGPT"])
def vision_report():
    return {"message": "🧾 Vision Report", "report": kernel.vision.report()}

@router.get("/health", tags=["SmartInjectGPT"])
def kernel_health():
    return {
        "kernel": kernel.identity,
        "status": "🟢 active",
        "memory": kernel.memory[-5:],
        "recent_activity": getattr(kernel, "activity_log", [])[-5:]
    }
