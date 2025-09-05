from __future__ import annotations
from fastapi import FastAPI, APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from typing import List, Optional
import uuid
from datetime import datetime

app = FastAPI(title="Title Suggester Pro", version="1.0.0")

# Mount static files and templates
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# Router for our API endpoints
router = APIRouter()

class SuggestionRequest(BaseModel):
    events: List[str]
    session_id: Optional[str] = None

class SuggestionResponse(BaseModel):
    suggested_title: str
    session_id: str
    timestamp: str

# Store session history (in production, use a proper database)
session_history = {}

@router.post("/suggest-title", response_model=SuggestionResponse)
async def suggest_title(request: SuggestionRequest):
    if not request.events:
        raise HTTPException(status_code=400, detail="Events list cannot be empty")
    
    # Generate or use existing session ID
    session_id = request.session_id or str(uuid.uuid4())
    
    # Simple suggestion logic (extended from original)
    suggested_title = "?? SmartBiz Replay"  # Default
    
    events_lower = [event.lower() for event in request.events]
    
    if any("big gift" in event for event in events_lower):
        suggested_title = "🎁 Gift Rain Show"
    elif any("peak moment" in event for event in events_lower):
        suggested_title = "🚀 Peak Power Experience"
    elif any("celebration" in event for event in events_lower):
        suggested_title = "🎉 Celebration Extravaganza"
    elif any("discount" in event for event in events_lower):
        suggested_title = "💰 Discount Bonanza"
    elif any("new product" in event for event in events_lower):
        suggested_title = "🆕 Product Launch Spectacular"
    elif any("anniversary" in event for event in events_lower):
        suggested_title = "🥳 Anniversary Special"
    
    # Store suggestion in history
    timestamp = datetime.now().isoformat()
    if session_id not in session_history:
        session_history[session_id] = []
    
    session_history[session_id].append({
        "events": request.events,
        "suggestion": suggested_title,
        "timestamp": timestamp
    })
    
    return SuggestionResponse(
        suggested_title=suggested_title,
        session_id=session_id,
        timestamp=timestamp
    )

@router.get("/history/{session_id}")
async def get_history(session_id: str):
    if session_id not in session_history:
        return {"history": []}
    return {"history": session_history[session_id]}

# Frontend routes
@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

# Include the router
app.include_router(router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)