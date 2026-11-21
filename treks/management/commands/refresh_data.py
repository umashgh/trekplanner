from django.core.management.base import BaseCommand

from treks.services.refresh import run_refresh


class Command(BaseCommand):
    help = "Fetch batches from every operator and flight prices (same as the Refresh button)."

    def add_arguments(self, parser):
        parser.add_argument("--no-flights", action="store_true", help="Only refresh trek batches.")

    def handle(self, *args, **opts):
        run = run_refresh(include_flights=not opts["no_flights"])
        self.stdout.write(run.log)
        self.stdout.write(self.style.SUCCESS(f"{run.get_state_display()}: {run.batches_seen} batches, "
                                             f"{run.batches_new} new, {run.quotes_fetched} fares"))
