import asyncio
import json
import math
import os
import random
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import re

import markdown as md
from textwrap import dedent
from fastapi import Cookie, FastAPI, Form, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.session import RESULTS_DIR, SessionStore
from utils import TestCasesSampler, is_valid_email

def _inline(text: str) -> str:
    """Convert **bold** and *em* inline markers to HTML."""
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    return text


def _instructions_to_html(text: str) -> str:
    """
    Convert the subset of markdown used in instruction strings to HTML.
    Handles: ### headings, **bold**, *em*, unordered lists (- / *), ordered
    lists (1. 2. …), and plain paragraphs. Robust against Python indentation.
    """
    lines = [l.rstrip() for l in dedent(text).strip().splitlines()]
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()

        if not line:                                      # blank line → skip
            i += 1

        elif line.startswith("### "):                    # heading
            out.append(f"<h3>{_inline(line[4:])}</h3>")
            i += 1

        elif re.match(r"[-*] ", line):                   # unordered list
            items: list[str] = []
            while i < len(lines) and re.match(r"[-*] ", lines[i].strip()):
                items.append(f"<li>{_inline(lines[i].strip()[2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")

        elif re.match(r"\d+\.\s", line):                 # ordered list
            items = []
            while i < len(lines) and re.match(r"\d+\.\s", lines[i].strip()):
                item_text = re.sub(r"^\d+\.\s+", "", lines[i].strip())
                items.append(f"<li>{_inline(item_text)}</li>")
                i += 1
            out.append("<ol>" + "".join(items) + "</ol>")

        else:                                            # paragraph
            para: list[str] = []
            while i < len(lines):
                stripped = lines[i].strip()
                if not stripped:
                    break
                if stripped.startswith("### ") or re.match(r"[-*] ", stripped) or re.match(r"\d+\.\s", stripped):
                    break
                para.append(stripped)
                i += 1
            out.append(f"<p>{_inline(' '.join(para))}</p>")

    return "\n".join(out)


# Maps test type string → Jinja2 template path
_TEMPLATE_MAP: dict[str, str] = {
    "smos": "pages/smos.html",
    "SMOS": "pages/smos.html",
    "smos_instruction": "pages/smos.html",
    "cmos": "pages/cmos.html",
    "CMOS": "pages/cmos.html",
    "cmos_instruction": "pages/cmos.html",
    "attention": "pages/cmos.html",
    "no_reference_attention": "pages/qmos.html",
    "nmos": "pages/nmos.html",
    "NMOS": "pages/nmos.html",
    "nmos_instruction": "pages/nmos.html",
    "qmos": "pages/qmos.html",
    "QMOS": "pages/qmos.html",
    "qmos_instruction": "pages/qmos.html",
    "qmos_negative_instruction": "pages/qmos.html",
    "emos": "pages/emos.html",
    "EMOS": "pages/emos.html",
    "emos_instruction": "pages/emos.html",
    "empha_pref": "pages/empha_pref.html",
    "empha_pref_instruction": "pages/empha_pref.html",
}

_EMOS_TYPES = {"emos", "EMOS", "emos_instruction"}

_ATTENTION_TYPES = {"attention", "no_reference_attention"}

# Deliberately an allowlist, not "anything that isn't attention/instruction".
# EMOS and empha_pref are excluded even though their audio-count nominally
# matches "dual"/"single" shape, because their instructions reference a
# transcript panel / second score slider that cmos.html and qmos.html don't
# render — borrowing that text would be a bigger giveaway than the thing
# implicit attention checks are trying to hide.
_SAFE_PREDECESSOR_TYPES: dict[str, set[str]] = {
    "attention": {"cmos", "CMOS", "smos", "SMOS"},                # dual-audio
    "no_reference_attention": {"qmos", "QMOS", "nmos", "NMOS"},   # single-audio
}


def _sample_session(
    sampler: TestCasesSampler,
    instruction_pages: list,
    attention_checks: list,
    num_attention: int,
) -> list:
    questions = sampler.sample_test_cases()

    if instruction_pages:
        _INSTRUCTION_KEY_MAP = {
            "smos_instruction": "SMOS",
            "cmos_instruction": "CMOS",
            "qmos_instruction": "QMOS",
            "qmos_negative_instruction": "QMOS",
            "empha_pref_instruction": "empha_pref",
        }
        for instr in instruction_pages:
            key = _INSTRUCTION_KEY_MAP.get(instr["type"])
            if key and key in questions:
                random.shuffle(questions[key])
                questions[key].insert(0, instr)

    test_cases: list = []
    for cases in questions.values():
        test_cases.extend(cases)

    checks_by_shape: dict[str, list] = {}
    for check in attention_checks:
        checks_by_shape.setdefault(check["type"], []).append(check)

    # Only hunt for predecessor types whose shape actually has checks
    # configured — e.g. a CMOS-only config never wastes a slot looking for
    # a QMOS/NMOS neighbor it has no "no_reference_attention" bank for.
    eligible_predecessor_types: set[str] = set()
    for shape, entries in checks_by_shape.items():
        if entries and shape in _SAFE_PREDECESSOR_TYPES:
            eligible_predecessor_types |= _SAFE_PREDECESSOR_TYPES[shape]

    n = min(num_attention, len(attention_checks)) if attention_checks else 0
    for i in range(n):
        total = len(test_cases)
        lo = math.floor(0.2 * (i + 1) * total)
        hi = math.floor(0.2 * (i + 2) * total)
        if total > 0:
            lo = max(lo, 1)   # never place a check at index 0 — it needs a predecessor
            hi = max(hi, lo)  # keep the window non-empty after raising lo

        # A valid insertion index is one whose immediate predecessor is a
        # real, shape-compatible question — never another attention check
        # (their type isn't in eligible_predecessor_types) and never an
        # instruction page (*_instruction type strings don't match either).
        candidates = [idx for idx in range(1, total + 1)
                      if test_cases[idx - 1]["type"] in eligible_predecessor_types]
        if not candidates:
            print(f"Warning: no eligible predecessor found for attention check {i + 1}/{n} — skipping")
            continue

        in_window = [idx for idx in candidates if lo <= idx <= hi]
        idx = random.choice(in_window if in_window else candidates)

        predecessor_type = test_cases[idx - 1]["type"]
        shape = "attention" if predecessor_type in _SAFE_PREDECESSOR_TYPES["attention"] else "no_reference_attention"
        check = random.choice(checks_by_shape[shape])
        test_cases.insert(idx, check)

    return test_cases


def _build_score_choices(page_obj) -> list[dict]:
    min_val, max_val, _ = page_obj.get_slider_config()
    labels = page_obj.get_level_label()
    return [{"value": min_val + i, "label": lbl} for i, lbl in enumerate(labels)]


def _audio_url(path: Optional[str]) -> Optional[str]:
    return f"/audio/{path}" if path else None


def _prefetch_urls(test_cases: list, from_page: int, lookahead: int = 2) -> list[str]:
    urls: list[str] = []
    for i in range(from_page, min(from_page + lookahead, len(test_cases))):
        tc = test_cases[i]
        if tc.get("reference"):
            urls.append(f"/audio/{tc['reference']}")
        urls.append(f"/audio/{tc['target']}")
    return urls


def create_app(
    sampler: TestCasesSampler,
    page_module,
    attention_checks: list,
    instruction_pages: list,
    num_attention: int = 3,
    prolific_return_code: Optional[str] = None,
    participant_cap: int = 30,
    audio_roots: Optional[list[str]] = None,
    session_max_age_seconds: int = 7200,
) -> FastAPI:
    app = FastAPI()
    store = SessionStore()
    app.add_event_handler("shutdown", store.close)
    PageFactory = getattr(page_module, "PageFactory")
    _audio_roots = audio_roots or [os.getcwd()]

    redirect_url = (
        f"https://app.prolific.com/submissions/complete?cc={prolific_return_code}"
        if prolific_return_code
        else "https://app.prolific.com/"
    )

    templates = Jinja2Templates(directory="templates")
    templates.env.filters["mdemphasis"] = lambda s: md.markdown(s or "")
    app.mount("/static", StaticFiles(directory="static"), name="static")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _get_session(sid: Optional[str]):
        if not sid:
            return None
        session = store.get(sid) or store.restore_from_disk(sid)
        if session is None:
            return None
        # Check server-side expiration
        if session_max_age_seconds > 0 and session.created_at > 0:
            age = time.time() - session.created_at
            if age > session_max_age_seconds:
                store.mark_abandoned()
                await store.delete(sid)
                return None
        return session

    async def _render_test(request: Request, sid: str, error: Optional[str] = None):
        session = await _get_session(sid)
        if session is None:
            return RedirectResponse(url="/", status_code=303)
        if session.current_page >= len(session.test_cases):
            prolific_flag = "1" if "@" not in session.user_id else "0"
            return RedirectResponse(url=f"/complete?prolific={prolific_flag}", status_code=303)

        tc = session.test_cases[session.current_page]
        page_obj = PageFactory.create_page(tc)
        test_type = tc["type"]

        template_name = _TEMPLATE_MAP.get(test_type, "pages/cmos.html")
        if test_type in _ATTENTION_TYPES and session.current_page > 0:
            predecessor_tc = session.test_cases[session.current_page - 1]
            predecessor_page = PageFactory.create_page(predecessor_tc)
            instructions_html = _instructions_to_html(predecessor_page.get_instructions())
        else:
            instructions_html = _instructions_to_html(page_obj.get_instructions())
        ref_url = _audio_url(tc.get("reference"))
        tar_url = _audio_url(tc["target"])
        score_choices = _build_score_choices(page_obj)

        second_score_choices = None
        if test_type in _EMOS_TYPES and hasattr(page_obj, "get_editing_slider_config"):
            mn, mx, _ = page_obj.get_editing_slider_config()
            labels = page_obj.get_editing_level_label()
            second_score_choices = [{"value": mn + i, "label": lbl} for i, lbl in enumerate(labels)]

        total = len(session.test_cases)
        current = session.current_page
        prefetch = _prefetch_urls(session.test_cases, current + 1)

        session_state_json = json.dumps({
            "session_id": sid,
            "current_page": current,
            "total_pages": total,
            "prefetch_urls": prefetch,
        })

        return templates.TemplateResponse(template_name, {
            "request": request,
            "page_num": current + 1,
            "total_pages": total,
            "progress_pct": int(current / total * 100) if total else 0,
            "instructions_html": instructions_html,
            "ref_audio_url": ref_url,
            "tar_audio_url": tar_url,
            "score_choices": score_choices,
            "second_score_choices": second_score_choices,
            "transcript": getattr(page_obj, "transcript", None),
            "edited_transcript": getattr(page_obj, "edited_transcript", None),
            "is_instruction": page_obj.is_instruction,
            "session_state_json": session_state_json,
            "error": error,
        })

    # ------------------------------------------------------------------
    # Audio serving
    # ------------------------------------------------------------------

    @app.get("/audio/{file_path:path}")
    async def serve_audio(file_path: str):
        # Guard against path traversal (e.g. ../../../etc/passwd)
        for root in _audio_roots:
            root_resolved = Path(root).resolve()
            full = (root_resolved / file_path).resolve()
            # Ensure the resolved path is still inside the audio root
            if not (str(full) + os.sep).startswith(str(root_resolved) + os.sep):
                continue
            if full.is_file():  # single stat call — implies exists
                return FileResponse(
                    str(full),
                    headers={
                        "Cache-Control": "public, max-age=86400",
                        "Accept-Ranges": "bytes",
                    },
                )
        return Response(status_code=404)

    # ------------------------------------------------------------------
    # Login
    # ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, mos_session_id: Optional[str] = Cookie(default=None)):
        prolific_pid = request.query_params.get("PROLIFIC_PID")
        if prolific_pid:
            # 1. Valid cookie? Resume the existing session.
            session = await _get_session(mos_session_id)
            if session is not None:
                return RedirectResponse(url="/test", status_code=303)

            # 2. Already completed? Block re-taking.
            if (RESULTS_DIR / f"{prolific_pid}_results.json").exists():
                return RedirectResponse(url="/complete?prolific=1", status_code=303)

            # 3. Active session exists for this PID (lost cookie / different device)?
            #    Restore it and re-attach the cookie.
            existing_sid = store.find_by_user(prolific_pid)
            if existing_sid is not None:
                resp = RedirectResponse(url="/test", status_code=303)
                resp.set_cookie("mos_session_id", existing_sid, httponly=True, samesite="lax", max_age=session_max_age_seconds)
                return resp

            # 4. Brand-new participant — check cap and create.
            if not store.reserve_slot(participant_cap):
                return templates.TemplateResponse("login.html", {
                    "request": request,
                    "error": "The maximum number of participants has been reached. Thank you for your interest!",
                })
            url_params = dict(request.query_params)
            test_cases = _sample_session(sampler, instruction_pages, attention_checks, num_attention)
            sid = await store.create(prolific_pid, test_cases, url_params)
            resp = RedirectResponse(url="/test", status_code=303)
            resp.set_cookie("mos_session_id", sid, httponly=True, samesite="lax", max_age=session_max_age_seconds)
            return resp

        return templates.TemplateResponse("login.html", {"request": request, "error": None})

    @app.post("/start", name="start")
    async def start(
        request: Request,
        email: str = Form(default=""),
        prolific_pid: str = Form(default=""),
    ):
        if not is_valid_email(email) and not prolific_pid:
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "Please provide a valid email address.",
            })

        valid_id = email if email else prolific_pid

        # Already completed? Block re-taking.
        if (RESULTS_DIR / f"{valid_id}_results.json").exists():
            prolific_flag = "1" if "@" not in valid_id else "0"
            return RedirectResponse(url=f"/complete?prolific={prolific_flag}", status_code=303)

        # Active session exists for this ID (different tab / lost cookie)?
        # Restore it instead of creating a duplicate.
        existing_sid = store.find_by_user(valid_id)
        if existing_sid is not None:
            resp = RedirectResponse(url="/test", status_code=303)
            resp.set_cookie("mos_session_id", existing_sid, httponly=True, samesite="lax", max_age=session_max_age_seconds)
            return resp

        if not store.reserve_slot(participant_cap):
            return templates.TemplateResponse("login.html", {
                "request": request,
                "error": "The maximum number of participants has been reached. Thank you for your interest!",
            })

        test_cases = _sample_session(sampler, instruction_pages, attention_checks, num_attention)
        sid = await store.create(valid_id, test_cases, {})
        resp = RedirectResponse(url="/test", status_code=303)
        resp.set_cookie("mos_session_id", sid, httponly=True, samesite="lax", max_age=session_max_age_seconds)
        return resp

    # ------------------------------------------------------------------
    # Test pages
    # ------------------------------------------------------------------

    @app.get("/test", name="test_page", response_class=HTMLResponse)
    async def test_page(
        request: Request,
        error: Optional[str] = None,
        mos_session_id: Optional[str] = Cookie(default=None),
    ):
        return await _render_test(request, mos_session_id, error=error)

    @app.post("/submit", name="submit")
    async def submit(
        request: Request,
        score: Optional[str] = Form(default=None),
        editing_score: Optional[str] = Form(default=None),
        ref_audio_played: str = Form(default="false"),
        target_audio_played: str = Form(default="false"),
        mos_session_id: Optional[str] = Cookie(default=None),
    ):
        session = await _get_session(mos_session_id)
        if session is None:
            return RedirectResponse(url="/", status_code=303)
        if session.current_page >= len(session.test_cases):
            prolific_flag = "1" if "@" not in session.user_id else "0"
            return RedirectResponse(url=f"/complete?prolific={prolific_flag}", status_code=303)

        tc = session.test_cases[session.current_page]
        test_type = tc["type"]
        page_obj = PageFactory.create_page(tc)
        needs_ref = page_obj.get_reference_audio() is not None

        # Server-side validation (client JS should prevent these in normal use)
        if target_audio_played != "true":
            return await _render_test(request, mos_session_id,
                                      error="Please finish listening to the audio before submitting.")
        if needs_ref and ref_audio_played != "true":
            return await _render_test(request, mos_session_id,
                                      error="Please finish listening to both audio samples before submitting.")
        if score is None:
            return await _render_test(request, mos_session_id,
                                      error="Please select a score before submitting.")
        if test_type in _EMOS_TYPES and editing_score is None:
            return await _render_test(request, mos_session_id,
                                      error="Please select an editing score before submitting.")

        score_int = int(score)
        result: dict = {
            "test_type": test_type,
            "reference_audio": tc.get("reference"),
            "target_audio": tc["target"],
            "ref_system": tc.get("ref_system"),
            "target_system": tc.get("target_system"),
            "swap": tc.get("swap", False),
        }

        if test_type in _EMOS_TYPES:
            result["naturalness_score"] = score_int
            result["editing_score"] = int(editing_score) if editing_score else None
            if hasattr(page_obj, "edited_transcript"):
                result["edited_transcript"] = page_obj.edited_transcript
        else:
            result["score"] = score_int

        if hasattr(page_obj, "transcript"):
            result["transcript"] = page_obj.transcript

        if session.url_params:
            result["url_params"] = session.url_params

        session.results.append(result)
        session.current_page += 1
        session.ref_audio_played = False
        session.target_audio_played = False
        await store.save(mos_session_id)

        if session.current_page >= len(session.test_cases):
            # Capture values before dispatching to thread pool
            _user_id = session.user_id
            _results = list(session.results)

            def _write_results():
                os.makedirs("results", exist_ok=True)
                filename = f"results/{_user_id}_results.json"
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump({
                        "user_id": _user_id,
                        "timestamp": datetime.now().isoformat(),
                        "results": _results,
                    }, f, indent=2, ensure_ascii=False)

            await asyncio.to_thread(_write_results)
            store.mark_completed()
            await store.delete(mos_session_id)
            prolific_flag = "1" if "@" not in _user_id else "0"
            return RedirectResponse(url=f"/complete?prolific={prolific_flag}", status_code=303)

        return RedirectResponse(url="/test", status_code=303)

    # ------------------------------------------------------------------
    # Complete
    # ------------------------------------------------------------------

    @app.get("/complete", name="complete", response_class=HTMLResponse)
    async def complete(
        request: Request,
        mos_session_id: Optional[str] = Cookie(default=None),
    ):
        session = await _get_session(mos_session_id)
        if session is not None:
            is_prolific = "@" not in session.user_id
            store.mark_abandoned()
        else:
            is_prolific = request.query_params.get("prolific") == "1"
        if mos_session_id:
            await store.delete(mos_session_id)
        resp = templates.TemplateResponse("complete.html", {
            "request": request,
            "is_prolific": is_prolific,
            "redirect_url": redirect_url,
        })
        resp.delete_cookie("mos_session_id")
        return resp

    # ------------------------------------------------------------------
    # Session restore (for browser localStorage recovery)
    # ------------------------------------------------------------------

    @app.post("/api/restore")
    async def restore_session(request: Request):
        try:
            data = await request.json()
        except Exception:
            return Response(status_code=400)

        sid = data.get("session_id")
        if not sid:
            return Response(status_code=400)

        session = store.get(sid) or store.restore_from_disk(sid)
        if session is None:
            return Response(status_code=404)

        if session.current_page >= len(session.test_cases):
            prolific_flag = "1" if "@" not in session.user_id else "0"
            return JSONResponse({"ok": True, "redirect": f"/complete?prolific={prolific_flag}"})

        resp = JSONResponse({"ok": True, "redirect": "/test"})
        resp.set_cookie("mos_session_id", sid, httponly=True, samesite="lax", max_age=session_max_age_seconds)
        return resp

    return app
