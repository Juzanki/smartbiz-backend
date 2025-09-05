$env:ACTIVE_DB = "railway"
uvicorn backend.main:app --reload
