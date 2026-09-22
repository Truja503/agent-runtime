"""Operator-only setup. Run with the same Python environment as the API server."""

import subprocess
import sys

if __name__ == "__main__":
    raise SystemExit(subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"]))
