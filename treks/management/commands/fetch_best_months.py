"""Scrape 'best time' FAQ answers from TTH pages to populate Trek.best_months."""
import json
import re

from django.core.management.base import BaseCommand

from treks.models import Operator, OperatorTrek, Trek
from treks.services.http import browser_client

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

JSON_LD_RE = re.compile(
    r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S
)


def _extract_best_months(html):
    for m in JSON_LD_RE.finditer(html):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for item in (data.get("@graph", []) if isinstance(data, dict) else []):
            if item.get("@type") != "FAQPage":
                continue
            for q in item.get("mainEntity", []):
                if "best time" not in q.get("name", "").lower():
                    continue
                answer = q["acceptedAnswer"]["text"].lower()
                months = sorted(n for name, n in MONTH_NAMES.items() if name in answer)
                if months:
                    return months
    return []


class Command(BaseCommand):
    help = "Populate Trek.best_months from TTH FAQ 'best time' answers."

    def add_arguments(self, parser):
        parser.add_argument(
            "--overwrite", action="store_true",
            help="Overwrite treks that already have best_months set.",
        )

    def handle(self, *args, **opts):
        client = browser_client()
        tth_listings = (
            OperatorTrek.objects
            .filter(operator__source=Operator.Source.TTH, active=True, trek__active=True)
            .select_related("trek")
        )
        updated = skipped = failed = 0
        for listing in tth_listings:
            trek = listing.trek
            if trek.best_months and not opts["overwrite"]:
                self.stdout.write(f"  {trek.name}: already set ({trek.best_months}), skipping")
                skipped += 1
                continue
            try:
                r = client.get(listing.page_url)
                if r.status_code != 200:
                    self.stderr.write(f"  {trek.name}: HTTP {r.status_code}")
                    failed += 1
                    continue
            except Exception as e:
                self.stderr.write(f"  {trek.name}: {e}")
                failed += 1
                continue
            months = _extract_best_months(r.text)
            if months:
                trek.best_months = ",".join(str(m) for m in months)
                trek.save(update_fields=["best_months"])
                self.stdout.write(self.style.SUCCESS(
                    f"  {trek.name}: {trek.best_months}"
                ))
                updated += 1
            else:
                self.stdout.write(f"  {trek.name}: no 'best time' FAQ found")
                failed += 1
        self.stdout.write(
            f"\nDone: {updated} updated, {skipped} skipped, {failed} not found"
        )
