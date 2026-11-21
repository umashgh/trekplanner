"""Flight price providers.

Kayak has no public API and serves results through JavaScript behind bot
protection, so it can't be read by a server-side refresh. Two Google Flights
providers are available:

- SerpApi (paid, needs SERPAPI_KEY)
- fast-flights (free, scrapes Google Flights directly via primp/TLS
  fingerprinting; add a delay between requests to stay under rate limits)
"""
import time
from dataclasses import dataclass
from datetime import date

from django.conf import settings

from .http import SourceError, get, session


@dataclass
class Fare:
    price: int
    currency: str
    airlines: str
    stops: int | None


class FlightProvider:
    name = "base"

    def cheapest_return(self, origin: str, dest: str, depart: date, ret: date) -> Fare | None:
        raise NotImplementedError


class NoFlights(FlightProvider):
    name = "none"

    def cheapest_return(self, *a, **kw):
        return None


class SerpApiGoogleFlights(FlightProvider):
    name = "serpapi"
    URL = "https://serpapi.com/search.json"

    def __init__(self, api_key):
        if not api_key:
            raise SourceError("SERPAPI_KEY is not set, so flight prices were skipped.")
        self.key = api_key
        self.sess = session()

    def cheapest_return(self, origin, dest, depart, ret):
        r = get(self.sess, self.URL, params={
            "engine": "google_flights",
            "departure_id": origin,
            "arrival_id": dest,
            "outbound_date": depart.isoformat(),
            "return_date": ret.isoformat(),
            "type": "1",          # round trip
            "stops": "2",         # one stop or fewer
            "currency": "INR",
            "hl": "en",
            "gl": "in",
            "api_key": self.key,
        })
        data = r.json()
        if data.get("error"):
            raise SourceError(f"SerpApi: {data['error']}")
        return self.parse(data)

    @staticmethod
    def parse(data):
        options = (data.get("best_flights") or []) + (data.get("other_flights") or [])
        options = [o for o in options if isinstance(o.get("price"), (int, float))]
        if not options:
            return None
        best = min(options, key=lambda o: o["price"])
        legs = best.get("flights") or []
        airlines = ", ".join(dict.fromkeys(l.get("airline", "") for l in legs if l.get("airline")))
        return Fare(price=int(best["price"]), currency="INR", airlines=airlines,
                    stops=max(len(legs) - 1, 0) if legs else None)


class FastFlightsProvider(FlightProvider):
    """Google Flights via the fast-flights library (no API key needed)."""

    name = "fast-flights"
    REQUEST_DELAY = 3  # seconds between requests to avoid rate limiting

    def __init__(self):
        self._last_request = 0.0

    def _throttle(self):
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.REQUEST_DELAY:
            time.sleep(self.REQUEST_DELAY - elapsed)
        self._last_request = time.monotonic()

    def cheapest_return(self, origin, dest, depart, ret):
        try:
            from fast_flights import FlightQuery, create_filter, get_flights
            from fast_flights.exceptions import FlightsNotFound
        except ImportError as e:
            raise SourceError("fast-flights is not installed: pip install fast-flights") from e

        query = create_filter(
            flights=[
                FlightQuery(date=depart.isoformat(), from_airport=origin, to_airport=dest),
                FlightQuery(date=ret.isoformat(), from_airport=dest, to_airport=origin),
            ],
            trip="round-trip",
            seat="economy",
            currency="INR",
            max_stops=1,
        )

        self._throttle()
        try:
            results = get_flights(query)
        except FlightsNotFound:
            return None
        except (IndexError, KeyError, TypeError) as e:
            raise SourceError(f"fast-flights: failed to parse Google Flights response ({e})") from e
        except Exception as e:
            raise SourceError(f"fast-flights: {e}") from e

        if not results:
            return None

        best = min(results, key=lambda f: f.price)
        airlines = ", ".join(dict.fromkeys(best.airlines))
        stops = max(len(best.flights) - 1, 0) if best.flights else None
        return Fare(price=int(best.price), currency="INR", airlines=airlines, stops=stops)


PROVIDERS = {"serpapi": SerpApiGoogleFlights, "fast-flights": FastFlightsProvider, "none": NoFlights}


def get_provider():
    cfg = settings.TREKS
    name = cfg["FLIGHT_PROVIDER"]
    if name == "serpapi":
        return SerpApiGoogleFlights(cfg["SERPAPI_KEY"])
    if name in PROVIDERS:
        return PROVIDERS[name]()
    raise SourceError(f"Unknown flight provider '{name}'")
