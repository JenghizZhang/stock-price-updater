import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf


# =========================================================
# Configuration
# =========================================================

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_READER_TOKEN = os.environ["NOTION_READER_TOKEN"]

NOTION_VERSION = "2026-03-11"

WRITER_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

READER_HEADERS = {
    "Authorization": f"Bearer {NOTION_READER_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


# =========================================================
# Notion: Stocks Price database
# =========================================================

def find_stock_data_source():
    url = "https://api.notion.com/v1/search"

    payload = {
        "query": "Stocks Price",
        "filter": {
            "property": "object",
            "value": "data_source"
        },
        "page_size": 20
    }

    response = requests.post(
        url,
        headers=WRITER_HEADERS,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    results = response.json()["results"]

    if not results:
        raise RuntimeError(
            "Cannot find the Stocks Price data source."
        )

    print(f"Found Stocks Price data source: {results[0]['id']}")
    return results[0]["id"]


def get_stock_rows(data_source_id):
    url = (
        f"https://api.notion.com/v1/data_sources/"
        f"{data_source_id}/query"
    )

    all_results = []
    payload = {"page_size": 100}

    while True:
        response = requests.post(
            url,
            headers=WRITER_HEADERS,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()
        all_results.extend(data["results"])

        if not data.get("has_more"):
            break

        payload["start_cursor"] = data["next_cursor"]

    return all_results


def get_ticker(page):
    title_items = page["properties"]["Ticker"]["title"]

    if not title_items:
        return None

    return "".join(
        item["plain_text"]
        for item in title_items
    ).strip().upper()


def get_last_alert_range(page):
    prop = page["properties"].get("Last Alert Range")

    if not prop:
        return ""

    items = prop.get("rich_text", [])

    return "".join(
        item.get("plain_text", "")
        for item in items
    ).strip()


# =========================================================
# Stock prices
# =========================================================

def verify_missing_ticker(ticker):
    try:
        stock = yf.Ticker(ticker)

        history = stock.history(
            period="5d",
            interval="1d",
            auto_adjust=False
        )

        if history is None or history.empty:
            print(f"{ticker}: no market data -> invalid ticker")
            return 0.0

        close = history["Close"].dropna()

        if close.empty:
            return 0.0

        price = float(close.iloc[-1])

        if price <= 0:
            return 0.0

        return price

    except Exception as exc:
        print(
            f"{ticker}: verification failed ({exc}) "
            f"-> preserve previous price"
        )
        return None


def get_prices(tickers):
    print(f"Downloading prices for: {', '.join(tickers)}")

    prices = {
        ticker: None
        for ticker in tickers
    }

    try:
        data = yf.download(
            tickers=tickers,
            period="5d",
            interval="1m",
            group_by="ticker",
            auto_adjust=False,
            prepost=True,
            progress=False,
            threads=True,
        )

    except Exception as exc:
        print(f"Yahoo batch request failed: {exc}")
        return prices

    for ticker in tickers:
        try:
            close = data[ticker]["Close"].dropna()

            if not close.empty:
                price = float(close.iloc[-1])

                if price > 0:
                    prices[ticker] = price
                    print(f"{ticker}: ${price:.2f}")
                    continue

        except Exception:
            pass

        print(f"{ticker}: batch price missing, verifying...")
        prices[ticker] = verify_missing_ticker(ticker)

    return prices


def update_notion_price(page_id, ticker, price):
    now = datetime.now(
        ZoneInfo("America/Los_Angeles")
    ).isoformat()

    payload = {
        "properties": {
            "Current Price": {
                "number": round(price, 2)
            },
            "Last Updated": {
                "date": {
                    "start": now
                }
            }
        }
    }

    url = f"https://api.notion.com/v1/pages/{page_id}"

    response = requests.patch(
        url,
        headers=WRITER_HEADERS,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    print(f"Updated price {ticker}: ${price:.2f}")


# =========================================================
# Notion: 股票 notes page
# =========================================================

def extract_page_title(page):
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(
                item.get("plain_text", "")
                for item in prop.get("title", [])
            ).strip()

    return ""


def find_stock_notes_page():
    url = "https://api.notion.com/v1/search"

    payload = {
        "query": "股票",
        "filter": {
            "property": "object",
            "value": "page"
        },
        "page_size": 20
    }

    response = requests.post(
        url,
        headers=READER_HEADERS,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    results = response.json()["results"]

    for page in results:
        title = extract_page_title(page)

        if title == "股票":
            print(f"Found 股票 page: {page['id']}")
            return page["id"]

    if len(results) == 1:
        print(
            f"Using only readable page: {results[0]['id']}"
        )
        return results[0]["id"]

    raise RuntimeError(
        "Cannot uniquely find the 股票 page."
    )


def get_block_text(block):
    block_type = block.get("type")

    data = block.get(block_type, {})

    rich_text = data.get("rich_text")

    if not rich_text:
        return ""

    return "".join(
        item.get("plain_text", "")
        for item in rich_text
    ).strip()


def get_block_children(block_id):
    all_blocks = []
    cursor = None

    while True:
        url = (
            f"https://api.notion.com/v1/blocks/"
            f"{block_id}/children?page_size=100"
        )

        if cursor:
            url += f"&start_cursor={cursor}"

        response = requests.get(
            url,
            headers=READER_HEADERS,
            timeout=30,
        )
        response.raise_for_status()

        data = response.json()

        all_blocks.extend(data["results"])

        if not data.get("has_more"):
            break

        cursor = data["next_cursor"]

    return all_blocks


# =========================================================
# Alert parsing
# =========================================================

ALERT_PATTERN = re.compile(
    r"^\s*@alert\s+"
    r"(\d+(?:\.\d+)?)"
    r"\s*[-–—~～]\s*"
    r"(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def parse_alert_line(text):
    match = ALERT_PATTERN.search(text)

    if not match:
        return None

    low = float(match.group(1))
    high = float(match.group(2))

    if low > high:
        low, high = high, low

    return {
        "low": low,
        "high": high,
        "text": text,
        "state": format_range(low, high),
    }


def format_number(value):
    if float(value).is_integer():
        return str(int(value))

    return str(value).rstrip("0").rstrip(".")


def format_range(low, high):
    return f"{format_number(low)}-{format_number(high)}"


def ticker_matches_heading(text, valid_tickers):
    """
    Examples:
        SPY             -> SPY
        SPY（*）        -> SPY
        ⭐SPY            -> SPY
        SPY重要SPY（*） -> SPY

    If two DIFFERENT valid tickers appear in one heading,
    return None to avoid guessing.
    """

    upper_text = text.upper()

    matches = set()

    for ticker in valid_tickers:
        pattern = (
            r"(?<![A-Z])"
            + re.escape(ticker)
            + r"(?![A-Z])"
        )

        if re.search(pattern, upper_text):
            matches.add(ticker)

    if len(matches) == 1:
        return next(iter(matches))

    if len(matches) > 1:
        print(
            f"Ambiguous ticker heading: {text} "
            f"-> {sorted(matches)}"
        )

    return None


def collect_alerts_from_page(page_id, valid_tickers):
    alerts = {
        ticker: []
        for ticker in valid_tickers
    }

    heading_types = {
        "heading_1",
        "heading_2",
        "heading_3",
        "toggle",
    }

    def walk(parent_id, inherited_ticker=None):
        blocks = get_block_children(parent_id)

        current_ticker = inherited_ticker

        for block in blocks:
            text = get_block_text(block)
            block_type = block.get("type")

            # Detect ticker from headings/toggles.
            if block_type in heading_types and text:
                detected = ticker_matches_heading(
                    text,
                    valid_tickers
                )

                if detected:
                    current_ticker = detected
                    print(
                        f"Detected ticker heading: "
                        f"{text} -> {detected}"
                    )

            # Parse @alert under current ticker.
            if (
                current_ticker
                and text.lower().startswith("@alert")
            ):
                alert = parse_alert_line(text)

                if alert:
                    alerts[current_ticker].append(alert)

                    print(
                        f"Found alert for {current_ticker}: "
                        f"{alert['state']}"
                    )
                else:
                    print(
                        f"Could not parse alert line: {text}"
                    )

            # Read nested Toggle / heading contents.
            if block.get("has_children"):
                walk(
                    block["id"],
                    inherited_ticker=current_ticker
                )

    walk(page_id)

    # Remove stocks that have no @alert lines.
    return {
        ticker: ranges
        for ticker, ranges in alerts.items()
        if ranges
    }


# =========================================================
# Alert state
# =========================================================

def determine_alert_state(price, alerts):
    """
    Return:
        "702-724"
        "ABOVE 724"
        "BELOW 588"
        ""
    """

    if not alerts or price is None or price <= 0:
        return ""

    # First check actual monitored ranges.
    for alert in alerts:
        if alert["low"] <= price <= alert["high"]:
            return alert["state"]

    highest = max(
        alert["high"]
        for alert in alerts
    )

    lowest = min(
        alert["low"]
        for alert in alerts
    )

    if price > highest:
        return f"ABOVE {format_number(highest)}"

    if price < lowest:
        return f"BELOW {format_number(lowest)}"

    # Price is between monitored ranges.
    return ""


def update_last_alert_range(
    page_id,
    ticker,
    new_state,
):
    rich_text = []

    if new_state:
        rich_text = [
            {
                "type": "text",
                "text": {
                    "content": new_state
                }
            }
        ]

    payload = {
        "properties": {
            "Last Alert Range": {
                "rich_text": rich_text
            }
        }
    }

    url = f"https://api.notion.com/v1/pages/{page_id}"

    response = requests.patch(
        url,
        headers=WRITER_HEADERS,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    print(
        f"{ticker}: Last Alert Range -> "
        f"{new_state or '(empty)'}"
    )


def process_alert_states(
    ticker_info,
    prices,
    alerts_by_ticker,
):
    for ticker, alerts in alerts_by_ticker.items():

        info = ticker_info.get(ticker)

        if not info:
            print(
                f"{ticker}: not found in Stocks Price, skipping"
            )
            continue

        price = prices.get(ticker)

        # No fresh valid market price -> do nothing.
        if price is None or price <= 0:
            print(
                f"{ticker}: invalid/stale price, "
                f"alert check skipped"
            )
            continue

        old_state = info["last_alert_range"]

        new_state = determine_alert_state(
            price,
            alerts
        )

        print(
            f"{ticker}: price={price:.2f}, "
            f"old={old_state or '(empty)'}, "
            f"new={new_state or '(empty)'}"
        )

        if new_state == old_state:
            continue

        # FIRST RUN:
        # establish current state without sending an alert.
        #
        # Notification will be added in the next step.
        if not old_state:
            print(
                f"{ticker}: initializing alert state "
                f"without notification."
            )

        else:
            if new_state:
                print(
                    f"ALERT STATE CHANGE: {ticker} "
                    f"{old_state} -> {new_state} "
                    f"at ${price:.2f}"
                )
            else:
                print(
                    f"{ticker}: left monitored range "
                    f"{old_state}"
                )

        update_last_alert_range(
            info["page_id"],
            ticker,
            new_state,
        )


# =========================================================
# Main
# =========================================================

def main():
    # 1. Read Stocks Price database.
    data_source_id = find_stock_data_source()
    pages = get_stock_rows(data_source_id)

    ticker_info = {}

    for page in pages:
        ticker = get_ticker(page)

        if not ticker:
            continue

        ticker_info[ticker] = {
            "page_id": page["id"],
            "last_alert_range": get_last_alert_range(page),
        }

    if not ticker_info:
        raise RuntimeError(
            "No tickers found in Stocks Price."
        )

    tickers = list(ticker_info.keys())

    # 2. Download prices.
    prices = get_prices(tickers)

    # 3. Update Current Price / Last Updated.
    for ticker in tickers:
        price = prices.get(ticker)

        if price is None:
            print(
                f"Skipping {ticker}: temporary market data failure."
            )
            continue

        update_notion_price(
            ticker_info[ticker]["page_id"],
            ticker,
            price,
        )

    # 4. Read @alert lines from 股票 page.
    notes_page_id = find_stock_notes_page()

    alerts_by_ticker = collect_alerts_from_page(
        notes_page_id,
        set(tickers),
    )

    print(
        "Tickers with alerts: "
        + ", ".join(sorted(alerts_by_ticker.keys()))
    )

    # 5. Determine and persist alert state.
    process_alert_states(
        ticker_info,
        prices,
        alerts_by_ticker,
    )

    print("Finished.")


if __name__ == "__main__":
    main()
