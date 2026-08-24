import time


def create_log(conn, *, client_protocol, group_name, path, stream: bool) -> int:
    cur = conn.execute(
        "INSERT INTO request_log (created_at, client_protocol, group_name, path, stream)"
        " VALUES (?,?,?,?,?)",
        (time.time(), client_protocol, group_name, path, 1 if stream else 0),
    )
    conn.commit()
    return cur.lastrowid


def finish_log(conn, log_id: int, *, status: str, total_ms: int,
               input_tokens: int | None, output_tokens: int | None) -> None:
    conn.execute(
        "UPDATE request_log SET status=?, total_duration_ms=?,"
        " input_tokens=?, output_tokens=? WHERE id=?",
        (status, total_ms, input_tokens, output_tokens, log_id),
    )
    conn.commit()


def add_attempt(conn, log_id: int, *, channel_id=None, channel_name=None,
                actual_model=None, converted=False, http_status=None,
                error_category=None, error_summary=None, raw_error=None,
                skipped=None) -> None:
    conn.execute(
        "INSERT INTO attempt (log_id, created_at, channel_id, channel_name,"
        " actual_model, converted, http_status, error_category, error_summary,"
        " raw_error, skipped) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (log_id, time.time(), channel_id, channel_name, actual_model,
         1 if converted else 0, http_status, error_category, error_summary,
         raw_error, skipped),
    )
    conn.commit()


def set_cooldown(conn, channel_id: int, seconds: int, error: str) -> None:
    until = time.time() + seconds if seconds > 0 else 0
    conn.execute(
        "INSERT INTO channel_state (channel_id, cooldown_until, last_error)"
        " VALUES (?,?,?) ON CONFLICT(channel_id) DO UPDATE SET"
        " cooldown_until=excluded.cooldown_until, last_error=excluded.last_error",
        (channel_id, until, error[:500]),
    )
    conn.commit()


def clear_cooldown(conn, channel_id: int) -> None:
    conn.execute(
        "INSERT INTO channel_state (channel_id, cooldown_until, last_error)"
        " VALUES (?,0,NULL) ON CONFLICT(channel_id) DO UPDATE SET"
        " cooldown_until=0, last_error=NULL",
        (channel_id,),
    )
    conn.commit()
