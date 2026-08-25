from app.update_check import current_version


def test_current_version_reads_pyproject():
    v = current_version()
    assert isinstance(v, str) and v.count(".") == 2