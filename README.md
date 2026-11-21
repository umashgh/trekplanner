# Trek Planner

A small Django site that tracks Himalayan trek batches from Indiahikes and
Trek The Himalayas, prices the flights to each trek's gateway airport, and
shows everything on one month-by-month calendar.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py seed_treks          # airports, operators, the six treks
python manage.py createsuperuser     # for /admin
python manage.py runserver           # flight prices use fast-flights (free) by default
```

Open http://127.0.0.1:8000 and press **Refresh all data**. The same refresh
runs from the command line with `python manage.py refresh_data`
(add `--no-flights` to skip fares). A refresh takes a while because it calls
each operator once per trek and then the flight API; the page waits for it.

## Where the data comes from

| Source | How it's read |
|---|---|
| Indiahikes | Public JSON API their trek pages call: `booking.indiahikes.com/tms-service/public-api/v1/trek-overview?trekId=…`. Each trek needs its numeric id in the listing's **external id** (seeded for the six treks). Find others with `python manage.py find_ih_ids --match everest`. |
| Trek The Himalayas | Each trek page embeds its departures as JSON in the Next.js payload; the parser reads `startDate`, `endDate`, `remainiBooking` and the status fields. |
| Himalayan Guides Nepal | The `/fixed-departures` page lists all treks in HTML tables (one per season). The parser reads trek names and date ranges from `<a>` tags. No availability or pricing data — all batches show as "open". |
| Bikat | Not fetched. Its dates only appear inside its booking flow, so the operator is seeded as inactive. Add batches by hand in the admin if you want them on the calendar. |
| Flights | Google Flights via [fast-flights](https://github.com/AWeirdDev/flights) (free, no API key) or [SerpApi](https://serpapi.com/google-flights-api) (paid, needs `SERPAPI_KEY`). Set `TREKS_FLIGHT_PROVIDER=fast-flights` (default) or `serpapi`. fast-flights adds a 3-second delay between requests to stay under rate limits. |

Both operator sources are unofficial. If a site changes its layout or API,
that listing shows the error on the calendar's Sources panel and in the
Refresh log, and the rest of the refresh carries on.

## How flights are priced

- Fly in the day before a batch starts, fly back the day after it ends,
  from `TREKS_HOME_AIRPORT` (default BLR), one stop or fewer.
- `TREKS_FLIGHT_SAMPLING=weekly` (default) prices one batch per airport per
  week and applies that fare to the other batches that week: about 40 searches
  for the six treks. `per_batch` prices every distinct date pair (hundreds of
  searches).
- Fares fetched in the last `TREKS_FLIGHT_CACHE_HOURS` (default 12) are reused.
- A batch gets a **$** when its fare is `TREKS_EXPENSIVE_PCT` (default 25)
  percent or more above the cheapest fare stored for the same airport.

## Models

- **Operator**: a trekking company and which fetcher it uses.
- **Airport**: home and gateway airports.
- **Trek**: a route, with its gateway airport.
- **OperatorTrek**: one operator's listing of one trek (page URL, external id, fee, last fetch/error).
- **Batch**: a departure (dates, status, seats left). Batches that vanish from the operator's site are marked `removed`, not deleted.
- **FlightQuote**: cheapest return fare for an airport and date pair, kept as history.
- **PlannedBatch**: batches you've pencilled in (click a square on the calendar).
- **RefreshRun**: each refresh with counts and a log.

## Settings (environment variables)

`DJANGO_SECRET_KEY`, `DJANGO_DEBUG`, `DJANGO_ALLOWED_HOSTS`,
`TREKS_HOME_AIRPORT`, `TREKS_EXPENSIVE_PCT`, `TREKS_FLIGHT_PROVIDER`
(`fast-flights`, `serpapi`, or `none`), `SERPAPI_KEY` (only if using serpapi),
`TREKS_FLIGHT_SAMPLING`, `TREKS_FLIGHT_CACHE_HOURS`.

## Tests

`python manage.py test treks` covers both parsers (with payloads in the shape
the sites return), the refresh flow, removed batches, fare caching and the
$ flag.
