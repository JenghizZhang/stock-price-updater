import os
import re
import json
import secrets
import time
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

# Optional.
#
# Normally you do NOT need to set these.
# The program first tries to identify the owner of
# Stock Alert Reader automatically.
#
# If your Notion workspace has several users and automatic
# detection does not work, you can set one of these later.
NOTION_NOTIFY_USER_ID = os.environ.get(
    "NOTION_NOTIFY_USER_ID",
    "",
).strip()

NOTION_NOTIFY_USER_NAME = os.environ.get(
    "NOTION_NOTIFY_USER_NAME",
    "",
).strip()


# Stocks Price database:
# Read + Update + Insert
#
# Insert is required because the script can automatically
# create missing ticker rows discovered in 股票.
WRITER_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


# 股票 page:
#
# Read content
# Read comments
# Insert comments
# Read user information without email
READER_HEADERS = {
    "Authorization": f"Bearer {NOTION_READER_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


# =========================================================
# Secondary market-data fallback: TradingView
# =========================================================
#
# Yahoo / yfinance remains the primary source.
# TradingView is contacted ONLY when both Yahoo 1-minute data
# and the existing Yahoo daily fallback fail for a ticker.
#
# Known TradingView-only symbols can be pinned here to avoid
# an extra symbol-search request and to prevent ambiguity.
TRADINGVIEW_SYMBOL_OVERRIDES = {
    "S5TW": "INDEX:S5TW",
}

TRADINGVIEW_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Origin": "https://www.tradingview.com",
    "Referer": "https://www.tradingview.com/",
}


# =========================================================
# Notion: Stocks Price database
# =========================================================

def find_stock_data_source():
    """
    Find Stocks Price data source.
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
    Read Ticker.
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
    Read Last Alert Range.
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
        item.get(
            "plain_text",
            ""
        )
        for item in items
    ).strip()


def get_current_price(page):
    """
    Read the Current Price currently stored in Notion.

    Because this happens BEFORE we update the price,
    this is the previous program run's price.
    """

    prop = (
        page["properties"]
        .get("Current Price")
    )

    if not prop:
        return None

    value = prop.get(
        "number"
    )

    if value is None:
        return None

    try:
        return float(value)

    except (
        TypeError,
        ValueError,
    ):
        return None


# =========================================================
# Stock prices
# =========================================================

def verify_missing_ticker(ticker):
    """
    Returns:

        > 0
            valid ticker / valid price

        0
            invalid ticker

        None
            temporary failure
            preserve previous Notion value
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
            f"{ticker}: verification succeeded "
            f"-> ${price:.2f}"
        )

        return price

    except Exception as exc:

        print(
            f"{ticker}: verification failed "
            f"({exc}) -> preserve previous price"
        )

        return None




def resolve_tradingview_symbol(ticker):
    """
    Resolve a plain ticker to TradingView's EXCHANGE:SYMBOL form.

    Known overrides are used first.  For other Yahoo failures,
    TradingView's symbol-search endpoint is queried and only an
    EXACT symbol match is accepted.

    Returns:
        e.g. "INDEX:S5TW"

    or:
        None
    """

    ticker = ticker.strip().upper()

    override = TRADINGVIEW_SYMBOL_OVERRIDES.get(ticker)

    if override:
        print(
            f"{ticker}: TradingView override "
            f"-> {override}"
        )
        return override

    url = (
        "https://symbol-search.tradingview.com/"
        "symbol_search/v3/"
    )

    params = {
        "text": ticker,
        "hl": 1,
        "lang": "en",
        "search_type": "undefined",
        "domain": "production",
        "sort_by_country": "US",
    }

    try:

        response = requests.get(
            url,
            headers=TRADINGVIEW_HEADERS,
            params=params,
            timeout=15,
        )

        response.raise_for_status()

        payload = response.json()

        candidates = payload.get(
            "symbols",
            [],
        )

        exact_matches = []

        for item in candidates:

            symbol = str(
                item.get(
                    "symbol",
                    "",
                )
            ).strip().upper()

            exchange = str(
                item.get(
                    "exchange",
                    "",
                )
            ).strip().upper()

            if (
                symbol != ticker
                or not exchange
            ):
                continue

            score = 0

            if str(
                item.get(
                    "country",
                    "",
                )
            ).upper() == "US":
                score += 100

            if item.get(
                "is_primary_listing"
            ):
                score += 50

            if exchange in {
                "INDEX",
                "NASDAQ",
                "NYSE",
                "AMEX",
                "ARCA",
                "BATS",
                "CBOE",
                "OTC",
            }:
                score += 25

            exact_matches.append(
                (
                    score,
                    f"{exchange}:{symbol}",
                )
            )

        if not exact_matches:
            print(
                f"{ticker}: TradingView symbol search "
                f"found no exact match."
            )
            return None

        exact_matches.sort(
            key=lambda item: item[0],
            reverse=True,
        )

        resolved = exact_matches[0][1]

        print(
            f"{ticker}: TradingView symbol resolved "
            f"-> {resolved}"
        )

        return resolved

    except Exception as exc:

        print(
            f"{ticker}: TradingView symbol search failed "
            f"({exc})"
        )

        return None


def _tradingview_message(method, params):
    """
    Build one TradingView websocket protocol frame.
    """

    payload = json.dumps(
        {
            "m": method,
            "p": params,
        },
        separators=(",", ":"),
    )

    length = len(
        payload.encode("utf-8")
    )

    return (
        f"~m~{length}~m~{payload}"
    )


def _tradingview_raw_frame(payload):
    """
    Wrap an already-serialized TradingView payload.

    This is mainly used to echo TradingView heartbeat messages.
    """

    length = len(
        payload.encode("utf-8")
    )

    return (
        f"~m~{length}~m~{payload}"
    )


def _split_tradingview_frames(message):
    """
    Split a websocket message that may contain several TradingView
    ~m~<length>~m~ frames.

    TradingView payloads themselves do not use this delimiter, so a
    regex split is enough for the quote / chart messages we consume.
    """

    if isinstance(message, bytes):
        message = message.decode(
            "utf-8",
            errors="replace",
        )

    parts = re.split(
        r"~m~\d+~m~",
        message,
    )

    return [
        part
        for part in parts
        if part
    ]


def _extract_tradingview_price_from_payload(payload, full_symbol):
    """
    Extract the latest usable price from one decoded TradingView
    websocket JSON payload.

    We accept either:
      - qsd quote updates (lp = last price)
      - timescale_update / du chart bars (close = v[4])
    """

    method = payload.get("m")
    params = payload.get("p", [])

    # -----------------------------------------------------
    # Quote-session update
    # -----------------------------------------------------
    if method == "qsd":

        if len(params) < 2:
            return None

        quote = params[1]

        if not isinstance(quote, dict):
            return None

        symbol = str(
            quote.get("n", "")
        ).upper()

        if (
            symbol
            and symbol != full_symbol.upper()
        ):
            return None

        values = quote.get(
            "v",
            {},
        )

        if not isinstance(values, dict):
            return None

        raw_price = values.get("lp")

        if raw_price is None:
            raw_price = values.get(
                "rtc"
            )

        try:
            price = float(raw_price)
        except (
            TypeError,
            ValueError,
        ):
            return None

        if price > 0:
            return price

        return None

    # -----------------------------------------------------
    # Chart-session OHLC update
    #
    # TradingView bar vector:
    #   [timestamp, open, high, low, close, volume]
    # -----------------------------------------------------
    if method in {
        "timescale_update",
        "du",
    }:

        if len(params) < 2:
            return None

        series_container = params[1]

        if not isinstance(
            series_container,
            dict,
        ):
            return None

        latest_timestamp = None
        latest_close = None

        for series in (
            series_container.values()
        ):

            if not isinstance(
                series,
                dict,
            ):
                continue

            bars = series.get(
                "s",
                [],
            )

            if not isinstance(
                bars,
                list,
            ):
                continue

            for bar in bars:

                if not isinstance(
                    bar,
                    dict,
                ):
                    continue

                values = bar.get(
                    "v",
                    [],
                )

                if (
                    not isinstance(
                        values,
                        list,
                    )
                    or len(values) < 5
                ):
                    continue

                try:
                    timestamp = float(
                        values[0]
                    )
                    close = float(
                        values[4]
                    )
                except (
                    TypeError,
                    ValueError,
                ):
                    continue

                if close <= 0:
                    continue

                if (
                    latest_timestamp is None
                    or timestamp
                    >= latest_timestamp
                ):
                    latest_timestamp = timestamp
                    latest_close = close

        return latest_close

    return None


def get_tradingview_websocket_price(
    ticker,
    full_symbol,
):
    """
    Fetch the latest available TradingView value through the same
    websocket chart feed used by TradingView charts.

    Why this exists:
        Some chart-only / breadth symbols (for example INDEX:S5TW)
        are visible on TradingView charts but are NOT returned by the
        scanner HTTP endpoint.

    Authentication:
        Anonymous / unauthorized TradingView access is used.
        TradingView may return delayed data for anonymous sessions.

    Returns:
        > 0     usable latest value
        None    websocket could not provide a value
    """

    try:
        from websockets.sync.client import (
            connect,
        )
    except Exception as exc:

        print(
            f"{ticker}: TradingView websocket "
            f"library unavailable ({exc})."
        )

        return None

    quote_session = (
        "qs_"
        + secrets.token_hex(6)
    )

    chart_session = (
        "cs_"
        + secrets.token_hex(6)
    )

    symbol_alias = "symbol_1"
    series_id = "s1"

    symbol_spec = (
        "="
        + json.dumps(
            {
                "symbol": full_symbol,
                "adjustment": "splits",
            },
            separators=(",", ":"),
        )
    )

    uri = (
        "wss://data.tradingview.com/"
        "socket.io/websocket"
    )

    # Different TradingView deployments have accepted either of
    # these browser-like origins.  Try both before giving up.
    origins = [
        "https://data.tradingview.com",
        "https://www.tradingview.com",
    ]

    last_error = None

    for origin in origins:

        try:

            with connect(
                uri,
                origin=origin,
                user_agent_header=(
                    TRADINGVIEW_HEADERS[
                        "User-Agent"
                    ]
                ),
                open_timeout=10,
                close_timeout=3,
                ping_interval=None,
                max_size=2 * 1024 * 1024,
            ) as websocket:

                websocket.send(
                    _tradingview_message(
                        "set_auth_token",
                        [
                            "unauthorized_user_token"
                        ],
                    )
                )

                websocket.send(
                    _tradingview_message(
                        "quote_create_session",
                        [quote_session],
                    )
                )

                websocket.send(
                    _tradingview_message(
                        "quote_set_fields",
                        [
                            quote_session,
                            "lp",
                            "lp_time",
                            "ch",
                            "chp",
                            "description",
                            "exchange",
                            "type",
                            "update_mode",
                            "rtc",
                        ],
                    )
                )

                websocket.send(
                    _tradingview_message(
                        "quote_add_symbols",
                        [
                            quote_session,
                            full_symbol,
                        ],
                    )
                )

                websocket.send(
                    _tradingview_message(
                        "quote_fast_symbols",
                        [
                            quote_session,
                            full_symbol,
                        ],
                    )
                )

                # Also request a chart series.  This is important for
                # chart-only breadth indicators such as INDEX:S5TW,
                # where the quote/scanner layer may not expose lp.
                websocket.send(
                    _tradingview_message(
                        "chart_create_session",
                        [
                            chart_session,
                            "",
                        ],
                    )
                )

                websocket.send(
                    _tradingview_message(
                        "resolve_symbol",
                        [
                            chart_session,
                            symbol_alias,
                            symbol_spec,
                        ],
                    )
                )

                # 1D is intentionally requested instead of 1-minute.
                # The latest daily bar's close is the latest available
                # value and is supported by more index/breadth symbols.
                websocket.send(
                    _tradingview_message(
                        "create_series",
                        [
                            chart_session,
                            series_id,
                            series_id,
                            symbol_alias,
                            "1D",
                            5,
                        ],
                    )
                )

                deadline = (
                    time.monotonic()
                    + 10
                )

                while (
                    time.monotonic()
                    < deadline
                ):

                    remaining = (
                        deadline
                        - time.monotonic()
                    )

                    try:
                        message = websocket.recv(
                            timeout=min(
                                2,
                                max(
                                    0.1,
                                    remaining,
                                ),
                            )
                        )
                    except TimeoutError:
                        continue

                    for raw_payload in (
                        _split_tradingview_frames(
                            message
                        )
                    ):

                        # TradingView application heartbeat.
                        if raw_payload.startswith(
                            "~h~"
                        ):

                            websocket.send(
                                _tradingview_raw_frame(
                                    raw_payload
                                )
                            )

                            continue

                        try:
                            payload = json.loads(
                                raw_payload
                            )
                        except (
                            TypeError,
                            json.JSONDecodeError,
                        ):
                            continue

                        price = (
                            _extract_tradingview_price_from_payload(
                                payload,
                                full_symbol,
                            )
                        )

                        if (
                            price is not None
                            and price > 0
                        ):

                            print(
                                f"{ticker}: TradingView websocket "
                                f"{full_symbol} -> ${price:.2f}"
                            )

                            return price

                        method = payload.get(
                            "m"
                        )

                        if method in {
                            "symbol_error",
                            "series_error",
                            "critical_error",
                        }:

                            print(
                                f"{ticker}: TradingView websocket "
                                f"reported {method}."
                            )

                print(
                    f"{ticker}: TradingView websocket "
                    f"returned no usable value for "
                    f"{full_symbol}."
                )

        except Exception as exc:

            last_error = exc

            print(
                f"{ticker}: TradingView websocket "
                f"connection failed with origin "
                f"{origin} ({exc})."
            )

    if last_error is not None:

        print(
            f"{ticker}: TradingView websocket "
            f"fallback exhausted ({last_error})."
        )

    return None


def get_tradingview_scanner_price(
    ticker,
    full_symbol,
):
    """
    First TradingView method: HTTP scanner.

    This is quick for symbols exposed through TradingView's scanner,
    but some chart-only indexes (including S5TW in current testing)
    return no row.  In that case the websocket chart fallback is used.
    """

    url = (
        "https://scanner.tradingview.com/"
        "global/scan"
    )

    columns = [
        "name",
        "description",
        "close",
        "update_mode",
    ]

    payload = {
        "symbols": {
            "tickers": [
                full_symbol
            ],
            "query": {
                "types": []
            },
        },
        "columns": columns,
        "range": [0, 1],
    }

    scanner_headers = {
        "User-Agent": (
            TRADINGVIEW_HEADERS[
                "User-Agent"
            ]
        ),
        "Content-Type": (
            "application/json"
        ),
        "Origin": (
            "https://www.tradingview.com"
        ),
        "Referer": (
            "https://www.tradingview.com/"
        ),
    }

    try:

        response = requests.post(
            url,
            headers=scanner_headers,
            json=payload,
            timeout=15,
        )

        response.raise_for_status()

        data = response.json()

        rows = data.get(
            "data",
            [],
        )

        if not rows:
            print(
                f"{ticker}: TradingView scanner returned "
                f"no quote for {full_symbol}."
            )
            return None

        values = rows[0].get(
            "d",
            [],
        )

        row = dict(
            zip(
                columns,
                values,
            )
        )

        raw_price = row.get(
            "close"
        )

        if raw_price is None:
            print(
                f"{ticker}: TradingView scanner quote "
                f"has no close value."
            )
            return None

        price = float(
            raw_price
        )

        if price <= 0:
            print(
                f"{ticker}: TradingView scanner returned "
                f"invalid price {price}."
            )
            return None

        print(
            f"{ticker}: TradingView scanner "
            f"{full_symbol} -> ${price:.2f}"
        )

        return price

    except Exception as exc:

        print(
            f"{ticker}: TradingView scanner failed "
            f"({exc})"
        )

        return None


def get_tradingview_fallback_price(ticker):
    """
    Get the latest available TradingView value for a ticker.

    Order inside TradingView:
        1. HTTP scanner (fast)
        2. Websocket quote/chart feed (handles chart-only symbols)

    This remains the SECOND PROVIDER after Yahoo.  It is called only
    after Yahoo 1-minute and Yahoo daily fallback both fail.

    Daily High / Daily Low are intentionally NOT populated from this
    fallback.  Those fields keep the yfinance 1-minute definition and
    remain unchanged when Yahoo intraday data is absent.
    """

    full_symbol = resolve_tradingview_symbol(
        ticker
    )

    if not full_symbol:
        return None

    price = (
        get_tradingview_scanner_price(
            ticker,
            full_symbol,
        )
    )

    if (
        price is not None
        and price > 0
    ):
        return price

    print(
        f"{ticker}: TradingView scanner had no usable "
        f"price; trying websocket chart feed..."
    )

    return get_tradingview_websocket_price(
        ticker,
        full_symbol,
    )


def get_secondary_fallback_price(ticker):
    """
    Secondary provider chain.

    Currently:
        TradingView

    More non-Yahoo providers can be added here later without
    changing the main price-download logic.
    """

    print(
        f"{ticker}: trying secondary data source "
        f"(TradingView)..."
    )

    return get_tradingview_fallback_price(
        ticker
    )

def get_intraday_daily_range(
    ticker,
    ticker_data,
):
    """
    Calculate today's intraday high / low from the SAME 1-minute
    yfinance data that is already downloaded in get_prices().

    The trading day is determined using America/New_York.

    Because yf.download(..., prepost=True) is already used,
    today's range naturally includes:
        pre-market
        regular session
        after-hours

    Returns:
        {
            "high": float,
            "low": float,
            "date": "YYYY-MM-DD",
        }

    or:
        None

    No extra Yahoo request is made here.
    """

    try:

        if (
            ticker_data is None
            or ticker_data.empty
        ):
            return None

        index = ticker_data.index

        if not hasattr(
            index,
            "tz"
        ):
            return None

        # Intraday yfinance indexes are normally timezone-aware.
        #
        # If a future yfinance version returns a naive index,
        # interpret it as UTC rather than guessing local machine time.
        if index.tz is None:

            eastern_index = (
                index
                .tz_localize("UTC")
                .tz_convert(
                    "America/New_York"
                )
            )

        else:

            eastern_index = (
                index
                .tz_convert(
                    "America/New_York"
                )
            )

        today_eastern = (
            datetime.now(
                ZoneInfo(
                    "America/New_York"
                )
            ).date()
        )

        today_mask = [
            timestamp.date()
            == today_eastern
            for timestamp
            in eastern_index
        ]

        today_data = (
            ticker_data.loc[
                today_mask
            ]
        )

        if today_data.empty:

            print(
                f"{ticker}: no 1-minute bars "
                f"for {today_eastern}; "
                f"Daily High/Low unchanged."
            )

            return None

        highs = (
            today_data["High"]
            .dropna()
        )

        lows = (
            today_data["Low"]
            .dropna()
        )

        if (
            highs.empty
            or lows.empty
        ):

            print(
                f"{ticker}: today's 1-minute "
                f"High/Low data is incomplete; "
                f"Daily High/Low unchanged."
            )

            return None

        daily_high = float(
            highs.max()
        )

        daily_low = float(
            lows.min()
        )

        if (
            daily_high <= 0
            or daily_low <= 0
            or daily_high < daily_low
        ):

            print(
                f"{ticker}: invalid daily range "
                f"from 1-minute data; "
                f"Daily High/Low unchanged."
            )

            return None

        result = {
            "high": daily_high,
            "low": daily_low,
            "date": (
                today_eastern.isoformat()
            ),
        }

        print(
            f"{ticker}: daily range "
            f"{result['date']} "
            f"high=${daily_high:.2f}, "
            f"low=${daily_low:.2f}"
        )

        return result

    except Exception as exc:

        print(
            f"{ticker}: could not calculate "
            f"Daily High/Low ({exc}); "
            f"keeping existing values."
        )

        return None


def get_prices(tickers):
    """
    Download latest market prices with a layered fallback chain.

    Price priority:

        1. Yahoo / yfinance 1-minute batch data
        2. Yahoo / yfinance existing 1-day fallback
        3. TradingView secondary fallback
        4. Everything failed -> 0

    Returns:
        prices
        daily_ranges

    Daily High / Daily Low are calculated ONLY from valid Yahoo
    1-minute data.  Yahoo daily fallback and TradingView fallback
    never fabricate or overwrite Daily High / Daily Low.
    """

    print(
        "Downloading prices for: "
        + ", ".join(tickers)
    )

    prices = {
        ticker: None
        for ticker in tickers
    }

    daily_ranges = {
        ticker: None
        for ticker in tickers
    }

    data = None

    # =====================================================
    # 1. Yahoo 1-minute batch
    # =====================================================

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
            f"Yahoo 1-minute batch request failed: {exc}"
        )

        print(
            "Continuing with per-ticker Yahoo daily "
            "fallback, then TradingView fallback."
        )

    for ticker in tickers:

        ticker_data = None

        if data is not None:

            try:

                ticker_data = (
                    data[ticker]
                )

            except Exception:
                ticker_data = None

        # -------------------------------------------------
        # Daily High / Daily Low
        #
        # Reuse Yahoo's same 1-minute batch response.
        # No second Yahoo intraday request.
        # -------------------------------------------------

        if ticker_data is not None:

            daily_ranges[ticker] = (
                get_intraday_daily_range(
                    ticker,
                    ticker_data,
                )
            )

        # -------------------------------------------------
        # Current Price from Yahoo 1-minute data
        # -------------------------------------------------

        try:

            close = (
                ticker_data["Close"]
                .dropna()
            )

            if not close.empty:

                price = float(
                    close.iloc[-1]
                )

                if price > 0:

                    prices[ticker] = price

                    print(
                        f"{ticker}: Yahoo 1m "
                        f"-> ${price:.2f}"
                    )

                    continue

        except Exception:
            pass

        # =================================================
        # 2. Existing Yahoo daily fallback
        # =================================================

        print(
            f"{ticker}: Yahoo 1m price missing; "
            f"trying Yahoo daily fallback..."
        )

        yahoo_daily_price = (
            verify_missing_ticker(
                ticker
            )
        )

        if (
            yahoo_daily_price is not None
            and yahoo_daily_price > 0
        ):

            prices[ticker] = (
                yahoo_daily_price
            )

            print(
                f"{ticker}: using Yahoo daily "
                f"fallback -> "
                f"${yahoo_daily_price:.2f}"
            )

            continue

        # =================================================
        # 3. Secondary provider fallback: TradingView
        # =================================================

        secondary_price = (
            get_secondary_fallback_price(
                ticker
            )
        )

        if (
            secondary_price is not None
            and secondary_price > 0
        ):

            prices[ticker] = (
                secondary_price
            )

            continue

        # =================================================
        # 4. Everything failed -> 0
        # =================================================

        prices[ticker] = 0.0

        print(
            f"{ticker}: all price sources failed "
            f"-> $0"
        )

    return (
        prices,
        daily_ranges,
    )


def update_notion_price(
    page_id,
    ticker,
    price,
    daily_range=None,
):
    """
    Update Stocks Price in ONE Notion PATCH.

    Always updates:
        Current Price
        Last Updated

    When today's 1-minute range is available, the SAME PATCH
    also updates:
        Daily High
        Daily Low
        Daily Range Date

    This keeps the Notion API request count the same as before.
    """

    now = datetime.now(
        ZoneInfo(
            "America/Los_Angeles"
        )
    ).isoformat()

    properties = {
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

    if (
        daily_range
        and price > 0
    ):

        properties[
            "Daily High"
        ] = {
            "number": round(
                daily_range["high"],
                2,
            )
        }

        properties[
            "Daily Low"
        ] = {
            "number": round(
                daily_range["low"],
                2,
            )
        }

        properties[
            "Daily Range Date"
        ] = {
            "date": {
                "start": (
                    daily_range["date"]
                )
            }
        }

    payload = {
        "properties": properties
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
            f"NO PRICE DATA -> $0"
        )

    else:

        print(
            f"Updated price {ticker}: "
            f"${price:.2f}"
        )

        if daily_range:

            print(
                f"Updated daily range {ticker}: "
                f"high=${daily_range['high']:.2f}, "
                f"low=${daily_range['low']:.2f}, "
                f"date={daily_range['date']}"
            )


# =========================================================
# Notion: 股票 page
# =========================================================

def extract_page_title(page):
    """
    Extract page title.
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
    Find 股票 page.
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
    Extract plain text from a block.
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
    Read all child blocks.
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
# Notion user / @mention
# =========================================================

def get_connection_owner_user_id():
    """
    Try to get the human user who owns this
    Notion connection.

    For an internal integration, the bot object may
    contain its owner.

    Returns:
        user_id
        or None
    """

    url = (
        "https://api.notion.com/v1/users/me"
    )

    try:

        response = requests.get(
            url,
            headers=READER_HEADERS,
            timeout=30,
        )

        if not response.ok:

            print(
                "Could not retrieve connection owner: "
                f"HTTP {response.status_code}"
            )

            return None

        data = response.json()

        # Some authentication types may directly
        # represent a person.
        if data.get("type") == "person":

            user_id = data.get(
                "id"
            )

            if user_id:

                print(
                    "Notification user detected "
                    "from token user."
                )

                return user_id

        # Internal integration bot.
        if data.get("type") == "bot":

            bot = data.get(
                "bot",
                {},
            )

            owner = bot.get(
                "owner",
                {},
            )

            if owner.get("type") == "user":

                user = owner.get(
                    "user",
                    {},
                )

                user_id = user.get(
                    "id"
                )

                if user_id:

                    print(
                        "Notification user detected "
                        "from connection owner."
                    )

                    return user_id

    except Exception as exc:

        print(
            "Connection owner detection failed: "
            f"{exc}"
        )

    return None


def get_notion_users():
    """
    List human users in the workspace.

    Requires:
        Read user information
        without email addresses
    """

    url = (
        "https://api.notion.com/v1/users"
    )

    users = []

    cursor = None

    while True:

        params = {
            "page_size": 100
        }

        if cursor:

            params[
                "start_cursor"
            ] = cursor

        response = requests.get(
            url,
            headers=READER_HEADERS,
            params=params,
            timeout=30,
        )

        if not response.ok:

            print(
                "Unable to list Notion users."
            )

            print(
                f"HTTP {response.status_code}: "
                f"{response.text}"
            )

            return []

        data = response.json()

        for user in data.get(
            "results",
            [],
        ):

            if user.get("type") == "person":

                users.append(
                    user
                )

        if not data.get(
            "has_more"
        ):
            break

        cursor = data.get(
            "next_cursor"
        )

    return users


def resolve_notification_user_id():
    """
    Determine which Notion user should be @mentioned.

    Priority:

    1. NOTION_NOTIFY_USER_ID environment variable

    2. Connection owner

    3. NOTION_NOTIFY_USER_NAME environment variable

    4. If there is exactly one human user in the
       workspace, use that user automatically.

    Returns:
        user_id

    or:
        None
    """

    # -----------------------------------------------------
    # Explicit user ID
    # -----------------------------------------------------

    if NOTION_NOTIFY_USER_ID:

        print(
            "Using NOTION_NOTIFY_USER_ID."
        )

        return (
            NOTION_NOTIFY_USER_ID
        )

    # -----------------------------------------------------
    # Connection owner
    # -----------------------------------------------------

    owner_user_id = (
        get_connection_owner_user_id()
    )

    if owner_user_id:

        return owner_user_id

    # -----------------------------------------------------
    # List human users
    # -----------------------------------------------------

    users = (
        get_notion_users()
    )

    if not users:

        print(
            "ERROR: No Notion person users "
            "could be found."
        )

        return None

    # -----------------------------------------------------
    # Explicit name
    # -----------------------------------------------------

    if NOTION_NOTIFY_USER_NAME:

        wanted_name = (
            NOTION_NOTIFY_USER_NAME
            .casefold()
        )

        matches = [
            user
            for user in users
            if (
                user.get(
                    "name",
                    ""
                )
                .strip()
                .casefold()
                == wanted_name
            )
        ]

        if len(matches) == 1:

            print(
                "Notification user found: "
                f"{matches[0].get('name')}"
            )

            return matches[0]["id"]

        if len(matches) > 1:

            print(
                "ERROR: More than one Notion "
                "user has this name."
            )

            return None

        print(
            "ERROR: NOTION_NOTIFY_USER_NAME "
            f"'{NOTION_NOTIFY_USER_NAME}' "
            "was not found."
        )

    # -----------------------------------------------------
    # Exactly one human user
    # -----------------------------------------------------

    if len(users) == 1:

        user = users[0]

        print(
            "Only one human Notion user found."
        )

        print(
            "Notification user: "
            f"{user.get('name', '(unknown)')}"
        )

        return user["id"]

    # -----------------------------------------------------
    # Ambiguous
    # -----------------------------------------------------

    print("")
    print(
        "Could not automatically determine "
        "which Notion user to @mention."
    )

    print(
        "Available Notion users:"
    )

    for user in users:

        print(
            f"  - "
            f"{user.get('name', '(unknown)')} "
            f"| {user.get('id')}"
        )

    print("")
    print(
        "Set NOTION_NOTIFY_USER_NAME "
        "or NOTION_NOTIFY_USER_ID."
    )

    return None


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


def format_range(
    low,
    high,
):

    return (
        f"{format_number(low)}"
        f"-"
        f"{format_number(high)}"
    )


def parse_alert_line(text):
    """
    Example:

        @alert 704-724（强）破位走弱 激进

    becomes:

        low = 704
        high = 724

        display_text =
        704-724（强）破位走弱 激进
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

# A ticker section must START with a ticker-like symbol.
#
# Examples that ARE ticker sections:
#
#     QQQ
#     NVDA（*）
#     XLK 科技行业精选指数ETF-SPDR（参考）
#     JPM 小摩
#     GS 高盛
#     BRK-B
#     ^GSPC标普500（参考）
#
# Examples that are NOT ticker sections:
#
#     ————常用指数————
#     ————半导体个股————
#     ————科技个股————
#     ————参考指数————
#     ————其他个股————
#     高估股票
#     正价股票
#     低估股票
#
# This is intentionally prefix-based.  A ticker appearing later
# in a Chinese heading does NOT turn that heading into a stock.
HEADING_TICKER_PATTERN = re.compile(
    r"^\s*[^\w^]*"
    r"(\^[A-Z][A-Z0-9.\-]{0,9}|[A-Z][A-Z0-9.\-]{0,9})"
    r"(?=$|[\s（(＊*：:【\[]|[\u3400-\u4DBF\u4E00-\u9FFF])",
    re.IGNORECASE,
)


CHINESE_CHARACTER_PATTERN = re.compile(
    r"[\u3400-\u4DBF\u4E00-\u9FFF]"
)


def extract_ticker_candidate_from_heading(text):
    """
    Extract a ticker-like code only from the START of a heading.

    Returns for example:

        "NVDA（*）"                 -> "NVDA"
        "JPM 小摩"                 -> "JPM"
        "BRK-B"                    -> "BRK-B"
        "^GSPC标普500（参考）"      -> "^GSPC"
        "————半导体个股————"       -> None
        "高估股票"                  -> None
    """

    if not text:
        return None

    match = HEADING_TICKER_PATTERN.search(
        text
    )

    if not match:
        return None

    return (
        match.group(1)
        .strip()
        .upper()
    )


def is_chinese_section_heading(text):
    """
    Return True when a top-level heading is a Chinese/category
    separator rather than a ticker section.

    Decorative punctuation before the Chinese text is ignored,
    so this also catches headings such as:

        ————半导体个股————
        ————科技个股————
        ————常用指数————

    A real ticker prefix wins first.  Therefore this does NOT
    treat the following as a Chinese category heading:

        ^GSPC标普500（参考）
        XLK 科技行业精选指数ETF-SPDR（参考）
        JPM 小摩
    """

    if not text:
        return False

    if extract_ticker_candidate_from_heading(
        text
    ):
        return False

    stripped = text.strip()

    # Remove only decorative separators from the beginning.
    # Do NOT remove letters, numbers or ^.
    stripped = re.sub(
        r"^[\s\-—–_=·•◆◇★☆━─]+",
        "",
        stripped,
    )

    if not stripped:
        return False

    return bool(
        CHINESE_CHARACTER_PATTERN.match(
            stripped[0]
        )
    )


def ticker_matches_heading(
    text,
    valid_tickers,
):
    """
    Match a top-level heading to a ticker that already exists in
    Stocks Price.  Matching is prefix-based, not substring-based.
    """

    candidate = (
        extract_ticker_candidate_from_heading(
            text
        )
    )

    if not candidate:
        return None

    if candidate in valid_tickers:
        return candidate

    return None


def discover_ticker_headings(page_id):
    """
    Discover ticker-like top-level headings from 股票, even when
    the ticker does not yet exist in Stocks Price.

    This is used BEFORE price download so missing ticker rows can
    be created automatically.

    Chinese/category headings are ignored.
    """

    heading_types = {
        "heading_1",
        "heading_2",
        "heading_3",
        "toggle",
    }

    top_blocks = (
        get_block_children(
            page_id
        )
    )

    discovered = []
    seen = set()

    for block in top_blocks:

        block_type = block.get(
            "type"
        )

        if block_type not in heading_types:
            continue

        text = (
            get_block_text(
                block
            )
        )

        if not text:
            continue

        candidate = (
            extract_ticker_candidate_from_heading(
                text
            )
        )

        if not candidate:
            continue

        if candidate in seen:
            continue

        seen.add(
            candidate
        )

        discovered.append(
            candidate
        )

        print(
            f"Discovered ticker heading: "
            f"{text} -> {candidate}"
        )

    return discovered


def create_stock_row(
    data_source_id,
    ticker,
):
    """
    Create a new row in Stocks Price for a ticker-like heading
    discovered in the 股票 page but missing from the database.

    The row is created EVEN IF Yahoo Finance cannot provide a
    price for that symbol.  This keeps Stocks Price complete and
    makes unsupported / mistyped symbols visible to the user.

    A newly-created row starts with Current Price = 0.  If market
    data is available later in the same run, get_prices() will
    overwrite 0 with the real price.  If the symbol is unsupported,
    it remains 0 and can be spotted easily in Stocks Price.

    IMPORTANT:

    The Stock Price Updater Notion connection needs:

        Read content
        Update content
        Insert content

    Without Insert content, Notion returns HTTP 403.

    Returns the newly created page object, or None when creation
    could not be completed.
    """

    url = (
        "https://api.notion.com/v1/pages"
    )

    payload = {
        "parent": {
            "type": "data_source_id",
            "data_source_id": (
                data_source_id
            ),
        },
        "properties": {
            "Ticker": {
                "title": [
                    {
                        "type": "text",
                        "text": {
                            "content": ticker
                        },
                    }
                ]
            },
            "Current Price": {
                "number": 0
            },
        },
    }

    response = requests.post(
        url,
        headers=WRITER_HEADERS,
        json=payload,
        timeout=30,
    )

    if response.status_code == 403:

        print(
            f"{ticker}: cannot create Stocks Price row. "
            f"Enable Insert content for the "
            f"Stock Price Updater Notion connection."
        )

        return None

    if not response.ok:

        print(
            f"{ticker}: failed to create Stocks Price row."
        )

        print(
            f"HTTP {response.status_code}: "
            f"{response.text}"
        )

        return None

    page = response.json()

    print(
        f"Added missing ticker to Stocks Price: "
        f"{ticker}"
    )

    return page


def add_missing_note_tickers_to_stock_price(
    data_source_id,
    notes_page_id,
    ticker_info,
):
    """
    Find ticker-like headings in 股票 that are missing from
    Stocks Price and create rows for ALL of them.

    IMPORTANT:

    We intentionally do NOT validate the symbol with Yahoo Finance
    before creating the row.

    Why:
        - TradingView-only symbols such as S5TW may not exist in
          Yahoo Finance.
        - A mistyped / unsupported ticker should still appear in
          Stocks Price so the user can see it and fix it.
        - Unsupported symbols will normally end up with
          Current Price = 0.

    Chinese/category headings are still ignored by
    discover_ticker_headings(), so headings such as 高估股票 or
    ————半导体个股———— are not added as tickers.
    """

    discovered = (
        discover_ticker_headings(
            notes_page_id
        )
    )

    missing = [
        ticker
        for ticker in discovered
        if ticker not in ticker_info
    ]

    if not missing:

        print(
            "No ticker headings are missing "
            "from Stocks Price."
        )

        return

    print(
        "Ticker headings missing from Stocks Price: "
        + ", ".join(missing)
    )

    for ticker in missing:

        print(
            f"Adding missing ticker {ticker} "
            f"to Stocks Price without Yahoo validation..."
        )

        page = (
            create_stock_row(
                data_source_id,
                ticker,
            )
        )

        if not page:
            continue

        ticker_info[ticker] = {
            "page_id": page["id"],
            "last_alert_range": "",
            "previous_price": 0.0,
        }


# =========================================================
# Collect @alert + ticker heading IDs
# =========================================================

def collect_alerts_from_page(
    page_id,
    valid_tickers,
):
    """
    Parse 股票 page by ticker section.

    @alert may be:

        separate paragraphs
        multiple lines in one paragraph
        inside toggles
        mixed with normal notes
        separated by images / embeds
        deeply nested

    Everything except @alert is ignored.

    Returns:

        alerts_by_ticker
        ticker_heading_ids
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
    # Scan one block recursively
    # -----------------------------------------------------

    def extract_alerts_from_block(
        block,
        ticker,
    ):

        text = (
            get_block_text(
                block
            )
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
    # Top-level blocks
    # -----------------------------------------------------

    top_blocks = (
        get_block_children(
            page_id
        )
    )

    current_ticker = None

    for block in top_blocks:

        text = (
            get_block_text(
                block
            )
        )

        block_type = (
            block.get(
                "type"
            )
        )

        detected = None
        candidate = None

        if (
            block_type
            in heading_types
            and text
        ):

            candidate = (
                extract_ticker_candidate_from_heading(
                    text
                )
            )

            detected = (
                ticker_matches_heading(
                    text,
                    valid_tickers,
                )
            )

        # -------------------------------------------------
        # New ticker section
        # -------------------------------------------------

        if detected:

            current_ticker = detected

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
        # Chinese/category section boundary
        # -------------------------------------------------
        #
        # Examples:
        #
        #     ————半导体个股————
        #     ————科技个股————
        #     ————参考指数————
        #     ————其他个股————
        #     高估股票
        #     正价股票
        #     低估股票
        #
        # Once one of these top-level headings is reached,
        # alerts below it must NOT leak into the previous stock.
        # -------------------------------------------------

        if (
            block_type
            in heading_types
            and text
            and is_chinese_section_heading(
                text
            )
        ):

            print(
                f"Section boundary (not a ticker): "
                f"{text}"
            )

            current_ticker = None
            continue

        # -------------------------------------------------
        # Ticker-looking heading that is still unavailable
        # -------------------------------------------------
        #
        # Normally auto-add runs before this parser.  If a
        # candidate could not be added/validated, stop the
        # previous ticker here so its @alert lines cannot be
        # assigned to the wrong stock.
        # -------------------------------------------------

        if (
            block_type
            in heading_types
            and candidate
            and not detected
        ):

            print(
                f"Ticker-like heading is not active: "
                f"{text} -> {candidate}"
            )

            current_ticker = None
            continue

        # -------------------------------------------------
        # Ordinary blocks after ticker heading
        # -------------------------------------------------

        if current_ticker:

            extract_alerts_from_block(
                block,
                current_ticker,
            )

    # -----------------------------------------------------
    # Deduplicate numeric ranges
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
    Possible states:

        RANGE:704-724

        GAP:687-697|704-724

        ABOVE:704-724

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
# Price movement
# =========================================================

def get_price_movement(
    previous_price,
    current_price,
):
    """
    Determine UP / DOWN based on previous program run.

    Example:

        703.82 -> 709.55
        = UP

        725.30 -> 719.80
        = DOWN
    """

    if (
        previous_price is None
        or previous_price <= 0
    ):

        return {
            "direction": None,
            "icon": "🔔",
            "previous_price": None,
        }

    previous = round(
        float(previous_price),
        2,
    )

    current = round(
        float(current_price),
        2,
    )

    if current > previous:

        return {
            "direction": "UP",
            "icon": "🔔 📈",
            "previous_price": previous,
        }

    if current < previous:

        return {
            "direction": "DOWN",
            "icon": "🔔 📉",
            "previous_price": previous,
        }

    return {
        "direction": "FLAT",
        "icon": "🔔",
        "previous_price": previous,
    }


# =========================================================
# Alert message
# =========================================================

def build_alert_list_text(alerts):
    """
    Preserve Notion order.

    @alert itself is hidden from notification.
    """

    return "\n".join(
        alert["display_text"]
        for alert in alerts
    )



def build_daily_range_text(
    price,
    daily_range,
):
    """
    Build the daily high / low context shown in every alert.

    Example:

        今日最高 194.00 → 当前 165.96
        回落 -28.04（-14.45%）

        今日最低 164.80 → 当前 165.96
        反弹 +1.16（+0.70%）

    Daily range includes pre-market / regular / after-hours
    because the source 1-minute data uses prepost=True.
    """

    if not daily_range:
        return ""

    try:

        daily_high = float(
            daily_range["high"]
        )

        daily_low = float(
            daily_range["low"]
        )

        current = float(
            price
        )

        if (
            daily_high <= 0
            or daily_low <= 0
            or current <= 0
        ):
            return ""

        from_high_amount = (
            current - daily_high
        )

        from_high_pct = (
            (
                current
                / daily_high
            )
            - 1
        ) * 100

        from_low_amount = (
            current - daily_low
        )

        from_low_pct = (
            (
                current
                / daily_low
            )
            - 1
        ) * 100

        return (
            f"今日最高 {daily_high:.2f} "
            f"→ 当前 {current:.2f}\n"
            f"回落 {from_high_amount:+.2f}"
            f"（{from_high_pct:+.2f}%）\n\n"
            f"今日最低 {daily_low:.2f} "
            f"→ 当前 {current:.2f}\n"
            f"反弹 {from_low_amount:+.2f}"
            f"（{from_low_pct:+.2f}%）"
        )

    except Exception as exc:

        print(
            f"Could not build daily range "
            f"alert text: {exc}"
        )

        return ""


def build_alert_message(
    ticker,
    price,
    previous_price,
    state,
    alerts,
    daily_range=None,
):
    """
    Build user-facing alert message.
    """

    price_text = (
        f"{price:.2f}"
    )

    movement = (
        get_price_movement(
            previous_price,
            price,
        )
    )

    direction = (
        movement["direction"]
    )

    icon = (
        movement["icon"]
    )

    old_price = (
        movement[
            "previous_price"
        ]
    )

    # -----------------------------------------------------
    # Price movement
    # -----------------------------------------------------

    if direction == "UP":

        price_description = (
            f"{ticker} 价格从 "
            f"{old_price:.2f} "
            f"上涨至 {price_text}"
        )

    elif direction == "DOWN":

        price_description = (
            f"{ticker} 价格从 "
            f"{old_price:.2f} "
            f"下跌至 {price_text}"
        )

    else:

        # Useful when @alert configuration changed
        # while price remained unchanged.
        price_description = (
            f"{ticker} 价格为 "
            f"{price_text}"
        )

    kind = state["kind"]

    # -----------------------------------------------------
    # RANGE
    # -----------------------------------------------------

    if kind == "RANGE":

        current_line = (
            state["alert"]
            ["display_text"]
        )

        headline = (
            f"{price_description}，"
            f"进入“{current_line}”"
        )

    # -----------------------------------------------------
    # ABOVE
    # -----------------------------------------------------

    elif kind == "ABOVE":

        highest_line = (
            state["alert"]
            ["display_text"]
        )

        headline = (
            f"{price_description}，"
            f"超过最高范围设置，"
            f"即“{highest_line}”"
        )

    # -----------------------------------------------------
    # BELOW
    # -----------------------------------------------------

    elif kind == "BELOW":

        lowest_line = (
            state["alert"]
            ["display_text"]
        )

        headline = (
            f"{price_description}，"
            f"低于最低范围设置，"
            f"即“{lowest_line}”"
        )

    # -----------------------------------------------------
    # GAP
    # -----------------------------------------------------

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
            f"{price_description}，"
            f"处于“{lower_line}”"
            f"和"
            f"“{upper_line}”之间"
        )

    else:

        headline = (
            f"{price_description}，"
            f"Alert 状态发生变化"
        )

    all_alerts = (
        build_alert_list_text(
            alerts
        )
    )

    daily_range_text = (
        build_daily_range_text(
            price,
            daily_range,
        )
    )

    message_parts = [
        f"{icon} {headline}"
    ]

    if daily_range_text:

        message_parts.append(
            daily_range_text
        )

    message_parts.append(
        all_alerts
    )

    return "\n\n".join(
        message_parts
    )


# =========================================================
# Notion Comment helper
# =========================================================

def split_text_for_notion(
    text,
    chunk_size=1800,
):
    """
    Split long message into multiple rich_text objects.

    One alert still remains ONE Notion comment.
    """

    chunks = []

    remaining = text

    while remaining:

        if len(remaining) <= chunk_size:

            chunks.append(
                remaining
            )

            break

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

            split_at += 1

        chunks.append(
            remaining[
                :split_at
            ]
        )

        remaining = (
            remaining[
                split_at:
            ]
        )

    return chunks


def send_notion_alert_comment(
    block_id,
    ticker,
    message,
    notify_user_id,
):
    """
    Create a comment on the stock heading.

    IMPORTANT:

    The first rich_text object is a REAL
    Notion user mention.

    It is NOT plain text such as:

        @User

    This is what allows Notion to treat the
    alert as a true @mention.
    """

    if not notify_user_id:

        print(
            f"{ticker}: notification user "
            f"ID is missing."
        )

        return False

    url = (
        "https://api.notion.com/v1/comments"
    )

    chunks = (
        split_text_for_notion(
            message
        )
    )

    # 1 mention
    # 1 newline text
    # + message chunks
    if (
        len(chunks) + 2
        > 100
    ):

        print(
            f"{ticker}: alert message is "
            f"too long for one comment."
        )

        return False

    # =====================================================
    # REAL @MENTION
    # =====================================================

    rich_text = [
        {
            "type": "mention",
            "mention": {
                "type": "user",
                "user": {
                    "object": "user",
                    "id": notify_user_id,
                },
            },
        },

        {
            "type": "text",
            "text": {
                "content": "\n\n"
            },
        },
    ]

    # =====================================================
    # Alert body
    # =====================================================

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
        f"{ticker}: sending Notion "
        f"@mention comment..."
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
        "(unknown)",
    )

    print(
        f"{ticker}: Notion @mention "
        f"comment created successfully."
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
    Prevent fake alerts when upgrading old format.

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

    kind = (
        new_state["kind"]
    )

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
    Save internal alert state.
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
    daily_ranges,
    alerts_by_ticker,
    ticker_heading_ids,
):
    """
    Rules:

    First initialization:
        Save state only.
        No notification.

    Same state:
        No notification.

    State changed:

        Build alert
            ↓
        Resolve notification user
            ↓
        Send comment with REAL @mention
            ↓
        Comment succeeds
            ↓
        Update Last Alert Range

    Comment fails:

        DO NOT update state
            ↓
        Next minute retries
    """

    notify_user_id = None

    notification_user_checked = False

    for ticker, alerts in (
        alerts_by_ticker.items()
    ):

        info = (
            ticker_info.get(
                ticker
            )
        )

        if not info:

            print(
                f"{ticker}: not found "
                f"in Stocks Price, skipping"
            )

            continue

        price = (
            prices.get(
                ticker
            )
        )

        # -------------------------------------------------
        # Invalid / missing price
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

        previous_price = (
            info.get(
                "previous_price"
            )
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

        previous_price_text = (
            f"{previous_price:.2f}"
            if (
                previous_price is not None
                and previous_price > 0
            )
            else "(empty)"
        )

        print(
            f"{ticker}: "
            f"previous_price="
            f"{previous_price_text}, "
            f"price={price:.2f}, "
            f"old="
            f"{old_state or '(empty)'}, "
            f"new={new_state}"
        )

        # =================================================
        # FIRST TIME
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
        # OLD FORMAT MIGRATION
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
                previous_price=(
                    previous_price
                ),
                state=state,
                alerts=alerts,
                daily_range=(
                    daily_ranges.get(
                        ticker
                    )
                ),
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
        # Find stock heading
        # -------------------------------------------------

        heading_block_id = (
            ticker_heading_ids.get(
                ticker
            )
        )

        if not heading_block_id:

            print(
                f"{ticker}: ERROR - "
                f"ticker heading block "
                f"not found."
            )

            print(
                f"{ticker}: Last Alert Range "
                f"will NOT be updated."
            )

            continue

        # -------------------------------------------------
        # Resolve user only when an alert actually occurs.
        #
        # No reason to call /users every minute when
        # there are no new alerts.
        # -------------------------------------------------

        if not notification_user_checked:

            notify_user_id = (
                resolve_notification_user_id()
            )

            notification_user_checked = (
                True
            )

        if not notify_user_id:

            print(
                f"{ticker}: ERROR - "
                f"notification user could "
                f"not be determined."
            )

            print(
                f"{ticker}: alert will retry "
                f"next run."
            )

            # IMPORTANT:
            # Do not update state.
            continue

        # -------------------------------------------------
        # Send comment FIRST
        # -------------------------------------------------

        comment_success = (
            send_notion_alert_comment(
                block_id=heading_block_id,
                ticker=ticker,
                message=message,
                notify_user_id=(
                    notify_user_id
                ),
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
                f"next run."
            )

            continue

        # -------------------------------------------------
        # Comment + @mention succeeded.
        #
        # NOW save state.
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
    #
    # IMPORTANT:
    #
    # Current Price is read here BEFORE we overwrite it.
    #
    # Therefore previous_price represents the previous
    # program run.
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

            "previous_price": (
                get_current_price(
                    page
                )
            ),
        }

    # -----------------------------------------------------
    # 2. Find 股票 page EARLY
    #
    # We do this before downloading prices because the notes
    # page may contain a ticker heading that has not yet been
    # added to Stocks Price.
    # -----------------------------------------------------

    notes_page_id = (
        find_stock_notes_page()
    )

    # -----------------------------------------------------
    # 3. Automatically add missing ticker headings
    # -----------------------------------------------------
    #
    # Example:
    #
    #     A new heading appears in 股票:
    #
    #         JPM 小摩
    #
    #     but JPM is not yet in Stocks Price.
    #
    # The program creates a new Stocks Price row automatically
    # WITHOUT requiring Yahoo Finance validation first.
    #
    # This is intentional: TradingView-only or unsupported symbols
    # such as S5TW should still appear in Stocks Price, usually with
    # Current Price = 0, so missing / unsupported tickers are visible.
    #
    # Chinese/category headings are ignored, including:
    #
    #     ————半导体个股————
    #     高估股票
    #     正价股票
    #     低估股票
    #
    # ^GSPC标普500（参考） is preserved because it STARTS
    # with a real ticker code: ^GSPC.
    # -----------------------------------------------------

    add_missing_note_tickers_to_stock_price(
        data_source_id,
        notes_page_id,
        ticker_info,
    )

    if not ticker_info:

        raise RuntimeError(
            "No tickers found "
            "in Stocks Price."
        )

    tickers = list(
        ticker_info.keys()
    )

    # -----------------------------------------------------
    # 4. Download latest prices
    # -----------------------------------------------------

    (
        prices,
        daily_ranges,
    ) = get_prices(
        tickers
    )

    # -----------------------------------------------------
    # 5. Update Stocks Price
    #
    # previous_price has already been saved in memory.
    # -----------------------------------------------------

    for ticker in tickers:

        price = (
            prices.get(
                ticker
            )
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
            daily_range=(
                daily_ranges.get(
                    ticker
                )
            ),
        )

    # -----------------------------------------------------
    # 6. Read @alert + remember ticker heading IDs
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
    # 7. Check states + send @mention notifications
    # -----------------------------------------------------

    process_alert_states(
        ticker_info,
        prices,
        daily_ranges,
        alerts_by_ticker,
        ticker_heading_ids,
    )

    print(
        "Finished."
    )


if __name__ == "__main__":
    main()
