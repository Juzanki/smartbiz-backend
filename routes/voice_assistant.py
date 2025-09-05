# ================= backend/routes/voice_assistant.py =================
# -*- coding: utf-8 -*-
from __future__ import annotations
import asyncio
import os
import re
import uuid
from pathlib import Path
from typing import Optional, Literal, Tuple, Any
from contextlib import suppress

from fastapi import (
    APIRouter, UploadFile, File, HTTPException,
    BackgroundTasks, Header, Query
)
from fastapi import status as http_status
from pydantic import BaseModel, Field

router = APIRouter(tags=["Voice Assistant"])  # prefix utaongezwa na main.py ("/assistant")

# ---------------- Config ----------------
TEMP_DIR = Path(os.getenv("AUDIO_TMP", "/tmp/voice_assistant"))
RESP_DIR = Path(os.getenv("AUDIO_RESP_DIR", "static/responses"))
TEMP_DIR.mkdir(parents=True, exist_ok=True)
RESP_DIR.mkdir(parents=True, exist_ok=True)

AUDIO_TIMEOUT_SEC = int(os.getenv("AUDIO_TIMEOUT_SEC", "120"))

ALLOWED_AUDIO_MIME = {
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav",
    "audio/webm", "audio/ogg", "audio/aac", "audio/mp4", "audio/m4a",
}
ALLOWED_VIDEO_MIME = {
    "video/mp4", "video/quicktime", "video/x-matroska", "video/webm", "video/mpeg",
}
ALLOWED_EXT = {
    ".mp3", ".wav", ".m4a", ".aac", ".ogg", ".webm",
    ".mp4", ".mov", ".mkv", ".qt",
}

OPENAI_API_KEY = (
    os.getenv("OPENAI_API_KEY")
    or os.getenv("OPENAI_API_TOKEN")
    or os.getenv("OPENAI_KEY")
)

# Project helpers (optional)
AUDIO_UTILS: dict[str, Any] = {}
with suppress(Exception):
    from backend.utils import audio_utils as _au  # type: ignore
    if all(hasattr(_au, n) for n in ("extract_audio_from_video", "transcribe_audio", "generate_voice_response")):
        AUDIO_UTILS = {
            "extract": _au.extract_audio_from_video,
            "transcribe": _au.transcribe_audio,
            "tts": _au.generate_voice_response,
        }

# SpeechRecognition (optional)
SR_AVAILABLE = False
with suppress(Exception):
    import speech_recognition as sr  # pip install SpeechRecognition
    SR_AVAILABLE = True

def _new_openai_client():
    with suppress(Exception):
        from openai import OpenAI  # pip install openai
        return OpenAI(api_key=OPENAI_API_KEY)
    return None

# ---------------- Helpers ----------------
def _sanitize_filename(name: str) -> str:
    name = name or "upload"
    name = re.sub(r"[^\w.\-]+", "_", name)
    return name[:120]

def _to_url(path: Path) -> Optional[str]:
    try:
        rel = path.resolve().relative_to(RESP_DIR.resolve())
        return f"/{RESP_DIR.as_posix().strip('/')}/{rel.as_posix()}"
    except Exception:
        return None

async def _write_temp(upload: UploadFile, dest: Path) -> None:
    CHUNK = 1024 * 1024
    with dest.open("wb") as f:
        while True:
            chunk = await upload.read(CHUNK)
            if not chunk:
                break
            f.write(chunk)

def _is_audio_video(file: UploadFile) -> bool:
    ct = (file.content_type or "").lower()
    ext_ok = any((_sanitize_filename(file.filename or "").lower().endswith(ext) for ext in ALLOWED_EXT))
    return (ct in ALLOWED_AUDIO_MIME) or (ct in ALLOWED_VIDEO_MIME) or ext_ok

def _safe_extract_transcript_payload(tx: Any) -> Tuple[str, Optional[str]]:
    if tx is None:
        return "", None
    if isinstance(tx, str):
        return tx.strip(), None
    if isinstance(tx, dict):
        text = str(tx.get("text", "")).strip()
        lang = tx.get("language")
        return text, (str(lang) if lang else None)
    return str(tx).strip(), None

# ---------------- DTOs ----------------
class VoiceAssistantResponse(BaseModel):
    request_id: str
    transcript: str = Field("", description="Recognized text")
    language: Optional[str] = Field(None, description="BCP-47 or engine code")
    message: str = Field(..., description="Assistant reply")
    voice_reply_url: Optional[str] = Field(None, description="URL of generated TTS audio")

class TTSRequest(BaseModel):
    text: str
    voice: Optional[str] = None
    lang: Optional[str] = None

# ---------------- Core ops with fallbacks ----------------
async def _extract_audio(input_path: Path, wav_path: Path, original_ct: Optional[str]) -> Path:
    if AUDIO_UTILS:
        await asyncio.wait_for(AUDIO_UTILS["extract"](str(input_path), str(wav_path)), timeout=AUDIO_TIMEOUT_SEC)
        return wav_path
    if original_ct and original_ct.lower().startswith("audio/"):
        return input_path
    raise HTTPException(status_code=400, detail="No audio extractor available for video files")

async def _transcribe_audio_generic(audio_path: Path, lang: str = "en-US") -> Tuple[str, Optional[str]]:
    if AUDIO_UTILS:
        tx = await asyncio.wait_for(AUDIO_UTILS["transcribe"](str(audio_path)), timeout=AUDIO_TIMEOUT_SEC)
        return _safe_extract_transcript_payload(tx)

    if OPENAI_API_KEY:
        client = _new_openai_client()
        if client:
            with suppress(Exception):
                resp = client.audio.transcriptions.create(
                    model=os.getenv("OPENAI_STT_MODEL", "whisper-1"),
                    file=open(str(audio_path), "rb"),
                    language=lang,
                )
                text = getattr(resp, "text", None) or str(resp)
                return str(text).strip(), lang

    if SR_AVAILABLE:
        try:
            recognizer = sr.Recognizer()
            with sr.AudioFile(str(audio_path)) as source:  # type: ignore[name-defined]
                audio = recognizer.record(source)
            text = recognizer.recognize_google(audio, language=lang)  # type: ignore[name-defined]
            return text, lang
        except sr.UnknownValueError:  # type: ignore[name-defined]
            return "", lang
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Local transcription error: {e}")

    raise HTTPException(status_code=503, detail="No STT backend available")

async def _tts_generate(message: str, request_id: str, voice: str, fmt: str) -> Optional[Path]:
    if not message:
        return None
    if AUDIO_UTILS:
        try:
            with suppress(TypeError):
                p = await asyncio.wait_for(
                    AUDIO_UTILS["tts"](message, request_id, voice=voice, fmt=fmt),
                    timeout=AUDIO_TIMEOUT_SEC,
                )
                return Path(str(p))
            p = await asyncio.wait_for(AUDIO_UTILS["tts"](message, request_id), timeout=AUDIO_TIMEOUT_SEC)
            return Path(str(p))
        except Exception:
            return None
    return None

# ---------------- Diagnostics ----------------
@router.get("/status")
def assistant_status():
    return {
        "audio_utils": bool(AUDIO_UTILS),
        "openai": bool(OPENAI_API_KEY),
        "speech_recognition": SR_AVAILABLE,
        "temp_dir": str(TEMP_DIR),
        "responses_dir": str(RESP_DIR),
    }

# ---------------- Quick STT ----------------
@router.post("/transcribe")
async def transcribe(file: UploadFile = File(...), lang: str = "en-US"):
    if not _is_audio_video(file):
        raise HTTPException(status_code=415, detail="Unsupported file type. Provide audio or video.")
    request_id = uuid.uuid4().hex
    safe_name = _sanitize_filename(file.filename or "upload")
    input_path = TEMP_DIR / f"{request_id}_{safe_name}"
    wav_path = TEMP_DIR / f"{request_id}.wav"
    try:
        await _write_temp(file, input_path)
        out_path = await asyncio.wait_for(_extract_audio(input_path, wav_path, file.content_type), timeout=AUDIO_TIMEOUT_SEC)
        transcript, _ = await _transcribe_audio_generic(out_path, lang=lang)
        return {"request_id": request_id, "text": transcript}
    finally:
        with suppress(Exception): input_path.exists() and input_path.unlink()
        with suppress(Exception): wav_path.exists() and wav_path.unlink()

# ---------------- Main endpoint ----------------
@router.post(
    "/voice-assistant",
    response_model=VoiceAssistantResponse,
    status_code=http_status.HTTP_201_CREATED,
    summary="Upload audio/video, get transcript and concise reply (+ optional TTS URL)",
)
async def voice_shopping_assistant(
    file: UploadFile = File(..., description="Audio/Video file"),
    background: BackgroundTasks = None,  # FastAPI injects if parameter exists
    return_audio: bool = Query(True, description="Generate TTS reply audio"),
    voice: Literal["female", "male", "neutral"] = Query("female", description="Preferred voice"),
    tts_format: Literal["mp3", "wav"] = Query("mp3", description="TTS output format"),
    idempotency_key: Optional[str] = Header(None, convert_underscores=False, description="Avoid duplicate processing"),
):
    if not _is_audio_video(file):
        raise HTTPException(status_code=415, detail="Unsupported file type. Provide audio or video.")

    request_id = idempotency_key or uuid.uuid4().hex
    safe_name = _sanitize_filename(file.filename or "upload")
    input_path = TEMP_DIR / f"{request_id}_{safe_name}"
    wav_path = TEMP_DIR / f"{request_id}.wav"

    try:
        await _write_temp(file, input_path)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to persist upload: {e}")

    def _cleanup():
        with suppress(Exception): input_path.exists() and input_path.unlink()
        with suppress(Exception): wav_path.exists() and wav_path.unlink()
    if background: background.add_task(_cleanup)

    try:
        out_path = await asyncio.wait_for(_extract_audio(input_path, wav_path, file.content_type), timeout=AUDIO_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Audio extraction timed out")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Audio extraction failed: {e}")

    try:
        transcript, language = await asyncio.wait_for(_transcribe_audio_generic(out_path), timeout=AUDIO_TIMEOUT_SEC)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Transcription timed out")

    message = f"You asked about: {transcript}. The product is available in our stock." if transcript \
              else "I couldn’t understand the audio clearly. Please try again."

    voice_url: Optional[str] = None
    if return_audio and message:
        tts_path = await _tts_generate(message, request_id, voice=voice, fmt=tts_format)
        if tts_path:
            RESP_DIR.mkdir(parents=True, exist_ok=True)
            voice_url = _to_url(tts_path) or f"/{RESP_DIR.as_posix().strip('/')}/{tts_path.name}"

    return VoiceAssistantResponse(
        request_id=request_id, transcript=transcript, language=language,
        message=message, voice_reply_url=voice_url
    )
