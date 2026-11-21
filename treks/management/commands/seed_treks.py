import re

from django.core.management.base import BaseCommand

from treks.models import Airport, Operator, OperatorTrek, Trek
from treks.services.hgn import PAGE_URL

# ---------------------------------------------------------------------------
# Airports
# ---------------------------------------------------------------------------
AIRPORTS = {
    "BLR": "Bengaluru", "IXB": "Bagdogra", "KTM": "Kathmandu",
    "DED": "Dehradun", "IXC": "Chandigarh", "BOM": "Mumbai",
    "PNQ": "Pune", "IXE": "Mangalore", "COK": "Kochi",
    "IXM": "Madurai", "CJB": "Coimbatore", "CCJ": "Kozhikode",
    "CNN": "Kannur", "SXR": "Srinagar", "IXL": "Leh",
    "KUU": "Kullu", "SLV": "Shimla", "DHM": "Dharamshala",
    "GOI": "Goa", "NAG": "Nagpur", "BHO": "Bhopal",
    "RPR": "Raipur", "JGB": "Jagdalpur", "UDR": "Udaipur",
    "HSR": "Rajkot", "BBI": "Bhubaneswar", "IXR": "Ranchi",
    "GAU": "Guwahati", "SHL": "Shillong", "DMU": "Dimapur",
    "PHH": "Pokhara", "KEP": "Nepalgunj", "BDP": "Bhadrapur",
    "PGH": "Pantnagar", "TRV": "Trivandrum", "TRZ": "Tiruchirappalli",
    "VTZ": "Vizag", "HBX": "Hubli", "JLR": "Jabalpur",
}

# ---------------------------------------------------------------------------
# Operator/trek link overrides for treks that had existing entries
# ---------------------------------------------------------------------------
# IH entries: trek_name -> (ih_external_id, ih_slug, tth_slug, bikat_slug)
EXISTING_IH = {
    "Sandakphu–Phalut": ("17", "sandakphu-phalut", "sandakphu-trek", "sandakphu-trek"),
    "Goecha La": ("3", "goechala", "goechala-trek", "goechala-trek"),
    "Annapurna Base Camp": ("67", "annapurna-base-camp", "annapurna-base-camp-trek", "Annapurna-Base-Camp-Trek"),
    "Rupin Pass": ("2", "rupin-pass", "rupin-pass-trek", "rupin-pass-trek"),
    "Buran Ghati": ("22", "buran-ghati", "buran-ghati-pass-trek", "buran-ghati-trek"),
    "Gaumukh–Tapovan": ("64", "gaumukh-tapovan", "gaumukh-tapovan-trek", "gaumukh-tapovan-trek"),
}

# HGN entries: trek_name -> hgn_external_id
EXISTING_HGN = {
    "Everest Base Camp": "Everest Base Camp Trek",
    "Annapurna Circuit": "Annapurna Circuit Trek",
    "Langtang Valley": "Langtang Valley Trek",
    "Manaslu Circuit": "Manaslu Circuit Trekking",
    "Upper Mustang": "Upper Mustang Trek",
    "Kanchenjunga Base Camp": "Kanchenjunga Base Camp Trek",
    "Everest Three Passes": "Everest Three Passes Trek",
    "Annapurna Base Camp": "Annapurna Base Camp Trek",
}

# Old name -> new name for treks being renamed
RENAMES = {
    "Sandakphu": "Sandakphu–Phalut",
    "Goechala": "Goecha La",
    "ABC": "Annapurna Base Camp",
    "EBC": "Everest Base Camp",
    "Kanchenjunga BC": "Kanchenjunga Base Camp",
    "Gaumukh Tapovan": "Gaumukh–Tapovan",
}

# Old treks from the original seed that are superseded by new entries
OBSOLETE = ["Ali Bedni Bugyal", "Valley of Flowers", "Deoriatal Chandrashila", "Dodital", "Phulara Ridge"]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

USD_TO_INR = 84


def _parse_month_range(start_str, end_str):
    s = MONTH_MAP.get(start_str.lower()[:3])
    e = MONTH_MAP.get(end_str.lower()[:3])
    if s is None or e is None:
        return []
    if s <= e:
        return list(range(s, e + 1))
    return list(range(s, 13)) + list(range(1, e + 1))


def parse_best_months(text):
    if not text:
        return ""
    t = text.strip()
    if t.lower() == "all year":
        return ",".join(str(m) for m in range(1, 13))
    # If there's a "best X–Y" portion, prefer that
    best_match = re.search(r"best\s+([A-Za-z]+)\s*[–\-]\s*([A-Za-z]+)", t)
    if best_match:
        months = _parse_month_range(best_match.group(1), best_match.group(2))
        return ",".join(str(m) for m in sorted(set(months))) if months else ""
    # Strip parentheticals and qualifiers
    t = re.sub(r"\(.*?\)", "", t)
    t = re.sub(r"mid-?", "", t, flags=re.IGNORECASE)
    t = t.replace(" only", "").strip()
    # "Winter (expected)" -> just try to parse
    if t.lower().startswith("winter"):
        return "11,12,1,2"
    months = set()
    for part in re.split(r"[,;]", t):
        part = part.strip()
        if not part:
            continue
        rng = re.match(r"([A-Za-z]+)\s*[–\-]\s*([A-Za-z]+)", part)
        if rng:
            months.update(_parse_month_range(rng.group(1), rng.group(2)))
        else:
            m = MONTH_MAP.get(part.lower()[:3])
            if m:
                months.add(m)
    return ",".join(str(m) for m in sorted(months)) if months else ""


def parse_duration(val):
    if val is None:
        return None, None
    if isinstance(val, (int, float)):
        d = int(val)
        return d, d
    s = str(val).strip()
    # "Multi-day"
    if s.lower().startswith("multi"):
        return 3, 5
    # Strip parenthetical like "1 (6 hrs, 11 km)" or "2 (camp stay)"
    s = re.sub(r"\s*\(.*?\)", "", s).strip()
    # "12+"
    plus = re.match(r"(\d+)\+", s)
    if plus:
        lo = int(plus.group(1))
        return lo, lo + 2
    # "5–6" or "5-6"
    rng = re.match(r"(\d+)\s*[–\-]\s*(\d+)", s)
    if rng:
        return int(rng.group(1)), int(rng.group(2))
    # single number
    try:
        d = int(s)
        return d, d
    except ValueError:
        return None, None


def parse_cost(text):
    if not text:
        return None, None
    t = text.strip()
    low = t.lower()
    if low in ("on request", "check site", "opening soon", "check yhai"):
        return None, None
    if low == "free":
        return 0, 0
    if low == "budget":
        return 0, 3000
    if low in ("permit fee", "entry fee"):
        return 0, 500
    # Handle extra permit costs like "+ $500 permit"
    extra = 0
    permit_match = re.search(r"\+\s*\$\s*([\d,]+)\s*permit", t, re.IGNORECASE)
    if permit_match:
        extra = int(permit_match.group(1).replace(",", "")) * USD_TO_INR
        t = t[: permit_match.start()].strip()
    permit_match2 = re.search(r"\+\s*permits?", t, re.IGNORECASE)
    if permit_match2:
        t = t[: permit_match2.start()].strip()
    # Remove GST mentions
    t = re.sub(r"\s*\+?\s*\d+%\s*GST", "", t, flags=re.IGNORECASE).strip()
    # Remove "(incl. ...)" or "(check)" etc
    t = re.sub(r"\(.*?\)", "", t).strip()
    # "$3500+ incl." -> strip "incl."
    t = re.sub(r"\s*incl\.?.*", "", t, flags=re.IGNORECASE).strip()
    # Dollar amounts
    usd_range = re.match(r"\$\s*([\d,]+)\s*[–\-]\s*\$?\s*([\d,]+)", t)
    if usd_range:
        lo = int(usd_range.group(1).replace(",", "")) * USD_TO_INR
        hi = int(usd_range.group(2).replace(",", "")) * USD_TO_INR + extra
        return lo, hi
    usd_single = re.match(r"\$\s*([\d,]+)\+?", t)
    if usd_single:
        val = int(usd_single.group(1).replace(",", "")) * USD_TO_INR
        return val, val + extra
    # Rupee range  ₹11,000–15,000
    inr_range = re.search(r"[₹₹]?\s*([\d,]+)\s*[–\-]\s*[₹₹]?\s*([\d,]+)", t)
    if inr_range:
        lo = int(inr_range.group(1).replace(",", ""))
        hi = int(inr_range.group(2).replace(",", "")) + extra
        return lo, hi
    # Single rupee amount  ₹1,999
    inr_single = re.search(r"[₹₹]?\s*([\d,]+)", t)
    if inr_single:
        val = int(inr_single.group(1).replace(",", ""))
        return val, val
    # Fallback: try parsing as a plain number
    t2 = re.sub(r"[^\d]", "", t)
    if t2:
        val = int(t2)
        return val, val
    return None, None


def guess_difficulty(alt, days_max, trek_name, state):
    overrides = {
        "Alang–Madan–Kulang": 3,
        "Pin Parvati Pass": 3,
        "Borasu Pass": 3,
    }
    if trek_name in overrides:
        return overrides[trek_name]
    if alt is None:
        alt = 0
    if days_max is None:
        days_max = 1
    if alt > 4500 or days_max > 7:
        return 3
    if alt >= 3500 and days_max <= 7:
        return 2
    if alt < 3500 and days_max <= 5:
        return 1
    return 2


def slugify_trek(name):
    s = name.lower()
    s = re.sub(r"[–—\-]+", "-", s)
    s = re.sub(r"[^a-z0-9\-]", "-", s)
    s = re.sub(r"-+", "-", s).strip("-")
    return s


# ---------------------------------------------------------------------------
# All 116 treks
# ---------------------------------------------------------------------------
# Keys: n=name, co=country, st=state, gw=gateway airport, alt=max altitude,
# dur=duration text, bm=best months text, sp=starting point, ops=operators text,
# cost=cost text, note=special note, bt=booking_type override, pu=portal_url
ALL_TREKS = [
    # --- Uttarakhand ---
    {"n": "Kedarkantha", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 3800, "dur": 6, "bm": "Dec–Apr", "sp": "Sankri", "ops": "IH,TTH,Bikat", "cost": "₹11,000–15,000", "note": "Classic beginner winter summit"},
    {"n": "Dayara Bugyal", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 3750, "dur": "5–6", "bm": "Dec–Mar, May–Jun, Sep–Nov", "sp": "Raithal", "ops": "IH,TTH", "cost": "₹9,000–13,000", "note": "Huge high meadows; Butter Festival in August"},
    {"n": "Brahmatal", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 3800, "dur": 6, "bm": "Dec–Mar", "sp": "Lohajung", "ops": "IH,TTH", "cost": "₹13,000–16,000", "note": "Frozen lake; views of Trishul and Nanda Ghunti"},
    {"n": "Roopkund", "co": "India", "st": "Uttarakhand", "gw": "PGH", "alt": 4800, "dur": 8, "bm": "May–Jun, Sep–Oct", "sp": "Lohajung", "ops": "IH,TTH", "cost": "₹20,000–25,000", "note": "Lake of ancient skeletons; reopened in 2026"},
    {"n": "Ali–Bedni Bugyal", "co": "India", "st": "Uttarakhand", "gw": "PGH", "alt": 3965, "dur": 6, "bm": "Apr–Jun, Sep–Nov", "sp": "Lohajung", "ops": "IH,TTH", "cost": "₹13,000–16,000", "note": "Among Asia’s largest alpine meadows"},
    {"n": "Valley of Flowers + Hemkund", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 4329, "dur": 6, "bm": "Jul–Sep", "sp": "Govindghat", "ops": "IH,TTH,Bikat", "cost": "₹13,000–17,000", "note": "UNESCO site; monsoon wildflower blooms"},
    {"n": "Har Ki Dun", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 3566, "dur": 7, "bm": "Apr–Jun, Sep–Dec", "sp": "Sankri / Taluka", "ops": "IH,Bikat", "cost": "₹12,000–16,000", "note": "Cradle-shaped valley; ancient Osla village"},
    {"n": "Kuari Pass", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 3876, "dur": 6, "bm": "Oct–Apr", "sp": "Joshimath", "ops": "IH,TTH", "cost": "₹12,000–15,000", "note": "Curzon Trail with Nanda Devi views"},
    {"n": "Pangarchulla", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 4590, "dur": 6, "bm": "Mar–May", "sp": "Joshimath", "ops": "IH,TTH", "cost": "₹14,000–18,000", "note": "Tough snow summit climb"},
    {"n": "Chopta–Tungnath–Chandrashila", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 4000, "dur": "3–5", "bm": "Oct–Apr", "sp": "Sari / Chopta", "ops": "local", "cost": "₹7,000–10,000", "note": "World’s highest Shiva temple"},
    {"n": "Nag Tibba", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 3022, "dur": 2, "bm": "All year", "sp": "Pantwari", "ops": "local", "cost": "₹3,000–6,000", "note": "Best weekend trek from Delhi"},
    {"n": "Kedartal", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 4750, "dur": 6, "bm": "May–Jun, Sep–Oct", "sp": "Gangotri", "ops": "IH,TTH", "cost": "₹18,000–22,000", "note": "Thalay Sagar reflected in the lake"},
    {"n": "Gaumukh–Tapovan", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 4463, "dur": "7–8", "bm": "May–Jun, Sep–Oct", "sp": "Gangotri", "ops": "TTH,Bikat", "cost": "₹18,000–22,000", "note": "Source of the Ganga; base of Shivling; permits capped"},
    {"n": "Pindari Glacier", "co": "India", "st": "Uttarakhand", "gw": "PGH", "alt": 3660, "dur": "6–7", "bm": "May–Jun, Sep–Oct", "sp": "Kharkiya / Loharkhet", "ops": "TTH,local", "cost": "₹15,000–20,000", "note": "Easy glacier trek in Kumaon"},
    {"n": "Bali Pass", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 4950, "dur": 8, "bm": "May–Jun, Sep–Oct", "sp": "Sankri", "ops": "Bikat,TTH", "cost": "₹21,000–28,000", "note": "Crosses from Ruinsara valley to Yamunotri"},
    {"n": "Borasu Pass", "co": "India", "st": "Uttarakhand", "gw": "DED", "alt": 5450, "dur": "8–9", "bm": "Jun, Sep", "sp": "Sankri (ends Chitkul)", "ops": "Bikat,TTH", "cost": "₹28,000–35,000", "note": "Remote crossing from Uttarakhand into Kinnaur"},
    # --- Himachal Pradesh ---
    {"n": "Hampta Pass", "co": "India", "st": "Himachal Pradesh", "gw": "KUU", "alt": 4270, "dur": 5, "bm": "Jun–Oct", "sp": "Manali", "ops": "IH,TTH,Bikat", "cost": "₹9,500–13,000", "note": "Green Kullu to Lahaul desert, plus Chandratal"},
    {"n": "Bhrigu Lake", "co": "India", "st": "Himachal Pradesh", "gw": "KUU", "alt": 4300, "dur": 4, "bm": "May–Oct", "sp": "Manali", "ops": "IH,TTH", "cost": "₹8,000–11,000", "note": "Short alpine lake trek"},
    {"n": "Beas Kund", "co": "India", "st": "Himachal Pradesh", "gw": "KUU", "alt": 3700, "dur": 4, "bm": "May–Oct", "sp": "Manali", "ops": "IH,Bikat", "cost": "₹8,000–11,000", "note": "Source of the Beas, close to Hanuman Tibba"},
    {"n": "Chandrakhani Pass", "co": "India", "st": "Himachal Pradesh", "gw": "KUU", "alt": 3660, "dur": 4, "bm": "May–Oct", "sp": "Naggar / Manali", "ops": "IH", "cost": "₹8,000–11,000", "note": "Passes the isolated Malana village"},
    {"n": "Sar Pass", "co": "India", "st": "Himachal Pradesh", "gw": "KUU", "alt": 4200, "dur": 5, "bm": "May–Jun", "sp": "Kasol", "ops": "YHAI,TTH", "cost": "₹8,000–11,000", "note": "Snow slides on the descent"},
    {"n": "Kheerganga", "co": "India", "st": "Himachal Pradesh", "gw": "KUU", "alt": 2950, "dur": 2, "bm": "Apr–Nov", "sp": "Barshaini", "ops": "local", "cost": "Budget", "note": "Natural hot spring at the top"},
    {"n": "Pin Parvati Pass", "co": "India", "st": "Himachal Pradesh", "gw": "KUU", "alt": 5319, "dur": "10–11", "bm": "Jul–Sep", "sp": "Barshaini", "ops": "Bikat,TTH", "cost": "₹28,000–35,000", "note": "Lush Kullu to barren Spiti"},
    {"n": "Rupin Pass", "co": "India", "st": "Himachal Pradesh", "gw": "DED", "alt": 4650, "dur": "7–8", "bm": "May–Jun, Sep–Oct", "sp": "Dhaula / Jiskun (ends Sangla)", "ops": "IH,Bikat,TTH", "cost": "₹16,000–22,000", "note": "Hanging village and a three-tier waterfall"},
    {"n": "Buran Ghati", "co": "India", "st": "Himachal Pradesh", "gw": "SLV", "alt": 4572, "dur": 7, "bm": "May–Jun, Sep–Oct", "sp": "Janglik", "ops": "IH,TTH", "cost": "₹16,000–20,000", "note": "Rappel down a snow wall"},
    {"n": "Bhaba Pass", "co": "India", "st": "Himachal Pradesh", "gw": "SLV", "alt": 4865, "dur": 7, "bm": "Jul–Sep", "sp": "Kafnu", "ops": "Bikat,local", "cost": "₹20,000–28,000", "note": "Green Kinnaur into Spiti’s Pin Valley"},
    {"n": "Triund", "co": "India", "st": "Himachal Pradesh", "gw": "DHM", "alt": 2850, "dur": "1–2", "bm": "Mar–Jun, Sep–Dec", "sp": "McLeodganj", "ops": "local", "cost": "₹1,500–3,000", "note": "Dhauladhar ridge, easy climb"},
    {"n": "Indrahar Pass", "co": "India", "st": "Himachal Pradesh", "gw": "DHM", "alt": 4342, "dur": "4–5", "bm": "May–Jun, Sep–Oct", "sp": "McLeodganj", "ops": "local", "cost": "₹10,000–14,000", "note": "Continues past Triund into the Chamba side"},
    # --- Jammu & Kashmir ---
    {"n": "Kashmir Great Lakes", "co": "India", "st": "Jammu & Kashmir", "gw": "SXR", "alt": 4200, "dur": "7–8", "bm": "Jul–Sep", "sp": "Sonamarg", "ops": "IH,TTH,Bikat", "cost": "₹18,000–24,000", "note": "Seven turquoise alpine lakes"},
    {"n": "Tarsar Marsar", "co": "India", "st": "Jammu & Kashmir", "gw": "SXR", "alt": 4000, "dur": 7, "bm": "Jul–Sep", "sp": "Aru / Pahalgam", "ops": "IH,TTH", "cost": "₹16,000–20,000", "note": "Twin lakes and gentle meadows"},
    # --- Ladakh ---
    {"n": "Chadar", "co": "India", "st": "Ladakh", "gw": "IXL", "alt": 3400, "dur": "6–9", "bm": "Jan–Feb", "sp": "Leh", "ops": "TTH,Bikat", "cost": "₹25,000–35,000", "note": "Walk on a frozen river; check it’s running"},
    {"n": "Markha Valley", "co": "India", "st": "Ladakh", "gw": "IXL", "alt": 5260, "dur": "6–8", "bm": "Jun–Sep", "sp": "Leh", "ops": "IH,local", "cost": "₹20,000–28,000", "note": "Monasteries and Kongmaru La pass"},
    {"n": "Sham Valley", "co": "India", "st": "Ladakh", "gw": "IXL", "alt": 3900, "dur": "3–4", "bm": "May–Oct", "sp": "Leh", "ops": "local", "cost": "₹10,000–15,000", "note": "Easy homestay trek through apricot villages"},
    {"n": "Kang Yatse II", "co": "India", "st": "Ladakh", "gw": "IXL", "alt": 6250, "dur": "9–10", "bm": "Jul–Aug", "sp": "Leh", "ops": "Bikat,TTH", "cost": "₹35,000–45,000", "note": "Trekking peak; Stok Kangri closed since 2020"},
    # --- Sikkim ---
    {"n": "Goecha La", "co": "India", "st": "Sikkim", "gw": "IXB", "alt": 4600, "dur": "10–11", "bm": "Apr–May, Oct–Nov", "sp": "Yuksom", "ops": "IH,TTH", "cost": "₹22,000–28,000", "note": "Face-to-face Kanchenjunga view"},
    {"n": "Dzongri", "co": "India", "st": "Sikkim", "gw": "IXB", "alt": 4020, "dur": "6–7", "bm": "Mar–May, Oct–Dec", "sp": "Yuksom", "ops": "IH,TTH", "cost": "₹14,000–18,000", "note": "Shorter version of Goecha La"},
    {"n": "Green Lake", "co": "India", "st": "Sikkim", "gw": "IXB", "alt": 4935, "dur": "12+", "bm": "Apr–May, Oct", "sp": "Lachen", "ops": "local", "cost": "On request", "note": "Restricted area at base of Kanchenjunga’s north face"},
    # --- West Bengal ---
    {"n": "Sandakphu–Phalut", "co": "India", "st": "West Bengal", "gw": "IXB", "alt": 3636, "dur": 6, "bm": "Oct–May", "sp": "Manebhanjan", "ops": "IH,TTH", "cost": "₹13,000–17,000", "note": "Everest, Lhotse, Makalu and Kanchenjunga in one view"},
    # --- Northeast ---
    {"n": "Dzukou Valley", "co": "India", "st": "Nagaland / Manipur", "gw": "DMU", "alt": 2452, "dur": 2, "bm": "Jun–Sep, Oct–Mar", "sp": "Viswema", "ops": "local", "cost": "Budget", "note": "Rare Dzukou lily"},
    {"n": "Double Decker Root Bridge", "co": "India", "st": "Meghalaya", "gw": "SHL", "alt": 1000, "dur": "1–2", "bm": "Oct–Apr", "sp": "Tyrna", "ops": "local", "cost": "Budget", "note": "Two-tier living root bridge at Nongriat; ~3,000 steps down"},
    # --- Maharashtra ---
    {"n": "Kalsubai", "co": "India", "st": "Maharashtra", "gw": "BOM", "alt": 1646, "dur": 1, "bm": "Jun–Feb", "sp": "Bari", "ops": "local", "cost": "₹1,000–2,000", "note": "Highest point in Maharashtra"},
    {"n": "Harishchandragad", "co": "India", "st": "Maharashtra", "gw": "PNQ", "alt": 1424, "dur": "1–2", "bm": "Jun–Feb", "sp": "Khireshwar / Pachnai", "ops": "local", "cost": "₹1,500–3,000", "note": "Konkan Kada overhanging cliff"},
    {"n": "Rajmachi", "co": "India", "st": "Maharashtra", "gw": "PNQ", "alt": 830, "dur": "1–2", "bm": "Jun–Feb", "sp": "Lonavala", "ops": "local", "cost": "₹1,000–2,000", "note": "Fireflies in May–June; classic monsoon night trek"},
    {"n": "Lohagad–Visapur", "co": "India", "st": "Maharashtra", "gw": "PNQ", "alt": 1033, "dur": 1, "bm": "Jun–Feb", "sp": "Malavli", "ops": "local", "cost": "₹1,000–1,500", "note": "Scorpion-tail ridge (Vinchu Kata)"},
    {"n": "Kalavantin Durg", "co": "India", "st": "Maharashtra", "gw": "BOM", "alt": 700, "dur": 1, "bm": "Oct–Feb", "sp": "Thakurwadi, Panvel", "ops": "local", "cost": "₹1,000–1,500", "note": "Exposed rock-cut steps"},
    {"n": "Harihar Fort", "co": "India", "st": "Maharashtra", "gw": "BOM", "alt": 1120, "dur": 1, "bm": "Oct–Feb", "sp": "Nirgudpada, Nashik", "ops": "local", "cost": "₹1,500–2,500", "note": "Near-vertical 80° rock-cut staircase"},
    {"n": "Ratangad", "co": "India", "st": "Maharashtra", "gw": "BOM", "alt": 1297, "dur": "1–2", "bm": "Jun–Feb", "sp": "Ratanwadi, Bhandardara", "ops": "local", "cost": "₹1,500–2,500", "note": "Natural rock window (Nedhe)"},
    {"n": "Sandhan Valley", "co": "India", "st": "Maharashtra", "gw": "BOM", "alt": 1000, "dur": 2, "bm": "Oct–May", "sp": "Samrad", "ops": "local", "cost": "₹2,500–4,000", "note": "Narrow canyon called ‘Valley of Shadows’; rappelling"},
    {"n": "Rajgad–Torna", "co": "India", "st": "Maharashtra", "gw": "PNQ", "alt": 1403, "dur": 2, "bm": "Jun–Feb", "sp": "Gunjavane", "ops": "local", "cost": "₹2,000–3,500", "note": "Shivaji’s first capital linked to Torna fort"},
    {"n": "Raigad", "co": "India", "st": "Maharashtra", "gw": "PNQ", "alt": 820, "dur": 1, "bm": "Oct–Feb", "sp": "Pachad", "ops": "local", "cost": "₹1,500–2,000", "note": "Shivaji’s coronation capital"},
    {"n": "Andharban", "co": "India", "st": "Maharashtra", "gw": "PNQ", "alt": 650, "dur": 1, "bm": "Jun–Sep", "sp": "Pimpri, Tamhini", "ops": "local", "cost": "₹1,500–2,500", "note": "Dense monsoon jungle; mostly downhill"},
    {"n": "Bhimashankar", "co": "India", "st": "Maharashtra", "gw": "BOM", "alt": 1000, "dur": 1, "bm": "Jun–Feb", "sp": "Khandas", "ops": "local", "cost": "₹1,500–2,000", "note": "Jyotirlinga temple and wildlife sanctuary"},
    {"n": "Alang–Madan–Kulang", "co": "India", "st": "Maharashtra", "gw": "BOM", "alt": 1470, "dur": "2–3", "bm": "Oct–Feb", "sp": "Ambewadi, Igatpuri", "ops": "local", "cost": "₹4,000–7,000", "note": "Toughest Sahyadri trek; rock climbing needed"},
    {"n": "Salher", "co": "India", "st": "Maharashtra", "gw": "BOM", "alt": 1567, "dur": "1–2", "bm": "Oct–Feb", "sp": "Waghambe", "ops": "local", "cost": "₹2,000–3,000", "note": "Highest fort in Maharashtra"},
    {"n": "Devkund Waterfall", "co": "India", "st": "Maharashtra", "gw": "PNQ", "alt": 500, "dur": 1, "bm": "Aug–Oct", "sp": "Bhira", "ops": "local", "cost": "₹1,200–2,000", "note": "Turquoise plunge pool"},
    # --- Karnataka ---
    {"n": "Kudremukh", "co": "India", "st": "Karnataka", "gw": "IXE", "alt": 1894, "dur": 1, "bm": "Oct–Feb", "sp": "Mullodi", "ops": "AV", "cost": "Permit fee", "note": "Grassland and shola forest", "bt": "govt_portal", "pu": "https://aranyavihaara.karnataka.gov.in"},
    {"n": "Kumara Parvatha", "co": "India", "st": "Karnataka", "gw": "IXE", "alt": 1712, "dur": 2, "bm": "Oct–Feb", "sp": "Kukke Subramanya", "ops": "AV", "cost": "₹3,000–5,000", "note": "Toughest trek in the Western Ghats; daily caps", "bt": "govt_portal", "pu": "https://aranyavihaara.karnataka.gov.in"},
    {"n": "Mullayanagiri", "co": "India", "st": "Karnataka", "gw": "IXE", "alt": 1930, "dur": 1, "bm": "Sep–Mar", "sp": "Sarpadari, Chikmagalur", "ops": "local", "cost": "₹1,500–3,000", "note": "Highest peak in Karnataka"},
    {"n": "Tadiandamol", "co": "India", "st": "Karnataka", "gw": "IXE", "alt": 1748, "dur": 1, "bm": "Oct–Feb", "sp": "Kakkabe, Coorg", "ops": "AV", "cost": "₹2,000–4,000", "note": "Highest peak in Coorg; shola grassland", "bt": "govt_portal", "pu": "https://aranyavihaara.karnataka.gov.in"},
    {"n": "Kodachadri", "co": "India", "st": "Karnataka", "gw": "IXE", "alt": 1343, "dur": "1–2", "bm": "Oct–Feb", "sp": "Nittur / Kollur", "ops": "AV", "cost": "₹2,500–4,000", "note": "Sunset point at Sarvajna Peetha", "bt": "govt_portal", "pu": "https://aranyavihaara.karnataka.gov.in"},
    {"n": "Netravati Peak", "co": "India", "st": "Karnataka", "gw": "IXE", "alt": 1520, "dur": 1, "bm": "Oct–Feb", "sp": "Samse, Kudremukh NP", "ops": "AV", "cost": "₹2,000–4,000", "note": "Rolling grassland ridges", "bt": "govt_portal", "pu": "https://aranyavihaara.karnataka.gov.in"},
    {"n": "Brahmagiri", "co": "India", "st": "Karnataka", "gw": "CNN", "alt": 1608, "dur": "1–2", "bm": "Oct–Feb", "sp": "Irupu Falls, Coorg", "ops": "AV", "cost": "₹2,000–4,000", "note": "Kerala border; elephant country", "bt": "govt_portal", "pu": "https://aranyavihaara.karnataka.gov.in"},
    {"n": "Narasimha Parvatha", "co": "India", "st": "Karnataka", "gw": "IXE", "alt": 1152, "dur": 1, "bm": "Oct–Feb", "sp": "Kigga, Sringeri", "ops": "AV", "cost": "₹2,000–3,000", "note": "Rainforest with streams", "bt": "govt_portal", "pu": "https://aranyavihaara.karnataka.gov.in"},
    {"n": "Skandagiri", "co": "India", "st": "Karnataka", "gw": "BLR", "alt": 1450, "dur": 1, "bm": "Oct–Feb", "sp": "Papagni Mutt", "ops": "local", "cost": "₹1,000–2,000", "note": "Sunrise above a sea of clouds"},
    {"n": "Savandurga", "co": "India", "st": "Karnataka", "gw": "BLR", "alt": 1226, "dur": 1, "bm": "Oct–Mar", "sp": "Magadi", "ops": "local", "cost": "₹800–1,500", "note": "One of Asia’s largest single-rock hills"},
    {"n": "Gokarna Beach Trek", "co": "India", "st": "Karnataka", "gw": "GOI", "alt": 0, "dur": 1, "bm": "Oct–Mar", "sp": "Gokarna", "ops": "local", "cost": "₹1,500–3,000", "note": "Coastal walk linking five beaches"},
    # --- Kerala ---
    {"n": "Meesapulimala", "co": "India", "st": "Kerala", "gw": "COK", "alt": 2640, "dur": "1–2", "bm": "Sep–Feb", "sp": "Munnar", "ops": "KF", "cost": "₹4,000–6,000", "note": "Second-highest peak in the Western Ghats", "bt": "govt_portal"},
    {"n": "Agasthyarkoodam", "co": "India", "st": "Kerala", "gw": "TRV", "alt": 1868, "dur": "2–3", "bm": "Jan–Feb", "sp": "Bonacaud", "ops": "KF", "cost": "Permit fee", "note": "Short annual season; biodiversity hotspot", "bt": "govt_portal", "pu": "https://booking.keralatourism.org"},
    {"n": "Chembra Peak", "co": "India", "st": "Kerala", "gw": "CCJ", "alt": 2100, "dur": 1, "bm": "Sep–Feb", "sp": "Meppadi, Wayanad", "ops": "local", "cost": "₹1,000–2,000", "note": "Heart-shaped lake on the way up"},
    # --- Tamil Nadu ---
    {"n": "Kurangani–Top Station", "co": "India", "st": "Tamil Nadu", "gw": "IXM", "alt": 1911, "dur": "1–2", "bm": "Oct–Feb", "sp": "Kurangani, Theni", "ops": "local", "cost": "₹1,000–3,000", "note": "Old tea-ropeway route up to the Munnar border"},
    {"n": "Kolukkumalai", "co": "India", "st": "Tamil Nadu", "gw": "COK", "alt": 2200, "dur": 1, "bm": "Sep–Mar", "sp": "Suryanelli / Bodinayakanur", "ops": "local", "cost": "₹2,000–3,000", "note": "One of the world’s highest tea estates; sunrise"},
    {"n": "Kolaribetta", "co": "India", "st": "Tamil Nadu", "gw": "CJB", "alt": 2625, "dur": 1, "bm": "best Oct–Jan", "sp": "Avalanche, Nilgiris", "ops": "TTN", "cost": "₹1,999", "note": "11 km round trip through Cauliflower shola", "bt": "govt_portal", "pu": "https://trektamilnadu.com"},
    {"n": "Devarbetta", "co": "India", "st": "Tamil Nadu", "gw": "CJB", "alt": None, "dur": 1, "bm": "best Oct–Jan", "sp": "Avalanche, Nilgiris", "ops": "TTN", "cost": "₹2,699", "note": "Tough 13 km shola trail with rich birdlife", "bt": "govt_portal", "pu": "https://trektamilnadu.com"},
    {"n": "Parsonvalley–Mukurthi Hut", "co": "India", "st": "Tamil Nadu", "gw": "CJB", "alt": 2512, "dur": 1, "bm": "best Oct–Jan", "sp": "Parsonvalley, Nilgiris", "ops": "TTN", "cost": "₹1,599", "note": "Mukurthi National Park; Nilgiri tahr country", "bt": "govt_portal", "pu": "https://trektamilnadu.com"},
    {"n": "Rangasamy Peak", "co": "India", "st": "Tamil Nadu", "gw": "CJB", "alt": 2000, "dur": 1, "bm": "best Oct–Jan", "sp": "Karikayur, Nilgiris", "ops": "TTN", "cost": "₹1,299", "note": "Sacred peak beside the Rangasamy Pillar rock", "bt": "govt_portal", "pu": "https://trektamilnadu.com"},
    {"n": "Vattakanal–Dolphin’s Nose", "co": "India", "st": "Tamil Nadu", "gw": "IXM", "alt": 2000, "dur": 1, "bm": "Sep–May", "sp": "Kodaikanal", "ops": "local", "cost": "Budget", "note": "Overhanging rock above a deep valley"},
    {"n": "Kodaikanal–Vellagavi", "co": "India", "st": "Tamil Nadu", "gw": "IXM", "alt": 2000, "dur": 2, "bm": "best Sep–Jan", "sp": "Kodaikanal / Vattakanal", "ops": "TTN", "cost": "₹2,999", "note": "Descent to an old village with no road access", "bt": "govt_portal", "pu": "https://trektamilnadu.com"},
    {"n": "Kodaikanal–Munnar", "co": "India", "st": "Tamil Nadu", "gw": "IXM", "alt": 2000, "dur": 5, "bm": "Dec–Jan", "sp": "Kodaikanal", "ops": "YHAI", "cost": "Check YHAI", "note": "Multi-day: Vellagavi, Kurangani, Top Station into Munnar"},
    {"n": "Velliangiri Hills", "co": "India", "st": "Tamil Nadu", "gw": "CJB", "alt": 1840, "dur": 1, "bm": "Feb–May", "sp": "Poondi, Coimbatore", "ops": "local", "cost": "Free", "note": "Seven-hill pilgrimage trail"},
    {"n": "Kolli Hills – Agaya Gangai", "co": "India", "st": "Tamil Nadu", "gw": "TRZ", "alt": 1300, "dur": 1, "bm": "Aug–Feb", "sp": "Kolli Hills, Namakkal", "ops": "local", "cost": "Entry fee", "note": "1,000+ steps down to a tall waterfall"},
    # --- Madhya Pradesh ---
    {"n": "Pench Tiger Trail", "co": "India", "st": "Madhya Pradesh", "gw": "NAG", "alt": 600, "dur": 4, "bm": "Nov–Feb", "sp": "Pench", "ops": "IH", "cost": "₹10,000–15,000", "note": "Walk a tiger reserve with forest rangers"},
    {"n": "MP Tiger Trail", "co": "India", "st": "Madhya Pradesh", "gw": "JLR", "alt": 600, "dur": "4–5", "bm": "Nov–Feb", "sp": "Beohari", "ops": "IH", "cost": "₹10,000–15,000", "note": "Birchuli Dol meadow inside a tiger reserve"},
    {"n": "Unexplored Satpura", "co": "India", "st": "Madhya Pradesh", "gw": "BHO", "alt": 1000, "dur": 5, "bm": "Nov–Mar", "sp": "Satpura Tiger Reserve", "ops": "IH", "cost": "Check site", "note": "Newly opened forest walking trip"},
    {"n": "Patalkot Gorge", "co": "India", "st": "Madhya Pradesh", "gw": "NAG", "alt": 1000, "dur": "Multi-day", "bm": "Winter (expected)", "sp": "Chhindwara", "ops": "IH", "cost": "Opening soon", "note": "Deep gorge with Bharia tribal villages"},
    {"n": "Dhupgarh, Pachmarhi", "co": "India", "st": "Madhya Pradesh", "gw": "BHO", "alt": 1352, "dur": 1, "bm": "Oct–Mar", "sp": "Pachmarhi", "ops": "local", "cost": "Budget", "note": "Highest point in MP; sunset point"},
    {"n": "Pachmarhi circuit", "co": "India", "st": "Madhya Pradesh", "gw": "BHO", "alt": 1100, "dur": "2–3", "bm": "Oct–Mar", "sp": "Pachmarhi", "ops": "local", "cost": "Budget", "note": "Bee Falls, Handi Khoh gorge, cave shelters"},
    # --- Chhattisgarh ---
    {"n": "Chhattisgarh Jungle Trek", "co": "India", "st": "Chhattisgarh", "gw": "RPR", "alt": 700, "dur": 4, "bm": "Nov–Feb", "sp": "Guru Ghasidas NP, Koriya", "ops": "IH", "cost": "₹10,000–15,000", "note": "Exclusive access to a tiger reserve"},
    {"n": "Kanger Valley", "co": "India", "st": "Chhattisgarh", "gw": "JGB", "alt": 600, "dur": "1–2", "bm": "Oct–Mar", "sp": "Jagdalpur", "ops": "local", "cost": "Budget", "note": "Kotumsar limestone caves, Tirathgarh Falls"},
    {"n": "Mainpat", "co": "India", "st": "Chhattisgarh", "gw": "RPR", "alt": 1100, "dur": "1–2", "bm": "Oct–Mar", "sp": "Ambikapur", "ops": "local", "cost": "Budget", "note": "Tibetan settlement; the ‘Shimla of Chhattisgarh’"},
    {"n": "Gaurlata", "co": "India", "st": "Chhattisgarh", "gw": "RPR", "alt": 1225, "dur": 1, "bm": "Oct–Feb", "sp": "Samri, Balrampur", "ops": "local", "cost": "Budget", "note": "Highest point in Chhattisgarh"},
    {"n": "Jatmai Temple", "co": "India", "st": "Chhattisgarh", "gw": "RPR", "alt": 400, "dur": 1, "bm": "Jul–Feb", "sp": "Near Raipur", "ops": "local", "cost": "Free", "note": "Forest temple beside waterfalls"},
    # --- Other states ---
    {"n": "Dudhsagar", "co": "India", "st": "Goa / Karnataka", "gw": "GOI", "alt": 600, "dur": 1, "bm": "Jul–Oct", "sp": "Kulem / Castle Rock", "ops": "local", "cost": "₹1,500–3,000", "note": "Monsoon waterfall; walking on railway tracks banned"},
    {"n": "Guru Shikhar", "co": "India", "st": "Rajasthan", "gw": "UDR", "alt": 1722, "dur": 1, "bm": "Oct–Mar", "sp": "Mount Abu", "ops": "local", "cost": "Free", "note": "Highest point in the Aravallis"},
    {"n": "Girnar", "co": "India", "st": "Gujarat", "gw": "HSR", "alt": 1100, "dur": 1, "bm": "Oct–Mar", "sp": "Junagadh", "ops": "local", "cost": "Free", "note": "About 10,000 steps; Jain and Hindu temples"},
    {"n": "Mahendragiri", "co": "India", "st": "Odisha", "gw": "BBI", "alt": 1501, "dur": 1, "bm": "Oct–Feb", "sp": "Paralakhemundi", "ops": "local", "cost": "Budget", "note": "Ancient temples in the Eastern Ghats"},
    {"n": "Parasnath", "co": "India", "st": "Jharkhand", "gw": "IXR", "alt": 1350, "dur": 1, "bm": "Oct–Mar", "sp": "Madhuban", "ops": "local", "cost": "Free", "note": "Holiest Jain site (Shikharji)"},
    # --- Nepal ---
    {"n": "Everest Base Camp", "co": "Nepal", "st": "Khumbu", "gw": "KTM", "alt": 5545, "dur": "12–16", "bm": "Mar–May, Oct–Nov", "sp": "Lukla", "ops": "local", "cost": "$1,300–2,700", "note": "Base of the world’s highest peak (height is Kala Patthar)"},
    {"n": "Gokyo Lakes", "co": "Nepal", "st": "Khumbu", "gw": "KTM", "alt": 5357, "dur": "12–14", "bm": "Mar–May, Oct–Nov", "sp": "Lukla", "ops": "local", "cost": "$1,200–1,700", "note": "Turquoise lakes and Ngozumpa glacier"},
    {"n": "Everest Three Passes", "co": "Nepal", "st": "Khumbu", "gw": "KTM", "alt": 5535, "dur": "18–20", "bm": "Apr–May, Oct–Nov", "sp": "Lukla", "ops": "local", "cost": "$1,700–2,400", "note": "Kongma La, Cho La and Renjo La"},
    {"n": "Pikey Peak", "co": "Nepal", "st": "Solukhumbu", "gw": "KTM", "alt": 4065, "dur": "5–7", "bm": "Oct–May", "sp": "Dhap", "ops": "local", "cost": "$500–800", "note": "Everest view without the crowds"},
    {"n": "Annapurna Circuit", "co": "Nepal", "st": "Annapurna", "gw": "KTM", "alt": 5416, "dur": "12–18", "bm": "Mar–May, Oct–Nov", "sp": "Besisahar", "ops": "local", "cost": "$700–1,500", "note": "Thorong La; optional Tilicho Lake add-on"},
    {"n": "Annapurna Base Camp", "co": "Nepal", "st": "Annapurna", "gw": "PHH", "alt": 4130, "dur": "7–12", "bm": "Mar–May, Oct–Dec", "sp": "Nayapul / Siwai", "ops": "local", "cost": "$600–1,200", "note": "Natural amphitheatre of peaks"},
    {"n": "Poon Hill", "co": "Nepal", "st": "Annapurna", "gw": "PHH", "alt": 3210, "dur": "4–5", "bm": "Oct–May", "sp": "Nayapul / Ulleri", "ops": "local", "cost": "$350–600", "note": "Sunrise viewpoint; spring rhododendrons"},
    {"n": "Mardi Himal", "co": "Nepal", "st": "Annapurna", "gw": "PHH", "alt": 4500, "dur": "5–7", "bm": "Oct–May", "sp": "Kande", "ops": "local", "cost": "$500–800", "note": "Close-up Machhapuchhre"},
    {"n": "Khopra Danda", "co": "Nepal", "st": "Annapurna", "gw": "PHH", "alt": 3660, "dur": "6–8", "bm": "Oct–May", "sp": "Nayapul", "ops": "local", "cost": "$600–900", "note": "Community lodges off the main trail"},
    {"n": "Nar Phu", "co": "Nepal", "st": "Annapurna", "gw": "KTM", "alt": 5320, "dur": "10–14", "bm": "Apr–May, Sep–Nov", "sp": "Koto", "ops": "local", "cost": "$1,300–1,900", "note": "Restricted Tibetan-style villages"},
    {"n": "Upper Mustang", "co": "Nepal", "st": "Mustang", "gw": "PHH", "alt": 4200, "dur": "12–17", "bm": "Mar–Nov", "sp": "Jomsom", "ops": "local", "cost": "$1,800–2,500 + $500 permit", "note": "Walled city of Lo Manthang in a rain shadow"},
    {"n": "Manaslu Circuit", "co": "Nepal", "st": "Gorkha", "gw": "KTM", "alt": 5106, "dur": "14–16", "bm": "Mar–May, Sep–Nov", "sp": "Machha Khola", "ops": "local", "cost": "$1,300–1,800", "note": "Restricted area; minimum 2 trekkers"},
    {"n": "Tsum Valley", "co": "Nepal", "st": "Gorkha", "gw": "KTM", "alt": 3700, "dur": "12–14", "bm": "Mar–May, Sep–Nov", "sp": "Machha Khola", "ops": "local", "cost": "$1,300–2,000", "note": "Hidden Buddhist valley"},
    {"n": "Langtang Valley", "co": "Nepal", "st": "Langtang", "gw": "KTM", "alt": 4773, "dur": "7–10", "bm": "Mar–May, Oct–Nov", "sp": "Syabrubesi", "ops": "local", "cost": "$600–950", "note": "Closest major trek to Kathmandu (height is Kyanjin Ri)"},
    {"n": "Gosaikunda", "co": "Nepal", "st": "Langtang", "gw": "KTM", "alt": 4610, "dur": "5–7", "bm": "Mar–May, Sep–Nov", "sp": "Dhunche", "ops": "local", "cost": "$500–800", "note": "Sacred lakes; August pilgrimage"},
    {"n": "Helambu", "co": "Nepal", "st": "Langtang", "gw": "KTM", "alt": 3640, "dur": "5–7", "bm": "Oct–May", "sp": "Sundarijal", "ops": "local", "cost": "$400–700", "note": "Low-altitude trek with Sherpa villages"},
    {"n": "Kanchenjunga Base Camp", "co": "Nepal", "st": "Far East", "gw": "BDP", "alt": 5143, "dur": "20–24", "bm": "Apr–May, Oct–Nov", "sp": "Taplejung", "ops": "local", "cost": "$2,000–3,000", "note": "Remote trek to the third-highest peak"},
    {"n": "Makalu Base Camp", "co": "Nepal", "st": "East", "gw": "KTM", "alt": 4870, "dur": "18–20", "bm": "Apr–May, Oct–Nov", "sp": "Tumlingtar", "ops": "local", "cost": "$1,800–2,800", "note": "Wild Barun valley"},
    {"n": "Upper Dolpo", "co": "Nepal", "st": "West", "gw": "KEP", "alt": 5350, "dur": "20–25", "bm": "May–Sep", "sp": "Juphal", "ops": "local", "cost": "$3,500+ incl. $500 permit", "note": "Shey Phoksundo Lake; ancient Bon culture"},
    {"n": "Rara Lake", "co": "Nepal", "st": "Far West", "gw": "KEP", "alt": 3000, "dur": "7–10", "bm": "Apr–May, Sep–Nov", "sp": "Talcha", "ops": "local", "cost": "$900–1,500", "note": "Nepal’s largest lake"},
]

# Extra HGN-only treks not in the Excel list
EXTRA_HGN_TREKS = [
    {"n": "Island Peak", "co": "Nepal", "st": "Khumbu", "gw": "KTM", "alt": 6189,
     "dur": 16, "bm": "Apr–May, Oct–Nov", "sp": "Lukla", "ops": "local",
     "cost": "$2,500–4,000", "note": "Trekking peak with one real climbing day above Chukhung",
     "hgn_id": "Island Peak"},
    {"n": "Mera Peak", "co": "Nepal", "st": "Khumbu", "gw": "KTM", "alt": 6476,
     "dur": 18, "bm": "Apr–May, Oct–Nov", "sp": "Lukla", "ops": "local",
     "cost": "$3,000–5,000", "note": "Highest trekking peak in Nepal; glacier walking, not technical climbing",
     "hgn_id": "Mera Peak"},
]


class Command(BaseCommand):
    help = "Seed airports, operators, and all 116+ treks (safe to re-run)."

    def handle(self, *args, **opts):
        # 1. Airports
        for iata, city in AIRPORTS.items():
            Airport.objects.update_or_create(iata=iata, defaults={"city": city})
        self.stdout.write(f"  {len(AIRPORTS)} airports")

        # 2. Rename old treks before creating new ones
        for old_name, new_name in RENAMES.items():
            Trek.objects.filter(name=old_name).update(name=new_name)
        Trek.objects.filter(name__in=OBSOLETE).update(active=False)

        # 3. Operators
        ih, _ = Operator.objects.update_or_create(name="Indiahikes", defaults={
            "short_name": "IH", "website": "https://indiahikes.com",
            "source": Operator.Source.INDIAHIKES})
        tth, _ = Operator.objects.update_or_create(name="Trek The Himalayas", defaults={
            "short_name": "TTH", "website": "https://trekthehimalayas.com",
            "source": Operator.Source.TTH})
        bikat, _ = Operator.objects.update_or_create(name="Bikat Adventures", defaults={
            "short_name": "Bikat", "website": "https://www.bikatadventures.com",
            "source": Operator.Source.MANUAL, "active": False})
        hgn, _ = Operator.objects.update_or_create(name="Himalayan Guides Nepal", defaults={
            "short_name": "HGN", "website": "https://www.himalayanguides.com",
            "source": Operator.Source.HGN})
        yhai, _ = Operator.objects.update_or_create(name="YHAI", defaults={
            "short_name": "YHAI", "website": "https://www.yhaindia.org",
            "source": Operator.Source.MANUAL})
        ttn, _ = Operator.objects.update_or_create(name="Trek Tamil Nadu", defaults={
            "short_name": "TTN", "website": "https://trektamilnadu.com",
            "source": Operator.Source.MANUAL})
        av, _ = Operator.objects.update_or_create(name="Aranya Vihaara", defaults={
            "short_name": "AV", "website": "https://aranyavihaara.karnataka.gov.in",
            "source": Operator.Source.MANUAL})
        kf, _ = Operator.objects.update_or_create(name="Kerala Forest", defaults={
            "short_name": "KF", "website": "https://booking.keralatourism.org",
            "source": Operator.Source.MANUAL})
        local_op, _ = Operator.objects.update_or_create(name="Local", defaults={
            "short_name": "Local", "website": "",
            "source": Operator.Source.MANUAL})

        op_map = {
            "IH": ih, "TTH": tth, "Bikat": bikat, "HGN": hgn,
            "YHAI": yhai, "TTN": ttn, "AV": av, "KF": kf, "local": local_op,
        }

        # 4. Seed treks
        all_items = ALL_TREKS + EXTRA_HGN_TREKS
        for i, t in enumerate(all_items):
            name = t["n"]
            dur_min, dur_max = parse_duration(t["dur"])
            cost_min, cost_max = parse_cost(t["cost"])
            best_months = parse_best_months(t["bm"])
            alt = t["alt"]
            state = t["st"]
            region = state if t["co"] == "India" else state  # for Nepal, state = sub-region

            # Booking type
            bt = t.get("bt")
            if not bt:
                ops_str = t["ops"]
                if ops_str == "local":
                    bt = "local"
                elif ops_str in ("AV", "TTN", "KF"):
                    bt = "govt_portal"
                else:
                    bt = "scheduled"

            difficulty = guess_difficulty(alt, dur_max, name, state)

            trek_defaults = {
                "country": t["co"],
                "state": state,
                "gateway_airport_id": t["gw"],
                "region": region,
                "max_altitude_m": alt,
                "difficulty": difficulty,
                "booking_type": bt,
                "sort_order": (i + 1) * 10,
                "best_months": best_months,
                "duration_days_min": dur_min,
                "duration_days_max": dur_max,
                "cost_inr_min": cost_min,
                "cost_inr_max": cost_max,
                "starting_point": t.get("sp", ""),
                "portal_url": t.get("pu", ""),
                "notes": t.get("note", ""),
            }

            trek, _ = Trek.objects.update_or_create(name=name, defaults=trek_defaults)

            # 5. OperatorTrek entries
            ops_str = t["ops"]
            ops_list = [o.strip() for o in ops_str.split(",")]

            # Existing IH/TTH/Bikat full entries
            if name in EXISTING_IH:
                ih_id, ih_slug, tth_slug, bikat_slug = EXISTING_IH[name]
                OperatorTrek.objects.update_or_create(operator=ih, trek=trek, defaults={
                    "page_url": f"https://indiahikes.com/{ih_slug}",
                    "external_id": ih_id, "needs_daily_refresh": True})
                OperatorTrek.objects.update_or_create(operator=tth, trek=trek, defaults={
                    "page_url": f"https://trekthehimalayas.com/{tth_slug}",
                    "needs_daily_refresh": True})
                OperatorTrek.objects.update_or_create(operator=bikat, trek=trek, defaults={
                    "page_url": f"https://www.bikatadventures.com/Home/Itinerary/{bikat_slug}",
                    "needs_daily_refresh": False})
                # Also add HGN if this trek has one
                if name in EXISTING_HGN:
                    OperatorTrek.objects.update_or_create(operator=hgn, trek=trek, defaults={
                        "page_url": PAGE_URL, "external_id": EXISTING_HGN[name],
                        "needs_daily_refresh": True})
                continue  # skip the generic loop below

            # Existing HGN entries for Nepal treks
            has_hgn = name in EXISTING_HGN
            # Extra HGN treks
            hgn_extra_id = t.get("hgn_id")

            created_ops = set()

            for op_key in ops_list:
                op_key = op_key.strip()
                if op_key in ("IH",):
                    slug = slugify_trek(name)
                    OperatorTrek.objects.update_or_create(operator=ih, trek=trek, defaults={
                        "page_url": f"https://indiahikes.com/{slug}",
                        "external_id": "", "needs_daily_refresh": True})
                    created_ops.add("IH")
                elif op_key in ("TTH",):
                    slug = slugify_trek(name)
                    OperatorTrek.objects.update_or_create(operator=tth, trek=trek, defaults={
                        "page_url": f"https://trekthehimalayas.com/{slug}-trek",
                        "needs_daily_refresh": True})
                    created_ops.add("TTH")
                elif op_key in ("Bikat",):
                    slug = slugify_trek(name)
                    OperatorTrek.objects.update_or_create(operator=bikat, trek=trek, defaults={
                        "page_url": f"https://www.bikatadventures.com/Home/Itinerary/{slug}-trek",
                        "needs_daily_refresh": False})
                    created_ops.add("Bikat")
                elif op_key in ("YHAI",):
                    OperatorTrek.objects.update_or_create(operator=yhai, trek=trek, defaults={
                        "page_url": "", "needs_daily_refresh": False})
                    created_ops.add("YHAI")
                elif op_key in ("TTN",):
                    OperatorTrek.objects.update_or_create(operator=ttn, trek=trek, defaults={
                        "page_url": t.get("pu", "https://trektamilnadu.com"),
                        "needs_daily_refresh": False})
                    created_ops.add("TTN")
                elif op_key in ("AV",):
                    OperatorTrek.objects.update_or_create(operator=av, trek=trek, defaults={
                        "page_url": t.get("pu", "https://aranyavihaara.karnataka.gov.in"),
                        "needs_daily_refresh": False})
                    created_ops.add("AV")
                elif op_key in ("KF",):
                    OperatorTrek.objects.update_or_create(operator=kf, trek=trek, defaults={
                        "page_url": t.get("pu", ""),
                        "needs_daily_refresh": False})
                    created_ops.add("KF")
                elif op_key in ("local",):
                    OperatorTrek.objects.update_or_create(operator=local_op, trek=trek, defaults={
                        "page_url": "", "needs_daily_refresh": False})
                    created_ops.add("local")

            # Add HGN entry for Nepal treks that have one
            if has_hgn and "HGN" not in created_ops:
                OperatorTrek.objects.update_or_create(operator=hgn, trek=trek, defaults={
                    "page_url": PAGE_URL, "external_id": EXISTING_HGN[name],
                    "needs_daily_refresh": True})

            if hgn_extra_id and "HGN" not in created_ops:
                OperatorTrek.objects.update_or_create(operator=hgn, trek=trek, defaults={
                    "page_url": PAGE_URL, "external_id": hgn_extra_id,
                    "needs_daily_refresh": True})

            # Nepal "local agencies" treks also get a Local operator entry
            if t["co"] == "Nepal" and "local" not in created_ops:
                OperatorTrek.objects.update_or_create(operator=local_op, trek=trek, defaults={
                    "page_url": "", "needs_daily_refresh": False})

        total = len(all_items)
        self.stdout.write(self.style.SUCCESS(
            f"Seeded {len(AIRPORTS)} airports, {len(op_map)} operators, {total} treks."))
