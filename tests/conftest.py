"""Run native widget tests without requiring a display server in CI."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
