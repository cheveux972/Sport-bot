"""
winamax_scraper.py (v3 - simplifie)
"""
import asyncio
import json
import logging
import os
import re
import time
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

from playwright.async_api import async_playwright

WINAMAX_URL = "https://www.winamax.fr/paris-sportifs/sports"
SCROLL_PAUSE_MS = 600
CAPTURE_DURATION_S = 90
POLL_INTERVAL_S = 60
MAX_SCROLL_LOOPS = 40
OUTPUT_DIR = Path(__file__).parent.parent / "data"
CACHE_MAX_AGE_S = 300

SPORT_IDS = {
    1: "Football",
    2: "Tennis",
    5: "Basketball",
    6: "Hockey sur glace",
    13: "Rugby",
    23: "Volleyball",
    4: "Handball",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

import random
log = logging.getLogger("winamax")


@dataclass
class Odds:
    home: float = None
    draw: float = None
    away: float = None
    total_over_25: float = None
    total_under_25: float = None
    btts_yes: float = None
    btts_no: float = None

    def is_complete(self):
        return self.home is not None and self.away is not None


@dataclass
class WinamaxMatch:
    match_id: str
    sport_id: int
    sport_name: str
    league: str
    home: str
    away: str
    start_ts: int
    status: str
    score_home: int = None
    score_away: int = None
    minute: int = None
    odds: Odds = field(default_factory=Odds)
    raw_markets: dict = field(default_factory=dict)
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def start_dt(self):
        return datetime.fromtimestamp(self.start_ts, tz=timezone.utc)

    @property
    def is_live(self):
        return self.status == "LIVE"

    @property
    def minutes_until_start(self):
        return max(0.0, (self.start_ts - time.time()) / 60)

    def to_dict(self):
        d = asdict(self)
        d["start_iso"] = self.start_dt.isoformat()
        d["minutes_until_start"] = round(self.minutes_until_start, 1)
        return d


class WinamaxParser:
    _FRAME_RE = re.compile(r"^4\d(\[.+\])$", re.DOTALL)

    @classmethod
    def parse_frame(cls, raw):
        raw = raw.strip()
        m = cls._FRAME_RE.match(raw)
        if not m:
            return None
        try:
            payload = json.loads(m.group(1))
            if isinstance(payload, list) and len(payload) >= 2:
                return {"event": payload[0], "data": payload[1]}
        except Exception:
            pass
        return None

    @classmethod
    def extract_matches(cls, messages):
        matches = {}
        for raw in messages:
            frame = cls.parse_frame(raw)
            if not frame:
                continue
            event = frame["event"]
            data = frame["data"]
            if event in ("sports_index", "matches_index", "initial_data", "event_list"):
                for item in cls._iter_match_list(data):
                    m = cls._build_match(item)
                    if m:
                        existing = matches.get(m.match_id)
                        if not existing or not existing.odds.is_complete():
                            matches[m.match_id] = m
            elif event in ("market_update", "odds_update", "odds_change"):
                mid = cls._extract_match_id(data)
                if mid and mid in matches:
                    cls._apply_market(matches[mid], data)
            elif event in ("score_update", "match_update", "live_update"):
                mid = cls._extract_match_id(data)
                if mid and mid in matches:
                    cls._apply_score(matches[mid], data)
        return list(matches.values())

    @staticmethod
    def _extract_match_id(data):
        if not isinstance(data, dict):
            return None
        raw = data.get("matchId") or data.get("match_id") or data.get("id")
        return str(raw) if raw else None

    @staticmethod
    def _iter_match_list(data):
        if isinstance(data, list):
            yield from data
        elif isinstance(data, dict):
            for key in ("matches", "data", "events", "items"):
                val = data.get(key)
                if isinstance(val, list):
                    yield from val
                    return
            if any(k in data for k in ("matchId", "match_id", "id")):
                yield data

    @staticmethod
    def _build_match(item):
        try:
            mid = str(item.get("matchId") or item.get("match_id") or item.get("id") or "").strip()
            if not mid:
                return None
            sport_id = int(item.get("sportId") or item.get("sport_id") or 1)
            league = str(item.get("competition") or item.get("league") or item.get("leagueName") or "?")
            home = str(item.get("competitor1Name") or item.get("home") or item.get("team1") or "").strip()
            away = str(item.get("competitor2Name") or item.get("away") or item.get("team2") or "").strip()
            if not home or not away:
                return None
            start_ts = int(item.get("matchStart") or item.get("start_time") or item.get("startTimestamp") or time.time())
            status = str(item.get("status") or "PREMATCH").upper()
            return WinamaxMatch(
                match_id=mid,
                sport_id=sport_id,
                sport_name=SPORT_IDS.get(sport_id, f"Sport#{sport_id}"),
                league=league,
                home=home,
                away=away,
                start_ts=start_ts,
                status=status,
            )
        except Exception:
            return None

    @staticmethod
    def _apply_market(match, data):
        markets = data.get("markets") or data.get("outcomes") or data.get("odds") or data
        if isinstance(markets, dict):
            match.raw_markets.update(markets)
        ml = None
        if isinstance(markets, dict):
            ml = markets.get("moneyline") or markets.get("1x2") or markets.get("win_draw_win")
        if isinstance(ml, list) and len(ml) >= 2:
            try:
                match.odds.home = float(ml[0])
                if len(ml) == 3:
                    match.odds.draw = float(ml[1])
                    match.odds.away = float(ml[2])
                else:
                    match.odds.away = float(ml[1])
            except Exception:
                pass

    @staticmethod
    def _apply_score(match, data):
        if not isinstance(data, dict):
            return
        score = data.get("score") or data.get("scores") or {}
        if isinstance(score, dict):
            try:
                h = score.get("home") or score.get("competitor1")
                a = score.get("away") or score.get("competitor2")
                if h is not None:
                    match.score_home = int(h)
                if a is not None:
                    match.score_away = int(a)
            except Exception:
                pass
        minute = data.get("minute") or data.get("clock")
        if minute is not None:
            try:
                match.minute = int(minute)
            except Exception:
                pass
        new_status = data.get("status") or data.get("matchStatus")
        if new_status:
            match.status = str(new_status).upper()


class WinamaxScraper:
    def __init__(self, headless=True, capture_duration=CAPTURE_DURATION_S, scroll_pause_ms=SCROLL_PAUSE_MS, proxy=None, max_retries=2):
        self.headless = headless
        self.capture_duration = capture_duration
        self.scroll_pause_ms = scroll_pause_ms
        self.proxy = proxy
        self.max_retries = max_retries
        self._raw_messages = []
        self._lock = asyncio.Lock()

    async def fetch_all_matches(self):
        async with self._lock:
            self._raw_messages.clear()
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                return await self._do_capture()
            except Exception as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    wait = 2 ** attempt * 5
                    log.warning(f"Capture echouee (tentative {attempt+1}): {exc}. Retry dans {wait}s")
                    await asyncio.sleep(wait)
        log.error(f"Toutes les tentatives echouees: {last_exc}")
        return []

    async def _do_capture(self):
        browser = None
        async with async_playwright() as pw:
            try:
                launch_kwargs = {
                    "headless": self.headless,
                    "args": ["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--disable-gpu"],
                }
                if self.proxy:
                    launch_kwargs["proxy"] = {"server": self.proxy}
                browser = await pw.chromium.launch(**launch_kwargs)
                ctx = await browser.new_context(
                    user_agent=random.choice(USER_AGENTS),
                    locale="fr-FR",
                    viewport={"width": 1366, "height": 768},
                )
                page = await ctx.new_page()
                page.on("websocket", self._on_websocket)
                await page.goto(WINAMAX_URL, wait_until="domcontentloaded", timeout=30000)
                await self._accept_cookies(page)
                await self._scroll_full_page(page)
                await asyncio.sleep(self.capture_duration)
            finally:
                if browser and browser.is_connected():
                    await browser.close()
        async with self._lock:
            messages_copy = list(self._raw_messages)
        matches = WinamaxParser.extract_matches(messages_copy)
        log.info(f"Captures: {len(matches)} matchs ({len(messages_copy)} trames WS)")
        return matches

    def _on_websocket(self, ws):
        ws.on("framereceived", lambda frame: self._on_frame(frame.get("payload", "")))

    def _on_frame(self, payload):
        if not payload or len(payload) < 6:
            return
        if payload[0] != "4" or "[" not in payload:
            return
        self._raw_messages.append(payload)

    @staticmethod
    async def _accept_cookies(page):
        try:
            for sel in ["button:has-text('Tout accepter')", "button:has-text('Accepter')", "#onetrust-accept-btn-handler"]:
                btn = page.locator(sel)
                if await btn.count() > 0:
                    await btn.first.click(timeout=4000)
                    return
        except Exception:
            pass

    async def _scroll_full_page(self, page):
        last_height = 0
        loops = 0
        while loops < MAX_SCROLL_LOOPS:
            try:
                current_height = await page.evaluate("document.body.scrollHeight")
            except Exception:
                break
            if current_height == last_height:
                break
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(self.scroll_pause_ms / 1000)
            last_height = current_height
            loops += 1


class WinamaxPoller:
    def __init__(self, interval_s=POLL_INTERVAL_S, output_dir=OUTPUT_DIR, scraper_kwargs=None):
        self.interval_s = interval_s
        self.output_dir = Path(output_dir)
        self.scraper_kwargs = scraper_kwargs or {}
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._matches = []
        self._error_count = 0

    @property
    def matches(self):
        return list(self._matches)

    async def run_forever(self):
        log.info(f"Poller demarre (intervalle: {self.interval_s}s)")
        while True:
            try:
                await self._tick()
                self._error_count = 0
            except Exception as exc:
                self._error_count += 1
                log.error(f"Erreur tick #{self._error_count}: {exc}")
            await asyncio.sleep(self.interval_s)

    async def run_once(self):
        await self._tick()
        return self.matches

    async def _tick(self):
        scraper = WinamaxScraper(**self.scraper_kwargs)
        new_matches = await scraper.fetch_all_matches()
        if new_matches:
            self._matches = new_matches
            self._save()
        else:
            log.warning("Capture vide - conservation du cache precedent")

    def _save(self):
        path = self.output_dir / "winamax_matches.json"
        data = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "count": len(self._matches),
            "matches": [m.to_dict() for m in self._matches],
        }
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self.output_dir, prefix=".winamax_tmp_", suffix=".json")
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
            Path(tmp_path).replace(path)
            log.info(f"Sauvegarde: {len(self._matches)} matchs")
        except Exception as exc:
            os.unlink(tmp_path)
            raise exc

    @classmethod
    def load_cached(cls, output_dir=OUTPUT_DIR, max_age_s=CACHE_MAX_AGE_S):
        path = Path(output_dir) / "winamax_matches.json"
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw.get("matches", [])
        except Exception as exc:
            log.error(f"Cache corrompu: {exc}")
            return []


async def _main():
    import argparse
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--headless", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--duration", type=int, default=CAPTURE_DURATION_S)
    parser.add_argument("--interval", type=int, default=POLL_INTERVAL_S)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [Winamax] %(levelname)s %(message)s")
    scraper_kwargs = {"headless": args.headless, "capture_duration": args.duration}
    if args.once:
        scraper = WinamaxScraper(**scraper_kwargs)
        matches = await scraper.fetch_all_matches()
        print(json.dumps([m.to_dict() for m in matches], ensure_ascii=False, indent=2))
        sys.exit(0)
    poller = WinamaxPoller(interval_s=args.interval, scraper_kwargs=scraper_kwargs)
    await poller.run_forever()


if __name__ == "__main__":
    asyncio.run(_main())
