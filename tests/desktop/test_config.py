import json
from desktop import config


def test_default_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert config.default_data_dir() == tmp_path / "llm-apig"


def test_load_config_not_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    # 确保 config.json 不存在
    assert config.load_config() == {}


def test_load_config_valid(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"data_dir": "D:\\MyData"}), encoding="utf-8")
    assert config.load_config() == {"data_dir": "D:\\MyData"}


def test_load_config_corrupt(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text("not json{{{", encoding="utf-8")
    assert config.load_config() == {}


def test_save_config(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config.save_config({"data_dir": "D:\\Test"})
    loaded = json.loads(
        (tmp_path / "llm-apig" / "config.json").read_text(encoding="utf-8"))
    assert loaded == {"data_dir": "D:\\Test"}


def test_get_data_dir_none(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert config.get_data_dir() is None


def test_get_data_dir_set(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"data_dir": "E:\\Elsewhere"}), encoding="utf-8")
    assert config.get_data_dir() == "E:\\Elsewhere"


def test_set_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config.set_data_dir("F:\\NewPath")
    assert config.get_data_dir() == "F:\\NewPath"


def test_set_data_dir_overwrites(monkeypatch, tmp_path):
    monkeypatch.setenv("APPDATA", str(tmp_path))
    config_dir = tmp_path / "llm-apig"
    config_dir.mkdir(parents=True)
    (config_dir / "config.json").write_text(
        json.dumps({"data_dir": "old", "other": "keep"}), encoding="utf-8")
    config.set_data_dir("new")
    result = config.load_config()
    assert result["data_dir"] == "new"
    # 其他字段保留
    assert result.get("other") == "keep"