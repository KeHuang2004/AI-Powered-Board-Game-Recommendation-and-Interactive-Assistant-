import os
import sys
from pathlib import Path

local_runtime = Path(__file__).resolve().parent / ".runtime"
if os.name == "nt" and sys.version_info[:2] == (3, 14) and local_runtime.is_dir():
    sys.path.insert(0, str(local_runtime))

try:
    import uvicorn
    import fastapi
except ModuleNotFoundError as error:
    raise SystemExit("请先运行 python -m pip install -r requirements.txt") from error

if __name__ == "__main__":
    uvicorn.run("backend.server:app", host="127.0.0.1", port=8001)
