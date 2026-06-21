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
1. `GET /` — shows login form, or auto-starts if `?PROLIFIC_PID=` is in the URL.
2. `POST /start` — validates email/PID, samples test cases for the session, creates a server-side session, sets a cookie, redirects to `/test`.
3. `GET /test` — reads the session, builds a context dict from the current test case, renders the appropriate Jinja2 template.
4. `POST /submit` — validates audio-played flags and score, records the result, advances the page index, saves session to disk, redirects back to `/test` (or `/complete` when done).
5. `GET /complete` — shows thank-you page; Prolific users get a redirect button.

All form submissions use the POST-Redirect-GET pattern, so the browser back button never re-submits a score.

### Session management (`app/session.py`)
`SessionStore` keeps sessions in memory and also persists each session to `sessions/<uuid>.json` after every submit. On server startup it reloads all session files, so in-progress tests survive server restarts. The session ID is stored in a plain `mos_session_id` cookie (HttpOnly, SameSite=Lax).

`SessionData` fields: `user_id`, `test_cases`, `current_page`, `results`, `url_params`, `ref_audio_played`, `target_audio_played`.

### Server (`app/server.py`)
`create_app(...)` is a factory that returns the configured FastAPI app. It holds the sampler, page module, attention checks, instruction pages, and config values in its closure — no global state.

Key internals:
- `_sample_session()` — samples test cases, inserts instruction pages, and interleaves attention checks at evenly-spaced positions (same logic as the old `MOSTest.sample_test_cases_for_session`).
- `_render_test()` — builds the full Jinja2 context for the current test page and calls `templates.TemplateResponse`.
- `GET /audio/{file_path:path}` — serves audio files from configured `audio_roots` with `Cache-Control: public, max-age=86400` and `Accept-Ranges` headers.
- `POST /api/restore` — called by browser JS to re-hydrate a session from disk after a server restart; sets the session cookie and returns a redirect URL.

### Page system (`pages/`)
Each language module (e.g. `pages/english.py`, `pages/finnish.py`) defines:
- A `TestPage` abstract base class hierarchy: `TestPage → NoReferencePage`, with concrete subclasses for each test type.
- Supported types: `SMOS` (speaker similarity), `CMOS` (comparative MOS, −3 to +3), `NMOS` (naturalness), `QMOS` (quality, 1-5), `EMOS` (editing MOS, dual-score), `empha_pref` (emphasis preference, −1/0/+1), and corresponding `*InstructionPage` and `*AttentionPage` variants.
- A `PageFactory` with `PAGE_CLASSES` dict mapping type strings to classes. Register new types via `PageFactory.register_page_type()`.
- Each page implements `get_instructions()`, `get_slider_config()` (returns min, max, default), `get_level_label()`, and `get_template_name()`.
- `is_instruction = True` on instruction-page subclasses; the template renders a "Practice question" banner when this is set.

Finnish, Swedish, and Norwegian page modules import `TestPage` and `NoReferencePage` from `pages/english.py` and only override the text methods — `get_template_name()` is inherited.

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
    _test_base.html      # Shared macros (progress_bar, error_box, submit_row, prefetch_links)
    cmos.html            # Two audio players + score radio
    smos.html            # Two audio players + score radio
    nmos.html            # Single audio player + score radio
    qmos.html            # Single audio player + score radio
    emos.html            # Single audio + two score radios + transcript
    empha_pref.html      # Two audio players + score radio + transcript
```

`attention` and `no_reference_attention` types reuse `cmos.html` and `qmos.html` respectively (same HTML structure; the instructions text distinguishes them). Template selection is driven by `_TEMPLATE_MAP` in `app/server.py`.

The `mdemphasis` Jinja2 filter (registered in `create_app`) converts `*word*` markdown emphasis in `empha_pref` transcripts to `<em>word</em>`, styled as bold-underline in CSS.

Context keys passed to every test template: `page_num`, `total_pages`, `progress_pct`, `instructions_html`, `ref_audio_url`, `tar_audio_url`, `score_choices`, `second_score_choices` (EMOS only), `transcript` (empha_pref), `edited_transcript` (EMOS), `is_instruction`, `session_state_json`, `error`.

### Static files (`static/`)
- `static/css/style.css` — clean semantic CSS, ~80ch centered container, responsive.
- `static/js/app.js` — handles four concerns:
  1. **Audio tracking**: listens for `ended` events on `#ref-audio` and `#tar-audio`, sets hidden form fields `ref_audio_played` / `target_audio_played` to `"true"`.
  2. **Client-side validation**: blocks form submit and shows an inline error if audio hasn't been played or no score is selected.
  3. **Enter key**: `keydown` listener calls `form.requestSubmit()` on Enter.
  4. **Browser cache / resume**: on test pages, saves `{session_id, current_page}` to `localStorage` after each render. On the login page, if `localStorage` has a session ID, POSTs to `/api/restore` to re-hydrate the session and redirects to `/test` — allowing users to resume after a network drop or browser restart.
  5. **Audio prefetch**: calls `new Audio().src = url` for the next two pages' audio files immediately after render.

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

**Attention check audio naming convention:** The expected score is parsed from the audio filename — the last underscore-separated segment before the extension is used as the expected integer score (e.g., `attention_score_3.wav` → expected score 3). This drives automatic pass/fail filtering across all analysis scripts.

### Config structure (`config/`)
`config/default.yaml` is always loaded by Hydra; named configs override it. Key fields:
- `sampler.test_list_path`: path to the test list JSON
- `sampler.sample_size_per_test`: how many items to sample per system per test type
- `attention_checks`: list of test case dicts for attention checks
- `instructions`: list of instruction page test case dicts
- `language`: selects the `pages/<language>.py` module
- `prolific_return_code`: Prolific completion code for redirect
- `participant_cap`: maximum number of participants (default 30)
- `num_attention`: how many attention checks to interleave per session (default 3)
- `server.server_name`, `server.server_port`, `server.root_path`, `server.allowed_paths`: uvicorn and audio-serving settings
