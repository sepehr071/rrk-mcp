"""Tool for the list of licensed currency exchange offices that the Official Gazette publishes (rrk.ir, page 176).

Same APEX mechanics as laws.py: load the page once for a session, then post the classic report's refresh call.
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
from .laws import AJAX_URL, _field, _strip
from .registry import tool

PAGE_URL = f"{http.SITE}/ords/r/rrs/rrs-front/r-exchanges"
PER_PAGE = 50  # the report's fixed page size
COLUMNS = ("STATENAME", "CITYNAME", "REGION", "PHONENUMBER", "ZIPCODE", "ADDRESS")

_state: dict[str, Any] = {}
_lock: asyncio.Lock | None = None


class _SessionLost(ApiError):
    """The portal answered with something other than the report (expired session); retried once with a new one."""


def reset() -> None:
    _state.clear()


async def _session(force: bool = False) -> dict[str, Any]:
    global _lock
    _lock = _lock or asyncio.Lock()
    async with _lock:
        if force or not _state:
            h = await http.fetch(PAGE_URL, cache=False)
            sel = re.search(r'<select[^>]*id="P176_PROVINCE".*?</select>', h, re.S)
            _state.clear()
            _state.update(
                instance=_field(h, r'name="p_instance" value="(\d+)"'),
                salt=_field(h, r'value="(\d+)" id="pSalt"'),
                protected=_field(h, r'id="pPageItemsProtected" value="([^"]+)"'),
                region=_field(h, r'apex\.widget\.report\.init\("R(\d+)"'),
                request=_field(h, r'apex\.widget\.report\.init\("R\d+","([^"]+)"').replace("\\u002F", "/"),
                provinces=[html.unescape(v) for v in re.findall(r'<option value="([^"]+)"', sel[0])] if sel else [],
            )
        return dict(_state)


def _val(s: str) -> str | None:
    """A cell's text, or None when the portal shows nothing or a dash."""
    t = _strip(s)
    return t if t.strip("-") else None


def _parse(out: str, page: int) -> dict[str, Any]:
    if "t-Report" not in out:
        raise _SessionLost("rrk.ir returned no exchange list (session problem). Try again in a minute.")
    m = re.search(r"t-Report-paginationText\">[^<]*?(\d+)\s*-\s*(\d+)\s*\S+\s*([\d,]+)", out)
    total = int(m[3].replace(",", "")) if m else 0
    offices = []
    for row in re.findall(r"<tr>(<td class=\"t-Report-cell\".*?)</tr>", out, re.S):
        cells = dict(re.findall(r'headers="([A-Z]+)"[^>]*>(.*?)</td>', row, re.S))
        offices.append(
            {
                "province": _strip(cells.get("STATENAME", "")),
                "city": _strip(cells.get("CITYNAME", "")),
                "district": _val(cells.get("REGION", "")),
                "phone": _val(cells.get("PHONENUMBER", "")),
                "postal_code": _val(cells.get("ZIPCODE", "")),
                "address": _val(cells.get("ADDRESS", "")),
            }
        )
    return {"total": total, "page": page, "more": page * PER_PAGE < total, "offices": offices}


_PERSIAN = str.maketrans({"ي": "ی", "ك": "ک", "ى": "ی"})  # the province/city lists use Persian ی and ک


@tool("Licensed exchange offices")
async def rk_exchange_offices(
    province: Annotated[str, Field(description="Province name as on the site, e.g. 'تهران', 'قم'. Empty = all.")] = "",
    city: Annotated[
        str, Field(description="City name as on the site, e.g. 'قم', 'سلفچگان'. Empty = whole province.")
    ] = "",
    page: Annotated[int, Field(description="Page number, 50 offices per page.", ge=1, le=200)] = 1,
) -> dict[str, Any]:
    """List the licensed currency exchange offices (صرافی مجاز) the Central Bank published in the Official Gazette (rrk.ir).

    Use to see where licensed exchange offices are or to find one's address and phone. Filter by `province` and
    `city` (Persian names; an unknown province is an error that lists the valid ones). Returns `total`, `more`
    and up to 50 `offices` with province, city, district, phone, postal_code and address. Office names are not
    published, only the location.
    """
    province, city = province.translate(_PERSIAN).strip(), city.translate(_PERSIAN).strip()
    try:
        return await _list(province, city, page, False)
    except _SessionLost:
        return await _list(province, city, page, True)


async def _list(province: str, city: str, page: int, force: bool) -> dict[str, Any]:
    s = await _session(force)
    if province and province not in s["provinces"]:
        raise ApiError(f"Unknown province {province!r}. Valid: " + ", ".join(s["provinces"]))
    if city and not province:
        raise ApiError("Give the province together with the city.")
    form = {
        "p_flow_id": "200",
        "p_flow_step_id": "176",
        "p_instance": s["instance"],
        "p_debug": "",
        "p_request": "PLUGIN=" + s["request"],
        "x01": s["region"],
        "p_widget_action": "reset",
        "p_json": json.dumps(
            {
                "pageItems": {
                    "itemsToSubmit": [{"n": "P176_PROVINCE", "v": province}, {"n": "P176_CITY", "v": city}],
                    "protected": s["protected"],
                    "rowVersion": "",
                    "formRegionChecksums": [],
                },
                "salt": s["salt"],
            }
        ),
    }
    if page > 1:
        form |= {
            "p_widget_action": "paginate",
            "p_pg_min_row": str((page - 1) * PER_PAGE + 1),
            "p_pg_max_rows": str(PER_PAGE),
            "p_pg_rows_fetched": str(PER_PAGE),
        }
    out = await http.fetch(f"{AJAX_URL}?p_context=rrs-front/r-exchanges/{s['instance']}", data=form)
    r = _parse(out, page)
    if city and not r["total"]:
        r["note"] = f"No office in city {city!r}; use the city name exactly as the site writes it."
    return r
