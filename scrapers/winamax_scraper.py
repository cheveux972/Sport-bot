import asyncio
import json
import logging
import os
import time
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

OUTPUT_DIR = Path(**file**).parent.parent / Path(‘data’)
POLL_INTERVAL_S = 60
CACHE_MAX_AGE_S = 300

SPORT_IDS = {
1: ‘Football’,
2: ‘Tennis’,
5: ‘Basketball’,
6: ‘Hockey sur glace’,
13: ‘Rugby’,
23: ‘Volleyball’,
4: ‘Handball’,
}

import random
log = logging.getLogger(‘winamax’)

@dataclass
class Odds:
home: float = None
draw: float = None
away: float = None

```
def is_complete(self):
    return self.home is not None and self.away is not None
```

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

```
@property
def is_live(self):
    return self.status == 'LIVE'

@property
def minutes_until_start(self):
    return max(0.0, (self.start_ts - time.time()) / 60)

def to_dict(self):
    d = asdict(self)
    d['start_iso'] = datetime.fromtimestamp(self.start_ts, tz=timezone.utc).isoformat()
    d['minutes_until_start'] = round(self.minutes_until_start, 1)
    return d
```

class WinamaxPoller:
def **init**(self, interval_s=POLL_INTERVAL_S, output_dir=OUTPUT_DIR, scraper_kwargs=None):
self.interval_s = interval_s
self.output_dir = Path(output_dir)
self.scraper_kwargs = scraper_kwargs or {}
self.output_dir.mkdir(parents=True, exist_ok=True)
self._matches = []

```
@property
def matches(self):
    return list(self._matches)

async def run_forever(self):
    log.info('Poller demarre')
    while True:
        try:
            await self._tick()
        except Exception as exc:
            log.error('Erreur tick: %s', exc)
        await asyncio.sleep(self.interval_s)

async def run_once(self):
    await self._tick()
    return self.matches

async def _tick(self):
    try:
        from playwright.async_api import async_playwright
        import re

        USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36'
        WINAMAX_URL = 'https://www.winamax.fr/paris-sportifs/sports'

        raw_messages = []

        def on_frame(payload):
            if payload and len(payload) > 5 and payload[0] == '4' and '[' in payload:
                raw_messages.append(payload)

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu'],
            )
            ctx = await browser.new_context(
                user_agent=USER_AGENT,
                locale='fr-FR',
            )
            page = await ctx.new_page()

            def on_ws(ws):
                ws.on('framereceived', lambda f: on_frame(f.get('payload', '')))

            page.on('websocket', on_ws)

            try:
                await page.goto(
                    WINAMAX_URL,
                    wait_until='domcontentloaded',
                    timeout=30000,
                )
                for sel in ['button:has-text("Tout accepter")', 'button:has-text("Accepter")']:
                    try:
                        btn = page.locator(sel)
                        if await btn.count() > 0:
                            await btn.first.click(timeout=4000)
                            break
                    except Exception:
                        pass
                await asyncio.sleep(60)
            finally:
                await browser.close()

        matches = []
        seen = set()
        for raw in raw_messages:
            try:
                m = re.match(r'^4\d(\[.+\])$', raw.strip(), re.DOTALL)
                if not m:
                    continue
                payload = json.loads(m.group(1))
                if not isinstance(payload, list) or len(payload) < 2:
                    continue
                data = payload[1]
                items = []
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    for key in ('matches', 'data', 'events', 'items'):
                        if key in data and isinstance(data[key], list):
                            items = data[key]
                            break
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    mid = str(item.get('matchId') or item.get('match_id') or item.get('id') or '')
                    if not mid or mid in seen:
                        continue
                    home = str(item.get('competitor1Name') or item.get('home') or item.get('team1') or '')
                    away = str(item.get('competitor2Name') or item.get('away') or item.get('team2') or '')
                    if not home or not away:
                        continue
                    seen.add(mid)
                    sport_id = int(item.get('sportId') or item.get('sport_id') or 1)
                    matches.append(WinamaxMatch(
                        match_id=mid,
                        sport_id=sport_id,
                        sport_name=SPORT_IDS.get(sport_id, 'Sport'),
                        league=str(item.get('competition') or item.get('league') or '?'),
                        home=home,
                        away=away,
                        start_ts=int(item.get('matchStart') or item.get('startTimestamp') or time.time()),
                        status=str(item.get('status') or 'PREMATCH').upper(),
                    ))
            except Exception:
                continue

        if matches:
            self._matches = matches
            self._save()
            log.info('Captures: %d matchs', len(matches))
        else:
            log.warning('Capture vide')

    except Exception as exc:
        log.error('Erreur capture: %s', exc)

def _save(self):
    path = self.output_dir / 'winamax_matches.json'
    data = {
        'updated_at': datetime.now(timezone.utc).isoformat(),
        'count': len(self._matches),
        'matches': [m.to_dict() for m in self._matches],
    }
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp_fd, tmp_path = tempfile.mkstemp(dir=self.output_dir, prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
            f.write(payload)
        Path(tmp_path).replace(path)
    except Exception as exc:
        os.unlink(tmp_path)
        raise exc

@classmethod
def load_cached(cls, output_dir=OUTPUT_DIR, max_age_s=CACHE_MAX_AGE_S):
    path = Path(output_dir) / 'winamax_matches.json'
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding='utf-8')).get('matches', [])
    except Exception:
        return []
```

class WinamaxScraper:
def **init**(self, **kwargs):
self._poller = WinamaxPoller(scraper_kwargs=kwargs)

```
async def fetch_all_matches(self):
    return await self._poller.run_once()
```
