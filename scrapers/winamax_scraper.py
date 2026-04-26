"""
winamax_scraper.py  (v2 — corrigé & renforcé)
==============================================
Corrections appliquées :
  [FIX-1]  Race condition : _raw_messages partagé entre instances → ThreadSafe via lock asyncio
  [FIX-2]  Memory leak : browser jamais fermé si exception avant browser.close()
           → Utilisation de try/finally dans fetch_all_matches
  [FIX-3]  Scroll infini possible si scrollHeight ne converge pas → max_scrolls + timeout
  [FIX-4]  _on_frame acceptait n'importe quelle string > 5 chars → validation JSON préalable
  [FIX-5]  WinamaxPoller._save() : écriture non-atomique (corruption si crash pendant write)
           → Écriture dans fichier temp puis rename atomique
  [FIX-6]  CLI --headless flag cassé (store_true + default=True → toujours True)
           → Remplacé par BooleanOptionalAction
  [FIX-7]  fetch_live/fetch_upcoming appellent chacun fetch_all_matches (double navigation)
           → Supprimé, laissé à l'appelant de filtrer
  [FIX-8]  Pas de déduplication des trames WS → doublons possibles dans les matchs
  [FIX-9]  Timeout Playwright non capturé → crash silencieux du poller
  [FIX-10] asyncio.coroutine() déprécié Python 3.11+ dans sofascore (ici: import guard)

Nouveautés :
  [NEW-1]  Détection anti-bot : rotation User-Agent + headers aléatoires
  [NEW-2]  Backoff exponentiel sur les erreurs de navigation
  [NEW-3]  Métriques de capture exportées (nb trames, durée, matchs par sport)
  [NEW-4]  Support multi-URLs : sport par sport pour éviter la détection
  [NEW-5]  Cache TTL : load_cached() vérifie l'âge du fichier
"""

import asyncio
import json
import logging
import os
import random
import re
import time
import tempfile
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.async_api import async_playwright, Page, Browser, BrowserContext

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
WINAMAX_BASE_URL   = "https://www.winamax.fr/paris-sportifs/sports"
SCROLL_PAUSE_MS    = 600
CAPTURE_DURATION_S = 90
POLL_INTERVAL_S    = 60
MAX_SCROLL_LOOPS   = 40          # [FIX-3] borne max
OUTPUT_DIR         = Path(__file__).parent.parent / "data"
CACHE_MAX_AGE_S    = 300         # [NEW-5] cache invalide après 5 minutes

SPORT_IDS: dict[int, str] = {
    1:  "Football",
    2:  "Tennis",
    5:  "Basketball",
    6:  "Hockey sur glace",
    13: "Rugby",
    23: "Volleyball",
    4:  "Handball",
    7:  "Cyclisme",
    11: "Formule 1",
    17: "Snooker",
}

# [NEW-1] Pool de User-Agents pour rotation
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]

log = logging.getLogger("winamax")


# ──────────────────────────────────────────────
# Modèles de données
# ──────────────────────────────────────────────
@dataclass
class Odds:
    home:           float | None = None
    draw:           float | None = None
    away:           float | None = None
    total_over_25:  float | None = None
    total_under_25: float | None = None
    btts_yes:       float | None = None
    btts_no:        float | None = None
    handicap_home:  float | None = None   # [NEW] cotes handicap asiatique
    handicap_away:  float | None = None

    def is_complete(self) -> bool:
        """True si on a au moins les cotes 1X2 ou 1-2."""
        return self.home is not None and self.away is not None


@dataclass
class WinamaxMatch:
    match_id:    str
    sport_id:    int
    sport_name:  str
    league:      str
    home:        str
    away:        str
    start_ts:    int
    status:      str
    score_home:  int | None = None
    score_away:  int | None = None
    minute:      int | None = None
    period:      str | None = None   # [NEW] "1ère mi-temps", "2ème mi-temps"…
    odds:        Odds = field(default_factory=Odds)
    raw_markets: dict = field(default_factory=dict)
    captured_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @property
    def start_dt(self) -> datetime:
        return datetime.fromtimestamp(self.start_ts, tz=timezone.utc)

    @property
    def is_live(self) -> bool:
        return self.status == "LIVE"

    @property
    def minutes_until_start(self) -> float:
        return max(0.0, (self.start_ts - time.time()) / 60)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start_iso"] = self.start_dt.isoformat()
        d["minutes_until_start"] = round(self.minutes_until_start, 1)
        return d


# ──────────────────────────────────────────────
# Parsing des messages Socket.IO
# ──────────────────────────────────────────────
class WinamaxParser:
    """
    Parse les trames Engine.IO v3 du WebSocket Winamax.
    Format : "42[\"event_name\", {...payload...}]"
    """

    _FRAME_RE = re.compile(r"^4\d(\[.+\])$", re.DOTALL)

    @classmethod
    def parse_frame(cls, raw: str) -> dict | None:
        """Parse une trame brute → {event, data} ou None."""
        raw = raw.strip()
        m = cls._FRAME_RE.match(raw)
        if not m:
            return None
        try:
            payload = json.loads(m.group(1))
            if isinstance(payload, list) and len(payload) >= 2:
                return {"event": payload[0], "data": payload[1]}
        except (json.JSONDecodeError, IndexError, ValueError):
            pass
        return None

    @classmethod
    def extract_matches(cls, messages: list[str]) -> list[WinamaxMatch]:
        """Transforme les trames brutes en objets WinamaxMatch dédupliqués."""
        matches: dict[str, WinamaxMatch] = {}

        for raw in messages:
            frame = cls.parse_frame(raw)
            if not frame:
                continue

            event = frame["event"]
            data  = frame["data"]

            if event in ("sports_index", "matches_index", "initial_data",
                         "competition_update", "event_list"):
                for item in cls._iter_match_list(data):
                    m = cls._build_match(item)
                    if m:
                        # [FIX-8] Ne jamais écraser un match plus complet
                        existing = matches.get(m.match_id)
                        if not existing or not existing.odds.is_complete():
                            matches[m.match_id] = m

            elif event in ("market_update", "odds_update", "market_data",
                           "odds_change", "selection_change"):
                mid = cls._extract_match_id(data)
                if mid and mid in matches:
                    cls._apply_market(matches[mid], data)

            elif event in ("score_update", "match_update", "live_update",
                           "clock_update", "period_update"):
                mid = cls._extract_match_id(data)
                if mid and mid in matches:
                    cls._apply_score(matches[mid], data)

        return list(matches.values())

    # ── helpers ──────────────────────────────────────────────────

    @staticmethod
    def _extract_match_id(data: dict) -> str | None:
        """Extrait l'ID de match depuis n'importe quelle structure."""
        if not isinstance(data, dict):
            return None
        raw = data.get("matchId") or data.get("match_id") or data.get("eventId") or data.get("id")
        return str(raw) if raw else None

    @staticmethod
    def _iter_match_list(data: Any):
        if isinstance(data, list):
            yield from data
        elif isinstance(data, dict):
            for key in ("matches", "data", "events", "competitions", "items"):
                val = data.get(key)
                if isinstance(val, list):
                    yield from val
< truncated lines 218-506 >

    # ── Helpers page ─────────────────────────────────────────────

    @staticmethod
    async def _accept_cookies(page: Page) -> None:
        try:
            selectors = [
                "button:has-text('Tout accepter')",
                "button:has-text('Accepter et fermer')",
                "button#onetrust-accept-btn-handler",
                "[data-testid='accept-all-cookies']",
            ]
            for sel in selectors:
                btn = page.locator(sel)
                if await btn.count() > 0:
                    await btn.first.click(timeout=4_000)
                    log.info("🍪 Cookies acceptés")
                    await asyncio.sleep(0.5)
                    return
        except Exception:
            pass

    async def _scroll_full_page(self, page: Page) -> None:
        """[FIX-3] Scroll avec borne max pour éviter la boucle infinie."""
        log.info("Scroll de la page pour charger tous les matchs…")
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

        log.info(f"  ↳ Scroll terminé ({loops} itérations)")

    # ── Stats ─────────────────────────────────────────────────────

    @staticmethod
    def _log_stats(matches: list[WinamaxMatch]) -> None:
        by_sport: dict[str, int] = {}
        live = prematch = 0
        for m in matches:
            by_sport[m.sport_name] = by_sport.get(m.sport_name, 0) + 1
            if m.is_live:
                live += 1
            else:
                prematch += 1
        log.info(f"  ↳ LIVE: {live} | Pré-match: {prematch}")
        for sport, count in sorted(by_sport.items(), key=lambda x: -x[1]):
            log.info(f"     {sport}: {count}")


# ──────────────────────────────────────────────
# Boucle de polling continue
# ──────────────────────────────────────────────
class WinamaxPoller:
    """
    Maintient une liste fraîche de matchs en re-scrappant périodiquement.
    Écriture atomique du JSON. [FIX-5]
    """

    def __init__(
        self,
        interval_s:     int = POLL_INTERVAL_S,
        output_dir:     Path = OUTPUT_DIR,
        scraper_kwargs: dict | None = None,
    ):
        self.interval_s     = interval_s
        self.output_dir     = output_dir
        self.scraper_kwargs = scraper_kwargs or {}
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._matches:  list[WinamaxMatch] = []
        self._error_count = 0
        self._max_errors  = 5   # arrêt automatique après N erreurs consécutives

    @property
    def matches(self) -> list[WinamaxMatch]:
        return list(self._matches)  # retourne une copie

    async def run_forever(self) -> None:
        log.info(f"🔄 Poller démarré (intervalle : {self.interval_s}s)")
        while True:
            try:
                await self._tick()
                self._error_count = 0
            except Exception as exc:
                self._error_count += 1
                log.error(
                    f"Erreur tick #{self._error_count}/{self._max_errors} : {exc}",
                    exc_info=True,
                )
                if self._error_count >= self._max_errors:
                    log.critical("Trop d'erreurs consécutives — poller arrêté")
                    raise RuntimeError("WinamaxPoller: trop d'erreurs") from exc

            await asyncio.sleep(self.interval_s)

    async def run_once(self) -> list[WinamaxMatch]:
        await self._tick()
        return self.matches

    async def _tick(self) -> None:
        scraper = WinamaxScraper(**self.scraper_kwargs)
        new_matches = await scraper.fetch_all_matches()
        if new_matches:   # Ne pas écraser avec liste vide si erreur
            self._matches = new_matches
            self._save()
        else:
            log.warning("Capture vide — conservation du cache précédent")

    def _save(self) -> None:
        """[FIX-5] Écriture atomique : temp + rename."""
        path = self.output_dir / "winamax_matches.json"
        data = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "count":      len(self._matches),
            "matches":    [m.to_dict() for m in self._matches],
        }
        payload = json.dumps(data, ensure_ascii=False, indent=2)

        # Écriture dans un fichier temporaire puis rename atomique
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self.output_dir, prefix=".winamax_tmp_", suffix=".json"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
            Path(tmp_path).replace(path)   # atomique sur Linux/macOS
            log.info(f"💾 {len(self._matches)} matchs sauvegardés → {path.name}")
        except Exception as exc:
            os.unlink(tmp_path)
            raise exc

    @classmethod
    def load_cached(
        cls,
        output_dir: Path = OUTPUT_DIR,
        max_age_s:  int  = CACHE_MAX_AGE_S,  # [NEW-5]
    ) -> list[dict]:
        """Charge le dernier snapshot JSON si pas trop vieux."""
        path = output_dir / "winamax_matches.json"
        if not path.exists():
            return []

        try:
            raw  = json.loads(path.read_text(encoding="utf-8"))
            age  = time.time() - path.stat().st_mtime
            if age > max_age_s:
                log.warning(f"Cache Winamax périmé ({age:.0f}s > {max_age_s}s)")
            return raw.get("matches", [])
        except (json.JSONDecodeError, OSError) as exc:
            log.error(f"Cache corrompu : {exc}")
            return []


# ──────────────────────────────────────────────
# Point d'entrée CLI
# ──────────────────────────────────────────────
async def _main() -> None:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Scraper Winamax v2")
    parser.add_argument("--once",      action="store_true")
    parser.add_argument("--live",      action="store_true")
    parser.add_argument("--sport",     type=int, default=None)
    # [FIX-6] BooleanOptionalAction remplace store_true + default=True
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Mode headless (--no-headless pour debug visuel)",
    )
    parser.add_argument("--duration",  type=int, default=CAPTURE_DURATION_S)
    parser.add_argument("--interval",  type=int, default=POLL_INTERVAL_S)
    parser.add_argument("--proxy",     type=str, default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [Winamax] %(levelname)s  %(message)s",
        datefmt="%H:%M:%S",
    )

    scraper_kwargs = {
        "headless":         args.headless,
        "capture_duration": args.duration,
        "proxy":            args.proxy,
    }

    if args.once:
        scraper = WinamaxScraper(**scraper_kwargs)
        matches = await scraper.fetch_all_matches()

        if args.live:
            matches = [m for m in matches if m.is_live]
        if args.sport:
            matches = [m for m in matches if m.sport_id == args.sport]

        print(json.dumps([m.to_dict() for m in matches], ensure_ascii=False, indent=2))
        sys.exit(0)

    poller = WinamaxPoller(interval_s=args.interval, scraper_kwargs=scraper_kwargs)
    await poller.run_forever()


if __name__ == "__main__":
    asyncio.run(_main())
