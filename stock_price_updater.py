import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests
import yfinance as yf


NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_VERSION = "2026-03-11"

NOTION_HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


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
        headers=NOTION_HEADERS,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    results = response.json()["results"]

    if not results:
        raise RuntimeError(
            "Cannot find the Stocks Price data source."
        )

    print(f"Found Notion data source: {results[0]['id']}")
    return results[0]["id"]


def get_stock_rows(data_source_id):
    url = (
        f"https://api.notion.com/v1/data_sources/"
        f"{data_source_id}/query"
    )

    response = requests.post(
        url,
        headers=NOTION_HEADERS,
        json={"page_size": 100},
        timeout=30,
    )
    response.raise_for_status()

    return response.json()["results"]


def get_ticker(page):
    title_items = page["properties"]["Ticker"]["title"]

    if not title_items:
        return None

    return "".join(
        item["plain_text"]
        for item in title_items
    ).strip().upper()


def verify_missing_ticker(ticker):
    """
    Returns:
        0.0  -> ticker appears invalid / no market data exists
        None -> temporary Yahoo/network failure; do not update Notion
        price -> valid ticker and price found
    """

    try:
        stock = yf.Ticker(ticker)

        history = stock.history(
            period="5d",
            interval="1d",
            auto_adjust=False
        )

        if history is None or history.empty:
            print(f"{ticker}: no market data found -> treating as invalid")
            return 0.0

        close = history["Close"].dropna()

        if close.empty:
            print(f"{ticker}: no valid closing price -> treating as invalid")
            return 0.0

        price = float(close.iloc[-1])

        if price <= 0:
            return 0.0

        print(f"{ticker}: verification succeeded -> ${price:.2f}")
        return price

    except Exception as exc:
        print(
            f"{ticker}: verification request failed ({exc}) "
            f"-> keep previous Notion price"
        )
        return None


def get_prices(tickers):
    """
    Returns:
        ticker: positive number -> update price
        ticker: 0.0             -> invalid ticker
        ticker: None            -> temporary failure, do not update
    """

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
        # Important:
        # If the whole Yahoo request fails, DO NOT set everything to 0.
        print(
            f"Yahoo Finance batch request failed: {exc}"
        )
        print(
            "Keeping all previous Notion prices."
        )
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

        # Batch download did not return a usable price.
        # Check this ticker one more time separately.
        print(
            f"{ticker}: missing from batch response, verifying..."
        )

        prices[ticker] = verify_missing_ticker(ticker)

    return prices


def update_notion_page(page_id, ticker, price):
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
        headers=NOTION_HEADERS,
        json=payload,
        timeout=30,
    )
    response.raise_for_status()

    if price == 0:
        print(f"Updated {ticker}: INVALID TICKER -> $0")
    else:
        print(f"Updated {ticker}: ${price:.2f}")


def main():
    data_source_id = find_stock_data_source()

    pages = get_stock_rows(data_source_id)

    ticker_pages = {}

    for page in pages:
        ticker = get_ticker(page)

        if ticker:
            ticker_pages[ticker] = page["id"]

    if not ticker_pages:
        raise RuntimeError(
            "No tickers found in Stocks Price."
        )

    prices = get_prices(
        list(ticker_pages.keys())
    )

    for ticker, page_id in ticker_pages.items():

        price = prices.get(ticker)

        # Temporary Yahoo/network failure:
        # keep existing Notion price untouched.
        if price is None:
            print(
                f"Skipping {ticker}: temporary data failure. "
                f"Previous Notion price preserved."
            )
            continue

        update_notion_page(
            page_id=page_id,
            ticker=ticker,
            price=price,
        )

    print("Finished updating stock prices.")


if __name__ == "__main__":
    main()
