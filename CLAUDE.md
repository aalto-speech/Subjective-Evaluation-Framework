# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

**Install dependencies:**
```bash
uv sync
```

**Run the app (FastAPI):**
```bash
uv run main_fastapi.py --config-name CONFIG_NAME
# e.g. uv run main_fastapi.py --config-name swedish_test
```

**Build a test list (local filesystem):**
```bash
uv run test_list_builders/local_fs/generate.py \
    --config_name CONFIG_NAME \
    output="YOUR_OUTPUT_DIR" \
    root_dir="ROOT_DIR_FOR_ALL_SYSTEM_SAMPLES"
```

**Analyze results (MOS-based tests):**
```bash
uv run analysis/analysis.py RESULTS_DIRECTORY
```

**Analyze preference test results:**
```bash
uv run analysis/analysis_pref.py RESULTS_DIRECTORY
```

**Standardize audio to 24kHz:**
```bash
bash scripts/standardize_audio_24k.sh INPUT_DIR OUTPUT_DIR
```

## Architecture

The app is a FastAPI-based Mean Opinion Score (MOS) listening test platform for speech/audio evaluation. It supports Prolific integration and multiple languages.

### Entry point & config
`main_fastapi.py` — Hydra-configured entry point. `@hydra.main` loads a YAML config from `config/`, creates the FastAPI app via `app.server.create_app()`, and launches it with `uvicorn`. The config specifies the `language`, which selects a page module from `pages/` (e.g. `language: swedish` → `pages.swedish`).

### Request flow
1. `GET /` — shows login form, or auto-starts if `?PROLIFIC_PID=` is in the URL. The Prolific path checks: (a) valid cookie → resume, (b) results already exist → block and redirect to `/complete`, (c) active session exists for this PID → restore it, (d) otherwise check cap and create a new session.
2. `POST /start` — validates email/PID, samples test cases for the session, creates a server-side session, sets a cookie, redirects to `/test`.
3. `GET /test` — reads the session, builds a context dict from the current test case, renders the appropriate Jinja2 template.
4. `POST /submit` — validates audio-played flags and score, records the result, advances the page index, saves session to disk, redirects back to `/test` (or `/complete` when done).
5. `GET /complete` — shows thank-you page; Prolific users get a redirect button.

All form submissions use the POST-Redirect-GET pattern, so the browser back button never re-submits a score.

### Session management (`app/session.py`)
`SessionStore` keeps sessions in memory (the primary store for hot-path reads) and persists them to a SQLite database (`sessions/sessions.db`) after every submit. On server startup it reloads all sessions from the DB, so in-progress tests survive server restarts. The session ID is stored in a plain `mos_session_id` cookie (HttpOnly, SameSite=Lax, Max-Age from config).

**Persistence is async and atomic:** `create()`, `save()`, and `delete()` are `async` — disk writes are offloaded to a worker thread via `asyncio.to_thread()`. SQLite's WAL mode and `INSERT OR REPLACE` provide atomic, crash-safe persistence without the temp-file pattern. A `threading.Lock` serializes DB access from the thread pool.

**Schema:** `sessions(id TEXT PK, user_id TEXT NOT NULL INDEXED, data_json TEXT, created_at REAL)`. The `user_id` index enables fast PID dedup lookups without scanning all files.

`SessionData` fields: `user_id`, `test_cases`, `current_page`, `results`, `url_params`, `ref_audio_played`, `target_audio_played`, `created_at` (epoch timestamp for expiration checks).

**Session expiration:** `_get_session()` checks `created_at` against `session_max_age_seconds` (config, default 7200). Expired sessions are deleted from memory and the DB. The cookie also carries `Max-Age` for browser-side enforcement.

**Participant cap:** `reserve_slot()` / `mark_completed()` / `mark_abandoned()` provide an atomic in-memory counter (`completed_count + in_progress_count`) that replaces the old `glob("results/*.json")` check. Because the methods are synchronous (no `await`), asyncio's cooperative multitasking guarantees the check-and-increment is atomic — no TOCTOU race. Counters are initialized from disk on startup. `find_by_user()` supports PID deduplication by looking up active sessions by user ID (in-memory first, then indexed SQLite query as fallback).

### Server (`app/server.py`)
`create_app(...)` is a factory that returns the configured FastAPI app. It holds the sampler, page module, attention checks, instruction pages, and config values in its closure — no global state.

Key internals:
- `_sample_session()` — samples test cases, inserts instruction pages, and interleaves attention checks at evenly-spaced positions (same logic as the old `MOSTest.sample_test_cases_for_session`). Attention checks are **implicit**: each one is only inserted at a position whose immediate predecessor is a real question of a shape-compatible type (`_SAFE_PREDECESSOR_TYPES` — `attention`/dual-audio checks require a `CMOS`/`SMOS` predecessor, `no_reference_attention`/single-audio checks require a `QMOS`/`NMOS` predecessor), so a single-audio question is never followed by a dual-audio attention check or vice versa. A check is skipped (with a logged warning) if no eligible predecessor exists anywhere in the session — e.g. an `empha_pref`-only test list places zero attention checks, since `empha_pref`'s instructions reference a transcript panel that the attention templates don't render.
- `_render_test()` — builds the full Jinja2 context for the current test page and calls `templates.TemplateResponse`. For attention-type pages, `instructions_html` is *not* built from the attention page's own `get_instructions()` — it's copied from the immediately preceding test case's page instead (guaranteed compatible by `_sample_session()`'s placement rule above), so the check is textually indistinguishable from a normal question. The score choices/slider still come from the attention page's own (unchanged) config — only the instructions text is borrowed.
- `GET /audio/{file_path:path}` — serves audio files from configured `audio_roots` with `Cache-Control: public, max-age=86400` and `Accept-Ranges` headers. Resolves paths and guards against traversal (e.g. `../../../etc/passwd` → 404). Uses a single `is_file()` call per root (no separate `exists()` check).
- `POST /api/restore` — called by browser JS to re-hydrate a session from disk after a server restart; sets the session cookie and returns a redirect URL.
- **Audio controls**: all `<audio>` elements have `controlsList="noplaybackrate"` to disable playback speed adjustment. Download is still allowed.
- **Shutdown**: `store.close()` is registered as a FastAPI shutdown handler to properly close the SQLite connection.

### Page system (`pages/`)
Each language module (e.g. `pages/english.py`, `pages/finnish.py`) defines:
- A `TestPage` abstract base class hierarchy: `TestPage → NoReferencePage`, with concrete subclasses for each test type.
- Supported types: `SMOS` (speaker similarity), `CMOS` (comparative MOS, −3 to +3), `NMOS` (naturalness), `QMOS` (quality, 1-5), `EMOS` (editing MOS, dual-score), `empha_pref` (emphasis preference, −1/0/+1), and corresponding `*InstructionPage` and `*AttentionPage` variants.
- A `PageFactory` with `PAGE_CLASSES` dict mapping type strings to classes. Register new types via `PageFactory.register_page_type()`.
- Each page implements `get_instructions()`, `get_slider_config()` (returns min, max, default), `get_level_label()`, and `get_template_name()`.
- `is_instruction = True` on instruction-page subclasses; the template renders a "Practice question" banner when this is set.

Finnish, Swedish, and Norwegian page modules import `TestPage` and `NoReferencePage` from `pages/english.py`; each concrete class still needs its own `get_template_name()` override (it's abstract on `TestPage`, not inherited from `pages/english.py`'s classes, since these modules subclass `TestPage` directly rather than the English concrete classes).

**To add a new language:** copy an existing page module, translate the instruction strings, and set `language: your_language` in the config.

**To add a new test type:** subclass `TestPage` (or `NoReferencePage`), implement all abstract methods including `get_template_name()`, register the class in `PageFactory.PAGE_CLASSES`, and create a corresponding template in `templates/pages/`.

### Templates (`templates/`)
Jinja2 templates; Python provides only a context dict — no HTML is generated in Python code.

```
templates/
  base.html              # Layout, CSS/JS links, window.SESSION_STATE injection
  login.html
  complete.html
  pages/
    _test_base.html      # Shared macros (progress_bar, error_box, submit_row, prefetch_links, shortcut_legend)
    cmos.html            # Two audio players + score radio
    smos.html            # Two audio players + score radio
    nmos.html            # Single audio player + score radio
    qmos.html            # Single audio player + score radio
    emos.html            # Single audio + two score radios + transcript
    empha_pref.html      # Two audio players + score radio + transcript
```

`attention` and `no_reference_attention` types reuse `cmos.html` and `qmos.html` respectively (same HTML structure; nothing in the template marks them as checks — see `_sample_session()`/`_render_test()` above for how they're made indistinguishable from a normal question). Template selection is driven by `_TEMPLATE_MAP` in `app/server.py`.

The `mdemphasis` Jinja2 filter (registered in `create_app`) converts `*word*` markdown emphasis in `empha_pref` transcripts to `<em>word</em>`, styled as bold-underline in CSS.

The `shortcut_legend` macro renders a keyboard shortcuts legend (with numbered keycap badges for scores and modifier keys for audio playback). It adapts to dual-audio vs single-audio page types.

Context keys passed to every test template: `page_num`, `total_pages`, `progress_pct`, `instructions_html`, `ref_audio_url`, `tar_audio_url`, `score_choices`, `second_score_choices` (EMOS only), `transcript` (empha_pref), `edited_transcript` (EMOS), `is_instruction`, `session_state_json`, `error`.

### Static files (`static/`)
- `static/css/style.css` — clean semantic CSS, ~80ch centered container, responsive. No-reference pages (NMOS, QMOS, EMOS) use a unified `.rating-card` that wraps the audio player and score section with a `.rating-divider` between them. Includes 3D gradient `kbd.shortcut-key` styles and `.shortcut-legend` card styling.
- `static/js/app.js` — handles five concerns:
  1. **Audio tracking**: listens for `ended` events on `#ref-audio` and `#tar-audio`, sets hidden form fields `ref_audio_played` / `target_audio_played` to `"true"`.
  2. **Client-side validation**: blocks form submit and shows an inline error if audio hasn't been played or no score is selected.
  3. **Browser cache / resume**: on test pages, saves `{session_id, current_page}` to `localStorage` after each render. On the login page, if `localStorage` has a session ID, POSTs to `/api/restore` to re-hydrate the session and redirects to `/test` — allowing users to resume after a network drop or browser restart. Clears localStorage on `/complete`.
  4. **Audio prefetch**: calls `new Audio().src = url` for the next two pages' audio files immediately after render.
  5. **Keyboard shortcuts (capture phase)**: Number keys 1–9 select score radio buttons (first unselected group, or first group). `Ctrl+Shift+,` and `Ctrl+Shift+.` play the first and second audio respectively (with fallback for no-ref pages). Enter submits the form. Audio elements are auto-blurred on `play` to prevent the browser's shadow-DOM controls from intercepting keystrokes.

### Test list JSON format
```json
{
  "CMOS": [
    [
      { "type": "CMOS", "reference": "path/a.wav", "target": "path/b.wav",
        "ref_system": "System_A", "target_system": "System_B" }
    ]
  ],
  "empha_pref": [
    [
      { "type": "empha_pref", "reference": "path/a.wav", "target": "path/b.wav",
        "ref_system": "System_A", "target_system": "System_B",
        "transcript": "She bought a *red* car." }
    ]
  ]
}
```
`empha_pref` test cases require a `transcript` field with the emphasized word wrapped in `*asterisks*`.

Use `test_list_builders/` scripts to generate these from local files, Google Drive, or a web server.

### Analysis (`analysis/`)
- `analysis.py`: Filters participants by attention check correctness, then computes per-system CMOS/SMOS means with 95% CI. SMOS scores are reported with a +3 offset. Outputs CSV and per-utterance JSON.
- `analysis_pref.py`: Filters by attention checks, then computes per-pair preference ratios for `empha_pref` tests. Normalizes scores by flipping sign when `swap=True`. Saves CSV and stacked bar plots.
- `dnsmos_analysis.py`, `qmos_analysis.py`: Variant analyses for DNSMOS/QMOS test types.

**Attention check audio naming convention:** the expected answer is parsed from the audio filename, but the convention and which script enforces it differs by type:
- `attention` (dual-audio, CMOS-shaped): the last underscore-separated segment before the extension is the expected **integer** score, e.g. `attention_check_-3.wav` → expected `-3`. Enforced by `analysis.py` and `analysis_pref.py` (both read `reference_audio`); `qmos_analysis.py` ignores this type entirely.
- `no_reference_attention` (single-audio, QMOS-shaped): the last underscore-separated segment is the expected quality **word** (`bad`/`poor`/`fair`/`good`/`excellent`, mapped to 1-5), e.g. `attention_check_bad.wav` → expected `1`. Enforced only by `analysis/qmos_analysis.py` (reads `target_audio`); `analysis.py`/`analysis_pref.py` ignore this type entirely — a QMOS-only or QMOS-heavy study must use `qmos_analysis.py` to get attention-check filtering.

### Config structure (`config/`)
`config/default.yaml` is always loaded by Hydra; named configs override it. Key fields:
- `sampler.test_list_path`: path to the test list JSON
- `sampler.sample_size_per_test`: how many items to sample per system per test type
- `attention_checks`: list of test case dicts for attention checks — `type: 'attention'` (dual-audio, needs `reference`+`target`) or `type: 'no_reference_attention'` (single-audio, `target` only); see the "Attention checks" section in `README.md` for setup details and the naming-convention note above for scoring
- `instructions`: list of instruction page test case dicts
- `language`: selects the `pages/<language>.py` module
- `prolific_return_code`: Prolific completion code for redirect
- `participant_cap`: maximum number of participants (default 30)
- `num_attention`: how many attention checks to interleave per session (default 3)
- `session_max_age_seconds`: session and cookie expiration in seconds (default 7200 = 2 hours)
- `server.server_name`, `server.server_port`, `server.root_path`, `server.allowed_paths`: uvicorn and audio-serving settings
- `config/dev.yaml` — self-contained dev setup using `devset/` directory; uses `root_path: ""` so no path prefix is needed on localhost
