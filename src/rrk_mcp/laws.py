"""Tools for the laws and regulations database of the Official Gazette (rrk.ir, روزنامه رسمی).

The portal is an Oracle APEX app. A search page load gives a session (instance id, salt, checksums); the report
is refreshed with the same AJAX call the site's search button makes. A law's text page is only reachable through
the checksummed link the report gives, so links are remembered per code for this process.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from typing import Annotated, Any

from pydantic import Field

from . import http
from .http import ApiError
from .registry import tool

PAGE_URL = f"{http.SITE}/ords/r/rrs/rrs-front/ghavanin-moghararat"
AJAX_URL = f"{http.SITE}/ords/wwv_flow.ajax"
PER_PAGE = 20  # the report's fixed page size
MAX_TITLE = 400

_state: dict[str, Any] = {}
_links: dict[str, str] = {}  # law code -> checksummed path from the report
_lock: asyncio.Lock | None = None

# The portal stores Arabic ي/ك; its own search box converts typed Persian letters, so we do the same.
_LETTERS = str.maketrans({"ی": "ي", "ک": "ك", "ى": "ي"})
_DIGITS = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_DATE = re.compile(r"^(1[34]\d\d)/(\d\d?)/(\d\d?)$")


def _text(s: str) -> str:
    return " ".join(s.translate(_LETTERS).translate(_DIGITS).split())


def _date(s: str, name: str) -> str:
    s = s.translate(_DIGITS).strip().replace("-", "/")
    if not s:
        return ""
    m = _DATE.match(s)
    if not m:
        raise ApiError(f"{name} must be a Jalali date like 1405/08/09, got {s!r}.")
    return f"{m[1]}/{int(m[2]):02d}/{int(m[3]):02d}"


def _field(h: str, pattern: str) -> str:
    m = re.search(pattern, h)
    if not m:
        raise ApiError("rrk.ir changed its page layout (a search field is missing); the server needs an update.")
    return html.unescape(m[1])


async def _session(force: bool = False) -> dict[str, Any]:
    """Load the search page once and keep what the AJAX search call needs."""
    global _lock
    _lock = _lock or asyncio.Lock()
    async with _lock:
        if force or not _state:
            h = await http.fetch(PAGE_URL, cache=False)
            _state.clear()
            _state.update(
                instance=_field(h, r'name="p_instance" value="(\d+)"'),
                salt=_field(h, r'value="(\d+)" id="pSalt"'),
                protected=_field(h, r'id="pPageItemsProtected" value="([^"]+)"'),
                request=_field(
                    h, r'"pageItems":"#P67_TREE_VALUE[^"]*","lazyLoading":false,"ajaxIdentifier":"([^"]+)"'
                ).replace("\\u002F", "/"),
                worksheet=_field(h, r'id="R\d+_worksheet_id" value="(\d+)"'),
                report=_field(h, r'id="R\d+_report_id" value="(\d+)"'),
                groups=_groups(h),
                lov=(m := re.search(r'popupLov\(\{"itemId":"P67_HCLAWAPPROVERCODE","ajaxIdentifier":"([^"]+)"', h))
                and m[1].replace("\\u002F", "/"),
            )
        return dict(_state)


def _groups(h: str) -> list[dict[str, str]]:
    """The group tree the page embeds as JSON: [{id, name, parent}], parents first."""
    m = re.search(r"var gTree\d+Data\s*=\s*", h)
    if not m:
        return []
    tree, _ = json.JSONDecoder().raw_decode(h[m.end() :])
    out: list[dict[str, str]] = []

    def walk(nodes: list[dict[str, Any]], parent: str) -> None:
        for n in nodes:
            v = re.search(r"P67_TREE_VALUE', '(\d+)'", n.get("link", ""))
            if v:
                out.append({"id": v[1], "name": n["label"], **({"parent": parent} if parent else {})})
            walk(n.get("children", []), n["label"])

    walk(tree["data"].get("children", []), "")
    return out


async def _approvers(query: str = "") -> list[dict[str, str]]:
    """The approving authorities (popup LOV of the search form), optionally filtered by name words."""
    s = await _session()
    if not s["lov"]:
        raise ApiError("rrk.ir changed its page layout (approver list missing); the server needs an update.")
    fetch = {"search": _text(query), "sortColumn": None, "sortDirection": None, "version": 1, "firstRow": 1}
    region = {"id": "P67_HCLAWAPPROVERCODE", "ajaxIdentifier": s["lov"], "fetchData": fetch | {"maxRows": 300}}
    form = {
        "p_flow_id": "200",
        "p_flow_step_id": "67",
        "p_instance": s["instance"],
        "p_debug": "",
        "p_json": json.dumps({"regions": [region], "salt": s["salt"]}),
    }
    out = await http.fetch(f"{AJAX_URL}?p_context=rrs-front/ghavanin-moghararat/{s['instance']}", data=form)
    try:
        values = json.loads(out)["regions"][0]["fetchedData"]["values"]
    except (ValueError, KeyError, IndexError):
        raise ApiError("rrk.ir did not return the approver list; try again in a minute.") from None
    return [{"code": v[1], "name": v[0]} for v in values]


def _pick(q: str, options: list[tuple[str, str]], what: str) -> str:
    """Resolve an id or name words to one option id; options are (id, label)."""
    q = q.strip()
    if q.isdigit() and any(i == q for i, _ in options):
        return q
    nq = _text(q)
    hits = [o for o in options if _text(o[1]) == nq] or [o for o in options if nq in _text(o[1])]
    if len(hits) == 1:
        return hits[0][0]
    if not hits:
        raise ApiError(f"No {what} matches {q!r}. List them with rk_law_filters.")
    raise ApiError(f"{q!r} matches several {what}s, pass the id: " + "; ".join(f"{i} = {n}" for i, n in hits[:8]))


def reset() -> None:
    """Forget the session and remembered links (tests)."""
    _state.clear()
    _links.clear()


async def _report(items: dict[str, str], page: int) -> str:
    for attempt in (0, 1):
        s = await _session(force=attempt == 1)
        body = {
            "pageItems": {
                "itemsToSubmit": [{"n": f"P67_{k}", "v": v} for k, v in items.items()],
                "protected": s["protected"],
                "rowVersion": "",
                "formRegionChecksums": [],
            },
            "salt": s["salt"],
        }
        form = {
            "p_flow_id": "200",
            "p_flow_step_id": "67",
            "p_instance": s["instance"],
            "p_debug": "",
            "p_request": "PLUGIN=" + s["request"],
            "p_widget_name": "worksheet",
            "p_widget_num_return": str(PER_PAGE),
            "x01": s["worksheet"],
            "x02": s["report"],
            "p_json": json.dumps(body),
        }
        if page > 1:
            form |= {
                "p_widget_mod": "ACTION",
                "p_widget_action": "PAGE",
                "p_widget_action_mod": f"pgR_min_row={(page - 1) * PER_PAGE + 1}max_rows={PER_PAGE}rows_fetched={PER_PAGE}",
            }
        else:
            form["p_widget_mod"] = "PULL"
        url = f"{AJAX_URL}?p_context=rrs-front/ghavanin-moghararat/{s['instance']}"
        out = await http.fetch(url, data=form, cache=False)
        # APEX answers a bad session with a JSON error, an expired one with a full "session has ended" page
        if not (out.lstrip().startswith("{") or "session has ended" in out):
            return out
    raise ApiError("rrk.ir rejected the search twice (session problem). Try again in a minute.")


def _strip(s: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", s)).split())


def _parse(out: str, page: int) -> dict[str, Any]:
    m = re.search(r"a-IRR-pagination-label[^>]*>\s*(\d+)\s*-\s*(\d+)\s*\S+\s*([\d,]+)", out)
    total = int(m[3].replace(",", "")) if m else 0
    laws = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", out, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        link = re.search(r'href="([^"]*p122_code[^"]*)"', cells[0]) if cells else None
        if not link or len(cells) < 6:
            continue
        path = html.unescape(link[1])
        code = re.search(r"p122_code=(\d+)", path)[1]
        _links[code] = path
        title = re.search(r'title="([^"]*)"', cells[0])
        c = [_strip(x) for x in cells]
        laws.append(
            {
                "code": code,
                "title": html.unescape(title[1]) if title else c[0],
                "approver": c[1],
                "approved": c[2],
                "gazette_no": c[3],
                "gazette_date": c[4],
                "special_no": c[5],
            }
        )
    if not laws and "a-IRR-noDataMsg" not in out:  # a real empty result says «داده ای یافت نشد»
        raise ApiError("rrk.ir returned no law list (blocked or changed page). Try again or set RRK_MCP_PROXY.")
    for x in laws:
        x["title"] = " ".join(x["title"].split())[:MAX_TITLE]
    return {"total": total, "page": page, "more": page * PER_PAGE < total, "laws": laws}


MAX_LOOKUPS = 24  # page fetches allowed when a code has to be located in the list


async def _locate(code: str) -> None:
    """Fill `_links[code]` without a prior search: codes fall with the page number (roughly), so bisect the list."""
    n = int(code)
    items = {"TREE_VALUE": "", "ALL": "1", "TITLE": "", "HCLAWAPPROVERCODE": "", "LAWCONTENT": "", "NO": ""}
    items |= {"FROM_DATE": "", "TO_DATE": ""}
    seen: set[int] = set()
    lo, hi = 1, 1
    spans: dict[int, tuple[int, int]] = {}

    async def look(p: int) -> bool:
        nonlocal hi
        seen.add(p)
        r = _parse(await _report(items, p), p)
        if p == 1:
            hi = max(1, -(-r["total"] // PER_PAGE))
        codes = [int(x["code"]) for x in r["laws"]]
        spans[p] = (min(codes), max(codes)) if codes else (0, 0)
        return code in _links

    if await look(1):
        return
    lo = 2
    while lo <= hi and len(seen) < MAX_LOOKUPS:
        mid = (lo + hi) // 2
        if mid in seen:
            mid = next((q for q in range(lo, hi + 1) if q not in seen), 0)
            if not mid:
                break
        if await look(mid):
            return
        low, high = spans[mid]
        if n > high:
            hi = mid - 1
        elif n < low:
            lo = mid + 1
        else:  # inside the page's range but not on it: ranges overlap, so try the neighbours, then keep bisecting
            for q in (mid - 1, mid + 1, mid - 2, mid + 2, mid - 3, mid + 3):
                if 1 <= q <= hi and q not in seen and len(seen) < MAX_LOOKUPS and await look(q):
                    return
            if n > (low + high) // 2:
                hi = mid - 1
            else:
                lo = mid + 1
    raise ApiError(
        f"Law code {code} was not found in the list. Find it with rk_search_laws and pass the code from there."
    )


@tool("Search laws and regulations")
async def rk_search_laws(
    title: Annotated[str, Field(description="Words in the title, Persian (e.g. 'ماليات').")] = "",
    text: Annotated[str, Field(description="Words in the full text (slower than title).")] = "",
    special_no: Annotated[str, Field(description="Special issue number (شماره ویژه), e.g. '2301'.")] = "",
    from_date: Annotated[str, Field(description="Gazette date from, Jalali YYYY/MM/DD, e.g. '1405/08/01'.")] = "",
    to_date: Annotated[str, Field(description="Gazette date to, Jalali YYYY/MM/DD.")] = "",
    group: Annotated[
        str,
        Field(
            description="Group id or name words from rk_law_filters, e.g. 'دیوان عدالت' or '3'. "
            "A top-level group includes its sub-groups."
        ),
    ] = "",
    approver: Annotated[
        str,
        Field(description="Approver (مرجع تصویب) code or name words from rk_law_filters."),
    ] = "",
    page: Annotated[int, Field(description="Page number, 20 per page.", ge=1, le=500)] = 1,
) -> dict[str, Any]:
    """Search the Official Gazette's laws and regulations database (rrk.ir) by title, full text, group, approver, special number or gazette date.

    Filters are optional and combined; none lists the newest. Returns `total`, `more` and up to 20 `laws` with
    `code`; pass a code to rk_law_text. Laws only: company registration notices are not searchable on rrk.ir.
    """
    gid = aid = ""
    if group.strip():
        gs = (await _session())["groups"]
        gid = _pick(
            group, [(g["id"], f"{g['parent']} / {g['name']}" if "parent" in g else g["name"]) for g in gs], "group"
        )
    if approver.strip():
        aid = _pick(approver, [(a["code"], a["name"]) for a in await _approvers()], "approver")
    items = {
        "TREE_VALUE": gid,
        "ALL": "0" if gid else "1",  # 1 = search every group; 0 = only the selected group
        "TITLE": _text(title),
        "HCLAWAPPROVERCODE": aid,
        "LAWCONTENT": _text(text),
        "NO": _text(special_no),
        "FROM_DATE": _date(from_date, "from_date"),
        "TO_DATE": _date(to_date, "to_date"),
    }
    return _parse(await _report(items, page), page)


@tool("Law groups and approvers")
async def rk_law_filters(
    query: Annotated[str, Field(description="Name words to narrow the lists, e.g. 'دیوان'.")] = "",
) -> dict[str, Any]:
    """List the law groups (categories) and approving authorities that rk_search_laws can filter by.

    Use first for one kind of document (court rulings, decrees, Majlis debates) or one issuing body. Pass a group
    `id` or approver `code` to rk_search_laws. `parent` is set on sub-groups.
    """
    q = _text(query)
    groups = [g for g in (await _session())["groups"] if q in _text(f"{g.get('parent', '')} {g['name']}")]
    return {"groups": groups, "approvers": await _approvers(query)}


@tool("Latest laws and regulations")
async def rk_latest_laws(
    page: Annotated[int, Field(description="Page number, 20 per page.", ge=1, le=500)] = 1,
) -> dict[str, Any]:
    """List the newest laws, rulings and regulations published in the Official Gazette (rrk.ir), newest gazette first.

    Each law has a `code` for rk_law_text. To narrow by topic or date use rk_search_laws.
    """
    return await rk_search_laws(page=page)


@tool("Read a law's text")
async def rk_law_text(
    code: Annotated[str, Field(description="Law `code` from rk_search_laws or rk_latest_laws.")],
    offset: Annotated[int, Field(description="Character to start from.", ge=0)] = 0,
    max_chars: Annotated[int, Field(description="Most characters to return.", ge=500, le=20000)] = 6000,
) -> dict[str, Any]:
    """Read the full text of one law or ruling from the Official Gazette (rrk.ir): approver, gazette number and date, body.

    Use with a `code` from a search. An unlisted code is located by paging the list (slow, 10-60 s; old laws may
    not be found). Long texts are cut at `max_chars`; continue with `offset` = `next_offset`.
    """
    code = code.strip()
    if not code.isdigit():
        raise ApiError("code must be the number rk_search_laws gives for a law.")
    for attempt in (0, 1):
        if code not in _links:
            await _locate(code)
        m = re.search(
            r'id="P122_LAWCONTENT" name="P122_LAWCONTENT" value="([^"]*)"', await http.fetch(http.SITE + _links[code])
        )
        if m:
            break
        if attempt:
            raise ApiError("The law page had no text (the portal expired the link); search again.")
        reset()  # the remembered links belong to an expired session: start over with new cookies
        http.new_session()
    doc = html.unescape(m[1])
    ends = [x.end() for x in re.finditer(r"</tr>", doc)]
    cut = (
        ends[2] if len(ends) > 2 else 0
    )  # rows: logo, approver+date, special no+issue+title (row left unclosed); then the body
    head = [_strip(r)[:160] for r in re.findall(r"<tr[^>]*>(.*?)</tr>", doc[:cut], re.S)][1:]
    body = re.sub(r"(?i)</(p|div|tr|h\d|li)>|<br\s*/?>", "\n", doc[cut:])
    lines = (" ".join(html.unescape(re.sub(r"<[^>]+>", " ", ln)).split()) for ln in body.split("\n"))
    body = "\n".join(ln for ln in lines if ln)
    out: dict[str, Any] = {"code": code, "header": head, "length": len(body)}
    chunk = body[offset : offset + max_chars]
    out["text"] = chunk
    if offset + max_chars < len(body):
        out["next_offset"] = offset + max_chars
    return out
