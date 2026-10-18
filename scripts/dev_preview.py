"""Start the testing app for a local visual check with live services disabled.

Binds 127.0.0.1:8001 only. HTTP login, AI extraction, image search and Google
connections are switched off for this process, so a preview never sends
business data to an external service. Use start-local.ps1 for normal testing.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
os.environ.update({
    "FASHION_APP_USERNAME": "", "FASHION_APP_PASSWORD": "",
    "FASHION_AI_API_KEY": "", "FASHION_GEMINI_API_KEY": "",
    "FASHION_GOOGLE_CSE_API_KEY": "", "FASHION_SEARXNG_URL": "",
    "FASHION_GOOGLE_CLIENT_ID": "", "FASHION_GOOGLE_CLIENT_SECRET": "",
})
os.environ.setdefault("FASHION_DATABASE_URL", f"sqlite:///{(ROOT / 'data' / 'fashion_orders.db').as_posix()}")
sys.path.insert(0, str(ROOT))

import uvicorn  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="127.0.0.1", port=8001)
