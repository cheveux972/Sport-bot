import asyncio
import json
import logging
import os
import time
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

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

WINAMAX_API_URLS = [
‘https://www.winamax.fr/paris-sportifs/sports’,
‘https://www.winamax.fr/appsports/data/1.0/sports/1/competitions’,
‘https://www.winamax.fr/appsports/data/1.0/matches/live’,
‘https://www.winamax.fr/appsports/data/1.0/matches/prematch’,
]

HEADERS = {
‘User-Agent’: ‘Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36’,
‘Accept’: ‘application/json, text/plain, */*’,
‘Accept-Language’: ‘fr-FR,fr;q=0.9’,
‘Referer’: ‘https://www.winamax.fr/paris-sportifs/sports’,
‘Origin’: ‘https://www.winamax.fr’,
}

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

class WinamaxAPIClient:
def **init**(self, session: aiohttp.ClientSession):
self._session = session

```
async def fetch(self, url: str) -> dict | None:
    try:
        async with self._session.get(
            url,
            headers=HEADERS,
            timeout=aiohttp.ClientTimeout(total=15),
            ssl=False,
        ) as r:
            if r.status == 200:
                ct = r.headers.get('Content-Type', '')
                if 'json' in ct:
                    return await r.json(content_type=None)
                text = await r.text()
                try:
                    return json.loads(text)
                except Exception:
                    return None
            log.debug('HTTP %d pour %s', r.status, url)
    except Exception as exc:
        log.debug('Erreur fetch %s: %s', url, exc)
    return None

async def get_matches(self) -> list[dict]:
    matches = []

    live_urls = [
        'https://www.winamax.fr/appsports/data/1.0/matches/live',
        'https://sports-eu-west-3.winamax.fr/sports/matches/live',
    ]
    prematch_urls = [
        'https://www.winamax.fr/appsports/data/1.0/matches/prematch',
        'https://sports-eu-west-3.winamax.fr/sports/matches/prematch',
    ]

    for url in live_urls + prematch_urls:
        data = await self.fetch(url)
        if data:
            extracted = self._extract_matches(data, url)
            matches.extend(extracted)
            if extracted:
                log.info('API %s: %d matchs', url.split('/')[-1], len(extracted))
                break

    if not matches:
        sports_url = 'https://www.winamax.fr/appsports/data/1.0/sports'
        data = await self.fetch(sports_url)
        if data:
            matches.extend(self._extract_matches(data, sports_url))

    return matches

def _extract_matches(self, data: any, source: str) -> list[dict]:
    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ('matches', 'events', 'data', 'competitions', 'sports'):
            val = data.get(key)
            if isinstance(val, list):
                items = val
                break
        if not items and ('matchId' in data or 'match_id' in data):
            items = [data]

    result = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get('matchId') or item.get('match_id') or item.get('id') or '')
        home = str(item.get('competitor1Name') or item.get('home') or item.get('team1') or '')
        away = str(item.get('competitor2Name') or item.get('away') or item.get('team2') or '')
        if mid and home and away:
            result.append(item)
        elif isinstance(item, dict):
            for subkey in ('matches', 'events', 'data'):
                sub = item.get(subkey)
                if isinstance(sub, list):
                    result.extend(self._extract_matches(sub, source))
    return result
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
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            client = WinamaxAPIClient(session)
            raw_items = await client.get_matches()

        matches = []
        seen = set()
        for item in raw_items:
            mid = str(item.get('matchId') or item.get('match_id') or item.get('id') or '')
            if not mid or mid in seen:
                continue
            home = str(item.get('competitor1Name') or item.get('home') or item.get('team1') or '').strip()
            away = str(item.get('competitor2Name') or item.get('away') or item.get('team2') or '').strip()
            if not home or not away:
                continue
            seen.add(mid)
            sport_id = int(item.get('sportId') or item.get('sport_id') or 1)
            status = str(item.get('status') or 'PREMATCH').upper()

            m = WinamaxMatch(
                match_id=mid,
                sport_id=sport_id,
                sport_name=SPORT_IDS.get(sport_id, 'Sport'),
                league=str(item.get('competition') or item.get('league') or '?'),
                home=home,
                away=away,
                start_ts=int(item.get('matchStart') or item.get('startTimestamp') or time.time()),
                status=status,
            )

            odds_data = item.get('odds') or item.get('mainOdds') or {}
            if isinstance(odds_data, dict):
                m.odds.home = odds_data.get('1') or odds_data.get('home')
                m.odds.draw = odds_data.get('X') or odds_data.get('draw')
                m.odds.away = odds_data.get('2') or odds_data.get('away')

            matches.append(m)

        if matches:
            self._matches = matches
            self._save()
            log.info('Captures: %d matchs via API', len(matches))
        else:
            log.warning('API Winamax: 0 matchs — possible blocage IP')

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
