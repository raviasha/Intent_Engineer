#!/usr/bin/env python3
"""Best-effort completion counter; never block or continue the host task."""

import subprocess
import sys

try:
    raw = sys.stdin.buffer.read(65537)
    if 0 < len(raw) <= 65536:
        subprocess.run(
            ["intent", "agent-stop-hook"],
            input=raw,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
        )
except (OSError, subprocess.SubprocessError):
    pass
sys.stdout.write("{}\n")
