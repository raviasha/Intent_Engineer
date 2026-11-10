"""Dispatch hooks and CLI with the same packaged interpreter."""

import runpy
import sys
from pathlib import Path

if len(sys.argv) >= 3 and sys.argv[1] == "--hook":
    name = sys.argv[2]
    if name not in {"prompt-hook", "stop-hook", "reconcile-hook", "monitor-hook"}:
        raise SystemExit("Unknown Intent Advisor hook")
    script = Path(__file__).with_name(name + ".py")
    sys.argv = [str(script), *sys.argv[3:]]
    runpy.run_path(str(script), run_name="__main__")
else:
    from intent_engineering.cli.app import app

    app()
