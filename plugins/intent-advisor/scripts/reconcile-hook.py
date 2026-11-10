#!/usr/bin/env python3
"""Trusted async entry point. Replace this process so cancellation reaches the worker."""

import os
import sys

try:
    os.execvp("intent", ["intent", "agent-reconcile-hook"])
except OSError:
    sys.stdout.write("{}\n")
