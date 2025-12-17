import re
import time
import json
import argparse
import functools
import logging
from pathlib import Path
from urllib.parse import urljoin
from playwright.sync_api import sync_playwright
from bs4 import BeautifulSoup

# configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# constants
SCRAPER_DIR = Path("scraper")
SCRAPER_DIR.mkdir(exist_ok=True)

def retry(times=3, delay=2):
    """Retry decorator for functions that may fail intermittently."""
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for attempt in range(times):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    logger.warning(f"Attempt {attempt + 1} failed: {e}")
                    time.sleep(delay)
            logger.error(f"Function {func.__name__} failed after {times} attempts")
            return None
        return wrapper
    return decorator

class LGScraper:
    BASE_URL = "https://www.lg.com/us"

    def __init__(self, headless=True):
        self.headless = headless
        self.categories = {}
        self.products_data = []
    
    def discover_categories(self):

        logger.info("Discovering categories...")

        with sync_playwright() as p:
            browser = p.firefox.launch(headless=self.headless)
            page = browser.new_page()

            try:
                page.goto(self.BASE_URL)

                # extracting links from the page that contain categories
                links = page.evaluate("""() => {
                    const anchors = Array.from(document.querySelectorAll('a'));
                    return anchors.map(a => ({
                        text: a.innerText.trim(),
                        href: a.href
                    })).filter(link =>
                        link.text.length > 0 &&
                        link.href.includes('/us') &&
                        !link.href.includes('/support') &&
                        !link.href.includes('/business')
                    );
                }""")
                for link in links:
                    name = link.get('text') or ''
                    href = link.get('href')
                    if name and href and name.lower() not in self.categories:
                        self.categories[name.lower()] = href
                logger.info(f"Discovered potential categories: {len(self.categories)}")
                
            except Exception as e:
                logger.error(f"Error discovering categories: {e}")
                # fallback categories
                self.categories = {
                    "oled tvs": "https://www.lg.com/us/tvs/oled-tvs",
                    "refrigerators": "https://www.lg.com/us/refrigerators",
                    "washers": "https://www.lg.com/us/washers-dryers",
                    "speakers": "https://www.lg.com/us/speakers-audio"
                }
            finally:
                browser.close()
        
    def get_category_url(self, user_query):

        query = user_query.lower().strip()

        if query in self.categories:
            return self.categories[query]
        
        # fuzzy matching
        for cat_name, url in self.categories.items():
            if query in cat_name or cat_name in query:
                logger.info(f"Fuzzy matched category '{cat_name}' for query '{query}'")
                return url
        
        # Fallback to constructing category
        slug = query.replace(" ", "-")
        url =  f"{self.BASE_URL}/{slug}"
        logger.info(f"Using fallback category URL: {url}")
        return url

    @retry(times=3, delay=5)
    def scrape_listing_page(self, page, url):

        logger.info(f"Scraping listing page: {url}")
        page.goto(url, timeout=60000)

        try:
            cookie_accept = page.get_by_text("Accept All", exact=False)
            if cookie_accept.count() > 0 and cookie_accept.first.is_visible():
                logger.info("Cookie acceptance prompt detected")
                cookie_accept.first.click()
                time.sleep(2)
        except Exception:
            pass
        
        # Handle "View All" toggle if present
        try:
            logger.info("Checking for 'View All' toggle...")
            time.sleep(2)

            toggle_input = page.locator("input[type='checkbox'][aria-label*='View All']")
            if toggle_input.count() > 0:
                logger.info("'View All' toggle found, activating...")
                toggle_input.first.check(force=True)
                page.wait_for_load_state("networkidle", timeout=20000)
                time.sleep(3)
            else:
                view_all_text = page.get_by_text("View All", exact=False)
                if view_all_text.count() > 0:
                    logger.info("'View All' text button found, clicking...")
                    view_all_text.first.click(force=True)
                    page.wait_for_load_state("networkidle", timeout=20000)
                    time.sleep(3)
        except Exception as e:
            logger.debug(f"No 'View All' toggle found, toggle check skipped: {e}")
            
        logger.info("Starting pagination...")

        previous_count = 0
        no_change_count = 0

        while True:
            # Scroll to bottom to trigger lazy loading
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)

            # check for load more button
            load_more_locator = page.locator("button:has-text('Load More')")
            try:
                if load_more_locator.count() > 0 and load_more_locator.first.is_visible():
                    load_more_btn = load_more_locator.first
                else:
                    load_more_btn = page.get_by_role("button", name=re.compile("Load More", re.IGNORECASE)).first
            except Exception:
                load_more_btn = None

            if load_more_btn:
                try:
                    if load_more_btn.is_visible():
                        logger.info("Clicking 'Load More' button...")
                        load_more_btn.scroll_into_view_if_needed()
                        load_more_btn.click(force=True)
                        try:
                            page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            logger.debug("Timeout waiting for network idle after clicking 'Load More'")
                        time.sleep(3)
                        no_change_count = 0  # reset no change count after successful load
                    else:
                        # invisible button, ignore
                        pass
                except Exception as e:
                    logger.error(f"Error clicking 'Load More' button: {e}")
                    time.sleep(2)
            else:
                # no more load more button
                current_count = page.locator("div[class*='mh-product-card']").count()
                if current_count == 0:
                    # fallback selector
                    current_count = page.locator("div[role='group'][aria-label]").count()
                
                if current_count > previous_count:
                    logger.info(f"Loaded more products: {current_count} total")
                    previous_count = current_count
                    no_change_count = 0
                    # scroll again just in case
                    continue
                else:
                    no_change_count += 1
                    logger.info(f"No new products loaded, no change count: {no_change_count}")
                    if no_change_count >= 5:
                        logger.info("No more products to load, ending pagination.")
                        break
                    time.sleep(2)

        content = page.content()
        soup = BeautifulSoup(content, 'html.parser')

        cards = soup.find_all("div", class_=re.compile(r"mh-product-card"))
        if not cards:
            cards = soup.find_all("div", attrs={"role": "group"})
            cards = [card for card in cards if card.get("aria-label")]

        logger.info(f"Found {len(cards)} product cards on the page.")

        listing_products = []

        for card in cards:
            try:
                name = card.get("aria-label") or (card.find("h3").get_text(strip=True) if card.find("h3") else None)
                link_tag = card.find("a", href=True)
                if not link_tag:
                    link_tag = card.find_parent("a", href=True)

                if link_tag and link_tag.get('href'):
                    product_url = urljoin(self.BASE_URL, link_tag['href'])
                    listing_products.append({
                        "name": name,
                        "url": product_url})
            except Exception:
                continue

        unique = list({p['url']: p for p in listing_products}.values())
        return unique
    
    @retry(times=2, delay=2)
    def extract_product_details(self, page, product):
        # product is expected to be a dict with 'url' and optional 'name'
        logger.info(f"Extracting details for product: {product.get('name') or product.get('url')}")

        page.goto(product['url'], timeout=60000)
        page.wait_for_load_state("domcontentloaded", timeout=30000)

        content = page.content()
        soup = BeautifulSoup(content, 'html.parser')

        next_data = soup.find("script", id="__NEXT_DATA__")
        if not next_data:
            logger.warning("No __NEXT_DATA__ script tag found on product page.")
            return None
        try:
            data = json.loads(next_data.string)
            prod = data.get("props", {}).get("pageProps", {}).get("productData", {}).get("product", {})

            if not prod:
                logger.warning("No product data found in __NEXT_DATA__.")
                return None
            
            sku = prod.get("sku")
            name = prod.get("title") or prod.get("modelName") or product.get("name")
            price = prod.get("price", {}).get("finalPrice") or prod.get("obsSellingPrice")
            stock = prod.get("stockStatus",  {}).get("statusCode")


            features = []
            for f in prod.get("keyFeatures", []):
                if isinstance(f, dict):
                    feat = f.get("feature") or f.get("featureTitle")
                    if feat: features.append(feat.strip())
                else:
                    features.append(str(f).strip())

            
            specs = []
            tech_specs = prod.get("techSpec", {})
            if isinstance(tech_specs, dict):
                spec_groups = tech_specs.get("spec", [])
                for group in spec_groups:
                    g_name = group.get("groupName", "General")
                    for item in group.get("specs", []):
                        specs.append({
                            "group": g_name,
                            "name": item.get("name"),
                            "value": item.get("value")
                        })
            
            rating = "N/A"
            try:

                bv_script = soup.find("script", id="bv-jsonld-reviews-data")
                if bv_script:
                    bv_data = json.loads(bv_script.string)
                    agg = bv_data.get("aggregateRating")
                    if agg:
                        rating = agg.get("ratingValue")

                # another method in case JSON-LD is not present

                if rating == "N/A":
                    rating_text_elem = soup.find("span", class_="bv_offscreen_text")
                    if rating_text_elem:
                        text = rating_text_elem.get_text(strip=True)
                        match = re.search(r"(\d+(\.\d+)?) out of 5", text)
                        if match:
                            rating = match.group(1)
                

            except Exception:
                pass

            return {
                "name": name,
                "sku_id": sku,
                "price": price,
                "rating": rating,
                "stock_availability": stock,
                "key_features": features,
                "specifications": specs,
                "url": product['url']
            }
        except Exception as e:
            logger.error(f"Error extracting product details: {e}")
            return None
        
    def run(self, category_query):
        self.discover_categories()

        target_url = self.get_category_url(category_query)
        if not target_url:
            logger.error("No valid category URL found. Exiting.")
            return
        
        with sync_playwright() as p:

            browser = p.firefox.launch(headless=self.headless)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Gecko/20100101 Firefox/115.0",
                viewport={"width": 1920, "height": 1080}
            )

            page = context.new_page()

            listings = self.scrape_listing_page(page, target_url)

            if listings:
                logger.info(f"Total products found in listing: {len(listings)}")

                results = []
                for item in listings:
                    details = self.extract_product_details(page, item)
                    if details:
                        results.append(details)
                    time.sleep(1)
                
                filename = SCRAPER_DIR / f"lg_{category_query.replace(' ', '_').lower()}.json"
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(results, f, indent=2)
                logger.info(f"Scraping completed. Data saved to {filename}")
            else:
                logger.warning("No products found in the listing page.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LG Product Scraper")
    parser.add_argument('--category', '-c', default='oled tvs', help='Category to scrape')
    args = parser.parse_args()

    scraper = LGScraper()
    scraper.run(args.category)