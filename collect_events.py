#!/usr/bin/env python3
"""
Collect events from approved sources into events.json for Stamp Book SG.

  python collect_events.py --sources sources.json --spots spots.json --out events.json

Source types (set in sources.json, each one can be switched on or off):
  csv          events_manual.csv: events you or an organiser typed in (for example from a Google Form sheet)
  ics          a calendar feed (.ics) that an organiser or venue publishes for others to use
  ticketmaster the Ticketmaster Discovery API (needs a free key in the TM_API_KEY setting)

Only turn on a source once you have checked its terms or have the owner's permission.
This script never scrapes web pages. It keeps only: title, time, venue, short summary, price, link.
No images and no long descriptions are copied. Every listing links back to its source.

What it does with the events:
  - drops events that are over, missing a title, or missing a usable date or time
  - links each event to the nearest stamp-book spot (by spot_id, by coordinates within 250 m, or by venue name)
  - removes duplicates (same title on the same day), keeping the source listed first
  - writes events.json, plus a short report on screen

Needs Python 3.9+. No other packages.
"""
import argparse, csv, datetime as dt, difflib, hashlib, html, json, math, os, re, sys
import urllib.parse, urllib.request
from zoneinfo import ZoneInfo

SGT = ZoneInfo("Asia/Singapore")
CATEGORIES = ["Music", "Festival", "Heritage", "Family", "Food", "Arts", "Sports", "Community", "Other"]
KEYWORDS = [("Music", "concert gig band live music orchestra choir dj"), ("Festival", "festival fair carnival lantern parade fireworks"),
            ("Heritage", "heritage walk tour museum history trail"), ("Family", "family kids children storytelling"),
            ("Food", "food hawker tasting market dining"), ("Arts", "art exhibition theatre film gallery dance"),
            ("Sports", "run race sport yoga fitness cycling"), ("Community", "community volunteer workshop talk")]
STATS = {}

def norm(s): return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()

def clean(s, n=160):
    s = re.sub(r"<[^>]+>", " ", html.unescape(s or "")); s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + "…"

def parse_dt(v, tz=SGT):
    """Turn many date/time spellings into an aware datetime. Returns None if it can't."""
    if isinstance(v, dt.datetime): return v if v.tzinfo else v.replace(tzinfo=tz)
    v = (v or "").strip()
    if not v: return None
    try:
        d = dt.datetime.fromisoformat(v.replace("Z", "+00:00").replace("T", " ") if " " in v else v.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=tz)
    except ValueError: pass
    for f in ("%d/%m/%Y %H:%M", "%d/%m/%Y", "%d %b %Y %H:%M", "%d %B %Y %H:%M", "%Y-%m-%d %H:%M"):
        try: return dt.datetime.strptime(v, f).replace(tzinfo=tz)
        except ValueError: pass
    return None

ALIASES = {"festival": "Festival", "festivals": "Festival", "convention": "Festival", "conventions": "Festival", "expo": "Festival",
           "exhibition": "Arts", "exhibitions": "Arts", "art": "Arts", "arts": "Arts", "theatre": "Arts", "film": "Arts", "art pop ups": "Arts",
           "concert": "Music", "concerts": "Music", "gig": "Music", "gigs": "Music", "music": "Music", "family": "Family", "kids": "Family",
           "food": "Food", "heritage": "Heritage", "culture": "Heritage", "sport": "Sports", "sports": "Sports", "community": "Community"}

def category(e):
    given = norm(e.get("category"))
    for k in CATEGORIES:
        if given == norm(k): return k
    if given in ALIASES: return ALIASES[given]
    words = set(norm((e.get("title", "") + " " + e.get("summary", "")) if not given else given).split())
    for k, kw in KEYWORDS:
        if words & set(kw.split()): return k
    return "Other"

# ---------- readers ----------
def read_csv(src):
    rows = []
    with open(src["path"], newline="", encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("title") or "").strip().lower().startswith("example"): continue
            rows.append(dict(title=r.get("title"), start=parse_dt(r.get("start")), end=parse_dt(r.get("end")), venue=r.get("venue"),
                             spot_id=(r.get("spot_id") or "").strip(), category=r.get("category"), price=r.get("price"), url=r.get("url"),
                             summary=r.get("summary"), source=r.get("source") or src["name"], sponsored=(r.get("sponsored") or "").strip().lower() in ("yes", "y", "true")))
    return rows

def ics_text(v): return v.replace("\\n", " ").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")

def ics_time(val, params):
    val = val.strip()
    if re.fullmatch(r"\d{8}", val): return dt.datetime.strptime(val, "%Y%m%d").replace(tzinfo=SGT)       # all-day
    m = re.fullmatch(r"(\d{8}T\d{6})(Z?)", val)
    if not m: return None
    d = dt.datetime.strptime(m.group(1), "%Y%m%dT%H%M%S")
    if m.group(2): return d.replace(tzinfo=dt.timezone.utc).astimezone(SGT)
    tzid = re.search(r"TZID=([^;:]+)", params or "")
    try: tz = ZoneInfo(tzid.group(1)) if tzid else SGT
    except Exception: tz = SGT
    return d.replace(tzinfo=tz).astimezone(SGT)

def read_ics_text(text, src):
    lines = []
    for ln in text.replace("\r\n", "\n").split("\n"):
        if ln[:1] in (" ", "\t") and lines: lines[-1] += ln[1:]
        else: lines.append(ln)
    out, cur = [], None
    for ln in lines:
        if ln == "BEGIN:VEVENT": cur = {}
        elif ln == "END:VEVENT" and cur is not None:
            if "RRULE" in cur: STATS["recurring skipped"] = STATS.get("recurring skipped", 0) + 1
            else:
                out.append(dict(title=ics_text(cur.get("SUMMARY", ("", ""))[0]), start=ics_time(*cur["DTSTART"]) if "DTSTART" in cur else None,
                                end=ics_time(*cur["DTEND"]) if "DTEND" in cur else None, venue=ics_text(cur.get("LOCATION", ("", ""))[0]),
                                url=cur.get("URL", ("", ""))[0], summary=ics_text(cur.get("DESCRIPTION", ("", ""))[0]), category=cur.get("CATEGORIES", ("", ""))[0],
                                spot_id=src.get("spot_id", ""), source=src["name"], price=src.get("price", "")))
            cur = None
        elif cur is not None and ":" in ln:
            k, v = ln.split(":", 1); name, _, params = k.partition(";"); cur[name.upper()] = (v, params)
    return out

def fetch(url, headers=None):
    req = urllib.request.Request(url, headers=dict({"User-Agent": "stamp-book-sg-events/1.0"}, **(headers or {})))
    with urllib.request.urlopen(req, timeout=30) as r: return r.read().decode("utf-8", "replace")

def read_ics(src):
    return read_ics_text(open(src["file"], encoding="utf-8").read() if src.get("file") else fetch(src["url"]), src)

def read_ticketmaster_json(data, src):
    out = []
    for e in (data.get("_embedded") or {}).get("events", []):
        s = (e.get("dates") or {}).get("start") or {}
        start = parse_dt(s.get("dateTime")) or parse_dt((s.get("localDate") or "") + " " + (s.get("localTime") or "00:00"))
        end = parse_dt(((e.get("dates") or {}).get("end") or {}).get("dateTime"))
        v = ((e.get("_embedded") or {}).get("venues") or [{}])[0]; loc = v.get("location") or {}
        cl = (e.get("classifications") or [{}])[0]; pr = (e.get("priceRanges") or [{}])[0]
        out.append(dict(title=e.get("name"), start=start, end=end, venue=v.get("name"), url=e.get("url"), source="Ticketmaster",
                        category=((cl.get("segment") or {}).get("name") or "").replace("Arts & Theatre", "Arts").replace("Miscellaneous", ""),
                        price=("From " + str(pr.get("currency", "")) + " " + str(pr["min"])) if pr.get("min") is not None else "Ticketed",
                        summary=((cl.get("genre") or {}).get("name") or ""), lat=float(loc["latitude"]) if loc.get("latitude") else None,
                        lng=float(loc["longitude"]) if loc.get("longitude") else None))
    return out

def read_ticketmaster(src):
    if src.get("file"): return read_ticketmaster_json(json.load(open(src["file"], encoding="utf-8")), src)
    key = os.environ.get(src.get("env_key", "TM_API_KEY"))
    if not key: raise RuntimeError("no API key: set " + src.get("env_key", "TM_API_KEY"))
    now = dt.datetime.now(dt.timezone.utc)
    q = {"apikey": key, "countryCode": src.get("country", "SG"), "size": 100, "sort": "date,asc",
         "startDateTime": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "endDateTime": (now + dt.timedelta(days=int(src.get("days", 180)))).strftime("%Y-%m-%dT%H:%M:%SZ")}
    out = []
    for page in range(int(src.get("max_pages", 5))):
        q["page"] = page
        data = json.loads(fetch("https://app.ticketmaster.com/discovery/v2/events.json?" + urllib.parse.urlencode(q)))
        out += read_ticketmaster_json(data, src)
        if page + 1 >= (data.get("page") or {}).get("totalPages", 1): break
    return out

READERS = {"csv": read_csv, "ics": read_ics, "ticketmaster": read_ticketmaster}

# ---------- matching to stamp-book spots ----------
def hav(a, b, c, d):
    p = math.radians
    h = math.sin(p(c - a) / 2) ** 2 + math.cos(p(a)) * math.cos(p(c)) * math.sin(p(d - b) / 2) ** 2
    return 2 * 6371000 * math.asin(math.sqrt(h))

def match_spots(e, spots, ids):
    if e.get("spot_id"):
        got = [s for s in re.split(r"[;,| ]+", e["spot_id"]) if s in ids]
        if got: return got
    if e.get("lat") is not None and e.get("lng") is not None:
        d, s = min((hav(e["lat"], e["lng"], s["lat"], s["lng"]), s["id"]) for s in spots)
        if d <= 250: return [s]
    v = norm(e.get("venue"))
    if len(v) >= 5:
        best = None
        for s in spots:
            n = norm(s["name"]); r = difflib.SequenceMatcher(None, v, n).ratio()
            if (len(n) >= 6 and (n in v or v in n)) or r >= 0.88:
                score = max(r, 0.9 if (n in v or v in n) else 0)
                if not best or score > best[0]: best = (score, s["id"])
        if best: return [best[1]]
    return []

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default="sources.json"); ap.add_argument("--spots", default="spots.json")
    ap.add_argument("--out", default="events.json"); ap.add_argument("--days", type=int, default=180)
    a = ap.parse_args(argv)
    spots = [s for s in json.load(open(a.spots, encoding="utf-8")) if s.get("lat") is not None]; ids = {s["id"] for s in spots}
    now = dt.datetime.now(SGT); horizon = now + dt.timedelta(days=a.days)
    events, seen, report = [], set(), []
    for src in json.load(open(a.sources, encoding="utf-8"))["sources"]:
        if not src.get("enabled"): report.append(f"  skipped (off): {src['name']}"); continue
        try: items = READERS[src["type"]](src)
        except Exception as ex: report.append(f"  FAILED: {src['name']}: {ex}"); continue
        kept = dropped = 0
        for e in items:
            title = clean(e.get("title"), 120); st = e.get("start"); en = e.get("end")
            if not title or not st: dropped += 1; continue
            if (en or st + dt.timedelta(hours=3)) < now or st > horizon: dropped += 1; continue
            key = norm(title) + "|" + st.astimezone(SGT).strftime("%Y-%m-%d")
            if key in seen: dropped += 1; continue
            seen.add(key)
            url = (e.get("url") or "").strip(); url = url if re.match(r"^https?://", url) else ""
            events.append(dict(id=hashlib.sha1(key.encode()).hexdigest()[:10], title=title, start=st.astimezone(SGT).isoformat(timespec="minutes"),
                               **({"end": en.astimezone(SGT).isoformat(timespec="minutes")} if en else {}), category=category(e),
                               price=clean(e.get("price"), 40), venue=clean(e.get("venue"), 80), spots=match_spots(e, spots, ids), url=url,
                               source=clean(e.get("source") or src["name"], 40), summary=clean(e.get("summary")),
                               **({"sponsored": True} if e.get("sponsored") else {})))
            kept += 1
        report.append(f"  {src['name']}: {kept} kept, {dropped} dropped")
    events.sort(key=lambda e: e["start"])
    json.dump({"updated": now.isoformat(timespec="minutes"), "events": events}, open(a.out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("Sources:"); print("\n".join(report))
    print(f"Wrote {len(events)} events to {a.out}; {sum(1 for e in events if e['spots'])} linked to a spot, {sum(1 for e in events if not e['spots'])} not linked")
    long = sum(1 for e in events if e.get('end') and (dt.datetime.fromisoformat(e['end']) - dt.datetime.fromisoformat(e['start'])).days >= 2)
    if long: print(f"Note: {long} events last 2 days or more. They show as 'On now' for the whole span, so split shows with separate nights into one row per night.")
    for k, v in STATS.items(): print(f"Note: {v} {k} (repeating events are not expanded; add each date to the CSV instead)")
    return events

if __name__ == "__main__": main()
