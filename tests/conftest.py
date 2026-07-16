"""
Make handlers/ importable for the test suite without depending on the
sys.path injection that dispatch.py does at runtime.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "labs" / "network-automation"))
