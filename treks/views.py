import calendar
import json
import threading
from collections import defaultdict
from datetime import date, timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .models import Batch, FlightQuote, Operator, OperatorTrek, PageView, PlannedBatch, RefreshRun, Trek
from .services.refresh import run_refresh

_refresh_lock = threading.Lock()


def explore_view(request):
    """Single-page 'Which hike next?' view with all data embedded as JSON."""
    today = timezone.localdate()
    cfg = settings.TREKS

    treks = Trek.objects.filter(active=True).prefetch_related(
        "listings__operator", "listings__batches",
    )

    # Build fare index for expense detection
    latest_fares, by_dest_week, cheapest_fares = _fare_index()
    pct_limit = cfg["EXPENSIVE_FLIGHT_PCT"]

    trek_list = []
    for trek in treks:
        active_listings = [
            l for l in trek.listings.all()
            if l.active and l.operator.active
        ]

        # Collect operators
        ops = []
        for l in active_listings:
            ops.append({
                "short": l.operator.short_name,
                "name": l.operator.name,
            })

        # Collect upcoming batches and compute months/departures
        dep_months = set()
        deps_by_month = defaultdict(int)
        total_deps = 0
        expensive_count = 0
        total_priced = 0

        for listing in active_listings:
            for b in listing.batches.all():
                if b.removed or b.end_date < today:
                    continue
                total_deps += 1
                dep_months.add(b.start_date.month)
                deps_by_month[b.start_date.month] += 1

                # Check flight expense
                q = _fare_for(b, latest_fares, by_dest_week)
                if q and cheapest_fares.get(q.destination_id):
                    total_priced += 1
                    pct = round((q.price / cheapest_fares[q.destination_id] - 1) * 100)
                    if pct >= pct_limit:
                        expensive_count += 1

        # Best months the trek can be done (for filtering); fall back to departure months
        best = [int(m) for m in trek.best_months.split(",") if m.strip()] if trek.best_months else sorted(dep_months)
        if not best:
            best = list(range(1, 13))  # show year-round if nothing specified

        alt = trek.max_altitude_m or 3000
        level = trek.difficulty
        booking = trek.booking_type

        # Duration: prefer Trek-level range, fall back to listing
        days_min = trek.duration_days_min
        days_max = trek.duration_days_max
        if not days_max:
            for l in active_listings:
                if l.duration_days:
                    days_min = days_min or l.duration_days
                    days_max = l.duration_days
                    break
        days = days_max or days_min or 7

        # Build a simple profile: rise from ~2000m to max, descend back
        start_alt = min(2000, alt - 500)
        summit_day = max(1, days * 2 // 3)
        profile = []
        for d in range(days):
            if d <= summit_day:
                t = d / summit_day if summit_day > 0 else 1
                profile.append(round(start_alt + (alt - start_alt) * t))
            else:
                t = (d - summit_day) / (days - 1 - summit_day) if (days - 1 - summit_day) > 0 else 1
                profile.append(round(alt - (alt - start_alt) * t))

        expensive = (expensive_count > total_priced / 2) if total_priced > 0 else False

        # Per-operator departure dates keyed by month
        # Structure: { "3": { "IH": [{"d":15,"end":"25 Mar","s":"open"}, ...], "HGN": [...] }, ... }
        op_deps = defaultdict(lambda: defaultdict(list))
        STATUS_MAP = {"open": "open", "last_few": "open", "waitlist": "wait",
                      "full": "full", "closed": "full", "unknown": "open"}
        for listing in active_listings:
            short = listing.operator.short_name
            for b in listing.batches.all():
                if b.removed or b.end_date < today:
                    continue
                m = str(b.start_date.month)
                op_deps[m][short].append({
                    "d": b.start_date.day,
                    "end": b.end_date.strftime("%-d %b"),
                    "s": STATUS_MAP.get(b.status, "open"),
                })
        # Sort each operator's batches by day
        for m in op_deps:
            for short in op_deps[m]:
                op_deps[m][short].sort(key=lambda x: x["d"])
        op_deps = {m: dict(ops) for m, ops in op_deps.items()}

        note = trek.notes or _trek_note(trek.name, alt, days, trek.region)

        trek_list.append({
            "id": trek.pk,
            "name": trek.name,
            "region": trek.region,
            "state": trek.state,
            "country": trek.country,
            "gateway": trek.gateway_airport_id,
            "days": days,
            "days_min": days_min or days,
            "days_max": days_max or days,
            "level": level,
            "alt": alt,
            "booking": booking,
            "cost_min": trek.cost_inr_min,
            "cost_max": trek.cost_inr_max,
            "portal_url": trek.portal_url or "",
            "starting_point": trek.starting_point or "",
            "months": sorted(best),
            "dep_months": sorted(dep_months),
            "deps": total_deps,
            "deps_by_month": dict(deps_by_month),
            "ops": ops,
            "op_deps": dict(op_deps),
            "profile": profile,
            "expensive": expensive,
            "note": note,
        })

    # Sort by altitude descending for default ordering
    trek_list.sort(key=lambda t: -t["alt"])

    last_run = RefreshRun.objects.first()
    last_refresh = None
    if last_run and last_run.finished_at:
        last_refresh = timezone.localtime(last_run.finished_at).strftime("%-d %b %Y, %H:%M IST")

    return render(request, "treks/explore.html", {
        "treks_json": trek_list,
        "last_refresh": last_refresh,
    })


_TREK_NOTES = {
    "Everest Base Camp": "The classic walk to the foot of the world's highest peak.",
    "Annapurna Base Camp": "A short, steep approach into the Annapurna sanctuary.",
    "Annapurna Circuit": "The grand loop — rice paddies to high desert in one trek.",
    "Langtang Valley": "A quieter valley close to Kathmandu, with big glacier views.",
    "Manaslu Circuit": "Remote and permit-restricted — fewer trekkers, wilder trail.",
    "Upper Mustang": "An arid, Tibetan-feeling kingdom behind the Himalayan rain shadow.",
    "Kanchenjunga Base Camp": "A long walk to the third-highest peak, through rhododendron forests.",
    "Everest Three Passes": "Three 5,000 m passes in one go — a serious high-altitude loop.",
    "Island Peak": "A trekking peak with one real climbing day above Chukhung.",
    "Mera Peak": "The highest trekking peak in Nepal — glacier walking, not technical climbing.",
    "Kedarkantha": "A short winter trek with wide summit views of Swargarohini.",
    "Brahmatal": "A frozen-lake winter trek with Trishul and Nanda Ghunti on the horizon.",
    "Kuari Pass": "Lord Curzon's trail — panoramic ridgeline walking above Joshimath.",
    "Har Ki Dun": "A gentle valley trek through ancient villages in western Garhwal.",
    "Roopkund": "The mysterious skeleton lake at 4,800 m, below Trishul's south face.",
    "Valley of Flowers": "A UNESCO-listed alpine meadow that blooms July through September.",
    "Hampta Pass": "A quick crossover from green Kullu to stark Lahaul.",
    "Chandrashila": "A summit day above Tungnath — the highest Shiva temple.",
    "Sandakphu": "Ridgeline walking with four of the five highest peaks in view.",
    "Goechala": "Face-to-face with Kanchenjunga's southwest wall at dawn.",
    "Chadar": "Walking a frozen river through a Zanskar gorge in deep winter.",
    "Markha Valley": "Ladakh's best-known trek — high passes, whitewashed gompas.",
    "Stok Kangri": "A non-technical 6,000 m summit above the Indus valley.",
    "Rupin Pass": "A dramatic hanging valley and waterfall crossing in Kinnaur.",
    "Buran Ghati": "A steep snow-pass trek connecting Shimla hills to Spiti.",
    "Pin Parvati": "A long, committing crossing from Parvati valley into Spiti.",
    "Beas Kund": "A short walk to the glacial source of the Beas river.",
    "Dayara Bugyal": "A wide alpine meadow at 3,400 m — gentle and green.",
    "Ali Bedni Bugyal": "Rolling grasslands beneath Trishul with shepherd camps.",
    "Pangarchulla": "A summit climb above Auli with close-up Nanda Devi views.",
    "Tarsar Marsar": "Twin alpine lakes in the Kashmir highlands.",
    "Deoriatal Chandrashila": "Lake reflections and a summit sunrise in one short trek.",
}


def _trek_note(name, alt, days, region):
    if name in _TREK_NOTES:
        return _TREK_NOTES[name]
    if alt >= 5500:
        return f"A high-altitude {days}-day route reaching {alt:,} m in {region}."
    if alt >= 4000:
        return f"A {days}-day trail through {region}, crossing above {alt:,} m."
    if days <= 4:
        return f"A short {days}-day escape in {region} — good for a long weekend."
    return f"A {days}-day walk through {region}, topping out at {alt:,} m."


def _fare_index():
    """Latest quote per (dest, depart, return) and the cheapest price per destination."""
    latest = {}
    for q in FlightQuote.objects.order_by("fetched_at"):
        latest[(q.destination_id, q.depart_date, q.return_date)] = q
    by_dest_week = defaultdict(list)
    cheapest = {}
    for (dest, dep, _), q in latest.items():
        y, w, _ = dep.isocalendar()
        by_dest_week[(dest, y, w)].append(q)
        cheapest[dest] = min(cheapest.get(dest, q.price), q.price)
    return latest, by_dest_week, cheapest


def _fare_for(batch, latest, by_dest_week):
    dest = batch.listing.trek.gateway_airport_id
    exact = latest.get((dest, batch.fly_in_date, batch.fly_out_date))
    if exact:
        return exact
    y, w, _ = batch.fly_in_date.isocalendar()
    week = by_dest_week.get((dest, y, w))
    if not week:
        return None
    return min(week, key=lambda q: (abs((q.depart_date - batch.fly_in_date).days), q.price))


@staff_member_required
def calendar_view(request):
    cfg = settings.TREKS
    today = timezone.localdate()
    treks = list(Trek.objects.filter(active=True))
    operators = list(Operator.objects.filter(active=True))

    sel_treks = set(request.GET.getlist("trek")) or {str(t.pk) for t in treks}
    sel_ops = set(request.GET.getlist("op")) or {str(o.pk) for o in operators}

    batches = (Batch.objects
               .filter(removed=False, end_date__gte=today,
                       listing__trek__in=[t for t in treks if str(t.pk) in sel_treks],
                       listing__operator__in=[o for o in operators if str(o.pk) in sel_ops])
               .select_related("listing__trek__gateway_airport", "listing__operator", "plan"))

    latest, by_dest_week, cheapest = _fare_index()
    pct_limit = cfg["EXPENSIVE_FLIGHT_PCT"]

    # month -> listing -> {day: cell}
    months = defaultdict(lambda: defaultdict(dict))
    listing_meta = {}
    for b in batches:
        q = _fare_for(b, latest, by_dest_week)
        pct = None
        if q and cheapest.get(q.destination_id):
            pct = round((q.price / cheapest[q.destination_id] - 1) * 100)
        cell = {
            "batch": b,
            "quote": q,
            "pct": pct,
            "expensive": pct is not None and pct >= pct_limit,
            "planned": hasattr(b, "plan"),
        }
        key = (b.start_date.year, b.start_date.month)
        months[key][b.listing_id][b.start_date.day] = cell
        listing_meta[b.listing_id] = b.listing

    month_blocks = []
    for (y, m) in sorted(months):
        ndays = calendar.monthrange(y, m)[1]
        days = [{"n": d, "wd": "MTWTFSS"[date(y, m, d).weekday()],
                 "weekend": date(y, m, d).weekday() >= 5} for d in range(1, ndays + 1)]
        rows = []
        for lid in sorted(months[(y, m)], key=lambda l: (listing_meta[l].trek.sort_order,
                                                         listing_meta[l].operator.name)):
            cells = months[(y, m)][lid]
            rows.append({"listing": listing_meta[lid],
                         "cells": [{"d": d, "c": cells.get(d["n"])} for d in days]})
        month_blocks.append({"label": date(y, m, 1).strftime("%B %Y"), "days": days, "rows": rows})

    return render(request, "treks/calendar.html", {
        "month_blocks": month_blocks,
        "treks": treks, "operators": operators,
        "sel_treks": sel_treks, "sel_ops": sel_ops,
        "last_run": RefreshRun.objects.first(),
        "pct_limit": pct_limit,
        "home": cfg["HOME_AIRPORT"],
        "cheapest": cheapest,
        "listings": [l for t in treks for l in t.listings.filter(active=True, operator__active=True)],
    })


@staff_member_required
@require_POST
def refresh_view(request):
    if not _refresh_lock.acquire(blocking=False):
        messages.warning(request, "A refresh is already running.")
        return redirect("treks:runs")
    try:
        run = run_refresh(include_flights=request.POST.get("flights", "1") == "1")
    finally:
        _refresh_lock.release()
    level = messages.SUCCESS if run.state == RefreshRun.State.OK else messages.WARNING
    messages.add_message(request, level,
                         f"{run.get_state_display()}: {run.batches_seen} batches ({run.batches_new} new), "
                         f"{run.quotes_fetched} flight fares.")
    return redirect("treks:runs")


@staff_member_required
def runs_view(request):
    return render(request, "treks/runs.html", {"runs": RefreshRun.objects.all()[:20]})


@staff_member_required
@require_POST
def toggle_plan(request, pk):
    batch = get_object_or_404(Batch, pk=pk)
    plan = PlannedBatch.objects.filter(batch=batch).first()
    if plan:
        plan.delete()
        planned = False
    else:
        PlannedBatch.objects.create(batch=batch)
        planned = True
    return JsonResponse({"planned": planned})


@staff_member_required
def metrics_view(request):
    from django.db.models import Count
    from django.db.models.functions import TruncDate

    days = int(request.GET.get("days", 30))
    since = timezone.now() - timedelta(days=days)
    qs = PageView.objects.filter(timestamp__gte=since)

    total_views = qs.count()
    unique_ips = qs.values("ip").distinct().count()
    unique_sessions = qs.exclude(session_key="").values("session_key").distinct().count()

    by_path_raw = list(
        qs.values("path")
        .annotate(count=Count("id"))
        .order_by("-count")[:15]
    )
    max_path_count = by_path_raw[0]["count"] if by_path_raw else 1
    by_path = [
        {**row, "pct": round(row["count"] / max_path_count * 100)}
        for row in by_path_raw
    ]

    by_day_raw = list(
        qs.annotate(day=TruncDate("timestamp"))
        .values("day")
        .annotate(count=Count("id"))
        .order_by("day")
    )
    max_day_count = max((d["count"] for d in by_day_raw), default=1)
    by_day = [
        {**d, "height": round(d["count"] / max_day_count * 60)}
        for d in by_day_raw
    ]

    by_referrer = list(
        qs.exclude(referrer="")
        .values("referrer")
        .annotate(count=Count("id"))
        .order_by("-count")[:10]
    )

    return render(request, "treks/metrics.html", {
        "days": days,
        "total_views": total_views,
        "unique_ips": unique_ips,
        "unique_sessions": unique_sessions,
        "by_path": by_path,
        "by_day": by_day,
        "by_referrer": by_referrer,
    })


ROBOTS_TXT = """\
User-agent: Googlebot
Allow: /
Disallow: /admin/
Disallow: /calendar/
Disallow: /refresh/
Disallow: /runs/

User-agent: Bingbot
Allow: /
Disallow: /admin/
Disallow: /calendar/
Disallow: /refresh/
Disallow: /runs/

User-agent: *
Disallow: /admin/
Disallow: /calendar/
Disallow: /refresh/
Disallow: /runs/

User-agent: GPTBot
Disallow: /

User-agent: ChatGPT-User
Disallow: /

User-agent: CCBot
Disallow: /

User-agent: Google-Extended
Disallow: /

User-agent: anthropic-ai
Disallow: /

User-agent: ClaudeBot
Disallow: /

User-agent: Bytespider
Disallow: /

User-agent: FacebookBot
Disallow: /

User-agent: Amazonbot
Disallow: /

User-agent: Applebot-Extended
Disallow: /

User-agent: PerplexityBot
Disallow: /

User-agent: Cohere-ai
Disallow: /

Sitemap: https://treks.freeup.in/sitemap.xml
"""


def robots_txt(request):
    return HttpResponse(ROBOTS_TXT, content_type="text/plain")


def sitemap_xml(request):
    urls = [
        ("https://treks.freeup.in/", "weekly", "1.0"),
    ]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>']
    xml.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
    for loc, freq, priority in urls:
        xml.append(f"  <url><loc>{loc}</loc><changefreq>{freq}</changefreq><priority>{priority}</priority></url>")
    xml.append("</urlset>")
    return HttpResponse("\n".join(xml), content_type="application/xml")
