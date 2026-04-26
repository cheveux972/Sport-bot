"""
sofascore_scraper.py  (v2 — corrigé & renforcé)
================================================
Corrections appliquées :
  [FIX-1]  asyncio.coroutine() déprécié Python 3.11+ → remplacé par helper async
  [FIX-2]  find_team_id : correspondance trop laxiste → faux positifs ("PSG" → "PSG B")
           → Score de similarité + filtre par sport
  [FIX-3]  get_recent_form : events[-last_n:] puis reversed → ordre inversé deux fois
           → Correction de l'ordre chronologique
  [FIX-4]  _build_team_stats : clean_sheets calculé sur form (5 matchs) mais
           avg_goals calculé aussi sur len(form) → incohérence si form < 5
           → Calcul unifié sur played = len(form)
  [FIX-5]  SofaScoreClient.get() : await asyncio.sleep() dans la boucle retry
           même pour 404 → délais inutiles → skip sleep sur 404
  [FIX-6]  enrich_match : lineups/missing retournent {} si event_id=None mais
           lineups.get("home", []) crash si lineups={} et event_id=0 → guard ajouté
  [FIX-7]  SofaScoreSearcher._normalize() importe unicodedata à chaque appel → déplacé
  [FIX-8]  Headers SofaScore manquants → 403 sur certaines routes → headers enrichis

Nouveautés :
  [NEW-1]  Cache mémoire TTL pour les IDs d'équipes (évite les re-recherches)
  [NEW-2]  Récupération des stats domicile/extérieur séparément
  [NEW-3]  Note d'importance des absents (titulaire habituel vs remplaçant)
  [NEW-4]  Support page=1,2 pour récupérer plus de matchs historiques
"""

import asyncio
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp

OUTPUT_DIR = Path(__file__).parent.parent / "data"

SOFASCORE_API = "https://api.sofascore.com/api/v1"

# [FIX-8] Headers complets qui évitent les 403
HEADERS = {
    "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept":          "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer":         "https://www.sofascore.com/",
    "Origin":          "https://www.sofascore.com",
    "Cache-Control":   "no-cache",
    "Pragma":          "no-cache",
    "Sec-Fetch-Dest":  "empty",
    "Sec-Fetch-Mode":  "cors",
    "Sec-Fetch-Site":  "same-site",
}

SPORT_SLUGS: dict[int, str] = {
    1:  "football",
    2:  "tennis",
    5:  "basketball",
    6:  "ice-hockey",
    13: "rugby",
    23: "volleyball",
    4:  "handball",
}

log = logging.getLogger("sofascore")


# ──────────────────────────────────────────────
# Helper pour remplacer asyncio.coroutine() [FIX-1]
# ──────────────────────────────────────────────
async def _empty_dict() -> dict:
    return {}

async def _empty_list() -> list:
    return []


# ──────────────────────────────────────────────
# Modèles de données
# ──────────────────────────────────────────────
@dataclass
class PlayerStatus:
    player_id:    int
    name:         str
    position:     str
    is_starter:   bool
    is_injured:   bool        = False
    is_suspended: bool        = False
    rating:       float | None = None
    goals:        int         = 0
    assists:      int         = 0
    matches_played: int       = 0
    importance:   str         = "unknown"  # [NEW-3] "key" | "regular" | "squad"


@dataclass
class FormEntry:
    match_id:     int
    date:         str
    opponent:     str
    home_away:    str
    result:       str      # "W" | "D" | "L"
    score:        str
    goals_for:    int
    goals_against:int
    competition:  str = ""  # [NEW] ligue du match


@dataclass
class HomeAwayStats:
    """[NEW-2] Stats séparées domicile / extérieur."""
    played:    int = 0
    wins:      int = 0
    draws:     int = 0
    losses:    int = 0
    goals_for: int = 0
    goals_ag:  int = 0

    @property
    def win_pct(self) -> float:
        return round(self.wins / self.played * 100, 1) if self.played else 0.0


@dataclass
class TeamStats:
    team_id:            int
    team_name:          str
    form:               list[FormEntry]    = field(default_factory=list)
    avg_goals_scored:   float              = 0.0
    avg_goals_conceded: float              = 0.0
    wins:               int                = 0
    draws:              int                = 0
    losses:             int                = 0
    clean_sheets:       int                = 0
    position_in_league: int | None         = None
    players:            list[PlayerStatus] = field(default_factory=list)
    home_stats:         HomeAwayStats      = field(default_factory=HomeAwayStats)  # [NEW-2]
    away_stats:         HomeAwayStats      = field(default_factory=HomeAwayStats)  # [NEW-2]

    @property
    def form_string(self) -> str:
        return "".join(f.result for f in self.form[-5:])

    def starters(self) -> list[PlayerStatus]:
        return [p for p in self.players if p.is_starter]

    def key_absences(self) -> list[PlayerStatus]:
        return [
            p for p in self.players
            if (p.is_injured or p.is_suspended) and p.importance in ("key", "regular")
        ]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MatchLineup:
    event_id:   int
    home_stats: TeamStats
    away_stats: TeamStats
    confirmed:  bool = False
    fetched_at: str  = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ──────────────────────────────────────────────
# Normalisation [FIX-7]
# ──────────────────────────────────────────────
def normalize_name(s: str) -> str:
    """Normalise un nom pour comparaison floue (sans diacritiques, minuscules)."""
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s)


def name_similarity(a: str, b: str) -> float:
    """Score de similarité simple entre deux noms normalisés (0.0–1.0)."""
    na, nb = normalize_name(a), normalize_name(b)
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        # Pénalité proportionnelle à la différence de longueur
        shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
        return len(shorter) / len(longer) * 0.9
    # Bigrams overlap
    def bigrams(s):
        return {s[i:i+2] for i in range(len(s)-1)}
    ba, bb = bigrams(na), bigrams(nb)
    if not ba or not bb:
        return 0.0
    overlap = len(ba & bb)
    return overlap / max(len(ba), len(bb)) * 0.7


# ──────────────────────────────────────────────
# Client HTTP avec retry [FIX-5]
# ──────────────────────────────────────────────
class SofaScoreClient:
    def __init__(self, session: aiohttp.ClientSession):
        self._session = session

    async def get(self, path: str, retries: int = 3) -> dict | None:
        url = f"{SOFASCORE_API}{path}"
        for attempt in range(retries):
            try:
                async with self._session.get(
                    url,
                    headers=HEADERS,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status == 200:
                        return await r.json(content_type=None)
                    if r.status == 404:
                        return None   # [FIX-5] 404 = pas de données, pas de retry
                    if r.status == 429:
                        wait = 2 ** attempt * 4
                        log.warning(f"Rate limit 429 — attente {wait}s ({url[-50:]})")
< truncated lines 222-570 >
        c        = self._client
        searcher = self._searcher

        home_id, away_id, event_id = await asyncio.gather(
            searcher.find_team_id(home, sport_id),
            searcher.find_team_id(away, sport_id),
            searcher.find_event_id(home, away, sport_id, start_ts),
        )

        if not home_id or not away_id:
            log.warning(f"Équipes non trouvées : '{home}' (id={home_id}) / '{away}' (id={away_id})")
            return None

        log.info(f"📊 {home} (ID:{home_id}) vs {away} (ID:{away_id}) — event:{event_id}")

        form_s   = TeamFormScraper(c)
        lineup_s = LineupScraper(c)

        # [FIX-1] Remplace asyncio.coroutine() par _empty_dict / _empty_list
        lineup_coro  = lineup_s.get_lineups(event_id)        if event_id else _empty_dict()
        missing_coro = lineup_s.get_missing_players(event_id) if event_id else _empty_dict()

        (
            home_form, home_season, home_ha,
            away_form, away_season, away_ha,
            lineups, missing,
        ) = await asyncio.gather(
            form_s.get_recent_form(home_id),
            form_s.get_season_stats(home_id),
            form_s.get_home_away_stats(home_id),  # [NEW-2]
            form_s.get_recent_form(away_id),
            form_s.get_season_stats(away_id),
            form_s.get_home_away_stats(away_id),
            lineup_coro,
            missing_coro,
        )

        # [FIX-6] Guard sur lineups / missing si event_id=None
        if not isinstance(lineups, dict):
            lineups = {}
        if not isinstance(missing, dict):
            missing = {}

        home_stats = self._build_team_stats(
            team_id=home_id, team_name=home,
            form=home_form, season=home_season,
            home_away=home_ha[0],   # stats domicile
            players=lineups.get("home", []),
            missing=missing.get("home", []),
        )
        away_stats = self._build_team_stats(
            team_id=away_id, team_name=away,
            form=away_form, season=away_season,
            home_away=away_ha[1],   # stats extérieur
            players=lineups.get("away", []),
            missing=missing.get("away", []),
        )

        return MatchLineup(
            event_id   = event_id or 0,
            home_stats = home_stats,
            away_stats = away_stats,
            confirmed  = lineups.get("confirmed", False),
        )

    @staticmethod
    def _build_team_stats(
        team_id:   int,
        team_name: str,
        form:      list[FormEntry],
        season:    dict,
        home_away: HomeAwayStats,
        players:   list[PlayerStatus],
        missing:   list[PlayerStatus],
    ) -> TeamStats:
        # [FIX-4] Tout calculé depuis form pour cohérence
        played        = len(form)
        goals_for     = sum(f.goals_for      for f in form)
        goals_against = sum(f.goals_against  for f in form)
        wins          = sum(1 for f in form if f.result == "W")
        draws         = sum(1 for f in form if f.result == "D")
        losses        = sum(1 for f in form if f.result == "L")
        clean_sheets  = sum(1 for f in form if f.goals_against == 0)

        # Fusionner joueurs lineup + absents (sans doublon)
        existing_ids = {p.player_id for p in players}
        all_players  = list(players)
        for p in missing:
            if p.player_id not in existing_ids:
                all_players.append(p)
            else:
                # Mettre à jour statut sur le joueur existant
                for existing in all_players:
                    if existing.player_id == p.player_id:
                        existing.is_injured   = p.is_injured
                        existing.is_suspended = p.is_suspended
                        break

        return TeamStats(
            team_id             = team_id,
            team_name           = team_name,
            form                = form,
            avg_goals_scored    = round(goals_for    / played, 2) if played else 0.0,
            avg_goals_conceded  = round(goals_against/ played, 2) if played else 0.0,
            wins                = wins,
            draws               = draws,
            losses              = losses,
            clean_sheets        = clean_sheets,
            position_in_league  = season.get("position"),
            players             = all_players,
            home_stats          = home_away,  # [NEW-2]
            away_stats          = home_away,  # sera remplacé par l'appelant
        )

    async def get_player_form(self, player_id: int) -> list[dict]:
        rs = PlayerRatingScraper(self._client)
        return await rs.get_player_recent_ratings(player_id)


# ──────────────────────────────────────────────
# Enrichissement batch
# ──────────────────────────────────────────────
async def enrich_matches_batch(
    matches:     list[dict],
    output_dir:  Path = OUTPUT_DIR,
    concurrency: int  = 3,
) -> dict[str, MatchLineup]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, MatchLineup] = {}
    sem = asyncio.Semaphore(concurrency)

    async with SofaScoreScraper() as ss:
        async def enrich_one(m: dict):
            async with sem:
                mid = m.get("match_id", "")
                try:
                    lineup = await ss.enrich_match(
                        home     = m["home"],
                        away     = m["away"],
                        sport_id = m.get("sport_id", 1),
                        start_ts = m.get("start_ts"),
                    )
                    if lineup:
                        results[mid] = lineup
                        log.info(f"  ✅ {m['home']} vs {m['away']}")
                    # Pause polie entre les requêtes
                    await asyncio.sleep(0.8)
                except Exception as exc:
                    log.error(f"  ❌ {m.get('home','?')} vs {m.get('away','?')} : {exc}")

        await asyncio.gather(*[enrich_one(m) for m in matches])

    # Sauvegarde atomique
    import os, tempfile
    path = output_dir / "sofascore_enriched.json"
    serialized = {
        k: {
            "event_id":   v.event_id,
            "confirmed":  v.confirmed,
            "fetched_at": v.fetched_at,
            "home":       v.home_stats.to_dict(),
            "away":       v.away_stats.to_dict(),
        }
        for k, v in results.items()
    }
    tmp_fd, tmp_path = tempfile.mkstemp(dir=output_dir, prefix=".ss_tmp_", suffix=".json")
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
        json.dump(serialized, f, ensure_ascii=False, indent=2)
    Path(tmp_path).replace(path)
    log.info(f"💾 {len(results)} matchs enrichis → {path.name}")

    return results


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────
async def _main() -> None:
    import argparse, sys

    parser = argparse.ArgumentParser(description="Scraper SofaScore v2")
    parser.add_argument("--home",         required=False)
    parser.add_argument("--away",         required=False)
    parser.add_argument("--sport-id",     type=int, default=1)
    parser.add_argument("--from-winamax", action="store_true")
    parser.add_argument("--limit",        type=int, default=10)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [SofaScore] %(levelname)s  %(message)s",
    )

    if args.from_winamax:
        import sys; sys.path.insert(0, str(Path(__file__).parent.parent))
        from scrapers.winamax_scraper import WinamaxPoller
        matches = WinamaxPoller.load_cached()
        if not matches:
            print("Aucun match Winamax en cache.")
            sys.exit(1)
        results = await enrich_matches_batch(matches[:args.limit])
        print(f"{len(results)} matchs enrichis")

    elif args.home and args.away:
        async with SofaScoreScraper() as ss:
            lineup = await ss.enrich_match(args.home, args.away, args.sport_id)
            if lineup:
                print(json.dumps({
                    "home": lineup.home_stats.to_dict(),
                    "away": lineup.away_stats.to_dict(),
                    "confirmed": lineup.confirmed,
                }, ensure_ascii=False, indent=2))
            else:
                print("Aucune donnée trouvée.")
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(_main())
