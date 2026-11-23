"""One-time command: scrape day-by-day elevation profiles from TTH and IH trek pages."""
import json
import re
import time

from django.core.management.base import BaseCommand

from treks.models import Operator, OperatorTrek
from treks.services.http import browser_client


FT_TO_M = 0.3048


def _parse_tth_profile(html):
    """Extract day-by-day altitudes from TTH's Next.js quick-itinerary payload."""
    from treks.services.tth import _payload_text
    text = _payload_text(html)
    if not text:
        return None

    decoder = json.JSONDecoder()
    altitudes = []
    distances = []
    seen_days = set()
    for m in re.finditer(r'\{"day":', text):
        try:
            obj, _ = decoder.raw_decode(text, m.start())
        except ValueError:
            continue
        day_num = obj.get("day")
        if day_num in seen_days:
            break
        seen_days.add(day_num)
        itin = obj.get("quickItinerary") or ""
        m_match = re.search(r"([\d,]+)\s*m\s*/", itin)
        if m_match:
            altitudes.append(int(m_match.group(1).replace(",", "")))
        else:
            ft_match = re.search(r"([\d,]+)\s*ft", itin, re.IGNORECASE)
            if ft_match:
                altitudes.append(round(int(ft_match.group(1).replace(",", "")) * FT_TO_M))
            else:
                altitudes.append(None)
        distances.append(_extract_distance_km(itin))

    if not altitudes or all(a is None for a in altitudes):
        return None

    _fill_gaps(altitudes)
    return _make_profile(altitudes, distances)


def _extract_day_altitude_ft(text):
    """Extract the ending altitude (ft) from a day's text content."""
    text = re.sub(r"<[^>]+>", " ", text)

    via3 = re.search(r"([\d,]+)\s*ft\s+to\s+([\d,]+)\s*ft\s+via\s+([\d,]+)\s*ft", text)
    if via3:
        return int(via3.group(2).replace(",", ""))
    rng = re.search(r"([\d,]+)\s*ft\s+to\s+([\d,]+)\s*ft", text)
    if rng:
        return int(rng.group(2).replace(",", ""))
    via2 = re.search(r"([\d,]+)\s*ft\s+via\s+([\d,]+)\s*ft", text)
    if via2:
        return int(via2.group(2).replace(",", ""))
    alt = re.search(r"[Aa]ltitude[^:]*:\s*([\d,]+)\s*ft", text)
    if alt:
        return int(alt.group(1).replace(",", ""))
    return None


def _extract_distance_km(text):
    """Extract trek distance in km from a day's text content."""
    text = re.sub(r"<[^>]+>", " ", text)
    m = re.search(r"[Tt]rek\s+[Dd]istance\s*:?\s*([\d.]+)\s*km", text)
    if m:
        return float(m.group(1))
    m = re.search(r"[Dd]istance\s*:?\s*([\d.]+)\s*km", text)
    if m:
        val = float(m.group(1))
        if val > 30:
            return None
        return val
    return None


def _make_profile(altitudes, distances=None):
    """Build a profile dict (with distances) or plain list (without).

    Only includes distances if at least half the days have non-zero values.
    """
    if distances:
        clean_dist = [d if d is not None else 0 for d in distances]
        nonzero = sum(1 for d in clean_dist if d > 0)
        if nonzero >= len(clean_dist) / 2:
            return {"altitudes": altitudes, "distances": clean_dist}
    return altitudes


def _parse_ih_html_profile(html):
    """Extract altitudes from IH's 'How Does Each Day Look' server-rendered HTML."""
    idx = html.find("How Does Each Day Look")
    if idx == -1:
        return None

    section = html[idx : idx + 200_000]
    pane_starts = [
        (m.start(), int(m.group(1)))
        for m in re.finditer(r"tabpane-Day\s*(\d+)", section)
    ]
    day_starts = {}
    for pos, day in pane_starts:
        day_starts[day] = pos
    if not day_starts:
        return None

    days_sorted = sorted(day_starts.items())
    altitudes = []
    distances = []
    for i, (day, start) in enumerate(days_sorted):
        end = days_sorted[i + 1][1] if i + 1 < len(days_sorted) else start + 15_000
        content = section[start:end]
        altitudes.append(_extract_day_altitude_ft(content))
        distances.append(_extract_distance_km(content))

    non_none = [a for a in altitudes if a is not None]
    if len(non_none) < 2 or len(set(non_none)) < 2:
        return None

    _fill_gaps(altitudes)
    return _make_profile([round(a * FT_TO_M) for a in altitudes], distances)


def _parse_ih_jsonld_profile(html):
    """Extract altitudes from IH's JSON-LD itinerary ItemList.

    IH pages embed a @graph array inside a single ld+json script that is
    sometimes malformed, so json.loads on the whole block fails.  Instead we
    find the ItemList object directly in the HTML and use raw_decode.
    """
    decoder = json.JSONDecoder()
    for m in re.finditer(r'\{"@type":\s*"ItemList"', html):
        try:
            data, _ = decoder.raw_decode(html, m.start())
        except (ValueError, json.JSONDecodeError):
            continue
        name = (data.get("name") or "").lower()
        if "itinerary" not in name:
            continue

        altitudes = []
        for item in data.get("itemListElement") or []:
            text = (item.get("item", {}).get("text") or "") + " " + (item.get("item", {}).get("name") or "")
            ft_matches = re.findall(r"([\d,]+)\s*ft", text, re.IGNORECASE)
            if ft_matches:
                alt = round(int(ft_matches[-1].replace(",", "")) * FT_TO_M)
                altitudes.append(alt)
                continue
            m_matches = re.findall(r"([\d,]+)\s*m(?:\s|,|\.)", text)
            if m_matches:
                altitudes.append(int(m_matches[-1].replace(",", "")))
                continue
            altitudes.append(None)

        if altitudes and not all(a is None for a in altitudes):
            _fill_gaps(altitudes)
            return altitudes
    return None


def _parse_ih_nextdata_profile(html):
    """Extract altitudes and distances from IH's __NEXT_DATA__ structured data.

    Tries day_wise_itinerary slices first (richest), then quick_itinerary.
    Distances come from quick_itinerary sub_heading2 (present in both paths).
    """
    m = re.search(r'__NEXT_DATA__.*?({.*?})</script>', html, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except (ValueError, json.JSONDecodeError):
        return None
    if data.get("isFallback"):
        return None
    body = (data.get("props", {}).get("pageProps", {})
            .get("trekData", {}).get("data", {}).get("body", []))

    # Extract distances from quick_itinerary (available for both paths)
    qi_slices = [s for s in body if s.get("slice_type") == "quick_itinerary"]
    qi_distances = []
    qi_altitudes = []
    for qi in qi_slices:
        for item in qi.get("items", []):
            sub = item.get("sub_heading2") or item.get("heading2") or []
            text = " ".join(p.get("text", "") for p in sub if isinstance(p, dict))
            qi_distances.append(_extract_distance_km(text))
            qi_altitudes.append(_extract_day_altitude_ft(text))

    # Try day_wise_itinerary slices for altitudes (richest source)
    day_slices = [s for s in body if s.get("slice_type") == "day_wise_itinerary"]
    if day_slices:
        altitudes = []
        for sl in day_slices:
            alt_rich = (sl.get("items") or [{}])[0].get("altitude", [])
            text = " ".join(p.get("text", "") for p in alt_rich if isinstance(p, dict))
            altitudes.append(_extract_day_altitude_ft(text))
        non_none = [a for a in altitudes if a is not None]
        if len(non_none) >= 2 and len(set(non_none)) >= 2:
            _fill_gaps(altitudes)
            alts_m = [round(a * FT_TO_M) for a in altitudes]
            distances = qi_distances if len(qi_distances) == len(alts_m) else None
            return _make_profile(alts_m, distances)

    # Fallback: quick_itinerary for altitudes too
    if qi_altitudes:
        non_none = [a for a in qi_altitudes if a is not None]
        if len(non_none) >= 2 and len(set(non_none)) >= 2:
            _fill_gaps(qi_altitudes)
            alts_m = [round(a * FT_TO_M) for a in qi_altitudes]
            return _make_profile(alts_m, qi_distances)

    return None


def _parse_ih_profile(html):
    """Try __NEXT_DATA__ first (most structured), then HTML, then JSON-LD."""
    return (_parse_ih_nextdata_profile(html)
            or _parse_ih_html_profile(html)
            or _parse_ih_jsonld_profile(html))


def _fill_gaps(altitudes):
    """Replace None entries by interpolating from neighbours."""
    # Forward fill
    last = None
    for i, a in enumerate(altitudes):
        if a is not None:
            last = a
        elif last is not None:
            altitudes[i] = last
    # Backward fill for leading Nones
    first = next((a for a in altitudes if a is not None), None)
    for i, a in enumerate(altitudes):
        if a is None and first is not None:
            altitudes[i] = first
        else:
            break


class Command(BaseCommand):
    help = "Scrape day-by-day elevation profiles from TTH and IH trek pages (one-time)."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Print profiles without saving.")
        parser.add_argument("--overwrite", action="store_true", help="Overwrite existing profiles.")

    def handle(self, *args, **opts):
        dry_run = opts["dry_run"]
        overwrite = opts["overwrite"]
        client = browser_client()

        listings = (
            OperatorTrek.objects
            .filter(active=True, trek__active=True, operator__active=True)
            .select_related("operator", "trek")
            .order_by("trek__sort_order", "operator__name")
        )

        # Group by trek, pick first operator alphabetically
        seen_treks = set()
        to_scrape = []
        for listing in listings:
            trek = listing.trek
            if trek.pk in seen_treks:
                continue
            if trek.elevation_profile and not overwrite:
                seen_treks.add(trek.pk)
                continue
            if listing.operator.source in (Operator.Source.TTH, Operator.Source.INDIAHIKES):
                seen_treks.add(trek.pk)
                to_scrape.append(listing)

        self.stdout.write(f"Found {len(to_scrape)} treks to scrape.\n")

        saved = skipped = failed = 0
        for listing in to_scrape:
            trek = listing.trek
            source = listing.operator.source
            url = listing.page_url

            self.stdout.write(f"  {trek.name} ({listing.operator.short_name}): {url} … ", ending="")
            try:
                r = client.get(url)
                if r.status_code == 404 and source == Operator.Source.INDIAHIKES:
                    alt_url = url.rstrip("/") + "-trek"
                    r = client.get(alt_url)
                    if r.status_code == 200:
                        self.stdout.write(f"(→ {alt_url}) ", ending="")
                if r.status_code != 200:
                    self.stdout.write(self.style.WARNING(f"HTTP {r.status_code}"))
                    failed += 1
                    time.sleep(2)
                    continue

                if source == Operator.Source.TTH:
                    profile = _parse_tth_profile(r.text)
                else:
                    profile = _parse_ih_profile(r.text)

                if not profile:
                    self.stdout.write(self.style.WARNING("no elevation data found"))
                    skipped += 1
                else:
                    if isinstance(profile, dict):
                        label = f"{profile['altitudes']} + km:{profile['distances']}"
                    else:
                        label = str(profile)
                    self.stdout.write(self.style.SUCCESS(label))
                    if not dry_run:
                        trek.elevation_profile = profile
                        trek.save(update_fields=["elevation_profile"])
                    saved += 1
            except Exception as e:
                self.stdout.write(self.style.ERROR(f"error: {e}"))
                failed += 1

            time.sleep(1.5)

        self.stdout.write(f"\nDone: {saved} saved, {skipped} no data, {failed} failed.")
        if dry_run:
            self.stdout.write("(dry run — nothing was saved)")
