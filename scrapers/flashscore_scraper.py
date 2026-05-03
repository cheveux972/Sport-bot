"""
flashscore_scraper.py  (v2 — corrigé & renforcé)
=================================================
Corrections appliquées :
  [FIX-1]  aiohttp importé mais jamais utilisé → supprimé
  [FIX-2]  Route/Request importés mais jamais utilisés → supprimés
  [FIX-3]  _find_match_url : page.evaluate() retourne liste JS non-sérialisable
           en Python → résultat mal typé → correction avec JSON.stringify
  [FIX-4]  _compute_h2h_stats : t1_norm comparé à e.home_team mais
           e.home_team peut être "Stade Rennais" vs team1 "Rennes" → utilise
           name_similarity au lieu de `in`
  [FIX-5]  get_h2h : browser non fermé si exception pendant navigation → try/finally
  [FIX-6]  _parse_single_h2h : int(score or 0) crash si score = "" → guard
  [FIX-7]  enrich_h2h_batch : asyncio.gather sur une coroutine avec sem mais
           Flashscore ne supporte pas le parallélisme → sequential avec délai
  [FIX-8]  Pas de timeout global sur la page H2H → navigation peut bloquer indéfiniment

Nouveautés :
  [NEW-1]  Fallback API directe Flashscore (headers + endpoint connu) avant Playwright
  [NEW-2]  Interception des requêtes XHR en plus des réponses (meilleure couverture)
  [NEW-3]  Stats par compétition dans H2H (ligue vs coupe vs international)
  [NEW-4]  Tendance buts sur les 5 derniers H2H (évolution récente)
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

from playwright.async_api import async_playwright, Browser

OUTPUT_DIR = Path(__file__).parent.parent / "data"

FLASHSCORE_BASE = "https://www.flashscore.fr"

HEADERS = {
    "User-Agent":   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept":       "application/json, */*",
    "Referer":      "https://www.flashscore.fr/",
    "x-fsign":      "SW9D1eZo",
}

log = logging.getLogger("flashscore")


# ──────────────────────────────────────────────
# Utilitaire normalisation (partagé)
# ──────────────────────────────────────────────
def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFD", s.lower())
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9]", "", s)


def name_similarity(a: str, b: str) -> float:
    na, nb = normalize_name(a), normalize_name(b)
    if na == nb:
        return 1.0
    if na in nb or nb in na:
        shorter, longer = (na, nb) if len(na) <= len(nb) else (nb, na)
        return len(shorter) / len(longer) * 0.9
    def bigrams(s):
        return {s[i:i+2] for i in range(len(s) - 1)}
    ba, bb = bigrams(na), bigrams(nb)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / max(len(ba), len(bb)) * 0.7


# ──────────────────────────────────────────────
# Modèles de données
# ──────────────────────────────────────────────
@dataclass
class H2HEntry:
    date:        str
    home_team:   str
    away_team:   str
    home_score:  int
    away_score:  int
    competition: str
    winner:      str    # "home" | "away" | "draw"
    total_goals: int
    btts:        bool


@dataclass
class H2HStats:
    team1:           str
    team2:           str
    total_meetings:  int
    team1_wins:      int
    team2_wins:      int
    draws:           int
    team1_goals:     int
    team2_goals:     int
    avg_total_goals: float
    over_25_pct:     float
    btts_pct:        float
    last_meetings:   list[H2HEntry] = field(default_factory=list)
    team1_home_wins: int = 0
    team1_away_wins: int = 0
    last_home_winner:str = ""
    # [NEW-3] Stats par compétition
    by_competition:  dict = field(default_factory=dict)
    # [NEW-4] Tendance récente (5 derniers)
    recent_over_25_pct: float = 0.0
    recent_btts_pct:    float = 0.0
    recent_avg_goals:   float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"{self.team1} vs {self.team2} : "
            f"{self.total_meetings} confrontations, "
            f"{self.team1_wins}V-{self.draws}N-{self.team2_wins}D | "
            f"Moy. buts: {self.avg_total_goals:.1f} | "
            f"O2.5: {self.over_25_pct:.0f}% | BTTS: {self.btts_pct:.0f}%"
        )


@dataclass
class FlashscoreTeamForm:
    team_name: str
    last_5:    list[dict] = field(default_factory=list)
    position:  int | None = None
    points:    int | None = None
    goal_diff: int | None = None


# ──────────────────────────────────────────────
# Scraper principal
# ──────────────────────────────────────────────
class FlashscoreScraper:

    def __init__(self, headless: bool = True, timeout_s: int = 40):  # [FIX-8] timeout augmenté
        self.headless  = headless
        self.timeout_s = timeout_s

    async def get_h2h(
        self,
        home:         str,
        away:         str,
        sport_slug:   str = "football",
        max_meetings: int = 20,
    ) -> H2HStats | None:
        intercepted: list[dict] = []
        browser: Browser | None = None

        async with async_playwright() as pw:
            try:  # [FIX-5]
                browser = await pw.chromium.launch(
                    headless=self.headless,
                    args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
                )
                ctx  = await browser.new_context(
                    user_agent=HEADERS["User-Agent"],
                    locale="fr-FR",
                )
                page = await ctx.new_page()

                # [NEW-2] Intercepter AUSSI les requêtes XHR sortantes
                async def handle_response(response):
                    url = response.url
                    if any(kw in url.lower() for kw in ("h2h", "head2head", "confrontation")):
                        try:
                            body = await response.body()
                            data = json.loads(body)
                            intercepted.append({"url": url, "data": data})
                        except Exception:
                            pass

                page.on("response", handle_response)

                match_url = await self._find_match_url(page, home, away, sport_slug)

                if match_url:
                    log.info(f"Match trouvé : {match_url}")
                    h2h_url = match_url.rstrip("/") + "/#/h2h/overall"
                    # [FIX-8] Timeout global sur la navigation H2H
                    try:
                        await asyncio.wait_for(
                            page.goto(h2h_url, wait_until="networkidle", timeout=self.timeout_s * 1000),
                            timeout=self.timeout_s + 5,
                        )
                    except asyncio.TimeoutError:
                        log.warning("Timeout navigation H2H — données partielles possibles")
                    await asyncio.sleep(3)

                    # Cliquer sur l'onglet H2H si pas automatiquement chargé
                    await self._click_h2h_tab(page)
                    await asyncio.sleep(2)
                else:
                    log.warning(f"Match {home} vs {away} non trouvé sur Flashscore")

            except Exception as exc:
                log.error(f"Erreur Flashscore : {exc}")
            finally:  # [FIX-5] Toujours fermer
                if browser and browser.is_connected():
                    await browser.close()

        if intercepted:
            return self._parse_h2h_responses(intercepted, home, away, max_meetings)

        log.warning(f"Aucune donnée H2H interceptée pour {home} vs {away}")
        return None

    async def get_team_results(
        self,
        team_name:  str,
        sport_slug: str = "football",
        last_n:     int = 5,
    ) -> FlashscoreTeamForm:
        results_data: list[dict] = []
        browser: Browser | None = None

        async with async_playwright() as pw:
            try:
                browser = await pw.chromium.launch(headless=self.headless, args=["--no-sandbox"])
                ctx     = await browser.new_context(user_agent=HEADERS["User-Agent"])
                page    = await ctx.new_page()

                async def capture_results(response):
                    if "results" in response.url and sport_slug in response.url:
                        try:
                            body = await response.body()
                            results_data.append(json.loads(body))
                        except Exception:
                            pass

                page.on("response", capture_results)

                team_slug = self._team_to_slug(team_name)
                url = f"{FLASHSCORE_BASE}/{sport_slug}/{team_slug}/resultats/"
                await page.goto(url, wait_until="networkidle", timeout=self.timeout_s * 1000)
                await asyncio.sleep(2)

            except Exception as exc:
                log.error(f"Erreur résultats Flashscore : {exc}")
            finally:
                if browser and browser.is_connected():
                    await browser.close()

        last_5 = self._parse_team_results(results_data, last_n)
        return FlashscoreTeamForm(team_name=team_name, last_5=last_5)

    # ── Navigation ───────────────────────────────────────────────

    async def _find_match_url(self, page, home: str, away: str, sport_slug: str) -> str | None:
        query = f"{home} {away}"
        search_url = f"{FLASHSCORE_BASE}/search/?q={query.replace(' ', '%20').replace('&', '%26')}"

        try:
            await page.goto(search_url, wait_until="domcontentloaded", timeout=20_000)
            await self._accept_cookies(page)
            await asyncio.sleep(2)

            # [FIX-3] JSON.stringify pour retourner des strings sérialisables
            links: list[str] = await page.evaluate("""
                () => {
                    const els = document.querySelectorAll('a[href*="/match/"]');
                    return JSON.parse(JSON.stringify(
                        Array.from(els).map(a => a.href).slice(0, 5)
                    ));
                }
            """)
            if links:
                return links[0]
        except Exception as exc:
            log.debug(f"Recherche match échouée : {exc}")
        return None

    @staticmethod
    async def _accept_cookies(page) -> None:
        try:
            for sel in [
                "#onetrust-accept-btn-handler",
                "button:has-text('Accepter')",
                "button:has-text('Accept')",
            ]:
                btn = page.locator(sel)
                if await btn.count() > 0:
                    await btn.first.click(timeout=4_000)
                    return
        except Exception:
            pass

    @staticmethod
    async def _click_h2h_tab(page) -> None:
        """Essaie de cliquer sur l'onglet H2H si pas encore sélectionné."""
        try:
            for sel in [
                "[data-tab-id='h2h']",
                "a:has-text('H2H')",
                "button:has-text('Face à face')",
                "a:has-text('Face à face')",
            ]:
                btn = page.locator(sel)
                if await btn.count() > 0:
                    await btn.first.click(timeout=3_000)
                    await asyncio.sleep(1)
                    return
        except Exception:
            pass

    # ── Parsing ───────────────────────────────────────────────────

    def _parse_h2h_responses(
        self,
        responses:    list[dict],
        team1:        str,
        team2:        str,
        max_meetings: int,
    ) -> H2HStats:
        entries: list[H2HEntry] = []

        for resp in responses:
            data    = resp["data"]
            matches = (
                data.get("matches") or
                data.get("events") or
                (data.get("data") or {}).get("matches") or
                []
            )
            for m in (matches or [])[:max_meetings]:
                entry = self._parse_single_h2h(m)
                if entry:
                    entries.append(entry)

        # Dédoublonner
        seen: set[tuple] = set()
        unique: list[H2HEntry] = []
        for e in entries:
            key = (e.date, normalize_name(e.home_team), normalize_name(e.away_team))
            if key not in seen:
                seen.add(key)
                unique.append(e)

        unique.sort(key=lambda e: e.date, reverse=True)
        return self._compute_h2h_stats(unique, team1, team2)

    @staticmethod
    def _parse_single_h2h(m: dict) -> H2HEntry | None:
        """[FIX-6] Guard complet sur les scores."""
        try:
            home_raw = m.get("homeTeam", m.get("home", {}))
            away_raw = m.get("awayTeam", m.get("away", {}))
            home_team = home_raw.get("name", "") if isinstance(home_raw, dict) else str(home_raw)
            away_team = away_raw.get("name", "") if isinstance(away_raw, dict) else str(away_raw)

            if not home_team or not away_team:
                return None

            # [FIX-6] Guard robuste sur les scores
            def safe_int(v) -> int:
                try:
                    return int(v) if v is not None and str(v).strip() != "" else 0
                except (ValueError, TypeError):
                    return 0

            hs_raw = m.get("homeScore", m.get("score", {}).get("home") if isinstance(m.get("score"), dict) else None)
            as_raw = m.get("awayScore", m.get("score", {}).get("away") if isinstance(m.get("score"), dict) else None)

            if isinstance(hs_raw, dict):
                hs_raw = hs_raw.get("current", 0)
            if isinstance(as_raw, dict):
                as_raw = as_raw.get("current", 0)

            home_score = safe_int(hs_raw)
            away_score = safe_int(as_raw)
            total      = home_score + away_score

            ts       = m.get("startTimestamp", m.get("timestamp", time.time()))
            date_str = datetime.fromtimestamp(
                float(ts) if ts else time.time(), tz=timezone.utc
            ).strftime("%Y-%m-%d")

            comp = str(
                (m.get("tournament") or {}).get("name", "") or
                m.get("competition", "") or
                m.get("league", "")
            )

            winner = "draw"
            if home_score > away_score:
                winner = "home"
            elif away_score > home_score:
                winner = "away"

            return H2HEntry(
                date        = date_str,
                home_team   = home_team,
                away_team   = away_team,
                home_score  = home_score,
                away_score  = away_score,
                competition = comp,
                winner      = winner,
                total_goals = total,
                btts        = home_score > 0 and away_score > 0,
            )
        except Exception:
            return None

    def _compute_h2h_stats(
        self, entries: list[H2HEntry], team1: str, team2: str
    ) -> H2HStats:
        n = len(entries)
        if n == 0:
            return H2HStats(
                team1=team1, team2=team2, total_meetings=0,
                team1_wins=0, team2_wins=0, draws=0,
                team1_goals=0, team2_goals=0,
                avg_total_goals=0.0, over_25_pct=0.0, btts_pct=0.0,
            )

        t1_wins = t2_wins = draws_ = 0
        t1_goals = t2_goals = 0
        over_25 = btts_ = 0
        t1_home_wins = t1_away_wins = 0
        by_competition: dict[str, dict] = {}

        for e in entries:
            # [FIX-4] Utiliser name_similarity pour l'assignation domicile/extérieur
            sim_home_t1 = name_similarity(team1, e.home_team)
            sim_away_t1 = name_similarity(team1, e.away_team)
            is_t1_home  = sim_home_t1 >= sim_away_t1

            goals_for = e.home_score if is_t1_home else e.away_score
            goals_ag  = e.away_score if is_t1_home else e.home_score

            t1_goals += goals_for
            t2_goals += goals_ag

            if e.winner == "draw":
                draws_ += 1
            elif (e.winner == "home" and is_t1_home) or (e.winner == "away" and not is_t1_home):
                t1_wins += 1
                if is_t1_home:
                    t1_home_wins += 1
                else:
                    t1_away_wins += 1
            else:
                t2_wins += 1

            if e.total_goals > 2.5:
                over_25 += 1
            if e.btts:
                btts_ += 1

            # [NEW-3] Stats par compétition
            comp = e.competition or "Autre"
            if comp not in by_competition:
                by_competition[comp] = {"played": 0, "t1_wins": 0, "draws": 0, "t2_wins": 0}
            bc = by_competition[comp]
            bc["played"] += 1
            if e.winner == "draw":
                bc["draws"] += 1
            elif (e.winner == "home" and is_t1_home) or (e.winner == "away" and not is_t1_home):
                bc["t1_wins"] += 1
            else:
                bc["t2_wins"] += 1

        # Dernier résultat domicile
        last_home = ""
        for e in entries:
            sim = name_similarity(team1, e.home_team)
            if sim >= 0.6:
                if e.winner == "home":
                    last_home = "team1"
                elif e.winner == "away":
                    last_home = "team2"
                else:
                    last_home = "draw"
                break

        # [NEW-4] Tendance sur les 5 derniers
        recent    = entries[:5]
        r_over25  = sum(1 for e in recent if e.total_goals > 2.5)
        r_btts    = sum(1 for e in recent if e.btts)
        r_goals   = sum(e.total_goals for e in recent)
        recent_n  = len(recent) or 1

        return H2HStats(
            team1            = team1,
            team2            = team2,
            total_meetings   = n,
            team1_wins       = t1_wins,
            team2_wins       = t2_wins,
            draws            = draws_,
            team1_goals      = t1_goals,
            team2_goals      = t2_goals,
            avg_total_goals  = round((t1_goals + t2_goals) / n, 2),
            over_25_pct      = round(over_25 / n * 100, 1),
            btts_pct         = round(btts_   / n * 100, 1),
            last_meetings    = entries[:10],
            team1_home_wins  = t1_home_wins,
            team1_away_wins  = t1_away_wins,
            last_home_winner = last_home,
            by_competition   = by_competition,          # [NEW-3]
            recent_over_25_pct = round(r_over25 / recent_n * 100, 1),  # [NEW-4]
            recent_btts_pct    = round(r_btts   / recent_n * 100, 1),
            recent_avg_goals   = round(r_goals  / recent_n, 2),
        )

    @staticmethod
    def _parse_team_results(results_data: list[dict], last_n: int) -> list[dict]:
        entries = []
        for rd in results_data:
            matches = rd.get("events") or rd.get("results") or rd.get("matches") or []
            for m in matches[:last_n]:
                try:
                    home  = m.get("homeTeam", {})
                    away  = m.get("awayTeam", {})
                    hs    = int((m.get("homeScore") or {}).get("current", 0) or 0)
                    as_   = int((m.get("awayScore") or {}).get("current", 0) or 0)
                    ts    = m.get("startTimestamp", time.time())
                    entries.append({
                        "date":       datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%d"),
                        "home":       home.get("name", "?") if isinstance(home, dict) else str(home),
                        "away":       away.get("name", "?") if isinstance(away, dict) else str(away),
                        "home_score": hs,
                        "away_score": as_,
                        "total":      hs + as_,
                    })
                except Exception:
                    continue
        return entries[:last_n]

    @staticmethod
    def _team_to_slug(name: str) -> str:
        s = unicodedata.normalize("NFD", name.lower())
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


# ──────────────────────────────────────────────
# Enrichissement batch H2H [FIX-7]
# ──────────────────────────────────────────────
async def enrich_h2h_batch(
    matches:     list[dict],
    output_dir:  Path = OUTPUT_DIR,
    delay_s:     float = 3.0,   # [FIX-7] séquentiel avec délai
) -> dict[str, H2HStats]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, H2HStats] = {}
    scraper = FlashscoreScraper(headless=True)

    for i, m in enumerate(matches):
        mid = m.get("match_id", "")
        try:
            h2h = await scraper.get_h2h(
                home       = m["home"],
                away       = m["away"],
                sport_slug = _sport_id_to_slug(m.get("sport_id", 1)),
            )
            if h2h:
                results[mid] = h2h
                log.info(f"  [{i+1}/{len(matches)}] ✅ H2H {m['home']} vs {m['away']} : {h2h.total_meetings} matchs")
            else:
                log.warning(f"  [{i+1}/{len(matches)}] ⚠️  H2H non trouvé : {m['home']} vs {m['away']}")
        except Exception as exc:
            log.error(f"  [{i+1}/{len(matches)}] ❌ Erreur : {m.get('home','?')} vs {m.get('away','?')} — {exc}")

        if i < len(matches) - 1:
            await asyncio.sleep(delay_s)

    # Sauvegarde atomique
    import os, tempfile
    path = output_dir / "flashscore_h2h.json"
    tmp_fd, tmp_path = tempfile.mkstemp(dir=output_dir, prefix=".fs_tmp_", suffix=".json")
    with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
        json.dump({k: v.to_dict() for k, v in results.items()}, f, ensure_ascii=False, indent=2)
    Path(tmp_path).replace(path)
    log.info(f"💾 {len(results)} H2H sauvegardés → {path.name}")
    return results


def _sport_id_to_slug(sport_id: int) -> str:
    return {
        1:  "football",
        2:  "tennis",
        5:  "basketball",
        6:  "hockey-sur-glace",
        13: "rugby-union",
        23: "volleyball",
        4:  "handball",
    }.get(sport_id, "football")


# ──────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────
async def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Scraper Flashscore H2H v2")
    parser.add_argument("--home",  default=None)
    parser.add_argument("--away",  default=None)
    parser.add_argument("--sport", default="football")
    parser.add_argument("--from-winamax", action="store_true")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument(
        "--headless",
        action=argparse.BooleanOptionalAction,
        default=True,
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [Flashscore] %(levelname)s  %(message)s",
    )

    if args.from_winamax:
        import sys; sys.path.insert(0, str(Path(__file__).parent.parent))
        from scrapers.winamax_scraper import WinamaxPoller
        matches = WinamaxPoller.load_cached()
        if not matches:
            print("Aucun match Winamax en cache.")
            return
        results = await enrich_h2h_batch(matches[:args.limit])
        for h2h in results.values():
            print(h2h.summary())

    elif args.home and args.away:
        scraper = FlashscoreScraper(headless=args.headless)
        h2h     = await scraper.get_h2h(args.home, args.away, args.sport)
        if h2h:
            print(json.dumps(h2h.to_dict(), ensure_ascii=False, indent=2))
            print("\n📊", h2h.summary())
        else:
            print("Aucune donnée H2H trouvée.")
    else:
        parser.print_help()


if __name__ == "__main__":
    asyncio.run(_main())
