from django.db import models
from django.utils import timezone


class Operator(models.Model):
    """A trekking company whose batches we track."""

    class Source(models.TextChoices):
        INDIAHIKES = "indiahikes", "Indiahikes (public booking API)"
        TTH = "tth", "Trek The Himalayas (page data)"
        HGN = "hgn", "Himalayan Guides Nepal (fixed departures page)"
        MANUAL = "manual", "Manual entry only"

    name = models.CharField(max_length=100, unique=True)
    short_name = models.CharField(max_length=10, help_text="Shown on calendar rows, e.g. IH, TTH")
    website = models.URLField(blank=True)
    source = models.CharField(max_length=20, choices=Source.choices, default=Source.MANUAL)
    active = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Airport(models.Model):
    iata = models.CharField(max_length=3, primary_key=True)
    city = models.CharField(max_length=100)

    class Meta:
        ordering = ["iata"]

    def __str__(self):
        return f"{self.city} ({self.iata})"


class Trek(models.Model):
    class Difficulty(models.IntegerChoices):
        EASY = 1, "Easy"
        MODERATE = 2, "Moderate"
        HARD = 3, "Hard"

    class BookingType(models.TextChoices):
        SCHEDULED = "scheduled", "Scheduled batches (operator-led)"
        GOVT_PORTAL = "govt_portal", "Government portal (check availability)"
        LOCAL = "local", "Local operators / self-guided"

    name = models.CharField(max_length=100, unique=True)
    country = models.CharField(max_length=50, default="India")
    state = models.CharField(max_length=100, blank=True, help_text="State or region within the country.")
    gateway_airport = models.ForeignKey(
        Airport, on_delete=models.PROTECT, related_name="treks",
        help_text="Airport you fly into for this trek; used for flight prices.",
    )
    region = models.CharField(max_length=100, blank=True)
    max_altitude_m = models.PositiveIntegerField(null=True, blank=True)
    difficulty = models.IntegerField(choices=Difficulty.choices, default=Difficulty.MODERATE)
    booking_type = models.CharField(
        max_length=20, choices=BookingType.choices, default=BookingType.SCHEDULED,
    )
    sort_order = models.PositiveIntegerField(default=100)
    best_months = models.CharField(
        max_length=50, blank=True,
        help_text="Comma-separated month numbers (1-12) when the trek can be done, e.g. '3,4,5,9,10'.",
    )
    duration_days_min = models.PositiveIntegerField(
        null=True, blank=True, help_text="Shortest variant in days.",
    )
    duration_days_max = models.PositiveIntegerField(
        null=True, blank=True, help_text="Longest variant in days; used as upper bound for filtering.",
    )
    cost_inr_min = models.PositiveIntegerField(
        null=True, blank=True, help_text="Low end of cost range per person in INR (ex-GST, ex-travel).",
    )
    cost_inr_max = models.PositiveIntegerField(
        null=True, blank=True, help_text="High end of cost range; used as upper bound for filtering.",
    )
    starting_point = models.CharField(max_length=200, blank=True)
    portal_url = models.URLField(blank=True, help_text="For govt_portal treks: URL to check availability.")
    elevation_profile = models.JSONField(
        null=True, blank=True,
        help_text="Day-by-day altitude in metres, e.g. [2000, 2800, 3400, 3800, 3200, 2000]. "
                  "Populated by scraping or manual entry.",
    )
    active = models.BooleanField(default=True, help_text="Inactive treks are hidden and skipped on refresh.")
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class OperatorTrek(models.Model):
    """How one operator lists one trek: where to fetch its batches from."""

    operator = models.ForeignKey(Operator, on_delete=models.CASCADE, related_name="listings")
    trek = models.ForeignKey(Trek, on_delete=models.CASCADE, related_name="listings")
    page_url = models.URLField(help_text="Operator's trek page (TTH batches are read from it).")
    external_id = models.CharField(
        max_length=50, blank=True,
        help_text="Operator's own trek id. Indiahikes needs this (e.g. Rupin Pass = 2).",
    )
    duration_days = models.PositiveIntegerField(null=True, blank=True)
    fee_inr = models.PositiveIntegerField(null=True, blank=True, help_text="Trek fee before GST.")
    single_occupancy_note = models.CharField(max_length=200, blank=True)
    needs_daily_refresh = models.BooleanField(
        default=True, help_text="Uncheck for local/govt treks that have no fetchable batches.",
    )
    active = models.BooleanField(default=True)
    last_fetched_at = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)

    class Meta:
        unique_together = [("operator", "trek")]
        ordering = ["trek__sort_order", "operator__name"]

    def __str__(self):
        return f"{self.trek} · {self.operator.short_name}"


class Batch(models.Model):
    class Status(models.TextChoices):
        OPEN = "open", "Open"
        LAST = "last", "Last few seats"
        WAITLIST = "waitlist", "Waitlist"
        FULL = "full", "Full"
        CLOSED = "closed", "Closed"
        UNKNOWN = "unknown", "Listed, no status"

    listing = models.ForeignKey(OperatorTrek, on_delete=models.CASCADE, related_name="batches")
    start_date = models.DateField()
    end_date = models.DateField()
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.UNKNOWN)
    seats_left = models.IntegerField(null=True, blank=True)
    raw_status = models.CharField(max_length=100, blank=True, help_text="Status text as the operator gave it.")
    external_id = models.CharField(max_length=50, blank=True)
    first_seen_at = models.DateTimeField(default=timezone.now)
    last_seen_at = models.DateTimeField(default=timezone.now)
    removed = models.BooleanField(
        default=False, help_text="Set when a refresh no longer finds this batch on the operator's site.",
    )

    class Meta:
        ordering = ["start_date"]
        indexes = [models.Index(fields=["start_date"])]
        constraints = [
            models.UniqueConstraint(
                fields=["listing", "external_id"],
                condition=~models.Q(external_id=""),
                name="batch_unique_external_id",
            ),
            models.UniqueConstraint(
                fields=["listing", "start_date", "end_date"],
                condition=models.Q(external_id=""),
                name="batch_unique_dates",
            ),
        ]

    def __str__(self):
        return f"{self.listing} {self.start_date:%d %b}–{self.end_date:%d %b %Y}"

    @property
    def fly_in_date(self):
        return self.start_date - timezone.timedelta(days=1)

    @property
    def fly_out_date(self):
        return self.end_date + timezone.timedelta(days=1)


class FlightQuote(models.Model):
    """Cheapest return fare found for one origin/destination/date pair."""

    origin = models.ForeignKey(Airport, on_delete=models.PROTECT, related_name="+")
    destination = models.ForeignKey(Airport, on_delete=models.PROTECT, related_name="quotes")
    depart_date = models.DateField()
    return_date = models.DateField()
    price = models.PositiveIntegerField()
    currency = models.CharField(max_length=3, default="INR")
    airlines = models.CharField(max_length=200, blank=True)
    stops = models.PositiveSmallIntegerField(null=True, blank=True)
    provider = models.CharField(max_length=30)
    fetched_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["destination", "depart_date"]
        indexes = [models.Index(fields=["destination", "depart_date", "return_date"])]

    def __str__(self):
        return f"{self.origin_id}→{self.destination_id} {self.depart_date}/{self.return_date} {self.currency} {self.price}"


class PlannedBatch(models.Model):
    """A batch you've pencilled in; outlined on the calendar."""

    batch = models.OneToOneField(Batch, on_delete=models.CASCADE, related_name="plan")
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Planned: {self.batch}"


class RefreshRun(models.Model):
    class State(models.TextChoices):
        RUNNING = "running", "Running"
        OK = "ok", "Completed"
        PARTIAL = "partial", "Completed with errors"
        FAILED = "failed", "Failed"

    started_at = models.DateTimeField(default=timezone.now)
    finished_at = models.DateTimeField(null=True, blank=True)
    state = models.CharField(max_length=10, choices=State.choices, default=State.RUNNING)
    batches_seen = models.PositiveIntegerField(default=0)
    batches_new = models.PositiveIntegerField(default=0)
    quotes_fetched = models.PositiveIntegerField(default=0)
    log = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"Refresh {self.started_at:%d %b %H:%M} ({self.get_state_display()})"

    def write(self, line):
        stamp = timezone.localtime().strftime("%H:%M:%S")
        self.log += f"[{stamp}] {line}\n"


class PageView(models.Model):
    path = models.CharField(max_length=500)
    method = models.CharField(max_length=10, default="GET")
    status_code = models.PositiveSmallIntegerField(default=200)
    user_agent = models.TextField(blank=True)
    referrer = models.URLField(max_length=1000, blank=True)
    ip = models.GenericIPAddressField(null=True, blank=True)
    session_key = models.CharField(max_length=40, blank=True)
    timestamp = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-timestamp"]
        indexes = [models.Index(fields=["path", "timestamp"])]

    def __str__(self):
        return f"{self.method} {self.path} {self.status_code} @ {self.timestamp:%d %b %H:%M}"
