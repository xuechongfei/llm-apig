import logging
import os
from pathlib import Path


def data_dir() -> Path:
    env = os.environ.get("LLMAPIG_DATA_DIR")
    if env:
        return Path(env)
    return Path(os.environ["APPDATA"]) / "llm-apig"


def log_dir() -> Path:
    return data_dir() / "logs"


def setup_logging() -> None:
    log_dir().mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(log_dir() / "app.log", encoding="utf-8")],
    )
