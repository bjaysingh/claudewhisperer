"""Test bootstrap: point WHISPERER_HOME at a scratch dir BEFORE whisper_lib is imported,
so no test can touch the real memory/ store. Import this first from every test module."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if not os.environ.get("WHISPERER_HOME", "").startswith(tempfile.gettempdir()):
    os.environ["WHISPERER_HOME"] = tempfile.mkdtemp(prefix="whisperer-test-")

sys.path.insert(0, os.path.join(ROOT, "scripts"))
