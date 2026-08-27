"""Playwright-based Kayak scraper for flight search."""

import json
import re
import time
import random
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

try:
    from playwright_stealth import Stealth
    _stealth = Stealth()
except ImportError:
    _stealth = None


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
        if _stealth:
            _stealth.apply_stealth_sync(ctx)
        page = ctx.new_page()

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

            for idx, (out_dest, ret_origin, dep_date, ret_date) in enumerate(combos):
                print(f"  [{idx+1}/{total}] {dep_date}/{ret_date} {origin}->{out_dest} / {ret_origin}->{origin}...", end="", flush=True)

                try:
                    result = _search_combo(
                        page, origin, out_dest, ret_origin,
                        dep_date, ret_date, passengers, max_stops,
                        max_layover, currency,
                    )
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
# URL builder
# ---------------------------------------------------------------------------

def _build_url(origin, out_dest, ret_origin, dep_date, ret_date,
               passengers, max_stops, max_layover, currency):
    path = f"/flights/{origin}-{out_dest}/{dep_date}/{ret_origin}-{origin}/{ret_date}"
    params = [f"sort=price_a", f"adults={passengers}"]
    filters = []
    if max_stops is not None:
        filters.append(f"stops=~{max_stops}")
    if max_layover is not None:
        filters.append(f"connDur=-{max_layover}")
    if filters:
        params.append(f"fs={';'.join(filters)}")
    if currency:
        params.append(f"currency={currency}")
    return f"https://www.kayak.com{path}?{'&'.join(params)}"


def kayak_url(config: dict, dest: str = None) -> str:
    """Build a human-friendly Kayak search URL for the report."""
    origin = config["origin"]
    if dest is None:
        dests = config.get("destinations", [config.get("destination", "BKK")])
        dest = dests[0]
    dep = config.get("departure_dates", [config.get("departure_date")])[0]
    ret = config.get("return_dates", [config.get("return_date")])[0]
    passengers = config.get("passengers", 1)
    currency = config.get("currency", "USD")
    return f"https://www.kayak.com/flights/{origin}-{dest}/{dep}/{dest}-{origin}/{ret}?sort=price_a&adults={passengers}&currency={currency}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pause(lo=0.5, hi=1.5):
    time.sleep(random.uniform(lo, hi))


def _max_stops_int(val: str) -> int | None:
    return {"NONSTOP": 0, "ONE_STOP": 1, "TWO_STOPS": 2}.get(val)


def _parse_duration(text: str) -> int:
    hours = minutes = 0
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*m", text)
    if h:
        hours = int(h.group(1))
    if m:
        minutes = int(m.group(1))
    return hours * 60 + minutes


def _parse_time_12h(time_str: str) -> str:
    """Convert '12:55 am' or '6:20 pm' to '00:55' or '18:20'."""
    time_str = time_str.strip().replace(" ", " ")
    m = re.match(r"(\d{1,2}):(\d{2})\s*(am|pm)", time_str, re.I)
    if not m:
        return time_str
    hour, minute, period = int(m.group(1)), m.group(2), m.group(3).lower()
    if period == "am" and hour == 12:
        hour = 0
    elif period == "pm" and hour != 12:
        hour += 12
    return f"{hour:02d}:{minute}"


def _build_datetime(time_24h: str, date_str: str, extra_days: int = 0) -> str:
    try:
        dt = datetime.strptime(f"{date_str} {time_24h}", "%Y-%m-%d %H:%M")
        if extra_days:
            dt += timedelta(days=extra_days)
        return dt.strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Search one combo
# ---------------------------------------------------------------------------

def _search_combo(page, origin, out_dest, ret_origin,
                  dep_date, ret_date, passengers, max_stops,
                  max_layover, currency):
    url = _build_url(origin, out_dest, ret_origin, dep_date, ret_date,
                     passengers, max_stops, max_layover, currency)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except PlaywrightTimeout:
        return None

    _pause(2, 3)

    # Wait for result cards
    try:
        page.wait_for_selector(".nrc6", timeout=30000)
    except PlaywrightTimeout:
        return None

    # Wait for search to complete (Kayak shows "Done" when finished)
    for _ in range(10):
        done = page.locator("text=Done").first
        comparing = page.locator("text=Comparing flight sites")
        if done.is_visible() or comparing.count() == 0:
            break
        _pause(2, 3)

    _pause(1, 2)

    # Extract cards and find cheapest that passes filters
    cards = page.locator(".nrc6")
    count = min(cards.count(), 10)
    if count == 0:
        return None

    for i in range(count):
        card_text = cards.nth(i).evaluate("el => el.innerText")
        result = _parse_card(card_text, out_dest, ret_origin, dep_date, ret_date, currency)
        if not result:
            continue
        if max_stops is not None:
            if result["outbound"]["stops"] > max_stops or result["inbound"]["stops"] > max_stops:
                continue
        if max_layover is not None:
            out_lo = result["outbound"]["layovers"]
            in_lo = result["inbound"]["layovers"]
            if any(lo["duration"] > max_layover for lo in out_lo + in_lo):
                continue
        return result

    return None


# ---------------------------------------------------------------------------
# Card parsing
# ---------------------------------------------------------------------------

def _parse_card(text, out_dest, ret_origin, dep_date, ret_date, currency):
    """Parse a Kayak flight card's innerText into structured data."""
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 10:
        return None

    # Find price (line matching currency pattern)
    price = 0
    actual_currency = currency
    for line in reversed(lines):
        m = re.match(r"[₪$€£]([\d,]+)", line)
        if m:
            price = int(m.group(1).replace(",", ""))
            sym = line[0]
            actual_currency = {"₪": "ILS", "$": "USD", "€": "EUR", "£": "GBP"}.get(sym, currency)
            break
    if not price:
        return None

    # Find time lines (contain am/pm with · separator or standalone)
    time_indices = []
    for i, line in enumerate(lines):
        if re.search(r"\d{1,2}:\d{2}\s*(?:am|pm)", line, re.I):
            time_indices.append(i)

    if len(time_indices) < 2:
        return None

    # Parse two legs starting from each time line
    outbound = _parse_leg(lines, time_indices[0], dep_date)
    inbound = _parse_leg(lines, time_indices[1], ret_date)

    if not outbound or not inbound:
        return None

    return {
        "price": price,
        "currency": actual_currency,
        "outbound_dest": out_dest,
        "return_origin": ret_origin,
        "outbound_airline": outbound["airline"],
        "inbound_airline": inbound["airline"],
        "outbound": _build_segment(outbound),
        "inbound": _build_segment(inbound),
    }


def _parse_leg(lines, start_idx, date_str):
    """Parse one leg from the card lines starting at start_idx."""
    try:
        time_line = lines[start_idx]
        parts = re.split(r"\s*[·–—]\s*", time_line)
        if len(parts) < 2:
            parts = re.findall(r"\d{1,2}:\d{2}\s*(?:am|pm)(?:\s*\+\d+)?", time_line, re.I)
        if len(parts) < 2:
            return None

        dep_raw = parts[0].strip()
        arr_raw = parts[1].strip()

        extra_days = 0
        day_match = re.search(r"\+(\d+)", arr_raw)
        if day_match:
            extra_days = int(day_match.group(1))
            arr_raw = re.sub(r"\s*\+\d+", "", arr_raw)

        dep_24 = _parse_time_12h(dep_raw)
        arr_24 = _parse_time_12h(arr_raw)

        dep_dt = _build_datetime(dep_24, date_str)
        arr_dt = _build_datetime(arr_24, date_str, extra_days)

        # Parse remaining lines for this leg
        i = start_idx + 1
        airline = ""
        stops = 0
        layovers = []
        duration = 0
        dep_airport = ""
        arr_airport = ""

        while i < len(lines):
            line = lines[i]

            # Airline line: not a time, not a price, not a code, not a duration
            if not airline and not re.match(r"^\d", line) and not re.match(r"^[A-Z]{3}$", line) and not re.match(r"^[₪$€£]", line) and not re.search(r"stop", line, re.I) and not re.search(r"nonstop", line, re.I) and not re.search(r"\d+h\s*\d+m", line) and not re.search(r"layover", line, re.I) and line != "-" and len(line) > 3:
                airline = line
                i += 1
                continue

            # Stops line
            if re.search(r"(\d+)\s*stop", line, re.I):
                stops = int(re.search(r"(\d+)", line).group(1))
                i += 1
                continue
            if re.search(r"nonstop", line, re.I):
                stops = 0
                i += 1
                continue

            # Layover airport code (3-letter code after stops, before duration)
            if re.match(r"^[A-Z]{3}$", line) and stops > 0 and duration == 0 and len(layovers) < stops:
                lo_airport = line
                i += 1
                # Next line should be layover duration
                if i < len(lines) and re.search(r"layover", lines[i], re.I):
                    lo_dur_match = re.search(r"(\d+h\s*\d+m)", lines[i])
                    lo_dur = _parse_duration(lo_dur_match.group(1)) if lo_dur_match else 0
                    layovers.append({"airport": lo_airport, "duration": lo_dur})
                    i += 1
                continue

            # Duration line
            dur_match = re.match(r"^(\d+h\s*\d+m)$", line)
            if dur_match:
                duration = _parse_duration(dur_match.group(1))
                i += 1
                continue

            # Departure airport (3-letter code after duration)
            if re.match(r"^[A-Z]{3}$", line) and duration > 0 and not dep_airport:
                dep_airport = line
                i += 1
                continue

            # Separator
            if line == "-":
                i += 1
                continue

            # Arrival airport (3-letter code after separator)
            if re.match(r"^[A-Z]{3}$", line) and dep_airport and not arr_airport:
                arr_airport = line
                i += 1
                break

            # Next time line means we've gone too far
            if re.search(r"\d{1,2}:\d{2}\s*(?:am|pm)", line, re.I):
                break

            i += 1

        return {
            "dep_time": dep_dt,
            "arr_time": arr_dt,
            "airline": airline,
            "stops": stops,
            "layovers": layovers,
            "duration": duration,
            "dep_airport": dep_airport,
            "arr_airport": arr_airport,
        }
    except Exception:
        return None


def _build_segment(leg: dict) -> dict:
    return {
        "legs": [
            {
                "departure_airport": leg["dep_airport"],
                "arrival_airport": leg["arr_airport"],
                "departure_time": leg["dep_time"],
                "arrival_time": leg["arr_time"],
                "duration": leg["duration"],
                "airline_code": leg["airline"],
                "flight_number": "",
            }
        ],
        "duration": leg["duration"],
        "stops": leg["stops"],
        "layovers": leg["layovers"],
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
