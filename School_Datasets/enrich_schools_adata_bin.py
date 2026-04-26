from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass

import pandas as pd
from selenium import webdriver
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    StaleElementReferenceException,
    TimeoutException,
)
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

SEARCH_URL = "https://pk.adata.kz/search?region_ids=2&most_viewed_companies=0"
INPUT_CSV = "astana_schools_cleaned.csv"
OUTPUT_CSV = "astana_schools_with_bin.csv"

SEARCH_INPUT_CSS = 'input[id="Введите ИИН, БИН, ФИО, название компании"]'
SEARCH_FALLBACK_CSS = "input[type='text'][required][autocomplete='off']"
# Limit to search-result cards (Astana region), avoids unrelated /company/ links elsewhere on the page.
COMPANY_LINK_CSS = 'a[href^="/company/"][href*="region_ids=2"]'
WAIT_TIMEOUT = 20
POST_SEARCH_PAUSE = 2.5
BETWEEN_ROWS_PAUSE = 1.0

BIN_PATTERN = re.compile(r"БИН\s*(\d{10,14})", re.UNICODE)


@dataclass
class LookupResult:
    bin_int: int | None
    date_mm_yy: str | None


def build_driver(headless: bool = False) -> webdriver.Chrome:
    chrome_options = Options()
    if headless:
        chrome_options.add_argument("--headless=new")
        chrome_options.add_argument("--window-size=1400,900")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
    else:
        chrome_options.add_experimental_option("detach", True)
        chrome_options.add_argument("--start-maximized")

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


def find_search_input(driver: webdriver.Chrome):
    wait = WebDriverWait(driver, WAIT_TIMEOUT)
    try:
        return wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SEARCH_INPUT_CSS)))
    except TimeoutException:
        return wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, SEARCH_FALLBACK_CSS)))


def _scroll_center(driver: webdriver.Chrome, el) -> None:
    driver.execute_script("arguments[0].scrollIntoView({block: 'center', inline: 'nearest'});", el)
    time.sleep(0.12)


def submit_search(driver: webdriver.Chrome, inp) -> None:
    """
    Trigger search: the site's primary CTA is a filled blue «Найти» button (Vue/Nuxt).
    Regular Selenium .click() often does nothing (intercept, z-index, or custom handling).
    Prefer JS .click() after scroll; exact span text «Найти» avoids the «Избранные» cluster.
    """
    _scroll_center(driver, inp)
    xpath = (
        "//button[contains(@class,'bg-blue-700')]"
        "[.//span[normalize-space()='Найти']]"
    )
    for btn in driver.find_elements(By.XPATH, xpath):
        try:
            if not btn.is_displayed():
                continue
            _scroll_center(driver, btn)
            # JS click reliably triggers Vue/Nuxt listeners when native click does not.
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(0.25)
            return
        except StaleElementReferenceException:
            continue
    # Fallback: any visible blue button whose full text is just «Найти».
    for btn in driver.find_elements(By.CSS_SELECTOR, "button.bg-blue-700"):
        try:
            if not btn.is_displayed():
                continue
            if (btn.text or "").strip() != "Найти":
                continue
            _scroll_center(driver, btn)
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(0.25)
            return
        except StaleElementReferenceException:
            continue
    # Last resort: Enter in the search field.
    try:
        inp.send_keys(Keys.ENTER)
    except ElementClickInterceptedException:
        driver.execute_script(
            "arguments[0].dispatchEvent(new KeyboardEvent('keydown',"
            "{key:'Enter',code:'Enter',keyCode:13,bubbles:true}));",
            inp,
        )
    except StaleElementReferenceException:
        fresh = find_search_input(driver)
        fresh.send_keys(Keys.ENTER)


def clear_search_input(el) -> None:
    el.click()
    select_all = Keys.COMMAND if sys.platform == "darwin" else Keys.CONTROL
    el.send_keys(select_all + "a")
    el.send_keys(Keys.BACKSPACE)


def parse_bin_from_card_text(text: str) -> str | None:
    m = BIN_PATTERN.search(text)
    return m.group(1) if m else None


def bin_prefix_to_mm_yy(bin_digits: str) -> str | None:
    digits = "".join(ch for ch in bin_digits if ch.isdigit())
    if len(digits) < 4:
        return None
    yy = digits[:2]
    mm = digits[2:4]
    month = int(mm)
    if not 1 <= month <= 12:
        return None
    return f"{month:02d}.{yy}"


def lookup_one(driver: webdriver.Chrome, query: str) -> LookupResult:
    inp = find_search_input(driver)
    clear_search_input(inp)
    inp.send_keys(query)
    submit_search(driver, inp)

    time.sleep(POST_SEARCH_PAUSE)

    cards = driver.find_elements(By.CSS_SELECTOR, COMPANY_LINK_CSS)
    visible = [c for c in cards if c.is_displayed()]
    if len(visible) != 1:
        return LookupResult(None, None)

    text = visible[0].text
    bin_raw = parse_bin_from_card_text(text)
    if not bin_raw:
        return LookupResult(None, None)

    try:
        bin_int = int(bin_raw)
    except ValueError:
        return LookupResult(None, None)

    mm_yy = bin_prefix_to_mm_yy(bin_raw)
    if mm_yy is None:
        return LookupResult(None, None)

    return LookupResult(bin_int, mm_yy)


def main() -> None:
    parser = argparse.ArgumentParser(description="Enrich schools CSV with adata.kz BIN lookup.")
    parser.add_argument("--headless", action="store_true", help="Run Chrome headless.")
    parser.add_argument("--limit", type=int, default=None, help="Process only first N rows.")
    parser.add_argument("--input", default=INPUT_CSV)
    parser.add_argument("--output", default=OUTPUT_CSV)
    args = parser.parse_args()

    df = pd.read_csv(args.input, encoding="utf-8-sig")
    name_col = "school name"
    type_col = "type of school"
    addr_col = "adress"
    for col in (name_col, type_col, addr_col):
        if col not in df.columns:
            raise SystemExit(f"Missing column {col!r} in {args.input}. Found: {list(df.columns)}")

    if args.limit is not None:
        df = df.head(args.limit).copy()

    driver = build_driver(headless=args.headless)
    try:
        driver.get(SEARCH_URL)
        WebDriverWait(driver, WAIT_TIMEOUT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, SEARCH_FALLBACK_CSS))
        )
        time.sleep(1.0)

        bins: list[int | None] = []
        dates: list[str | None] = []

        for n_done, (_, row) in enumerate(df.iterrows(), start=1):
            q = str(row[name_col]).strip()
            if not q:
                bins.append(None)
                dates.append(None)
                continue
            try:
                res = lookup_one(driver, q)
                bins.append(res.bin_int)
                dates.append(res.date_mm_yy)
            except Exception:
                bins.append(None)
                dates.append(None)
            time.sleep(BETWEEN_ROWS_PAUSE)
            if n_done % 25 == 0:
                print(f"Processed {n_done} rows...")
    finally:
        driver.quit()

    out = pd.DataFrame(
        {
            "school_name": df[name_col].astype(str),
            "type_of_school": df[type_col].astype(str),
            "adress": df[addr_col].astype(str),
            "BIN": pd.array(bins, dtype="Int64"),
            "date_of_start": dates,
        }
    )
    out.to_csv(args.output, index=False, encoding="utf-8-sig", na_rep="NULL")
    print(f"Wrote {len(out)} rows to {args.output}")


if __name__ == "__main__":
    main()
