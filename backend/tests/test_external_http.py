"""Anonymous text retrieval must pin public addresses and never load page media."""

import json

import httpx
import pytest

from learning_app.errors import AppError
from learning_app.workbench.external_http import (
    DeepSeekWebSearch,
    PageText,
    PublicTextReader,
    public_url,
)


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Users/private.txt",
        "http://localhost/a",
        "http://127.0.0.1/",
        "https://10.0.0.1/",
        "https://[::1]/",
        "https://[::ffff:127.0.0.1]/",
        "https://user:pass@example.com/",
        "https://example.com:8443/",
        "http://machine/",
        "https://server.local/a",
        "https://example.com/\n",
    ],
)
def test_nonpublic_urls_are_rejected_before_network(url):
    with pytest.raises(AppError):
        public_url(url)


def test_only_anonymous_text_is_sent_to_pinned_ip_and_redirect_dns_is_rechecked():
    requests = []

    def serve(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "example.com"
        assert request.extensions["sni_hostname"] == "example.com"
        assert request.headers["cookie"] == "" and "authorization" not in request.headers
        if len(requests) == 1:
            return httpx.Response(
                302, headers={"location": "/text", "set-cookie": "do_not_replay=1"}
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            stream=httpx.ByteStream(
                '<title>公开文字</title><h1>事实</h1><p>保留原条件 &amp; 单位</p><script>ignored()</script><video>隐藏画面文字</video><img src="http://localhost/secret"><iframe src="https://bad/">不读取</iframe>'.encode()
            ),
        )

    dns = []

    def resolve(host, port):
        dns.append((host, port))
        return ["93.184.216.34"]

    result = PublicTextReader(resolver=resolve, transport=httpx.MockTransport(serve)).read(
        "https://example.com/start#position"
    )
    assert len(requests) == len(dns) == 2 and result["final_url"] == "https://example.com/text"
    assert result["title"] == "公开文字" and result["text"] == "事实\n保留原条件 & 单位"
    assert result["http_status"] == 200 and not result["truncated"]


def test_private_dns_after_redirect_never_dispatches_the_second_request():
    calls = []

    def serve(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://changed.example.net/"})

    def resolve(host, _port):
        return ["127.0.0.1"] if host == "changed.example.net" else ["93.184.216.34"]

    with pytest.raises(AppError, match="非公开地址"):
        PublicTextReader(resolver=resolve, transport=httpx.MockTransport(serve)).read(
            "https://example.com/"
        )
    assert len(calls) == 1


@pytest.mark.parametrize("media", ["image/png", "video/mp4", "application/pdf", "application/json"])
def test_media_and_other_binary_bodies_are_never_read(media):
    class Unreadable(httpx.SyncByteStream):
        def __iter__(self):
            raise AssertionError("forbidden body must never be read")

    with pytest.raises(AppError, match="只读网页文字"):
        PublicTextReader(
            resolver=lambda *_: ["93.184.216.34"],
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, headers={"content-type": media}, stream=Unreadable())
            ),
        ).read("https://example.com/media")


def test_html_parser_never_executes_or_collects_hidden_media():
    parser = PageText()
    parser.feed(
        "<p>外部事实</p><svg><text>原图像素替身</text></svg><object>补图</object><p>可读末尾</p>"
    )
    assert parser.result()[0] == "外部事实\n可读末尾"


def test_search_uses_dedicated_query_only_and_preserves_missing_usage(tmp_path):
    secret = tmp_path / "synthetic.env"
    secret.write_text("DEEPSEEK_API_KEY=synthetic-test-only\n", "utf-8")
    seen = []

    def serve(request):
        seen.append(request)
        assert json.loads(request.content) == {
            "query": "synthetic query",
            "count": 6,
            "need_content": False,
        }
        assert request.url.path == "/v1/tools/search"
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "url": "https://example.com/a",
                        "title": "A",
                        "snippet": "evidence",
                        "date": None,
                    }
                ]
            },
        )

    result = DeepSeekWebSearch(secret_file=secret, transport=httpx.MockTransport(serve)).search(
        "synthetic query"
    )
    assert len(seen) == 1 and result["raw_usage"] is None
    assert result["items"][0]["text"] == "evidence" and result["http_status"] == 200


def test_search_http_failure_is_not_retried_or_replaced_with_model(tmp_path):
    secret = tmp_path / "synthetic.env"
    secret.write_text("DEEPSEEK_API_KEY=synthetic-test-only\n", "utf-8")
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(404)

    with pytest.raises(AppError, match="未调用辅助模型"):
        DeepSeekWebSearch(secret_file=secret, transport=httpx.MockTransport(serve)).search(
            "synthetic query"
        )
    assert len(requests) == 1
