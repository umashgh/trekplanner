"""Trek The Himalayas: departures are embedded as JSON in the trek page.

The page is a Next.js app; its server payload is pushed as
`self.__next_f.push([1,"..."])` string chunks. Inside them each departure is an
object like {"departureId":9152,"startDate":"2027-05-08T00:00:00",...,
"remainiBooking":25,"autoStatus":"Open","mannualStatus":"Default",...}.
"""
import json
import re
from datetime import datetime

from .http import SourceError, browser_client
from .indiahikes import ParsedBatch

PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', re.S)
LAST_FEW_SEATS = 12  # TTH shows "n Seats Left" around this level


def _payload_text(html):
    chunks = []
    for m in PUSH_RE.finditer(html):
        try:
            chunks.append(json.loads('"' + m.group(1) + '"'))
        except ValueError:
            continue
    return "".join(chunks)


def _d(s):
    return datetime.fromisoformat(s.replace("Z", "")).date()


def map_status(dep):
    manual = (dep.get("mannualStatus") or "").strip()
    raw = manual if manual and manual.lower() != "default" else (dep.get("autoStatus") or "")
    low = raw.lower()
    remaining = dep.get("remainiBooking")
    if "full" in low or "sold" in low:
        return "full", raw
    if "wait" in low:
        return "waitlist", raw
    if "clos" in low:
        return "closed", raw
    if dep.get("isFillingFast") or (isinstance(remaining, int) and 0 < remaining <= LAST_FEW_SEATS):
        return "last", raw
    if "open" in low:
        return "open", raw
    return "unknown", raw


def parse_page(html):
    text = _payload_text(html)
    if not text:
        raise SourceError("TTH page had no embedded data; the page layout may have changed.")
    decoder = json.JSONDecoder()
    seen, out = set(), []
    for m in re.finditer(r'\{"departureId":', text):
        try:
            dep, _ = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        if dep.get("isDeleted") or not dep.get("isApproved", True):
            continue
        key = dep.get("departureId")
        if key in seen:
            continue
        seen.add(key)
        try:
            start, end = _d(dep["startDate"]), _d(dep["endDate"])
        except (KeyError, ValueError, AttributeError):
            continue
        status, raw = map_status(dep)
        rem = dep.get("remainiBooking")
        out.append(ParsedBatch(
            start=start, end=end, status=status,
            seats_left=rem if isinstance(rem, int) else None,
            raw_status=raw, external_id=str(key),
        ))
    return sorted(out, key=lambda b: b.start)


def fetch_batches(page_url, sess=None):
    client = browser_client()
    try:
        r = client.get(page_url)
    except Exception as e:
        raise SourceError(f"Could not reach {page_url}: {e}") from e
    if r.status_code != 200:
        raise SourceError(f"{page_url} returned HTTP {r.status_code}")
    return parse_page(r.text)
