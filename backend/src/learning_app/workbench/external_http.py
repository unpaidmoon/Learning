"""Bounded anonymous text HTTP and a fixed non-model search endpoint."""

import hashlib
import ipaddress
import json
import re
import socket
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx
from dotenv import dotenv_values

from learning_app.config import PROJECT_ROOT
from learning_app.errors import AppError

MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGE_CHARACTERS = 200_000
TEXT_PARSER = "public-html-text-v1"
SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"


def public_url(value):
    if (
        not isinstance(value, str)
        or len(value) > 4096
        or any(ord(c) < 33 for c in value)
        or "\\" in value
    ):
        raise AppError("EXTERNAL_URL_INVALID", "请指定公开HTTP或HTTPS网址", 422)
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower().rstrip(".")
        if (
            parts.scheme not in {"http", "https"}
            or not host
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443 if parts.scheme == "https" else 80}
        ):
            raise ValueError("invalid public URL")
        if (
            host == "localhost"
            or host.endswith((".localhost", ".local", ".internal"))
            or "%" in host
        ):
            raise ValueError("local host")
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            if "." not in host:
                raise ValueError("local host") from None
        else:
            if not address.is_global or (
                address.version == 6
                and address.ipv4_mapped is not None
                and not address.ipv4_mapped.is_global
            ):
                raise ValueError("nonpublic address")
        netloc = f"[{host}]" if ":" in host else host
        return urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError) as error:
        raise AppError(
            "EXTERNAL_URL_DENIED", "此入口只读取公开网站，不读取本机或私网", 403
        ) from error


def resolve_public(host, port):
    try:
        addresses = list(
            dict.fromkeys(
                row[4][0] for row in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            )
        )
    except OSError as error:
        raise AppError("EXTERNAL_DNS_UNAVAILABLE", "公开网站地址暂时无法解析") from error
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise AppError("EXTERNAL_ADDRESS_DENIED", "网站解析到非公开地址，未访问", 403)
    return addresses


class PageText(HTMLParser):
    ignored = frozenset(
        {
            "script",
            "style",
            "noscript",
            "template",
            "iframe",
            "frame",
            "object",
            "svg",
            "video",
            "audio",
            "canvas",
        }
    )
    breaks = frozenset(
        {
            "p",
            "div",
            "article",
            "section",
            "li",
            "br",
            "h1",
            "h2",
            "h3",
            "h4",
            "tr",
            "blockquote",
            "pre",
            "hr",
        }
    )

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title_parts, self.hidden = [], [], []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.ignored:
            self.hidden.append(tag)
        if self.hidden:
            return
        if tag == "title":
            self.in_title = True
        if tag in self.breaks:
            self.parts.append("\n")
        if tag in {"td", "th"}:
            self.parts.append("\t")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag in self.hidden:
                self.hidden = self.hidden[: self.hidden.index(tag)]
            return
        if tag == "title":
            self.in_title = False
        if tag in self.breaks:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            if self.in_title:
                self.title_parts.append(data)
            else:
                self.parts.append(data)

    def result(self):
        lines = [
            re.sub(r"[\t\f\v ]+", " ", line).strip() for line in "".join(self.parts).splitlines()
        ]
        return "\n".join(line for line in lines if line), " ".join(
            "".join(self.title_parts).split()
        )[:500]


class PublicTextReader:
    """Resolve and pin each anonymous hop; never load images or execute a page."""

    def __init__(
        self,
        *,
        resolver=resolve_public,
        transport=None,
        media_types=None,
        schemes=None,
        allow_query=True,
    ):
        self.resolver, self.transport = resolver, transport
        self.media_types = frozenset(media_types or {"text/html", "text/plain"})
        self.schemes = frozenset(schemes or {"http", "https"})
        self.allow_query = allow_query

    def _url(self, value):
        value = public_url(value)
        parsed = urlsplit(value)
        if parsed.scheme not in self.schemes or (not self.allow_query and parsed.query):
            raise AppError("EXTERNAL_URL_POLICY_DENIED", "网址不符合此功能的读取范围", 403)
        return value

    def read(self, url, check=lambda: None, observe=lambda *_: None):
        requested = current = self._url(url)
        started = time.monotonic()
        with httpx.Client(
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(10, read=5),
            transport=self.transport,
        ) as client:
            for hop in range(4):
                check()
                if time.monotonic() - started > 25:
                    raise AppError("EXTERNAL_READ_TIMEOUT", "公开网页读取超时，未自动重试")
                parsed = urlsplit(current)
                addresses = self.resolver(parsed.hostname, 443 if parsed.scheme == "https" else 80)
                if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
                    raise AppError("EXTERNAL_ADDRESS_DENIED", "网站解析到非公开地址，未访问", 403)
                # The same validated IP reaches the socket; Host and TLS SNI
                # retain the public authority. Environment proxies are disabled.
                address = addresses[0]
                target = httpx.URL(current).copy_with(host=address)
                check()
                observe(
                    "request_started", {"url": current, "pinned_address": address, "method": "GET"}
                )
                with client.stream(
                    "GET",
                    target,
                    headers={
                        "host": parsed.hostname,
                        "accept": ",".join(sorted(self.media_types)),
                        "accept-encoding": "identity",
                        "user-agent": "LearningWorkbench/0.1",
                        "cookie": "",
                    },
                    extensions={"sni_hostname": parsed.hostname},
                ) as response:
                    observe(
                        "response_received", {"url": current, "http_status": response.status_code}
                    )
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or hop == 3:
                            raise AppError(
                                "EXTERNAL_REDIRECT_LIMIT", "网页跳转超过读取范围，未继续访问"
                            )
                        current = self._url(urljoin(current, location))
                        continue
                    if response.status_code != 200:
                        raise AppError(
                            "EXTERNAL_HTTP_UNAVAILABLE",
                            f"公开网页返回HTTP {response.status_code}，未自动重试",
                        )
                    media = (
                        response.headers.get("content-type", "").split(";", 1)[0].lower().strip()
                    )
                    if media not in self.media_types:
                        raise AppError(
                            "EXTERNAL_CONTENT_TYPE_DENIED",
                            "此入口只读网页文字，不下载图片、视频或其他文件",
                            403,
                        )
                    if response.headers.get("content-encoding", "identity").lower() not in {
                        "",
                        "identity",
                    }:
                        raise AppError(
                            "EXTERNAL_ENCODING_UNSUPPORTED", "网页使用未支持的压缩编码，未读取内容"
                        )
                    chunks, count = [], 0
                    for chunk in response.iter_raw(chunk_size=16384):
                        check()
                        count += len(chunk)
                        if count > MAX_RESPONSE_BYTES or time.monotonic() - started > 25:
                            raise AppError(
                                "EXTERNAL_RESPONSE_LIMIT",
                                "网页超过本次读取大小或时间上限，未静默截断",
                            )
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    charset = response.charset_encoding
                    if not charset and media == "text/html":
                        match = re.search(rb"charset\s*=\s*[\"']?([a-zA-Z0-9_-]+)", raw[:4096])
                        charset = match[1].decode("ascii") if match else None
                    try:
                        text = raw.decode(charset or "utf-8", errors="strict")
                    except (LookupError, UnicodeError) as error:
                        raise AppError(
                            "EXTERNAL_TEXT_ENCODING", "网页文字编码无法可靠读取"
                        ) from error
                    if "\x00" in text:
                        raise AppError("EXTERNAL_TEXT_INVALID", "网页包含非文字内容，未提供给模型")
                    title = ""
                    if media == "text/html":
                        parser = PageText()
                        parser.feed(text)
                        text, title = parser.result()
                    if not text.strip():
                        raise AppError(
                            "EXTERNAL_TEXT_EMPTY", "未取得可读网页文字，不补图像或自动联网兜底"
                        )
                    if len(text) > MAX_PAGE_CHARACTERS:
                        raise AppError(
                            "EXTERNAL_TEXT_SIZE_LIMIT", "网页正文超过当前上限，未静默截断"
                        )
                    return {
                        "requested_url": requested,
                        "final_url": current,
                        "http_status": response.status_code,
                        "media_type": media,
                        "response_sha256": hashlib.sha256(raw).hexdigest(),
                        "text": text,
                        "title": title,
                        "parser_version": TEXT_PARSER,
                        "truncated": False,
                    }
        raise AppError("EXTERNAL_READ_UNAVAILABLE", "未取得公开网页文字")


class DeepSeekWebSearch:
    """Dedicated /search, not the auxiliary model-turn web-search provider."""

    def __init__(self, *, secret_file=None, transport=None):
        self.secret_file = secret_file or PROJECT_ROOT / ".local/secrets/deepseek.env"
        self.transport = transport

    def search(self, query, check=lambda: None, observe=lambda *_: None):
        key = dotenv_values(self.secret_file).get("DEEPSEEK_API_KEY")
        if not key:
            raise AppError(
                "EXTERNAL_SEARCH_NOT_CONFIGURED", "公开检索尚未配置；可以明确提供网页地址读取"
            )
        check()
        payload = {"query": query, "count": 6, "need_content": False}
        observe("request_started", {"url": SEARCH_ENDPOINT, "method": "POST", "body": payload})
        with httpx.Client(
            trust_env=False, follow_redirects=False, timeout=20, transport=self.transport
        ) as client:
            with client.stream(
                "POST", SEARCH_ENDPOINT, headers={"authorization": "Bearer " + key}, json=payload
            ) as response:
                observe(
                    "response_received",
                    {"url": SEARCH_ENDPOINT, "http_status": response.status_code},
                )
                if response.status_code != 200:
                    raise AppError(
                        "EXTERNAL_SEARCH_UNAVAILABLE",
                        f"检索服务返回HTTP {response.status_code}，未调用辅助模型或其他服务",
                    )
                chunks, size = [], 0
                for chunk in response.iter_bytes(chunk_size=16384):
                    check()
                    size += len(chunk)
                    if size > MAX_RESPONSE_BYTES:
                        raise AppError("EXTERNAL_SEARCH_RESPONSE_LIMIT", "检索响应过大，未静默截断")
                    chunks.append(chunk)
        raw = b"".join(chunks)
        try:
            value = json.loads(raw)
        except (UnicodeError, ValueError) as error:
            raise AppError("EXTERNAL_SEARCH_STRUCTURE", "检索服务没有返回可靠结构") from error
        if not isinstance(value, dict) or not isinstance(value.get("results"), list):
            raise AppError("EXTERNAL_SEARCH_STRUCTURE", "检索服务没有返回结果清单")
        results = []
        for item in value["results"]:
            if (
                not isinstance(item, dict)
                or not all(isinstance(item.get(k), str) for k in ("url", "title", "snippet"))
                or "date" not in item
                or (item["date"] is not None and not isinstance(item["date"], str))
            ):
                raise AppError("EXTERNAL_SEARCH_STRUCTURE", "检索条目字段缺失或类型不符")
            url = public_url(item["url"])
            if len(item["title"]) > 500 or len(item["snippet"]) > 8000:
                raise AppError("EXTERNAL_SEARCH_ITEM_LIMIT", "检索条目过长，未静默截断")
            results.append(
                {
                    "url": url,
                    "title": item["title"],
                    "text": item["snippet"],
                    "published_at": item["date"],
                }
            )
        return {
            "items": results[:6],
            "truncated": len(results) > 6,
            "response_sha256": hashlib.sha256(raw).hexdigest(),
            "http_status": 200,
            "raw_usage": None,
        }
