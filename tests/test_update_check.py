import httpx

import app.db as db
import app.update_check as uc


def test_version_gt():
    assert uc.version_gt("1.0.1", "1.0.0")
    assert uc.version_gt("1.10.0", "1.9.0")
    assert uc.version_gt("v1.0.1", "1.0.0")
    assert not uc.version_gt("1.0.0", "1.0.0")
    assert not uc.version_gt("1.0", "1.0.0")
    assert not uc.version_gt("垃圾", "1.0.0")


def test_current_version_reads_pyproject():
    import tomllib
    from pathlib import Path
    with open(Path(__file__).parent.parent / "pyproject.toml", "rb") as f:
        expected = tomllib.load(f)["project"]["version"]
    assert uc.current_version() == expected


async def test_update_info_no_url(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    uc._cache.clear()
    assert await uc.get_update_info() == {"update": None}


async def test_update_info_newer_version(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    db.set_setting(conn, "update_url", "http://x/version.json")
    conn.close()
    monkeypatch.setattr(uc, "_transport", httpx.MockTransport(lambda req: httpx.Response(200, json={
        "version": "99.0.0", "notes": "大更新", "url": "http://rel"})))
    uc._cache.clear()
    info = await uc.get_update_info()
    assert info["update"]["latest"] == "99.0.0"
    assert info["update"]["notes"] == "大更新"
    assert info["update"]["url"] == "http://rel"


async def test_update_info_same_version_means_none(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    db.set_setting(conn, "update_url", "http://x/version.json")
    conn.close()
    monkeypatch.setattr(uc, "_transport", httpx.MockTransport(lambda req: httpx.Response(200, json={
        "version": uc.current_version()})))
    uc._cache.clear()
    assert await uc.get_update_info() == {"update": None}


async def test_update_info_network_error_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    db.set_setting(conn, "update_url", "http://x/version.json")
    conn.close()

    def boom(request):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(uc, "_transport", httpx.MockTransport(boom))
    uc._cache.clear()
    assert await uc.get_update_info() == {"update": None}
