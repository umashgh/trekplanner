import json
from datetime import date, timedelta
from unittest import mock

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from treks.models import Batch, FlightQuote, OperatorTrek, RefreshRun
from treks.services import hgn, indiahikes, tth
from treks.services.flights import Fare, SerpApiGoogleFlights

# Shape captured from booking.indiahikes.com trek-overview (Buran Ghati, Nov 2026)
IH_PAYLOAD = {
    "trekData": {"name": "Buran Ghati", "duration": 8, "state": "Himachal Pradesh"},
    "availableBatchesList": [
        {"batchId": 1, "startDate": "2027-05-11T00:00:00", "endDate": "2027-05-18T00:00:00",
         "status": "ACTIVE", "availableSlots": 19, "availableWaitingListSlots": 5,
         "currentWaitingListStatus": 0, "bookingsClosed": False, "privateBatch": False},
        {"batchId": 2, "startDate": "2027-05-13T00:00:00", "endDate": "2027-05-20T00:00:00",
         "status": "ACTIVE", "availableSlots": 4, "bookingsClosed": False, "privateBatch": False},
        {"batchId": 3, "startDate": "2027-05-15T00:00:00", "endDate": "2027-05-22T00:00:00",
         "status": "FULL", "availableSlots": 0, "bookingsClosed": True, "privateBatch": False},
        {"batchId": 4, "startDate": "2027-06-05T00:00:00", "endDate": "2027-06-12T00:00:00",
         "status": "WAITING_LIST", "availableSlots": 0, "bookingsClosed": False, "privateBatch": False},
        {"batchId": 5, "startDate": "2027-06-06T00:00:00", "endDate": "2027-06-13T00:00:00",
         "status": "ACTIVE", "availableSlots": 20, "privateBatch": True},
    ],
}


def tth_html(deps):
    """Build a page the way TTH's Next.js server embeds departures."""
    payload = '4b:["$","$L81",null,{"trekId":52,"initialMonthData":[{"monthYear":"May-2027","departures":' \
              + json.dumps(deps, separators=(",", ":")) + "}]}]"
    escaped = json.dumps(payload)[1:-1]  # JS string literal body
    half = len(escaped) // 2
    # split across two push() calls, as real pages do
    while escaped[half - 1] == "\\":
        half += 1
    return ("<html><body><span>May 8 – May 14</span>"
            f'<script>self.__next_f.push([1,"{escaped[:half]}"])</script>'
            f'<script>self.__next_f.push([1,"{escaped[half:]}"])</script></body></html>')


TTH_DEPS = [
    {"departureId": 9152, "startDate": "2027-05-08T00:00:00", "endDate": "2027-05-14T00:00:00",
     "seats": 25, "remainiBooking": 25, "autoStatus": "Open", "mannualStatus": "Default",
     "isFillingFast": False, "isDeleted": None, "isApproved": True},
    {"departureId": 9153, "startDate": "2027-05-14T00:00:00", "endDate": "2027-05-20T00:00:00",
     "seats": 25, "remainiBooking": 12, "autoStatus": "Open", "mannualStatus": "Default",
     "isFillingFast": False, "isDeleted": None, "isApproved": True},
    {"departureId": 9154, "startDate": "2027-05-16T00:00:00", "endDate": "2027-05-22T00:00:00",
     "seats": 25, "remainiBooking": 8, "autoStatus": "Open", "mannualStatus": "Default",
     "isFillingFast": True, "isDeleted": None, "isApproved": True},
    {"departureId": 9155, "startDate": "2027-04-23T00:00:00", "endDate": "2027-05-01T00:00:00",
     "seats": 25, "remainiBooking": 0, "autoStatus": "Open", "mannualStatus": "Full",
     "isFillingFast": False, "isDeleted": None, "isApproved": True},
    {"departureId": 9156, "startDate": "2027-05-30T00:00:00", "endDate": "2027-06-05T00:00:00",
     "seats": 25, "remainiBooking": 25, "autoStatus": "Open", "mannualStatus": "Default",
     "isDeleted": True, "isApproved": True},
]


class IndiahikesParserTests(TestCase):
    def test_statuses_and_private_batches(self):
        trek, batches = indiahikes.parse_overview(IH_PAYLOAD)
        self.assertEqual(trek["duration"], 8)
        self.assertEqual([b.status for b in batches], ["open", "last", "full", "waitlist"])
        self.assertEqual(batches[0].start, date(2027, 5, 11))
        self.assertEqual(batches[1].seats_left, 4)


class TTHParserTests(TestCase):
    def test_parses_embedded_departures(self):
        batches = tth.parse_page(tth_html(TTH_DEPS))
        self.assertEqual(len(batches), 4)  # deleted one dropped
        by_start = {b.start: b for b in batches}
        self.assertEqual(by_start[date(2027, 5, 8)].status, "open")
        self.assertEqual(by_start[date(2027, 5, 14)].status, "last")
        self.assertEqual(by_start[date(2027, 5, 16)].status, "last")
        self.assertEqual(by_start[date(2027, 4, 23)].status, "full")
        self.assertEqual(by_start[date(2027, 4, 23)].raw_status, "Full")

    def test_page_without_data_raises(self):
        with self.assertRaises(tth.SourceError):
            tth.parse_page("<html>nothing</html>")


HGN_HTML = """<html><body>
<section class="fixed-departure">
<div class="departure-main">
<h4>Fixed Departure of 2026 Autumn</h4>
<table class="table table-striped">
<thead><tr><th>Expedition Name</th><th>Departure Date</th></tr></thead>
<tbody>
<tr>
  <td>Everest Base Camp Trek</td>
  <td>
    <a href="https://www.himalayanguides.com/fixed-departures/3">September 25 - October 10</a>
    <a href="https://www.himalayanguides.com/fixed-departures/9">October 7 - October 23</a>
  </td>
</tr>
<tr>
  <td>Annapurna Circuit Trek</td>
  <td>
    <a href="https://www.himalayanguides.com/fixed-departures/35">October 1 - October 14</a>
  </td>
</tr>
<tr>
  <td>Mera Peak</td>
  <td>
    <a href="https://www.himalayanguides.com/fixed-departures/2">October 15 - November 6</a>
  </td>
</tr>
</tbody></table></div>
<div class="departure-main">
<h4>Fixed Departure of 2027 Spring</h4>
<table class="table table-striped">
<thead><tr><th>Expedition Name</th><th>Departure Date</th></tr></thead>
<tbody>
<tr>
  <td>Everest Base Camp Trek</td>
  <td>
    <a href="https://www.himalayanguides.com/fixed-departures/63">16 March - 01 April</a>
  </td>
</tr>
</tbody></table></div>
</section></body></html>"""


class HGNParserTests(TestCase):
    def test_parses_all_treks(self):
        result = hgn.parse_page(HGN_HTML)
        self.assertIn("everest base camp", result)
        self.assertIn("annapurna circuit", result)
        self.assertIn("mera peak", result)

    def test_ebc_batches(self):
        result = hgn.parse_page(HGN_HTML)
        ebc = result["everest base camp"]
        self.assertEqual(len(ebc), 3)  # 2 autumn + 1 spring
        self.assertEqual(ebc[0].start, date(2026, 9, 25))
        self.assertEqual(ebc[0].end, date(2026, 10, 10))
        self.assertEqual(ebc[0].external_id, "3")
        # spring batch uses 2027
        self.assertEqual(ebc[2].start, date(2027, 3, 16))
        self.assertEqual(ebc[2].end, date(2027, 4, 1))

    def test_all_batches_status_open(self):
        result = hgn.parse_page(HGN_HTML)
        for batches in result.values():
            for b in batches:
                self.assertEqual(b.status, "open")
                self.assertIsNone(b.seats_left)

    def test_page_without_table_returns_empty(self):
        result = hgn.parse_page("<html>nothing</html>")
        self.assertEqual(result, {})

    def test_normalise(self):
        self.assertEqual(hgn._normalise("Everest Base Camp Trek"), "everest base camp")
        self.assertEqual(hgn._normalise("Manaslu Circuit Trekking"), "manaslu circuit")
        self.assertEqual(hgn._normalise("Mera Peak"), "mera peak")
        self.assertEqual(hgn._normalise("Ama Dablam Expedition"), "ama dablam")


class SerpApiParseTests(TestCase):
    def test_picks_cheapest_option(self):
        data = {"best_flights": [{"price": 21000, "flights": [{"airline": "IndiGo"}]}],
                "other_flights": [{"price": 19500, "flights": [{"airline": "Akasa Air"}, {"airline": "Akasa Air"}]}]}
        fare = SerpApiGoogleFlights.parse(data)
        self.assertEqual((fare.price, fare.airlines, fare.stops), (19500, "Akasa Air", 1))

    def test_no_options(self):
        self.assertIsNone(SerpApiGoogleFlights.parse({}))


@override_settings(TREKS={
    "HOME_AIRPORT": "BLR", "EXPENSIVE_FLIGHT_PCT": 25, "FLIGHT_PROVIDER": "serpapi", "SERPAPI_KEY": "x",
    "FLIGHT_SAMPLING": "weekly", "FLIGHT_CACHE_HOURS": 12, "FLIGHT_HORIZON_DAYS": 900,
    "HTTP_TIMEOUT": 5, "USER_AGENT": "test"})
class RefreshFlowTests(TestCase):
    def setUp(self):
        call_command("seed_treks", stdout=open("/dev/null", "w"))
        # Only one listing per operator active, to keep the test small
        OperatorTrek.objects.exclude(trek__name="Buran Ghati").update(active=False)

    def _ih(self, trek_id, sess=None):
        return indiahikes.parse_overview(IH_PAYLOAD)

    def _tth(self, url, sess=None):
        return tth.parse_page(tth_html(TTH_DEPS))

    def test_refresh_stores_batches_flights_and_flags(self):
        prices = iter([30000, 22000, 22000, 22000, 22000, 22000])
        with mock.patch("treks.services.indiahikes.fetch_batches", self._ih), \
             mock.patch("treks.services.tth.fetch_batches", self._tth), \
             mock.patch.object(SerpApiGoogleFlights, "cheapest_return",
                               lambda self, o, d, dep, ret: Fare(next(prices), "INR", "IndiGo", 0)):
            run = RefreshRun.objects.none()
            from treks.services.refresh import run_refresh
            run = run_refresh()

        self.assertEqual(run.state, RefreshRun.State.OK, run.log)
        self.assertEqual(Batch.objects.count(), 4 + 4)
        self.assertEqual(Batch.objects.filter(status="last").count(), 3)
        self.assertGreater(FlightQuote.objects.count(), 0)

        resp = self.client.get(reverse("treks:calendar"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("Buran Ghati", html)
        self.assertIn(">$</td>", html)  # the 30,000 week is 36% above 22,000

        # second refresh: a batch disappears -> marked removed, fares reused from cache
        def fewer(url, sess=None):
            return tth.parse_page(tth_html(TTH_DEPS[:1]))
        with mock.patch("treks.services.indiahikes.fetch_batches", self._ih), \
             mock.patch("treks.services.tth.fetch_batches", fewer), \
             mock.patch.object(SerpApiGoogleFlights, "cheapest_return",
                               side_effect=AssertionError("should use cache")):
            run2 = run_refresh()
        self.assertEqual(run2.state, RefreshRun.State.OK, run2.log)
        self.assertEqual(Batch.objects.filter(removed=True).count(), 3)

    def test_source_error_is_logged_not_fatal(self):
        def boom(*a, **kw):
            raise indiahikes.SourceError("HTTP 403 (blocked by Cloudflare's bot check)")
        with mock.patch("treks.services.indiahikes.fetch_batches", boom), \
             mock.patch("treks.services.tth.fetch_batches", self._tth):
            from treks.services.refresh import run_refresh
            run = run_refresh(include_flights=False)
        self.assertEqual(run.state, RefreshRun.State.PARTIAL)
        self.assertIn("Cloudflare", run.log)
        self.assertEqual(Batch.objects.count(), 4)

    def test_toggle_plan(self):
        with mock.patch("treks.services.tth.fetch_batches", self._tth), \
             mock.patch("treks.services.indiahikes.fetch_batches", self._ih):
            from treks.services.refresh import run_refresh
            run_refresh(include_flights=False)
        b = Batch.objects.first()
        url = reverse("treks:toggle_plan", args=[b.pk])
        self.assertTrue(self.client.post(url).json()["planned"])
        self.assertFalse(self.client.post(url).json()["planned"])
