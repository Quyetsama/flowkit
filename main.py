"""
FlowKit server entrypoint.
Usage:
    python main.py
    # or: python -m agent.main
"""
import os
import uvicorn
from agent.config import API_HOST, API_PORT

if __name__ == "__main__":
    reload_enabled = os.environ.get("GLA_RELOAD", "0") == "1"
    uvicorn.run(
        "agent.main:app",
        host=API_HOST,
        port=API_PORT,
        reload=reload_enabled,
        reload_excludes=["*.db", "*.db-wal", "*.db-shm", "output/*"],
    )
