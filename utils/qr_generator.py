import qrcode
import os

def generate_product_qr(data: str, filename: str = "product_qr.png") -> str:
    qr = qrcode.make(data)
    output_path = os.path.join("static", "qr", filename)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    qr.save(output_path)
    return output_path