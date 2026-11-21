from django.core.management.base import BaseCommand

from treks.services.indiahikes import discover_trek_ids


class Command(BaseCommand):
    help = "List Indiahikes trek ids (needed in a listing's external_id) by probing their booking API."

    def add_arguments(self, parser):
        parser.add_argument("--max-id", type=int, default=200)
        parser.add_argument("--match", default="", help="Only show names containing this text.")

    def handle(self, *args, **opts):
        found = discover_trek_ids(opts["max_id"])
        for name, i in sorted(found.items(), key=lambda kv: kv[1]):
            if opts["match"].lower() in name.lower():
                self.stdout.write(f"{i:>4}  {name}")
