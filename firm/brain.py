"""Claude CLI brain — runs every analyst, debater, and the PM judge on config.CLAUDE_MODEL
(Fable 5.1 at max effort since 2026-09-01; Opus 5 before that).

Uses the local `claude` CLI in print mode (subscription-billed, no API key).
All prompts demand strict JSON; extraction is defensive.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import config



_OFFICE_ROOT = (Path(os.environ["LOCH_RAVEN_OFFICE_ROOT"])
                if os.environ.get("LOCH_RAVEN_OFFICE_ROOT") else None)

def _office_module(name):
    """Use the canonical shared policy when installed, fail closed if broken."""
    if _OFFICE_ROOT is None or not (_OFFICE_ROOT / "office").is_dir():
        return None
    # Engines run from their own repo, which is not the Office import path.
    if str(_OFFICE_ROOT) not in sys.path:
        sys.path.insert(0, str(_OFFICE_ROOT))
    import importlib
    return importlib.import_module("office." + name)


_CLAUDE_EXE: str | None = None

# Quota/usage-limit awareness (operator 2026-06-25: the Opus subscription ran dry
# and every brain call failed -> the whole cycle degraded, but the fund couldn't
# tell a QUOTA outage from a crash). When the CLI exits non-zero with a usage-limit
# signature in stderr we remember it, so the cycle can report "Opus quota exhausted,
# will retry after reset" instead of a vague "degraded" and skip stamping the cycle
# fresh (so it re-runs for real once the quota window resets).
# 2026-09-22: the CLI's newer sentences, "You've hit your weekly limit" and
# "You've hit your session limit", matched none of these, so a weekly outage
# never climbed the ladder. "hit your ... limit" is now read as quota too.
_QUOTA_RE = re.compile(
    r"usage limit|usage_limit|rate.?limit|quota|exceeded your|too many requests|"
    r"limit reached|reached your|hit your [\w .'-]{0,40}?limit|out of (?:credits?|tokens?)", re.I)
_last_quota_hit_ts: float = 0.0

# Auth-expiry awareness (operator 2026-07-03: the subscription OAuth login in
# ~/.claude/.credentials.json expired -> every `claude -p` returned HTTP 401
# "Invalid authentication credentials" and the whole research floor degraded to
# quant-only for a full day, yet the fund reported a vague "CLI/network outage"
# with no actionable cause. A 401 is NOT a quota outage: it does NOT self-heal on
# a reset window — it needs an interactive `claude login`. Detect it distinctly so
# the cycle alert and the health desk say exactly that.)
_AUTH_RE = re.compile(
    r"invalid authentication|failed to authenticate|authentication_error|"
    r"\bunauthorized\b|invalid api key|invalid[^.\"]{0,24}credential|"
    r"api_error_status\"?\s*:?\s*401|oauth[^.\"]{0,24}(?:expire|invalid|revoke)|"
    r"run\s+`?claude login`?", re.I)
_last_auth_fail_ts: float = 0.0
_auth_failing: bool = False   # module view of last-persisted CLI-auth state


def _record_auth(ok: bool) -> None:
    """Persist CLI-auth health to state/cli_auth.json so the 30-min health sweep
    (a separate call from the cycle that saw the 401) and the operator can see a
    login-expiry outage — and so it AUTO-CLEARS the moment a call succeeds again
    after `claude login`. Writes only on transition to avoid churn."""
    global _auth_failing
    if (not ok) == _auth_failing:
        return
    _auth_failing = not ok
    try:
        from datetime import datetime, timezone
        from firm import state
        state.save("cli_auth", {"ok": ok,
                                "ts": datetime.now(timezone.utc).isoformat()})
    except Exception:
        pass


def _note_failure(stderr: str | None) -> None:
    global _last_quota_hit_ts, _last_auth_fail_ts
    if not stderr:
        return
    if _QUOTA_RE.search(stderr):
        _last_quota_hit_ts = time.monotonic()
    if _AUTH_RE.search(stderr):
        _last_auth_fail_ts = time.monotonic()
        _record_auth(False)


def quota_recently_hit(within_s: float = 180.0) -> bool:
    """True if a brain call recently failed with a quota/usage-limit signature, so
    the cycle can classify a degraded run as an Opus-subscription outage (which
    self-heals on reset) rather than a crash/stall."""
    return bool(_last_quota_hit_ts) and (time.monotonic() - _last_quota_hit_ts) < within_s


def auth_recently_failed(within_s: float = 180.0) -> bool:
    """True if a brain call recently failed with an auth/401 signature — a login
    expiry that needs an interactive `claude login`, distinct from a quota window
    that self-heals on reset. Lets the cycle name the REAL, actionable cause."""
    return bool(_last_auth_fail_ts) and (time.monotonic() - _last_auth_fail_ts) < within_s


def _resolve_cli() -> list[str]:
    global _CLAUDE_EXE
    if _CLAUDE_EXE is None:
        _CLAUDE_EXE = shutil.which("claude") or "claude"
    if sys.platform == "win32" and _CLAUDE_EXE.lower().endswith((".cmd", ".bat")):
        return ["cmd", "/c", _CLAUDE_EXE]
    return [_CLAUDE_EXE]


# Set False the first time the installed CLI rejects the isolation flags
# (auto-update could remove them) — the fund must degrade to the old full-
# context call, never to a dead brain.
_safe_mode_ok: bool = True
# Same contract for --effort (operator 2026-09-01): if the CLI ever rejects the
# flag, drop it for the rest of this process and retry — never a dead brain.
_effort_ok: bool = True
_UNKNOWN_FLAG_RE = re.compile(r"unknown (?:option|argument)|unrecognized", re.I)


_SYS_PROMPT_FILE: str | None = None


def _full_system_prompt() -> str:
    """Persona + the institutional finance doctrine — the curated, verified best
    of the fund's finance skills that must ride EVERY Opus call (operator
    2026-07-02: 'every call should carry the relevant finance knowledge')."""
    doc = getattr(config, "BRAIN_FINANCE_DOCTRINE", "")
    return (config.BRAIN_SYSTEM_PROMPT + "\n\n" + doc) if doc else config.BRAIN_SYSTEM_PROMPT


def _system_prompt_path() -> str | None:
    """Write the composed system prompt to a stable file ONCE per process and
    return its path — passed via --system-prompt-file so the ~750-word doctrine
    rides every call without argv-length/escaping limits AND is prompt-cached
    (marginal per-call cost = a cache read, not 750 words re-sent). Returns None
    if the write fails; the caller then falls back to inline --system-prompt."""
    global _SYS_PROMPT_FILE
    if _SYS_PROMPT_FILE is not None:
        return _SYS_PROMPT_FILE or None
    try:
        p = config.STATE_DIR / "brain_system_prompt.txt"
        p.write_text(_full_system_prompt(), encoding="utf-8")
        _SYS_PROMPT_FILE = str(p)
        return _SYS_PROMPT_FILE
    except Exception:
        _SYS_PROMPT_FILE = ""      # remember failure; inline fallback below
        return None


def _safe_mode_args() -> list[str]:
    """Isolation flags (measured 2026-07-02): --safe-mode strips the operator's
    2,818 skills / CLAUDE.md rule files / MCP servers / hooks from the call
    (~127.6k -> ~25k context tokens, API time 5.0s -> 3.1s) while keeping the
    subscription OAuth login (unlike --bare, which requires an API key). The
    fund-domain system prompt + finance doctrine replaces the coding-agent
    persona — the judge attends to the packet, its lessons, and institutional
    research discipline, not to tool documentation."""
    spf = _system_prompt_path()
    sp = (["--system-prompt-file", spf] if spf
          else ["--system-prompt", _full_system_prompt()])
    return ["--safe-mode", *sp, "--exclude-dynamic-system-prompt-sections"]


def _quota_fallback(prompt: str, model: str | None, timeout: int,
                    tried: bool, blob: str) -> str | None:
    """Re-issue THIS call on the fallback model when the failure was a usage
    limit (2026-09-02, operator request).

    Returns the fallback's answer, or None when there is nothing to fall back
    to. Only fires on a QUOTA signature -- an auth failure or a crash must not
    silently burn the fallback model's budget -- and only once per call, so a
    fallback that is itself limited cannot recurse."""
    if tried or model is not None:
        return None                      # already retried, or caller pinned a model
    fb = str(getattr(config, "CLAUDE_FALLBACK_MODEL", "") or "")
    if not fb or not _QUOTA_RE.search(blob or ""):
        return None
    try:
        from firm import state
        state.journal("brain_model_fallback", frm=config.CLAUDE_MODEL, to=fb,
                      why=(blob or "")[:160])
    except Exception:
        pass
    out = ask(prompt, model=fb, timeout=timeout, _fb_tried=True)
    if out is not None:
        # A RESCUED call is not an outage (2026-09-02). _note_failure stamps
        # the quota clock before we get here, so without this every successful
        # fallback still left quota_recently_hit() True -- and the cycle then
        # told the operator "Opus quota exhausted, calls returning quant-only
        # fallback, the cycle will pause and retry on quota reset" while the
        # research floor was in fact completing normally on the fallback model.
        # A detector that fires on the very condition it just recovered from
        # reports an outage that is not happening, and at cycle end would
        # mislabel a good run `opus_quota` and arm a needless retry.
        global _last_quota_hit_ts
        _last_quota_hit_ts = 0.0
    return out


def ask(prompt: str, model: str | None = None,
        timeout: int = config.CLAUDE_TIMEOUT_S,
        _fb_tried: bool = False) -> str | None:
    """One-shot prompt to Claude via CLI. Returns raw text or None on failure."""
    global _safe_mode_ok, _effort_ok, _last_quota_hit_ts
    # Explicit independent-review pins remain separate exact-model calls.
    if model is None and not _fb_tried:
        try:
            shared = _office_module("model_text")
            if shared is not None:
                result = shared.generate(prompt, system=_full_system_prompt(), timeout=timeout)
                if result.status == "ok" and result.text:
                    _last_quota_hit_ts = 0.0
                    if result.model.startswith("claude"):
                        _record_auth(True)
                    try:
                        from firm import state
                        state.journal("brain_model_shared", model=result.model)
                    except Exception:
                        pass
                    return result.text
                detail = str(result.detail or "Shared model returned no text")
                _note_failure(detail)
                from firm import state
                state.journal("brain_model_unavailable", status=result.status, detail=detail[:500])
                return None
        except Exception as exc:
            # Never bypass a broken shared router with a private model ladder.
            _note_failure(str(exc))
            try:
                from firm import state
                state.journal("brain_model_router_error", detail=str(exc)[:500])
            except Exception:
                pass
            return None
    use_safe = bool(getattr(config, "BRAIN_SAFE_MODE", True)) and _safe_mode_ok
    cmd = _resolve_cli() + ["-p", "--model", model or config.CLAUDE_MODEL,
                            "--output-format", "json"]
    _eff = str(getattr(config, "CLAUDE_EFFORT", "") or "")
    if _eff and _effort_ok:
        cmd += ["--effort", _eff]
    if use_safe:
        cmd += _safe_mode_args()
    # STAGED (off by default): the CLI can auto-fall back to a cheaper model
    # when Opus is unavailable, keeping REAL committee verdicts flowing on
    # Sonnet through a quota window instead of the quant-composite degraded
    # fallback. Left OFF because a Sonnet verdict is NON-degraded and WOULD
    # trade — an operator decision, not a silent default. Flip
    # config.CLAUDE_FALLBACK_MODEL='sonnet' to enable (2026-07-02 review).
    _fb = getattr(config, "CLAUDE_FALLBACK_MODEL", "") or ""
    if _fb and model is None:
        cmd += ["--fallback-model", _fb]
    try:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            # Hidden console: app.py runs DETACHED (no console), so without this
            # every desk call opened a visible console that Windows Terminal kept
            # as a window (2026-09-17: ~400 stray Terminal windows, box unusable).
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0:
        _err = proc.stderr or ""
        if (_eff and _effort_ok and _UNKNOWN_FLAG_RE.search(_err)
                and "effort" in _err.lower()):
            # CLI does not know --effort — drop it for this process, retry once.
            _effort_ok = False
            try:
                from firm import state
                state.journal("brain_effort_flag_unsupported", stderr=_err[:200])
            except Exception:
                pass
            return ask(prompt, model=model, timeout=timeout, _fb_tried=_fb_tried)
        if use_safe and _UNKNOWN_FLAG_RE.search(_err):
            # CLI no longer knows the isolation flags — drop them for the rest
            # of this process and retry THIS call full-context (journal once).
            _safe_mode_ok = False
            try:
                from firm import state
                state.journal("brain_safe_mode_unsupported",
                              stderr=(proc.stderr or "")[:200])
            except Exception:
                pass
            return ask(prompt, model=model, timeout=timeout, _fb_tried=_fb_tried)
        # A usage-limit signature can land on stderr OR, with --output-format
        # json, on STDOUT (the CLI's error channel is the json envelope) —
        # scan BOTH so quota_recently_hit() classifies the outage instead of
        # misreading a quota window as a crash (adversarial review 2026-07-02).
        _blob = (proc.stderr or "") + "\n" + (proc.stdout or "")
        _note_failure(_blob)
        return _quota_fallback(prompt, model, timeout, _fb_tried, _blob)
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return proc.stdout.strip() or None
    # An in-band error envelope (exit 0, is_error=true) is NOT model output:
    # returning its text would feed the judge an error string AND burn the
    # ask_json retry on a prompt that cannot succeed. Treat it as a failure
    # and let _QUOTA_RE see the message (2026-07-02).
    if isinstance(envelope, dict) and envelope.get("is_error"):
        # This is the shape a Fable usage limit takes: exit 0, is_error true,
        # result "You've reached your Fable limit. Switch to another model."
        _blob = json.dumps(envelope)[:500]
        _note_failure(_blob)
        return _quota_fallback(prompt, model, timeout, _fb_tried, _blob)
    _record_auth(True)   # a non-error envelope means auth is working again
    return envelope.get("result") if isinstance(envelope, dict) else None


def _balanced_objects(text: str) -> list[str]:
    """Every TOP-LEVEL {...} region in text, in order, by a brace-depth scan
    that ignores braces inside strings — so a complete object at the END of a
    reply is recoverable even when earlier prose contains stray/echoed braces."""
    out: list[str] = []
    depth, start = 0, -1
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start != -1:
                out.append(text[start:i + 1])
                start = -1
    return out


def extract_json(text: str) -> dict | None:
    """Pull the model's JSON object out of its reply. The verdict prompts say
    'reason in prose first ... then END your reply with exactly one JSON code
    block', so when several JSON objects exist the LAST one is the answer — a
    draft or an echoed previous_verdict in the working prose must never displace
    it (adversarial review 2026-07-02: re.search grabbed the FIRST fence, which
    silently degraded a real committee verdict to the quant fallback). Order:
    fenced blocks last-first, then balanced {...} regions last-first, then the
    legacy first-{-to-last-} slice as a final catch-all."""
    if not text:
        return None
    candidates: list[str] = []
    fenced = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidates += list(reversed(fenced))                 # 'END your reply' wins
    candidates += list(reversed(_balanced_objects(text)))
    start = text.find("{")
    if start != -1:
        candidates.append(text[start:text.rfind("}") + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    return None


def ask_json(prompt: str, retries: int = 1, **kwargs) -> dict | None:
    for attempt in range(retries + 1):
        text = ask(prompt, **kwargs)
        obj = extract_json(text or "")
        if obj:
            return obj
        if attempt < retries:
            prompt = prompt + "\n\nIMPORTANT: respond with ONLY the JSON object, nothing else."
    return None


def available() -> bool:
    if _OFFICE_ROOT is not None and (_OFFICE_ROOT / "office").is_dir():
        return any(shutil.which(cli) for cli in ("claude", "codex"))
    return shutil.which("claude") is not None
