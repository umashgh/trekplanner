"""Long-running scheduler: runs refresh_data daily at the configured time."""
import signal
import time
from datetime import datetime, timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from treks.services.refresh import run_refresh

DEFAULT_HOUR = 3
DEFAULT_MINUTE = 0
MAX_RETRIES = 3
RETRY_DELAY = 300  # seconds


class Command(BaseCommand):
    help = "Run the daily refresh loop. Intended to be managed by systemd."

    def add_arguments(self, parser):
        parser.add_argument("--hour", type=int, default=DEFAULT_HOUR,
                            help=f"Hour (in {settings.TIME_ZONE}) to run the refresh (default: {DEFAULT_HOUR}).")
        parser.add_argument("--minute", type=int, default=DEFAULT_MINUTE,
                            help=f"Minute to run the refresh (default: {DEFAULT_MINUTE}).")

    def handle(self, *args, **opts):
        hour, minute = opts["hour"], opts["minute"]
        self._running = True
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

        self.stdout.write(f"Scheduler started — daily refresh at {hour:02d}:{minute:02d} {settings.TIME_ZONE}")

        while self._running:
            now = timezone.localtime()
            target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            wait = (target - now).total_seconds()
            self.stdout.write(f"Next refresh at {target.strftime('%Y-%m-%d %H:%M %Z')} (in {wait / 3600:.1f}h)")

            if not self._sleep(wait):
                break

            self._run_with_retries()

    def _run_with_retries(self):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                self.stdout.write(f"Refresh attempt {attempt}/{MAX_RETRIES}…")
                run = run_refresh(include_flights=True)
                self.stdout.write(
                    f"Refresh {run.get_state_display()}: "
                    f"{run.batches_seen} batches ({run.batches_new} new), "
                    f"{run.quotes_fetched} fares"
                )
                if run.state in ("ok", "partial"):
                    return
                self.stderr.write(f"Refresh finished with state '{run.state}', retrying…")
            except Exception as e:
                self.stderr.write(f"Refresh attempt {attempt} failed: {e}")

            if attempt < MAX_RETRIES:
                self.stdout.write(f"Waiting {RETRY_DELAY}s before retry…")
                if not self._sleep(RETRY_DELAY):
                    return

        raise RuntimeError(f"Refresh failed after {MAX_RETRIES} attempts")

    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(min(10, end - time.monotonic()))
        return self._running

    def _stop(self, signum, frame):
        self.stdout.write("Received shutdown signal, exiting…")
        self._running = False
