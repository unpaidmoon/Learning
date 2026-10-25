"""Programme season metadata, explicit episode identity and original ordering."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from learning_app.errors import AppError


@dataclass(frozen=True)
class ProgrammeLocator:
    kind: str
    identity: str

    @property
    def url(self):
        return f"https://www.bilibili.com/bangumi/play/{self.kind}{self.identity}"


def programme_link(value):
    try:
        parsed = urlsplit(value)
    except ValueError as error:
        raise AppError("BILI_PROGRAMME_LINK_UNSUPPORTED", "番剧／影视链接格式无效", 422) from error
    match = re.fullmatch(r"/bangumi/play/(ss|ep)([1-9][0-9]{0,19})/?", parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc not in {"www.bilibili.com", "bilibili.com"}
        or not match
    ):
        raise AppError("BILI_PROGRAMME_LINK_UNSUPPORTED", "请输入番剧／影视的季或分集完整链接", 422)
    return ProgrammeLocator(match[1], match[2])


def incomplete():
    raise AppError(
        "BILI_PROGRAMME_DIRECTORY_INCOMPLETE",
        "番剧／影视目录身份、数量或时长不完整，未当作完整导入",
        409,
    )


def positive(value):
    if type(value) is int and 0 < value < 10**20:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,19}", value):
        return value
    incomplete()


def resolve_programme(provider, grant, value):
    locator = programme_link(value)
    data = provider.json(
        grant,
        "https://api.bilibili.com/pgc/view/web/season",
        {"season_id" if locator.kind == "ss" else "ep_id": locator.identity},
        result_key="result",
    )
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("title"), str)
        or not data["title"].strip()
    ):
        incomplete()
    season = positive(data.get("season_id"))
    if locator.kind == "ss" and season != locator.identity:
        raise AppError("BILI_SOURCE_VERSION_CHANGED", "平台返回的节目季身份不同，未改指来源", 409)
    main, sections = data.get("episodes"), data.get("section", [])
    if not isinstance(main, list) or not isinstance(sections, list):
        incomplete()
    if locator.kind == "ss" and "total" in data:
        if positive(data["total"]) != str(len(main)):
            incomplete()
    rows = list(main)
    for section in sections:
        if not isinstance(section, dict) or not isinstance(section.get("episodes"), list):
            incomplete()
        rows.extend(section["episodes"])
    if not rows or len(rows) > 10000:
        incomplete()
    parts, identities = [], {}
    ordinal = 0
    for row in rows:
        if not isinstance(row, dict):
            incomplete()
        episode = positive(row.get("id"))
        if episode in identities:
            previous = identities[episode]
            if any(row.get(key) != previous.get(key) for key in ("cid", "aid", "duration")):
                incomplete()
            continue
        identities[episode] = row
        ordinal += 1
        if locator.kind == "ep" and episode != locator.identity:
            continue
        cid, aid = positive(row.get("cid")), positive(row.get("aid"))
        duration = row.get("duration")
        if type(duration) is not int or not 0 < duration <= 315_360_000_000:
            incomplete()
        title, long_title = row.get("title"), row.get("long_title", "")
        if not isinstance(title, str) or not title.strip() or not isinstance(long_title, str):
            incomplete()
        parts.append(
            {
                "object_id": f"bangumi:{episode}:{cid}",
                "kind": "bangumi",
                "season_id": season,
                "episode_id": episode,
                "cid": int(cid),
                "aid": int(aid),
                "url": f"https://www.bilibili.com/bangumi/play/ep{episode}",
                "page": ordinal,
                "title": " ".join(v.strip() for v in (title, long_title) if v.strip()),
                "duration_ms": duration,
                "cover_url": row.get("cover") or data.get("cover"),
                # A VIP badge or episode status is not proof of the current account's access.
                "availability": "preview" if row.get("is_preview") is True else "unknown",
            }
        )
    if not parts:
        raise AppError("BILI_PROGRAMME_EPISODE_NOT_FOUND", "当前节目目录没有指定分集", 404)
    return {
        "title": data["title"],
        "parts": parts,
        "complete": True,
        "multipart": ordinal > 1,
        "group_id": "bangumi:" + season,
    }
