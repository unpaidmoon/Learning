"""Created and followed favourite directories; metadata only, never a subscription worker."""

import re
from types import SimpleNamespace

from sqlalchemy import select

from learning_app.errors import AppError

from .. import schema as s
from .provider import video_url

PAGE_SIZE = 20


def incomplete():
    raise AppError("BILI_FAVORITE_PAGE_INCOMPLETE", "收藏目录缺项或已变化，请重新读取此页", 409)


def identifier(value):
    return (
        str(value) if type(value) in {str, int} and re.fullmatch(r"[1-9]\d*", str(value)) else None
    )


def page_shape(total, rows, page, more=None):
    if type(total) is not int or total < 0 or not isinstance(rows, list):
        incomplete()
    offset = (page - 1) * PAGE_SIZE
    if (page > 1 and offset >= total) or len(rows) != max(0, min(PAGE_SIZE, total - offset)):
        incomplete()
    expected = offset + len(rows) < total
    if more is not None and (type(more) is not bool or more != expected):
        incomplete()
    return expected


class Favorites:
    def __init__(self, host):
        self.host = host

    def _account(self, connection_id):
        grant = self.host.access.grant(connection_id)
        with self.host.repository.engine.connect() as conn:
            account = (
                conn.execute(
                    select(s.connections).where(s.connections.c.connection_id == connection_id)
                )
                .mappings()
                .one()
            )
        if account["state"] != "connected":
            raise AppError("BILI_LOGIN_REQUIRED", "收藏夹需要连接B站账号", 409)
        return grant, account["account_id"]

    def _folder(self, row, kind):
        if (
            not isinstance(row, dict)
            or not identifier(row.get("id"))
            or not isinstance(row.get("title"), str)
            or type(row.get("media_count")) is not int
            or row["media_count"] < 0
        ):
            incomplete()
        resource_type = 11 if kind == "created" else row.get("type")
        if type(resource_type) is not int or resource_type not in {11, 21}:
            raise AppError(
                "BILI_FAVORITE_TYPE_UNKNOWN", "此收藏目录类型尚未识别，未按普通收藏夹处理", 409
            )
        return {
            "folder_id": str(row["id"]),
            "title": row["title"],
            "count": row["media_count"],
            "resource_type": "collection" if resource_type == 21 else "folder",
        }

    def _created(self, grant, account):
        data = self.host.provider.json(
            grant, "https://api.bilibili.com/x/v3/fav/folder/created/list-all", {"up_mid": account}
        )
        if not isinstance(data, dict):
            incomplete()
        total, rows = data.get("count"), data.get("list")
        if rows is None and total == 0:
            rows = []
        if type(total) is not int or not isinstance(rows, list) or total != len(rows):
            incomplete()
        result = [self._folder(row, "created") for row in rows]
        if len({r["folder_id"] for r in result}) != len(result) or any(
            str(row.get("mid")) != str(account) for row in rows
        ):
            incomplete()
        return result

    def _folders(self, grant, account, kind, page):
        if kind == "created":
            rows = self._created(grant, account)
            total = len(rows)
            rows = rows[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
            more = page_shape(total, rows, page)
        else:
            data = self.host.provider.json(
                grant,
                "https://api.bilibili.com/x/v3/fav/folder/collected/list",
                {"up_mid": account, "pn": page, "ps": PAGE_SIZE, "platform": "web"},
            )
            if not isinstance(data, dict) or type(data.get("has_more")) is not bool:
                incomplete()
            total, rows = data.get("count"), data.get("list")
            if rows is None and total == 0:
                rows = []
            more = page_shape(total, rows, page, data["has_more"])
            rows = [self._folder(row, kind) for row in rows]
        if len({(r["resource_type"], r["folder_id"]) for r in rows}) != len(rows):
            incomplete()
        self.host.access.check(grant)
        return {"kind": kind, "page": page, "total": total, "has_more": more, "items": rows}

    def folders(self, connection_id):
        # Backwards-compatible created/list-all projection, without truncation.
        grant, account = self._account(connection_id)
        result = self._created(grant, account)
        self.host.access.check(grant)
        return result

    def folder_page(self, value):
        grant, account = self._account(value.connection_id)
        return self._folders(grant, account, value.kind, value.page)

    def browse(self, value):
        return self._browse(value)[0]

    def _browse(self, value, expected_grant=None):
        grant, account = self._account(value.connection_id)
        if expected_grant is not None:
            self.host.access.check(expected_grant)
            grant = expected_grant
        folders = self._folders(grant, account, value.kind, value.folder_page)
        folder = next(
            (
                f
                for f in folders["items"]
                if f["folder_id"] == value.folder_id and f["resource_type"] == value.resource_type
            ),
            None,
        )
        if folder is None:
            raise AppError(
                "BILI_FAVORITE_SCOPE_DENIED", "收藏目录不属于所选连接的原目录页，请重新选择", 403
            )
        collection = folder["resource_type"] == "collection"
        endpoint = (
            "https://api.bilibili.com/x/space/fav/season/list"
            if collection
            else "https://api.bilibili.com/x/v3/fav/resource/list"
        )
        data = self.host.provider.json(
            grant,
            endpoint,
            {
                "season_id" if collection else "media_id": value.folder_id,
                "pn": value.page,
                "ps": PAGE_SIZE,
                **({} if collection else {"order": "mtime", "platform": "web"}),
            },
        )
        if not isinstance(data, dict) or not isinstance(data.get("info"), dict):
            incomplete()
        info, rows = data["info"], data.get("medias")
        if info.get("title") and not isinstance(info["title"], str):
            incomplete()
        total = info.get("media_count")
        if str(info.get("id", info.get("season_id"))) != value.folder_id:
            incomplete()
        if rows is None and total == 0:
            rows = []
        if collection:
            # The official web client slices this endpoint's complete metadata
            # array locally. It does not resolve each video's parts or media.
            if type(total) is not int or not isinstance(rows, list) or len(rows) != total:
                incomplete()
            rows = rows[(value.page - 1) * PAGE_SIZE : value.page * PAGE_SIZE]
            more = page_shape(total, rows, value.page)
        else:
            if type(data.get("has_more")) is not bool:
                incomplete()
            more = page_shape(total, rows, value.page, data["has_more"])
        items, seen = [], set()
        for index, row in enumerate(rows):
            if not isinstance(row, dict) or not isinstance(row.get("title"), str):
                incomplete()
            kind = row.get("type", 2 if collection else None)
            identity = identifier(row.get("id"))
            key = f"{kind}:{identity}" if identity else f"unknown:{value.page}:{index}"
            if key in seen:
                incomplete()
            seen.add(key)
            bvid = row.get("bvid") or row.get("bv_id")
            bvid = (
                bvid if isinstance(bvid, str) and re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid) else None
            )
            attr = row.get("attr", 0)
            if type(attr) is not int or attr < 0:
                incomplete()
            reason = (
                "unsupported"
                if kind != 2
                else "removed"
                if attr & 1
                else "identity_missing"
                if not bvid or not identity
                else None
            )
            items.append(
                {
                    "resource_id": key,
                    "bvid": bvid,
                    "title": row["title"],
                    "available": reason is None,
                    "unavailable_reason": reason,
                }
            )
        self.host.access.check(grant)
        view = {
            "items": items,
            "has_more": more,
            "page": value.page,
            "total": total,
            "title": info.get("title") or folder["title"],
            "folder_id": value.folder_id,
            "kind": value.kind,
        }
        relation = None
        if collection:
            upper = info.get("upper")
            owner = identifier(upper.get("mid")) if isinstance(upper, dict) else None
            if not owner:
                incomplete()
            relation = {
                "group_id": f"collection:{owner}:{value.folder_id}",
                "title": view["title"],
                "catalogue_kind": "collection",
                "url": f"https://space.bilibili.com/{owner}/lists/{value.folder_id}?type=season",
                "provider_revision": str(info["mtime"]) if info.get("mtime") is not None else None,
            }
        return view, relation

    def selection(self, connection_id, origin, url, grant):
        bvid, _ = video_url(url)
        value = SimpleNamespace(
            connection_id=connection_id,
            folder_id=origin.folder_id,
            kind=origin.kind,
            resource_type=origin.resource_type,
            folder_page=origin.folder_page,
            page=origin.video_page,
        )
        view, relation = self._browse(value, grant)
        index = next(
            (i for i, v in enumerate(view["items"]) if v["bvid"] == bvid and v["available"]), None
        )
        if index is None:
            raise AppError(
                "BILI_FAVORITE_VIDEO_DENIED", "视频不属于原收藏页或已不可用，请重新选择", 403
            )
        if relation:
            relation.update(ordinal=(value.page - 1) * PAGE_SIZE + index + 1)
        return relation
