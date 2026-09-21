"""Test bootstrap: point WHISPERER_HOME at a scratch dir BEFORE whisper_lib is imported,
so no test can touch the real memory/ store. Import this first from every test module.

Also drops TYPESAFE_API_KEY: with it set, jev_available() is true and the suite makes live
API calls, so triage answers change under the developer's environment and tests that pin
heuristic behaviour fail. run_cases.py --jev is where the Jev path is exercised, and it restores the key from
_STASHED_JEV_KEY."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

if not os.environ.get("WHISPERER_HOME", "").startswith(tempfile.gettempdir()):
    os.environ["WHISPERER_HOME"] = tempfile.mkdtemp(prefix="whisperer-test-")

_STASHED_JEV_KEY = os.environ.pop("TYPESAFE_API_KEY", "")

sys.path.insert(0, os.path.join(ROOT, "scripts"))
