"""One refresh: re-read every active operator listing, then price flights."""
from collections import defaultdict
from datetime import timedelta
from difflib import SequenceMatcher

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ..models import Airport, Batch, FlightQuote, Operator, OperatorTrek, RefreshRun
from . import hgn, indiahikes, tth
from .flights import get_provider
from .http import SourceError, session


def _resolve_ih_ids(listings, sess, run):
    """Auto-discover and save Indiahikes external_ids for listings that lack them."""
    missing = [l for l in listings
               if l.operator.source == Operator.Source.INDIAHIKES and not l.external_id]
    if not missing:
        return
    run.write(f"Discovering Indiahikes trek ids for {len(missing)} listings…")
    try:
        ih_map = indiahikes.discover_trek_ids(sess=sess)
    except Exception as e:
        run.write(f"✗ IH discovery failed: {e}")
        return
    ih_names = {_normalise_name(k): v for k, v in ih_map.items()}
    for listing in missing:
        trek_name = listing.trek.name
        ih_id = _match_ih_name(trek_name, ih_names)
        if ih_id is not None:
            listing.external_id = str(ih_id)
            listing.save(update_fields=["external_id"])
            run.write(f"  ✓ {trek_name} → IH id {ih_id}")
        else:
            run.write(f"  · {trek_name}: no IH match found")


def _normalise_name(name):
    import re
    s = name.lower().strip()
    s = re.sub(r"[–—\-]+", " ", s)
    s = re.sub(r"\s+", " ", s)
    for suffix in (" trek", " pass trek", " lake trek"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
    return s.strip()


def _match_ih_name(trek_name, ih_names):
    """Find the best IH match for a trek name. Returns the IH id or None."""
    norm = _normalise_name(trek_name)
    if norm in ih_names:
        return ih_names[norm]
    for ih_norm, ih_id in ih_names.items():
        if norm in ih_norm or ih_norm in norm:
            return ih_id
    best_ratio, best_id = 0, None
    for ih_norm, ih_id in ih_names.items():
        ratio = SequenceMatcher(None, norm, ih_norm).ratio()
        if ratio > best_ratio:
            best_ratio, best_id = ratio, ih_id
    if best_ratio >= 0.7:
        return best_id
    return None


def run_refresh(include_flights=True):
    run = RefreshRun.objects.create()
    errors = 0
    sess = session()
    try:
        listings = list(OperatorTrek.objects
                    .filter(active=True, trek__active=True, operator__active=True,
                            needs_daily_refresh=True)
                    .select_related("operator", "trek"))
        _resolve_ih_ids(listings, sess, run)
        hgn_cache = {}
        for listing in listings:
            try:
                parsed = _fetch_listing(listing, sess, hgn_cache=hgn_cache)
            except SourceError as e:
                errors += 1
                listing.last_error = str(e)
                listing.save(update_fields=["last_error"])
                run.write(f"✗ {listing}: {e}")
                continue
            if parsed is None:
                continue
            seen, new = _store_batches(listing, parsed)
            run.batches_seen += seen
            run.batches_new += new
            run.write(f"✓ {listing}: {seen} batches ({new} new)")

        if include_flights:
            try:
                n, flight_errors = _refresh_flights(run)
                run.quotes_fetched = n
                errors += flight_errors
            except SourceError as e:
                errors += 1
                run.write(f"✗ Flights: {e}")

        run.state = RefreshRun.State.PARTIAL if errors else RefreshRun.State.OK
    except Exception as e:  # keep a record of unexpected failures
        run.state = RefreshRun.State.FAILED
        run.write(f"✗ Refresh stopped: {e!r}")
        raise
    finally:
        run.finished_at = timezone.now()
        run.save()
    return run


def _fetch_listing(listing, sess, *, hgn_cache=None):
    src = listing.operator.source
    if src == Operator.Source.INDIAHIKES:
        listing.refresh_from_db(fields=["external_id"])
        if not listing.external_id:
            raise SourceError("no Indiahikes trek id found (trek may not be on Indiahikes)")
        trek_data, batches = indiahikes.fetch_batches(listing.external_id, sess)
        if trek_data.get("duration") and not listing.duration_days:
            listing.duration_days = trek_data["duration"]
        return batches
    if src == Operator.Source.TTH:
        return tth.fetch_batches(listing.page_url, sess)
    if src == Operator.Source.HGN:
        if not listing.external_id:
            raise SourceError("no HGN trek name set in external_id")
        if hgn_cache is not None and "all" not in hgn_cache:
            hgn_cache["all"] = hgn.fetch_page(sess)
        all_treks = hgn_cache["all"] if hgn_cache is not None else hgn.fetch_page(sess)
        key = hgn._normalise(listing.external_id)
        batches = all_treks.get(key)
        if batches is None:
            close = [k for k in all_treks if key.split()[0] in k]
            hint = f" (did you mean: {', '.join(close)}?)" if close else ""
            raise SourceError(f"HGN: no trek matching '{listing.external_id}'{hint}")
        return batches
    return None  # manual operators are maintained in the admin


@transaction.atomic
def _store_batches(listing, parsed):
    now = timezone.now()
    all_batches = list(listing.batches.all())
    by_ext_id = {b.external_id: b for b in all_batches if b.external_id}
    by_dates = {(b.start_date, b.end_date): b for b in all_batches if not b.external_id}
    seen_ids, new = set(), 0
    for p in parsed:
        if p.external_id:
            b = by_ext_id.get(p.external_id)
            seen_ids.add(("ext", p.external_id))
        else:
            b = by_dates.get((p.start, p.end))
            seen_ids.add(("dates", p.start, p.end))
        if b is None:
            Batch.objects.create(
                listing=listing, start_date=p.start, end_date=p.end, status=p.status,
                seats_left=p.seats_left, raw_status=p.raw_status, external_id=p.external_id,
                first_seen_at=now, last_seen_at=now,
            )
            new += 1
        else:
            b.status, b.seats_left, b.raw_status = p.status, p.seats_left, p.raw_status
            b.start_date, b.end_date = p.start, p.end
            b.external_id, b.last_seen_at, b.removed = p.external_id, now, False
            b.save()
    today = timezone.localdate()
    for b in all_batches:
        if b.external_id:
            key = ("ext", b.external_id)
        else:
            key = ("dates", b.start_date, b.end_date)
        if key not in seen_ids and b.start_date >= today and not b.removed:
            b.removed = True
            b.save(update_fields=["removed"])
    listing.last_fetched_at = now
    listing.last_error = ""
    listing.save(update_fields=["last_fetched_at", "last_error", "duration_days"])
    return len(parsed), new


def flight_pairs():
    """Which (destination, depart, return) fares to fetch, and which batches each covers.

    Returns {(iata, depart, return): [batch, ...]}.
    """
    cfg = settings.TREKS
    today = timezone.localdate()
    horizon = today + timedelta(days=cfg["FLIGHT_HORIZON_DAYS"])
    batches = (Batch.objects.filter(removed=False, start_date__gt=today + timedelta(days=1),
                                    start_date__lte=horizon, listing__active=True,
                                    listing__trek__active=True)
               .select_related("listing__trek"))
    pairs = defaultdict(list)
    if cfg["FLIGHT_SAMPLING"] == "per_batch":
        for b in batches:
            pairs[(b.listing.trek.gateway_airport_id, b.fly_in_date, b.fly_out_date)].append(b)
        return pairs
    # weekly: group by airport + ISO week of fly-in; price the earliest batch in each group
    groups = defaultdict(list)
    for b in batches:
        y, w, _ = b.fly_in_date.isocalendar()
        groups[(b.listing.trek.gateway_airport_id, y, w)].append(b)
    for (iata, _, _), group in groups.items():
        rep = min(group, key=lambda b: (b.start_date, b.end_date))
        pairs[(iata, rep.fly_in_date, rep.fly_out_date)].extend(group)
    return pairs


def _refresh_flights(run):
    cfg = settings.TREKS
    provider = get_provider()
    if provider.name == "none":
        run.write("Flights: provider is 'none', skipped")
        return 0, 0
    origin, _ = Airport.objects.get_or_create(iata=cfg["HOME_AIRPORT"], defaults={"city": cfg["HOME_AIRPORT"]})
    fresh_after = timezone.now() - timedelta(hours=cfg["FLIGHT_CACHE_HOURS"])
    fetched = errors = cached = 0
    for (iata, dep, ret) in sorted(flight_pairs()):
        recent = FlightQuote.objects.filter(origin=origin, destination_id=iata, depart_date=dep,
                                            return_date=ret, provider=provider.name,
                                            fetched_at__gte=fresh_after).exists()
        if recent:
            cached += 1
            continue
        try:
            fare = provider.cheapest_return(origin.iata, iata, dep, ret)
        except SourceError as e:
            errors += 1
            run.write(f"✗ Flight {origin.iata}→{iata} {dep}/{ret}: {e}")
            if errors >= 5:
                run.write("✗ Flights: stopping after 5 errors")
                break
            continue
        if fare is None:
            run.write(f"· Flight {origin.iata}→{iata} {dep}/{ret}: no fares returned")
            continue
        FlightQuote.objects.create(origin=origin, destination_id=iata, depart_date=dep, return_date=ret,
                                   price=fare.price, currency=fare.currency, airlines=fare.airlines,
                                   stops=fare.stops, provider=provider.name)
        fetched += 1
    run.write(f"Flights: {fetched} fetched, {cached} reused from the last {cfg['FLIGHT_CACHE_HOURS']}h")
    return fetched, errors
