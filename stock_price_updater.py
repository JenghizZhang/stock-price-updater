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

# Stocks Price database:
# Read + Update
WRITER_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}

# 股票 page:
# Read content + Insert comments
READER_HEADERS = {
    "Authorization": f"Bearer {NOTION_READER_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


# =========================================================
# Notion: Stocks Price database
# =========================================================

def find_stock_data_source():
    """
    Find the Stocks Price data source.
    """

    url = "https://api.notion.com/v1/search"

    payload = {
        "query": "Stocks Price",
        "filter": {
            "property": "object",
            "value": "data_source",
        },
        "page_size": 20,
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

    print(
        f"Found Stocks Price data source: "
        f"{results[0]['id']}"
    )

    return results[0]["id"]


def get_stock_rows(data_source_id):
    """
    Read all rows from Stocks Price.
    """

    url = (
        f"https://api.notion.com/v1/data_sources/"
        f"{data_source_id}/query"
    )

    all_results = []

    payload = {
        "page_size": 100
    }

    while True:

        response = requests.post(
            url,
            headers=WRITER_HEADERS,
            json=payload,
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        all_results.extend(
            data["results"]
        )

        if not data.get("has_more"):
            break

        payload["start_cursor"] = (
            data["next_cursor"]
        )

    return all_results


def get_ticker(page):
    """
    Read ticker symbol from a Stocks Price row.
    """

    title_items = (
        page["properties"]["Ticker"]["title"]
    )

    if not title_items:
        return None

    ticker = "".join(
        item["plain_text"]
        for item in title_items
    )

    return ticker.strip().upper()


def get_last_alert_range(page):
    """
    Read internal alert state.
    """

    prop = (
        page["properties"]
        .get("Last Alert Range")
    )

    if not prop:
        return ""

    items = prop.get(
        "rich_text",
        [],
    )

    return "".join(
        item.get("plain_text", "")
        for item in items
    ).strip()


# =========================================================
# Stock prices
# =========================================================

def verify_missing_ticker(ticker):
    """
    If the batch request cannot return a ticker,
    verify it individually.

    Returns:

        positive float
            Valid ticker.

        0.0
            Appears to be invalid.

        None
            Temporary Yahoo/network error.
            Keep existing Notion price.
    """

    try:

        stock = yf.Ticker(
            ticker
        )

        history = stock.history(
            period="5d",
            interval="1d",
            auto_adjust=False,
        )

        if (
            history is None
            or history.empty
        ):

            print(
                f"{ticker}: no market data "
                f"-> invalid ticker"
            )

            return 0.0

        close = (
            history["Close"]
            .dropna()
        )

        if close.empty:

            print(
                f"{ticker}: no closing price "
                f"-> invalid ticker"
            )

            return 0.0

        price = float(
            close.iloc[-1]
        )

        if price <= 0:
            return 0.0

        print(
            f"{ticker}: verification "
            f"succeeded -> ${price:.2f}"
        )

        return price

    except Exception as exc:

        print(
            f"{ticker}: verification failed "
            f"({exc}) -> preserve previous price"
        )

        return None


def get_prices(tickers):
    """
    Get latest market price.
    """

    print(
        "Downloading prices for: "
        + ", ".join(tickers)
    )

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

        print(
            f"Yahoo batch request failed: {exc}"
        )

        print(
            "Keeping all existing Notion prices."
        )

        return prices

    for ticker in tickers:

        try:

            close = (
                data[ticker]["Close"]
                .dropna()
            )

            if not close.empty:

                price = float(
                    close.iloc[-1]
                )

                if price > 0:

                    prices[ticker] = price

                    print(
                        f"{ticker}: "
                        f"${price:.2f}"
                    )

                    continue

        except Exception:
            pass

        print(
            f"{ticker}: batch price missing, "
            f"verifying..."
        )

        prices[ticker] = (
            verify_missing_ticker(
                ticker
            )
        )

    return prices


def update_notion_price(
    page_id,
    ticker,
    price,
):
    """
    Update Current Price and Last Updated.
    """

    now = datetime.now(
        ZoneInfo(
            "America/Los_Angeles"
        )
    ).isoformat()

    payload = {
        "properties": {
            "Current Price": {
                "number": round(
                    price,
                    2,
                )
            },
            "Last Updated": {
                "date": {
                    "start": now
                }
            },
        }
    }

    url = (
        f"https://api.notion.com/v1/pages/"
        f"{page_id}"
    )

    response = requests.patch(
        url,
        headers=WRITER_HEADERS,
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    if price == 0:

        print(
            f"Updated price {ticker}: "
            f"INVALID TICKER -> $0"
        )

    else:

        print(
            f"Updated price {ticker}: "
            f"${price:.2f}"
        )


# =========================================================
# Notion: 股票 page
# =========================================================

def extract_page_title(page):
    """
    Extract title from a Notion page result.
    """

    for prop in (
        page.get(
            "properties",
            {}
        ).values()
    ):

        if prop.get("type") == "title":

            return "".join(
                item.get(
                    "plain_text",
                    ""
                )
                for item in prop.get(
                    "title",
                    []
                )
            ).strip()

    return ""


def find_stock_notes_page():
    """
    Find 股票 page using Stock Alert Reader.
    """

    url = "https://api.notion.com/v1/search"

    payload = {
        "query": "股票",
        "filter": {
            "property": "object",
            "value": "page",
        },
        "page_size": 20,
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

        title = extract_page_title(
            page
        )

        if title == "股票":

            print(
                f"Found 股票 page: "
                f"{page['id']}"
            )

            return page["id"]

    if len(results) == 1:

        print(
            "Using only readable page: "
            f"{results[0]['id']}"
        )

        return results[0]["id"]

    raise RuntimeError(
        "Cannot uniquely find 股票 page."
    )


def get_block_text(block):
    """
    Extract plain text from a Notion block.
    """

    block_type = block.get(
        "type"
    )

    if not block_type:
        return ""

    data = block.get(
        block_type,
        {},
    )

    rich_text = data.get(
        "rich_text"
    )

    if not rich_text:
        return ""

    return "".join(
        item.get(
            "plain_text",
            ""
        )
        for item in rich_text
    ).strip()


def get_block_children(block_id):
    """
    Read all children of a Notion block.
    """

    all_blocks = []

    cursor = None

    while True:

        url = (
            f"https://api.notion.com/v1/blocks/"
            f"{block_id}/children"
            f"?page_size=100"
        )

        if cursor:

            url += (
                f"&start_cursor={cursor}"
            )

        response = requests.get(
            url,
            headers=READER_HEADERS,
            timeout=30,
        )

        response.raise_for_status()

        data = response.json()

        all_blocks.extend(
            data["results"]
        )

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


def format_number(value):
    """
    Examples:

        702.0 -> 702
        96.4  -> 96.4
    """

    if float(value).is_integer():

        return str(
            int(value)
        )

    return (
        str(value)
        .rstrip("0")
        .rstrip(".")
    )


def format_range(low, high):

    return (
        f"{format_number(low)}"
        f"-"
        f"{format_number(high)}"
    )


def parse_alert_line(text):
    """
    Example:

        @alert 702-724（强）破位走弱 激进

    becomes:

        low = 702
        high = 724
        range = 702-724
        display_text = 702-724（强）破位走弱 激进
    """

    match = ALERT_PATTERN.search(
        text
    )

    if not match:
        return None

    low = float(
        match.group(1)
    )

    high = float(
        match.group(2)
    )

    if low > high:

        low, high = (
            high,
            low,
        )

    display_text = re.sub(
        r"^\s*@alert\s+",
        "",
        text,
        count=1,
        flags=re.IGNORECASE,
    ).strip()

    return {
        "low": low,
        "high": high,
        "text": text,
        "display_text": display_text,
        "range": format_range(
            low,
            high,
        ),
    }


# =========================================================
# Ticker heading detection
# =========================================================

def ticker_matches_heading(
    text,
    valid_tickers,
):
    """
    Supports headings such as:

        SPY
        SPY（*）
        ⭐SPY
        SPY重要
        SPY重要SPY（*）

    Repeated same ticker is OK.

    Two different ticker symbols in the same
    heading are considered ambiguous.
    """

    upper_text = text.upper()

    matches = set()

    for ticker in valid_tickers:

        pattern = (
            r"(?<![A-Z])"
            + re.escape(ticker)
            + r"(?![A-Z])"
        )

        if re.search(
            pattern,
            upper_text,
        ):

            matches.add(
                ticker
            )

    if len(matches) == 1:

        return next(
            iter(matches)
        )

    if len(matches) > 1:

        print(
            f"Ambiguous ticker heading: "
            f"{text} -> {sorted(matches)}"
        )

    return None


# =========================================================
# Collect @alert + heading block IDs
# =========================================================

def collect_alerts_from_page(
    page_id,
    valid_tickers,
):
    """
    Parse 股票 page by stock section.

    @alert lines may be:

        - separate paragraphs
        - several lines in one paragraph
        - inside toggles
        - separated by unrelated notes
        - separated by images / embeds
        - nested inside child blocks

    Other content is ignored.

    Returns:

        alerts_by_ticker

        ticker_heading_ids

    Example:

        ticker_heading_ids["QQQ"]
            -> Notion block ID of QQQ heading
    """

    alerts = {
        ticker: []
        for ticker in valid_tickers
    }

    ticker_heading_ids = {}

    heading_types = {
        "heading_1",
        "heading_2",
        "heading_3",
        "toggle",
    }

    # -----------------------------------------------------
    # Scan a block and all descendants for @alert
    # -----------------------------------------------------

    def extract_alerts_from_block(
        block,
        ticker,
    ):

        text = get_block_text(
            block
        )

        if text:

            for raw_line in (
                text.splitlines()
            ):

                line = (
                    raw_line.strip()
                )

                if not line:
                    continue

                if not (
                    line.lower()
                    .startswith("@alert")
                ):
                    continue

                alert = (
                    parse_alert_line(
                        line
                    )
                )

                if not alert:

                    print(
                        f"Could not parse alert "
                        f"for {ticker}: {line}"
                    )

                    continue

                alerts[ticker].append(
                    alert
                )

                print(
                    f"Found alert for {ticker}: "
                    f"{alert['range']} | "
                    f"{alert['display_text']}"
                )

        # Recursively inspect children
        if block.get(
            "has_children"
        ):

            children = (
                get_block_children(
                    block["id"]
                )
            )

            for child in children:

                extract_alerts_from_block(
                    child,
                    ticker,
                )

    # -----------------------------------------------------
    # Read top-level 股票 blocks
    # -----------------------------------------------------

    top_blocks = get_block_children(
        page_id
    )

    current_ticker = None

    for block in top_blocks:

        text = get_block_text(
            block
        )

        block_type = block.get(
            "type"
        )

        detected = None

        # -------------------------------------------------
        # Does this block begin a new stock section?
        # -------------------------------------------------

        if (
            block_type in heading_types
            and text
        ):

            detected = (
                ticker_matches_heading(
                    text,
                    valid_tickers,
                )
            )

        if detected:

            current_ticker = detected

            # IMPORTANT:
            # Remember this exact title / toggle block.
            # Alert comments will be attached here.
            ticker_heading_ids[
                detected
            ] = block["id"]

            print(
                f"\nDetected ticker section: "
                f"{text} -> {detected}"
            )

            print(
                f"{detected}: heading block "
                f"-> {block['id']}"
            )

            # If ticker section itself is a toggle,
            # scan everything nested inside it.
            if block.get(
                "has_children"
            ):

                children = (
                    get_block_children(
                        block["id"]
                    )
                )

                for child in children:

                    extract_alerts_from_block(
                        child,
                        current_ticker,
                    )

            continue

        # -------------------------------------------------
        # Blocks following ticker heading belong to that
        # ticker until another ticker heading is found.
        # -------------------------------------------------

        if current_ticker:

            extract_alerts_from_block(
                block,
                current_ticker,
            )

    # -----------------------------------------------------
    # Remove duplicate numeric ranges while preserving
    # original Notion order.
    # -----------------------------------------------------

    cleaned = {}

    for ticker, ticker_alerts in (
        alerts.items()
    ):

        if not ticker_alerts:
            continue

        seen = set()

        unique_alerts = []

        for alert in ticker_alerts:

            key = (
                alert["low"],
                alert["high"],
            )

            if key in seen:
                continue

            seen.add(
                key
            )

            unique_alerts.append(
                alert
            )

        cleaned[ticker] = (
            unique_alerts
        )

    return (
        cleaned,
        ticker_heading_ids,
    )


# =========================================================
# Alert state logic
# =========================================================

def determine_alert_state(
    price,
    alerts,
):
    """
    Every valid price belongs to one state:

        RANGE:702-724

        GAP:687-697|702-724

        ABOVE:702-724

        BELOW:588-613
    """

    if (
        not alerts
        or price is None
        or price <= 0
    ):

        return None

    sorted_alerts = sorted(
        alerts,
        key=lambda x: (
            x["low"],
            x["high"],
        ),
    )

    # -----------------------------------------------------
    # RANGE
    # -----------------------------------------------------

    for alert in alerts:

        if (
            alert["low"]
            <= price
            <= alert["high"]
        ):

            return {
                "kind": "RANGE",
                "key": (
                    f"RANGE:"
                    f"{alert['range']}"
                ),
                "alert": alert,
            }

    lowest_alert = (
        sorted_alerts[0]
    )

    highest_alert = (
        sorted_alerts[-1]
    )

    # -----------------------------------------------------
    # BELOW
    # -----------------------------------------------------

    if (
        price
        < lowest_alert["low"]
    ):

        return {
            "kind": "BELOW",
            "key": (
                f"BELOW:"
                f"{lowest_alert['range']}"
            ),
            "alert": lowest_alert,
        }

    # -----------------------------------------------------
    # ABOVE
    # -----------------------------------------------------

    if (
        price
        > highest_alert["high"]
    ):

        return {
            "kind": "ABOVE",
            "key": (
                f"ABOVE:"
                f"{highest_alert['range']}"
            ),
            "alert": highest_alert,
        }

    # -----------------------------------------------------
    # GAP
    # -----------------------------------------------------

    for index in range(
        len(sorted_alerts) - 1
    ):

        lower_alert = (
            sorted_alerts[index]
        )

        upper_alert = (
            sorted_alerts[
                index + 1
            ]
        )

        if (
            lower_alert["high"]
            < price
            < upper_alert["low"]
        ):

            return {
                "kind": "GAP",
                "key": (
                    f"GAP:"
                    f"{lower_alert['range']}"
                    f"|"
                    f"{upper_alert['range']}"
                ),
                "lower_alert": (
                    lower_alert
                ),
                "upper_alert": (
                    upper_alert
                ),
            }

    return None


# =========================================================
# Alert message
# =========================================================

def build_alert_list_text(alerts):
    """
    Preserve original Notion order.

    @alert itself is removed from the displayed message.
    """

    return "\n".join(
        alert["display_text"]
        for alert in alerts
    )


def build_alert_message(
    ticker,
    price,
    state,
    alerts,
):
    """
    Build user-facing alert message.
    """

    price_text = (
        f"{price:.2f}"
    )

    kind = state["kind"]

    if kind == "RANGE":

        current_line = (
            state["alert"]
            ["display_text"]
        )

        headline = (
            f"{ticker} 价格为 "
            f"{price_text}，"
            f"进入“{current_line}”"
        )

    elif kind == "ABOVE":

        highest_line = (
            state["alert"]
            ["display_text"]
        )

        headline = (
            f"{ticker} 价格为 "
            f"{price_text}，"
            f"超过最高范围设置，"
            f"即“{highest_line}”"
        )

    elif kind == "BELOW":

        lowest_line = (
            state["alert"]
            ["display_text"]
        )

        headline = (
            f"{ticker} 价格为 "
            f"{price_text}，"
            f"低于最低范围设置，"
            f"即“{lowest_line}”"
        )

    elif kind == "GAP":

        lower_line = (
            state[
                "lower_alert"
            ]["display_text"]
        )

        upper_line = (
            state[
                "upper_alert"
            ]["display_text"]
        )

        headline = (
            f"{ticker} 价格为 "
            f"{price_text}，"
            f"处于“{lower_line}”"
            f"和"
            f"“{upper_line}”之间"
        )

    else:

        headline = (
            f"{ticker} 价格为 "
            f"{price_text}，"
            f"Alert 状态发生变化"
        )

    all_alerts = (
        build_alert_list_text(
            alerts
        )
    )

    return (
        f"🔔 {headline}\n\n"
        f"{all_alerts}"
    )


# =========================================================
# Notion Comment
# =========================================================

def split_text_for_notion(
    text,
    chunk_size=1800,
):
    """
    Split long messages into multiple rich_text objects.

    This keeps one alert as ONE Notion comment,
    even when the message is long.
    """

    chunks = []

    remaining = text

    while remaining:

        if len(remaining) <= chunk_size:

            chunks.append(
                remaining
            )

            break

        # Prefer splitting at a newline.
        split_at = (
            remaining.rfind(
                "\n",
                0,
                chunk_size,
            )
        )

        if split_at <= 0:

            split_at = (
                chunk_size
            )

        else:

            # Keep the newline in the previous chunk.
            split_at += 1

        chunks.append(
            remaining[:split_at]
        )

        remaining = (
            remaining[split_at:]
        )

    return chunks


def send_notion_alert_comment(
    block_id,
    ticker,
    message,
):
    """
    Create a NEW comment attached directly
    to the ticker's heading/toggle block.

    Examples:

        QQQ alert -> QQQ heading
        SPY alert -> SPY heading
        TSM alert -> TSM heading

    Returns True only if Notion confirms success.
    """

    url = (
        "https://api.notion.com/v1/comments"
    )

    chunks = (
        split_text_for_notion(
            message
        )
    )

    if len(chunks) > 100:

        raise RuntimeError(
            f"{ticker}: alert message is "
            f"too long for one Notion comment."
        )

    rich_text = []

    for chunk in chunks:

        rich_text.append(
            {
                "type": "text",
                "text": {
                    "content": chunk
                },
            }
        )

    payload = {
        "parent": {
            "block_id": block_id
        },
        "rich_text": rich_text,
    }

    print(
        f"{ticker}: sending Notion comment "
        f"to heading block {block_id}..."
    )

    response = requests.post(
        url,
        headers=READER_HEADERS,
        json=payload,
        timeout=30,
    )

    if not response.ok:

        print(
            f"{ticker}: Notion comment failed."
        )

        print(
            f"HTTP {response.status_code}: "
            f"{response.text}"
        )

        return False

    data = response.json()

    comment_id = data.get(
        "id",
        "(unknown)"
    )

    print(
        f"{ticker}: Notion alert comment "
        f"created successfully."
    )

    print(
        f"{ticker}: comment id "
        f"-> {comment_id}"
    )

    return True


# =========================================================
# Legacy state migration
# =========================================================

def legacy_state_matches(
    old_state,
    new_state,
):
    """
    Prevent fake alerts when migrating old state format.

    Old:

        702-724
        ABOVE 724
        BELOW 588

    New:

        RANGE:702-724
        ABOVE:702-724
        BELOW:588-613
    """

    if (
        not old_state
        or not new_state
    ):

        return False

    kind = new_state["kind"]

    if kind == "RANGE":

        return (
            old_state
            == new_state[
                "alert"
            ]["range"]
        )

    if kind == "ABOVE":

        old_format = (
            "ABOVE "
            + format_number(
                new_state[
                    "alert"
                ]["high"]
            )
        )

        return (
            old_state
            == old_format
        )

    if kind == "BELOW":

        old_format = (
            "BELOW "
            + format_number(
                new_state[
                    "alert"
                ]["low"]
            )
        )

        return (
            old_state
            == old_format
        )

    return False


# =========================================================
# Save Last Alert Range
# =========================================================

def update_last_alert_range(
    page_id,
    ticker,
    new_state,
):
    """
    Save internal alert state to Stocks Price.
    """

    rich_text = []

    if new_state:

        rich_text = [
            {
                "type": "text",
                "text": {
                    "content": (
                        new_state
                    )
                },
            }
        ]

    payload = {
        "properties": {
            "Last Alert Range": {
                "rich_text": (
                    rich_text
                )
            }
        }
    }

    url = (
        f"https://api.notion.com/v1/pages/"
        f"{page_id}"
    )

    response = requests.patch(
        url,
        headers=WRITER_HEADERS,
        json=payload,
        timeout=30,
    )

    response.raise_for_status()

    print(
        f"{ticker}: Last Alert Range "
        f"-> {new_state or '(empty)'}"
    )


# =========================================================
# Process alerts
# =========================================================

def process_alert_states(
    ticker_info,
    prices,
    alerts_by_ticker,
    ticker_heading_ids,
):
    """
    Rules:

    First initialization:
        Save state only.
        No notification.

    Same state:
        Do nothing.

    State changed:
        1. Build message
        2. Send Notion comment
        3. ONLY after comment succeeds,
           update Last Alert Range

    If comment fails:
        Do NOT update Last Alert Range.
        Next run will retry.
    """

    for ticker, alerts in (
        alerts_by_ticker.items()
    ):

        info = ticker_info.get(
            ticker
        )

        if not info:

            print(
                f"{ticker}: not found "
                f"in Stocks Price, skipping"
            )

            continue

        price = prices.get(
            ticker
        )

        # -------------------------------------------------
        # Missing / invalid market price
        # -------------------------------------------------

        if (
            price is None
            or price <= 0
        ):

            print(
                f"{ticker}: invalid/stale "
                f"price, alert check skipped"
            )

            continue

        old_state = (
            info[
                "last_alert_range"
            ]
        )

        state = (
            determine_alert_state(
                price,
                alerts,
            )
        )

        if state is None:

            print(
                f"{ticker}: unable to "
                f"determine alert state"
            )

            continue

        new_state = (
            state["key"]
        )

        print(
            f"{ticker}: "
            f"price={price:.2f}, "
            f"old="
            f"{old_state or '(empty)'}, "
            f"new={new_state}"
        )

        # =================================================
        # FIRST TIME
        #
        # Establish current state only.
        # Do not send notification.
        # =================================================

        if not old_state:

            print(
                f"{ticker}: initializing "
                f"alert state without "
                f"notification."
            )

            update_last_alert_range(
                info["page_id"],
                ticker,
                new_state,
            )

            continue

        # =================================================
        # MIGRATE OLD FORMAT
        #
        # Example:
        #
        #   702-724
        #
        # becomes:
        #
        #   RANGE:702-724
        #
        # No fake notification.
        # =================================================

        if legacy_state_matches(
            old_state,
            state,
        ):

            print(
                f"{ticker}: migrating "
                f"old alert state "
                f"without notification."
            )

            update_last_alert_range(
                info["page_id"],
                ticker,
                new_state,
            )

            continue

        # =================================================
        # SAME STATE
        #
        # No repeated notification.
        # =================================================

        if old_state == new_state:

            print(
                f"{ticker}: alert "
                f"state unchanged."
            )

            continue

        # =================================================
        # STATE CHANGED
        # =================================================

        message = (
            build_alert_message(
                ticker=ticker,
                price=price,
                state=state,
                alerts=alerts,
            )
        )

        print("")
        print("=" * 70)
        print("ALERT")
        print("=" * 70)
        print(message)
        print("=" * 70)
        print("")

        # -------------------------------------------------
        # Find ticker's exact Notion heading block.
        # -------------------------------------------------

        heading_block_id = (
            ticker_heading_ids.get(
                ticker
            )
        )

        if not heading_block_id:

            print(
                f"{ticker}: ERROR - "
                f"ticker heading block ID "
                f"not found."
            )

            print(
                f"{ticker}: Last Alert Range "
                f"will NOT be updated."
            )

            print(
                f"{ticker}: next run will retry."
            )

            continue

        # -------------------------------------------------
        # IMPORTANT:
        #
        # Send comment FIRST.
        #
        # Only update Last Alert Range after
        # the comment succeeds.
        # -------------------------------------------------

        comment_success = (
            send_notion_alert_comment(
                block_id=heading_block_id,
                ticker=ticker,
                message=message,
            )
        )

        if not comment_success:

            print(
                f"{ticker}: alert delivery "
                f"failed."
            )

            print(
                f"{ticker}: keeping "
                f"Last Alert Range = "
                f"{old_state}"
            )

            print(
                f"{ticker}: will retry "
                f"on the next run."
            )

            continue

        # -------------------------------------------------
        # Notification succeeded.
        #
        # Now save the new state.
        # -------------------------------------------------

        update_last_alert_range(
            info["page_id"],
            ticker,
            new_state,
        )


# =========================================================
# Main
# =========================================================

def main():

    # -----------------------------------------------------
    # 1. Read Stocks Price
    # -----------------------------------------------------

    data_source_id = (
        find_stock_data_source()
    )

    pages = (
        get_stock_rows(
            data_source_id
        )
    )

    ticker_info = {}

    for page in pages:

        ticker = (
            get_ticker(
                page
            )
        )

        if not ticker:
            continue

        ticker_info[
            ticker
        ] = {
            "page_id": (
                page["id"]
            ),
            "last_alert_range": (
                get_last_alert_range(
                    page
                )
            ),
        }

    if not ticker_info:

        raise RuntimeError(
            "No tickers found "
            "in Stocks Price."
        )

    tickers = list(
        ticker_info.keys()
    )

    # -----------------------------------------------------
    # 2. Get latest prices
    # -----------------------------------------------------

    prices = (
        get_prices(
            tickers
        )
    )

    # -----------------------------------------------------
    # 3. Update Stocks Price
    # -----------------------------------------------------

    for ticker in tickers:

        price = prices.get(
            ticker
        )

        if price is None:

            print(
                f"Skipping {ticker}: "
                f"temporary market "
                f"data failure."
            )

            continue

        update_notion_price(
            ticker_info[
                ticker
            ]["page_id"],
            ticker,
            price,
        )

    # -----------------------------------------------------
    # 4. Find 股票 page
    # -----------------------------------------------------

    notes_page_id = (
        find_stock_notes_page()
    )

    # -----------------------------------------------------
    # 5. Read @alert AND remember ticker heading IDs
    # -----------------------------------------------------

    (
        alerts_by_ticker,
        ticker_heading_ids,
    ) = collect_alerts_from_page(
        notes_page_id,
        set(tickers),
    )

    print("")

    print(
        "Tickers with alerts: "
        + ", ".join(
            sorted(
                alerts_by_ticker.keys()
            )
        )
    )

    print("")

    print(
        "Ticker heading blocks:"
    )

    for ticker in sorted(
        ticker_heading_ids.keys()
    ):

        print(
            f"  {ticker}: "
            f"{ticker_heading_ids[ticker]}"
        )

    print("")

    # -----------------------------------------------------
    # 6. Determine alert states and send notifications
    # -----------------------------------------------------

    process_alert_states(
        ticker_info,
        prices,
        alerts_by_ticker,
        ticker_heading_ids,
    )

    print("Finished.")


if __name__ == "__main__":
    main()
