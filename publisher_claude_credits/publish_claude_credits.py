#!/usr/bin/env python3
"""
Claude API credits remaining publisher (Anthropic Admin API -> broker).

Important limitation, worth reading before configuring this: Anthropic
does NOT expose your organization's credit balance through any public
API -- the Console UI shows it, but there is no "GET /v1/organizations/
balance" or similar. What the Admin API *does* expose is a Cost Report
(https://platform.claude.com/docs/en/manage-claude/usage-cost-api) --
actual USD spend over a date range. So "credits remaining" here is
computed, not fetched directly:

    remaining = CLAUDE_CREDIT_BUDGET_USD - (cost incurred since
                CLAUDE_CREDIT_PERIOD_START, from the Cost Report)

You tell this script what your budget is (e.g. what you've actually
loaded/allocated for your n8n workflows specifically); it tells you how
much of that's left. It does NOT know your real account balance -- if
credits get spent by something other than what CLAUDE_WORKSPACE_ID /
CLAUDE_API_KEY_IDS scopes this to, or a top-up happens outside
CLAUDE_CREDIT_PERIOD_START, this number will drift from reality. Treat it
as a budget tracker, not an authoritative balance.

Requires an Admin API key (sk-ant-admin01-..., NOT a regular API key) --
see https://platform.claude.com/docs/en/manage-claude/admin-api-keys for
where to create one (needs Cost Report read access).

To scope this to just what your n8n workflows spend (rather than your
whole organization's Claude usage), set CLAUDE_WORKSPACE_ID and/or
CLAUDE_API_KEY_IDS to whatever workspace/key your n8n workflows actually
call Claude through -- if n8n shares a workspace/key with other things,
this can't separate them out; Anthropic's cost data doesn't go finer than
workspace/API key.

Config is entirely environment variables (no config file):
    ANTHROPIC_ADMIN_KEY      Admin API key (required)
    CLAUDE_CREDIT_BUDGET_USD  total budget to track against, in USD
                              (required, e.g. "50.00")
    CLAUDE_CREDIT_PERIOD_START  ISO date (e.g. "2026-07-01") to start
                              summing cost from -- typically your billing
                              cycle start or last top-up date. Default:
                              the 1st of the current UTC month.
    CLAUDE_WORKSPACE_ID       optional -- restrict cost to one workspace
    CLAUDE_API_KEY_IDS        optional, comma-separated -- restrict cost
                              to specific API key IDs (see the List API
                              Keys admin endpoint to find these)
    CLAUDE_CREDIT_WARN_FRACTION  default "0.1" -- turn the metric red once
                              remaining budget drops to/below this
                              fraction (0.1 = 10% left)
    CLAUDE_COST_UNIT          "dollars" (default) or "cents" -- see the
                              _fetch_total_cost_usd() docstring; if
                              --dry-run's number is ~100x off from what
                              you know you've actually spent, flip this
    CLAUDE_CREDITS_LABEL      default "n8n Credits"
    BROKER_URL                e.g. http://localhost:9090 (required)
    DASHBOARD_TOKEN            same token the broker/other publishers use

Usage:
    python3 publish_claude_credits.py            # fetch + push
    python3 publish_claude_credits.py --dry-run  # fetch + print, don't push

Example crontab (every 30 minutes -- this is spend data, not something
that changes second to second; own line/log file like every other
publisher here):
    */30 * * * * ANTHROPIC_ADMIN_KEY=sk-ant-admin01-... \\
        CLAUDE_CREDIT_BUDGET_USD=50.00 CLAUDE_WORKSPACE_ID=wrkspc_... \\
        BROKER_URL=http://localhost:9090 DASHBOARD_TOKEN=... \\
        /usr/bin/python3 /path/to/eink_dashboard/publisher_claude_credits/publish_claude_credits.py \\
        >> /var/log/claude_credits_publish.log 2>&1
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s publish_claude_credits: %(message)s"
)
log = logging.getLogger("publish_claude_credits")

COST_REPORT_URL = "https://api.anthropic.com/v1/organizations/cost_report"
ANTHROPIC_VERSION = "2023-06-01"


class ClaudeCreditsError(RuntimeError):
    pass


def _get_json(url: str, params: dict[str, Any], admin_key: str) -> dict[str, Any]:
    full_url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
    req = urllib.request.Request(
        full_url,
        headers={
            "Accept": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            "x-api-key": admin_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            pass
        raise ClaudeCreditsError(f"HTTP {exc.code} calling {url}: {exc.reason} -- {body}") from exc
    except urllib.error.URLError as exc:
        raise ClaudeCreditsError(f"couldn't reach {url}: {exc.reason}") from exc


def _fetch_total_cost_usd(
    admin_key: str, starting_at: str, ending_at: str,
    workspace_id: Optional[str], api_key_ids: list[str], cost_unit: str,
) -> Decimal:
    """Sums the Cost Report's `amount` field across every bucket/page in
    [starting_at, ending_at).

    Unit ambiguity worth flagging explicitly: Anthropic's docs describe
    amounts as "decimal strings in lowest units (cents)", which could
    mean either "123.45" (dollars, as a decimal string) or "12345" (an
    integer number of cents, as a string) depending on how literally you
    read "lowest units" -- the two published example requests in the docs
    don't show a full example response to settle it either way. Rather
    than guess silently and risk a 100x-wrong number on the dashboard,
    CLAUDE_COST_UNIT lets you pick, and main() logs the raw total
    alongside the interpreted one so a --dry-run run can be sanity-checked
    against what you already know you've spent (the Console's Cost page)
    before trusting it unattended."""
    total = Decimal("0")
    page: Optional[str] = None
    pages_fetched = 0
    while True:
        params: dict[str, Any] = {
            "starting_at": starting_at,
            "ending_at": ending_at,
            "limit": 31,  # max daily buckets per request
        }
        if workspace_id:
            params["group_by[]"] = "workspace_id"
        if page:
            params["page"] = page

        result = _get_json(COST_REPORT_URL, params, admin_key)
        pages_fetched += 1

        for bucket in result.get("data") or []:
            for item in bucket.get("results") or []:
                if workspace_id and item.get("workspace_id") != workspace_id:
                    continue
                raw = item.get("amount")
                if raw is None:
                    continue
                try:
                    total += Decimal(str(raw))
                except InvalidOperation:
                    log.warning("skipping unparseable cost amount %r", raw)

        if not result.get("has_more"):
            break
        page = result.get("next_page")
        if not page:
            break
        if pages_fetched > 40:  # safety valve, shouldn't happen at 1d granularity
            log.warning("cost report pagination exceeded 40 pages, stopping early")
            break

    if cost_unit == "cents":
        total = total / Decimal("100")
    return total


def build_payload(
    spent_usd: Decimal, budget_usd: Decimal, warn_fraction: float, label: str
) -> dict[str, dict]:
    remaining = budget_usd - spent_usd
    if budget_usd > 0:
        fraction_left = remaining / budget_usd
    else:
        fraction_left = Decimal("0")

    is_low = fraction_left <= Decimal(str(warn_fraction))
    value = f"${remaining:,.2f}"

    return {
        "claude_credits": {
            "label": label,
            "value": value,
            "unit": "",
            "color": "red" if is_low else "black",
        }
    }


def push_to_broker(broker_url: str, token: str, payload: dict[str, dict]) -> None:
    url = f"{broker_url.rstrip('/')}/api/v1/widgets/bulk"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"broker rejected push: HTTP {exc.code} {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"couldn't reach broker at {broker_url}: {exc.reason}") from exc


def _default_period_start() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-01T00:00:00Z")


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude credits remaining -> broker publisher")
    parser.add_argument("--dry-run", action="store_true", help="fetch + print payload, don't push")
    args = parser.parse_args()

    admin_key = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
    budget_raw = os.environ.get("CLAUDE_CREDIT_BUDGET_USD", "")
    period_start = os.environ.get("CLAUDE_CREDIT_PERIOD_START", "").strip()
    workspace_id = os.environ.get("CLAUDE_WORKSPACE_ID", "").strip() or None
    api_key_ids = [k.strip() for k in os.environ.get("CLAUDE_API_KEY_IDS", "").split(",") if k.strip()]
    warn_fraction = float(os.environ.get("CLAUDE_CREDIT_WARN_FRACTION", "0.1"))
    cost_unit = os.environ.get("CLAUDE_COST_UNIT", "dollars").strip().lower()
    if cost_unit not in ("dollars", "cents"):
        log.warning("CLAUDE_COST_UNIT %r not recognized, defaulting to dollars", cost_unit)
        cost_unit = "dollars"
    label = os.environ.get("CLAUDE_CREDITS_LABEL", "n8n Credits")
    broker_url = os.environ.get("BROKER_URL", "")
    token = os.environ.get("DASHBOARD_TOKEN", "")

    if not admin_key:
        log.error("ANTHROPIC_ADMIN_KEY is not set (needs an sk-ant-admin01-... key)")
        return 1
    if not budget_raw:
        log.error(
            "CLAUDE_CREDIT_BUDGET_USD is not set -- required, since Anthropic has no API "
            "for your actual balance; this script can only track spend against a budget "
            "you tell it (see the module docstring)"
        )
        return 1
    try:
        budget_usd = Decimal(budget_raw)
    except InvalidOperation:
        log.error("CLAUDE_CREDIT_BUDGET_USD=%r is not a valid number", budget_raw)
        return 1

    if not period_start:
        period_start = _default_period_start()
    ending_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
    # Cost Report is daily-granularity only -- push ending_at to the start
    # of *tomorrow* (UTC) so today's own bucket is actually included,
    # rather than being excluded by an exclusive upper bound landing on
    # today's midnight.
    ending_dt = datetime.strptime(ending_at, "%Y-%m-%dT00:00:00Z").replace(
        tzinfo=timezone.utc
    ) + timedelta(days=1)
    ending_at = ending_dt.strftime("%Y-%m-%dT00:00:00Z")

    if not args.dry_run and not broker_url:
        log.error("BROKER_URL is not set (e.g. http://localhost:9090)")
        return 1

    try:
        spent_usd = _fetch_total_cost_usd(
            admin_key, period_start, ending_at, workspace_id, api_key_ids, cost_unit
        )
    except ClaudeCreditsError as exc:
        log.error("%s", exc)
        return 1

    log.info(
        "cost report %s -> %s: spent=$%.2f (unit=%s, workspace=%s)",
        period_start, ending_at, spent_usd, cost_unit, workspace_id or "<org-wide>",
    )

    payload = build_payload(spent_usd, budget_usd, warn_fraction, label)

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0

    try:
        push_to_broker(broker_url, token, payload)
    except RuntimeError as exc:
        log.error("%s", exc)
        return 1

    log.info("pushed Claude credits remaining: %s", payload["claude_credits"]["value"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
