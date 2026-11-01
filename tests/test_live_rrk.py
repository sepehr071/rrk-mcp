import pytest

from rrk_mcp import laws

pytestmark = [pytest.mark.live, pytest.mark.anyio]


async def test_live_latest_then_text(client):
    r = (await client.call_tool("rk_latest_laws", {})).structured_content
    assert r["total"] > 20000 and len(r["laws"]) == 20 and r["more"] is True
    t = (await client.call_tool("rk_law_text", {"code": r["laws"][0]["code"], "max_chars": 800})).structured_content
    assert t["text"] and t["header"]


async def test_live_title_search_and_date_filter(client):
    r = (await client.call_tool("rk_search_laws", {"title": "ماليات"})).structured_content
    assert r["total"] > 100 and all("مال" in x["title"].replace("ی", "ي") for x in r["laws"])
    d = (
        await client.call_tool("rk_search_laws", {"from_date": "1405/08/05", "to_date": "1405/08/05"})
    ).structured_content
    assert d["laws"] and all(x["gazette_date"] == "1405/08/05" for x in d["laws"])


async def test_live_second_page(client):
    a = (await client.call_tool("rk_latest_laws", {"page": 1})).structured_content
    b = (await client.call_tool("rk_latest_laws", {"page": 2})).structured_content
    assert {x["code"] for x in a["laws"]}.isdisjoint(x["code"] for x in b["laws"])


async def test_live_filters_and_group_search(client):
    f = (await client.call_tool("rk_law_filters", {})).structured_content
    assert len(f["groups"]) > 50 and len(f["approvers"]) > 40
    r = (await client.call_tool("rk_search_laws", {"group": "3", "title": "ماليات"})).structured_content
    assert r["total"] > 100 and all("ديوان" in x["approver"] for x in r["laws"])
    a = (await client.call_tool("rk_search_laws", {"approver": "دبير هيات دولت"})).structured_content
    assert a["total"] > 100 and all("دبير" in x["approver"] for x in a["laws"])


async def test_live_law_text_without_a_prior_search_in_this_session(client):
    r = (await client.call_tool("rk_latest_laws", {"page": 2})).structured_content
    code = r["laws"][5]["code"]
    laws.reset()  # forget the links: the code has to be located again
    t = (await client.call_tool("rk_law_text", {"code": code, "max_chars": 500})).structured_content
    assert t["code"] == code and t["text"]


async def test_live_exchange_offices(client):
    r = (await client.call_tool("rk_exchange_offices", {"province": "قم"})).structured_content
    assert r["total"] > 10 and all(o["province"] == "قم" for o in r["offices"])
    c = (await client.call_tool("rk_exchange_offices", {"province": "قم", "city": "سلفچگان"})).structured_content
    assert c["total"] >= 1 and c["offices"][0]["city"] == "سلفچگان"
