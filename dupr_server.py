#!/usr/bin/env python3
"""
dupr_server.py — single-file DUPR lookup tool.

Combines everything dupr_profile.sh + the old server wrapper did into one
pure-Python script: login, match history fetching/pagination, HTML
rendering, and the local web server. No bash, curl, or jq required —
just the Python standard library.

This replaces dupr_profile.sh entirely for the "local server + browser"
workflow. (dupr_menu.sh / the .command launchers still call the bash
script directly, so if you want to keep using those too, leave
dupr_profile.sh in place alongside this — they don't conflict.)

Usage:
    python3 dupr_server.py
    (opens http://127.0.0.1:8787 in your browser automatically)

SECURITY NOTE: credentials (EMAIL, PASSWORD) and your PLAYERS list live
in the separate DuprSettings.py file, imported below — not in this
script. Keep DuprSettings.py out of any shared/synced or committed
location, and run `chmod 600 DuprSettings.py` on it.
"""

import calendar
import html
import json
import re
import ssl
import time
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse, parse_qs
from urllib.request import Request, urlopen

# Build a proper SSL context. Python installed from python.org on macOS
# doesn't automatically use the system certificate store, which causes
# "CERTIFICATE_VERIFY_FAILED" errors. certifi (a bundled CA cert list)
# fixes this reliably — we use it if available, falling back to the
# system default otherwise.
try:
    import certifi
    _SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    _SSL_CONTEXT = ssl.create_default_context()

# login credentials and player list moved to external python file
from DuprSettings import EMAIL, PASSWORD, PLAYERS

SCRIPT_VERSION = "1.5"   # bump this with each new build — helps confirm which version is actually running

# --- config ------------------------------------------------------------------
API_BASE = "https://api.dupr.com"   # migrated from api.dupr.gg — confirmed via live capture, 13 Sep 2026
HOST = "127.0.0.1"
PORT = 8787
TOKEN_CACHE_PATH = Path.home() / ".dupr_token_cache"
# Default location bias for DUPR's player search, used when no better location
# is known. Sydney coords — DUPR's search ranking appears to use this to order
# results, so an unbiased (null) search risks surfacing an unrelated
# same-named player first. Update if most of your searches are elsewhere.
DEFAULT_SEARCH_LAT = -33.8688197
DEFAULT_SEARCH_LNG = 151.2092955


# Common English first-name nickname pairs, used as a fallback when an exact
# name search comes back empty (e.g. tournament roster says "David Casserly"
# but DUPR has him registered as "Dave Casserly"). Not exhaustive — covers
# the common cases, not every possible variant or non-English name.
NICKNAMES = {
    "david": ["dave"], "dave": ["david"],
    "michael": ["mike"], "mike": ["michael"],
    "robert": ["rob", "bob", "bobby"], "rob": ["robert"], "bob": ["robert"], "bobby": ["robert"],
    "william": ["will", "bill", "billy"], "will": ["william"], "bill": ["william"], "billy": ["william"],
    "james": ["jim", "jimmy"], "jim": ["james"], "jimmy": ["james"],
    "richard": ["rick", "dick", "ricky"], "rick": ["richard"], "dick": ["richard"],
    "elizabeth": ["liz", "beth", "eliza", "lizzie"], "liz": ["elizabeth"], "beth": ["elizabeth"],
    "christopher": ["chris"], "chris": ["christopher"],
    "anthony": ["tony"], "tony": ["anthony"],
    "thomas": ["tom", "tommy"], "tom": ["thomas"],
    "joseph": ["joe", "joey"], "joe": ["joseph"],
    "daniel": ["dan", "danny"], "dan": ["daniel"],
    "matthew": ["matt"], "matt": ["matthew"],
    "andrew": ["andy", "drew"], "andy": ["andrew"],
    "stephen": ["steve"], "steven": ["steve"], "steve": ["stephen"],
    "patrick": ["pat", "paddy"], "pat": ["patrick"],
    "nicholas": ["nick"], "nick": ["nicholas"],
    "jonathan": ["jon", "johnny"], "jon": ["jonathan"],
    "katherine": ["kate", "kathy", "katie"], "catherine": ["kate", "cathy", "katie"],
    "kate": ["katherine", "catherine"],
    "jennifer": ["jen", "jenny"], "jen": ["jennifer"],
    "margaret": ["maggie", "meg", "peggy"], "maggie": ["margaret"],
    "deborah": ["deb", "debbie"], "deb": ["deborah"],
    "samuel": ["sam"], "sam": ["samuel"],
    "benjamin": ["ben"], "ben": ["benjamin"],
    "alexander": ["alex"], "alex": ["alexander"],
    "timothy": ["tim"], "tim": ["timothy"],
    "gregory": ["greg"], "greg": ["gregory"],
    "edward": ["ed", "eddie", "ted"], "ed": ["edward"],
    "charles": ["charlie", "chuck"], "charlie": ["charles"],
    "peter": ["pete"], "pete": ["peter"],
    "kenneth": ["ken", "kenny"], "ken": ["kenneth"],
    "donald": ["don", "donnie"], "don": ["donald"],
}


class DUPRError(Exception):
    pass


# =============================================================================
# pickleballtournaments.com client — tournament events + rosters
# (unauthenticated, no login required for these endpoints)
# =============================================================================

class PBError(Exception):
    pass


class PickleballClient:
    BASE = "https://pickleballtournaments.com"
    PROFILE_BASE = "https://pickleball.com"

    @staticmethod
    def parse_slug(text):
        """Accepts either a full tournament URL or a bare slug, returns the slug."""
        text = text.strip()
        if "pickleballtournaments.com/tournaments/" in text:
            after = text.split("/tournaments/", 1)[1]
            return after.split("/")[0].strip()
        return text

    def _get_json(self, url):
        req = Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        })
        try:
            with urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
                status = resp.status
                raw = resp.read()
        except HTTPError as e:
            status = e.code
            raw = e.read()
        except URLError as e:
            raise PBError(f"Network error reaching pickleballtournaments.com: {e.reason}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            snippet = raw.decode("utf-8", errors="replace")[:300]
            print(f"  [pb] non-JSON response from {url}")
            print(f"  [pb] HTTP status: {status}")
            print(f"  [pb] body snippet: {snippet!r}")
            raise PBError(
                f"pickleballtournaments.com returned a non-JSON response "
                f"(HTTP {status}). This is often transient — try again in a moment."
            )

    def _get_text(self, url):
        req = Request(url, headers={
            "Accept": "text/html",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        })
        try:
            with urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
                raw = resp.read()
        except HTTPError as e:
            raw = e.read()
        except URLError as e:
            raise PBError(f"Network error reaching pickleball.com: {e.reason}")
        return raw.decode("utf-8", errors="replace")

    def fetch_dupr_code(self, slug):
        """Scrapes the DUPR code off a player's pickleball.com profile page.
        The page is Next.js server-rendered, and the code lives inside an
        embedded JSON data payload as "duprId":"XXXXXX" — backslash-escaped
        since it's JSON nested inside a JS string (e.g. \\"duprId\\":\\"G5DN6M\\").
        Stripping backslashes first turns that back into plain JSON we can
        match directly. Falls back to the human-readable "DUPR ID:" label
        format too, in case that appears on some page variants."""
        if not slug:
            return None
        url = f"{self.PROFILE_BASE}/players/{slug}"
        raw_html = self._get_text(url)

        unescaped = raw_html.replace("\\", "")
        match = re.search(r'"duprId"\s*:\s*"([A-Za-z0-9]{2,12})"', unescaped)
        if match:
            return match.group(1).upper()

        plain_text = re.sub(r"<[^>]+>", " ", raw_html)
        plain_text = re.sub(r"\s+", " ", plain_text)
        match = re.search(r"DUPR ID:\s*([A-Z0-9]{4,10})", plain_text, re.IGNORECASE)
        if match:
            return match.group(1).upper()

        print(f"  [pb] no DUPR code found (either format) on {url}")
        return None

    @staticmethod
    def humanize_slug(slug):
        """Fallback display name when the API doesn't hand us a proper
        tournament title: 'barrington-coast-doubles-2026-indoors' ->
        'Barrington Coast Doubles 2026 Indoors'."""
        return " ".join(word.capitalize() for word in slug.replace("-", " ").split())

    def fetch_events(self, slug):
        """Returns (tournament_title, events) — events is a flat list of
        {title, activityId, numRegistered}. tournament_title is taken from
        the API response if it exposes one under a recognizable key,
        otherwise derived from the slug itself."""
        url = (f"{self.BASE}/tournaments/api/tourneyEvents"
               f"?slug={slug}&searchTerm=&needAPartnerOnly=false")
        data = self._get_json(url)
        groups = data.get("events") or []
        flat = []
        for group in groups:
            for ev in group.get("events") or []:
                flat.append({
                    "title": ev.get("title", "Untitled event"),
                    "activityId": ev.get("activityId"),
                    "numRegistered": ev.get("numOfRegistered", 0),
                })
        if not flat:
            raise PBError(
                "No events found. Check the tournament slug/URL is correct."
            )

        tournament_title = (
            data.get("tournamentTitle") or data.get("title") or data.get("name")
            or (data.get("tournament") or {}).get("title")
            or self.humanize_slug(slug)
        )
        return tournament_title, flat

    def fetch_roster(self, activity_id):
        """Returns the raw list of team-pairing dicts for one event."""
        url = (f"{self.BASE}/tournaments/api/eventPlayers"
               f"?activityId={activity_id}&activitySplitId=null")
        data = self._get_json(url)
        if not isinstance(data, list):
            raise PBError("Unexpected roster response shape.")
        return data


# =============================================================================
# DUPR API client — login, profile, match history + pagination
# =============================================================================

class DUPRClient:
    def __init__(self, email, password):
        self.email = email
        self.password = password
        self.token = None
        if TOKEN_CACHE_PATH.exists():
            self.token = TOKEN_CACHE_PATH.read_text().strip() or None

    # --- low-level HTTP -----------------------------------------------------

    def _raw_request(self, method, path, params=None, body=None, token=None):
        url = API_BASE + path
        if params:
            url += "?" + urlencode(params)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        }
        if token:
            # 'token' now holds the raw session cookie string
            # (e.g. "__Host-dupr_at=...; __Host-dupr_rt=..."), not a bearer
            # token — DUPR switched to httpOnly cookie auth, confirmed via
            # live capture on 13 Sep 2026.
            headers["Cookie"] = token
        data = json.dumps(body).encode() if body is not None else None
        req = Request(url, data=data, headers=headers, method=method)
        try:
            with urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
                status = resp.status
                raw = resp.read()
        except HTTPError as e:
            status = e.code
            raw = e.read()
        except URLError as e:
            if isinstance(e.reason, ssl.SSLCertVerificationError) or "CERTIFICATE" in str(e.reason):
                raise DUPRError(
                    "SSL certificate verification failed. Try: pip3 install certifi"
                )
            raise DUPRError(f"Network error reaching DUPR: {e.reason}")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            snippet = raw.decode("utf-8", errors="replace")[:300]
            print(f"  [dupr] non-JSON response from {url}")
            print(f"  [dupr] HTTP status: {status}")
            print(f"  [dupr] body snippet: {snippet!r}")
            raise DUPRError(
                f"DUPR returned a non-JSON response (HTTP {status}, possibly rate-limited)."
            )

    # --- auth -----------------------------------------------------------------

    def login(self):
        resp = self._raw_request(
            "POST", "/auth/v1.0/login",
            body={"email": self.email, "password": self.password},
        )

        # Path 1: legacy direct success (kept in case DUPR ever reverts —
        # unlikely now, but cheap to keep).
        if resp.get("status") == "SUCCESS" and resp.get("result", {}).get("accessToken"):
            self.token = resp["result"]["accessToken"]
            self._save_token(self.token)
            return

        # Path 2: 2FA challenge required — confirmed live behaviour as of
        # 13 Sep 2026, following DUPR's security incident. DUPR emails a
        # 6-digit code; the code must be submitted with the challengeToken
        # to /auth/v1.0/2fa/verify, which sets session cookies (no bearer
        # token in the response body at all anymore).
        if "TWO_FACTOR_CHALLENGE" in (resp.get("requiredActions") or []):
            challenge_token = resp.get("challengeToken")
            if not challenge_token:
                raise DUPRError("2FA challenge required but no challengeToken was returned.")

            print("\n" + "=" * 60)
            print(f"DUPR sent a 6-digit code to {self.email}.")
            code = input("Enter the code to finish signing in: ").strip()
            print("=" * 60 + "\n")

            self.token = self._verify_2fa(challenge_token, code)
            self._save_token(self.token)
            return

        print("  [dupr] login did not return a token. Full response:")
        print(f"  [dupr] {json.dumps(resp, indent=2)}")
        raise DUPRError(resp.get("message", "Unknown login error"))

    def _verify_2fa(self, challenge_token, code):
        """Submits the 6-digit code, and pulls the session cookies out of
        the response headers (DUPR sets them via Set-Cookie rather than
        returning a token in the JSON body)."""
        url = API_BASE + "/auth/v1.0/2fa/verify"
        data = json.dumps({"challengeToken": challenge_token, "code": code}).encode()
        req = Request(url, data=data, headers={
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)",
        }, method="POST")

        try:
            with urlopen(req, timeout=30, context=_SSL_CONTEXT) as resp:
                status = resp.status
                raw = resp.read()
                set_cookie_headers = resp.headers.get_all("Set-Cookie") or []
        except HTTPError as e:
            status = e.code
            raw = e.read()
            set_cookie_headers = e.headers.get_all("Set-Cookie") if e.headers else []
        except URLError as e:
            raise DUPRError(f"Network error during 2FA verification: {e.reason}")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            raise DUPRError(f"2FA verification returned a non-JSON response (HTTP {status}).")

        if parsed.get("status") != "SUCCESS":
            raise DUPRError(parsed.get("message", "2FA verification failed — check the code and try again."))

        cookie_parts = [
            sc.split(";", 1)[0].strip()
            for sc in set_cookie_headers
            if sc.strip().startswith("__Host-dupr_")
        ]
        if not cookie_parts:
            raise DUPRError("2FA succeeded but DUPR returned no session cookies.")

        return "; ".join(cookie_parts)

    def _save_token(self, token):
        TOKEN_CACHE_PATH.write_text(token)
        try:
            TOKEN_CACHE_PATH.chmod(0o600)
        except OSError:
            pass

    def _token_still_valid(self):
        if not self.token:
            return False
        resp = self._raw_request("GET", "/user/v1.0/profile", token=self.token)
        return resp.get("status") == "SUCCESS"

    def ensure_logged_in(self):
        if not self._token_still_valid():
            self.login()

    # --- profile ---------------------------------------------------------------

    def fetch_profile(self, player_id=None):
        self.ensure_logged_in()
        path = f"/player/v1.0/{player_id}" if player_id else "/user/v1.0/profile"
        resp = self._raw_request("GET", path, token=self.token)
        if resp.get("status") != "SUCCESS" or not resp.get("result"):
            raise DUPRError(resp.get("message", "Could not fetch profile"))
        result = resp["result"]
        stats = result.get("stats") or {}
        ratings = result.get("ratings") or {}
        addresses = result.get("addresses") or []
        return {
            "fullName": result.get("fullName", "Unknown"),
            "doubles": stats.get("doubles") or ratings.get("doubles") or "NR",
            "singles": stats.get("singles") or ratings.get("singles") or "NR",
            "location": (addresses[0].get("formattedAddress") if addresses
                         else result.get("shortAddress", "")),
            "imageUrl": result.get("imageUrl") or None,
            "age": self._extract_age(result),
        }

    @staticmethod
    def _extract_age(result):
        """DUPR's two profile shapes disagree: /player/v1.0/{id} (other
        players) returns a direct 'age' field; /user/v1.0/profile (your
        own) returns 'birthdate' instead. Either can also simply be
        absent — many players hide their age via a privacy setting."""
        age = result.get("age")
        if isinstance(age, int) and age > 0:
            return age
        birthdate = result.get("birthdate")
        if birthdate:
            try:
                birth = date.fromisoformat(birthdate)
                today = date.today()
                computed = today.year - birth.year - (
                    (today.month, today.day) < (birth.month, birth.day)
                )
                return computed
            except ValueError:
                pass
        return None

    # --- player search (name -> numeric ID + rating) ---------------------------

    def search_by_code(self, code, lat=None, lng=None):
        """Search DUPR by its short duprId code (e.g. 'L2OW4Z'). Unlike
        search_player, this is unambiguous — the code is unique per
        player, so no name-collision or nickname risk exists here."""
        self.ensure_logged_in()
        resp = self._raw_request(
            "POST", "/player/v1.0/search",
            body={
                "limit": 5, "offset": 0, "query": code, "exclude": [],
                "includeUnclaimedPlayers": True,
                "filter": {
                    "lat": lat if lat is not None else DEFAULT_SEARCH_LAT,
                    "lng": lng if lng is not None else DEFAULT_SEARCH_LNG,
                    "rating": {"maxRating": None, "minRating": None},
                    "locationText": "",
                },
            },
            token=self.token,
        )
        if resp.get("status") != "SUCCESS":
            raise DUPRError(resp.get("message", "DUPR search failed"))
        hits = (resp.get("result") or {}).get("hits") or []
        if not hits:
            return None
        target = code.strip().upper()
        exact = [h for h in hits if (h.get("duprId") or "").strip().upper() == target]
        top = exact[0] if exact else hits[0]
        ratings = top.get("ratings") or {}
        return {
            "id": top.get("id"),
            "duprId": top.get("duprId"),
            "fullName": top.get("fullName"),
            "doubles": ratings.get("doubles") or "NR",
            "matched_by": "code",
            "imageUrl": top.get("imageUrl") or None,
        }

    def search_player(self, name, lat=None, lng=None):
        """Search DUPR by name (or short duprId code). Prefers an exact
        (case-insensitive) full-name match among the results over blindly
        trusting the first hit, since DUPR's ranking can surface an
        unrelated same-ish-named player first — especially without a
        location bias, which is why lat/lng defaults to Sydney below.

        If the exact name draws a total blank, retries with common
        nickname variants of the first name (e.g. "David" -> "Dave")
        before giving up."""
        result = self._search_once(name, lat, lng)
        if result is not None:
            return result

        parts = name.strip().split(" ", 1)
        if len(parts) == 2:
            first, rest = parts
            for variant in NICKNAMES.get(first.lower(), []):
                result = self._search_once(f"{variant} {rest}", lat, lng)
                if result is not None:
                    result["nickname_variant"] = True
                    return result

        return None

    def _search_once(self, name, lat=None, lng=None):
        self.ensure_logged_in()
        resp = self._raw_request(
            "POST", "/player/v1.0/search",
            body={
                "limit": 10, "offset": 0, "query": name, "exclude": [],
                "includeUnclaimedPlayers": True,
                "filter": {
                    "lat": lat if lat is not None else DEFAULT_SEARCH_LAT,
                    "lng": lng if lng is not None else DEFAULT_SEARCH_LNG,
                    "rating": {"maxRating": None, "minRating": None},
                    "locationText": "",
                },
            },
            token=self.token,
        )
        if resp.get("status") != "SUCCESS":
            raise DUPRError(resp.get("message", "DUPR search failed"))
        hits = (resp.get("result") or {}).get("hits") or []
        if not hits:
            return None

        target = name.strip().lower()
        exact = [h for h in hits if (h.get("fullName") or "").strip().lower() == target]
        top = exact[0] if exact else hits[0]

        ratings = top.get("ratings") or {}
        return {
            "id": top.get("id"),
            "duprId": top.get("duprId"),
            "fullName": top.get("fullName"),
            "doubles": ratings.get("doubles") or "NR",
            "ambiguous": len(hits) > 1 and not exact,
            "imageUrl": top.get("imageUrl") or None,
            "matched_by": "name",
        }

    def search_players_list(self, name, lat=None, lng=None, limit=15):
        """Like search_player, but returns every hit instead of collapsing
        to one best guess — used by the "look up by name" feature, where
        the user picks the right person themselves from a candidate list
        rather than us guessing on their behalf."""
        self.ensure_logged_in()
        resp = self._raw_request(
            "POST", "/player/v1.0/search",
            body={
                "limit": limit, "offset": 0, "query": name, "exclude": [],
                "includeUnclaimedPlayers": True,
                "filter": {
                    "lat": lat if lat is not None else DEFAULT_SEARCH_LAT,
                    "lng": lng if lng is not None else DEFAULT_SEARCH_LNG,
                    "rating": {"maxRating": None, "minRating": None},
                    "locationText": "",
                },
            },
            token=self.token,
        )
        if resp.get("status") != "SUCCESS":
            raise DUPRError(resp.get("message", "DUPR search failed"))
        hits = (resp.get("result") or {}).get("hits") or []

        results = []
        for h in hits:
            ratings = h.get("ratings") or {}
            addresses = h.get("addresses") or []
            location = (h.get("shortAddress")
                        or (addresses[0].get("formattedAddress") if addresses else None))
            results.append({
                "id": h.get("id"),
                "duprId": h.get("duprId"),
                "fullName": h.get("fullName") or "Unknown",
                "doubles": ratings.get("doubles") or "NR",
                "imageUrl": h.get("imageUrl") or None,
                "location": location,
            })
        return results

    # --- match history -----------------------------------------------------------

    @staticmethod
    def _months_ago(months):
        today = date.today()
        year, month = today.year, today.month - months
        while month <= 0:
            month += 12
            year -= 1
        day = min(today.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    def fetch_matches(self, player_id, months_back=6):
        self.ensure_logged_in()
        cutoff = self._months_ago(months_back).isoformat()
        pid = int(player_id)
        offset, limit = 0, 25   # DUPR's API caps page size at 25
        rows = []

        while True:
            resp = self._raw_request(
                "GET", f"/player/v1.0/{player_id}/history",
                params={"limit": limit, "offset": offset}, token=self.token,
            )
            if resp.get("status") != "SUCCESS" or not resp.get("result"):
                raise DUPRError(resp.get("message", "Could not fetch match history"))
            result = resp["result"]
            hits = result.get("hits", [])
            if not hits:
                break

            oldest_in_page = min(h["eventDate"] for h in hits)

            for hit in hits:
                if hit.get("eventFormat") != "DOUBLES":
                    continue
                row = self._flatten(hit, pid)
                if row:
                    rows.append(row)

            offset += limit
            if not result.get("hasMore"):
                break
            if oldest_in_page < cutoff:
                break
            time.sleep(1)   # be polite to the API between pages

        filtered = [r for r in rows if r["date"] >= cutoff]
        filtered.sort(key=lambda r: r["date"], reverse=True)
        return filtered

    @staticmethod
    def _flatten(hit, pid):
        own_team = opp_team = None
        for team in hit["teams"]:
            ids = {team["player1"]["id"], team["player2"]["id"]}
            if pid in ids:
                own_team = team
            else:
                opp_team = team
        if not own_team or not opp_team:
            return None

        is_p1 = own_team["player1"]["id"] == pid
        partner_obj = own_team["player2"] if is_p1 else own_team["player1"]
        partner = partner_obj.get("fullName") or ""
        partner_id = partner_obj.get("id")
        opp1_obj = opp_team["player1"]
        opp2_obj = opp_team["player2"]
        opp1 = opp1_obj.get("fullName") or "?"
        opp2 = opp2_obj.get("fullName") or "?"
        opp1_id = opp1_obj.get("id")
        opp2_id = opp2_obj.get("id")

        own_games = [own_team.get(f"game{i}", -1) for i in range(1, 6)]
        opp_games = [opp_team.get(f"game{i}", -1) for i in range(1, 6)]
        score = ", ".join(f"{o}-{p}" for o, p in zip(own_games, opp_games) if o != -1)

        impact = own_team.get("preMatchRatingAndImpact") or {}
        delta_key = "matchDoubleRatingImpactPlayer1" if is_p1 else "matchDoubleRatingImpactPlayer2"
        delta = impact.get(delta_key) or 0.0

        own_player = own_team["player1"] if is_p1 else own_team["player2"]
        post_rating = (own_player.get("postMatchRating") or {}).get("doubles")

        return {
            "date": hit["eventDate"],
            "win": bool(own_team.get("winner")),
            "partner": partner,
            "partner_id": partner_id,
            "opponents": f"{opp1} / {opp2}",
            "opp1": opp1, "opp1_id": opp1_id,
            "opp2": opp2, "opp2_id": opp2_id,
            "score": score,
            "delta": delta,
            "post_rating": post_rating,
        }


# =============================================================================
# HTML rendering
# =============================================================================

def esc(s):
    return html.escape(s or "")


def format_date(iso_str):
    return date.fromisoformat(iso_str).strftime("%d %b %y")


def render_rating_chart(rows):
    """Inline SVG line chart of doubles rating over time, built with plain
    string formatting — no JS charting library or CDN dependency, matching
    the rest of this script. Returns '' if there isn't enough data to
    plot a meaningful trend."""
    points = [
        (r["date"], float(r["post_rating"]))
        for r in rows
        if r.get("post_rating") not in (None, "NR")
    ]
    points.sort(key=lambda p: p[0])   # chronological, oldest first
    if len(points) < 2:
        return ""

    width, height = 700, 200
    pad_left, pad_right, pad_top, pad_bottom = 44, 16, 16, 28

    ratings = [p[1] for p in points]
    r_min, r_max = min(ratings), max(ratings)
    if r_min == r_max:   # flat line — still show it, just fake a small range
        r_min -= 0.05
        r_max += 0.05
    r_range = r_max - r_min
    pad_range = r_range * 0.1
    r_min -= pad_range
    r_max += pad_range

    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom
    n = len(points)

    def x_at(i):
        return pad_left + (i / (n - 1)) * plot_w if n > 1 else pad_left

    def y_at(rating):
        return pad_top + (1 - (rating - r_min) / (r_max - r_min)) * plot_h

    coords = [(x_at(i), y_at(rating)) for i, (_, rating) in enumerate(points)]
    line_path = "M " + " L ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    area_path = (line_path
                 + f" L {coords[-1][0]:.1f},{pad_top + plot_h:.1f}"
                 + f" L {coords[0][0]:.1f},{pad_top + plot_h:.1f} Z")

    circles = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3" fill="#0F8A72">'
        f'<title>{format_date(points[i][0])}: {points[i][1]:.3f}</title></circle>'
        for i, (x, y) in enumerate(coords)
    )

    # y-axis labels at top/bottom of the plotted (padded) range
    y_labels = (
        f'<text x="{pad_left - 8}" y="{pad_top + 4}" font-size="11" fill="#6B7570" text-anchor="end">{r_max - pad_range:.2f}</text>'
        f'<text x="{pad_left - 8}" y="{pad_top + plot_h}" font-size="11" fill="#6B7570" text-anchor="end">{r_min + pad_range:.2f}</text>'
    )
    # x-axis labels: first and last date only, to avoid crowding
    x_labels = (
        f'<text x="{coords[0][0]:.1f}" y="{height - 6}" font-size="11" fill="#6B7570" text-anchor="start">{format_date(points[0][0])}</text>'
        f'<text x="{coords[-1][0]:.1f}" y="{height - 6}" font-size="11" fill="#6B7570" text-anchor="end">{format_date(points[-1][0])}</text>'
    )

    return f"""<svg viewBox="0 0 {width} {height}" style="width:100%; max-width:900px; height:auto;">
  <line x1="{pad_left}" y1="{pad_top + plot_h}" x2="{width - pad_right}" y2="{pad_top + plot_h}" stroke="#E3E7E5" stroke-width="1"/>
  <path d="{area_path}" fill="#0F8A72" fill-opacity="0.08" stroke="none"/>
  <path d="{line_path}" fill="none" stroke="#0F8A72" stroke-width="2"/>
  {circles}
  {y_labels}
  {x_labels}
</svg>"""


def _match_name_link(name, other_player_id):
    """Renders a name as a link to that player's own 6-month match history,
    if we have their DUPR ID — otherwise plain escaped text."""
    if other_player_id:
        return (f'<a href="/matches?id={other_player_id}&months=6" '
                f'style="color:inherit; text-decoration:none; border-bottom:1px dotted #999;">'
                f'{esc(name)}</a>')
    return esc(name)


def render_matches_html(player_name, player_id, doubles_rating, months, rows, image_url=None, age=None, location=None):
    try:
        rating_fmt = f"{float(doubles_rating):.2f}"
    except (TypeError, ValueError):
        rating_fmt = str(doubles_rating)

    if isinstance(age, int) and age > 0:
        age_html = f"Age: {age}"
    else:
        # Age hidden or unavailable — DUPR lets players opt out of showing it.
        age_html = "🎂 Age: NK"

    # Location is unobtrusive: only shown when DUPR actually has it, no
    # "not known" filler cluttering the subtitle when it's missing.
    location_segment = f" — 📍 {esc(location)}" if location else ""

    row_html = []
    for r in rows:
        wl = "W" if r["win"] else "L"
        wl_class = "win" if r["win"] else "loss"
        sign = "+" if r["delta"] >= 0 else ""
        delta_class = "delta-pos" if r["delta"] >= 0 else "delta-neg"
        post_rating = r.get("post_rating")
        rating_text = f"{post_rating:.3f}" if isinstance(post_rating, (int, float)) else "NR"

        partner_html = _match_name_link(r["partner"], r.get("partner_id"))
        opp1_html = _match_name_link(r.get("opp1", ""), r.get("opp1_id"))
        opp2_html = _match_name_link(r.get("opp2", ""), r.get("opp2_id"))
        opponents_html = f"{opp1_html} / {opp2_html}"

        row_html.append(
            f"<tr><td>{format_date(r['date'])}</td>"
            f'<td class="{wl_class}">{wl}</td>'
            f"<td>{partner_html}</td>"
            f"<td>{opponents_html}</td>"
            f'<td class="{wl_class}">{esc(r["score"])}</td>'
            f'<td class="{delta_class}">{sign}{r["delta"]:.3f}</td>'
            f'<td>{rating_text}</td></tr>'
        )

    generated = datetime.now().strftime("%d %b %y %H:%M")
    wins = sum(1 for r in rows if r["win"])
    losses = len(rows) - wins
    chart_svg = render_rating_chart(rows)
    chart_block = f'<div style="margin: 8px 0 24px;">{chart_svg}</div>' if chart_svg else ""

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>DUPR Match History — {esc(player_name)}</title>
<style>
  body {{ font-family: -apple-system, Helvetica, Arial, sans-serif; margin: 2em; color: #222; }}
  h1 {{ font-size: 1.3em; margin-bottom: 0.2em; }}
  .subtitle {{ color: #666; font-size: 0.85em; margin-bottom: 1.2em; }}
  table {{ border-collapse: collapse; width: 100%; max-width: 900px; }}
  th, td {{ padding: 6px 12px; border-bottom: 1px solid #ddd; text-align: left; font-size: 0.9em; }}
  th {{ background: #f4f4f4; }}
  tr:hover {{ background: #fafafa; }}
  .win {{ color: #1a7f37; font-weight: 600; }}
  .loss {{ color: #c0392b; font-weight: 600; }}
  .delta-pos {{ color: #1a7f37; font-weight: 600; }}
  .delta-neg {{ color: #c0392b; font-weight: 600; }}
  a {{ color: #2563eb; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<p><a href="/">&larr; Back to player list</a></p>
<div style="display:flex; align-items:center; gap:14px; margin-bottom:0.2em;">
{f'<img src="{esc(image_url)}" style="width:56px;height:56px;border-radius:50%;object-fit:cover;flex-shrink:0;" alt="">' if image_url else ''}
<h1 style="margin:0;">Match history — {esc(player_name)} ({rating_fmt})</h1>
</div>
<div class="subtitle">Player ID {player_id} — {esc(age_html)}{location_segment} — last {months} months — {len(rows)} matches (W:{wins}, L:{losses}) — generated {generated}</div>
{chart_block}
<table>
<tr><th>Date</th><th>W/L</th><th>Partner</th><th>Opponents</th><th>Score</th><th>Delta</th><th>Dupr</th></tr>
{''.join(row_html)}
</table>
</body></html>"""


def render_profile_html(profile, player_id):
    name = profile.get("fullName", "Unknown")
    image_url = profile.get("imageUrl")

    if image_url:
        avatar_html = f'<img src="{esc(image_url)}" class="profile-avatar" alt="">'
    else:
        color = _avatar_color(name)
        initials = _initials(name)
        avatar_html = f'<div class="profile-avatar" style="background:{color}; display:flex; align-items:center; justify-content:center; color:#fff; font-weight:600; font-size:1.4rem;">{esc(initials)}</div>'

    id_line = f"Player ID {esc(str(player_id))}" if player_id else "Your profile"
    matches_link = f"/matches?id={player_id}&months=6" if player_id else "/matches"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{esc(name)} — DUPR Profile</title>
<style>{_base_style()}
  .profile-header {{ display: flex; align-items: center; gap: 18px; margin-bottom: 24px; }}
  .profile-avatar {{ width: 88px; height: 88px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }}
  .stats-row {{ display: flex; }}
  .stat-block {{ flex: 1; padding: 18px 20px; text-align: center; }}
  .stat-block + .stat-block {{ border-left: 1px solid var(--border); }}
  .stat-value {{ font-size: 1.8rem; font-weight: 700; color: var(--accent); }}
  .stat-value.unrated {{ color: var(--text-muted); font-weight: 500; font-size: 1.3rem; }}
  .stat-label {{ font-size: 0.78rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.03em; margin-top: 2px; }}
  .location-row {{ padding: 14px 20px; border-top: 1px solid var(--border); font-size: 0.88rem; color: var(--text-muted); }}
  .cta-row {{ margin-top: 20px; }}
</style></head>
<body>
<div class="page">
  <a class="back-link" href="/">&larr; Back to player list</a>

  <div class="profile-header">
    {avatar_html}
    <div>
      <h1 style="margin-bottom:2px;">{esc(name)}</h1>
      <div class="subtitle" style="margin:0;">{id_line}</div>
    </div>
  </div>

  <div class="card">
    <div class="stats-row">
      <div class="stat-block">
        <div class="stat-value{' unrated' if profile.get('doubles') in (None, 'NR') else ''}">{esc(str(profile.get('doubles', 'NR')))}</div>
        <div class="stat-label">Doubles</div>
      </div>
      <div class="stat-block">
        <div class="stat-value{' unrated' if profile.get('singles') in (None, 'NR') else ''}">{esc(str(profile.get('singles', 'NR')))}</div>
        <div class="stat-label">Singles</div>
      </div>
    </div>
    <div class="location-row">{esc(profile.get('location') or 'Location unknown')}</div>
  </div>

  <div class="cta-row">
    <a href="{matches_link}" class="back-link" style="margin:0;">View 6-month match history &rarr;</a>
  </div>
</div>
</body></html>"""


def render_error_html(message):
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Error</title></head>
<body>
<p><a href="/">&larr; Back to player list</a></p>
<h1>Something went wrong</h1>
<pre>{esc(message)}</pre>
</body></html>"""


def _base_style():
    """Shared CSS so every page looks like one app. Returns the full <style> block."""
    return """
  :root {
    --bg: #F5F7F6;
    --card-bg: #FFFFFF;
    --border: #E3E7E5;
    --text: #1C2521;
    --text-muted: #6B7570;
    --accent: #0F8A72;
    --accent-dark: #0B6E5C;
    --radius: 12px;
  }
  * { box-sizing: border-box; }
  body {
    background: var(--bg);
    font-family: -apple-system, "SF Pro Text", Helvetica, Arial, sans-serif;
    color: var(--text);
    margin: 0;
    padding: 56px 24px;
  }
  .page { max-width: 640px; margin: 0 auto; }
  h1 { font-size: 1.6rem; font-weight: 700; letter-spacing: -0.02em; margin: 0 0 6px; }
  .subtitle { color: var(--text-muted); font-size: 0.95rem; margin: 0 0 24px; }
  .back-link { color: var(--text-muted); font-size: 0.85rem; text-decoration: none; display: inline-block; margin-bottom: 16px; }
  .back-link:hover { color: var(--accent); }
  .card { background: var(--card-bg); border: 1px solid var(--border); border-radius: var(--radius); overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }
  .lookup-card { padding: 18px 20px; }
  .lookup-form { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  input[type=text], input[type=number] { padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; font-size: 0.85rem; }
  input[type=text] { width: 100%; max-width: 420px; }
  button.primary { background: var(--accent); color: #fff; border: none; padding: 8px 16px; border-radius: 6px; font-size: 0.85rem; font-weight: 600; cursor: pointer; }
  button.primary:hover { background: var(--accent-dark); }
  .event-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 14px 20px; border-bottom: 1px solid var(--border); text-decoration: none; color: var(--text); }
  .event-row:last-child { border-bottom: none; }
  .event-row:hover { background: #FAFCFB; }
  .event-title { font-size: 0.92rem; }
  .event-count { font-size: 0.78rem; color: var(--text-muted); flex-shrink: 0; }
  .team-card { border: 1px solid var(--border); border-radius: 12px; overflow: hidden; margin-bottom: 10px; }
  .player-line { display: flex; align-items: center; gap: 10px; padding: 10px 12px; }
  .pair-divider { display: flex; align-items: center; gap: 6px; padding: 2px 12px 2px 26px; font-size: 0.7rem; color: var(--text-muted); }
  .avatar-sm { width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 600; font-size: 0.7rem; color: #fff; flex-shrink: 0; }
  .player-meta { flex: 1; min-width: 0; }
  .player-name { font-size: 0.88rem; }
  .player-detail { font-size: 0.75rem; color: var(--text-muted); margin: 1px 0 0; }
  .rating-pill { font-size: 0.75rem; font-weight: 600; padding: 3px 8px; border-radius: 6px; flex-shrink: 0; background: #E1F5EE; color: #085041; }
  .rating-pill.unresolved { background: transparent; color: var(--text-muted); font-weight: 400; }
  .empty { background: var(--card-bg); border: 1px dashed var(--border); border-radius: var(--radius); padding: 24px; color: var(--text-muted); font-size: 0.9rem; }
  code { background: #EEF2F0; padding: 2px 5px; border-radius: 4px; }
"""


def render_tournament_form(slug="", events=None, error=None, tournament_title=None):
    if error:
        body = f'<div class="empty">{esc(error)}</div>'
    elif events is not None:
        rows = "".join(
            f'<a class="event-row" href="/tournament/roster?slug={slug}&activity_id={ev["activityId"]}&title={esc(ev["title"])}">'
            f'<span class="event-title">{esc(ev["title"])}</span>'
            f'<span class="event-count">{ev["numRegistered"]} registered</span>'
            f'</a>'
            for ev in events
        )
        body = f'<div class="card">{rows}</div>'
    else:
        body = ""

    heading = esc(tournament_title) if tournament_title else "Event DUPR Lookup"
    page_title = tournament_title or "Event DUPR Lookup"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{esc(page_title)}</title>
<style>{_base_style()}</style></head>
<body>
<div class="page">
  <a class="back-link" href="/">&larr; Back to player list</a>
  <h1>{heading}</h1>
  <p class="subtitle">Paste a tournament link to see its events and real DUPR ratings for each entrant.</p>
  <div class="card lookup-card">
    <form class="lookup-form" action="/tournament" method="get">
      <input type="text" name="slug" placeholder="pickleballtournaments.com/tournaments/..." value="{esc(slug)}" required>
      <button type="submit" class="primary">Find events</button>
    </form>
  </div>
  {'<div style="height: 20px;"></div>' if body else ''}
  {body}
</div>
</body></html>"""


def render_search_html(query, results, error=None):
    """Name lookup: enter a name, get back every DUPR player that could
    plausibly match, each one linking straight to their match history."""
    if error:
        body = f'<div class="empty">{esc(error)}</div>'
    elif query is None:
        body = ""
    elif not results:
        body = f'<div class="empty">No DUPR players found matching "{esc(query)}".</div>'
    else:
        rows = []
        for r in results:
            color = _avatar_color(r["fullName"])
            initials = _initials(r["fullName"])
            if r.get("imageUrl"):
                avatar_html = f'<img src="{esc(r["imageUrl"])}" class="avatar-sm" style="object-fit:cover;" alt="" loading="lazy">'
            else:
                avatar_html = f'<div class="avatar-sm" style="background:{color};">{esc(initials)}</div>'

            try:
                rating_fmt = f'{float(r["doubles"]):.2f}'
                rating_html = f'<span class="rating-pill">DUPR {rating_fmt}</span>'
            except (TypeError, ValueError):
                rating_html = '<span class="rating-pill unresolved">NR</span>'

            detail_bits = []
            if r.get("location"):
                detail_bits.append(esc(r["location"]))
            if r.get("duprId"):
                detail_bits.append(f'DUPR ID: {esc(r["duprId"])}')
            detail_html = (f'<p class="player-detail">{" &middot; ".join(detail_bits)}</p>'
                           if detail_bits else "")

            rows.append(f"""
<a class="event-row" href="/matches?id={r['id']}&months=6">
  <div class="player-line" style="padding:0; flex:1; min-width:0;">
    {avatar_html}
    <div class="player-meta">
      <div class="player-name">{esc(r['fullName'])}</div>
      {detail_html}
    </div>
  </div>
  {rating_html}
</a>""")
        body = f'<div class="card">{"".join(rows)}</div>'

    query_val = esc(query) if query else ""

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Name Lookup — DUPR</title>
<style>{_base_style()}
  a.event-row {{ text-decoration: none; }}
  a.event-row:hover .player-name {{ color: var(--accent); }}
</style></head>
<body>
<div class="page">
  <a class="back-link" href="/">&larr; Back to player list</a>
  <h1>Name Lookup</h1>
  <p class="subtitle">Search DUPR by name and jump straight to a match history page.</p>
  <div class="card lookup-card">
    <form class="lookup-form" action="/search" method="get">
      <input type="text" name="name" placeholder="Player name" value="{query_val}" required autofocus>
      <button type="submit" class="primary">Search</button>
    </form>
  </div>
  {'<div style="height: 20px;"></div>' if body else ''}
  {body}
</div>
</body></html>"""


def render_roster_fragment(teams):
    """Just the team-card list — used both by the full page and the
    background data endpoint that fills it in asynchronously."""
    if not teams:
        return '<div class="empty">No registered teams found for this event.</div>'
    team_html = []
    for t in teams:
        p1 = _render_pb_player(t.get("player"), t.get("player_dupr"))
        rows = [p1]
        if t.get("partner"):
            rows.append(
                '<div class="pair-divider"><i class="ti ti-corner-down-right" '
                'aria-hidden="true"></i>paired with</div>'
            )
            rows.append(_render_pb_player(t.get("partner"), t.get("partner_dupr")))
        team_html.append(f'<div class="team-card">{"".join(rows)}</div>')
    return "".join(team_html)


def render_roster_html(event_title, teams, slug=""):
    """Full roster page rendered synchronously (kept for completeness /
    fallback use) — the live route uses render_roster_shell instead so the
    page appears instantly while resolution happens in the background."""
    body = render_roster_fragment(teams)
    back_url = f"/tournament?slug={slug}" if slug else "/tournament"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{esc(event_title)}</title>
<style>{_base_style()}</style></head>
<body>
<div class="page">
  <a class="back-link" href="{back_url}">&larr; Back to events</a>
  <h1>{esc(event_title)}</h1>
  <p class="subtitle">{len(teams)} teams — DUPR doubles ratings resolved automatically</p>
  {body}
</div>
</body></html>"""


def render_roster_shell(event_title, data_url, slug=""):
    """Loads instantly. Shows rotating status text while a background
    fetch() call resolves the actual roster (which involves multiple
    slow, sequential lookups per player) and swaps it in once ready."""
    status_phrases = [
        "Fetching the team roster…",
        "Looking up each player's DUPR profile…",
        "Cross-checking ratings…",
        "Almost there…",
    ]
    back_url = f"/tournament?slug={slug}" if slug else "/tournament"
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>{esc(event_title)}</title>
<style>{_base_style()}
  .loading-wrap {{ padding: 40px 0; text-align: center; color: var(--text-muted); }}
  .spinner {{
    width: 22px; height: 22px; margin: 0 auto 14px;
    border: 2px solid var(--border); border-top-color: var(--accent);
    border-radius: 50%; animation: spin 0.8s linear infinite;
  }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}
  #status-text {{ font-size: 0.88rem; }}
</style></head>
<body>
<div class="page">
  <a class="back-link" href="{back_url}">&larr; Back to events</a>
  <h1>{esc(event_title)}</h1>
  <p class="subtitle" id="subtitle">Resolving DUPR ratings for each player…</p>
  <div id="content">
    <div class="loading-wrap">
      <div class="spinner"></div>
      <div id="status-text">{esc(status_phrases[0])}</div>
    </div>
  </div>
</div>
<script>
  const phrases = {json.dumps(status_phrases)};
  let i = 0;
  const statusEl = document.getElementById('status-text');
  const cycle = setInterval(() => {{
    i = (i + 1) % phrases.length;
    statusEl.textContent = phrases[i];
  }}, 2200);

  fetch({json.dumps(data_url)})
    .then(r => r.json())
    .then(data => {{
      clearInterval(cycle);
      if (data.error) {{
        document.getElementById('content').innerHTML =
          '<div class="empty">Something went wrong: ' + data.error + '</div>';
        return;
      }}
      document.getElementById('subtitle').textContent = data.subtitle;
      document.getElementById('content').innerHTML = data.html;
    }})
    .catch(err => {{
      clearInterval(cycle);
      document.getElementById('content').innerHTML =
        '<div class="empty">Something went wrong loading this roster: ' + err + '</div>';
    }});
</script>
</body></html>"""


def _render_pb_player(entry, dupr):
    if not entry or not entry.get("name") or entry["name"].strip() == "":
        return ""
    name = entry["name"]
    age = entry.get("age")
    age_text = f"{age}" if age else "Age unknown"
    gender = entry.get("gender") or "?"
    city_state = entry.get("cityState", "").strip() or "Location unknown"

    color = _avatar_color(name)
    initials = _initials(name)

    if dupr:
        rating_html = f'<span class="rating-pill">DUPR {esc(str(dupr["doubles"]))}</span>'
        if dupr.get("matched_by") == "name":
            if dupr.get("ambiguous"):
                rating_html += ' <span class="rating-pill unresolved" title="Matched by name only, and multiple DUPR players share it — verify manually">&#9888; verify (by name)</span>'
            elif dupr.get("nickname_variant"):
                rating_html += f' <span class="rating-pill unresolved" title="Matched via nickname as {esc(dupr["fullName"])} — no DUPR code available on their pickleball.com profile">by name (nickname)</span>'
            else:
                rating_html += ' <span class="rating-pill unresolved" title="No DUPR code found on their pickleball.com profile — matched by name only">by name</span>'
    else:
        rating_html = '<span class="rating-pill unresolved">Not found on DUPR</span>'

    if dupr and dupr.get("id"):
        name_html = f'<a href="/matches?id={dupr["id"]}&months=6" style="color:inherit; text-decoration:none; border-bottom:1px dotted var(--text-muted);">{esc(name)}</a>'
    else:
        name_html = esc(name)

    image_url = dupr.get("imageUrl") if dupr else None
    if image_url:
        avatar_html = f'<img src="{esc(image_url)}" class="avatar-sm" style="object-fit:cover;" alt="" loading="lazy">'
    else:
        avatar_html = f'<div class="avatar-sm" style="background:{color};">{esc(initials)}</div>'

    return f"""<div class="player-line">
  {avatar_html}
  <div class="player-meta">
    <div class="player-name">{name_html}</div>
    <p class="player-detail">{esc(age_text)} &middot; {esc(gender)} &middot; {esc(city_state)}</p>
  </div>
  {rating_html}
</div>"""


AVATAR_COLORS = ["#0F8A72", "#2563EB", "#B45309", "#7C3AED", "#0E7490"]


def _avatar_color(name):
    return AVATAR_COLORS[sum(ord(c) for c in name) % len(AVATAR_COLORS)]


def _initials(name):
    parts = [p for p in name.split() if p]
    if len(parts) >= 2:
        return (parts[0][0] + parts[-1][0]).upper()
    return name[:2].upper() if name else "?"


def render_index():
    if not PLAYERS:
        body = (
            '<div class="empty">No players configured yet — edit '
            '<code>PLAYERS</code> at the top of dupr_server.py.</div>'
        )
    else:
        player_rows = []
        for name, pid in PLAYERS.items():
            color = _avatar_color(name)
            initials = _initials(name)
            player_rows.append(f"""
<div class="player-row">
  <div class="avatar" data-player-id="{pid}" style="background:{color};">{esc(initials)}</div>
  <div class="player-info">
    <div class="player-name">{esc(name)}</div>
    <a class="profile-link" href="/profile?id={pid}">View profile</a>
  </div>
  <form class="controls" action="/matches" method="get">
    <input type="hidden" name="id" value="{pid}">
    <input type="number" name="months" value="6" min="1" max="24">
    <span class="months-label">months</span>
    <button type="submit" class="primary">View matches</button>
  </form>
</div>""")
        body = f'<div class="card">{"".join(player_rows)}</div>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Doubles DUPR Lookup</title>
<style>
  :root {{
    --bg: #F5F7F6;
    --card-bg: #FFFFFF;
    --border: #E3E7E5;
    --text: #1C2521;
    --text-muted: #6B7570;
    --accent: #0F8A72;
    --accent-dark: #0B6E5C;
    --radius: 12px;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--bg);
    font-family: -apple-system, "SF Pro Text", Helvetica, Arial, sans-serif;
    color: var(--text);
    margin: 0;
    padding: 56px 24px;
  }}
  .page {{ max-width: 640px; margin: 0 auto; }}
  h1 {{
    font-size: 1.6rem;
    font-weight: 700;
    letter-spacing: -0.02em;
    margin: 0 0 6px;
  }}
  .subtitle {{
    color: var(--text-muted);
    font-size: 0.95rem;
    margin: 0 0 28px;
  }}
  h2.section-label {{
    font-size: 0.8rem;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--text-muted);
    margin: 28px 0 10px;
  }}
  .card {{
    background: var(--card-bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
  }}
  .player-row {{
    display: flex;
    align-items: center;
    gap: 14px;
    padding: 16px 20px;
    border-bottom: 1px solid var(--border);
  }}
  .player-row:last-child {{ border-bottom: none; }}
  .avatar {{
    width: 40px; height: 40px;
    border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-weight: 600; font-size: 0.8rem; color: #fff;
    flex-shrink: 0;
  }}
  .player-info {{ flex: 1; min-width: 0; }}
  .player-name {{ font-weight: 600; font-size: 0.95rem; }}
  .profile-link {{
    color: var(--text-muted);
    font-size: 0.8rem;
    text-decoration: none;
    border-bottom: 1px dotted var(--text-muted);
  }}
  .profile-link:hover {{ color: var(--accent); border-color: var(--accent); }}
  .controls {{
    display: flex; align-items: center; gap: 8px;
    margin: 0; flex-shrink: 0;
  }}
  .months-label {{ font-size: 0.82rem; color: var(--text-muted); }}
  input[type=number], input[type=text] {{
    padding: 6px 8px;
    border: 1px solid var(--border);
    border-radius: 6px;
    font-size: 0.85rem;
  }}
  input[type=number] {{ width: 52px; }}
  input[type=text] {{ width: 150px; }}
  button.primary {{
    background: var(--accent);
    color: #fff;
    border: none;
    padding: 7px 14px;
    border-radius: 6px;
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
  }}
  button.primary:hover {{ background: var(--accent-dark); }}
  button.secondary {{
    background: transparent;
    color: var(--accent);
    border: 1px solid var(--border);
    padding: 7px 14px;
    border-radius: 6px;
    font-size: 0.85rem;
    font-weight: 600;
    cursor: pointer;
  }}
  button.secondary:hover {{ border-color: var(--accent); }}
  .lookup-card {{ padding: 18px 20px; }}
  .lookup-form {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }}
  .empty {{
    background: var(--card-bg);
    border: 1px dashed var(--border);
    border-radius: var(--radius);
    padding: 24px;
    color: var(--text-muted);
    font-size: 0.9rem;
  }}
  code {{ background: #EEF2F0; padding: 2px 5px; border-radius: 4px; }}
</style></head>
<body>
<div class="page">
  <h1>Doubles DUPR Lookup</h1>
  <p class="subtitle">Track doubles ratings and match history.</p>

  <p style="margin: -12px 0 20px; display: flex; gap: 18px;">
    <a href="/tournament" style="color: var(--accent); font-size: 0.85rem; text-decoration: none; font-weight: 600;">Event DUPR Lookup &rarr;</a>
    <a href="/search" style="color: var(--accent); font-size: 0.85rem; text-decoration: none; font-weight: 600;">Search by name &rarr;</a>
  </p>

  <h2 class="section-label">Look up any player</h2>
  <div class="card lookup-card">
    <form class="lookup-form" action="/matches" method="get">
      <input type="text" name="id" placeholder="DUPR player ID" required>
      <input type="number" name="months" value="6" min="1" max="24">
      <span class="months-label">months</span>
      <button type="submit" class="primary">View matches</button>
      <button type="submit" formaction="/profile" class="secondary">View profile</button>
    </form>
  </div>

  <div style="height: 24px;"></div>

  {body}
</div>
<script>
  // Quietly swap in real DUPR photos after the page has already painted,
  // so the page still loads instantly — colored initials are a perfectly
  // fine avatar on their own, this is just a nice-to-have upgrade.
  fetch('/players/photos')
    .then(r => r.json())
    .then(photos => {{
      document.querySelectorAll('.avatar[data-player-id]').forEach(el => {{
        const url = photos[el.dataset.playerId];
        if (!url) return;   // no photo for this player — leave initials as-is
        const img = document.createElement('img');
        img.src = url;
        img.alt = '';
        img.loading = 'lazy';
        img.style.cssText = 'width:40px;height:40px;border-radius:50%;object-fit:cover;';
        el.replaceWith(img);
      }});
    }})
    .catch(() => {{}});   // fail silently — initials stay, nothing broken
</script>
</body></html>"""


# =============================================================================
# HTTP server
# =============================================================================

client = DUPRClient(EMAIL, PASSWORD)
pb_client = PickleballClient()
_resolve_cache = {}   # cache key -> search result or None, kept for the server's lifetime


def resolve_dupr(name, slug=None):
    """Resolves a roster entry to a DUPR profile.

    Primary path (exact, collision-proof): use the player's pickleball.com
    slug to scrape their public DUPR code, then look that code up on DUPR
    directly. Falls back to fuzzy name search (with nickname handling)
    only if no slug is available or the profile page doesn't expose a
    DUPR code — that fallback result is flagged 'matched_by': 'name' so
    the UI can show it as less certain.
    """
    cache_key = slug or f"name:{name.strip().lower()}"
    if cache_key in _resolve_cache:
        return _resolve_cache[cache_key]

    result = None
    if slug:
        try:
            code = pb_client.fetch_dupr_code(slug)
        except PBError as e:
            code = None
            print(f"  [pb] error fetching profile for slug={slug}: {e}")
        if code:
            result = client.search_by_code(code)
            print(f"  [resolve] {name}: matched by CODE ({code}) -> {result['doubles'] if result else 'no DUPR hit'}")
        time.sleep(0.3)   # be polite between profile-page fetches

    if result is None and name:
        result = client.search_player(name)
        print(f"  [resolve] {name}: fell back to NAME search -> "
              f"{result['doubles'] if result else 'not found'}")
        time.sleep(0.3)   # be polite between DUPR search calls

    _resolve_cache[cache_key] = result
    return result


def build_roster_teams(activity_id):
    """Fetches an event's roster and resolves every player's DUPR rating.
    This is the slow part (one or two HTTP calls per unique player) —
    kept as its own function so it can be called from the async data
    endpoint while the shell page shows a loading state."""
    raw_teams = pb_client.fetch_roster(activity_id)
    teams = []
    for entry in raw_teams:
        if not entry.get("isRegistered"):
            continue
        player_name = (entry.get("playerFullName") or "").strip()
        partner_name = (entry.get("partnerFullName") or "").strip()
        player = {
            "name": player_name,
            "age": entry.get("playerAge") or None,
            "gender": entry.get("playerGender"),
            "cityState": entry.get("playerCityState", ""),
        }
        partner = {
            "name": partner_name,
            "age": entry.get("partnerAge") or None,
            "gender": entry.get("partnerGender"),
            "cityState": entry.get("partnerCityState", ""),
        } if partner_name else None

        player_dupr = resolve_dupr(player_name, entry.get("playerSlug")) if player_name else None
        partner_dupr = resolve_dupr(partner_name, entry.get("partnerSlug")) if partner_name else None

        teams.append({
            "player": player, "player_dupr": player_dupr,
            "partner": partner, "partner_dupr": partner_dupr,
        })
    return teams


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print(f"  {self.address_string()} - {fmt % args}")

    def send_html(self, body, status=200):
        encoded = body.encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass   # client navigated away / closed the connection before we responded — nothing to do

    def send_json(self, obj, status=200):
        encoded = json.dumps(obj).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass   # same as above — the client is already gone

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        pid = qs.get("id", [None])[0]
        months = int(qs.get("months", ["6"])[0])

        try:
            if parsed.path == "/":
                self.send_html(render_index())

            elif parsed.path == "/search":
                name_q = (qs.get("name", [None])[0] or "").strip()
                if not name_q:
                    self.send_html(render_search_html(None, []))
                    return
                try:
                    results = client.search_players_list(name_q)
                except DUPRError as e:
                    self.send_html(render_search_html(name_q, [], error=f"Search failed: {e}"))
                    return
                self.send_html(render_search_html(name_q, results))

            elif parsed.path == "/players/photos":
                photos = {}
                for pid in PLAYERS.values():
                    try:
                        profile = client.fetch_profile(pid)
                        photos[pid] = profile.get("imageUrl")
                    except DUPRError:
                        photos[pid] = None   # one player failing shouldn't break the rest
                self.send_json(photos)

            elif parsed.path == "/matches":
                if not pid:
                    self.send_html(render_error_html("Missing player id."), status=400)
                    return
                profile = client.fetch_profile(pid)
                rows = client.fetch_matches(pid, months)
                self.send_html(render_matches_html(
                    profile["fullName"], pid, profile["doubles"], months, rows,
                    image_url=profile.get("imageUrl"),
                    age=profile.get("age"),
                    location=profile.get("location"),
                ))

            elif parsed.path == "/profile":
                profile = client.fetch_profile(pid)
                self.send_html(render_profile_html(profile, pid))

            elif parsed.path == "/tournament":
                raw_slug = qs.get("slug", [""])[0]
                if not raw_slug:
                    self.send_html(render_tournament_form())
                    return
                slug = PickleballClient.parse_slug(raw_slug)
                try:
                    tournament_title, events = pb_client.fetch_events(slug)
                    self.send_html(render_tournament_form(
                        slug=raw_slug, events=events, tournament_title=tournament_title
                    ))
                except PBError as e:
                    self.send_html(render_tournament_form(slug=raw_slug, error=str(e)))

            elif parsed.path == "/tournament/roster":
                activity_id = qs.get("activity_id", [None])[0]
                title = qs.get("title", ["Event"])[0]
                slug = qs.get("slug", [""])[0]
                if not activity_id:
                    self.send_html(render_error_html("Missing activity_id."), status=400)
                    return
                data_url = "/tournament/roster/data?" + urlencode({
                    "activity_id": activity_id, "title": title,
                })
                self.send_html(render_roster_shell(title, data_url, slug=slug))

            elif parsed.path == "/tournament/roster/data":
                activity_id = qs.get("activity_id", [None])[0]
                title = qs.get("title", ["Event"])[0]
                if not activity_id:
                    self.send_json({"error": "Missing activity_id."}, status=400)
                    return
                try:
                    teams = build_roster_teams(activity_id)
                    self.send_json({
                        "subtitle": f"{len(teams)} teams — DUPR doubles ratings resolved automatically",
                        "html": render_roster_fragment(teams),
                    })
                except (DUPRError, PBError) as e:
                    self.send_json({"error": str(e)}, status=500)

            else:
                self.send_html("<h1>404</h1><p><a href='/'>Back home</a></p>", status=404)

        except DUPRError as e:
            self.send_html(render_error_html(str(e)), status=500)
        except PBError as e:
            self.send_html(render_error_html(str(e)), status=500)
        except Exception as e:  # noqa: BLE001 — surface anything unexpected too
            self.send_html(render_error_html(f"Unexpected error: {e}"), status=500)


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    url = f"http://{HOST}:{PORT}"
    print(f"DUPR server v{SCRIPT_VERSION} running at {url}")
    print("Press Ctrl+C to stop.")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping server...")
        server.shutdown()