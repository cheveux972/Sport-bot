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

OUTPUT_DIR = Path(__file__).parent.parent / Path(os.getenv(
    str(bytes([100,97,116,97]).decode()),
    str(bytes([100,97,116,97]).decode())
))
POLL_INTERVAL_S = 60
CACHE_MAX_AGE_S = 300

SPORT_IDS = {
    1: bytes([70,111,111,116,98,97,108,108]).decode(),
    2: bytes([84,101,110,110,105,115]).decode(),
    5: bytes([66,97,115,107,101,116,98,97,108,108]).decode(),
    6: bytes([72,111,99,107,101,121]).decode(),
    13: bytes([82,117,103,98,121]).decode(),
    23: bytes([86,111,108,108,101,121,98,97,108,108]).decode(),
    4: bytes([72,97,110,100,98,97,108,108]).decode(),
}

UA = bytes([77,111,122,105,108,108,97,47,53,46,48,32,40,87,105,110,100,111,119,115,32,78,84,32,49,48,46,48,59,32,87,105,110,54,52,59,32,120,54,52,41,32,65,112,112,108,101,87,101,98,75,105,116,47,53,51,55,46,51,54,32,67,104,114,111,109,101,47,49,50,52,46,48,46,48,46,48,32,83,97,102,97,114,105,47,53,51,55,46,51,54]).decode()

API1 = bytes([104,116,116,112,115,58,47,47,119,119,119,46,119,105,110,97,109,97,120,46,102,114,47,97,112,112,115,112,111,114,116,115,47,100,97,116,97,47,49,46,48,47,109,97,116,99,104,101,115,47,108,105,118,101]).decode()
API2 = bytes([104,116,116,112,115,58,47,47,119,119,119,46,119,105,110,97,109,97,120,46,102,114,47,97,112,112,115,112,111,114,116,115,47,100,97,116,97,47,49,46,48,47,109,97,116,99,104,101,115,47,112,114,101,109,97,116,99,104]).decode()
API3 = bytes([104,116,116,112,115,58,47,47,119,119,119,46,119,105,110,97,109,97,120,46,102,114,47,97,112,112,115,112,111,114,116,115,47,100,97,116,97,47,49,46,48,47,115,112,111,114,116,115]).decode()

log = logging.getLogger(bytes([119,105,110,97,109,97,120]).decode())


@dataclass
class Odds:
    home: float = None
    draw: float = None
    away: float = None

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
    def is_live(self):
        return self.status == bytes([76,73,86,69]).decode()

    @property
    def minutes_until_start(self):
        return max(0.0, (self.start_ts - time.time()) / 60)

    def to_dict(self):
        d = asdict(self)
        d[bytes([115,116,97,114,116,95,105,115,111]).decode()] = datetime.fromtimestamp(self.start_ts, tz=timezone.utc).isoformat()
        d[bytes([109,105,110,117,116,101,115,95,117,110,116,105,108,95,115,116,97,114,116]).decode()] = round(self.minutes_until_start, 1)
        return d


class WinamaxPoller:
    def __init__(self, interval_s=POLL_INTERVAL_S, output_dir=OUTPUT_DIR, scraper_kwargs=None):
        self.interval_s = interval_s
        self.output_dir = Path(output_dir)
        self.scraper_kwargs = scraper_kwargs or {}
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._matches = []

    @property
    def matches(self):
        return list(self._matches)

    async def run_forever(self):
        log.info(bytes([80,111,108,108,101,114,32,100,101,109,97,114,114,101]).decode())
        while True:
            try:
                await self._tick()
            except Exception as exc:
                log.error(bytes([69,114,114,101,117,114,32,116,105,99,107,58,32,37,115]).decode(), exc)
            await asyncio.sleep(self.interval_s)

    async def run_once(self):
        await self._tick()
        return self.matches

    async def _tick(self):
        try:
            headers = {
                bytes([85,115,101,114,45,65,103,101,110,116]).decode(): UA,
                bytes([65,99,99,101,112,116]).decode(): bytes([97,112,112,108,105,99,97,116,105,111,110,47,106,115,111,110]).decode(),
            }

            matches = []
            seen = set()

            connector = aiohttp.TCPConnector(ssl=False)
            async with aiohttp.ClientSession(connector=connector) as session:
                for url in [API1, API2, API3]:
                    try:
                        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as r:
                            if r.status == 200:
                                try:
                                    data = await r.json(content_type=None)
                                except Exception:
                                    text = await r.text()
                                    try:
                                        data = json.loads(text)
                                    except Exception:
                                        continue

                                items = []
                                if isinstance(data, list):
                                    items = data
                                elif isinstance(data, dict):
                                    for key in [bytes([109,97,116,99,104,101,115]).decode(), bytes([101,118,101,110,116,115]).decode(), bytes([100,97,116,97]).decode()]:
                                        if key in data and isinstance(data[key], list):
                                            items = data[key]
                                            break

                                for item in items:
                                    if not isinstance(item, dict):
                                        continue
                                    mid = str(
                                        item.get(bytes([109,97,116,99,104,73,100]).decode()) or
                                        item.get(bytes([109,97,116,99,104,95,105,100]).decode()) or
                                        item.get(bytes([105,100]).decode()) or bytes([]).decode()
                                    )
                                    if not mid or mid in seen:
                                        continue
                                    home = str(
                                        item.get(bytes([99,111,109,112,101,116,105,116,111,114,49,78,97,109,101]).decode()) or
                                        item.get(bytes([104,111,109,101]).decode()) or
                                        item.get(bytes([116,101,97,109,49]).decode()) or bytes([]).decode()
                                    ).strip()
                                    away = str(
                                        item.get(bytes([99,111,109,112,101,116,105,116,111,114,50,78,97,109,101]).decode()) or
                                        item.get(bytes([97,119,97,121]).decode()) or
                                        item.get(bytes([116,101,97,109,50]).decode()) or bytes([]).decode()
                                    ).strip()
                                    if not home or not away:
                                        continue
                                    seen.add(mid)
                                    sport_id = int(item.get(bytes([115,112,111,114,116,73,100]).decode()) or item.get(bytes([115,112,111,114,116,95,105,100]).decode()) or 1)
                                    status = str(item.get(bytes([115,116,97,116,117,115]).decode()) or bytes([80,82,69,77,65,84,67,72]).decode()).upper()
                                    m = WinamaxMatch(
                                        match_id=mid,
                                        sport_id=sport_id,
                                        sport_name=SPORT_IDS.get(sport_id, bytes([83,112,111,114,116]).decode()),
                                        league=str(item.get(bytes([99,111,109,112,101,116,105,116,105,111,110]).decode()) or item.get(bytes([108,101,97,103,117,101]).decode()) or bytes([63]).decode()),
                                        home=home,
                                        away=away,
                                        start_ts=int(item.get(bytes([109,97,116,99,104,83,116,97,114,116]).decode()) or item.get(bytes([115,116,97,114,116,84,105,109,101,115,116,97,109,112]).decode()) or time.time()),
                                        status=status,
                                    )
                                    matches.append(m)

                                if matches:
                                    log.info(bytes([65,80,73,32,87,105,110,97,109,97,120,58,32,37,100,32,109,97,116,99,104,115]).decode(), len(matches))
                                    break
                    except Exception as exc:
                        log.debug(bytes([69,114,114,101,117,114,32,37,115,58,32,37,115]).decode(), url, exc)
                        continue

            if matches:
                self._matches = matches
                self._save()
            else:
                log.warning(bytes([65,80,73,32,87,105,110,97,109,97,120,58,32,48,32,109,97,116,99,104,115]).decode())

        except Exception as exc:
            log.error(bytes([69,114,114,101,117,114,32,99,97,112,116,117,114,101,58,32,37,115]).decode(), exc)

    def _save(self):
        fname = bytes([119,105,110,97,109,97,120,95,109,97,116,99,104,101,115,46,106,115,111,110]).decode()
        path = self.output_dir / fname
        data = {
            bytes([117,112,100,97,116,101,100,95,97,116]).decode(): datetime.now(timezone.utc).isoformat(),
            bytes([99,111,117,110,116]).decode(): len(self._matches),
            bytes([109,97,116,99,104,101,115]).decode(): [m.to_dict() for m in self._matches],
        }
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        prefix = bytes([46,116,109,112,95]).decode()
        suffix = bytes([46,106,115,111,110]).decode()
        tmp_fd, tmp_path = tempfile.mkstemp(dir=self.output_dir, prefix=prefix, suffix=suffix)
        try:
            with os.fdopen(tmp_fd, bytes([119]).decode(), encoding=bytes([117,116,102,45,56]).decode()) as f:
                f.write(payload)
            Path(tmp_path).replace(path)
        except Exception as exc:
            os.unlink(tmp_path)
            raise exc

    @classmethod
    def load_cached(cls, output_dir=OUTPUT_DIR, max_age_s=CACHE_MAX_AGE_S):
        fname = bytes([119,105,110,97,109,97,120,95,109,97,116,99,104,101,115,46,106,115,111,110]).decode()
        path = Path(output_dir) / fname
        if not path.exists():
            return []
        try:
            enc = bytes([117,116,102,45,56]).decode()
            key = bytes([109,97,116,99,104,101,115]).decode()
            return json.loads(path.read_text(encoding=enc)).get(key, [])
        except Exception:
            return []


class WinamaxScraper:
    def __init__(self, **kwargs):
        self._poller = WinamaxPoller(scraper_kwargs=kwargs)

    async def fetch_all_matches(self):
        return await self._poller.run_once()
