from backend.schemas.user import UserOut
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import httpx

router = APIRouter()

SMARTINJECT_API_BASE = "http://localhost:8010/smartinject"

# === Request Schemas ===
class InjectionRequest(BaseModel):
    tag: str
    response: str

class PromptRequest(BaseModel):
    prompt: str


# === Forward Routes to SmartInjectGPT Kernel ===

@router.post("/inject-code")
def forward_injection(req: InjectionRequest):
    try:
        response = httpx.post(f"{SMARTINJECT_API_BASE}/inject", json=req.dict())
        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"âŒ Injection forwarding failed: {str(e)}")


@router.post("/send-prompt")
def forward_prompt(req: PromptRequest):
    try:
        response = httpx.post(f"{SMARTINJECT_API_BASE}/observe", json=req.dict())
        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"âŒ Prompt forwarding failed: {str(e)}")


@router.post("/execute-kernel")
def forward_execution():
    try:
        response = httpx.post(f"{SMARTINJECT_API_BASE}/execute")
        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"âŒ Execution forwarding failed: {str(e)}")


@router.get("/vision-report")
def get_vision_report():
    try:
        response = httpx.get(f"{SMARTINJECT_API_BASE}/vision/report")
        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"âŒ Vision report failed: {str(e)}")


@router.get("/kernel-health")
def get_kernel_health():
    try:
        response = httpx.get(f"{SMARTINJECT_API_BASE}/health")
        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"âŒ Kernel health check failed: {str(e)}")

