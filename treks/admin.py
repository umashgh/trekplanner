import threading

from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import path, reverse

from .models import Airport, Batch, FlightQuote, Operator, OperatorTrek, PageView, PlannedBatch, RefreshRun, Trek
from .services.refresh import run_refresh
from .views import _refresh_lock


@admin.register(Operator)
class OperatorAdmin(admin.ModelAdmin):
    list_display = ("name", "short_name", "source", "active")
    list_editable = ("active",)


@admin.register(Airport)
class AirportAdmin(admin.ModelAdmin):
    list_display = ("iata", "city")


class ListingInline(admin.TabularInline):
    model = OperatorTrek
    extra = 0
    fields = ("operator", "page_url", "external_id", "duration_days", "fee_inr", "needs_daily_refresh", "active", "last_fetched_at", "last_error")
    readonly_fields = ("last_fetched_at", "last_error")


@admin.register(Trek)
class TrekAdmin(admin.ModelAdmin):
    list_display = ("name", "country", "state", "gateway_airport", "max_altitude_m", "booking_type", "difficulty", "best_months", "sort_order", "active")
    list_editable = ("difficulty", "best_months", "sort_order", "active")
    list_filter = ("booking_type", "country", "state", "difficulty", "active")
    inlines = [ListingInline]


@admin.register(OperatorTrek)
class OperatorTrekAdmin(admin.ModelAdmin):
    list_display = ("trek", "operator", "external_id", "duration_days", "fee_inr", "needs_daily_refresh", "active", "last_fetched_at", "last_error")
    list_filter = ("operator", "needs_daily_refresh", "active")


@admin.register(Batch)
class BatchAdmin(admin.ModelAdmin):
    list_display = ("listing", "start_date", "end_date", "status", "seats_left", "raw_status", "removed", "last_seen_at")
    list_filter = ("status", "removed", "listing__operator", "listing__trek")
    date_hierarchy = "start_date"


@admin.register(FlightQuote)
class FlightQuoteAdmin(admin.ModelAdmin):
    list_display = ("destination", "depart_date", "return_date", "price", "airlines", "stops", "provider", "fetched_at")
    list_filter = ("destination", "provider")
    date_hierarchy = "depart_date"


@admin.register(PlannedBatch)
class PlannedBatchAdmin(admin.ModelAdmin):
    list_display = ("batch", "note", "created_at")


@admin.register(RefreshRun)
class RefreshRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "state", "batches_seen", "batches_new", "quotes_fetched", "finished_at")
    readonly_fields = [f.name for f in RefreshRun._meta.fields]
    change_list_template = "admin/treks/refreshrun/change_list.html"

    def get_urls(self):
        custom = [
            path("run-refresh/", self.admin_site.admin_view(self.run_refresh_view), name="treks_refreshrun_run_refresh"),
        ]
        return custom + super().get_urls()

    def run_refresh_view(self, request):
        if not _refresh_lock.acquire(blocking=False):
            messages.warning(request, "A refresh is already running.")
            return HttpResponseRedirect(reverse("admin:treks_refreshrun_changelist"))
        try:
            run = run_refresh()
        finally:
            _refresh_lock.release()
        messages.success(request, f"{run.get_state_display()}: {run.batches_seen} batches, {run.batches_new} new, {run.quotes_fetched} fares")
        return HttpResponseRedirect(reverse("admin:treks_refreshrun_changelist"))


@admin.register(PageView)
class PageViewAdmin(admin.ModelAdmin):
    list_display = ("timestamp", "method", "path", "status_code", "ip")
    list_filter = ("method", "status_code")
    date_hierarchy = "timestamp"
    readonly_fields = [f.name for f in PageView._meta.fields]

    def has_add_permission(self, request):
        return False
