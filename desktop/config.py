"""引导配置读写 —— 固定位置 %APPDATA%/llm-apig/config.json。

该文件始终位于默认数据目录，不随用户配置的 data_dir 迁移。
应用启动时先读此文件确定数据目录，再打开数据库。
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def default_data_dir() -> Path:
    """始终返回默认数据目录 %APPDATA%/llm-apig（不随配置变化）。"""
    return Path(os.environ["APPDATA"]) / "llm-apig"


def config_file() -> Path:
    """返回 config.json 的完整路径（延迟求值，测试可通过 monkeypatch 覆盖 APPDATA）。"""
    return default_data_dir() / "config.json"


def load_config() -> dict:
    """读取 config.json，文件不存在或 JSON 非法时返回空 dict。"""
    cf = config_file()
    try:
        if not cf.exists():
            return {}
        with open(cf, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.warning("config.json 读取失败: %s，回退到默认配置", e)
        return {}


def save_config(data: dict) -> None:
    """写入 config.json（自动创建父目录）。"""
    cf = config_file()
    cf.parent.mkdir(parents=True, exist_ok=True)
    with open(cf, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_data_dir() -> str | None:
    """读取用户配置的自定义数据目录，无配置时返回 None。"""
    cfg = load_config()
    val = cfg.get("data_dir")
    return val if isinstance(val, str) and val else None


def set_data_dir(path: str) -> None:
    """写入自定义数据目录路径到 config.json（保留其他字段）。"""
    cfg = load_config()
    cfg["data_dir"] = path
    save_config(cfg)