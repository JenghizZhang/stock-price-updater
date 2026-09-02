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
    """
    Find the Stocks Price data source shared with this Notion connection.
    """
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
    """
    Read all rows from Stocks Price.
    """
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
    """
    Read ticker from Notion row.
    """
    title_items = page["properties"]["Ticker"]["title"]

    if not title_items:
        return None

    return "".join(
        item["plain_text"]
        for item in title_items
    ).strip().upper()


def get_prices(tickers):
    """
    Get latest available price.

    Valid ticker:
        latest price

    Invalid ticker / no data:
        0
    """

    print(f"Downloading prices for: {', '.join(tickers)}")

    # Default every ticker to 0.
    # If Yahoo returns a valid price, it will overwrite 0.
    prices = {
        ticker: 0.0
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

        for ticker in tickers:
            try:
                close = data[ticker]["Close"].dropna()

                if not close.empty:
                    price = float(close.iloc[-1])

                    if price > 0:
                        prices[ticker] = price
                        print(
                            f"{ticker}: ${price:.2f}"
                        )
                    else:
                        print(
                            f"{ticker}: invalid price -> 0"
                        )

                else:
                    print(
                        f"{ticker}: no price data -> 0"
                    )

            except Exception as exc:
                print(
                    f"{ticker}: failed to read price "
                    f"({exc}) -> 0"
                )

    except Exception as exc:
        print(
            f"Yahoo Finance request failed: {exc}"
        )

    return prices


def update_notion_page(page_id, ticker, price):
    """
    Update only Current Price and Last Updated.
    """
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
        print(
            f"Updated {ticker}: INVALID / NO DATA -> $0"
        )
    else:
        print(
            f"Updated {ticker}: ${price:.2f}"
        )


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
        price = prices.get(ticker, 0.0)

        update_notion_page(
            page_id=page_id,
            ticker=ticker,
            price=price,
        )

    print("Finished updating stock prices.")


if __name__ == "__main__":
    main()
