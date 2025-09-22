web: bash -lc 'PYTHONPATH=. uvicorn backend.main:app \
  --host 0.0.0.0 --port ${PORT:-8000} \
  --proxy-headers --forwarded-allow-ips="*" --log-level info'
