# LG Scraper

A small Playwright-based scraper for LG product pages (script: `lg_scrapper.py`).

## Requirements
- Python 3.8+
- Playwright (and a browser, recommended: Firefox)

Install:
```bash
pip install -r requirements.txt
# or
pip install playwright
playwright install firefox
```

## Usage
```bash
python3 lg_scrapper.py <product-type>
```

Arguments (examples):
- `oled-tv`
- `refrigerator`
- `washer`
- any other product type supported by the site

Example:
```bash
python3 lg_scrapper.py "oled tv"
```

## What this scraper extracts
- SKU
- Price
- Stock status
- Rating (where available)
- Other relevant product details (from page JSON when available)

## Notes & Troubleshooting

- View-all / Load-more behavior
    - Some product pages (e.g., refrigerators) use a "View all" toggle that reveals a "Load more" button. Ensure the scraper:
        - Clicks/enables any "View all" toggle when present
        - Calls the "load more" action repeatedly until no additional items load

- Ratings
    - Ratings were inconsistent in markup. A matching-based extraction heuristic is used to find rating values; it is not ideal but works across observed pages.

- Accurate SKU / Price / Stock
    - The most reliable source was the `__NEXT_DATA__` JSON state embedded in the page. Extract and parse that JSON to get consistent SKU/price/stock values.

- Browser choice
    - Chromium had intermittent issues during development; Firefox with Playwright was more stable. If you hit renderer-specific problems, try running with Firefox:
        ```py
        browser = await playwright.firefox.launch(...)
        ```

- Navigation timeouts
    - Page navigation sometimes timed out. Use a DOMContentLoaded wait and/or increase timeouts:
        ```py
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        ```

## Development tips
- Log responses when parsing JSON to confirm the structure before extracting fields.
- Add retries and exponential backoff around click/load-more interactions to handle flaky network/load states.
- Keep selectors resilient to small markup changes; prefer JSON-backed extraction when available.

## Credits
Generated with assistance from GitHub Copilot.

