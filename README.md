# Subjective Evaluation Framework

A FastAPI-based platform for conducting subjective listening tests (MOS, preference, emphasis) for speech and audio evaluation. Supports Prolific integration and multiple languages.

## Setup

We use [uv](https://github.com/astral-sh/uv) for dependency management. Install **uv** first, then:

```bash
uv sync
```

## Run locally

```bash
uv run main_fastapi.py --config-name CONFIG_NAME
# e.g. uv run main_fastapi.py --config-name dev
```

For local development, `config/dev.yaml` provides a minimal self-contained test setup using the `devset/` directory.

## Prepare your test

### 1. Prepare your samples

Gather all samples from all systems you would like to evaluate, and organize them under a root directory:

```
root_dir/
├── System_A/
├── System_B/
├── GroundTruth/
└── ...
```

### 2. Build the test list

The *Test List* is a JSON file that defines all test cases. Format:

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

Use one of the `test_list_builders/` scripts to generate test lists from your samples:

```bash
# Local filesystem
uv run test_list_builders/local_fs/generate.py \
    --config_name CONFIG_NAME \
    output="YOUR_OUTPUT_DIR" \
    root_dir="ROOT_DIR_FOR_ALL_SYSTEM_SAMPLES"

# Google Drive
uv run test_list_builders/google_drive/generate.py \
    --config_name CONFIG_NAME \
    output="YOUR_OUTPUT_DIR" \
    root_dir="ROOT_DIR_FOR_ALL_SYSTEM_SAMPLES"
```

### 3. Configure and run your test

Create a config YAML under `config/` specifying the test list path, language, attention checks, and instruction pages. Then run:

```bash
uv run main_fastapi.py --config-name YOUR_CONFIG_NAME
```

## Test types

| Type | Description | Score range | Audio |
|---|---|---|---|
| CMOS | Comparative MOS | −3 to +3 | Reference + Target |
| SMOS | Speaker similarity MOS | −3 to +3 (+3 offset in analysis) | Reference + Target |
| NMOS | Naturalness MOS | 1 to 5 | Target only |
| QMOS | Quality MOS | 1 to 5 | Target only |
| EMOS | Editing MOS (dual-score) | Two scores | Target only |
| empha_pref | Emphasis preference | −1 / 0 / +1 | Reference + Target |

## Keyboard shortcuts (during the test)

| Shortcut | Action |
|---|---|
| `1`–`9` | Select score option by position |
| `Ctrl`+`Shift`+`,` | Play first audio (Sample A, or the only sample on no-ref pages) |
| `Ctrl`+`Shift`+`.` | Play second audio (Sample B, or the only sample on no-ref pages) |
| `Enter` | Submit rating |

Numbered keycap badges appear next to each score option and audio label, and a legend at the bottom of each test page shows all available shortcuts.

## Analyze results

```bash
# CMOS/SMOS analysis
uv run analysis/analysis.py RESULTS_DIRECTORY

# Preference test analysis
uv run analysis/analysis_pref.py RESULTS_DIRECTORY

# QMOS analysis
uv run analysis/qmos_analysis.py -d RESULTS_DIRECTORY

# DNSMOS correlation analysis
uv run analysis/dnsmos_analysis.py -i TEST_LIST -m MOS_RESULTS
```

## Extending

### Add a new language

Copy an existing page module (e.g. `pages/english.py`), translate the instruction strings, and set `language: your_language` in your config.

### Add a new test type

1. Subclass `TestPage` or `NoReferencePage` in `pages/english.py`
2. Implement all abstract methods including `get_template_name()`
3. Register the class in `PageFactory.PAGE_CLASSES`
4. Create a corresponding template in `templates/pages/`
5. Add the type to `_TEMPLATE_MAP` in `app/server.py`
