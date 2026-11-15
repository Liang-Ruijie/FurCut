"""Start a local FurCut workspace."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))
os.environ.setdefault("APP_PORT", "8004")

if __name__ == "__main__":
    from app.main import run
    run()
