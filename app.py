

import hashlib
import requests
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask, render_template, request, redirect, session
import mysql.connector
from mysql.connector import Error

# ─────────────────────────────────────────────────────────────────────────────
# API KEYS
# ─────────────────────────────────────────────────────────────────────────────
PEXELS_KEY     = "twjs4p5zjKUHLJyA59mpUutfnSsmC5XiUQFteLc1NY56wgMW87GWvx0o"
FOURSQUARE_KEY = "fsq3gBwo8zSURBg4GjDS9mt8bnqNE73D5fWzErnvO9WfIR0="
GEOAPIFY_KEY   = "2ee2dc0995ac4454b73abd74d7628f9c"

PEXELS_HEADERS = {"Authorization": PEXELS_KEY}
FSQ_HEADERS    = {"Authorization": FOURSQUARE_KEY, "accept": "application/json"}

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PlanMyTrip/1.0 (travel planner; educational project)"})

# ─────────────────────────────────────────────────────────────────────────────
# FOURSQUARE & GEOAPIFY CATEGORY IDs
# Using broad parent categories — narrow IDs miss too many venues in Indian cities
# ─────────────────────────────────────────────────────────────────────────────
FSQ_CATS = {
    # 19000 = Lodging (broad parent — catches hotels, resorts, guest houses, homestays)
    "hotel":      "19000",
    # 13000 = Food (broad parent — catches all dining types)
    "restaurant": "13000",
    # 16000 = Landmarks & Outdoors, 10000 = Arts & Entertainment, 12000 = Community & Government
    "attraction": "16000,10000,12000",
}

# Keyword queries used as fallback when category search returns too few results
FSQ_QUERIES = {
    "hotel":      ["hotel", "resort", "inn", "lodge", "guest house"],
    "restaurant": ["restaurant", "cafe", "dhaba", "food", "biryani"],
    "attraction": ["temple", "fort", "museum", "garden", "tourist place", "park", "waterfall"],
}

GEO_CATS = {
    "hotel":      "accommodation.hotel,accommodation.guest_house,accommodation.hostel",
    "restaurant": "catering.restaurant,catering.fast_food,catering.cafe,catering.food_court",
    "attraction": "tourism.sights,tourism.attraction,entertainment.museum,leisure.park",
}

# ─────────────────────────────────────────────────────────────────────────────
# BLACKLISTS  (prevent cross-category contamination)
# ─────────────────────────────────────────────────────────────────────────────
HOTEL_BL = {"temple","mandir","masjid","church","dargah","cafe","restaurant",
            "dhaba","museum","park","garden","school","college","hospital","clinic"}
REST_BL  = {"hotel","resort","inn","lodge","guest house","guesthouse","temple",
            "mandir","museum","park","palace","fort","garden","school","college"}
ATTR_BL  = {"hotel","resort","inn","lodge","restaurant","dhaba","cafe","bar",
            "pub","salon","spa","gym"}
BLACKLISTS = {"hotel": HOTEL_BL, "restaurant": REST_BL, "attraction": ATTR_BL}

BAD_IMG = [".svg",".gif","icon","logo","map","blank","seal","flag",
           "placeholder","default","avatar","profile","user","banner",
           "coat_of_arms","emblem"]


def _name_ok(name: str, kind: str) -> bool:
    low = name.lower()
    return bool(name) and not any(b in low for b in BLACKLISTS.get(kind, set()))


# ─────────────────────────────────────────────────────────────────────────────
# IMAGE REGISTRY  (prevents duplicate images across cards)
# ─────────────────────────────────────────────────────────────────────────────
class ImageRegistry:
    def __init__(self):
        self._urls: set = set()
        self._ids:  set = set()

    def claim(self, url: str, uid: str = None) -> bool:
        if not url or not url.startswith("http"):
            return False
        canon = url.split("?")[0].rstrip("/").lower()
        fname = canon.rsplit("/", 1)[-1]
        if uid and uid in self._ids:
            return False
        if canon in self._urls or fname in self._urls:
            return False
        self._urls.update([canon, fname])
        if uid:
            self._ids.add(uid)
        return True


# ─────────────────────────────────────────────────────────────────────────────
# GEOCODING  (OpenStreetMap Nominatim — free, accurate for Indian cities)
# ─────────────────────────────────────────────────────────────────────────────
def geocode_city(city: str) -> dict:
    empty = {"lat": None, "lon": None, "canonical": city,
             "city_name": city, "state": "", "country": "India"}
    try:
        res = SESSION.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": f"{city}, India", "format": "json", "limit": 5,
                    "countrycodes": "in", "addressdetails": 1},
            timeout=10,
        )
        res.raise_for_status()
        results = res.json()
        if not results:
            return empty

        city_lower = city.lower().strip()
        best, best_score = None, -999

        for r in results:
            lat = float(r.get("lat", 0))
            lon = float(r.get("lon", 0))
            if abs(lat) < 0.01 or abs(lon) < 0.01:
                continue
            addr      = r.get("address", {})
            display   = r.get("display_name", "").lower()
            osm_type  = r.get("type", "")
            osm_class = r.get("class", "")
            city_name = (addr.get("city") or addr.get("town") or
                         addr.get("municipality") or addr.get("district") or
                         addr.get("county") or city).strip()
            state   = addr.get("state", "").strip()
            country = addr.get("country", "India").strip()

            score = 0
            if city_lower in city_name.lower(): score += 20
            if city_lower in display:           score += 10
            if osm_type == "city":              score += 15
            elif osm_type == "town":            score += 10
            elif osm_type in ("municipality","administrative"): score += 5
            elif osm_type == "village":         score -= 20
            if osm_class == "place":            score += 5

            if score > best_score:
                best_score = score
                best = {"lat": lat, "lon": lon, "city_name": city_name,
                        "state": state, "country": country}

        if not best:
            r    = results[0]
            addr = r.get("address", {})
            best = {"lat": float(r["lat"]), "lon": float(r["lon"]),
                    "city_name": (addr.get("city") or addr.get("town") or city),
                    "state": addr.get("state", ""), "country": addr.get("country","India")}

        canonical = ", ".join(p for p in [best["city_name"], best["state"], best["country"]] if p)
        print(f"[Geocode] {city!r} → {canonical!r} ({best['lat']:.4f}, {best['lon']:.4f})")
        return {"lat": best["lat"], "lon": best["lon"], "canonical": canonical,
                "city_name": best["city_name"], "state": best["state"], "country": best["country"]}
    except Exception as e:
        print(f"[Geocode ERROR] {city!r}: {e}")
        return empty


# ─────────────────────────────────────────────────────────────────────────────
# FOURSQUARE
# ─────────────────────────────────────────────────────────────────────────────
def _fsq_photos_for_id(fsq_id: str, registry: ImageRegistry) -> str | None:
    try:
        res = SESSION.get(
            f"https://api.foursquare.com/v3/places/{fsq_id}/photos",
            headers=FSQ_HEADERS,
            params={"limit": 20, "classifications": "outdoor,indoor,food,menu"},
            timeout=8,
        )
        if res.status_code != 200:
            return None
        for photo in res.json():
            prefix = photo.get("prefix", "")
            suffix = photo.get("suffix", "")
            if not prefix or not suffix:
                continue
            if any(b in suffix.lower() for b in BAD_IMG):
                continue
            url = f"{prefix}800x600{suffix}"
            if registry.claim(url, uid=f"fsq_{fsq_id}_{suffix}"):
                return url
    except Exception as e:
        print(f"[FSQ photo] {fsq_id}: {e}")
    return None


def _fsq_search(lat: float, lon: float, kind: str,
                canonical: str, limit: int = 12) -> list:
    """
    3-tier Foursquare search strategy:
      Tier 1 — broad category + lat/lon   (most accurate, uses geocoded coords)
      Tier 2 — broad category + near text (catches venues missed by radius)
      Tier 3 — keyword query + lat/lon    (fallback for small Indian cities
                                           where category coverage is thin)
    All tiers are deduplicated by fsq_id.
    """
    cat_ids  = FSQ_CATS.get(kind, "")
    results  = []
    seen_ids = set()
    has_ll   = lat and lon and abs(lat) > 0.01 and abs(lon) > 0.01

    def _parse(data: dict, label: str) -> int:
        added = 0
        for r in data.get("results", []):
            fsq_id = r.get("fsq_id", "")
            name   = r.get("name", "").strip()
            loc    = r.get("location", {})
            addr   = ", ".join(filter(None, [
                loc.get("address",""), loc.get("locality",""),
                loc.get("region",""), loc.get("country",""),
            ])) or canonical
            if fsq_id and name and _name_ok(name, kind) and fsq_id not in seen_ids:
                seen_ids.add(fsq_id)
                results.append({"name": name, "address": addr, "fsq_id": fsq_id})
                added += 1
        if added:
            print(f"[FSQ {label}] {kind}: +{added} venues (total {len(results)})")
        return added

    # ── Tier 1: category + ll (primary — broad category, correct geocoded coords) ──
    if has_ll:
        try:
            res = SESSION.get(
                "https://api.foursquare.com/v3/places/search",
                headers=FSQ_HEADERS,
                params={
                    "ll":         f"{lat},{lon}",
                    "radius":     25000,          # 25km — wider net for smaller cities
                    "limit":      limit,
                    "sort":       "POPULARITY",
                    "categories": cat_ids,
                    "fields":     "fsq_id,name,location",
                },
                timeout=10,
            )
            if res.status_code == 200:
                _parse(res.json(), "cat+ll")
            else:
                print(f"[FSQ cat+ll] HTTP {res.status_code}: {res.text[:120]}")
        except Exception as e:
            print(f"[FSQ cat+ll] {kind}: {e}")

    # ── Tier 2: category + near text (catches venues outside radius or missed by coords) ──
    if len(results) < limit:
        try:
            res = SESSION.get(
                "https://api.foursquare.com/v3/places/search",
                headers=FSQ_HEADERS,
                params={
                    "near":       canonical,
                    "limit":      limit,
                    "sort":       "POPULARITY",
                    "categories": cat_ids,
                    "fields":     "fsq_id,name,location",
                },
                timeout=10,
            )
            if res.status_code == 200:
                _parse(res.json(), "cat+near")
            else:
                print(f"[FSQ cat+near] HTTP {res.status_code}: {res.text[:120]}")
        except Exception as e:
            print(f"[FSQ cat+near] {kind}: {e}")

    # ── Tier 3: keyword text queries + ll — most powerful for thin-coverage cities ──
    # Runs keyword by keyword until we have enough results
    if len(results) < limit and has_ll:
        for kw in FSQ_QUERIES.get(kind, []):
            if len(results) >= limit:
                break
            try:
                res = SESSION.get(
                    "https://api.foursquare.com/v3/places/search",
                    headers=FSQ_HEADERS,
                    params={
                        "query":  kw,
                        "ll":     f"{lat},{lon}",
                        "radius": 25000,
                        "limit":  limit,
                        "sort":   "POPULARITY",
                        "fields": "fsq_id,name,location",
                    },
                    timeout=10,
                )
                if res.status_code == 200:
                    _parse(res.json(), f"kw='{kw}'")
                else:
                    print(f"[FSQ kw '{kw}'] HTTP {res.status_code}")
            except Exception as e:
                print(f"[FSQ kw '{kw}'] {e}")

    print(f"[FSQ total] {kind} in {canonical!r}: {len(results)} unique venues")
    return results[:limit]


# ─────────────────────────────────────────────────────────────────────────────
# WIKIPEDIA / WIKIMEDIA  — attractions only, with strict relevance checks
#
# KEY RULES:
#  • Wiki is ONLY used for attractions (landmarks, forts, temples, museums).
#    Hotels and restaurants get FSQ photos or Pexels — never Wiki.
#  • A Wiki article is only accepted if the place name words appear in the
#    article title (not just somewhere nearby on the map).
#  • Geo-search is disabled — it returns any random nearby article (beach,
#    police station, park) whose image has nothing to do with the venue.
#  • Commons is only tried for attractions with a very specific title match.
# ─────────────────────────────────────────────────────────────────────────────

# Extra bad-image signals specifically for Wiki (aerial/map/blueprint images)
BAD_WIKI_IMG = BAD_IMG + ["aerial","satellite","map","plan","blueprint",
                           "diagram","sketch","drawing","route","location"]

def _name_words(name: str) -> list[str]:
    """Return meaningful words from a place name (length > 3, no stop words)."""
    stops = {"the","and","for","with","near","road","lane","street","marg",
             "nagar","colony","sector","phase","plot","area","india","goa",
             "from","into","that","this","are","was"}
    return [w.lower() for w in name.split() if len(w) > 3 and w.lower() not in stops]


def _title_matches_name(title: str, name: str) -> bool:
    """
    True only if at least ONE meaningful word from `name` appears in `title`.
    This prevents grabbing a beach photo for 'Hotel Yuvraj'.
    """
    words = _name_words(name)
    if not words:
        return False
    title_low = title.lower()
    return any(w in title_low for w in words)


def _wiki_page_image(title: str, name: str, registry: ImageRegistry) -> str | None:
    """
    Fetch main image from a Wikipedia article.
    Only returns if the article title actually matches the place name.
    """
    if not _title_matches_name(title, name):
        return None
    try:
        res = SESSION.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action":"query","titles":title,"prop":"pageimages",
                    "piprop":"original","pithumbsize":800,"format":"json","redirects":1},
            timeout=7,
        )
        for page in res.json().get("query",{}).get("pages",{}).values():
            if page.get("pageid",-1) == -1:
                continue
            url = page.get("original",{}).get("source","")
            if not url:
                continue
            url_low = url.lower()
            if any(b in url_low for b in BAD_WIKI_IMG):
                continue
            if registry.claim(url, uid=f"wiki_{title}"):
                print(f"[Wiki ✓] {title!r} for {name!r}")
                return url
    except Exception as e:
        print(f"[Wiki page] {title!r}: {e}")
    return None


def _wiki_attraction_image(name: str, city: str, state: str,
                            registry: ImageRegistry) -> str | None:
    """
    Look up a Wikipedia image for an attraction (landmark, fort, temple, museum).
    Only used for attractions — NOT for hotels or restaurants.
    Tries direct article lookup only (no geo-search, which returns unrelated places).
    """
    # Try progressively broader queries, but always check title relevance
    for q in [f"{name}", f"{name} {city}", f"{name}, {city}", f"{name} {state}"]:
        img = _wiki_page_image(q, name, registry)
        if img:
            return img

    # Wikimedia Commons — only if name has 2+ meaningful words (specific enough)
    words = _name_words(name)
    if len(words) >= 2:
        try:
            q = f"{name} {city}"
            res = SESSION.get(
                "https://commons.wikimedia.org/w/api.php",
                params={"action":"query","list":"search",
                        "srsearch":f"{name} filetype:bitmap",
                        "srnamespace":6,"srlimit":8,"format":"json"},
                timeout=6,
            )
            for item in res.json().get("query",{}).get("search",[]):
                title = item.get("title","")
                if not title.startswith("File:"):
                    continue
                # File name must contain a name word
                fname_low = title.lower()
                if not any(w in fname_low for w in words):
                    continue
                ir = SESSION.get(
                    "https://commons.wikimedia.org/w/api.php",
                    params={"action":"query","titles":title,"prop":"imageinfo",
                            "iiprop":"url","iiurlwidth":800,"format":"json"},
                    timeout=6,
                )
                for p in ir.json().get("query",{}).get("pages",{}).values():
                    url = (p.get("imageinfo",[{}])[0].get("thumburl") or
                           p.get("imageinfo",[{}])[0].get("url",""))
                    if not url:
                        continue
                    url_low = url.lower()
                    if any(b in url_low for b in BAD_WIKI_IMG):
                        continue
                    if registry.claim(url, uid=f"commons_{title}"):
                        print(f"[Commons ✓] {name!r}")
                        return url
        except Exception as e:
            print(f"[Commons] {name!r}: {e}")

    return None


# ─────────────────────────────────────────────────────────────────────────────
# PEXELS  — category-aware queries for relevant images
# ─────────────────────────────────────────────────────────────────────────────

# Pexels search terms per category — ensures visually relevant results
PEXELS_KIND_TERMS = {
    "hotel":      ["hotel exterior", "hotel lobby", "hotel room", "resort pool"],
    "restaurant": ["restaurant interior", "indian food", "restaurant dining", "food dish"],
    "attraction": ["temple india", "fort india", "museum india", "india landmark", "waterfall india"],
}

def _pexels_search(query: str, registry: ImageRegistry,
                   page: int = 1, strict: bool = True) -> str | None:
    """Search Pexels. strict=True requires a keyword match in the photo alt text."""
    stopwords = {"the","a","an","in","at","of","for","and","or","india",
                 "hotel","restaurant","place","tourist","attraction","popular","best"}
    keywords = [w for w in query.lower().split() if len(w) > 3 and w not in stopwords]
    try:
        res = SESSION.get(
            "https://api.pexels.com/v1/search",
            headers=PEXELS_HEADERS,
            params={"query": query, "per_page": 20, "page": page, "orientation": "landscape"},
            timeout=8,
        )
        if res.status_code != 200:
            return None
        for photo in res.json().get("photos", []):
            photo_id = photo.get("id")
            alt      = (photo.get("alt") or "").lower()
            if strict and keywords:
                if not any(kw in alt for kw in keywords):
                    continue
            url = (photo.get("src",{}).get("large2x") or
                   photo.get("src",{}).get("large",""))
            if url and registry.claim(url, uid=f"px_{photo_id}"):
                print(f"[Pexels ✓] {query!r}")
                return url
    except Exception as e:
        print(f"[Pexels] {query!r}: {e}")
    return None


def _pexels_for_place(name: str, city: str, state: str, kind: str,
                      registry: ImageRegistry, idx: int = 0) -> str | None:
    """
    Smart Pexels lookup for a specific place.
    Tries name-specific query first, then falls back to category-generic terms.
    """
    page = (idx % 3) + 1

    # 1. Try name + city (strict — must match alt text)
    img = _pexels_search(f"{name} {city}", registry, page=page, strict=True)
    if img:
        return img

    # 2. Try name alone (strict)
    img = _pexels_search(name, registry, page=page, strict=True)
    if img:
        return img

    # 3. Use category-specific generic terms (not strict — visual relevance over name match)
    for term in PEXELS_KIND_TERMS.get(kind, []):
        img = _pexels_search(term, registry, page=page, strict=False)
        if img:
            return img

    # 4. Last resort: city + kind
    return _pexels_search(f"{city} {kind}", registry, page=page, strict=False)


# ─────────────────────────────────────────────────────────────────────────────
# GEOAPIFY  (supplemental place discovery)
# ─────────────────────────────────────────────────────────────────────────────
def _geo_places(city: str, kind: str, lat: float, lon: float, limit: int = 20) -> list:
    if not lat or not lon or abs(lat) < 0.01:
        print(f"[Geo {kind}] Skipped — invalid coordinates ({lat}, {lon})")
        return []
    category = GEO_CATS.get(kind, "tourism.sights")
    try:
        res = SESSION.get(
            "https://api.geoapify.com/v2/places",
            params={"categories": category, "filter": f"circle:{lon},{lat},20000",
                    "limit": limit, "apiKey": GEOAPIFY_KEY},
            timeout=10,
        )
        res.raise_for_status()
        out = []
        for f in res.json().get("features", []):
            p    = f.get("properties", {})
            name = p.get("name","").strip()
            addr = p.get("formatted","").strip()
            if name and _name_ok(name, kind):
                out.append({"name": name, "address": addr or f"{city}, India"})
        print(f"[Geo {kind}] {len(out)} places near ({lat:.3f},{lon:.3f})")
        return out
    except Exception as e:
        print(f"[Geo {kind}] {e}")
        return []


# ─────────────────────────────────────────────────────────────────────────────
# PICSUM  (absolute last resort placeholder)
# ─────────────────────────────────────────────────────────────────────────────
def _picsum_placeholder(name: str, city: str, kind: str) -> str:
    seed = hashlib.md5(f"{name}|{city}|{kind}".encode()).hexdigest()[:12]
    url  = f"https://picsum.photos/seed/{seed}/800/600"
    print(f"[Picsum] {name!r}")
    return url


# ─────────────────────────────────────────────────────────────────────────────
# BUILD PLACES  (main place-card builder per category)
#
# Image priority per category:
#   hotel      → FSQ photo  →  Pexels "hotel exterior/lobby/room"  →  Picsum
#   restaurant → FSQ photo  →  Pexels "restaurant interior/food"   →  Picsum
#   attraction → FSQ photo  →  Wikipedia/Commons (strict match)    →  Pexels  →  Picsum
# ─────────────────────────────────────────────────────────────────────────────
def build_places(city: str, geo_places: list, kind: str,
                 max_places: int = 5,
                 registry: ImageRegistry = None,
                 lat: float = 0, lon: float = 0,
                 canonical: str = "",
                 state: str = "") -> list:
    if registry is None:
        registry = ImageRegistry()

    can        = canonical or f"{city}, India"
    results    = []
    used_ids   = set()
    used_names = set()

    # ── Phase 1: Foursquare (3-tier) ─────────────────────────────────────────
    fsq_places = _fsq_search(lat, lon, kind, can, limit=max(15, max_places * 3))
    print(f"[FSQ] {kind} in {city}: {len(fsq_places)} total venues found")

    # ── Phase 2: Fetch FSQ photos in parallel ────────────────────────────────
    # FSQ photos are the most reliable — actual photos of the venue
    if fsq_places:
        with ThreadPoolExecutor(max_workers=min(8, len(fsq_places))) as ex:
            fmap = {ex.submit(_fsq_photos_for_id, p["fsq_id"], registry): p
                    for p in fsq_places}
            for future in as_completed(fmap):
                p = fmap[future]
                if p["name"] in used_names:
                    continue
                try:
                    img = future.result()
                    used_ids.add(p["fsq_id"])
                    used_names.add(p["name"])
                    results.append({"name": p["name"], "address": p["address"],
                                    "image": img, "_fsq": True})
                except Exception as e:
                    print(f"[FSQ phase2] {e}")

    # ── Phase 3: Fill missing images based on category ────────────────────────
    # Hotels & restaurants → Pexels only (Wiki never gives relevant hotel/restaurant photos)
    # Attractions → Wiki first (can find actual landmark photos), then Pexels
    missing = [r for r in results if not r.get("image")]
    if missing:
        if kind == "attraction":
            # Try Wikipedia for attractions — strict name-title match required
            with ThreadPoolExecutor(max_workers=min(6, len(missing))) as ex:
                fmap2 = {
                    ex.submit(_wiki_attraction_image, r["name"], city, state, registry): r
                    for r in missing
                }
                for future in as_completed(fmap2):
                    r = fmap2[future]
                    try:
                        img = future.result()
                        if img:
                            r["image"] = img
                    except Exception as e:
                        print(f"[Wiki fallback] {e}")

        # Anything still missing after Wiki (or hotels/restaurants) → Pexels
        still_missing = [r for r in results if not r.get("image")]
        for i, place in enumerate(still_missing):
            place["image"] = _pexels_for_place(
                place["name"], city, state, kind, registry, idx=i
            )

    # ── Phase 4: Geoapify supplement if not enough results ───────────────────
    if len(results) < max_places:
        need     = max_places - len(results)
        geo_cand = [p for p in geo_places
                    if _name_ok(p["name"], kind) and p["name"] not in used_names][:need * 3]
        if geo_cand:
            for i, p in enumerate(geo_cand):
                if len(results) >= max_places:
                    break
                img = None
                if kind == "attraction":
                    img = _wiki_attraction_image(p["name"], city, state, registry)
                if not img:
                    img = _pexels_for_place(p["name"], city, state, kind, registry, idx=i)
                if p["name"] not in used_names:
                    used_names.add(p["name"])
                    results.append({"name": p["name"], "address": p["address"], "image": img})

    # ── Phase 5: Sort — FSQ with real photo first ─────────────────────────────
    results.sort(key=lambda x: (
        0 if (x.get("_fsq") and x.get("image") and "picsum" not in (x.get("image") or "")) else
        1 if x.get("_fsq") else
        2 if (x.get("image") and "picsum" not in (x.get("image") or "")) else 3
    ))

    # ── Phase 6: Picsum for anything still missing ────────────────────────────
    for place in results:
        if not place.get("image"):
            place["image"] = _picsum_placeholder(place["name"], city, kind)

    # ── Phase 7: Trim / pad to max_places ────────────────────────────────────
    cards = results[:max_places]
    slot  = 0
    while len(cards) < max_places:
        label = f"Popular {kind.title()} #{slot + 1} in {city}"
        cards.append({"name": label, "address": f"{city}, India",
                      "image": _picsum_placeholder(label, city, kind)})
        slot += 1

    real = sum(1 for c in cards if c.get("image") and "picsum" not in c.get("image",""))
    print(f"[build_places] {kind} in {city}: {len(cards)} cards, {real} with real images")
    return cards


# ─────────────────────────────────────────────────────────────────────────────
# CITY HERO IMAGE
# ─────────────────────────────────────────────────────────────────────────────
def get_city_hero(city: str, registry: ImageRegistry,
                  lat: float = 0, lon: float = 0,
                  canonical: str = "",
                  state: str = "") -> str:
    # Strategy 1: Wikipedia direct city article (name must match title)
    for q in [city, f"{city} city", f"{city}, {state}", f"{city}, India"]:
        img = _wiki_page_image(q, city, registry)
        if img:
            print(f"[Hero Wiki ✓] {city}")
            return img

    # Strategy 2: FSQ top landmark photo at correct coordinates
    if lat and lon and abs(lat) > 0.01:
        try:
            res = SESSION.get(
                "https://api.foursquare.com/v3/places/search",
                headers=FSQ_HEADERS,
                params={"ll": f"{lat},{lon}", "radius": 15000, "limit": 5,
                        "sort": "POPULARITY", "categories": "16000,10000",
                        "fields": "fsq_id,name"},
                timeout=8,
            )
            if res.status_code == 200:
                for r in res.json().get("results", []):
                    fsq_id = r.get("fsq_id")
                    if fsq_id:
                        img = _fsq_photos_for_id(fsq_id, registry)
                        if img:
                            print(f"[Hero FSQ ✓] {city}")
                            return img
        except Exception as e:
            print(f"[Hero FSQ] {e}")

    # Strategy 3: Pexels city landscape
    for q, strict in [
        (f"{city} {state} landmark", True),
        (f"{city} India landmark",   True),
        (f"{city} India",            False),
        (f"{state} India cityscape", False),
    ]:
        img = _pexels_search(q, registry, strict=strict)
        if img:
            print(f"[Hero Pexels ✓] {city}")
            return img

    return _picsum_placeholder(city, "India", "hero")


# ─────────────────────────────────────────────────────────────────────────────
# FLASK APP
# ─────────────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = "secret123"

http = requests.Session()
http.headers.update({"User-Agent": "PlanMyTrip/1.0"})

# ─────────────────────────────────────────────────────────────────────────────
# DATABASE
# ─────────────────────────────────────────────────────────────────────────────
DB_CONFIG = {
    "host":     "localhost",
    "user":     "root",
    "password": "shweta",
    "database": "Planmytrip"
}

def get_db():
    try:
        return mysql.connector.connect(**DB_CONFIG)
    except Error as e:
        print(f"[DB] {e}")
        return None

def hash_password(p: str) -> str:
    return hashlib.sha256(p.encode()).hexdigest()

def db_register_user(name, email, phone, password):
    conn = get_db()
    if not conn:
        return False, "Database connection failed."
    try:
        cur = conn.cursor()
        cur.execute("SELECT id FROM users WHERE email=%s", (email,))
        if cur.fetchone():
            return False, "Email already registered."
        cur.execute(
            "INSERT INTO users (name,email,phone,password) VALUES (%s,%s,%s,%s)",
            (name, email, phone, hash_password(password))
        )
        conn.commit()
        return True, "Registered successfully."
    except Error:
        return False, "Registration failed."
    finally:
        conn.close()

def db_login_user(identifier, password):
    conn = get_db()
    if not conn:
        return None
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT * FROM users WHERE email=%s OR phone=%s",
                    (identifier, identifier))
        user = cur.fetchone()
        return user if user and user["password"] == hash_password(password) else None
    except Error:
        return None
    finally:
        conn.close()

def db_save_feedback(user_email, city, rating, comment):
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO feedbacks (user_email,city,rating,comment) VALUES (%s,%s,%s,%s)",
            (user_email, city, rating, comment)
        )
        conn.commit()
    except Error:
        pass
    finally:
        conn.close()

def db_get_feedbacks(user_email):
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT city,rating,comment FROM feedbacks WHERE user_email=%s ORDER BY id DESC",
            (user_email,)
        )
        return cur.fetchall()
    except Error:
        return []
    finally:
        conn.close()

def db_save_trip(user_email, city, days, budget, people, budget_type, per_person):
    conn = get_db()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO trip_history
               (user_email,city,days,budget,people,budget_type,per_person)
               VALUES (%s,%s,%s,%s,%s,%s,%s)""",
            (user_email, city, days, budget, people, budget_type, per_person)
        )
        conn.commit()
    except Error:
        pass
    finally:
        conn.close()

def db_get_trips(user_email, limit=10):
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            """SELECT city,days,budget,people,budget_type,per_person
               FROM trip_history WHERE user_email=%s ORDER BY id DESC LIMIT %s""",
            (user_email, limit)
        )
        return cur.fetchall()
    except Error:
        return []
    finally:
        conn.close()

def db_get_total_users():
    conn = get_db()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM users")
        r = cur.fetchone()
        return r[0] if r else 0
    except Error:
        return 0
    finally:
        conn.close()

def db_get_all_trips(limit=100):
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute(
            "SELECT city,days,budget,people,budget_type,per_person "
            "FROM trip_history ORDER BY id DESC LIMIT %s", (limit,)
        )
        return cur.fetchall()
    except Error:
        return []
    finally:
        conn.close()

def db_get_all_feedbacks():
    conn = get_db()
    if not conn:
        return []
    try:
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT city,rating,comment FROM feedbacks ORDER BY id DESC")
        return cur.fetchall()
    except Error:
        return []
    finally:
        conn.close()

def db_get_total_trips():
    conn = get_db()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM trip_history")
        r = cur.fetchone()
        return r[0] if r else 0
    except Error:
        return 0
    finally:
        conn.close()


# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────
def safe_int(value, default=0):
    try:
        r = int(str(value).strip())
        return r if r > 0 else default
    except:
        return default


# ─────────────────────────────────────────────────────────────────────────────
# TRIP PLANNING LOGIC
# ─────────────────────────────────────────────────────────────────────────────
BUDGET_PROFILES = {
    "low": {
        "ratios":    {"Hotel": 0.30, "Food": 0.30, "Travel": 0.20, "Activities": 0.20},
        "transport": "Public transport / Bus",
        "stay":      "Budget hotel / Hostel",
        "transport_options": [
            {"mode": "🚌 Local Bus",     "ratio": 0.40, "icon": "bus-front-fill",   "tip": "Cheapest option, widely available"},
            {"mode": "🚇 Metro / Train", "ratio": 0.35, "icon": "train-front-fill", "tip": "Fast & reliable for city travel"},
            {"mode": "🛺 Auto Rickshaw", "ratio": 0.25, "icon": "taxi-front-fill",  "tip": "Short distances & last mile"},
        ],
    },
    "medium": {
        "ratios":    {"Hotel": 0.40, "Food": 0.25, "Travel": 0.20, "Activities": 0.15},
        "transport": "Bike rental / Cab",
        "stay":      "Standard hotel",
        "transport_options": [
            {"mode": "🏍 Bike Rental",    "ratio": 0.35, "icon": "bicycle",         "tip": "Flexible, explore at your pace"},
            {"mode": "🚕 Ola / Uber Cab", "ratio": 0.45, "icon": "taxi-front-fill", "tip": "Comfortable, book on demand"},
            {"mode": "🚌 AC Bus",         "ratio": 0.20, "icon": "bus-front-fill",  "tip": "Intercity travel on a budget"},
        ],
    },
    "high": {
        "ratios":    {"Hotel": 0.50, "Food": 0.20, "Travel": 0.20, "Activities": 0.10},
        "transport": "Private car / Taxi",
        "stay":      "Luxury hotel / Resort",
        "transport_options": [
            {"mode": "🚗 Private Car Hire", "ratio": 0.55, "icon": "car-front-fill", "tip": "Full-day hire with driver"},
            {"mode": "✈️ Domestic Flight",  "ratio": 0.30, "icon": "airplane-fill",  "tip": "For long distances"},
            {"mode": "🚕 Premium Cab",      "ratio": 0.15, "icon": "taxi-front-fill","tip": "Airport transfers & city rides"},
        ],
    },
}


def generate_plan(city, days, budget, budget_type, people, hotels, restaurants, attractions):
    budget = safe_int(budget, 10000)
    days   = safe_int(days, 1)
    people = safe_int(people, 1)

    profile = BUDGET_PROFILES.get(budget_type, BUDGET_PROFILES["medium"])
    amounts = {n: int(budget * r) for n, r in profile["ratios"].items()}
    total   = sum(amounts.values()) or 1

    breakdown = [
        {"name": n, "amount": a, "percent": int(a / total * 100)}
        for n, a in amounts.items()
    ]

    travel_budget       = amounts.get("Travel", 0)
    transport_breakdown = []
    for opt in profile["transport_options"]:
        cost = int(travel_budget * opt["ratio"])
        transport_breakdown.append({
            "mode":       opt["mode"],
            "icon":       opt["icon"],
            "tip":        opt["tip"],
            "total_cost": cost,
            "per_person": cost // people if people else cost,
            "percent":    int(opt["ratio"] * 100),
        })

    num_attr = len(attractions)
    num_rest = len(restaurants)
    itinerary = []
    for i in range(days):
        place = (
            attractions[i % num_attr]["name"] +
            (" (Extended Explore)" if i >= num_attr else "")
        ) if attractions else f"Explore {city} — Day {i + 1}"
        food = restaurants[i % num_rest]["name"] if restaurants else "Local restaurant"
        itinerary.append({"day": i + 1, "place": place, "food": food})

    return breakdown, itinerary, profile["transport"], profile["stay"], transport_breakdown


def compute_analytics(trip_history, all_feedbacks):
    cc = Counter(t["city"] for t in trip_history if t.get("city"))
    rm = Counter(int(f["rating"]) for f in all_feedbacks if f.get("rating"))
    return (
        list(cc.keys()), list(cc.values()),
        [str(i) for i in range(1, 6)],
        [rm.get(i, 0) for i in range(1, 6)],
    )


# ─────────────────────────────────────────────────────────────────────────────
# ROUTES
# ─────────────────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/planner")
def planner():
    return render_template("planner.html")


@app.route("/generate-trip", methods=["POST"])
def generate_trip():
    city        = request.form.get("destination", "").strip()
    days        = request.form.get("days",        "3")
    budget      = request.form.get("budget",      "10000")
    budget_type = request.form.get("budget_type", "medium")
    people      = request.form.get("people",      "1")

    if not city:
        return redirect("/planner")

    print(f"\n{'='*60}")
    print(f"[Trip] {city} | {days}d | ₹{budget} | {budget_type} | {people}p")
    print(f"{'='*60}")

    # Step 1: Geocode (Nominatim — accurate for Indian cities)
    geo_info  = geocode_city(city)
    lat       = geo_info["lat"]      or 0.0
    lon       = geo_info["lon"]      or 0.0
    canonical = geo_info["canonical"]
    state     = geo_info["state"]
    city_name = geo_info["city_name"]

    print(f"[Coords] lat={lat}, lon={lon}")
    print(f"[Canonical] {canonical!r}  [State] {state!r}")

    registry = ImageRegistry()

    # Step 2: Geo places + hero image (parallel)
    with ThreadPoolExecutor(max_workers=4) as ex:
        f_geo_h = ex.submit(_geo_places, city, "hotel",      lat, lon, 20)
        f_geo_r = ex.submit(_geo_places, city, "restaurant", lat, lon, 20)
        f_geo_a = ex.submit(_geo_places, city, "attraction", lat, lon, 20)
        f_hero  = ex.submit(get_city_hero, city, registry, lat, lon, canonical, state)

        geo_hotels      = f_geo_h.result()
        geo_restaurants = f_geo_r.result()
        geo_attractions = f_geo_a.result()
        hero_image      = f_hero.result()

    print(f"[Geo] H={len(geo_hotels)} R={len(geo_restaurants)} A={len(geo_attractions)}")

    # Step 3: Build place cards (FSQ + Wiki + Pexels, parallel)
    with ThreadPoolExecutor(max_workers=3) as ex:
        f_h = ex.submit(build_places, city, geo_hotels,      "hotel",
                        5, registry, lat, lon, canonical, state)
        f_r = ex.submit(build_places, city, geo_restaurants, "restaurant",
                        5, registry, lat, lon, canonical, state)
        f_a = ex.submit(build_places, city, geo_attractions, "attraction",
                        5, registry, lat, lon, canonical, state)
        hotels      = f_h.result()
        restaurants = f_r.result()
        attractions = f_a.result()

    print(f"[Final] H={len(hotels)} R={len(restaurants)} A={len(attractions)}")

    if not hero_image:
        hero_image = _picsum_placeholder(city, "India", "hero")

    breakdown, itinerary, transport, stay, transport_breakdown = generate_plan(
        city, days, budget, budget_type, people, hotels, restaurants, attractions
    )

    people_int = safe_int(people, 1)
    budget_int = safe_int(budget, 0)
    per_person = budget_int // people_int if people_int else budget_int

    trip_data = {
        "city": city, "days": days, "budget": budget,
        "people": people, "budget_type": budget_type, "per_person": per_person,
    }
    session["last_trip"] = trip_data

    if "user" in session:
        db_save_trip(session["user"], city, days, budget, people, budget_type, per_person)

    history = session.get("trip_history", [])
    if not history or history[0].get("city") != city:
        history.insert(0, trip_data)
    session["trip_history"] = history[:10]

    return render_template(
        "result.html",
        city=city, days=days, budget=budget, people=people,
        per_person=per_person, breakdown=breakdown, itinerary=itinerary,
        transport=transport, stay=stay,
        transport_breakdown=transport_breakdown,
        hotels=hotels, restaurants=restaurants, attractions=attractions,
        hero_image=hero_image,
    )


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = db_login_user(
            request.form.get("identifier", "").strip(),
            request.form.get("password",   "").strip()
        )
        if user:
            session["user"]      = user["email"]
            session["user_name"] = user.get("name", "Traveller")
            return redirect("/dashboard")
        return render_template("login.html", error="Invalid credentials.")
    return render_template("login.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "GET":
        return render_template("login.html", show_register=True)

    name     = request.form.get("name",     "").strip()
    email    = request.form.get("email",    "").strip().lower()
    phone    = request.form.get("phone",    "").strip()
    password = request.form.get("password", "").strip()

    if not all([name, email, phone, password]):
        return render_template("login.html", reg_error="All fields required.", show_register=True)

    ok, msg = db_register_user(name, email, phone, password)
    if not ok:
        return render_template("login.html", reg_error=msg, show_register=True)

    user = db_login_user(email, password)
    if user:
        session["user"]      = user["email"]
        session["user_name"] = user.get("name", "Traveller")
    return redirect("/dashboard")


@app.route("/dashboard")
def dashboard():
    if "user" not in session:
        return redirect("/login")

    user_email    = session["user"]
    all_trips     = db_get_all_trips()
    all_feedbacks = db_get_all_feedbacks()
    dest, counts, r_labels, r_counts = compute_analytics(all_trips, all_feedbacks)

    return render_template(
        "dashboard.html",
        user_name     = session.get("user_name", "Traveller"),
        user_email    = user_email,
        last_trip     = session.get("last_trip"),
        trip_history  = db_get_trips(user_email),
        feedbacks     = db_get_feedbacks(user_email),
        destinations  = dest,
        counts        = counts,
        rating_labels = r_labels,
        rating_counts = r_counts,
        total_users   = db_get_total_users(),
        total_trips   = db_get_total_trips(),
    )


@app.route("/feedback", methods=["POST"])
def feedback():
    rating  = request.form.get("rating")
    comment = request.form.get("comment", "")
    city    = session.get("last_trip", {}).get("city", "Unknown")
    user    = session.get("user", "Anonymous")
    if rating:
        db_save_feedback(user, city, int(rating), comment)
    return redirect("/dashboard")


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")


@app.errorhandler(404)
def not_found(e):
    return (
        "<h2 style='font-family:sans-serif;padding:40px'>"
        "404 – Not found. <a href='/'>Go Home</a></h2>", 404,
    )

@app.errorhandler(500)
def server_error(e):
    return (
        f"<h2 style='font-family:sans-serif;padding:40px'>"
        f"500 – {e} <a href='/'>Go Home</a></h2>", 500,
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)