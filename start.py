# start.py — SmartBiz Assistance entrypoint
import os
import uvicorn
from backend.main import app


def run():
    """Start SmartBiz backend using uvicorn with sane defaults."""
    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8000"))

    # Logging level
    log_level = os.getenv("LOG_LEVEL", "info").lower()

    # Environment mode
    env = os.getenv("ENVIRONMENT", "production").lower()
    debug = os.getenv("DEBUG", "0").lower() in {"1", "true", "yes"}

    # Auto-reload only in dev
    reload_enabled = env in {"dev", "development"} or debug

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
        reload=reload_enabled,
        proxy_headers=True,
        forwarded_allow_ips="*",
        # You can pick "httptools" for speed if installed
        http="httptools" if os.getenv("USE_HTTPTOOLS", "1") == "1" else "h11",
    )


if __name__ == "__main__":
    run()
