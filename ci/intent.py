"""Run the reviewed checkout's CLI without installing repository code."""

import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
os.environ["INTENT_CI_TRUST_REQUIRED"] = "1"

from intent_engineering.cli.app import app

app()
