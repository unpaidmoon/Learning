"""Bounded, read-only collection/series pages; videos are resolved only after selection."""

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from learning_app.errors import AppError

PAGE_SIZE = 30


@dataclass(frozen=True)
class CatalogueLocator:
    kind: str
    owner_id: str
    catalogue_id: str

    @property
    def group_id(self):
        return f"{self.kind}:{self.owner_id}:{self.catalogue_id}"

    @property
    def url(self):
        variant = "series" if self.kind == "series" else "season"
        return (
            f"https://space.bilibili.com/{self.owner_id}/lists/{self.catalogue_id}?type={variant}"
        )


def _split(value):
    try:
        return urlsplit(value)
    except ValueError as error:
        raise AppError(
            "BILI_CATALOGUE_LINK_UNSUPPORTED", "链接格式无效，请重新粘贴完整链接", 422
        ) from error


def catalogue_link(value):
    parsed = _split(value)
    if parsed.scheme != "https" or parsed.netloc != "space.bilibili.com" or parsed.fragment:
        raise AppError(
            "BILI_CATALOGUE_LINK_UNSUPPORTED", "请输入B站空间中的合集或系列目录链接", 422
        )
    query = parse_qs(parsed.query, keep_blank_values=True)
    current = re.fullmatch(r"/(\d+)/lists/(\d+)/?", parsed.path)
    legacy = re.fullmatch(r"/(\d+)/channel/(collectiondetail|seriesdetail)/?", parsed.path)
    variants = query.get("type", ["season"])
    if len(variants) != 1 or variants[0] not in {"season", "series"}:
        raise AppError(
            "BILI_CATALOGUE_LINK_UNSUPPORTED", "合集或系列类型不明确，请使用完整目录链接", 422
        )
    if current:
        return CatalogueLocator(
            "series" if variants[0] == "series" else "collection", current[1], current[2]
        )
    ids = query.get("sid", [])
    if legacy and len(ids) == 1 and ids[0].isdecimal():
        return CatalogueLocator(
            "series" if legacy[2] == "seriesdetail" else "collection", legacy[1], ids[0]
        )
    raise AppError("BILI_CATALOGUE_LINK_UNSUPPORTED", "当前链接没有明确的合集／系列身份", 422)


def is_catalogue_url(value):
    return _split(value).hostname == "space.bilibili.com"


def _mapping(value):
    if not isinstance(value, dict):
        raise AppError("BILI_CATALOGUE_PAGE_INCOMPLETE", "目录响应格式不完整，请重新读取", 409)
    return value


def browse_catalogue(provider, grant, locator, page):
    if not 1 <= page <= 10000:
        raise AppError("BILI_CATALOGUE_PAGE_INVALID", "目录页码不在允许范围内", 422)
    if locator.kind == "collection":
        data = provider.json(
            grant,
            "https://api.bilibili.com/x/polymer/web-space/seasons_archives_list",
            {
                "mid": locator.owner_id,
                "season_id": locator.catalogue_id,
                "page_num": page,
                "page_size": PAGE_SIZE,
            },
        )
        data = _mapping(data)
        meta = _mapping(data.get("meta"))
        page_info = _mapping(data.get("page"))
        size = page_info.get("page_size")
        number = page_info.get("page_num", page_info.get("num"))
    else:
        header = provider.json(
            grant, "https://api.bilibili.com/x/series/series", {"series_id": locator.catalogue_id}
        )
        meta = _mapping(_mapping(header).get("meta"))
        data = provider.json(
            grant,
            "https://api.bilibili.com/x/series/archives",
            {
                "mid": locator.owner_id,
                "series_id": locator.catalogue_id,
                "pn": page,
                "ps": PAGE_SIZE,
            },
        )
        data = _mapping(data)
        page_info = _mapping(data.get("page"))
        size, number = page_info.get("size"), page_info.get("num")
    total = page_info.get("total")
    archives = data.get("archives")
    if (
        not isinstance(meta.get("name"), str)
        or not meta["name"].strip()
        or type(total) is not int
        or total < 0
        or size != PAGE_SIZE
        or not isinstance(archives, list)
        or len(archives) > PAGE_SIZE
        or (number is not None and number != page)
        or (meta.get("mid") is not None and str(meta["mid"]) != locator.owner_id)
        or any(
            key in meta and str(meta[key]) != locator.catalogue_id
            for key in ("season_id", "series_id")
        )
    ):
        raise AppError(
            "BILI_CATALOGUE_PAGE_INCOMPLETE", "目录页身份或分页信息不完整，未按完整结果处理", 409
        )
    offset = (page - 1) * PAGE_SIZE
    expected = max(0, min(PAGE_SIZE, total - offset))
    if len(archives) != expected or (page > 1 and offset >= total):
        raise AppError(
            "BILI_CATALOGUE_PAGE_INCOMPLETE", "目录页存在缺项或已变化，请重新读取原页", 409
        )
    videos = []
    seen = set()
    for index, archive in enumerate(archives, 1):
        archive = _mapping(archive)
        bvid = archive.get("bvid")
        if (
            not isinstance(bvid, str)
            or not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid)
            or bvid in seen
            or not isinstance(archive.get("title"), str)
        ):
            raise AppError(
                "BILI_CATALOGUE_PAGE_INCOMPLETE", "目录含缺失或重复的视频身份，未静默过滤", 409
            )
        seen.add(bvid)
        duration = archive.get("duration")
        videos.append(
            {
                "bvid": bvid,
                "title": archive["title"],
                "ordinal": offset + index,
                "cover_url": archive.get("pic"),
                "duration_ms": round(duration * 1000)
                if type(duration) in {int, float} and duration >= 0
                else None,
            }
        )
    revision = meta.get("mtime", meta.get("ptime"))
    return {
        "kind": "catalogue",
        "catalogue_kind": locator.kind,
        "url": locator.url,
        "group_id": locator.group_id,
        "title": meta["name"],
        "page": page,
        "total": total,
        "has_more": offset + len(videos) < total,
        "provider_revision": str(revision) if revision is not None else None,
        "videos": videos,
    }
