"""Protected scheduled-assurance adapter for the reviewed checkout."""

import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))

from intent_engineering.integrations.github_action import main

raise SystemExit(main(require_ci=True))
