"""Media redirects retain source identity, per-hop grants and bounded range reads."""

# pytest injects the imported fixture by name.
# ruff: noqa: F811
from urllib.parse import urlsplit

import httpx
import pytest
from test_bilibili_media_faults import body, media_host, request, transport  # noqa: F401

from learning_app.errors import AppError


@pytest.mark.parametrize(
    "host_name,expected",
    [
        ("0123456789abcdef0123456789abcdef.v.smtcdns.com", True),
        ("0123456789abcdef0123456789abcdef.v.smtcdns.com.evil.invalid", False),
        ("not-a-hash.v.smtcdns.com", False),
    ],
)
async def test_observed_audio_cdn_is_allowed_only_as_cid_bound_redirect(
    media_host, monkeypatch, host_name, expected
):
    from learning_app.integrations.bilibili.provider import allowed_url

    _, host, item = media_host
    redirected_source(host, item, monkeypatch)
    target = "https://" + host_name + "/1/audio"
    with pytest.raises(AppError):
        allowed_url(target)
    calls = []

    def http(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(302, headers={"Location": target})
        return httpx.Response(
            206, content=b"abcd", headers={"Content-Length": "4", "Content-Range": "bytes 0-3/4"}
        )

    transport(monkeypatch, http)
    if expected:
        response = host.media.content(item.asset_id, item.source_version, request())
        assert await body(response) == b"abcd"
        await response.background()
        assert len(calls) == 2 and calls[1].headers.get("cookie") is None
    else:
        with pytest.raises(AppError):
            host.media.content(item.asset_id, item.source_version, request())
        assert len(calls) == 1


def redirected_source(host, item, monkeypatch):
    def media(grant, part):
        host.access.check(grant)
        return {
            "video": {"url": "https://www.bilibili.com/1/start", "format_id": "fixture"},
            "audio": None,
            "duration_ms": 2000,
        }

    def headers(grant, url):
        host.access.check(grant)
        return {
            "User-Agent": "fixture",
            **({"Cookie": "fixture-only"} if urlsplit(url).hostname == "www.bilibili.com" else {}),
        }

    monkeypatch.setattr(host.provider, "media", media)
    monkeypatch.setattr(host.provider, "headers", headers)
    return host.media.session(host.source(item.asset_id))


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_media_redirect_preserves_range_and_rebuilds_domain_credentials(
    media_host, monkeypatch, status
):
    _, host, item = media_host
    redirected_source(host, item, monkeypatch)
    calls = []

    def http(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(
                status, headers={"Location": "https://edge.bilivideo.com/1/media"}
            )
        return httpx.Response(
            206, content=b"abcd", headers={"Content-Length": "4", "Content-Range": "bytes 0-3/4"}
        )

    transport(monkeypatch, http)
    response = host.media.content(item.asset_id, item.source_version, request())
    assert await body(response) == b"abcd"
    await response.background()
    assert len(calls) == 2 and all(r.headers["range"] == "bytes=0-3" for r in calls)
    assert (
        calls[0].headers.get("cookie") == "fixture-only" and calls[1].headers.get("cookie") is None
    )


@pytest.mark.parametrize(
    "destination,code",
    [
        ("https://unapproved.invalid/1/media", "PLATFORM_URL_DENIED"),
        ("http://edge.bilivideo.com/1/media", "PLATFORM_URL_DENIED"),
        ("https://edge.bilivideo.com/2/media", "BILI_MEDIA_IDENTITY_UNKNOWN"),
        ("/1/start", "BILI_MEDIA_REDIRECT_LIMIT"),
        # httpx rejects this while constructing next_request, even with follow_redirects=False.
        ("https://edge.bilivideo.com:invalid/1/media", "BILI_MEDIA_NETWORK_FAILED"),
    ],
)
def test_unsafe_or_looping_media_redirect_never_dispatches_next_hop(
    media_host, monkeypatch, destination, code
):
    _, host, item = media_host
    redirected_source(host, item, monkeypatch)
    calls = []
    transport(
        monkeypatch,
        lambda req: calls.append(req) or httpx.Response(302, headers={"Location": destination}),
    )
    with pytest.raises(AppError) as error:
        host.media.content(item.asset_id, item.source_version, request())
    assert error.value.code == code and len(calls) == 1


def test_media_redirect_checks_revocation_before_next_hop(media_host, monkeypatch):
    _, host, item = media_host
    redirected_source(host, item, monkeypatch)
    calls = []

    def http(req):
        calls.append(req)
        host.access.change("bilibili", False)
        return httpx.Response(307, headers={"Location": "https://edge.bilivideo.com/1/media"})

    transport(monkeypatch, http)
    with pytest.raises(AppError) as error:
        host.media.content(item.asset_id, item.source_version, request())
    assert error.value.code == "PLATFORM_DISABLED" and len(calls) == 1


@pytest.mark.parametrize("missing", [False, True])
def test_redirect_missing_target_or_too_many_hops_are_bounded(media_host, monkeypatch, missing):
    _, host, item = media_host
    redirected_source(host, item, monkeypatch)
    calls = []

    def http(req):
        calls.append(req)
        return httpx.Response(302, headers={} if missing else {"Location": f"/1/hop{len(calls)}"})

    transport(monkeypatch, http)
    with pytest.raises(AppError) as error:
        host.media.content(item.asset_id, item.source_version, request())
    assert error.value.code == "BILI_MEDIA_REDIRECT_LIMIT" and len(calls) == (1 if missing else 4)


def test_download_if_range_survives_redirect_and_does_not_read_another_cid(media_host, monkeypatch):
    _, host, item = media_host
    session = redirected_source(host, item, monkeypatch)
    calls = []

    def http(req):
        calls.append(req)
        if len(calls) == 1:
            return httpx.Response(307, headers={"Location": "https://edge.bilivideo.com/1/media"})
        return httpx.Response(
            206, content=b"efgh", headers={"Content-Length": "4", "Content-Range": "bytes 4-7/8"}
        )

    transport(monkeypatch, http)
    client, response = host.media.upstream(
        session["grant"],
        session["video"]["url"],
        "bytes=4-",
        if_range='"fixed"',
        allow_full_response=True,
        expected_cid=session["cid"],
    )
    try:
        assert response.read() == b"efgh"
    finally:
        response.close()
        client.close()
    assert len(calls) == 2 and all(
        r.headers["if-range"] == '"fixed"' and r.headers["range"] == "bytes=4-" for r in calls
    )
