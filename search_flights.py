import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
RESULTS_HTML = SCRIPT_DIR / "results.html"
RESULTS_JSON = SCRIPT_DIR / "results.json"
LOG_PATH = SCRIPT_DIR / "search.log"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def airline_code(airline) -> str:
    return getattr(airline, "name", str(airline)).lstrip("_")


def airport_code(airport) -> str:
    name = getattr(airport, "name", str(airport))
    return name.replace("Airport.", "").lstrip("_")


def serialize_leg(leg) -> dict:
    return {
        "departure_airport": airport_code(leg.departure_airport),
        "arrival_airport": airport_code(leg.arrival_airport),
        "departure_time": str(leg.departure_datetime),
        "arrival_time": str(leg.arrival_datetime),
        "duration": leg.duration,
        "airline_code": airline_code(leg.airline),
        "flight_number": leg.flight_number,
    }


def serialize_layover(lo) -> dict:
    out = {"airport": airline_code(lo.airport), "duration": lo.duration}
    if lo.overnight:
        out["overnight"] = True
    return out


def serialize_flight(flight_tuple) -> dict | None:
    outbound, inbound = flight_tuple
    price = outbound.price
    if price is None:
        return None
    currency = outbound.currency or "USD"

    out_layovers = getattr(outbound, "layovers", None)
    in_layovers = getattr(inbound, "layovers", None)

    return {
        "price": price,
        "currency": currency,
        "outbound_airline": getattr(outbound, "primary_airline_name", None) or "",
        "inbound_airline": getattr(inbound, "primary_airline_name", None) or "",
        "outbound": {
            "legs": [serialize_leg(leg) for leg in outbound.legs],
            "duration": outbound.duration,
            "stops": outbound.stops,
            "layovers": [serialize_layover(lo) for lo in out_layovers] if out_layovers else [],
        },
        "inbound": {
            "legs": [serialize_leg(leg) for leg in inbound.legs],
            "duration": inbound.duration,
            "stops": inbound.stops,
            "layovers": [serialize_layover(lo) for lo in in_layovers] if in_layovers else [],
        },
    }


def google_flights_url(config: dict) -> str:
    q = f"flights from {config['origin']} to {config['destination']} on {config['departure_date']} return {config['return_date']}"
    return f"https://www.google.com/travel/flights?q={urllib.parse.quote(q)}&curr={config.get('currency', 'USD')}"


def format_duration(minutes: int) -> str:
    h, m = divmod(minutes, 60)
    return f"{h}h {m}m"


def format_datetime(dt_str: str) -> tuple[str, str]:
    dt = datetime.fromisoformat(dt_str)
    return dt.strftime("%a %b %d"), dt.strftime("%H:%M")


def build_leg_html(leg: dict) -> str:
    _, dep_time = format_datetime(leg["departure_time"])
    _, arr_time = format_datetime(leg["arrival_time"])
    return (
        f'<div class="leg">'
        f'<span class="airline">{leg["airline_code"]} {leg["flight_number"]}</span>'
        f'<span class="times">{dep_time} &rarr; {arr_time}</span>'
        f'<span class="duration">{format_duration(leg["duration"])}</span>'
        f"</div>"
    )


def build_segment_html(label: str, segment: dict, airline_name: str) -> str:
    legs_html = "\n".join(build_leg_html(leg) for leg in segment["legs"])
    first_dep_date, first_dep_time = format_datetime(segment["legs"][0]["departure_time"])
    _, last_arr_time = format_datetime(segment["legs"][-1]["arrival_time"])
    dep_airport = segment["legs"][0]["departure_airport"]
    arr_airport = segment["legs"][-1]["arrival_airport"]

    layover_html = ""
    if segment["layovers"]:
        parts = []
        for lo in segment["layovers"]:
            txt = f'{lo["airport"]} ({format_duration(lo["duration"])})'
            if lo.get("overnight"):
                txt += " overnight"
            parts.append(txt)
        layover_html = f'<div class="layover">Layover: {", ".join(parts)}</div>'

    stops_text = "Direct" if segment["stops"] == 0 else f'{segment["stops"]} stop{"s" if segment["stops"] > 1 else ""}'
    airline_badge = f'<span class="airline-name">{airline_name}</span>' if airline_name else ""

    return f"""
    <div class="segment">
      <div class="segment-header">
        <span class="label">{label}</span>
        {airline_badge}
        <span class="route">{dep_airport} &rarr; {arr_airport}</span>
        <span class="date">{first_dep_date}</span>
      </div>
      <div class="segment-summary">
        <span>{first_dep_time} &rarr; {last_arr_time}</span>
        <span class="total-duration">{format_duration(segment["duration"])}</span>
        <span class="stops">{stops_text}</span>
      </div>
      <div class="legs">{legs_html}</div>
      {layover_html}
    </div>"""


def build_html(flights: list[dict], config: dict, search_time: str) -> str:
    gf_url = google_flights_url(config)
    cards = []
    for i, flight in enumerate(flights, 1):
        outbound_html = build_segment_html("Outbound", flight["outbound"], flight["outbound_airline"])
        inbound_html = build_segment_html("Return", flight["inbound"], flight["inbound_airline"])
        cards.append(f"""
    <div class="card">
      <div class="card-header">
        <span class="rank">#{i}</span>
        <span class="price">{flight["currency"]} {flight["price"]:,.0f}</span>
        <span class="price-note">round-trip for {config["passengers"]} adults</span>
      </div>
      {outbound_html}
      {inbound_html}
    </div>""")

    cards_html = "\n".join(cards)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TLV-BKK Flight Search</title>
<style>
  :root {{
    --bg: #ffffff; --fg: #1a1a1a; --card-bg: #f8f9fa; --card-border: #e0e0e0;
    --accent: #2563eb; --muted: #6b7280; --green: #059669;
    --segment-bg: #f0f4ff; --layover-bg: #fef3c7;
  }}
  @media (prefers-color-scheme: dark) {{
    :root {{
      --bg: #0f172a; --fg: #e2e8f0; --card-bg: #1e293b; --card-border: #334155;
      --accent: #60a5fa; --muted: #94a3b8; --green: #34d399;
      --segment-bg: #1e2a4a; --layover-bg: #422006;
    }}
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: var(--bg); color: var(--fg); padding: 1.5rem; max-width: 800px; margin: 0 auto; }}
  .header {{ margin-bottom: 1.5rem; }}
  .header h1 {{ font-size: 1.5rem; margin-bottom: 0.25rem; }}
  .header .meta {{ color: var(--muted); font-size: 0.85rem; }}
  .header .gf-link {{ display: inline-block; margin-top: 0.5rem; font-size: 0.85rem;
    color: var(--accent); text-decoration: none; }}
  .header .gf-link:hover {{ text-decoration: underline; }}
  .card {{ background: var(--card-bg); border: 1px solid var(--card-border);
    border-radius: 12px; padding: 1.25rem; margin-bottom: 1rem; }}
  .card-header {{ display: flex; align-items: baseline; gap: 0.75rem;
    margin-bottom: 1rem; flex-wrap: wrap; }}
  .rank {{ font-weight: 700; color: var(--accent); font-size: 1.1rem; }}
  .price {{ font-size: 1.5rem; font-weight: 700; color: var(--green); }}
  .price-note {{ color: var(--muted); font-size: 0.8rem; }}
  .segment {{ background: var(--segment-bg); border-radius: 8px; padding: 0.75rem 1rem; margin-bottom: 0.5rem; }}
  .segment-header {{ display: flex; gap: 0.75rem; align-items: center; margin-bottom: 0.4rem; flex-wrap: wrap; }}
  .label {{ font-weight: 700; font-size: 0.8rem; text-transform: uppercase;
    color: var(--accent); background: var(--bg); padding: 0.15rem 0.5rem;
    border-radius: 4px; }}
  .airline-name {{ font-weight: 600; font-size: 0.9rem; }}
  .route {{ font-weight: 600; }}
  .date {{ color: var(--muted); font-size: 0.85rem; }}
  .segment-summary {{ display: flex; gap: 1rem; font-size: 0.95rem; margin-bottom: 0.4rem; flex-wrap: wrap; }}
  .total-duration {{ font-weight: 600; }}
  .stops {{ color: var(--muted); }}
  .legs {{ font-size: 0.85rem; color: var(--muted); }}
  .leg {{ display: flex; gap: 0.75rem; padding: 0.15rem 0; }}
  .airline {{ font-weight: 600; min-width: 80px; }}
  .layover {{ font-size: 0.8rem; color: var(--fg); background: var(--layover-bg);
    padding: 0.3rem 0.6rem; border-radius: 4px; margin-top: 0.4rem; display: inline-block; }}
</style>
</head>
<body>
  <div class="header">
    <h1>TLV &rarr; BKK Cheapest Flights</h1>
    <div class="meta">
      {config["departure_date"]} &rarr; {config["return_date"]} &middot;
      {config["passengers"]} adults &middot; max {config["max_stops"].replace("_", " ").lower()} &middot;
      Last updated: {search_time}
    </div>
    <a class="gf-link" href="{gf_url}" target="_blank">View on Google Flights &rarr;</a>
  </div>
  {cards_html}
</body>
</html>"""


def send_telegram(config: dict, message: str):
    bot_token = config.get("telegram_bot_token")
    chat_id = config.get("telegram_chat_id")
    if not bot_token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    try:
        req = urllib.request.Request(url, data=data)
        urllib.request.urlopen(req, timeout=10)
        log("Telegram notification sent.")
    except Exception as e:
        log(f"Telegram send failed: {e}")


def build_telegram_message(flights: list[dict], config: dict, gf_url: str) -> str:
    lines = [f"<b>TLV -> BKK Flight Update</b>", f"{config['departure_date']} to {config['return_date']}", ""]
    for i, fl in enumerate(flights, 1):
        out_airline = fl["outbound_airline"]
        in_airline = fl["inbound_airline"]
        out_legs = fl["outbound"]["legs"]
        in_legs = fl["inbound"]["legs"]
        out_via = ""
        if fl["outbound"]["layovers"]:
            out_via = f" via {fl['outbound']['layovers'][0]['airport']}"
        in_via = ""
        if fl["inbound"]["layovers"]:
            in_via = f" via {fl['inbound']['layovers'][0]['airport']}"

        dep_dt = datetime.fromisoformat(out_legs[0]["departure_time"])
        arr_dt = datetime.fromisoformat(out_legs[-1]["arrival_time"])
        ret_dep_dt = datetime.fromisoformat(in_legs[0]["departure_time"])
        ret_arr_dt = datetime.fromisoformat(in_legs[-1]["arrival_time"])

        lines.append(f"<b>#{i} - {fl['currency']} {fl['price']:,.0f}</b>")
        lines.append(f"  Out: {out_airline} {dep_dt:%H:%M}-{arr_dt:%H:%M}{out_via} ({format_duration(fl['outbound']['duration'])})")
        lines.append(f"  Ret: {in_airline} {ret_dep_dt:%H:%M}-{ret_arr_dt:%H:%M}{in_via} ({format_duration(fl['inbound']['duration'])})")
        lines.append("")
    lines.append(f'<a href="{gf_url}">View on Google Flights</a>')
    return "\n".join(lines)


def search():
    from fli.core import build_flight_segments, parse_currency, parse_max_stops, parse_sort_by, resolve_airport
    from fli.models import FlightSearchFilters, PassengerInfo
    from fli.search import SearchFlights

    config = load_config()
    log(f"Searching {config['origin']}->{config['destination']} "
        f"{config['departure_date']} to {config['return_date']} "
        f"({config['passengers']} pax, {config['max_stops']})")

    origins = [resolve_airport(config["origin"])]
    destinations = [resolve_airport(config["destination"])]

    segments, trip_type = build_flight_segments(
        origin=origins,
        destination=destinations,
        departure_date=config["departure_date"],
        return_date=config["return_date"],
    )

    max_stops = parse_max_stops(config["max_stops"])
    sort_by = parse_sort_by("CHEAPEST")
    currency = parse_currency(config.get("currency", "USD"))

    filters = FlightSearchFilters(
        trip_type=trip_type,
        passenger_info=PassengerInfo(adults=config["passengers"]),
        flight_segments=segments,
        stops=max_stops,
        sort_by=sort_by,
    )

    results = None
    for attempt in range(3):
        try:
            client = SearchFlights()
            results = client.search(filters, currency=currency, language="en", country="IL")
            if results:
                break
        except Exception as e:
            log(f"Attempt {attempt + 1} failed: {e}")
        if attempt < 2:
            time.sleep(10 * (attempt + 1))

    if not results:
        log("No flights found (Google may be rate-limiting).")
        send_telegram(config, "TLV->BKK search: no results (Google rate limit). Will retry in 4h.")
        return

    top_n = config.get("top_n", 3)
    serialized = []
    for flight in results:
        if not isinstance(flight, tuple) or len(flight) != 2:
            continue
        s = serialize_flight(flight)
        if s:
            serialized.append(s)
        if len(serialized) >= top_n:
            break

    if not serialized:
        log("All results had no price. Skipping.")
        return

    search_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    output = {
        "search_time": search_time,
        "config": config,
        "flights": serialized,
    }
    with open(RESULTS_JSON, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, default=str)

    html = build_html(serialized, config, search_time)
    with open(RESULTS_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    summary_parts = [f"#{i+1}: {fl['currency']} {fl['price']:,.0f} ({fl['outbound_airline']})" for i, fl in enumerate(serialized)]
    summary = f"TLV->BKK {config['departure_date']}: {' | '.join(summary_parts)}"
    log(f"Done. {summary}")
    print(f"\n{summary}")

    gf_url = google_flights_url(config)
    telegram_msg = build_telegram_message(serialized, config, gf_url)
    send_telegram(config, telegram_msg)


def main():
    try:
        search()
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
