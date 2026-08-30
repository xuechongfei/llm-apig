import time
from dataclasses import dataclass

IMAGE_TYPES = {"image", "image_url", "input_image"}
VIDEO_TYPES = {"video", "video_url", "input_video"}


def detect_modalities(payload: dict) -> set[str]:
    found: set[str] = set()

    def walk(node):
        if isinstance(node, dict):
            t = node.get("type")
            if t in IMAGE_TYPES:
                found.add("image")
            elif t in VIDEO_TYPES:
                found.add("video")
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(payload.get("messages", []))
    return found


@dataclass
class Candidate:
    mapping_id: int
    channel_id: int
    channel_name: str
    protocol: str
    base_url: str
    api_key: str
    actual_model: str
    priority: int
    needs_conversion: bool
    cooling: bool


@dataclass
class Skipped:
    channel_id: int
    channel_name: str
    actual_model: str
    reason: str  # "capability"


def select_candidates(conn, group_name: str, entry_protocol: str,
                      modalities: set[str], now: float | None = None
                      ) -> tuple[list[Candidate], list[Skipped]]:
    now = time.time() if now is None else now
    rows = conn.execute(
        """
        SELECT m.id AS mapping_id, m.actual_model, m.priority,
               m.supports_image, m.supports_video,
               c.id AS channel_id, c.name AS channel_name, c.protocol,
               c.base_url, c.api_key,
               COALESCE(s.cooldown_until, 0) AS cooldown_until
        FROM model_mapping m
        JOIN model_group g ON g.id = m.group_id
        JOIN channel c ON c.id = m.channel_id
        LEFT JOIN channel_state s ON s.channel_id = c.id
        WHERE g.name = ? AND c.enabled = 1
        """,
        (group_name,),
    ).fetchall()

    candidates: list[Candidate] = []
    skipped: list[Skipped] = []
    for r in rows:
        if ("image" in modalities and not r["supports_image"]) or (
            "video" in modalities and not r["supports_video"]
        ):
            skipped.append(Skipped(r["channel_id"], r["channel_name"],
                               r["actual_model"], "capability"))
            continue
        cooling = r["cooldown_until"] > now
        candidates.append(Candidate(
            mapping_id=r["mapping_id"], channel_id=r["channel_id"],
            channel_name=r["channel_name"], protocol=r["protocol"],
            base_url=r["base_url"], api_key=r["api_key"],
            actual_model=r["actual_model"],
            priority=r["priority"],
            needs_conversion=(r["protocol"] != entry_protocol),
            cooling=cooling,
        ))

    candidates.sort(key=lambda c: c.priority)
    candidates.sort(key=lambda c: (c.cooling, c.needs_conversion))
    return candidates, skipped
