import qrcode
import os
from pathlib import Path
from io import BytesIO
from fastapi.responses import StreamingResponse


def generate_qr_code(data: str, filename: str) -> str:
    folder = Path("static/qr_codes")
    folder.mkdir(parents=True, exist_ok=True)
    filepath = folder / f"{filename}.png"

    img = qrcode.make(data)
    img.save(filepath)
    return str(filepath)

def generate_qr_code(data: str):
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(data)
    qr.make(fit=True)
    
    img = qr.make_image(fill="black", back_color="white")
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")