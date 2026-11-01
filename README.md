<!-- mcp-name: io.github.sepehr071/rrk-mcp -->

<div align="center">

# rrk-mcp: Official Gazette of Iran (روزنامه رسمی) laws and rulings MCP server for AI agents

**rrk-mcp is an unofficial, read-only MCP server for the laws and regulations database of the Official Gazette of Iran (rrk.ir).**<br>
Its 5 tools list the newest laws, search them by title, full text, group, approver, special issue number or gazette date,<br>
read the full text of a law, court ruling or regulation, and list the licensed exchange offices. No API key or account is needed.

[![PyPI](https://img.shields.io/pypi/v/rrk-mcp?color=2563eb)](https://pypi.org/project/rrk-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/rrk-mcp)](https://pypi.org/project/rrk-mcp/)
[![License: MIT](https://img.shields.io/badge/license-MIT-16a34a)](https://github.com/sepehr071/rrk-mcp/blob/main/LICENSE)

[For AI agents](#for-ai-agents) · [Quick start](#quick-start) · [Tools](#tools) · [Limits](#limits) · [فارسی](#فارسی)

</div>

---

## For AI agents

- **What:** unofficial MCP server for rrk.ir, the Official Gazette: laws (قوانین), regulations (آیین‌نامه، مصوبات), Court of Administrative Justice rulings (آرای دیوان عدالت اداری) and other texts published in the gazette since 1285 (2026 = 1405).
- **Install:** `uvx rrk-mcp` or `claude mcp add rrk -- uvx rrk-mcp`. Transport: stdio. Needs [uv](https://docs.astral.sh/uv/getting-started/installation/) (Python 3.10+).
- **Auth:** none. Optional `RRK_MCP_PROXY` (HTTP proxy URL); system proxy settings are ignored.
- **Access:** read-only; no confirmation is needed before calling any tool.
- **Tools:** 5, all prefixed `rk_`. Typical sequence: `rk_law_filters` (optional) -> `rk_search_laws` -> `rk_law_text`.
- **Use when:** the user asks what the gazette published lately, wants a law or ruling by topic or date, or needs the text of one.
- **Do not use when:** the user wants company registration notices (founding, capital change, board change, liquidation). rrk.ir does not offer those in an open search, so this server cannot look companies up.
- **Output:** compact JSON, Persian text. Dates are Jalali (`1405/08/09`).

## Quick start

```bash
claude mcp add rrk -- uvx rrk-mcp
```

Any other client: run `uvx rrk-mcp` as a stdio MCP server (`{"command": "uvx", "args": ["rrk-mcp"]}`).

## Tools

| Tool | What it does |
|---|---|
| `rk_latest_laws` | List the newest laws, rulings and regulations in the Official Gazette, newest first |
| `rk_search_laws` | Search laws by title words, full-text words, law group, approver, special issue number or Jalali gazette date range |
| `rk_law_filters` | List the law groups (categories) and approving authorities to filter by |
| `rk_law_text` | Read one law's approver, gazette number and date, and full text (long texts in parts) |
| `rk_exchange_offices` | List the licensed currency exchange offices (location, phone, address) by province and city |

All 5 tools are annotated `readOnlyHint: true`.

## Limits

- Laws only. Company notices (آگهی‌های ثبتی) sit behind rrk.ir's paid data service and login, so they are left out.
- The portal is slow (a full-text search can take 10 seconds) and shares one session; the server keeps one session and reuses it.
- `rk_law_text` works with any code, but a code the session has not listed is located by paging the list (10-60 s, and codes below about 10,000 may not be found). Majlis debates (group 64) have a title only; the debate text is a PDF behind login.
- Gazette PDFs and the shop need login or payment, and are left out.
- Persian ی and ک are converted to the Arabic ي and ك the portal stores; you can type either.

## Development

```bash
uv sync
uv run ruff check . && uv run ruff format --check .
uv run python -m pytest -q
uv run python -m pytest -m live -q
```

## فارسی

`rrk-mcp` یک سرور MCP غیررسمی و فقط‌خواندنی برای پایگاه قوانین و مقررات روزنامه رسمی (rrk.ir) است: فهرست جدیدترین قوانین، جستجو بر اساس عنوان، متن، شماره ویژه‌نامه و تاریخ روزنامه، فیلتر بر اساس گروه قوانین و مرجع تصویب، خواندن متن کامل قانون یا رأی، و فهرست صرافی‌های مجاز بانک مرکزی. جستجوی آگهی‌های ثبتی شرکت‌ها در سایت به‌صورت باز وجود ندارد و پشتیبانی نمی‌شود.

---

Made by Sepehr Radmard (سپهر رادمرد) · [github.com/sepehr071](https://github.com/sepehr071) · [linkedin.com/in/sepehr-radmard](https://linkedin.com/in/sepehr-radmard)
