"""Display only the current official account picture without relaxing page CSP."""

import hashlib
import io
from urllib.parse import urlsplit

import httpx
from PIL import Image

from learning_app.errors import AppError
from learning_app.workbench.external_http import public_url, resolve_public


def picture_reference(profile):
    if not isinstance(profile, dict) or profile.get("status") != "ready":
        return None
    value = profile.get("value") or {}
    url = value.get("avatarUrl")
    if not url:
        return None
    try:
        url = public_url(url)
        if urlsplit(url).scheme != "https":
            return None
    except AppError:
        return None
    version = hashlib.sha256(f"{value.get('id')}\n{url}".encode()).hexdigest()
    return url, version


def project_account_picture(envelope):
    result = envelope.get("result", {})
    profile = result.get("value") if result.get("ok") else None
    reference = picture_reference(profile)
    if isinstance(profile, dict) and profile.get("status") == "ready":
        profile["value"]["avatarUrl"] = (
            "/api/v1/desktop/account-avatar/" + reference[1] if reference else None
        )
    return envelope


def read_account_picture(url):
    """Pin public DNS, send no credentials, refuse redirects and bound raster bytes."""
    parts = urlsplit(public_url(url))
    if parts.scheme != "https":
        raise AppError("ACCOUNT_AVATAR_UNAVAILABLE", "账号头像暂时无法加载", 502)
    address = resolve_public(parts.hostname, 443)[0]
    with httpx.Client(trust_env=False, follow_redirects=False, timeout=8) as client:
        with client.stream("GET", httpx.URL(url).copy_with(host=address),
                           headers={"Host": parts.hostname, "Accept": "image/png,image/jpeg,image/webp",
                                    "Accept-Encoding": "identity"},
                           extensions={"sni_hostname": parts.hostname}) as response:
            media = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if response.status_code != 200 or media not in {
                "image/png", "image/jpeg", "image/webp", "application/octet-stream", "binary/octet-stream",
            }:
                raise AppError("ACCOUNT_AVATAR_UNAVAILABLE", "账号头像暂时无法加载", 502)
            raw = bytearray()
            for chunk in response.iter_bytes(chunk_size=65536):
                raw.extend(chunk)
                if len(raw) > 2 * 1024 * 1024:
                    raise AppError("ACCOUNT_AVATAR_UNAVAILABLE", "账号头像超出大小限制", 502)
    with Image.open(io.BytesIO(raw)) as image:
        if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > 4_000_000:
            raise AppError("ACCOUNT_AVATAR_UNAVAILABLE", "账号头像格式不受支持", 502)
        actual_media = Image.MIME[image.format]
        if media.startswith("image/") and media != actual_media:
            raise AppError("ACCOUNT_AVATAR_UNAVAILABLE", "账号头像格式与内容不一致", 502)
        image.verify()
    return bytes(raw), actual_media
