"""Run only the protected checkout's adapter, never the proposed repository package."""

import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))

from intent_engineering.integrations.protected_ci import main

raise SystemExit(main(root))
