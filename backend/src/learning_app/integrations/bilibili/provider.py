"""Read-only Bilibili protocol and pinned extraction, never exposed as raw tools."""

import copy
import hashlib
import http.cookiejar
import io
import json
import re
import sys
import threading
from urllib.parse import parse_qs, urljoin, urlsplit

import httpx

from learning_app.errors import AppError

_ENGINE_LOCK = threading.Lock()
DOMAINS = ("bilibili.com", "bilivideo.com", "bilivideo.cn", "hdslb.com", "biliapi.net")


def allowed_url(url, *, media_redirect=False):
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.username
        or parts.password
        or parts.port not in {None, 443}
        or not (
            any(parts.hostname == d or (parts.hostname or "").endswith("." + d) for d in DOMAINS)
            # Observed on the authorized P2 audio redirect, never a general API/cover origin.
            or (
                media_redirect
                and re.fullmatch(r"[0-9a-f]{32}\.v\.smtcdns\.com", parts.hostname or "")
            )
        )
    ):
        raise AppError(
            "PLATFORM_URL_DENIED",
            f"平台响应包含未获准的网络目标（{parts.scheme}://{parts.hostname}）",
            403,
        )
    return url


def video_url(value):
    if re.fullmatch(r"BV[0-9A-Za-z]{10}|av[0-9]+", value):
        value = "https://www.bilibili.com/video/" + value
    parts = urlsplit(value)
    match = re.fullmatch(r"/video/(BV[0-9A-Za-z]{10}|av[0-9]+)/?", parts.path)
    if (
        parts.scheme != "https"
        or parts.netloc not in {"www.bilibili.com", "bilibili.com"}
        or not match
    ):
        raise AppError("BILI_LINK_UNSUPPORTED", "请输入普通BV／AV视频链接；此链接形式尚未验证", 422)
    query = parse_qs(parts.query)
    page = query.get("p", [None])[0]
    if page is not None and (not page.isdecimal() or int(page) < 1):
        raise AppError("BILI_PAGE_INVALID", "分P序号无效", 422)
    return match[1], int(page) if page else None


class QuietLogger:
    def __init__(self):
        self.preview = False

    def debug(self, message):
        pass

    def warning(self, message):
        if "preview" in message.lower() or "试看" in message:
            self.preview = True

    def error(self, message):
        pass


def extraction_error(error):
    nested = error
    for _ in range(8):
        if isinstance(nested, AppError):
            return AppError(nested.code, nested.message, nested.status)
        nested = nested.__cause__ or nested.__context__
        if nested is None:
            break
    return AppError("BILI_EXTRACTION_FAILED", "B站媒体解析失败，未自动更换引擎或重试", 502)


class BilibiliProvider:
    def __init__(self, access, private, project_root):
        self.access, self.private, self.root = access, private, project_root

    def headers(self, grant, url="https://api.bilibili.com/"):
        self.access.check(grant)
        cookies = self.private.get(grant.connection_id).get("cookies", {})
        result = {"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}
        hostname = urlsplit(url).hostname or ""
        if cookies and (hostname == "bilibili.com" or hostname.endswith(".bilibili.com")):
            result["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
        return result

    def check_authentication(self, grant, value):
        if not isinstance(value, dict) or value.get("code") != -101:
            return
        expired = self.access.expire(grant)
        if not expired:
            # A late failure may belong to credentials replaced since dispatch.
            self.access.check(grant)
        raise AppError(
            "BILI_LOGIN_REQUIRED",
            "B站登录已失效，请重新扫码连接；原资料和任务保留"
            if expired
            else "此内容需要B站账号登录，请连接后重试",
            409,
        )

    def json(self, grant, url, params=None, *, result_key="data"):
        allowed_url(url)
        identity = self.access.request(grant, "metadata")
        response = None
        try:
            with httpx.Client(
                timeout=25, follow_redirects=False, headers=self.headers(grant, url)
            ) as client:
                response = client.get(url, params=params)
            self.access.receipt(identity, response.status_code)
            response.raise_for_status()
            value = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if response is None:
                self.access.receipt(identity)
            raise AppError("BILI_NETWORK_FAILED", "B站读取失败，未自动重试", 502) from exc
        self.check_authentication(grant, value)
        if value.get("code") != 0:
            raise AppError(
                "BILI_REMOTE_REJECTED",
                "B站未允许读取此内容，请核对连接及权限",
                409,
            )
        return value.get(result_key, {})

    def credential_poll(self, grant, key):
        """Login-only return value; cookie material never enters public DTOs or logs."""
        url = "https://passport.bilibili.com/x/passport-login/web/qrcode/poll"
        identity = self.access.request(grant, "login-receipt")
        try:
            with httpx.Client(
                timeout=25, follow_redirects=False, headers=self.headers(grant, url)
            ) as client:
                response = client.get(url, params={"qrcode_key": key})
            self.access.receipt(identity, response.status_code)
        except httpx.HTTPError as exc:
            self.access.receipt(identity)
            raise AppError("BILI_LOGIN_RESULT_UNKNOWN", "登录回执未知，请核对原连接", 502) from exc
        if response.status_code != 200:
            raise AppError("BILI_LOGIN_RECEIPT_FAILED", "平台登录回执读取失败", 502)
        result = response.json()
        if result.get("code") != 0:
            raise AppError("BILI_LOGIN_RECEIPT_FAILED", "平台未接受登录结果查询", 409)
        allowed = {"SESSDATA", "bili_jct", "DedeUserID", "DedeUserID__ckMd5"}
        return {
            "data": result["data"],
            "cookies": {
                cookie.name: cookie.value
                for cookie in response.cookies.jar
                if cookie.name in allowed
            },
        }

    def resolve(self, grant, value):
        parsed = urlsplit(value)
        if parsed.path.startswith("/bangumi/"):
            from .programme import resolve_programme

            return resolve_programme(self, grant, value)
        cheese = re.fullmatch(r"/cheese/play/(ss|ep)(\d+)/?", parsed.path)
        if (
            parsed.scheme == "https"
            and parsed.netloc in {"www.bilibili.com", "bilibili.com"}
            and cheese
        ):
            return self.resolve_cheese(grant, cheese[1], cheese[2])
        identity, selected_page = video_url(value)
        data = self.json(
            grant,
            "https://api.bilibili.com/x/web-interface/view",
            {
                "bvid" if identity.startswith("BV") else "aid": identity
                if identity.startswith("BV")
                else identity[2:]
            },
        )
        pages = data.get("pages")
        if not pages or len(pages) != data.get("videos", len(pages)):
            raise AppError("BILI_PARTS_INCOMPLETE", "分P目录不完整，未按完整视频导入", 409)
        parts = []
        for page in pages:
            if selected_page and page["page"] != selected_page:
                continue
            parts.append(
                {
                    "object_id": f"{data['bvid']}:{page['cid']}",
                    "bvid": data["bvid"],
                    "cid": page["cid"],
                    "aid": data["aid"],
                    "page": page["page"],
                    "title": page["part"],
                    "duration_ms": round(page["duration"] * 1000),
                    "cover_url": data.get("pic"),
                    "availability": "unknown",
                }
            )
        if not parts:
            raise AppError("BILI_PART_NOT_FOUND", "指定分P不存在", 404)
        return {
            "title": data["title"],
            "author": (data.get("owner") or {}).get("name"),
            "parts": parts,
            "complete": True,
            "multipart": len(pages) > 1,
            "restricted": bool(data.get("is_upower_exclusive")),
        }

    def resolve_cheese(self, grant, kind, identity):
        data = self.json(
            grant,
            "https://api.bilibili.com/pugv/view/web/season",
            {"season_id" if kind == "ss" else "ep_id": identity},
        )
        episodes = data.get("episodes")
        if not isinstance(episodes, list) or not episodes:
            raise AppError("BILI_COURSE_DIRECTORY_UNKNOWN", "课程目录未返回完整分集信息", 409)
        if data.get("ep_count", data.get("episode_count", len(episodes))) > len(
            episodes
        ) or data.get("episode_page", {}).get("next"):
            raise AppError(
                "BILI_COURSE_DIRECTORY_INCOMPLETE", "课程分集目录尚不完整，未当作全课成功", 409
            )
        parts = []
        for index, episode in enumerate(episodes, 1):
            if kind == "ep" and str(episode["id"]) != identity:
                continue
            if not episode.get("aid") or not episode.get("cid") or not episode.get("duration"):
                raise AppError("BILI_COURSE_EPISODE_INCOMPLETE", "课程分集缺少稳定身份或时长", 409)
            availability = (
                "preview"
                if episode.get("is_preview")
                else "restricted"
                if episode.get("playable") is False or episode.get("ep_status") == -1
                else "full"
                if episode.get("playable") is True
                else "unknown"
            )
            parts.append(
                {
                    "object_id": f"cheese:{episode['id']}:{episode['cid']}",
                    "kind": "cheese",
                    "episode_id": str(episode["id"]),
                    "cid": episode["cid"],
                    "aid": episode["aid"],
                    "url": f"https://www.bilibili.com/cheese/play/ep{episode['id']}",
                    "page": index,
                    "title": str(episode.get("title") or episode.get("index") or index),
                    "duration_ms": round(episode["duration"] * 1000),
                    "cover_url": episode.get("cover") or data.get("cover"),
                    "availability": availability,
                }
            )
        if not parts:
            raise AppError("BILI_COURSE_EPISODE_NOT_FOUND", "当前课程没有此分集", 404)
        return {
            "title": data.get("title") or "B站课程",
            "author": (data.get("up_info") or {}).get("uname"),
            "parts": parts,
            "complete": True,
            "multipart": len(episodes) > 1,
            "group_id": "cheese:" + str(data.get("season_id") or data.get("id") or identity),
        }

    def cover(self, grant, part):
        from ..covers import MAX_COVER_BYTES, image_type

        url = part.get("cover_url")
        if not url:
            plan = self.resolve(grant, part.get("url") or part["bvid"])
            matching = next((p for p in plan["parts"] if p["object_id"] == part["object_id"]), None)
            if not matching:
                raise AppError("SOURCE_VERSION_MISMATCH", "原视频或分集身份已变化，未改指封面", 409)
            url = matching.get("cover_url")
        if not isinstance(url, str) or not url:
            raise AppError("BILI_COVER_UNAVAILABLE", "平台未提供此视频的封面", 404)
        if url.startswith("//"):
            url = "https:" + url
        try:
            parsed = urlsplit(url)
            if parsed.port not in {None, 443}:
                raise AppError("BILI_COVER_TARGET_DENIED", "平台封面端口不在允许范围内", 403)
        except ValueError as error:
            raise AppError("BILI_COVER_TARGET_DENIED", "平台封面地址格式无效", 403) from error
        if parsed.scheme == "http" and (
            parsed.hostname == "hdslb.com" or (parsed.hostname or "").endswith(".hdslb.com")
        ):
            # Older metadata uses HTTP for the same image CDN. Only dispatch HTTPS.
            parsed = parsed._replace(scheme="https")
            url = parsed.geturl()
        # This additional host is a cover CDN observed in the authorized course
        # response and pinned extractor source. It is not added to media/tool URLs.
        if parsed.scheme != "https" or parsed.netloc != "archive.biliimg.com":
            allowed_url(url)
            hostname = parsed.hostname or ""
            if hostname != "hdslb.com" and not hostname.endswith(".hdslb.com"):
                raise AppError("BILI_COVER_TARGET_DENIED", "封面目标不属于已实现的图片服务", 403)
        identity = self.access.request(grant, "ui-cover")
        received = False
        try:
            with httpx.Client(timeout=15, follow_redirects=False) as client:
                with client.stream("GET", url, headers=self.headers(grant, url)) as response:
                    self.access.receipt(identity, response.status_code)
                    received = True
                    if response.status_code != 200:
                        raise AppError("BILI_COVER_UNAVAILABLE", "平台封面不可用，未自动重试", 409)
                    data = bytearray()
                    for block in response.iter_bytes(64 * 1024):
                        self.access.check(grant)
                        data.extend(block)
                        if len(data) > MAX_COVER_BYTES:
                            raise AppError(
                                "BILI_COVER_SIZE_INVALID", "平台封面过大，未继续接收", 409
                            )
                    image_type(data)
                    self.access.check(grant)
                    return bytes(data)
        except httpx.HTTPError as error:
            if not received:
                self.access.receipt(identity)
            raise AppError("BILI_COVER_NETWORK_FAILED", "封面读取中断，未自动重试", 502) from error

    def engine(self, kind="video"):
        lock = json.loads(
            (self.root / "resources/integrations/bilibili-engine.lock.json").read_text()
        )
        wheel = self.root / lock["path"]
        if not wheel.is_file() or hashlib.sha256(wheel.read_bytes()).hexdigest() != lock["sha256"]:
            raise AppError("BILI_ENGINE_NOT_PREPARED", "请先准备固定B站解析引擎", 503)
        with _ENGINE_LOCK:
            if str(wheel) not in sys.path:
                sys.path.insert(0, str(wheel))
            from yt_dlp import YoutubeDL
            from yt_dlp.extractor.bilibili import BiliBiliBangumiIE, BilibiliCheeseIE, BiliBiliIE
        return YoutubeDL, {"cheese": BilibiliCheeseIE, "bangumi": BiliBiliBangumiIE}.get(
            kind, BiliBiliIE
        )

    def media(self, grant, part):
        self.access.check(grant)
        if part.get("availability") in {"preview", "restricted"}:
            raise AppError(
                "BILI_PREVIEW_ONLY"
                if part["availability"] == "preview"
                else "BILI_ACCESS_RESTRICTED",
                "该分集未取得完整访问权，未按完整媒体播放或保存",
                409,
            )
        base, extractor = self.engine(part.get("kind", "video"))
        access = self.access
        from yt_dlp.networking.common import Response
        from yt_dlp.networking.exceptions import HTTPError
        from yt_dlp.utils import ExtractorError, GeoRestrictedError

        provider = self

        class GuardedDL(base):
            def urlopen(inner, request):
                url = request if isinstance(request, str) else request.url
                if not isinstance(request, str) and request.method != "GET":
                    raise AppError("BILI_READ_ONLY", "媒体解析仅允许只读请求", 403)
                for _ in range(4):
                    allowed_url(url)
                    query = parse_qs(urlsplit(url).query)
                    if "cid" in query and any(value != str(part["cid"]) for value in query["cid"]):
                        raise AppError(
                            "BILI_SOURCE_VERSION_CHANGED", "分P媒体身份已变化，旧来源没有改指", 409
                        )
                    identity = access.request(grant, "extract-media")
                    try:
                        # Reuse TLS connections within this resolution, while
                        # credentials still come only from the checked grant.
                        client.cookies.clear()
                        response = client.get(url, headers=provider.headers(grant, url))
                    except httpx.HTTPError:
                        access.receipt(identity)
                        raise
                    access.receipt(identity, response.status_code)
                    if response.status_code == 200 and urlsplit(url).hostname in {
                        "api.bilibili.com",
                        "api.biliapi.net",
                    }:
                        try:
                            api_result = response.json()
                        except ValueError:
                            pass
                        else:
                            provider.check_authentication(grant, api_result)
                    if response.status_code in {301, 302, 303, 307, 308} and response.headers.get(
                        "location"
                    ):
                        url = urljoin(url, response.headers["location"])
                        continue
                    # httpx already decoded HTTP content compression.
                    headers = {
                        k: v
                        for k, v in response.headers.items()
                        if k not in {"content-encoding", "content-length"}
                    }
                    result = Response(
                        io.BytesIO(response.content),
                        url=url,
                        headers=headers,
                        status=response.status_code,
                    )
                    if response.status_code >= 300:
                        raise HTTPError(result)
                    return result
                raise AppError("BILI_REDIRECT_LIMIT", "平台重定向超过限定次数", 502)

        logger = QuietLogger()
        observed_durations = []

        class ScopedExtractor(extractor):
            def _get_chapters(inner, *args, **kwargs):
                # The player consumes tracks only. Upstream otherwise waits on
                # a separate optional metadata service before returning them.
                return None

            def _real_extract(inner, url):
                try:
                    return super()._real_extract(url)
                except GeoRestrictedError as error:
                    if part.get("kind") == "bangumi":
                        raise AppError(
                            "BILI_AREA_RESTRICTED", "当前地区未获准访问此分集，未播放或保存", 409
                        ) from error
                    raise
                except ExtractorError as error:
                    # This expected message belongs to the hash-pinned programme extractor.
                    if (
                        part.get("kind") == "bangumi"
                        and error.expected
                        and str(error.msg).startswith("This video is for premium members only")
                    ):
                        raise AppError(
                            "BILI_ACCESS_RESTRICTED",
                            "当前账号未取得此分集完整访问权，未播放或保存",
                            409,
                        ) from error
                    raise

            def extract_formats(inner, play_info):
                # Upstream discards backupUrl. Choose a safe HTTPS CDN before any media request;
                # this is track selection, never a network retry or engine fallback.
                data = copy.deepcopy(play_info)
                duration = data.get("timelength") or (
                    data.get("dash", {}).get("duration", 0) * 1000
                )
                if isinstance(duration, (int, float)) and duration > 0:
                    observed_durations.append(round(duration))
                if data.get("is_preview") or data.get("isPreview"):
                    logger.preview = True
                dash = data.get("dash") or {}
                for track in [*(dash.get("video") or []), *(dash.get("audio") or [])]:
                    candidates = [
                        track.get("baseUrl") or track.get("base_url"),
                        *(track.get("backupUrl") or track.get("backup_url") or []),
                    ]
                    for candidate in candidates:
                        if candidate:
                            try:
                                allowed_url(candidate)
                            except AppError:
                                continue
                            track["baseUrl"] = candidate
                            break
                return super().extract_formats(data)

        options = {
            "quiet": True,
            "logger": logger,
            "no_warnings": True,
            "cachedir": False,
            "retries": 0,
            "extractor_retries": 0,
            "fragment_retries": 0,
            "socket_timeout": 25,
            "noplaylist": True,
            "skip_download": True,
            "getcomments": False,
            "writesubtitles": False,
            "writeautomaticsub": False,
            "plugin_dirs": [],
        }
        with (
            httpx.Client(timeout=25, follow_redirects=False) as client,
            GuardedDL(options, auto_init=False) as downloader,
        ):
            downloader.add_info_extractor(ScopedExtractor())
            for key, value in self.private.get(grant.connection_id).get("cookies", {}).items():
                downloader.cookiejar.set_cookie(
                    http.cookiejar.Cookie(
                        0,
                        key,
                        value,
                        None,
                        False,
                        ".bilibili.com",
                        True,
                        True,
                        "/",
                        True,
                        True,
                        None,
                        True,
                        None,
                        None,
                        {},
                    )
                )
            try:
                url = (
                    part.get("url")
                    or f"https://www.bilibili.com/video/{part['bvid']}?p={part['page']}"
                )
                info = downloader.extract_info(url, download=False, process=False)
            except AppError:
                raise
            except Exception as exc:
                raise extraction_error(exc) from exc
        if (
            part.get("kind") == "bangumi"
            and str(info.get("episode_id") or info.get("id")) != part["episode_id"]
        ):
            raise AppError("BILI_SOURCE_VERSION_CHANGED", "平台分集身份已变化，旧来源没有改指", 409)
        duration = min(observed_durations) if observed_durations else 0
        if logger.preview or (duration and duration + 2000 < part["duration_ms"]):
            raise AppError("BILI_PREVIEW_ONLY", "当前账号只得到试看媒体，未标记为完整", 409)
        if not duration:
            raise AppError("BILI_COMPLETENESS_UNKNOWN", "无法核对媒体完整时长", 409)
        formats = info.get("formats", [])
        videos = [
            f
            for f in formats
            if f.get("ext") == "mp4"
            and not f.get("fragments")
            and (f.get("vcodec") or "").startswith(("avc", "h264"))
            and f.get("url")
        ]
        within = [f for f in videos if (f.get("height") or 0) <= 1080]
        if within:
            videos = within
        if not videos:
            raise AppError("BILI_CODEC_UNSUPPORTED", "当前没有可安全播放的完整H.264轨道", 409)
        video = max(videos, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        audio = None
        if video.get("acodec") in {None, "none"}:
            audios = [
                f
                for f in formats
                if f.get("vcodec") == "none"
                and (f.get("acodec") or "").startswith(("mp4a", "aac"))
                and f.get("url")
            ]
            if not audios:
                raise AppError("BILI_AUDIO_MISSING", "媒体缺少可播放音轨", 409)
            audio = max(audios, key=lambda f: f.get("abr") or f.get("tbr") or 0)
        for track in [video, audio]:
            if track:
                allowed_url(track["url"])
                if str(part["cid"]) not in urlsplit(track["url"]).path.split("/"):
                    raise AppError(
                        "BILI_MEDIA_IDENTITY_UNKNOWN",
                        "媒体轨道无法对应冻结CID，未作为旧来源播放",
                        409,
                    )
        return {"video": video, "audio": audio, "duration_ms": duration}

    def subtitle(self, grant, part):
        data = self.json(
            grant, "https://api.bilibili.com/x/player/v2", {"aid": part["aid"], "cid": part["cid"]}
        )
        subtitle = data.get("subtitle")
        if not isinstance(subtitle, dict) or "subtitles" not in subtitle:
            return {"state": "unknown", "tracks": []}
        if data.get("need_login_subtitle") or subtitle.get("need_login_subtitle"):
            return {"state": "login_required", "tracks": []}
        tracks = []
        for row in subtitle["subtitles"]:
            url = row["subtitle_url"]
            if url.startswith("//"):
                url = "https:" + url
            allowed_url(url)
            identity = self.access.request(grant, "subtitle")
            response = None
            try:
                with httpx.Client(
                    timeout=25, follow_redirects=False, headers=self.headers(grant, url)
                ) as client:
                    response = client.get(url)
                self.access.receipt(identity, response.status_code)
                response.raise_for_status()
                body = response.json()["body"]
            except (httpx.HTTPError, ValueError, KeyError) as exc:
                if response is None:
                    self.access.receipt(identity)
                raise AppError(
                    "BILI_SUBTITLE_FAILED", "平台字幕读取失败，未当作无字幕", 502
                ) from exc
            tracks.append(
                {
                    "language": row.get("lan", "unknown"),
                    "origin": "platform_ai"
                    if row.get("ai_type") or row.get("lan", "").startswith("ai-")
                    else "platform_cc",
                    "segments": [
                        {
                            "start_ms": round(v["from"] * 1000),
                            "end_ms": round(v["to"] * 1000),
                            "text": v["content"],
                        }
                        for v in body
                    ],
                }
            )
        return {"state": "available" if tracks else "absent", "tracks": tracks}
