from __future__ import annotations
from backend.schemas.user import UserOut
# backend/routes/ai_captions.py
import math
from typing import List, Dict, Optional, AsyncGenerator, Literal

from fastapi import APIRouter, HTTPException, status, Response
from fastapi.responses import StreamingResponse, PlainTextResponse
from pydantic import BaseModel, Field, validator

router = APIRouter(prefix="/caption-ai", tags=["AI Captions"])

# -----------------------------------------------------------------------------
# Schemas
# -----------------------------------------------------------------------------
class CaptionRequest(BaseModel):
    """
    Ombi la kutengeneza manukuu (captions).
    Tumia MOJA kati ya hizi:
      - timestamp (+ window) -> hukata kipande kuzunguka sekunde hiyo
      - start + end          -> hukata kipande kati ya muda huo
    """
    # Chaguo 1: timestamp + window
    timestamp: Optional[float] = Field(
        default=None, ge=0, description="Sekunde ndani ya video."
    )
    window: float = Field(
        default=8.0, ge=2.0, le=120.0, description="Urefu wa kipande (sekunde) ukitumia timestamp."
    )

    # Chaguo 2: start/end
    start: Optional[float] = Field(default=None, ge=0)
    end: Optional[float] = Field(default=None, ge=0)

    language: str = Field(default="sw", description="Lugha ya matokeo, kama 'sw' au 'en'.")
    format: Literal["json", "srt", "vtt"] = Field(default="json")
    stream: bool = Field(default=False, description="Iwapo urudishe majibu taratibu (stream).")
    prompt: Optional[str] = Field(default=None, description="Context ya hiari kuboresha caption.")

    @validator("language")
    def _norm_lang(cls, v: str) -> str:
        v = (v or "").strip().lower().replace("_", "-")
        return v or "sw"

    @validator("end")
    def _validate_range(cls, v, values):
        s = values.get("start")
        if v is not None and s is not None and v <= s:
            raise ValueError("end lazima iwe kubwa kuliko start")
        return v

    @validator("window")
    def _cap_window(cls, v):
        # weka decimals kidogo kwa usahihi mzuri
        return round(float(v), 3)

    def resolve_range(self) -> tuple[float, float]:
        """
        Rudisha (start, end) kwa kipimo cha sekunde.
        Ikiwa umetumia timestamp, range = [t - window/2, t + window/2] ikibanwa kwenda >=0.
        """
        if self.start is not None and self.end is not None:
            return (round(float(self.start), 3), round(float(self.end), 3))

        if self.timestamp is None:
            raise ValueError("Weka aidha {timestamp} au {start & end}.")
        t = float(self.timestamp)
        half = self.window / 2.0
        s = max(0.0, t - half)
        e = s + self.window
        return (round(s, 3), round(e, 3))


class WordItem(BaseModel):
    start: float
    end: float
    word: str

class CaptionSegment(BaseModel):
    idx: int
    start: float
    end: float
    text: str
    words: Optional[List[WordItem]] = None
    confidence: Optional[float] = None

class CaptionJSONResponse(BaseModel):
    start: float
    end: float
    language: str
    segments: List[CaptionSegment]


# -----------------------------------------------------------------------------
# Utilities: time formatting (SRT/VTT)
# -----------------------------------------------------------------------------
def _fmt_srt_ts(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - math.floor(sec)) * 1000.0))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"

def _fmt_vtt_ts(sec: float) -> str:
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = int(sec % 60)
    ms = int(round((sec - math.floor(sec)) * 1000.0))
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

def _segments_to_srt(segments: List[CaptionSegment]) -> str:
    lines = []
    for seg in segments:
        lines.append(str(seg.idx))
        lines.append(f"{_fmt_srt_ts(seg.start)} --> {_fmt_srt_ts(seg.end)}")
        lines.append(seg.text.strip())
        lines.append("")  # blank line
    return "\n".join(lines).strip() + "\n"

def _segments_to_vtt(segments: List[CaptionSegment]) -> str:
    lines = ["WEBVTT", ""]
    for seg in segments:
        lines.append(f"{_fmt_vtt_ts(seg.start)} --> {_fmt_vtt_ts(seg.end)}")
        lines.append(seg.text.strip())
        lines.append("")  # blank line
    return "\n".join(lines).strip() + "\n"


# -----------------------------------------------------------------------------
# MOCK ASR/LLM: badili sehemu hii kuunganisha WHISPER/ASR ya kweli
# -----------------------------------------------------------------------------
def _mock_transcribe(start: float, end: float, lang: str, prompt: Optional[str]) -> List[CaptionSegment]:
    """
    Hii ni placeholder. Badili iite service yako (Whisper, Deepgram, Azure, n.k.).
    Unaporudisha segments, andaa start/end/text/words/confidence kama inavyoonekana.
    """
    # Tuchome segments 2-3 za mfano
    span = max(0.5, (end - start) / 3.0)
    segs: List[CaptionSegment] = []
    t = start
    idx = 1
    samples = [
        "Habari, tunachakata ombi lako.",
        "Tafadhali subiri kidogo, tutakuletea majibu.",
        "Asante kwa uvumilivu wako."
    ] if lang.startswith("sw") else [
        "Hello, we are processing your request.",
        "Please hold on, we will provide the answer shortly.",
        "Thank you for your patience."
    ]
    for sample in samples:
        s = t
        e = min(end, s + span)
        segs.append(CaptionSegment(
            idx=idx,
            start=round(s, 3),
            end=round(e, 3),
            text=(f"{sample} {('[Context] ' + prompt) if prompt else ''}").strip(),
            words=None,
            confidence=0.92
        ))
        idx += 1
        t = e
        if t >= end:
            break
    return segs


# -----------------------------------------------------------------------------
# Streaming generator (text lines). Mobile clients hupokea â€œchunksâ€ haraka.
# -----------------------------------------------------------------------------
async def _stream_segments_as_srt(segments: List[CaptionSegment]) -> AsyncGenerator[bytes, None]:
    # Header optional kwa SRT (wengi hawahitaji header)
    for seg in segments:
        chunk = f"{seg.idx}\n{_fmt_srt_ts(seg.start)} --> {_fmt_srt_ts(seg.end)}\n{seg.text}\n\n"
        yield chunk.encode("utf-8")
    yield b""


# -----------------------------------------------------------------------------
# Endpoint
# -----------------------------------------------------------------------------
@router.post(
    "/",
    summary="Generate captions for a timestamp or time range",
    responses={
        200: {"description": "Success"},
        206: {"description": "Partial content (stream)"},
        422: {"description": "Validation error"},
    },
)
async def generate_caption(data: CaptionRequest):
    """
    - Ukipeleka `timestamp` (na option `window`), kipande kitakatwa kuizunguka.
    - Au peleka `start` + `end` moja kwa moja.
    - `format`: `json` (default), `srt`, `vtt`
    - `stream`: True (stream kwa `srt` tu hapa â€” rahisi kwa mobile)
    """
    try:
        start, end = data.resolve_range()
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(e))

    if end - start < 0.5:
        raise HTTPException(status_code=422, detail="Kipande kifupi mno (< 0.5s). Ongeza window au end.")

    # >>> Hapa ungeunganisha WHISPER/ASR halisi <<<
    segments = _mock_transcribe(start, end, data.language, data.prompt)

    # --- JSON ---
    if data.format == "json":
        payload = CaptionJSONResponse(
            start=start, end=end, language=data.language, segments=segments
        )
        return payload

    # --- SRT ---
    if data.format == "srt":
        if data.stream:
            gen = _stream_segments_as_srt(segments)
            return StreamingResponse(gen, media_type="text/plain; charset=utf-8", status_code=status.HTTP_206_PARTIAL_CONTENT)
        srt_text = _segments_to_srt(segments)
        return PlainTextResponse(srt_text, media_type="text/plain; charset=utf-8", status_code=200)

    # --- VTT ---
    if data.format == "vtt":
        vtt_text = _segments_to_vtt(segments)
        # VTT mara nyingi hutumia media_type 'text/vtt'
        return PlainTextResponse(vtt_text, media_type="text/vtt; charset=utf-8", status_code=200)

    # Isipofika hapa tayari, format haijatambuliwa (lakini validator inatulinda)
    raise HTTPException(status_code=422, detail="Unsupported format")

