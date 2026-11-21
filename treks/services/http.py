import primp
import requests
from django.conf import settings


class SourceError(Exception):
    """A data source could not be read; the message is shown in the refresh log."""


def session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": settings.TREKS["USER_AGENT"],
        "Accept-Language": "en-IN,en;q=0.9",
    })
    return s


def browser_client():
    """A primp client that impersonates Chrome's TLS fingerprint.

    Use for sites behind Vercel/Cloudflare JS challenges.
    """
    return primp.Client(
        impersonate="chrome",
        timeout=settings.TREKS["HTTP_TIMEOUT"],
    )


def get(sess, url, **kw):
    kw.setdefault("timeout", settings.TREKS["HTTP_TIMEOUT"])
    try:
        r = sess.get(url, **kw)
    except requests.RequestException as e:
        raise SourceError(f"Could not reach {url}: {e}") from e
    if r.status_code != 200:
        hint = ""
        if r.status_code == 429 and "vercel" in r.headers.get("server", "").lower():
            hint = " (blocked by Vercel bot protection)"
        elif r.status_code == 429:
            hint = " (rate-limited — too many requests)"
        elif r.status_code in (403, 503) and "cloudflare" in r.text.lower():
            hint = " (blocked by Cloudflare's bot check)"
        raise SourceError(f"{url} returned HTTP {r.status_code}{hint}")
    return r
