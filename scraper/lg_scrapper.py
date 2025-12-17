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

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            for i in range(times):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    logger.warning(f"Attempt {i+1}/{times} failed for {func.__name__}: {e}")
                    time.sleep(delay)
            logger.error(f"All {times} attempts failed for {func.__name__}")
            return None
        return wrapper
    return decorator

class LGScraper:
    BASE_URL = "https://www.lg.com/us/"
    
    def __init__(self, headless=True):
        self.headless = headless
        self.categories = {}
        self.products_data = []

    def clean_text(self, text):
        if not text: 
            return ""
        
        # Convert to string just in case
        text = str(text)
        
        text = re.sub(r'[\u00B9\u00B2\u00B3\u2070-\u209F]', '', text)
        
        # Remove Trademark, Registered, Copyright symbols
        text = re.sub(r'[\u00AE\u2122\u00A9]', '', text)
        
        # Normalize smart quotes and dashes
        text = text.replace('\u2018', "'").replace('\u2019', "'")
        text = text.replace('\u201C', '"').replace('\u201D', '"')
        text = text.replace('\u2013', '-').replace('\u2014', '-')
        
        # Clean extra whitespace
        return re.sub(r'\s+', ' ', text).strip()

    def discover_categories(self):
        logger.info("Discovering categories from homepage...")
        with sync_playwright() as p:
            browser = p.firefox.launch(headless=self.headless)
            page = browser.new_page()
            
            try:
                page.goto(self.BASE_URL, timeout=60000, wait_until="domcontentloaded")
                
                # Extract links from the page that look like categories
                links = page.evaluate("""() => {
                    const anchors = Array.from(document.querySelectorAll('a'));
                    return anchors.map(a => ({
                        text: a.innerText.trim(),
                        href: a.href
                    })).filter(link => 
                        link.text.length > 0 && 
                        link.href.includes('/us/') && 
                        !link.href.includes('/support') &&
                        !link.href.includes('/business')
                    );
                }""")
                
                for link in links:
                    name = link['text'].lower()
                    href = link['href']
                    if name not in self.categories:
                        self.categories[name] = href
                        
                logger.info(f"Discovered {len(self.categories)} potential categories.")
                
            except Exception as e:
                logger.error(f"Failed to discover categories: {e}")
                self.categories = {
                    "oled tvs": "https://www.lg.com/us/oled-tvs",
                    "refrigerators": "https://www.lg.com/us/refrigerators",
                    "washers": "https://www.lg.com/us/washers-dryers",
                    "speakers": "https://www.lg.com/us/speakers"
                }
            finally:
                browser.close()

    def get_category_url(self, user_query):
        
        query = user_query.lower().strip()
        
        # Exact match
        if query in self.categories:
            return self.categories[query]
        
        # Fuzzy match
        for cat_name, url in self.categories.items():
            if query in cat_name or cat_name in query:
                logger.info(f"Matched query '{user_query}' to category '{cat_name}'")
                return url
        
        # Fallback: construct slug
        slug = query.replace(" ", "-")
        url = f"https://www.lg.com/us/{slug}"
        logger.warning(f"No direct category match found. Trying constructed URL: {url}")
        return url

    @retry(times=3, delay=5)
    def scrape_listing_page(self, page, url):
        """
        Navigates to the listing page and loads all products via infinite scroll.
        """
        logger.info(f"Navigating to listing page: {url}")
        
        page.goto(url, timeout=60000, wait_until="domcontentloaded")
        
        try:
            cookie_accept = page.get_by_text("Accept All", exact=False)
            if cookie_accept.count() > 0 and cookie_accept.first.is_visible():
                logger.info("Accepting cookies...")
                cookie_accept.first.click()
                time.sleep(2)
        except Exception:
            pass

        # Handle "View All" Toggle (Common in Appliances)
        try:
            
            logger.info("Checking for 'View All' toggle...")
            time.sleep(3)
            
            toggle_input = page.locator("input[type='checkbox'][aria-label*='View All']")
            if toggle_input.count() > 0:
                if not toggle_input.first.is_checked():
                     logger.info("Toggling 'View All' via input...")
                     toggle_input.first.check(force=True)
                     page.wait_for_load_state("networkidle", timeout=20000)
                     time.sleep(5)
            else:
                 # Try clicking the label text if input not found
                 view_all_text = page.get_by_text("View All", exact=False)
                 if view_all_text.count() > 0:
                     logger.info("Clicking 'View All' text...")
                     view_all_text.first.click(force=True)
                     page.wait_for_load_state("networkidle", timeout=20000)
                     time.sleep(5)
                     
        except Exception as e:
            logger.debug(f"View All toggle check skipped: {e}")

        logger.info("Starting pagination...")
        
        previous_count = 0
        no_change_count = 0
        
        while True:
            # Scroll to bottom
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            time.sleep(2)
            
            # Check for Load More button
            load_more_btn = page.locator("button:has-text('Load More')").first
            if not load_more_btn.is_visible():
                load_more_btn = page.get_by_role("button", name=re.compile("Load More", re.IGNORECASE)).first

            if load_more_btn.is_visible():
                logger.info("Clicking 'Load More'...")
                try:
                    load_more_btn.scroll_into_view_if_needed()
                    load_more_btn.click(force=True)
                    
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    
                    time.sleep(3) 
                    no_change_count = 0 
                except Exception as e:
                    logger.error(f"Error interacting with 'Load More': {e}")
                    time.sleep(2)
            else:
                # If "Load More" is missing, check if we need to force scroll for "View All" lazy loading
                # Even with "View All" on, items are often lazy-loaded as you scroll down.
                
                # Check for "View All" toggle again, ensure it's on
                try:
                    toggle = page.locator("input[type='checkbox'][aria-label*='View All']").first
                    if toggle.is_visible() and not toggle.is_checked():
                        logger.info("Re-enabling 'View All' toggle...")
                        toggle.check(force=True)
                        time.sleep(5)
                except:
                    pass

                current_count = page.locator("div[class*='mh-product-card']").count()
                if current_count == 0:
                     # Fallback selector
                     current_count = page.locator("div[role='group'][aria-label]").count()
                
                if current_count > previous_count:
                    logger.info(f"Loaded more items: {current_count} (was {previous_count})")
                    previous_count = current_count
                    no_change_count = 0
                    continue
                else:
                    # Force scroll a bit more aggressively
                    page.mouse.wheel(0, 5000)
                    time.sleep(1)
                    
                    no_change_count += 1
                    logger.info(f"No new items loaded. Attempt {no_change_count}/10")
                    if no_change_count >= 10:
                        logger.info("Content stabilized. Stopping pagination.")
                        break
                    time.sleep(2)
        
        content = page.content()
        soup = BeautifulSoup(content, "html.parser")
        
        cards = soup.find_all("div", class_=re.compile("mh-product-card"))
        if not cards:
             cards = soup.find_all("div", attrs={"role": "group"})
             cards = [c for c in cards if c.get("aria-label")]
        
        logger.info(f"Found {len(cards)} product cards.")
        
        listing_products = []
        for card in cards:
            try:
                name = card.get("aria-label") or card.find("h3").get_text(strip=True)
                link_tag = card.find("a", href=True)
                if not link_tag:
                    link_tag = card.find_parent("a", href=True)
                
                if link_tag:
                    product_url = urljoin(self.BASE_URL, link_tag['href'])
                    listing_products.append({"name": name, "url": product_url})
            except Exception:
                continue
                
        unique = {p['url']: p for p in listing_products}.values()
        return list(unique)

    @retry(times=2, delay=2)
    def extract_product_details(self, page, url):
        logger.info(f"Scraping product details: {url}")
        # Optimized goto to avoid full load wait
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        
        try:
            # Wait up to 5s for the rating container to appear
            page.wait_for_selector(".bv_avgRating_component_container", state="attached", timeout=5000)
        except:
            # Proceed anyway if not found (might be no reviews)
            pass
        
        content = page.content()
        soup = BeautifulSoup(content, "html.parser")
        
        next_data = soup.find("script", id="__NEXT_DATA__")
        if not next_data:
            logger.warning(f"No JSON data found for {url}")
            return None
            
        try:
            data = json.loads(next_data.string)
            prod = data.get("props", {}).get("pageProps", {}).get("productData", {}).get("product", {})
            
            if not prod:
                return None
            
            sku = prod.get("sku")
            name = prod.get("title") or prod.get("modelName")
            price = prod.get("price", {}).get("finalPrice") or prod.get("obsSellingPrice")
            stock = prod.get("stockStatus", {}).get("statusCode")
            
            features = []
            for f in prod.get("keyFeatures", []):
                if isinstance(f, dict):
                    feat = f.get("feature") or f.get("featureTitle")
                    if feat: features.append(self.clean_text(feat))
                else:
                    features.append(self.clean_text(str(f)))
            
            specs = []
            tech_specs = prod.get("techSpec", {})
            if isinstance(tech_specs, dict) and tech_specs.get("spec"):
                 spec_groups = tech_specs.get("spec", [])
                 for group in spec_groups:
                     g_name = group.get("groupName", "General")
                     for item in group.get("specs", []):
                         specs.append({
                             "group": g_name,
                             "name": item.get("name"),
                             "value": self.clean_text(item.get("value"))
                         })
            
            # DOM Fallback for Specs (if JSON missing)
            if not specs:
                known_keys = ["Display Resolution", "Resolution", "Screen Size", "Refresh Rate", "Speakers","Power", "Sound", "Capacity", "Dimensions"]
                found_key = None
                for key in known_keys:
                    found_key = soup.find(string=lambda t: t and key == t.strip())
                    if found_key:
                        break
                
                if found_key:
                    # found_key -> div -> div (row container) -> div (list container)
                    # Structure observed: Row -> [Div(Key), Div(Value)]
                    try:
                        # Go up to the row container
                        # Structure: <div row> <div key_wrap> <div key>...</div> </div> <div val_wrap>...</div> </div>
                        # found_key.parent is the div containing text
                        # found_key.parent.parent is the key_wrap
                        # found_key.parent.parent.parent is likely the row
                        
                        current = found_key.parent
                        row = None
                        # Traverse up max 4 levels to find a container with >1 div children
                        for _ in range(4):
                            if current.name == 'div' and len(current.find_all('div', recursive=False)) >= 2:
                                row = current
                                break
                            current = current.parent
                        
                        if row:
                            # Now find the parent list container
                            list_container = row.parent
                            # Iterate all rows in this list
                            for row_div in list_container.find_all('div', recursive=False):
                                cols = row_div.find_all('div', recursive=False)
                                if len(cols) >= 2:
                                    k = cols[0].get_text(strip=True)
                                    v = cols[1].get_text(strip=True)
                                    if k and v:
                                        specs.append({"name": k, "value": self.clean_text(v)})
                    except Exception:
                        pass
            
            rating = "N/A"
            try:
                # Method 1: JSON-LD (Preferred)
                bv_script = soup.find("script", id="bv-jsonld-reviews-data")
                if bv_script:
                    bv_data = json.loads(bv_script.string)
                    agg = bv_data.get("aggregateRating")
                    if agg:
                        rating = agg.get("ratingValue")
                
                # Method 2: Fallback to DOM parsing if JSON-LD missing or incomplete
                if rating == "N/A":
                    # Strategies: 
                    # 1. Offscreen text (e.g., "3.8 out of 5 stars")
                    # 2. Visual container (e.g., "4.8")
                    fallback_selectors = [
                        ("span", "bv_offscreen_text"),
                        ("div", "bv_avgRating_component_container")
                    ]
                    
                    for tag, cls in fallback_selectors:
                        elem = soup.find(tag, class_=cls)
                        if elem:
                            # Extract the first float-like number found in the text
                            match = re.search(r"(\d+(\.\d+)?)", elem.get_text(strip=True))
                            if match:
                                rating = match.group(1)
                                break
            except Exception:
                pass

            return {
                "name": name,
                "sku_id": sku,
                "url": url,
                "price": price,
                "rating": rating,
                "stock_availability": stock,
                "key_features": features,
                "specifications": specs
            }
        except Exception as e:
            logger.error(f"JSON parsing error: {e}")
            return None

    def run(self, category_query):
        self.discover_categories()
        
        target_url = self.get_category_url(category_query)
        if not target_url:
            logger.error("Could not determine category URL.")
            return
            
        with sync_playwright() as p:
            # Use specific User-Agent to avoid headless detection
            browser = p.firefox.launch(headless=self.headless)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:109.0) Gecko/20100101 Firefox/115.0",
                viewport={"width": 1920, "height": 1080}
            )
            page = context.new_page()
            
            listings = self.scrape_listing_page(page, target_url)
            
            if listings:
                logger.info(f"Processing {len(listings)} products...")
                results = []
                for item in listings:
                    details = self.extract_product_details(page, item['url'])
                    if details:
                        results.append(details)
                    time.sleep(1)
                
                filename = SCRAPER_DIR / f"lg_{category_query.replace(' ', '_')}.json"
                with open(filename, "w", encoding="utf-8") as f:
                    json.dump(results, f, indent=2)
                logger.info(f"Saved {len(results)} products to {filename}")
            else:
                logger.error("No products found in listing.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="LG Product Scraper")
    parser.add_argument('category', default='oled tvs', help='Category to scrape')
    args = parser.parse_args()

    scraper = LGScraper(headless=True)
    scraper.run(args.category)