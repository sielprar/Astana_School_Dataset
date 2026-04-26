import csv
import time
from dataclasses import dataclass
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager


URL = (
    "https://2gis.kz/astana/search/%D1%88%D0%BA%D0%BE%D0%BB%D1%8B?m=71.454278%2C51.120707%2F12.5"
)
TARGET_SCHOOLS = 246
SCHOOLS_PER_PAGE = 12
OUTPUT_CSV = "astana_schools.csv"
WAIT_TIMEOUT_SECONDS = 15
PAGE_LOAD_SLEEP_SECONDS = 12
START_COLLECTION_DELAY_SECONDS = 5


@dataclass(frozen=True)
class School:
    school_name: str
    type_of_school: str
    adress: str


def build_driver() -> webdriver.Chrome:
    chrome_options = Options()
    chrome_options.add_experimental_option("detach", True)
    chrome_options.add_argument("--start-maximized")

    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=chrome_options)


def normalize(text: str) -> str:
    return " ".join(text.split()).strip()


def wait_for_cards(driver: webdriver.Chrome, timeout: int = WAIT_TIMEOUT_SECONDS) -> None:
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "div._1kf6gff"))
    )


def get_scroll_panel(driver: webdriver.Chrome):
    return driver.execute_script(
        """
        const firstCard = document.querySelector("div._1kf6gff");
        if (!firstCard) return null;

        let el = firstCard.parentElement;
        while (el) {
            const style = window.getComputedStyle(el);
            const canScroll = el.scrollHeight > (el.clientHeight + 20);
            const overflow = style.overflowY || "";
            if (canScroll && (overflow.includes("auto") || overflow.includes("scroll"))) {
                return el;
            }
            el = el.parentElement;
        }

        // Fallback to known panel if no ancestor match found.
        return document.querySelector("div._8hh56jx[data-scroll='true']") ||
               document.querySelector("div[data-scroll='true']");
        """
    )


def scroll_to_load_target(
    driver: webdriver.Chrome,
    scroll_container,
    target_cards: int = SCHOOLS_PER_PAGE,
    max_scrolls: int = 30,
    scroll_pause: float = 0.7,
) -> int:
    if scroll_container is None:
        return len(driver.find_elements(By.CSS_SELECTOR, "div._1kf6gff"))

    for i in range(max_scrolls):
        cards_count = len(driver.find_elements(By.CSS_SELECTOR, "div._1kf6gff"))
        if cards_count >= target_cards:
            print(f"Reached {cards_count} cards after {i} scrolls")
            return cards_count

        scroll_amount = 700 + (i * 40)
        driver.execute_script(
            "arguments[0].scrollTop = arguments[0].scrollTop + arguments[1];",
            scroll_container,
            scroll_amount,
        )
        time.sleep(scroll_pause)

    final_count = len(driver.find_elements(By.CSS_SELECTOR, "div._1kf6gff"))
    print(f"After max scrolls loaded {final_count} cards")
    return final_count


def extract_current_page(driver: webdriver.Chrome) -> list[School]:
    cards = driver.find_elements(By.CSS_SELECTOR, "div._1kf6gff")
    page_schools: list[School] = []

    # 2GIS shows up to 12 cards per page for this search.
    for card in cards[:SCHOOLS_PER_PAGE]:
        # Use card-local top block selectors to avoid grabbing nested CTA/link texts.
        name_el = card.find_elements(By.CSS_SELECTOR, "div._zjunba a._1rehek")
        type_el = card.find_elements(By.CSS_SELECTOR, "div._1idnaau a")
        address_el = card.find_elements(By.CSS_SELECTOR, "div._klarpw span")

        if not name_el:
            continue

        name_text = normalize(name_el[0].get_attribute("textContent") or "")
        type_text = normalize(type_el[0].get_attribute("textContent") or "") if type_el else ""

        # Choose the first address-looking line, skip status like "Закрыто".
        address_text = ""
        for addr in address_el:
            candidate = normalize(addr.get_attribute("textContent") or "")
            if candidate and candidate.lower() not in {"закрыто", "открыто", "скоро открытие"}:
                address_text = candidate
                break

        if not name_text:
            continue

        school = School(
            school_name=name_text,
            type_of_school=type_text,
            adress=address_text,
        )
        page_schools.append(school)

    return page_schools


def current_page_number(driver: webdriver.Chrome) -> int | None:
    cur = driver.find_elements(By.CSS_SELECTOR, "div._l934xo5 span._19xy60y")
    if not cur:
        return None
    txt = normalize(cur[0].text)
    return int(txt) if txt.isdigit() else None


def go_to_next_numbered_page(driver: webdriver.Chrome, target_page: int) -> bool:
    # Prefer clicking the exact numbered page link.
    for _ in range(4):
        links = driver.find_elements(By.CSS_SELECTOR, "a._12164l30")
        target_link = None
        for link in links:
            if normalize(link.text) == str(target_page):
                target_link = link
                break

        if target_link is not None:
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", target_link)
            time.sleep(0.5)
            driver.execute_script("arguments[0].click();", target_link)
            try:
                WebDriverWait(driver, WAIT_TIMEOUT_SECONDS).until(
                    lambda d: current_page_number(d) == target_page
                )
                return True
            except TimeoutException:
                return False

        # If target page link is not yet visible, advance pager window.
        pager_next = driver.find_elements(By.CSS_SELECTOR, "div._n5hmn94")
        if not pager_next:
            break
        driver.execute_script("arguments[0].click();", pager_next[0])
        time.sleep(1)

    return False


def save_csv(schools: list[School], output_path: str) -> None:
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["school name", "type of school", "adress"])
        for s in schools:
            writer.writerow([s.school_name, s.type_of_school, s.adress])


def main() -> None:
    driver = build_driver()
    seen: set[School] = set()

    try:
        driver.get(URL)
        wait_for_cards(driver)
        time.sleep(PAGE_LOAD_SLEEP_SECONDS)
        time.sleep(START_COLLECTION_DELAY_SECONDS)

        page_number = 1
        stagnant_pages = 0
        max_pages = 25
        while len(seen) < TARGET_SCHOOLS and page_number <= max_pages:
            panel = get_scroll_panel(driver)
            if panel is not None:
                loaded_count = scroll_to_load_target(driver, panel, target_cards=SCHOOLS_PER_PAGE)
                print(f"Page {page_number}: visible cards in panel = {loaded_count}")
            before_count = len(seen)
            for school in extract_current_page(driver):
                seen.add(school)

            print(f"Collected: {len(seen)} schools (page {page_number})")
            if len(seen) >= TARGET_SCHOOLS:
                break

            if len(seen) == before_count:
                stagnant_pages += 1
            else:
                stagnant_pages = 0

            if stagnant_pages >= 2:
                print("No new schools found on subsequent pages. Stopping.")
                break

            next_page = page_number + 1
            if not go_to_next_numbered_page(driver, next_page):
                print(f"Unable to navigate to page {next_page}. Stopping.")
                break

            time.sleep(3)
            page_number += 1

        schools_sorted = sorted(seen, key=lambda x: x.school_name.lower())
        save_csv(schools_sorted[:TARGET_SCHOOLS], OUTPUT_CSV)
        print(f"Saved {min(len(schools_sorted), TARGET_SCHOOLS)} rows to {OUTPUT_CSV}")

    finally:
        # Keep browser open for debugging because of detach=True.
        # Close manually when done.
        pass


if __name__ == "__main__":
    main()
