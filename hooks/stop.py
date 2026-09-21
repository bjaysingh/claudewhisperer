#!/usr/bin/env python3
"""Claude Whisperer - opt-in Stop hook.

Records the length of Claude's final reply on the open Whisperer run for this session, so
`/whisper stats` can show whether replies are actually getting shorter. Never blocks, never prints.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))


def main() -> None:
    try:
        data = json.load(sys.stdin)
        msg = data.get("last_assistant_message") or ""
        session = data.get("session_id", "")
        if not msg:
            return
        import whisper_lib as W
        with W.memory_lock(timeout_s=1.0):
            recs = W.read_log()
            idx = W.find_pending(recs, session=session, max_age_s=3 * 3600)
            if idx is None:
                return
            r = recs[idx]
            if r.get("reply_chars") is None:
                r["reply_chars"] = len(msg)
                r["reply_lines"] = msg.count("\n") + 1
                W.rewrite_log(recs)
    except Exception:
        return


if __name__ == "__main__":
    main()
