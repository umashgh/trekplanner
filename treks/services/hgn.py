"""Himalayan Guides Nepal: fixed departures from a single HTML table page.

GET https://www.himalayanguides.com/fixed-departures

The page has one or more <table> elements (one per season) with rows like:
    <tr>
      <td>Everest Base Camp Trek</td>
      <td>
        <a href="/fixed-departures/3">September 25 - October 10</a>
        <a href="/fixed-departures/9">October 7 - October 23</a>
      </td>
    </tr>

Since all treks live on one page, fetch_page() downloads it once and
parse_page() returns *all* treks.  fetch_batches() filters to a single trek
by the name stored in OperatorTrek.external_id.
"""
import re
from datetime import date, datetime

from .http import SourceError, get, session
from .indiahikes import ParsedBatch

PAGE_URL = "https://www.himalayanguides.com/fixed-departures"

_ROW_RE = re.compile(
    r"<tr>\s*<td>\s*(.*?)\s*</td>\s*<td>(.*?)</td>",
    re.S,
)
_LINK_RE = re.compile(
    r'<a\s+href="[^"]*?/fixed-departures/(\d+)"[^>]*>\s*(.*?)\s*</a>',
    re.S,
)
# "October 15 - October 30" or "04 April - 31 May"
_DATE_RE = re.compile(
    r"(\d{1,2}\s+\w+|\w+\s+\d{1,2})\s*[-–]\s*(\d{1,2}\s+\w+|\w+\s+\d{1,2})",
)
_SEASON_RE = re.compile(r"<h4>[^<]*?(\d{4})\s*(Autumn|Spring)[^<]*</h4>", re.I)


def _parse_date(text, year):
    """Parse 'October 15' or '04 April' into a date for the given year."""
    text = " ".join(text.split())
    for fmt in ("%B %d", "%d %B", "%b %d", "%d %b"):
        try:
            return datetime.strptime(text, fmt).replace(year=year).date()
        except ValueError:
            continue
    return None


def _guess_year(raw_date_text, seasons):
    """Pick the year from the season headers that best fits a date string."""
    if not seasons:
        return date.today().year
    return seasons[-1]


def parse_page(html):
    """Return {normalised_trek_name: [ParsedBatch, ...]} for every trek on the page."""
    seasons = []
    for m in _SEASON_RE.finditer(html):
        seasons.append(int(m.group(1)))

    result = {}
    current_year = seasons[0] if seasons else date.today().year
    season_idx = 0
    season_positions = [(m.start(), int(m.group(1))) for m in _SEASON_RE.finditer(html)]

    for row in _ROW_RE.finditer(html):
        trek_name = row.group(1).strip()
        if not trek_name:
            continue

        # figure out which season this row belongs to
        row_pos = row.start()
        for pos, yr in season_positions:
            if pos < row_pos:
                current_year = yr

        dates_cell = row.group(2)
        batches = []
        for link in _LINK_RE.finditer(dates_cell):
            dep_id = link.group(1)
            date_text = " ".join(link.group(2).split())
            dm = _DATE_RE.match(date_text)
            if not dm:
                continue
            start = _parse_date(dm.group(1), current_year)
            end = _parse_date(dm.group(2), current_year)
            if not start or not end:
                continue
            # if end is before start, end is in the next year
            if end < start:
                end = end.replace(year=end.year + 1)
            batches.append(ParsedBatch(
                start=start,
                end=end,
                status="open",
                seats_left=None,
                raw_status="",
                external_id=dep_id,
            ))

        key = _normalise(trek_name)
        if key not in result:
            result[key] = []
        result[key].extend(batches)

    return result


def _normalise(name):
    """Lower-case, collapse whitespace, strip 'trek'/'trekking'/'expedition' suffix."""
    name = " ".join(name.lower().split())
    name = re.sub(r"\s+(trek|trekking|expedition)$", "", name)
    return name


def fetch_page(sess=None):
    sess = sess or session()
    return parse_page(get(sess, PAGE_URL).text)


def fetch_batches(trek_name, sess=None):
    """Fetch all departures then return only those matching trek_name."""
    all_treks = fetch_page(sess)
    key = _normalise(trek_name)
    batches = all_treks.get(key)
    if batches is None:
        close = [k for k in all_treks if key.split()[0] in k]
        hint = f" (did you mean: {', '.join(close)}?)" if close else ""
        raise SourceError(
            f"HGN: no trek matching '{trek_name}' on the departures page{hint}"
        )
    return batches
