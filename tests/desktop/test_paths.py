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
