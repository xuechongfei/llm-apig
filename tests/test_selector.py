import app.db as db
from app.selector import detect_modalities, select_candidates


def _setup(conn):
    conn.execute("INSERT INTO channel (id,name,protocol,base_url,api_key) VALUES "
                 "(1,'claude-a','anthropic','http://a','k1'),"
                 "(2,'ds','openai','http://b','k2'),"
                 "(3,'claude-cool','anthropic','http://c','k3'),"
                 "(4,'disabled','anthropic','http://d','k4')")
    conn.execute("INSERT INTO model_group (id,name) VALUES (1,'claude-sonnet')")
    conn.execute("INSERT INTO model_mapping "
                 "(group_id,channel_id,actual_model,priority,supports_image) VALUES "
                 "(1,1,'m-a',10,1),(1,2,'deepseek-chat',5,0),(1,3,'m-c',1,1),(1,4,'m-d',1,1)")
    conn.execute("UPDATE channel SET enabled=0 WHERE id=4")
    conn.execute("INSERT INTO channel_state (channel_id,cooldown_until) VALUES (3, 9999999999.0)")
    conn.commit()


def _conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.db")
    db.init_db()
    conn = db.connect()
    _setup(conn)
    return conn


def test_detect_modalities_anthropic():
    p = {"messages": [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "x"}},
    ]}]}
    assert detect_modalities(p) == {"image"}


def test_detect_modalities_openai_video():
    p = {"messages": [{"role": "user", "content": [
        {"type": "video_url", "video_url": {"url": "http://x/v.mp4"}}]}]}
    assert detect_modalities(p) == {"video"}


def test_detect_modalities_plain_text():
    assert detect_modalities({"messages": [{"role": "user", "content": "hi"}]}) == set()


def test_select_filters_capability_and_sorts(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    # 带图片：ds(supports_image=0) 被跳过；同协议优先于优先级（claude-a p10 排在 ds 之前不出现，ds 被过滤）
    cands, skipped = select_candidates(conn, "claude-sonnet", "anthropic", {"image"}, now=1000.0)
    names = [c.channel_name for c in cands]
    assert names == ["claude-a", "claude-cool"]  # 冷却排最后，disabled 不出现
    assert [s.channel_name for s in skipped] == ["ds"]
    assert skipped[0].reason == "capability"
    assert cands[0].needs_conversion is False
    assert cands[1].cooling is True
    conn.close()


def test_select_protocol_match_beats_priority(tmp_path, monkeypatch):
    conn = _conn(tmp_path, monkeypatch)
    conn.execute("UPDATE channel_state SET cooldown_until=0 WHERE channel_id=3")
    conn.commit()
    # 纯文本请求：三个都可选；anthropic 入口下同协议优先 → claude-cool(p1) 和 claude-a(p10) 都在 ds(p5) 前
    cands, skipped = select_candidates(conn, "claude-sonnet", "anthropic", set(), now=1000.0)
    assert [c.channel_name for c in cands] == ["claude-cool", "claude-a", "ds"]
    assert cands[2].needs_conversion is True
    conn.close()
