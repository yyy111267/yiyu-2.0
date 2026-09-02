"""web.search（博查版）解析映射与降级逻辑自检——全部走 MockTransport，不联网不花钱。"""

import asyncio
import json

import httpx
import pytest

from toolkit.web import tools


def _bocha_response(pages: list[dict], sent: dict | None = None):
    """构造博查 mock handler；sent 用于捕获请求体。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if sent is not None:
            sent.update(json.loads(request.read()))
        return httpx.Response(200, json={
            "code": 200,
            "data": {"webPages": {"value": pages}},
        })

    return httpx.MockTransport(handler)


@pytest.fixture
def set_key(monkeypatch):
    monkeypatch.setattr(tools, "_bocha_api_key", lambda: "sk-test")


def test_search_once_maps_name_to_title(monkeypatch, set_key):
    monkeypatch.setattr(tools, "_transport", _bocha_response([
        {"name": "标题A", "snippet": "摘要A", "url": "https://xueqiu.com/a"},
        {"name": "无URL", "snippet": "x", "url": ""},
    ]))
    res = asyncio.run(tools.WebSearchTool().execute("测试", max_results=5))
    assert res["count"] == 1
    assert res["results"][0] == {"title": "标题A", "snippet": "摘要A",
                                 "url": "https://xueqiu.com/a"}
    assert res["sources_verified"] is False


def test_restricted_sends_include_whitelist(monkeypatch, set_key):
    sent: dict = {}
    monkeypatch.setattr(tools, "_transport", _bocha_response([
        {"name": "t", "snippet": "s", "url": "https://finance.sina.com.cn/z/1.shtml"},
    ], sent))
    res = asyncio.run(tools.WebSearchTool().execute("q", max_results=3,
                                                    sources="finance"))
    assert "xueqiu.com" in sent["include"]      # 白名单组展开进 include（服务端过滤）
    assert sent["summary"] is False
    assert res["sources_verified"] is True


def test_restricted_falls_back_to_web_when_empty(monkeypatch, set_key):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        calls.append(body.get("include", ""))
        return httpx.Response(200, json={"code": 200,
                                         "data": {"webPages": {"value": []}}})

    monkeypatch.setattr(tools, "_transport", httpx.MockTransport(handler))
    res = asyncio.run(tools.WebSearchTool().execute("q", max_results=3,
                                                    sources="finance"))
    assert calls == [calls[0], ""]              # 第一次带 include，降级第二次全网
    assert res["sources_verified"] is False
    assert "降级" in res["note"]


def test_missing_key_returns_clear_error(monkeypatch):
    monkeypatch.setattr(tools, "_bocha_api_key", lambda: "")
    res = asyncio.run(tools.WebSearchTool().execute("q"))
    assert "IC_BOCHA_API_KEY" in res["error"]
    assert res["results"] == []


if __name__ == "__main__":
    test_search_once_maps_name_to_title.__wrapped__  # noqa: B018
    print("请用 pytest 运行：python -m pytest tests/test_web_search_tool.py -q")
