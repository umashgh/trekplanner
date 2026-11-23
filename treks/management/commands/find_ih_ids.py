from django.core.management.base import BaseCommand

from treks.models import Operator, OperatorTrek
from treks.services.indiahikes import discover_trek_ids
from treks.services.refresh import _match_ih_name, _normalise_name


class Command(BaseCommand):
    help = "List Indiahikes trek ids and optionally save them to listings missing an external_id."

    def add_arguments(self, parser):
        parser.add_argument("--max-id", type=int, default=200)
        parser.add_argument("--match", default="", help="Only show names containing this text.")
        parser.add_argument("--save", action="store_true",
                            help="Auto-populate external_id on IH listings that lack one.")

    def handle(self, *args, **opts):
        found = discover_trek_ids(opts["max_id"])
        for name, i in sorted(found.items(), key=lambda kv: kv[1]):
            if opts["match"].lower() in name.lower():
                self.stdout.write(f"{i:>4}  {name}")

        if opts["save"]:
            ih_names = {_normalise_name(k): v for k, v in found.items()}
            missing = OperatorTrek.objects.filter(
                operator__source=Operator.Source.INDIAHIKES,
                external_id="", active=True, trek__active=True,
            ).select_related("trek")
            updated = 0
            for listing in missing:
                ih_id = _match_ih_name(listing.trek.name, ih_names)
                if ih_id is not None:
                    listing.external_id = str(ih_id)
                    listing.save(update_fields=["external_id"])
                    self.stdout.write(self.style.SUCCESS(
                        f"  Saved: {listing.trek.name} → IH id {ih_id}"))
                    updated += 1
                else:
                    self.stdout.write(self.style.WARNING(
                        f"  No match: {listing.trek.name}"))
            self.stdout.write(f"\nUpdated {updated} of {missing.count()} listings.")
