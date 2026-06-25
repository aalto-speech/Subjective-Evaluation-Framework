import os
from importlib import import_module
from pathlib import Path

import hydra
import uvicorn
from omegaconf import DictConfig, OmegaConf

from app.server import create_app
from utils import TestCasesSampler


@hydra.main(version_base=None, config_path="config")
def main(cfg: DictConfig) -> None:
    language = cfg.language
    page_module = import_module(f"pages.{language}")

    sampler = TestCasesSampler(
        test_cases_json=cfg.sampler.test_list_path,
        sample_size_per_test=cfg.sampler.sample_size_per_test,
    )

    server_cfg = cfg.server
    audio_roots: list[str] = []
    for path in server_cfg.allowed_paths:
        if path == "cwd":
            audio_roots.append(os.getcwd())
        else:
            audio_roots.append(str(Path(path).resolve()))

    # Convert OmegaConf objects to plain Python dicts/lists so they are
    # JSON-serialisable when stored in session files.
    attention_checks = OmegaConf.to_container(cfg.attention_checks, resolve=True) if cfg.get("attention_checks") else []
    instruction_pages = OmegaConf.to_container(cfg.instructions, resolve=True) if cfg.get("instructions") else []

    app = create_app(
        sampler=sampler,
        page_module=page_module,
        attention_checks=attention_checks,
        instruction_pages=instruction_pages,
        num_attention=cfg.get("num_attention", 3),
        prolific_return_code=cfg.get("prolific_return_code", None),
        participant_cap=cfg.get("participant_cap", 30),
        audio_roots=audio_roots,
        session_max_age_seconds=cfg.get("session_max_age_seconds", 7200),
    )

    uvicorn.run(
        app,
        host=server_cfg.server_name,
        port=server_cfg.server_port,
        root_path=server_cfg.get("root_path", ""),
    )


if __name__ == "__main__":
    main()
