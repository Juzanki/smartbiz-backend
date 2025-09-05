$env:ACTIVE_DB = "local"
uvicorn backend.main:app --reload
