# backend/middleware/cors.py
from fastapi.middleware.cors import CORSMiddleware

def add_cors(app):
    # Ruhusu Vite dev origin yako
    allowed = {
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    }
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed),          # usitumie "*" ukiwa unatumia credentials/bearer
        allow_credentials=True,               # cookies/Authorization headers
        allow_methods=["*"],                  # ama ["GET","POST","PUT","PATCH","DELETE","OPTIONS"]
        allow_headers=["*"],                  # hakikisha Content-Type & Authorization zinaruhusiwa
        expose_headers=["*"],                 # hiari
        max_age=86400,                        # cache ya preflight (OPTIONS)
    )
