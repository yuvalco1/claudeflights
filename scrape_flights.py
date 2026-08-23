"""Playwright-based Google Flights scraper using multi-city search."""

import base64
import json
import re
import sys
import time
import random
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout


AIRPORT_ENTITIES = {
    "TLV": "/m/07qzv",
}


def scrape(config: dict) -> list[dict]:
    origin = config["origin"]
    destinations = config.get("destinations", [config.get("destination", "BKK")])
    dep_dates = config.get("departure_dates", [config.get("departure_date")])
    ret_dates = config.get("return_dates", [config.get("return_date")])
    passengers = config.get("passengers", 1)
    max_stops = _max_stops_int(config.get("max_stops", "ANY"))
    max_layover = config.get("max_layover_minutes")
    currency = config.get("currency", "USD")
    top_n = config.get("top_n", 3)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = browser.new_context(
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        try:
            combos = [
                (od, ro, dd, rd)
                for dd in dep_dates
                for rd in ret_dates
                for od in destinations
                for ro in destinations
            ]
            total = len(combos)
            all_results = []
            consent_dismissed = False

            for idx, (out_dest, ret_origin, dep_date, ret_date) in enumerate(combos):
                dep_year = int(dep_date[:4])
                ret_year = int(ret_date[:4])
                print(f"  [{idx+1}/{total}] {dep_date}/{ret_date} {origin}->{out_dest} / {ret_origin}->{origin}...", end="", flush=True)

                try:
                    result = _search_combo(
                        page, origin, out_dest, ret_origin,
                        dep_date, ret_date, passengers, max_stops,
                        max_layover, currency, dep_year, ret_year,
                        dismiss_consent=not consent_dismissed,
                    )
                    consent_dismissed = True
                except Exception as e:
                    print(f" error: {e}")
                    _pause(10, 20)
                    continue

                if result:
                    result["departure_date"] = dep_date
                    result["return_date"] = ret_date
                    all_results.append(result)
                    print(f" ${result['price']} ({result['outbound_airline']}/{result['inbound_airline']})")
                else:
                    print(" no results")

                if idx < total - 1:
                    _pause(5, 12)

            all_results.sort(key=lambda r: r["price"])
            return all_results[:top_n]
        finally:
            browser.close()


# ---------------------------------------------------------------------------
# Protobuf URL builder — constructs tfs parameter for Google Flights
# ---------------------------------------------------------------------------

def _varint(value):
    result = bytearray()
    while value > 0x7F:
        result.append((value & 0x7F) | 0x80)
        value >>= 7
    result.append(value & 0x7F)
    return bytes(result)


def _pb_tag(field, wire_type):
    return _varint((field << 3) | wire_type)


def _pb_varint(field, value):
    return _pb_tag(field, 0) + _varint(value)


def _pb_bytes(field, data):
    return _pb_tag(field, 2) + _varint(len(data)) + data


def _pb_string(field, s):
    return _pb_bytes(field, s.encode("utf-8"))


def _encode_airport(field_num, iata):
    entity = AIRPORT_ENTITIES.get(iata)
    if entity:
        inner = _pb_varint(1, 2) + _pb_string(2, entity)
    else:
        inner = _pb_varint(1, 1) + _pb_string(2, iata)
    return _pb_bytes(field_num, inner)


def _build_tfs(dep_date, ret_date, origin, out_dest, ret_origin, passengers=1):
    tfs = b""
    tfs += _pb_varint(1, 28)
    tfs += _pb_varint(2, 2)  # multi-city
    seg1 = _pb_string(2, dep_date) + _encode_airport(13, origin) + _encode_airport(14, out_dest)
    tfs += _pb_bytes(3, seg1)
    seg2 = _pb_string(2, ret_date) + _encode_airport(13, ret_origin) + _encode_airport(14, origin)
    tfs += _pb_bytes(3, seg2)
    tfs += _pb_varint(8, 1)
    tfs += _pb_varint(9, 1)
    tfs += _pb_varint(14, 1)
    pax_inner = _pb_varint(1, 0xFFFFFFFFFFFFFFFF)
    tfs += _pb_bytes(16, pax_inner)
    tfs += _pb_varint(19, passengers + 1)
    return base64.urlsafe_b64encode(tfs).rstrip(b"=").decode()


def _build_search_url(dep_date, ret_date, origin, out_dest, ret_origin, passengers=1, currency="USD"):
    tfs = _build_tfs(dep_date, ret_date, origin, out_dest, ret_origin, passengers)
    return f"https://www.google.com/travel/flights/search?tfs={tfs}&tfu=KgIIAw&hl=en&curr={currency}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pause(lo=0.5, hi=1.5):
    time.sleep(random.uniform(lo, hi))


def _parse_duration(text: str) -> int:
    hours = minutes = 0
    h = re.search(r"(\d+)\s*hr", text)
    m = re.search(r"(\d+)\s*min", text)
    if h:
        hours = int(h.group(1))
    if m:
        minutes = int(m.group(1))
    return hours * 60 + minutes


def _build_datetime(time_str: str, date_str: str, year: int) -> str:
    try:
        full = f"{date_str}, {year} {time_str}"
        dt = datetime.strptime(full, "%B %d, %Y %I:%M %p")
        return dt.strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return ""


def _max_stops_int(val: str) -> int | None:
    return {"NONSTOP": 0, "ONE_STOP": 1, "TWO_STOPS": 2}.get(val)


def _dismiss_consent(page):
    for label in ("Reject all", "Accept all"):
        try:
            btn = page.locator(f'button:has-text("{label}")').first
            if btn.is_visible(timeout=2000):
                btn.click()
                _pause(0.5, 1)
                return
        except Exception:
            continue


def _set_passengers(page, count: int):
    pax_btn = page.locator("button").filter(
        has_text=re.compile(r"\d+\s*(?:passenger|adult)")
    )
    if pax_btn.count() == 0:
        return False
    pax_btn.first.click()
    _pause(0.3, 0.5)
    add_btn = page.get_by_label("Add adult")
    for _ in range(count - 1):
        if add_btn.count() > 0:
            add_btn.first.click()
            _pause(0.15, 0.25)
    done = page.locator("button").filter(has_text="Done")
    if done.count() > 0:
        done.first.click()
    _pause(0.3, 0.5)
    return True


def _wait_for_results(page, timeout=15000):
    for attempt in range(3):
        try:
            page.wait_for_selector("li.pIav2d", timeout=timeout)
            return True
        except PlaywrightTimeout:
            oops = page.locator("text=Oops, something went wrong")
            reload_btn = page.locator("button:has-text('Reload')")
            if oops.count() > 0 and reload_btn.count() > 0:
                reload_btn.first.click()
                _pause(2, 3)
            else:
                page.reload()
                _pause(2, 3)
    return False


# ---------------------------------------------------------------------------
# Result extraction
# ---------------------------------------------------------------------------

_JS_EXTRACT = """() => {
    const cards = document.querySelectorAll('li.pIav2d');
    return Array.from(cards).map(card => {
        const link = card.querySelector('.JMc5Xc');
        const priceEl = card.querySelector('.U3gSDe');
        const airports = card.querySelectorAll('.QylvBf');
        const layoverEls = card.querySelectorAll('[aria-label^="Layover"]');
        const layoverCodes = Array.from(layoverEls).map(el => {
            for (const ch of el.querySelectorAll('*')) {
                const t = ch.textContent.trim();
                if (t.length === 3 && /^[A-Z]{3}$/.test(t)) return t;
            }
            return '';
        });
        const rawDep = airports[0]?.textContent?.trim() || '';
        const rawArr = airports[1]?.textContent?.trim() || '';
        return {
            ariaLabel: link ? link.getAttribute('aria-label') || '' : '',
            priceText: priceEl ? priceEl.textContent || '' : '',
            depCode: (rawDep.match(/^([A-Z]{3})/) || [])[1] || rawDep,
            arrCode: (rawArr.match(/^([A-Z]{3})/) || [])[1] || rawArr,
            layoverCodes,
        };
    });
}"""


def _extract_flights(page, max_stops, year, max_layover=None):
    raw = page.evaluate(_JS_EXTRACT)
    flights = []
    for item in raw:
        parsed = _parse_card(item, year)
        if parsed is None:
            continue
        if max_stops is not None and parsed["stops"] > max_stops:
            continue
        if max_layover is not None and parsed["layovers"]:
            if any(lo["duration"] > max_layover for lo in parsed["layovers"]):
                continue
        flights.append(parsed)
    flights.sort(key=lambda f: f["total_price"])
    return flights


def _parse_card(item: dict, year: int) -> dict | None:
    label = item.get("ariaLabel", "")
    if not label:
        return None

    pm = re.search(r"From ([\d,]+)", label)
    if not pm:
        pm = re.search(r"([\d,]+)", item.get("priceText", ""))
    if not pm:
        return None
    total_price = int(pm.group(1).replace(",", ""))

    stops = 0
    sm = re.search(r"(\d+) stops? flight", label)
    if sm:
        stops = int(sm.group(1))

    airline = ""
    am = re.search(r"flight with (.+?)\.\s*(?:Leaves|Select)", label)
    if am:
        airline = am.group(1)

    dep_dt = ""
    dm = re.search(
        r"Leaves .+? at (\d+:\d+ [AP]M) on \w+day, (\w+ \d+)", label
    )
    if dm:
        dep_dt = _build_datetime(dm.group(1), dm.group(2), year)

    arr_dt = ""
    arm = re.search(
        r"arrives at .+? at (\d+:\d+ [AP]M) on \w+day, (\w+ \d+)", label
    )
    if arm:
        arr_dt = _build_datetime(arm.group(1), arm.group(2), year)

    duration = 0
    drm = re.search(r"Total duration (.+?)\.", label)
    if drm:
        duration = _parse_duration(drm.group(1))

    lo_codes = item.get("layoverCodes", [])
    layovers = []
    for i, lm in enumerate(
        re.finditer(r"Layover \(\d+ of \d+\) is a (.+?) layover at", label)
    ):
        lo_dur = _parse_duration(lm.group(1))
        code = lo_codes[i] if i < len(lo_codes) else ""
        layovers.append({"airport": code, "duration": lo_dur})

    return {
        "total_price": total_price,
        "stops": stops,
        "airline": airline,
        "dep_code": item.get("depCode", ""),
        "arr_code": item.get("arrCode", ""),
        "dep_datetime": dep_dt,
        "arr_datetime": arr_dt,
        "duration": duration,
        "layovers": layovers,
    }


def _build_segment(flight: dict) -> dict:
    return {
        "legs": [
            {
                "departure_airport": flight["dep_code"],
                "arrival_airport": flight["arr_code"],
                "departure_time": flight["dep_datetime"],
                "arrival_time": flight["arr_datetime"],
                "duration": flight["duration"],
                "airline_code": flight["airline"],
                "flight_number": "",
            }
        ],
        "duration": flight["duration"],
        "stops": flight["stops"],
        "layovers": flight["layovers"],
    }


# ---------------------------------------------------------------------------
# Search one destination combination
# ---------------------------------------------------------------------------

def _search_combo(page, origin, out_dest, ret_origin,
                  dep_date, ret_date, passengers, max_stops,
                  max_layover, currency, dep_year, ret_year,
                  dismiss_consent=True):
    url = _build_search_url(dep_date, ret_date, origin, out_dest, ret_origin, passengers, currency)
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
    except PlaywrightTimeout:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=10000)
        except Exception:
            return None
    _pause(2, 3)

    if dismiss_consent:
        _dismiss_consent(page)

    if not _wait_for_results(page):
        return None

    if passengers > 1:
        if _set_passengers(page, passengers):
            page.wait_for_load_state("networkidle", timeout=15000)
            _pause(2, 3)
            _wait_for_results(page)

    _pause(1, 2)

    outbound_flights = _extract_flights(page, max_stops, dep_year, max_layover)
    if not outbound_flights:
        return None

    cards = page.locator("li.pIav2d")
    if cards.count() == 0:
        return None

    cards.first.click(force=True)
    _pause(3, 4)

    try:
        page.wait_for_selector("li.pIav2d", timeout=15000)
    except PlaywrightTimeout:
        return None

    return_flights = _extract_flights(page, max_stops, ret_year, max_layover)
    if not return_flights:
        return None

    out = outbound_flights[0]
    ret = return_flights[0]

    return {
        "price": ret["total_price"],
        "currency": currency,
        "outbound_dest": out_dest,
        "return_origin": ret_origin,
        "outbound_airline": out["airline"],
        "inbound_airline": ret["airline"],
        "outbound": _build_segment(out),
        "inbound": _build_segment(ret),
    }


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cfg_path = Path(__file__).parent / "config.json"
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)

    dests = cfg.get("destinations", [cfg.get("destination", "BKK")])
    dep_dates = cfg.get("departure_dates", [cfg.get("departure_date")])
    ret_dates = cfg.get("return_dates", [cfg.get("return_date")])
    n_combos = len(dests) ** 2 * len(dep_dates) * len(ret_dates)
    print(f"Scraping flights: {cfg['origin']}->{','.join(dests)} "
          f"dep={','.join(dep_dates)} ret={','.join(ret_dates)} "
          f"({n_combos} combos)...")

    flights = scrape(cfg)
    if flights:
        print(f"\nTop {len(flights)} results:")
        print(json.dumps(flights, indent=2))
    else:
        print("No flights found.")
