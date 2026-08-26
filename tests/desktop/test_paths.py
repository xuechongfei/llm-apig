import json

from desktop import paths


def test_data_dir_env(monkeypatch, tmp_path):
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path))
    assert paths.data_dir() == tmp_path


def test_data_dir_appdata_default(monkeypatch, tmp_path):
    monkeypatch.delenv("LLMAPIG_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert paths.data_dir() == tmp_path / "llm-apig"


def test_log_dir_under_data(monkeypatch, tmp_path):
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(tmp_path))
    assert paths.log_dir() == tmp_path / "logs"


def test_data_dir_from_config(monkeypatch, tmp_path):
    """config.json 中的 data_dir 生效（无环境变量时）。"""
    monkeypatch.delenv("LLMAPIG_DATA_DIR", raising=False)
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"data_dir": str(tmp_path / "custom")}), encoding="utf-8")
    assert paths.data_dir() == tmp_path / "custom"


def test_data_dir_env_beats_config(monkeypatch, tmp_path):
    """环境变量优先于 config.json。"""
    env_dir = tmp_path / "env-dir"
    monkeypatch.setenv("LLMAPIG_DATA_DIR", str(env_dir))
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"data_dir": str(tmp_path / "ignored")}), encoding="utf-8")
    assert paths.data_dir() == env_dir
