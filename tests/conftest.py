"""Shared test setup.

app.main reads META_DIR / DOWNLOADS_DIR from the environment at import time,
so point them at a temp directory before any test imports the app.
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_TEST_ROOT = tempfile.mkdtemp(prefix="swa-test-")
os.environ["META_DIR"] = os.path.join(_TEST_ROOT, "meta")
os.environ["DOWNLOADS_DIR"] = os.path.join(_TEST_ROOT, "downloads")
os.environ["STEAMCMD_PATH"] = os.path.join(_TEST_ROOT, "steamcmd.sh")
os.environ.pop("AUTH_PASSWORD", None)
