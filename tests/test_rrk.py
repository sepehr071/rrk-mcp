import json
import urllib.parse

import httpx
import pytest
from conftest import text

from rrk_mcp import http

pytestmark = pytest.mark.anyio


@pytest.fixture
def portal():
    """Fake rrk.ir: the search page, the report AJAX call and one law page. portal.calls records POST forms."""

    class P:
        calls: list[dict[str, str]] = []
        report = text("report.html")
        expire_first = False
        session_ended_first = False
        expire_law_link = False
        law_calls = 0

    p = P()
    p.calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/ghavanin-moghararat"):
            return httpx.Response(200, text=text("search_page.html"))
        if path.endswith("/r-exchanges"):
            return httpx.Response(200, text=text("exchanges_page.html"))
        if path.endswith("/wwv_flow.ajax"):
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            p.calls.append(form)
            if form["p_flow_step_id"] == "176":
                return httpx.Response(200, text=text("exchanges_report.html"))
            if "regions" in form["p_json"]:
                return httpx.Response(200, text=text("approvers.json"))
            if p.session_ended_first and len(p.calls) == 1:
                return httpx.Response(200, text="<html><body>Your session has ended. OK</body></html>")
            if p.expire_first and len(p.calls) == 1:
                return httpx.Response(200, text='{"error":"Ajax call returned server error"}')
            return httpx.Response(200, text=p.report)
        if "p122_code" in str(request.url):
            p.law_calls += 1
            if p.expire_law_link and p.law_calls == 1:
                return httpx.Response(200, text="<html>error page</html>")
            return httpx.Response(200, text=text("law_page.html"))
        return httpx.Response(404)

    http.set_transport(httpx.MockTransport(handler))
    return p


def items(form):
    return {i["n"]: i["v"] for i in json.loads(form["p_json"])["pageItems"]["itemsToSubmit"]}


async def test_search_parses_rows_and_normalizes_letters(client, portal):
    r = (await client.call_tool("rk_search_laws", {"title": "مالیات", "from_date": "1405-8-1"})).structured_content
    assert r["total"] == 771 and r["more"] is True and r["page"] == 1
    law = r["laws"][0]
    assert law["code"] == "39273" and law["approver"].startswith("هيات") and law["gazette_date"] == "1405/08/05"
    assert law["special_no"] == "2298" and law["title"].startswith("رأي شماره")
    f = portal.calls[0]
    assert items(f)["P67_TITLE"] == "ماليات" and items(f)["P67_FROM_DATE"] == "1405/08/01"
    assert f["p_widget_mod"] == "PULL" and f["x01"] == "22908922017848805"


async def test_page_two_uses_the_page_action(client, portal):
    await client.call_tool("rk_latest_laws", {"page": 3})
    f = portal.calls[0]
    assert f["p_widget_action"] == "PAGE" and f["p_widget_action_mod"] == "pgR_min_row=41max_rows=20rows_fetched=20"


async def test_bad_date_is_a_tool_error(client, portal):
    r = await client.call_tool("rk_search_laws", {"to_date": "next week"})
    assert r.is_error and "Jalali" in r.content[0].text


async def test_expired_session_is_retried_once(client, portal):
    portal.expire_first = True
    r = (await client.call_tool("rk_latest_laws", {})).structured_content
    assert len(portal.calls) == 2 and r["laws"]


async def test_ended_session_page_is_retried(client, portal):
    portal.session_ended_first = True
    r = (await client.call_tool("rk_latest_laws", {})).structured_content
    assert len(portal.calls) == 2 and r["total"] == 771


async def test_expired_law_link_restarts_the_session(client, portal):
    portal.expire_law_link = True
    r = (await client.call_tool("rk_law_text", {"code": "39273", "max_chars": 500})).structured_content
    assert r["text"] and portal.law_calls == 2


async def test_law_text(client, portal):
    await client.call_tool("rk_latest_laws", {})
    r = (await client.call_tool("rk_law_text", {"code": "39273", "max_chars": 500})).structured_content
    assert r["length"] > 500 and len(r["text"]) == 500 and r["next_offset"] == 500
    assert any("مرجع تصویب" in h for h in r["header"])
    r2 = (await client.call_tool("rk_law_text", {"code": "39273", "offset": 500, "max_chars": 500})).structured_content
    assert r2["text"] != r["text"]


async def test_law_text_unknown_code_is_not_found(client, portal):
    r = await client.call_tool("rk_law_text", {"code": "1"})
    assert r.is_error and "rk_search_laws" in r.content[0].text


async def test_law_text_locates_a_code_without_a_prior_search(client, portal):
    r = (await client.call_tool("rk_law_text", {"code": "39273", "max_chars": 500})).structured_content
    assert r["code"] == "39273" and r["text"] and len(portal.calls) == 1


async def test_filters_list_groups_and_approvers(client, portal):
    r = (await client.call_tool("rk_law_filters", {})).structured_content
    assert {"id": "3", "name": "آرای هیات عمومی دیوان عدالت اداری"} in r["groups"]
    assert any(g.get("parent") == "تصویب نامه ها" for g in r["groups"])
    assert r["approvers"][0] == {"code": "56", "name": "آراء هیات\u200cهای تخصصی دیوان عدالت اداری"}
    narrowed = (await client.call_tool("rk_law_filters", {"query": "ديوان"})).structured_content
    assert [g["id"] for g in narrowed["groups"]] == ["3"]


async def test_search_by_group_and_approver_sends_their_ids(client, portal):
    await client.call_tool("rk_search_laws", {"group": "آرای هيات عمومی ديوان عدالت اداری", "approver": "56"})
    f = portal.calls[-1]
    assert (
        items(f)["P67_TREE_VALUE"] == "3" and items(f)["P67_ALL"] == "0" and items(f)["P67_HCLAWAPPROVERCODE"] == "56"
    )
    await client.call_tool("rk_search_laws", {"title": "x"})
    assert items(portal.calls[-1])["P67_ALL"] == "1" and items(portal.calls[-1])["P67_TREE_VALUE"] == ""


async def test_unknown_or_ambiguous_group_is_a_tool_error(client, portal):
    r = await client.call_tool("rk_search_laws", {"group": "nothing like it"})
    assert r.is_error and "rk_law_filters" in r.content[0].text


async def test_exchange_offices(client, portal):
    r = (await client.call_tool("rk_exchange_offices", {"province": "اردبیل", "page": 2})).structured_content
    assert r["total"] == 441 and r["more"] is True and r["offices"][0]["city"] == "سلفچگان"
    assert {"province", "city", "phone", "postal_code", "address"} <= set(r["offices"][0])
    f = portal.calls[-1]
    assert f["p_widget_action"] == "paginate" and f["p_pg_min_row"] == "51" and items(f)["P176_PROVINCE"] == "اردبیل"


async def test_exchange_unknown_province_lists_valid_ones(client, portal):
    r = await client.call_tool("rk_exchange_offices", {"province": "Atlantis"})
    assert r.is_error and "Valid:" in r.content[0].text


async def test_tools_listed_read_only(client):
    tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == {
        "rk_search_laws",
        "rk_latest_laws",
        "rk_law_text",
        "rk_law_filters",
        "rk_exchange_offices",
    }
    assert all(t.annotations.read_only_hint for t in tools)


async def test_ajax_post_sends_the_headers_apex_sends(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, text="ok")

    monkeypatch.setattr(http, "JITTER", (0, 0))
    http.set_transport(httpx.MockTransport(handler))
    await http.fetch(http.SITE + "/ords/wwv_flow.ajax?p_context=rrs-front/ghavanin-moghararat/123", data={"a": "b"})
    h = seen[0].headers
    assert h["content-type"] == "application/x-www-form-urlencoded; charset=UTF-8"
    assert h["x-requested-with"] == "XMLHttpRequest"
    assert h["accept"] == "text/html, */*; q=0.01"
    assert h["referer"] == http.SITE + "/ords/r/rrs/rrs-front/ghavanin-moghararat"
    assert h["origin"] == http.SITE and h["priority"] == "u=1, i" and h["sec-fetch-site"] == "same-origin"


async def test_retry_after_is_honoured(monkeypatch):
    waits, calls = [], []

    async def sleep(s):
        waits.append(s)

    def handler(request):
        calls.append(1)
        return httpx.Response(429, headers={"retry-after": "3"}) if len(calls) == 1 else httpx.Response(200, text="ok")

    monkeypatch.setattr(http.asyncio, "sleep", sleep)
    http.set_transport(httpx.MockTransport(handler))
    assert await http.fetch(http.SITE + "/x") == "ok"
    assert 3 in waits


def test_proxy_list_is_split(monkeypatch):
    monkeypatch.setenv("RRK_MCP_PROXY", "http://a:1, http://b:2")
    assert http._proxies() == ["http://a:1", "http://b:2"]
