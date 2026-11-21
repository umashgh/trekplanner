"""Indiahikes: batches come from the public booking API that their trek pages call.

GET https://booking.indiahikes.com/tms-service/public-api/v1/trek-overview
    ?trekId=<id>&fetchTrekData=true&fetchAvailableBatchesData=true

Each trek page uses a numeric trekId (Rupin Pass = 2, Goechala = 3, ...).
`discover_trek_ids` scans a range of ids to find them by name.
"""
from dataclasses import dataclass
from datetime import date, datetime

from .http import SourceError, get, session

API = "https://booking.indiahikes.com/tms-service/public-api/v1/trek-overview"
LAST_FEW_SEATS = 9  # Indiahikes shows "LAST n" below roughly this many slots


@dataclass
class ParsedBatch:
    start: date
    end: date
    status: str
    seats_left: int | None
    raw_status: str
    external_id: str


def _d(s):
    return datetime.fromisoformat(s.replace("Z", "")).date()


def map_status(item):
    raw = (item.get("status") or "").upper()
    slots = item.get("availableSlots")
    if "WAIT" in raw or "WL" == raw:
        return "waitlist"
    if "FULL" in raw:
        return "full"
    if "CLOS" in raw or item.get("bookingsClosed"):
        return "closed"
    if isinstance(slots, int) and 0 < slots <= LAST_FEW_SEATS:
        return "last"
    if raw:
        return "open"
    return "unknown"


def parse_overview(payload):
    """Turn a trek-overview JSON payload into (trek_data, [ParsedBatch])."""
    trek = payload.get("trekData") or {}
    out = []
    for item in payload.get("availableBatchesList") or []:
        if item.get("privateBatch"):
            continue
        try:
            start, end = _d(item["startDate"]), _d(item["endDate"])
        except (KeyError, ValueError, AttributeError):
            continue
        out.append(ParsedBatch(
            start=start,
            end=end,
            status=map_status(item),
            seats_left=item.get("availableSlots"),
            raw_status=str(item.get("status") or ""),
            external_id=str(item.get("batchId") or ""),
        ))
    return trek, out


def fetch_batches(trek_id, sess=None):
    sess = sess or session()
    r = get(sess, API, params={
        "trekId": trek_id, "fetchTrekData": "true", "fetchAvailableBatchesData": "true",
    })
    try:
        payload = r.json()
    except ValueError as e:
        raise SourceError(f"Indiahikes trek {trek_id}: response was not JSON") from e
    return parse_overview(payload)


def discover_trek_ids(max_id=200, sess=None):
    """Return {trek name: id} by probing ids 1..max_id."""
    sess = sess or session()
    found = {}
    for i in range(1, max_id + 1):
        try:
            r = sess.get(API, params={"trekId": i, "fetchTrekData": "true",
                                      "fetchAvailableBatchesData": "false"}, timeout=15)
            if r.ok:
                name = (r.json().get("trekData") or {}).get("name")
                if name:
                    found[name] = i
        except Exception:
            continue
    return found
