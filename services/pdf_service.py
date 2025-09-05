# backend/services/pdf_service.py

import requests
from backend.config import PDF_API_KEY, PDF_SECRET_KEY

def send_invoice(payload: dict):
    url = "https://api.pdfgeneratorapi.com/v3/documents"
    
    headers = {
        "Authorization": f"Bearer {PDF_API_KEY}",
        "X-Secret-Key": PDF_SECRET_KEY,
        "Content-Type": "application/json"
    }

    response = requests.post(url, json=payload, headers=headers)

    if response.status_code == 201:
        return response.json()
    else:
        raise Exception(f"PDF generation failed: {response.status_code} - {response.text}")
