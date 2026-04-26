from __future__ import annotations

import argparse
import os
import platform
import re
import sys
import time
from pathlib import Path
from urllib.parse import unquote

import pandas as pd
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

URL_2GIS_ASTANA = "https://2gis.kz/astana"
INPUT_CSV_NAME = "astana_schools_with_bin.csv"
OUTPUT_CSV_NAME = "astana_schools_with_bin_geocoded.csv"
CITY_SUFFIX = " Астана"

BOT_WALL_PHRASE = "подозрительную активность"


def parse_lon_lat_from_url(url: str) -> tuple[str | None, str | None]:
    """Extract longitude, latitude from 2GIS map URL (m=lon,lat/zoom or unquoted)."""
    decoded = unquote(url)
    # ?m=71.443111%2C51.129548%2F11 → after unquote: ?m=71.443111,51.129548/11
    m = re.search(r"[?&]m=([0-9.]+)\s*[,/]\s*([0-9.]+)", decoded)
    if m:
        return m.group(1), m.group(2)
    m = re.search(r"[?&]m=([0-9.]+)%2C([0-9.]+)", url)
    if m:
        return m.group(1), m.group(2)
    if "/geo/" in decoded:
        # e.g. .../geo/71.4,51.1 or similar fragments in path
        m = re.search(r"/geo/([0-9.]+)[,/;]([0-9.]+)", decoded)
        if m:
            return m.group(1), m.group(2)
    return None, None


def page_looks_like_bot_wall(driver: webdriver.Chrome) -> bool:
    try:
        body = driver.find_element(By.TAG_NAME, "body").text
    except Exception:
        return False
    return BOT_WALL_PHRASE in body


def build_chrome(options: Options) -> webdriver.Chrome:
    chromedriver_path = os.environ.get("CHROMEDRIVER_PATH")
    service = Service(chromedriver_path) if chromedriver_path else Service()
    return webdriver.Chrome(service=service, options=options)


def wait_for_search_input(driver: webdriver.Chrome, timeout: float) -> object:
    """Wait for a usable search field (2GIS markup changes; avoid brittle single selectors)."""
    end = time.time() + timeout
    last_err: Exception | None = None
    while time.time() < end:
        if page_looks_like_bot_wall(driver):
            raise RuntimeError(
                "2GIS показала страницу проверки (антибот). Откройте сайт в обычном Chrome, "
                "при необходимости пройдите проверку, затем запустите скрипт с тем же профилем: "
                "переменная окружения CHROME_USER_DATA_DIR (каталог User Data) или снимите --headless."
            )
        try:
            for inp in driver.find_elements(By.TAG_NAME, "input"):
                if not inp.is_displayed():
                    continue
                typ = (inp.get_attribute("type") or "text").lower()
                if typ in ("submit", "button", "hidden", "checkbox", "radio"):
                    continue
                if inp.size.get("height", 0) < 8:
                    continue
                return inp
        except Exception as e:
            last_err = e
        time.sleep(0.4)
    if last_err:
        raise TimeoutError("Не найдено поле поиска") from last_err
    raise TimeoutError("Не найдено поле поиска")


def clear_input(el) -> None:
    el.click()
    mod = Keys.COMMAND if platform.system() == "Darwin" else Keys.CONTROL
    el.send_keys(mod, "a")
    el.send_keys(Keys.BACKSPACE)


def open_astana_map(driver: webdriver.Chrome, wait: WebDriverWait) -> None:
    driver.get(URL_2GIS_ASTANA)
    wait.until(lambda d: d.execute_script("return document.readyState") == "complete")
    time.sleep(1.5)


def geocode_one(
    driver: webdriver.Chrome,
    wait: WebDriverWait,
    query: str,
    settle_s: float,
) -> tuple[str | None, str | None]:
    open_astana_map(driver, wait)
    inp = wait_for_search_input(driver, timeout=90)
    clear_input(inp)
    inp.send_keys(query)
    time.sleep(1.0)
    # Prefer first suggestion when present (closer to chosen building).
    for css in (
        "[role='option']",
        "[role='listbox'] li",
        "a[href*='/astana/geo/']",
        "[class*='suggest'] a",
    ):
        try:
            candidates = [e for e in driver.find_elements(By.CSS_SELECTOR, css) if e.is_displayed()]
            if candidates:
                candidates[0].click()
                time.sleep(settle_s)
                lon, lat = parse_lon_lat_from_url(driver.current_url)
                if lon and lat:
                    return lon, lat
                break
        except Exception:
            continue

    before = driver.current_url
    inp.send_keys(Keys.ENTER)
    time.sleep(settle_s)
    deadline = time.time() + 12
    while time.time() < deadline:
        lon, lat = parse_lon_lat_from_url(driver.current_url)
        if lon and lat and driver.current_url != before:
            return lon, lat
        time.sleep(0.35)
    return parse_lon_lat_from_url(driver.current_url)


def main() -> int:
    parser = argparse.ArgumentParser(description="Geocode addresses from CSV using 2GIS Astana in the browser.")
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=f"Input CSV (default: {INPUT_CSV_NAME} next to this script)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help=f"Output CSV (default: {OUTPUT_CSV_NAME} next to this script)",
    )
    parser.add_argument("--headless", action="store_true", help="Headless Chrome (часто блокируется 2GIS).")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N rows (0 = all).")
    parser.add_argument("--delay", type=float, default=2.0, help="Pause between rows (seconds).")
    parser.add_argument("--settle", type=float, default=2.5, help="Wait after search/suggestion (seconds).")
    args = parser.parse_args()

    base = Path(__file__).resolve().parent
    input_path = args.input or (base / INPUT_CSV_NAME)
    output_path = args.output or (base / OUTPUT_CSV_NAME)

    if not input_path.is_file():
        print(f"Файл не найден: {input_path}", file=sys.stderr)
        return 1

    df = pd.read_csv(input_path)
    expected = ["school_name", "type_of_school", "adress", "BIN", "date_of_start"]
    missing = [c for c in expected if c not in df.columns]
    if missing:
        print(f"В CSV нет колонок: {missing}. Есть: {list(df.columns)}", file=sys.stderr)
        return 1

    options = Options()
    if args.headless:
        options.add_argument("--headless=new")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])

    user_data = os.environ.get("CHROME_USER_DATA_DIR")
    if user_data:
        options.add_argument(f"--user-data-dir={user_data}")
    profile_dir = os.environ.get("CHROME_PROFILE_DIRECTORY")
    if profile_dir:
        options.add_argument(f"--profile-directory={profile_dir}")

    driver = build_chrome(options)
    wait = WebDriverWait(driver, 25)
    longitudes: list[str | None] = []
    latitudes: list[str | None] = []

    try:
        n = len(df) if args.limit <= 0 else min(args.limit, len(df))
        for i in range(n):
            row = df.iloc[i]
            addr = str(row["adress"]).strip()
            if not addr or addr.lower() == "nan":
                longitudes.append(None)
                latitudes.append(None)
                continue
            query = f"{addr}{CITY_SUFFIX}"
            print(f"[{i + 1}/{n}] {query!r}")
            try:
                lon, lat = geocode_one(driver, wait, query, args.settle)
            except Exception as e:
                print(f"  ошибка: {e}", file=sys.stderr)
                lon, lat = None, None
            longitudes.append(lon)
            latitudes.append(lat)
            print(f"  → lon={lon}, lat={lat}")
            if i + 1 < n and args.delay > 0:
                time.sleep(args.delay)
    finally:
        driver.quit()

    out = df.iloc[: len(longitudes)].copy()
    out["longitude"] = longitudes
    out["latitude"] = latitudes
    out.to_csv(output_path, index=False)
    print(f"Записано: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
