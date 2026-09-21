"""Claude Whisperer - shared library.

Stdlib only. Python 3.9+ (macOS system python works).
Everything here is deterministic except the optional Jev calls, which
degrade to heuristics when TYPESAFE_API_KEY is absent or the call fails.
"""
import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

try:
    import fcntl
except ImportError:      # non-POSIX; locking degrades to last-writer-wins
    fcntl = None

# --------------------------------------------------------------------------- paths

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEMORY_DIR = os.environ.get("WHISPERER_HOME") or os.path.join(SKILL_DIR, "memory")
LOG_PATH = os.path.join(MEMORY_DIR, "log.jsonl")
LEARNINGS_PATH = os.path.join(MEMORY_DIR, "learnings.md")
CONFIG_PATH = os.path.join(MEMORY_DIR, "config.json")
PENDING_DIR = os.path.join(MEMORY_DIR, "pending")
JEV_STATUS_PATH = os.path.join(MEMORY_DIR, "jev_status.json")
SHORTCUTS_PATH = os.path.join(MEMORY_DIR, "shortcuts.json")
PROMPTS_PATH = os.path.join(MEMORY_DIR, "prompts.jsonl")
LOCK_PATH = os.path.join(MEMORY_DIR, ".lock")
_LOCK_DEPTH = 0                 # re-entrancy counter for memory_lock()   # fingerprints of every prompt seen by the hook (for repetition detection)

DEFAULT_CONFIG: Dict[str, Any] = {
    "consolidate_every": 10,      # run `learn` after this many new runs
    "store_prompts": False,       # keep full prompt text in the log (off: fingerprint + hash only)
    "store_previews": False,      # keep a short redacted preview of each prompt (off: content-word fingerprint only)
    "preview_chars": 160,
    "stale_after_runs": 30,       # a rule unused for this many runs is flagged stale
    "max_active_rules": 25,       # `learn` warns above this; more rules than this get ignored, not followed
    "hook": {"skip_under_words": 7},
    "jev": {
        "enabled": True,
        # pinned, not `jev-latest`: the thresholds below are tuned against whatever model answered
        # last, and an alias moves without a change on your side. Bump it deliberately, then run
        # `python3 tests/run_cases.py --jev` and read the diff before shipping.
        "model": "jev-1.13.0",
        "endpoint": "https://api.typesafe.ai/v1/systemone",
        "timeout_s": 2.5,
        "task_min_confidence": 0.55,   # below this, fall back to the keyword heuristic
        "label_min_confidence": 0.6,   # below this, outcome stays "uncertain"
        "intent_drop_pause_at": 0.35,  # noul >= this -> pause for approval (asymmetric: dropping intent is costly)
    },
}


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ensure_memory() -> None:
    os.makedirs(MEMORY_DIR, exist_ok=True)
    os.makedirs(PENDING_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        write_json(CONFIG_PATH, DEFAULT_CONFIG)
    if not os.path.exists(LEARNINGS_PATH):
        with open(LEARNINGS_PATH, "w", encoding="utf-8") as f:
            f.write(LEARNINGS_TEMPLATE)
    if not os.path.exists(LOG_PATH):
        open(LOG_PATH, "a", encoding="utf-8").close()
    if not os.path.exists(SHORTCUTS_PATH):
        write_json(SHORTCUTS_PATH, {})


def load_config() -> Dict[str, Any]:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    except Exception:
        pass
    return cfg


def atomic_write(path: str, text: str) -> None:
    """Write via a per-process temp file. The pid suffix matters: two sessions share one memory
    dir, and a shared temp name lets one process' os.replace pull the file out from under another."""
    tmp = "%s.tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def write_json(path: str, data: Any) -> None:
    atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))


@contextlib.contextmanager
def memory_lock(timeout_s: float = 3.0):
    """Serialise a read-modify-write of the memory files across processes.

    Both hooks and the CLI read a whole file, mutate it and write it back, and several Claude Code
    sessions share one memory dir, so without this the last writer wins and the other's labels are
    lost. Best-effort by contract: if the lock cannot be taken in time it yields anyway, because
    memory must never block the task it is attached to."""
    global _LOCK_DEPTH
    if _LOCK_DEPTH > 0:      # re-entrant: flock is per open-file-description, so a second
        _LOCK_DEPTH += 1     # handle in this process would block against our own lock
        try:
            yield
        finally:
            _LOCK_DEPTH -= 1
        return
    ensure_memory()
    handle = None
    if fcntl is not None:
        try:
            handle = open(LOCK_PATH, "a+")
            deadline = time.time() + timeout_s
            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() >= deadline:
                        break
                    time.sleep(0.01)
        except Exception:
            handle = None
    _LOCK_DEPTH += 1
    try:
        yield
    finally:
        _LOCK_DEPTH -= 1
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
            handle.close()


def one_line(text: str) -> str:
    """Collapse to a single line. learnings.md is line-oriented (RULE_RE is line-anchored), so a
    newline inside a rule silently truncates it at the next parse and drops the rest on the next save."""
    return re.sub(r"\s+", " ", (text or "").replace("\r", " ")).strip()


def read_stdin_text() -> str:
    if sys.stdin.isatty():
        return ""
    return sys.stdin.read()


def split_sections(text: str) -> Dict[str, str]:
    """Parse '<<<NAME>>>' delimited sections from stdin text."""
    parts: Dict[str, str] = {}
    matches = list(re.finditer(r"^<<<([A-Z_]+)>>>[ \t]*\n?", text, flags=re.M))
    if not matches:
        return {"ORIGINAL": text.strip()}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        parts[m.group(1)] = text[m.end():end].strip()
    return parts


# --------------------------------------------------------------------------- text heuristics

def estimate_tokens(text: str) -> int:
    """Rough token estimate (chars/4). Good enough for before/after ratios."""
    return max(1, int(len(text) / 4 + 0.5))


FILLER_GROUPS: Dict[str, List[str]] = {
    "greeting": [r"\bhey( there)?( claude)?\b", r"\bhi( there)?( claude)?\b", r"\bhello\b",
                 r"\bi hope you'?re (doing )?well\b", r"\bgood (morning|afternoon|evening)\b"],
    "politeness": [r"\bplease\b", r"\bkindly\b", r"\bthanks?( you)?( so much| a lot)?\b", r"\bcould you\b",
                   r"\bcan you\b", r"\bwould you( mind)?\b", r"\bi(?:'d| would) (like|love) (you )?to\b",
                   r"\bi want you to\b", r"\bi need you to\b", r"\bif possible\b", r"\bif you can\b"],
    "hedge": [r"\bi think\b", r"\bmaybe\b", r"\bperhaps\b", r"\bsort of\b", r"\bkind of\b", r"\bi guess\b",
              r"\bnot (totally |entirely |100% )?sure\b", r"\bprobably\b", r"\bsomewhere\b"],
    "intensifier": [r"\bjust\b", r"\bbasically\b", r"\bactually\b", r"\breally\b", r"\bvery\b", r"\bquite\b",
                    r"\bdriving me (crazy|nuts)\b", r"\bsuper\b", r"\btotally\b"],
    "meta_instruction": [r"\bmake sure (to|that|you)\b", r"\bbe (very )?careful\b", r"\bdon'?t break anything\b",
                         r"\bdo your best\b", r"\btake your time\b", r"\byou are an? (expert|senior|world[- ]class|helpful)\b",
                         r"\bas an? (expert|senior|experienced)\b", r"\bact as\b", r"\bpretend (to be|you are)\b",
                         r"\bthink (step[- ]by[- ]step|carefully|hard|deeply)\b", r"\bdouble[- ]check\b",
                         r"\bbe thorough\b", r"\bbe smart\b"],
}

RISK_PATTERNS: Dict[str, str] = {
    "delete": r"\b(delete|remove|rm\s+-rf?|rmdir|unlink|purge|wipe)\b",
    # `drop table x` is SQL word order; people write "drop the sessions table", so allow words between
    "schema": r"\b((drop|delete)\s+(?:\w+\s+){0,3}(tables?|columns?|databases?|indexe?s?)|migrations?|migrate|alter\s+table|truncate)\b",
    "git_destructive": r"(force[- ]push|push\s+--force|--force-with-lease|reset\s+--hard|git\s+push|filter-branch|rebase\s+-i|history\s+rewrite)",
    "deploy": r"\b(deploy|release|publish|ship(ping)? to prod|production|\bprod\b|rollout)\b",
    "external_effects": r"\b(send (an? |the )?(email|message|slack|sms|notification)|post to|payment|charge|invoice|webhook|tweet)\b",
    "dependencies": r"\b(npm (i|install|add)|pip install|yarn add|pnpm add|cargo add|go get|add (a |the )?dependenc(y|ies)|bun add)\b",
    "privileges_secrets": r"\b(sudo|chmod|chown|\.env\b|secrets?|credentials?|api[ _-]?keys?)\b",
}

SECRET_PATTERNS: List[str] = [
    r"sk-[A-Za-z0-9_\-]{16,}",
    r"AKIA[0-9A-Z]{16}",
    r"gh[pousr]_[A-Za-z0-9]{20,}",
    r"xox[baprs]-[A-Za-z0-9\-]{10,}",
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    r"(?i)\b(password|passwd|pwd|token|api[_\-]?key|secret|bearer)\b\s*[=:]\s*['\"]?[A-Za-z0-9_\-./+]{8,}",
    r"(?i)\b(postgres|mysql|mongodb(\+srv)?|redis)://[^\s'\"]+:[^\s'\"]+@",
]

TASK_KEYWORDS: List[Tuple[str, str]] = [
    ("bugfix", r"\b(fix|bug|broken|error|exception|crash|fail(s|ing|ed)?|doesn'?t work|not working|500|traceback|regression)\b"),
    ("test", r"\b(tests?|coverage|spec|unit test|integration test|pytest|jest|vitest)\b"),
    ("refactor", r"\b(refactor|clean ?up|rename|extract|simplify|reorganize|dedupe|tidy|restructure)\b"),
    ("review", r"\b(review|audit|critique|check (this|the|my)|look over|feedback on|is this (ok|correct|good))\b"),
    ("explain", r"\b(explain|what does|how does|why (does|is)|walk me through|understand|what is)\b"),
    ("research", r"\b(research|compare|investigate|find out|options for|evaluate|which (library|approach|tool)|pros and cons)\b"),
    ("docs", r"\b(readme|docs?|documentation|docstrings?|changelog|write ?up|comment the)\b"),
    ("ops", r"\b(config(ure|uration)?|set ?up|install|ci|cd|pipeline|deploy|docker|env(ironment)?|makefile|github actions)\b"),
    ("feature", r"\b(add|implement|build|create|support|new|feature|endpoint|component|page|flag|integrate)\b"),
]

ACK_PATTERNS: List[str] = [
    r"^(y|yes|yep|yeah|ok|okay|sure|go|go ahead|do it|proceed|continue|next|lgtm|looks good|approved|fine|sounds good|k|kk)\b[.!]?$",
    r"^(no|nope|stop|cancel|hold on|wait|never ?mind)\b[.!]?$",
    r"^(thanks|thank you|ty|thx|cheers|perfect|great|nice|awesome)\b[.!]?$",
    r"^(run|rerun|re-run) (the )?(tests?|build|linter|lint)$",
    r"^(commit|commit it|push|push it|try again|retry|undo|revert)$",
    r"^(option|choice)?\s*[1-9a-d]$",
]

CORRECTION_PATTERNS: Dict[str, List[str]] = {
    "dropped_detail": [r"\byou (missed|dropped|forgot|ignored|skipped|left out|removed)\b", r"\bi (said|asked|meant|told you|wanted)\b",
                       r"\bwhere('?s| is) the\b", r"\byou didn'?t (include|add|keep|do)\b", r"\bnot what i (asked|wanted|meant)\b",
                       r"\bthat'?s not what i\b", r"\bas i (said|mentioned|asked)\b"],
    "too_verbose": [r"\btoo (long|verbose|much|wordy|detailed)\b", r"\bshorter\b", r"\bbe (brief|concise)\b", r"\bless (output|text|explanation)\b",
                    r"\bstop (explaining|narrating|summari[sz]ing)\b", r"\btl;?dr\b", r"\bjust (the|give me the) (answer|code|diff)\b"],
    "scope_creep": [r"\bi didn'?t ask (you )?(for|to)\b", r"\b(don'?t|do not) (touch|change|modify|refactor)\b", r"\bonly (change|touch|the)\b",
                    r"\bwhy did you (change|touch|add|refactor|delete)\b", r"\btoo (many|much) change", r"\bundo (the|those|that)\b", r"\brevert\b"],
    "wrong_approach": [r"\bstill (broken|failing|wrong|not working|doesn'?t work)\b", r"\bdidn'?t work\b", r"\bthat'?s wrong\b", r"\bincorrect\b",
                       r"\bnot (right|correct)\b", r"^(no|nope|wrong)\b", r"\bbroke\b", r"\bregression\b"],
    "needed_clarification": [r"\byou should have asked\b", r"\bwhy didn'?t you ask\b", r"\bshould'?ve (asked|checked)\b", r"\bnext time ask\b"],
}

POSITIVE_PATTERNS: List[str] = [r"\b(thanks|thank you|perfect|great|nice|awesome|works|working now|lgtm|looks good|exactly|love it|good job|well done)\b"]

PATH_RE = re.compile(
    r"(?<![\w/])((?:[\w.\-~]+/)*[\w.\-]+\.(?:py|pyi|ts|tsx|js|jsx|mjs|cjs|go|rs|rb|java|kt|kts|swift|c|h|cpp|hpp|cc|cs|php|scala|"
    r"md|mdx|json|ya?ml|toml|sh|zsh|bash|sql|css|scss|less|html|vue|svelte|txt|env|cfg|ini|xml|proto|graphql|tf|lock))(?::(\d+))?\b")
FENCE_RE = re.compile(r"```([\w+-]*)[ \t]*\n(.*?)```", re.S)


def count_filler(text: str) -> Dict[str, Any]:
    low = text.lower()
    out: Dict[str, Any] = {"total": 0, "by_group": {}, "examples": []}
    for group, pats in FILLER_GROUPS.items():
        n = 0
        for p in pats:
            for m in re.finditer(p, low):
                n += 1
                if len(out["examples"]) < 8:
                    out["examples"].append(m.group(0))
        if n:
            out["by_group"][group] = n
            out["total"] += n
    return out


def find_risks(text: str) -> List[str]:
    low = text.lower()
    return [name for name, pat in RISK_PATTERNS.items() if re.search(pat, low)]


def find_secrets(text: str) -> List[str]:
    kinds = []
    for pat in SECRET_PATTERNS:
        if re.search(pat, text):
            kinds.append(pat[:24] + "...")
    return kinds


def redact_secrets(text: str) -> str:
    for pat in SECRET_PATTERNS:
        text = re.sub(pat, "[REDACTED]", text)
    return text


def task_type_heuristic(text: str) -> Tuple[str, Dict[str, int]]:
    low = text.lower()
    scores: Dict[str, int] = {}
    for name, pat in TASK_KEYWORDS:
        n = len(re.findall(pat, low))
        if n:
            scores[name] = n
    if not scores:
        return "other", scores
    # bugfix and test outrank the generic 'feature' verbs when tied
    order = ["bugfix", "test", "refactor", "review", "explain", "research", "docs", "ops", "feature"]
    best = max(scores.items(), key=lambda kv: (kv[1], -order.index(kv[0])))
    return best[0], scores


def find_paths(text: str, cwd: str) -> Dict[str, List[str]]:
    exists, missing = [], []
    seen = set()
    for m in PATH_RE.finditer(text):
        p = m.group(1)
        if p in seen or p.startswith("http"):
            continue
        seen.add(p)
        cand = os.path.expanduser(p)
        full = cand if os.path.isabs(cand) else os.path.join(cwd, cand)
        (exists if os.path.exists(full) else missing).append(p)
    return {"exists": exists[:20], "missing": missing[:20]}


def git_root(cwd: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True, timeout=2)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def locate_pasted_code(block: str, cwd: str) -> Optional[str]:
    """Try to find where a pasted code block came from, cheaply, via git grep on one distinctive line."""
    root = git_root(cwd)
    if not root:
        return None
    lines = [ln.strip() for ln in block.splitlines() if len(ln.strip()) >= 24 and not ln.strip().startswith(("#", "//", "*"))]
    if not lines:
        return None
    probe = max(lines, key=len)
    try:
        r = subprocess.run(["git", "grep", "-lF", "--", probe], cwd=root, capture_output=True, text=True, timeout=2.5)
        hits = [h for h in r.stdout.splitlines() if h.strip()]
        return hits[0] if len(hits) == 1 else (hits[0] + " (+%d more)" % (len(hits) - 1) if hits else None)
    except Exception:
        return None


def fenced_blocks(text: str, cwd: str) -> List[Dict[str, Any]]:
    out = []
    for m in FENCE_RE.finditer(text):
        body = m.group(2)
        n_lines = len(body.strip().splitlines())
        entry: Dict[str, Any] = {"lang": m.group(1) or "", "lines": n_lines, "tokens": estimate_tokens(body)}
        if n_lines >= 6:
            src = locate_pasted_code(body, cwd)
            if src:
                entry["looks_like"] = src
        out.append(entry)
    return out


def claude_md_files(cwd: str) -> List[str]:
    found = []
    home = os.path.expanduser("~")
    for p in (os.path.join(home, ".claude", "CLAUDE.md"),):
        if os.path.exists(p):
            found.append(p)
    root = git_root(cwd) or cwd
    d = os.path.abspath(cwd)
    visited = set()
    while True:
        for name in ("CLAUDE.md", os.path.join(".claude", "CLAUDE.md"), "CLAUDE.local.md"):
            p = os.path.join(d, name)
            if os.path.exists(p) and p not in found:
                found.append(p)
        visited.add(d)
        if d == root or os.path.dirname(d) == d or len(visited) > 8:
            break
        d = os.path.dirname(d)
    return found


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower())


def claude_md_overlap(prompt: str, files: List[str]) -> List[Dict[str, str]]:
    """Sentences in the prompt that restate something already in a CLAUDE.md (5-word shingle match)."""
    corpus = ""
    for p in files:
        try:
            with open(p, encoding="utf-8", errors="ignore") as f:
                corpus += " " + _norm(f.read())
        except Exception:
            pass
    corpus = re.sub(r"\s+", " ", corpus)
    if not corpus.strip():
        return []
    hits = []
    for sent in re.split(r"(?<=[.!?\n])\s+", prompt):
        words = _norm(sent).split()
        if len(words) < 6:
            continue
        for i in range(len(words) - 4):
            shingle = " ".join(words[i:i + 5])
            if shingle in corpus:
                hits.append({"sentence": sent.strip()[:120], "matched": shingle})
                break
        if len(hits) >= 6:
            break
    return hits


def is_ack(prompt: str, max_words: int) -> bool:
    p = prompt.strip().lower()
    if len(p.split()) > max_words:
        return False
    return any(re.match(pat, p) for pat in ACK_PATTERNS)


def correction_heuristic(prompt: str) -> Tuple[str, Optional[str]]:
    """Returns (kind, cause). kind in correction|ok_explicit|new_task."""
    low = prompt.strip().lower()
    for cause, pats in CORRECTION_PATTERNS.items():
        if any(re.search(p, low) for p in pats):
            return "correction", cause
    if any(re.search(p, low) for p in POSITIVE_PATTERNS):
        return "ok_explicit", None
    return "new_task", None


def triage_heuristic(prompt: str, analysis: Dict[str, Any], cfg: Dict[str, Any]) -> Tuple[str, str]:
    p = prompt.strip()
    words = p.split()
    if p.startswith("/"):
        return "skip", "slash command"
    if is_ack(p, cfg["hook"]["skip_under_words"]):
        return "skip", "short acknowledgement or one-line command"
    if len(words) <= 3:
        return "skip", "too short to optimize"
    if analysis["tokens"] < 60 and analysis["filler"]["total"] <= 1 and not analysis["fenced_blocks"] and not analysis["claude_md_overlap"]:
        return "light", "short and already tight"
    return "full", "long, vague, or carries pasted material"


# --------------------------------------------------------------------------- repetition, shortcuts, verify hints

STOPWORDS = set("""a an the and or but so to of in on at for with from by as is are was were be been being am do does did
have has had can could would should will shall may might must i me my we our you your it its this that these those there here
what whats what's which who whom how when where why please just now then also again still yet ok okay hey hi hello thanks thank
lets let's let me us go ahead up out any some all more most next last new current currently pending done left remaining
is there anything we should""".split())

CONTENT_KEEP = {"next", "pending", "status", "tests", "test", "commit", "push", "build", "lint", "deploy", "review", "summary",
                "todo", "todos", "remaining", "left", "done", "progress", "plan"}


def content_words(text: str) -> List[str]:
    words = re.findall(r"[a-z0-9][a-z0-9'\-]*", text.lower())
    out = []
    for w in words:
        w = w.strip("'-")
        if not w:
            continue
        if w in CONTENT_KEEP or (w not in STOPWORDS and len(w) > 2):
            out.append(w)
    return out


def fingerprint(text: str) -> str:
    """Order-insensitive content-word fingerprint: 'what's pending and what's next?' -> 'next pending'.
    Secrets are redacted first so a key can never land in memory through the fingerprint."""
    return " ".join(sorted(set(content_words(redact_secrets(text)))))[:400]


def jaccard(a: str, b: str) -> float:
    sa, sb = set(a.split()), set(b.split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def record_prompt_seen(prompt: str, session: str, source: str) -> None:
    """Append a fingerprint of a prompt (never the full text) so repetition can be detected later."""
    ensure_memory()
    if find_secrets(prompt):
        return
    fp = fingerprint(prompt)
    if not fp or len(prompt.split()) > 60 or "```" in prompt:
        return
    cfg = load_config()
    preview = redact_secrets(prompt)[:120] if cfg.get("store_previews") else ""
    with open(PROMPTS_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ts": now_iso(), "session": session, "source": source, "fp": fp,
                            "preview": preview, "words": len(prompt.split())}, ensure_ascii=False) + "\n")


def read_prompts_seen() -> List[Dict[str, Any]]:
    if not os.path.exists(PROMPTS_PATH):
        return []
    out = []
    with open(PROMPTS_PATH, encoding="utf-8") as f:
        for line in f:
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def load_shortcuts() -> Dict[str, Dict[str, Any]]:
    ensure_memory()
    try:
        with open(SHORTCUTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_shortcuts(d: Dict[str, Dict[str, Any]]) -> None:
    write_json(SHORTCUTS_PATH, d)


def match_shortcut(prompt: str, shortcuts: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """Exact match on the shortcut key (case-insensitive, trailing punctuation ignored)."""
    key = re.sub(r"[\s.!?]+$", "", prompt.strip().lower())
    key = key[len("/whisper "):] if key.startswith("/whisper ") else key
    for k in shortcuts:
        if key == k.lower():
            return k
    return None


def repeated_prompts(min_count: int = 3, sim: float = 0.7) -> List[Dict[str, Any]]:
    """Clusters of near-identical prompts seen >= min_count times that no shortcut covers yet."""
    seen = read_prompts_seen()
    shortcuts = load_shortcuts()
    covered = {fingerprint(s.get("expands_to", "")) for s in shortcuts.values()} | {fingerprint(k) for k in shortcuts}
    for s in shortcuts.values():
        for src in s.get("learned_from", []):
            covered.add(fingerprint(src))
    clusters: List[Dict[str, Any]] = []
    for p in seen:
        fp = p["fp"]
        if fp in covered or len(fp.split()) < 1:
            continue
        for c in clusters:
            if fp == c["fp"] or jaccard(fp, c["fp"]) >= sim:
                c["count"] += 1
                c["examples"].append(p["preview"])
                c["words"] += p.get("words", 0)
                break
        else:
            clusters.append({"fp": fp, "count": 1, "examples": [p["preview"]], "words": p.get("words", 0)})
    out = []
    for c in clusters:
        # a shortcut only pays off when the prompt is long enough to be worth abbreviating
        if c["count"] >= min_count and c["words"] / c["count"] >= 5:
            ex = sorted({e for e in c["examples"] if e}, key=len)
            out.append({"fp": c["fp"], "count": c["count"], "examples": ex[:3] or ["(previews off; content words: %s)" % c["fp"]]})
    return sorted(out, key=lambda c: -c["count"])


def verify_hints(cwd: str) -> Dict[str, Any]:
    """Cheap guesses at how to verify work in this repo, so the rewrite can name a check instead of asking."""
    root = git_root(cwd) or cwd
    hints: Dict[str, Any] = {}
    def exists(*names: str) -> bool:
        return any(os.path.exists(os.path.join(root, n)) for n in names)
    try:
        pj = os.path.join(root, "package.json")
        if os.path.exists(pj):
            with open(pj, encoding="utf-8") as f:
                scripts = (json.load(f).get("scripts") or {})
            runner = "pnpm" if exists("pnpm-lock.yaml") else "bun" if exists("bun.lockb", "bun.lock") else "yarn" if exists("yarn.lock") else "npm"
            for s in ("test", "lint", "typecheck", "build", "check"):
                if s in scripts:
                    hints[s] = "%s run %s" % (runner, s) if runner != "npm" else "npm run %s" % s if s != "test" else "npm test"
    except Exception:
        pass
    if exists("pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini") or os.path.isdir(os.path.join(root, "tests")):
        hints.setdefault("test", "pytest" if not exists("uv.lock") else "uv run pytest")
    if exists("Cargo.toml"):
        hints.setdefault("test", "cargo test"); hints.setdefault("build", "cargo build")
    if exists("go.mod"):
        hints.setdefault("test", "go test ./..."); hints.setdefault("build", "go build ./...")
    if exists("Makefile"):
        try:
            with open(os.path.join(root, "Makefile"), encoding="utf-8", errors="ignore") as f:
                targets = re.findall(r"^([a-zA-Z_\-]+):", f.read(), flags=re.M)
            for t in ("test", "lint", "check", "build"):
                if t in targets:
                    hints.setdefault(t, "make " + t)
        except Exception:
            pass
    return hints


Q_FOLLOWUP: Dict[str, Dict[str, Any]] = {
    "followup": {
        "type": "choice",
        "instructions": "The user's NEW message comes right after the assistant finished a task. Which routine follow-up is it, if any?",
        "criteria": {
            "commit": "Asks to commit, push, or open a PR",
            "tests": "Asks to add, run, or fix tests",
            "lint": "Asks to lint, format, or typecheck",
            "docs": "Asks to update docs, README, comments, or changelog",
            "explain": "Asks to explain or summarize what was done",
            "shorten": "Asks for a shorter or less verbose answer",
            "cleanup": "Asks to remove temp files, debug output, or leftovers",
            "status": "Asks what is pending, what is next, or for a status summary",
            "unrelated": "A different task, not a routine follow-up",
        },
    },
}

FOLLOWUP_KEYWORDS: List[Tuple[str, str]] = [
    ("commit", r"\b(commit|push|open (a )?pr|pull request)\b"),
    ("tests", r"\b(add|write|run|fix)\b.{0,20}\btests?\b|\btests?\b.{0,12}\b(pass|fail)"),
    ("lint", r"\b(lint|format|prettier|typecheck|type-check|mypy|eslint|ruff)\b"),
    ("docs", r"\b(readme|docs?|documentation|changelog|comments?)\b"),
    ("explain", r"\b(explain|summari[sz]e|what did you (do|change)|walk me through)\b"),
    ("shorten", r"\b(shorter|too long|tl;?dr|concise|brief)\b"),
    ("cleanup", r"\b(clean ?up|remove (the )?(temp|debug|print|console)|leftover)\b"),
    ("status", r"\b(pending|what'?s next|status|remaining|where are we|progress)\b"),
]


def followup_heuristic(prompt: str) -> str:
    low = prompt.lower()
    for kind, pat in FOLLOWUP_KEYWORDS:
        if re.search(pat, low):
            return kind
    return "unrelated"


# --------------------------------------------------------------------------- Jev (TypeSafe AI) - optional

def jev_available(cfg: Dict[str, Any]) -> bool:
    return bool(cfg["jev"].get("enabled", True)) and bool(os.environ.get("TYPESAFE_API_KEY"))


def read_jev_status() -> Dict[str, Any]:
    try:
        with open(JEV_STATUS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _jev_status_update(ok: bool, ms: int, err: str = "", data: Optional[Dict[str, Any]] = None,
                       requested: str = "") -> None:
    """Counters plus the model that actually answered.

    The response carries a top-level `model`. Recording it is the only way to notice that a tuned
    threshold is now being applied to a different model's probabilities."""
    try:
        with memory_lock(timeout_s=1.0):
            st = read_jev_status()
            st["calls"] = st.get("calls", 0) + 1
            st["failures"] = st.get("failures", 0) + (0 if ok else 1)
            st["last_ms"] = ms
            st["last_error"] = err[:200] if err else ""
            st["last_at"] = now_iso()
            if data:
                reported = data.get("model") or ""
                if reported:
                    st["model_reported"] = reported
                    st["model_requested"] = requested
                    seen = st.setdefault("models_seen", {})
                    seen[reported] = int(seen.get(reported, 0)) + 1
                    # an alias is expected to resolve to something else; a pinned id is not
                    st["version_drift"] = bool(requested and not requested.endswith(("latest", "preview"))
                                               and reported != requested)
                usage = data.get("usage") or {}
                if usage:
                    st["tokens_in_total"] = st.get("tokens_in_total", 0) + int(usage.get("input_tokens", 0) or 0)
                    st["tokens_out_total"] = st.get("tokens_out_total", 0) + int(usage.get("output_tokens", 0) or 0)
            write_json(JEV_STATUS_PATH, st)
    except Exception:
        pass


def jev_ask(state: Any, questions: Dict[str, Dict[str, Any]], cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """POST to TypeSafe System One. Returns answers dict or None on any failure. Never raises."""
    if not jev_available(cfg):
        return None
    body = {"model": cfg["jev"]["model"], "state": state, "questions": questions}
    req = urllib.request.Request(
        cfg["jev"]["endpoint"], data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"], "Content-Type": "application/json",
                 "User-Agent": "claude-whisperer/1.0"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=cfg["jev"]["timeout_s"]) as r:
            data = json.load(r)
        _jev_status_update(True, int((time.time() - t0) * 1000), data=data, requested=cfg["jev"]["model"])
        return data.get("answers")
    except urllib.error.HTTPError as e:
        _jev_status_update(False, int((time.time() - t0) * 1000), "HTTP %s" % e.code)
    except Exception as e:  # timeout, network, parse
        _jev_status_update(False, int((time.time() - t0) * 1000), type(e).__name__ + ": " + str(e))
    return None


Q_ANALYZE: Dict[str, Dict[str, Any]] = {
    "triage": {
        "type": "choice",
        "instructions": "How much rewriting does this message to a coding agent need before the agent acts on it?",
        "criteria": {
            "skip": "A short reply, acknowledgement, answer to the agent's question, or a one-line command that is already precise. Rewriting would add nothing.",
            "light": "A clear task that only needs filler trimmed or a bounded reply format added.",
            "full": "A task with vague scope, missing done-criteria, pasted material, restated context, backstory, or several tasks mixed together.",
        },
    },
    "task_type": {
        "type": "choice",
        "instructions": "What kind of work is being requested?",
        "criteria": {
            "bugfix": "Fix incorrect behavior, an error, or a failing test",
            "feature": "Add new behavior or capability",
            "refactor": "Restructure code without changing behavior",
            "explain": "Explain or answer a question about code or a concept",
            "review": "Review, audit, or critique code or a design",
            "research": "Investigate, compare options, or find information",
            "test": "Write or fix tests, improve coverage",
            "docs": "Write or update documentation",
            "ops": "Configure, install, set up, deploy, CI/CD, environment",
            "other": "None of the above",
        },
    },
    "risky": {
        "type": "noul",
        "instructions": "The requested work involves destructive or hard-to-reverse actions: deleting files, changing a database schema, force-pushing or rewriting git history, deploying or publishing, sending messages or payments, or handling secrets.",
    },
    "multi_task": {
        "type": "noul",
        "instructions": "The message asks for more than one independent task that could be done separately.",
    },
}

Q_GATE: Dict[str, Dict[str, Any]] = {
    "intent_drop": {
        "type": "noul",
        "instructions": "Compare ORIGINAL and REWRITTEN prompts. The rewrite drops, weakens, or changes a requirement, constraint, requested output, or specific detail that the original asked for.",
        "criteria": {
            "true": "Something the user asked for is missing, weakened, or altered in the rewrite",
            "false": "The rewrite preserves everything the user asked for; it only removed filler or restated context, or added structure",
        },
    },
}

Q_OUTCOME: Dict[str, Dict[str, Any]] = {
    "kind": {
        "type": "choice",
        "instructions": "How does the NEW user message relate to the assistant's previous piece of work (described in PREVIOUS)?",
        "criteria": {
            "correction": "The user is correcting, complaining about, or asking to redo the previous result",
            "new_task": "The user moves on to a new or next task",
            "acknowledgement": "The user accepts, approves, or thanks for the previous result",
            "reply": "The user answers a question the assistant asked",
        },
    },
    "cause": {
        "type": "choice",
        "instructions": "If the NEW message is a correction, what went wrong with the previous result?",
        "criteria": {
            "dropped_detail": "The assistant missed or ignored something the user had asked for",
            "too_verbose": "The output was too long or over-explained",
            "scope_creep": "The assistant did more than asked or touched things it should not have",
            "wrong_approach": "The result is incorrect or does not work",
            "needed_clarification": "The assistant should have asked before acting",
            "none": "Not a correction",
        },
    },
}


def _answer(answers: Optional[Dict[str, Any]], key: str) -> Optional[Dict[str, Any]]:
    if not answers:
        return None
    a = answers.get(key)
    return a if isinstance(a, dict) else None


# --------------------------------------------------------------------------- analysis

def analyze_prompt(prompt: str, cwd: str, cfg: Dict[str, Any], use_jev: bool = True) -> Dict[str, Any]:
    cwd = cwd or os.getcwd()
    a: Dict[str, Any] = {
        "id": "a-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + hashlib.sha1(prompt.encode("utf-8")).hexdigest()[:6],
        "ts": now_iso(),
        "cwd": cwd,
        "project": os.path.basename(git_root(cwd) or cwd),
        "chars": len(prompt),
        "words": len(prompt.split()),
        "tokens": estimate_tokens(prompt),
        "filler": count_filler(prompt),
        "risks": find_risks(prompt),
        "secrets_detected": bool(find_secrets(prompt)),
        "paths": find_paths(prompt, cwd),
        "fenced_blocks": fenced_blocks(prompt, cwd),
        "multi_task_hint": bool(re.search(r"\b(and then|after that|additionally|secondly|finally|as well as|also (?!(please )?(explain|summari[sz]e|tell|show|describe|walk|let me know)))\b", prompt.lower())) and len(prompt.split()) > 25,
        "questions_in_prompt": prompt.count("?"),
    }
    files = claude_md_files(cwd)
    a["claude_md_files"] = files
    a["claude_md_overlap"] = claude_md_overlap(prompt, files)
    a["verify_hints"] = verify_hints(cwd)
    shortcuts = load_shortcuts()
    a["shortcut_match"] = match_shortcut(prompt, shortcuts)
    a["fingerprint"] = fingerprint(prompt)
    a["seen_before"] = sum(1 for p in read_prompts_seen() if p["fp"] == a["fingerprint"] or jaccard(p["fp"], a["fingerprint"]) >= 0.7)

    tt, scores = task_type_heuristic(prompt)
    a["task_type"] = {"value": tt, "source": "heuristic", "confidence": None, "keyword_scores": scores}
    a["risk"] = {"value": bool(a["risks"]), "source": "heuristic", "flags": a["risks"]}

    tri, why = triage_heuristic(prompt, a, cfg)
    a["triage"] = {"value": tri, "source": "heuristic", "reason": why}

    a["jev"] = {"available": jev_available(cfg), "used": False, "model": cfg["jev"]["model"]}
    if use_jev and a["jev"]["available"] and tri != "skip":
        answers = jev_ask({"prompt": prompt[:12000]}, Q_ANALYZE, cfg)
        if answers:
            a["jev"]["used"] = True
            st = read_jev_status()
            a["jev"]["model_answered"] = st.get("model_reported", "")
            a["jev"]["version_drift"] = bool(st.get("version_drift"))
            t = _answer(answers, "triage")
            if t and t.get("choice") and (t.get("confidence") or 0) >= cfg["jev"]["task_min_confidence"]:
                a["triage"] = {"value": t["choice"], "source": "jev", "confidence": round(t.get("confidence", 0), 2), "reason": "jev"}
            k = _answer(answers, "task_type")
            if k and k.get("choice") and (k.get("confidence") or 0) >= cfg["jev"]["task_min_confidence"]:
                a["task_type"] = {"value": k["choice"], "source": "jev", "confidence": round(k.get("confidence", 0), 2), "keyword_scores": scores}
            r = _answer(answers, "risky")
            if r and r.get("noul") is not None:
                a["risk"] = {"value": (r["noul"] >= 0.5) or bool(a["risks"]), "source": "jev+heuristic", "flags": a["risks"], "jev_p": round(r["noul"], 2)}
            m = _answer(answers, "multi_task")
            if m and m.get("noul") is not None:
                a["multi_task_hint"] = m["noul"] >= 0.5
                a["multi_task_p"] = round(m["noul"], 2)

    a["rules"] = relevant_rules(load_rules(), a)
    a["learn_due"] = learn_due(cfg)
    a["hook_on"] = hook_installed()
    return a


def hook_installed() -> bool:
    try:
        p = os.path.expanduser("~/.claude/settings.json")
        with open(p, encoding="utf-8") as f:
            settings = json.load(f)
        return any("claude-whisperer" in str(h.get("command", ""))
                   for g in settings.get("hooks", {}).get("UserPromptSubmit", []) for h in g.get("hooks", []))
    except Exception:
        return False


# --------------------------------------------------------------------------- learnings.md (rules)

LEARNINGS_TEMPLATE = """# Claude Whisperer - learned rules

Managed by `scripts/whisper.py`; hand edits are fine as long as each rule stays on one line
and keeps its `[Rnnn]` id. Rules are read at the start of every run and applied to the rewrite.
Format: `- [R001] (source, created, applied N, corrections M) rule text`

## Rules

## Candidates

## Retired
"""

RULE_RE = re.compile(r"^- \[(?P<kind>[RC])(?P<num>\d{3,})\] \((?P<meta>[^)]*)\) (?P<text>.+?)\s*$")


def load_learnings_text() -> str:
    ensure_memory()
    with open(LEARNINGS_PATH, encoding="utf-8") as f:
        return f.read()


def parse_learnings(text: str) -> Dict[str, List[Dict[str, Any]]]:
    sections: Dict[str, List[Dict[str, Any]]] = {"Rules": [], "Candidates": [], "Retired": []}
    current = None
    for line in text.splitlines():
        h = re.match(r"^## (Rules|Candidates|Retired)\s*$", line)
        if h:
            current = h.group(1)
            continue
        m = RULE_RE.match(line)
        if m and current:
            meta = [x.strip() for x in m.group("meta").split(",")]
            entry: Dict[str, Any] = {"id": m.group("kind") + m.group("num"), "text": m.group("text"), "source": meta[0] if meta else "?",
                                     "created": meta[1] if len(meta) > 1 else "", "applied": 0, "corrections": 0, "extra": []}
            for part in meta[2:]:
                mm = re.match(r"(applied|corrections|n)\s+(\d+)", part)
                if mm:
                    key = "applied" if mm.group(1) == "n" else mm.group(1)
                    entry[key] = int(mm.group(2))
                else:
                    entry["extra"].append(part)
            sections[current].append(entry)
    return sections


def render_learnings(sections: Dict[str, List[Dict[str, Any]]]) -> str:
    head = LEARNINGS_TEMPLATE.split("## Rules")[0]
    out = [head.rstrip("\n"), ""]
    for name in ("Rules", "Candidates", "Retired"):
        out.append("## " + name)
        for e in sections[name]:
            meta = [e.get("source", "?"), e.get("created", "")]
            if name == "Candidates":
                meta.append("n %d" % e.get("applied", 0))
            else:
                meta.append("applied %d" % e.get("applied", 0))
                meta.append("corrections %d" % e.get("corrections", 0))
            meta.extend(e.get("extra", []))
            out.append("- [%s] (%s) %s" % (e["id"], ", ".join(m for m in meta if m != ""), one_line(e["text"])))
        out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def load_rules() -> Dict[str, List[Dict[str, Any]]]:
    return parse_learnings(load_learnings_text())


def save_rules(sections: Dict[str, List[Dict[str, Any]]]) -> None:
    atomic_write(LEARNINGS_PATH, render_learnings(sections))


def next_id(sections: Dict[str, List[Dict[str, Any]]], kind: str) -> str:
    nums = [int(e["id"][1:]) for sec in sections.values() for e in sec if e["id"].startswith(kind)]
    return "%s%03d" % (kind, (max(nums) + 1) if nums else 1)


def relevant_rules(sections: Dict[str, List[Dict[str, Any]]], analysis: Dict[str, Any]) -> List[Dict[str, str]]:
    """All active rules, with the ones tagged for this task type / project first. Rules are short, so send them all."""
    tt = analysis.get("task_type", {}).get("value", "")
    proj = analysis.get("project", "")
    def key(e: Dict[str, Any]) -> int:
        txt = e["text"].lower()
        score = 0
        if tt and (("task=" + tt) in txt or ("[" + tt + "]") in txt):
            score -= 2
        if proj and proj.lower() in txt:
            score -= 3
        return score
    rules = sorted(sections["Rules"], key=key)
    return [{"id": e["id"], "text": e["text"]} for e in rules]


def bump_rules(ids: List[str], field: str) -> None:
    if not ids:
        return
    with memory_lock():
        sections = load_rules()
        for e in sections["Rules"]:
            if e["id"] in ids:
                e[field] = int(e.get(field, 0)) + 1
        save_rules(sections)


# --------------------------------------------------------------------------- log

def read_log() -> List[Dict[str, Any]]:
    ensure_memory()
    out = []
    with open(LOG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


def append_log(rec: Dict[str, Any]) -> None:
    ensure_memory()
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def rewrite_log(records: List[Dict[str, Any]]) -> None:
    atomic_write(LOG_PATH, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records))


def find_pending(records: List[Dict[str, Any]], session: Optional[str] = None, max_age_s: int = 6 * 3600) -> Optional[int]:
    """Index of the most recent run that is still open (the user has not moved on to a new task), preferring the same session."""
    now = time.time()
    for i in range(len(records) - 1, -1, -1):
        r = records[i]
        if r.get("closed") or r.get("dry"):
            continue
        try:
            ts = datetime.strptime(r["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        except Exception:
            ts = now
        if now - ts > max_age_s:
            return None
        if session and r.get("session") and r["session"] != session:
            continue
        return i
    return None


def learn_due(cfg: Dict[str, Any]) -> bool:
    recs = read_log()
    real = [r for r in recs if not r.get("dry")]
    if not real:
        return False
    last = 0
    marker = os.path.join(MEMORY_DIR, "last_learn.json")
    if os.path.exists(marker):
        try:
            with open(marker, encoding="utf-8") as f:
                last = json.load(f).get("runs", 0)
        except Exception:
            last = 0
    since = len(real) - last
    labeled = sum(1 for r in real[last:] if r.get("outcome") in ("ok", "correction"))
    return since >= cfg["consolidate_every"] and labeled >= 3


def mark_learned() -> None:
    write_json(os.path.join(MEMORY_DIR, "last_learn.json"), {"runs": len([r for r in read_log() if not r.get("dry")]), "at": now_iso()})
