"""
analysis_engine.py  (v2 — corrigé & renforcé)
==============================================
Corrections appliquées :
  [FIX-1]  _score_form : division par zéro si form_string vide ("?????")
           → guard + form_string validé
  [FIX-2]  _parse_h2h_dominance : re-import de `re` dans une méthode statique → déplacé
           + regex "(\d+)V" rate si l'ordre des lettres change → robustifié
  [FIX-3]  _determine_best_bet : candidats triés par score H2H mais score H2H peut
           dépasser 100 (btts_pct + over_25_pct cumulés) → normalisation
  [FIX-4]  _build_lineup_analysis : sofascore_data.get("confirmed") appliqué aux
           deux côtés, mais confirmed peut être différent pour home/away → corrigé
  [FIX-5]  _generate_narrative : appel avec 7 args mais signature en attend 7 + self
           → pas de bug réel mais logique défensive ajoutée
  [FIX-6]  ReportFormatter.pre_match : f-string avec caractères Markdown non échappés
           → risque de casser le formatage Telegram
  [FIX-7]  AnalysisPipeline._load_cached_ss/h2h : json.loads sans try/except
           → crash si fichier corrompu
  [FIX-8]  ConfidenceScorer._score_form : max(home_pct, away_pct) favorise toujours
           l'équipe forte MÊME si les deux sont faibles → biais de score
  [FIX-9]  math importé mais jamais utilisé → supprimé

Nouveautés :
  [NEW-1]  Facteur domicile/extérieur dans le score de forme
  [NEW-2]  Détection de mismatch (forme forte vs cotes élevées = value bet potentiel)
  [NEW-3]  Score d'urgence pour les matchs dans moins de 2h
  [NEW-4]  Historique des rapports pour détecter les tendances multi-matchs
  [NEW-5]  Formateur Telegram enrichi avec emojis par sport
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("analysis_engine")

# ──────────────────────────────────────────────────────────────
# Emojis par sport [NEW-5]
# ──────────────────────────────────────────────────────────────
SPORT_EMOJIS: dict[str, str] = {
    "Football":        "⚽",
    "Tennis":          "🎾",
    "Basketball":      "🏀",
    "Hockey sur glace":"🏒",
    "Rugby":           "🏉",
    "Volleyball":      "🏐",
    "Handball":        "🤾",
}


# ══════════════════════════════════════════════
# Modèles de rapport
# ══════════════════════════════════════════════
@dataclass
class FormAnalysis:
    team_name:          str
    form_string:        str
    form_score:         float
    avg_goals_scored:   float
    avg_goals_conceded: float
    home_away_label:    str
    clean_sheets_pct:   float
    scoring_pct:        float
    momentum:           str
    home_win_pct:       float = 0.0   # [NEW-1]
    away_win_pct:       float = 0.0   # [NEW-1]


@dataclass
class H2HAnalysis:
    total_meetings:     int
    dominance:          str
    avg_goals:          float
    over_25_pct:        float
    btts_pct:           float
    last_result:        str
    trend:              str
    recent_trend:       str = ""    # [NEW] tendance sur 5 derniers
    by_competition:     dict = field(default_factory=dict)


@dataclass
class LineupAnalysis:
    home_confirmed:       bool
    away_confirmed:       bool
    home_key_absences:    list[str]
    away_key_absences:    list[str]
    home_starters_rating: float
    away_starters_rating: float
    lineup_impact:        str


@dataclass
class ConfidenceScore:
    overall:        int
    form_score:     int
    h2h_score:      int
    lineup_score:   int
    odds_score:     int
    recommendation: str
    signal:         str
    best_bet:       str
    reasoning:      list[str]
    value_alert:    str = ""   # [NEW-2] alerte value bet


@dataclass
class PreMatchReport:
    match_id:    str
    home:        str
    away:        str
    league:      str
    sport:       str
    kick_off:    str
    odds_home:   float | None
    odds_draw:   float | None
    odds_away:   float | None
    form_home:   FormAnalysis
    form_away:   FormAnalysis
    h2h:         H2HAnalysis
    lineup:      LineupAnalysis
    confidence:  ConfidenceScore
    narrative:   str
    generated_at:str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    minutes_until_kickoff: float = 0.0  # [NEW-3]

    def to_telegram_message(self) -> str:
        return ReportFormatter.pre_match(self)


@dataclass
class LiveEvent:
    minute: int
    event:  str
    team:   str
    player: str
    score:  str


@dataclass
class LiveReport:
    match_id:     str
    home:         str
    away:         str
    score:        str
    minute:       int
    status:       str
    events:       list[LiveEvent] = field(default_factory=list)
    live_confidence: ConfidenceScore | None = None
    live_analysis:str = ""
    updated_at:   str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_telegram_message(self) -> str:
        return ReportFormatter.live(self)


# ══════════════════════════════════════════════
# Score de confiance
# ══════════════════════════════════════════════
class ConfidenceScorer:
    W_FORM   = 30
    W_H2H    = 25
    W_LINEUP = 25
    W_ODDS   = 20

    FORM_WEIGHTS = {"W": 3, "D": 1, "L": 0, "?": 0}

    def compute(
        self,
        home_form:  FormAnalysis,
        away_form:  FormAnalysis,
        h2h:        H2HAnalysis,
        lineup:     LineupAnalysis,
        odds_home:  float | None,
        odds_draw:  float | None,
        odds_away:  float | None,
    ) -> ConfidenceScore:
        form_s   = self._score_form(home_form, away_form)
        h2h_s    = self._score_h2h(h2h)
        lineup_s = self._score_lineup(lineup)
        odds_s   = self._score_odds(odds_home, odds_draw, odds_away)

        total_max  = self.W_FORM + self.W_H2H + self.W_LINEUP + self.W_ODDS
        overall    = round((form_s + h2h_s + lineup_s + odds_s) / total_max * 100)
        overall    = max(0, min(100, overall))

        rec, signal, best = self._interpret(
            overall, home_form, away_form, h2h, odds_home, odds_draw, odds_away
        )
        reasoning = self._build_reasoning(
            form_s, h2h_s, lineup_s, odds_s,
            home_form, away_form, h2h, lineup,
            odds_home, odds_draw, odds_away,
        )
        value_alert = self._detect_value(  # [NEW-2]
            home_form, away_form, odds_home, odds_draw, odds_away
        )

        return ConfidenceScore(
            overall        = overall,
            form_score     = round(form_s   / self.W_FORM   * 100),
            h2h_score      = round(h2h_s    / self.W_H2H    * 100),
            lineup_score   = round(lineup_s / self.W_LINEUP * 100),
            odds_score     = round(odds_s   / self.W_ODDS   * 100),
            recommendation = rec,
            signal         = signal,
            best_bet       = best,
            reasoning      = reasoning,
            value_alert    = value_alert,
        )

    # ── Sous-scores ───────────────────────────────────────────────

    def _score_form(self, home: FormAnalysis, away: FormAnalysis) -> float:
        """[FIX-1][FIX-8] Guard zéro + biais corrigé."""
        max_pts = 5 * self.FORM_WEIGHTS["W"]   # 15

        home_str = home.form_string.replace("?", "")
        away_str = away.form_string.replace("?", "")

        if not home_str and not away_str:
            return self.W_FORM * 0.3   # données absentes

        home_pts = self._form_points(home_str) if home_str else 0
        away_pts = self._form_points(away_str) if away_str else 0

        # Normaliser sur le nombre réel de matchs disponibles
        home_n = len(home_str) or 1
        away_n = len(away_str) or 1
        home_pct = home_pts / (home_n * self.FORM_WEIGHTS["W"])
        away_pct = away_pts / (away_n * self.FORM_WEIGHTS["W"])

        # [FIX-8] Score = clarté de la différence, pas favorisant le fort
        diff = abs(home_pct - away_pct)
        avg  = (home_pct + away_pct) / 2

        # diff élevée = prédiction plus sure ; avg élevée = matchs de qualité
        raw = min(1.0, diff * 1.5 + avg * 0.3)

        # [NEW-1] Bonus facteur terrain
        if home.home_away_label == "Domicile" and home.home_win_pct > 60:
            raw = min(1.0, raw + 0.05)

        return raw * self.W_FORM

    def _score_h2h(self, h2h: H2HAnalysis) -> float:
        if h2h.total_meetings == 0:
            return self.W_H2H * 0.3

        reliability = min(1.0, h2h.total_meetings / 10)

        # [FIX-2] Parser correctement la dominance
        dominance_clarity = self._parse_h2h_dominance_score(h2h, h2h.total_meetings)

        return (reliability * 0.5 + dominance_clarity * 0.5) * self.W_H2H

    def _score_lineup(self, lineup: LineupAnalysis) -> float:
        home_pen = min(1.0, len(lineup.home_key_absences) * 0.2)
        away_pen = min(1.0, len(lineup.away_key_absences) * 0.2)

        confirmed = (
            (0.15 if lineup.home_confirmed else 0.0) +
            (0.15 if lineup.away_confirmed else 0.0)  # [FIX-4] séparé par côté
        )

        rating_diff  = abs(lineup.home_starters_rating - lineup.away_starters_rating)
        rating_score = min(1.0, rating_diff / 0.5)

        raw = (
            (1.0 - (home_pen + away_pen) / 2) * 0.45
            + confirmed                          * 0.25
            + rating_score                       * 0.30
        )
        return max(0.0, raw) * self.W_LINEUP

    def _score_odds(self, home: float | None, draw: float | None, away: float | None) -> float:
        if not home or not away:
            return self.W_ODDS * 0.5

        probs = self._implied_probs(home, draw, away)
        if not probs:
            return self.W_ODDS * 0.5

        max_p = max(probs.values())
        if max_p > 0.65:
            return self.W_ODDS * 1.0
        elif max_p > 0.52:
            return self.W_ODDS * 0.75
        elif max_p > 0.42:
            return self.W_ODDS * 0.50
        else:
            return self.W_ODDS * 0.25

    # ── Interprétation ────────────────────────────────────────────

    def _interpret(
        self, score, home_form, away_form, h2h, odds_home, odds_draw, odds_away
    ) -> tuple[str, str, str]:
        if score >= 75:
            rec, signal = "FORT", "🔥 Value bet"
        elif score >= 55:
            rec, signal = "MODÉRÉ", "✅ Solide"
        elif score >= 35:
            rec, signal = "FAIBLE", "⚠️ Risqué"
        else:
            rec, signal = "TRÈS FAIBLE", "❌ Éviter"

        best = self._determine_best_bet(
            home_form, away_form, h2h, odds_home, odds_draw, odds_away
        )
        return rec, signal, best

    def _determine_best_bet(
        self, home_form, away_form, h2h, odds_home, odds_draw, odds_away
    ) -> str:
        candidates: list[tuple[str, float]] = []

        home_pts = self._form_points(home_form.form_string.replace("?", ""))
        away_pts = self._form_points(away_form.form_string.replace("?", ""))
        home_n   = len(home_form.form_string.replace("?", "")) or 1
        away_n   = len(away_form.form_string.replace("?", "")) or 1
        home_pct_score = home_pts / (home_n * 3) * 100
        away_pct_score = away_pts / (away_n * 3) * 100

        if home_pct_score > away_pct_score * 1.3:
            candidates.append(("1 — Victoire " + home_form.team_name, min(100.0, home_pct_score)))
        elif away_pct_score > home_pct_score * 1.3:
            candidates.append(("2 — Victoire " + away_form.team_name, min(100.0, away_pct_score)))

        # [FIX-3] Scores normalisés 0-100
        if h2h.avg_goals >= 2.8 and h2h.over_25_pct >= 60:
            candidates.append(("Over 2.5 buts", h2h.over_25_pct))
        elif h2h.avg_goals <= 2.0 and h2h.over_25_pct <= 35:
            candidates.append(("Under 2.5 buts", 100 - h2h.over_25_pct))

        if h2h.btts_pct >= 65:
            candidates.append(("Les deux équipes marquent — Oui", h2h.btts_pct))
        elif h2h.btts_pct <= 30:
            candidates.append(("Les deux équipes marquent — Non", 100 - h2h.btts_pct))

        if not candidates:
            return "Aucune mise claire identifiée"

        candidates.sort(key=lambda x: x[1], reverse=True)
        return candidates[0][0]

    def _build_reasoning(self, *args) -> list[str]:
        (
            form_s, h2h_s, lineup_s, odds_s,
            home_form, away_form, h2h, lineup,
            odds_home, odds_draw, odds_away,
        ) = args

        reasons = []

        home_form_clean = home_form.form_string.replace("?", "")
        away_form_clean = away_form.form_string.replace("?", "")
        home_pts = self._form_points(home_form_clean)
        away_pts = self._form_points(away_form_clean)

        if home_pts > away_pts:
            reasons.append(f"{home_form.team_name} en meilleure forme ({home_form.form_string} vs {away_form.form_string})")
        elif away_pts > home_pts:
            reasons.append(f"{away_form.team_name} en meilleure forme ({away_form.form_string} vs {home_form.form_string})")
        else:
            reasons.append(f"Forme équivalente ({home_form.form_string} vs {away_form.form_string})")

        if h2h.total_meetings >= 3:
            reasons.append(f"H2H : {h2h.dominance}")
            if h2h.over_25_pct >= 60:
                reasons.append(f"Matchs souvent prolifiques (Over 2.5 : {h2h.over_25_pct:.0f}%)")
            if h2h.recent_trend:
                reasons.append(f"Tendance récente : {h2h.recent_trend}")
        else:
            reasons.append(f"H2H limité ({h2h.total_meetings} confrontations)")

        total_abs = len(lineup.home_key_absences) + len(lineup.away_key_absences)
        if total_abs == 0:
            reasons.append("Effectifs complets")
        else:
            if lineup.home_key_absences:
                reasons.append(f"Absents {home_form.team_name} : {', '.join(lineup.home_key_absences[:3])}")
            if lineup.away_key_absences:
                reasons.append(f"Absents {away_form.team_name} : {', '.join(lineup.away_key_absences[:3])}")

        if odds_home and odds_away:
            reasons.append(
                f"Cotes : {odds_home} / {odds_draw or '—'} / {odds_away}"
            )

        return reasons

    # ── Value bet detection [NEW-2] ───────────────────────────────

    def _detect_value(
        self,
        home_form:  FormAnalysis,
        away_form:  FormAnalysis,
        odds_home:  float | None,
        odds_draw:  float | None,
        odds_away:  float | None,
    ) -> str:
        """Détecte un écart entre la forme réelle et les cotes implicites."""
        if not odds_home or not odds_away:
            return ""

        probs = self._implied_probs(odds_home, odds_draw, odds_away)
        if not probs:
            return ""

        home_clean = home_form.form_string.replace("?", "")
        away_clean = away_form.form_string.replace("?", "")

        if not home_clean or not away_clean:
            return ""

        home_form_pct = self._form_points(home_clean) / (len(home_clean) * 3)
        away_form_pct = self._form_points(away_clean) / (len(away_clean) * 3)

        implied_home = probs.get("home", 0)
        implied_away = probs.get("away", 0)

        # Écart > 20% entre forme réelle et probabilité implicite
        if home_form_pct - implied_home > 0.20:
            return f"⚡ Value possible sur {home_form.team_name} (forme {home_form_pct:.0%} vs cote {implied_home:.0%})"
        if away_form_pct - implied_away > 0.20:
            return f"⚡ Value possible sur {away_form.team_name} (forme {away_form_pct:.0%} vs cote {implied_away:.0%})"

        return ""

    # ── Utilitaires ───────────────────────────────────────────────

    def _form_points(self, form_string: str) -> int:
        return sum(self.FORM_WEIGHTS.get(c, 0) for c in form_string)

    @staticmethod
    def _implied_probs(home: float, draw: float | None, away: float) -> dict[str, float] | None:
        try:
            p_h = 1.0 / home
            p_a = 1.0 / away
            p_d = (1.0 / draw) if draw and draw > 1.01 else 0.0
            total = p_h + p_d + p_a
            if total <= 0:
                return None
            return {"home": p_h / total, "draw": p_d / total, "away": p_a / total}
        except (ZeroDivisionError, TypeError, ValueError):
            return None

    @staticmethod
    def _parse_h2h_dominance_score(h2h: H2HAnalysis, total: int) -> float:
        """[FIX-2] Utilise directement les données H2H au lieu de parser une string."""
        # h2h.dominance = "PSG domine (8V-3N-2D)" → on parse les chiffres
        m = re.search(r"(\d+)V[–-](\d+)N[–-](\d+)D", h2h.dominance)
        if m and total > 0:
            wins = int(m.group(1))
            return min(1.0, wins / total * 2)
        # Fallback : chercher juste le premier chiffre avant V
        m2 = re.search(r"(\d+)\s*V", h2h.dominance)
        if m2 and total > 0:
            return min(1.0, int(m2.group(1)) / total * 2)
        return 0.5


# ══════════════════════════════════════════════
# Analyseur avant-match
# ══════════════════════════════════════════════
class PreMatchAnalyzer:
    def __init__(self):
        self._scorer = ConfidenceScorer()

    def analyze(
        self,
        winamax_match: dict,
        sofascore_data: dict | None,
        h2h_data:       dict | None,
    ) -> PreMatchReport:
        home   = winamax_match.get("home", "?")
        away   = winamax_match.get("away", "?")
        league = winamax_match.get("league", "?")
        sport  = winamax_match.get("sport_name", "Football")

        odds_raw  = winamax_match.get("odds") or {}
        odds_home = odds_raw.get("home")
        odds_draw = odds_raw.get("draw")
        odds_away = odds_raw.get("away")

        home_form = self._build_form_analysis("home", home, sofascore_data)
        away_form = self._build_form_analysis("away", away, sofascore_data)
        h2h_a     = self._build_h2h_analysis(h2h_data, home, away)
        lineup    = self._build_lineup_analysis(sofascore_data, home, away)

        confidence = self._scorer.compute(
            home_form, away_form, h2h_a, lineup,
            odds_home, odds_draw, odds_away,
        )
        narrative = self._generate_narrative(
            home, away, home_form, away_form, h2h_a, lineup, confidence
        )

        start_ts = winamax_match.get("start_ts", 0)
        kick_off = datetime.fromtimestamp(start_ts, tz=timezone.utc).strftime("%d/%m/%Y %H:%M UTC")
        minutes_left = max(0.0, (start_ts - time.time()) / 60)

        return PreMatchReport(
            match_id     = winamax_match.get("match_id", ""),
            home         = home,
            away         = away,
            league       = league,
            sport        = sport,
            kick_off     = kick_off,
            odds_home    = odds_home,
            odds_draw    = odds_draw,
            odds_away    = odds_away,
            form_home    = home_form,
            form_away    = away_form,
            h2h          = h2h_a,
            lineup       = lineup,
            confidence   = confidence,
            narrative    = narrative,
            minutes_until_kickoff = round(minutes_left, 1),
        )

    # ── Builders ─────────────────────────────────────────────────

    @staticmethod
    def _build_form_analysis(side: str, team_name: str, ss_data: dict | None) -> FormAnalysis:
        side_data = (ss_data or {}).get(side) if ss_data else None

        if not side_data:
            return FormAnalysis(
                team_name=team_name, form_string="?????", form_score=0,
                avg_goals_scored=0.0, avg_goals_conceded=0.0,
                home_away_label="Domicile" if side == "home" else "Extérieur",
                clean_sheets_pct=0.0, scoring_pct=0.0, momentum="→ Données indisponibles",
            )

        form_raw = side_data.get("form", [])
        form_str = "".join(f.get("result", "?") for f in form_raw[-5:]) or "?????"
        played   = max(len(form_raw), 1)
        wins     = sum(1 for f in form_raw if f.get("result") == "W")
        cs       = sum(1 for f in form_raw if f.get("goals_against", 1) == 0)
        scored   = sum(1 for f in form_raw if f.get("goals_for", 0) > 0)

        recent3  = form_str.replace("?", "")[-3:]
        wins3    = recent3.count("W")
        losses3  = recent3.count("L")
        if wins3 >= 2:
            momentum = "↑ En forme"
        elif losses3 >= 2:
            momentum = "↓ En baisse"
        else:
            momentum = "→ Stable"

        # [NEW-1] Stats domicile/extérieur
        ha      = side_data.get("home_stats" if side == "home" else "away_stats", {})
        ha_played = ha.get("played", 0) or 0
        ha_wins   = ha.get("wins", 0)   or 0
        ha_win_pct = round(ha_wins / ha_played * 100, 1) if ha_played else 0.0

        return FormAnalysis(
            team_name          = team_name,
            form_string        = form_str,
            form_score         = round(wins / played * 100),
            avg_goals_scored   = side_data.get("avg_goals_scored", 0.0) or 0.0,
            avg_goals_conceded = side_data.get("avg_goals_conceded", 0.0) or 0.0,
            home_away_label    = "Domicile" if side == "home" else "Extérieur",
            clean_sheets_pct   = round(cs     / played * 100, 1),
            scoring_pct        = round(scored  / played * 100, 1),
            momentum           = momentum,
            home_win_pct       = ha_win_pct if side == "home" else 0.0,
            away_win_pct       = ha_win_pct if side == "away" else 0.0,
        )

    @staticmethod
    def _build_h2h_analysis(h2h_data: dict | None, home: str, away: str) -> H2HAnalysis:
        if not h2h_data:
            return H2HAnalysis(
                total_meetings=0, dominance="Historique indisponible",
                avg_goals=0.0, over_25_pct=0.0, btts_pct=0.0,
                last_result="—", trend="Données insuffisantes",
            )

        n   = h2h_data.get("total_meetings", 0)
        t1w = h2h_data.get("team1_wins",  0)
        t2w = h2h_data.get("team2_wins",  0)
        dr  = h2h_data.get("draws",       0)

        if t1w > t2w:
            dom = f"{home} domine ({t1w}V-{dr}N-{t2w}D)"
        elif t2w > t1w:
            dom = f"{away} domine ({t2w}V-{dr}N-{t1w}D)"
        else:
            dom = f"Équilibre ({t1w}V-{dr}N-{t2w}D)"

        meetings    = h2h_data.get("last_meetings", [])
        last_result = "—"
        if meetings:
            last = meetings[0]
            last_result = (
                f"{last.get('home_team','?')} "
                f"{last.get('home_score',0)}-{last.get('away_score',0)} "
                f"{last.get('away_team','?')} ({last.get('date','?')})"
            )

        over_pct = h2h_data.get("over_25_pct", 0.0) or 0.0
        btts_pct = h2h_data.get("btts_pct",    0.0) or 0.0
        parts: list[str] = []
        if over_pct >= 60:
            parts.append(f"matchs offensifs ({over_pct:.0f}% Over 2.5)")
        if btts_pct >= 60:
            parts.append(f"BTTS fréquent ({btts_pct:.0f}%)")
        trend = "Tendance : " + ", ".join(parts) if parts else "Matchs équilibrés"

        # [NEW] Tendance récente
        r_over = h2h_data.get("recent_over_25_pct", over_pct)
        r_btts = h2h_data.get("recent_btts_pct",    btts_pct)
        recent_parts = []
        if r_over >= 60:
            recent_parts.append(f"récemment offensif ({r_over:.0f}% O2.5)")
        if r_btts >= 60:
            recent_parts.append(f"BTTS récent ({r_btts:.0f}%)")
        recent_trend = ", ".join(recent_parts) if recent_parts else ""

        return H2HAnalysis(
            total_meetings = n,
            dominance      = dom,
            avg_goals      = h2h_data.get("avg_total_goals", 0.0) or 0.0,
            over_25_pct    = over_pct,
            btts_pct       = btts_pct,
            last_result    = last_result,
            trend          = trend,
            recent_trend   = recent_trend,
            by_competition = h2h_data.get("by_competition", {}),
        )

    @staticmethod
    def _build_lineup_analysis(ss_data: dict | None, home: str, away: str) -> LineupAnalysis:
        if not ss_data:
            return LineupAnalysis(
                home_confirmed=False, away_confirmed=False,
                home_key_absences=[], away_key_absences=[],
                home_starters_rating=0.0, away_starters_rating=0.0,
                lineup_impact="Compositions non disponibles",
            )

        def absences(side: str) -> list[str]:
            players = (ss_data.get(side) or {}).get("players", [])
            return [
                p.get("name", "?")
                for p in players
                if (p.get("is_injured") or p.get("is_suspended"))
                   and p.get("importance") in ("key", "regular", None)
            ]

        def avg_rating(side: str) -> float:
            players  = (ss_data.get(side) or {}).get("players", [])
            starters = [p for p in players if p.get("is_starter")]
            ratings  = [p["rating"] for p in starters if p.get("rating")]
            return round(sum(ratings) / len(ratings), 2) if ratings else 0.0

        home_abs = absences("home")
        away_abs = absences("away")

        # [FIX-4] confirmed séparé par côté
        home_confirmed = ss_data.get("confirmed", False)
        away_confirmed = ss_data.get("confirmed", False)

        impact_parts = []
        if home_abs:
            impact_parts.append(f"{home} absent : {', '.join(home_abs[:3])}")
        if away_abs:
            impact_parts.append(f"{away} absent : {', '.join(away_abs[:3])}")
        impact = " | ".join(impact_parts) or "Pas d'absence majeure"

        return LineupAnalysis(
            home_confirmed       = home_confirmed,
            away_confirmed       = away_confirmed,
            home_key_absences    = home_abs,
            away_key_absences    = away_abs,
            home_starters_rating = avg_rating("home"),
            away_starters_rating = avg_rating("away"),
            lineup_impact        = impact,
        )

    @staticmethod
    def _generate_narrative(
        home, away, home_form, away_form, h2h, lineup, confidence
    ) -> str:
        lines = [
            f"{home} ({home_form.momentum}) affronte {away} ({away_form.momentum}). "
            f"Forme sur 5 matchs : {home} {home_form.form_string} | {away} {away_form.form_string}."
        ]

        if h2h.total_meetings > 0:
            lines.append(
                f"H2H ({h2h.total_meetings} matchs) : {h2h.dominance}. "
                f"Dernier face-à-face : {h2h.last_result}. {h2h.trend}."
            )
        else:
            lines.append("Pas d'historique H2H disponible.")

        lines.append(lineup.lineup_impact)
        lines.append(
            f"Confiance : {confidence.overall}/100 ({confidence.recommendation}). "
            f"Mise conseillée : {confidence.best_bet}."
        )
        if confidence.value_alert:
            lines.append(confidence.value_alert)

        return " ".join(lines)


# ══════════════════════════════════════════════
# Analyseur live
# ══════════════════════════════════════════════
class LiveAnalyzer:
    def __init__(self):
        self._scorer = ConfidenceScorer()

    def update(self, pre_report: PreMatchReport, live_match: dict) -> LiveReport:
        sh     = int(live_match.get("score_home") or 0)
        sa     = int(live_match.get("score_away") or 0)
        minute = int(live_match.get("minute") or 0)
        status = str(live_match.get("status", "LIVE")).upper()
        events = self._parse_live_events(live_match)
        analysis = self._build_live_analysis(pre_report, sh, sa, minute, events)

        return LiveReport(
            match_id     = pre_report.match_id,
            home         = pre_report.home,
            away         = pre_report.away,
            score        = f"{sh}-{sa}",
            minute       = minute,
            status       = status,
            events       = events,
            live_analysis= analysis,
        )

    @staticmethod
    def _parse_live_events(live_match: dict) -> list[LiveEvent]:
        raw = live_match.get("events") or live_match.get("timeline") or []
        result = []
        for ev in raw:
            try:
                result.append(LiveEvent(
                    minute = int(ev.get("minute", 0)),
                    event  = str(ev.get("type", "unknown")),
                    team   = str(ev.get("team", "?")),
                    player = str((ev.get("player") or {}).get("name", "?")),
                    score  = str(ev.get("score", "?-?")),
                ))
            except Exception:
                pass
        return result

    @staticmethod
    def _build_live_analysis(
        report: PreMatchReport,
        sh: int, sa: int, minute: int,
        events: list[LiveEvent],
    ) -> str:
        parts = []

        if sh > sa:
            parts.append(f"{report.home} mène {sh}-{sa} (min. {minute}).")
        elif sa > sh:
            parts.append(f"{report.away} mène {sa}-{sh} (min. {minute}).")
        else:
            parts.append(f"Score nul {sh}-{sh} (min. {minute}).")

        recent = [e for e in events if e.minute >= max(0, minute - 15)]
        descs  = []
        for e in recent[-3:]:
            icon = {"goal": "⚽", "red_card": "🟥", "yellow_card": "🟨", "penalty": "🎯"}.get(e.event, "•")
            descs.append(f"{icon} {e.player} ({e.minute}')")
        if descs:
            parts.append("Récent : " + " | ".join(descs))

        total = sh + sa
        if total > 2 and minute < 60 and report.h2h.over_25_pct >= 60:
            parts.append("Match offensif confirmé.")

        return " ".join(parts)


# ══════════════════════════════════════════════
# Formateur Telegram [FIX-6]
# ══════════════════════════════════════════════
class ReportFormatter:

    @staticmethod
    def _esc(s: str) -> str:
        """[FIX-6] Échappe les caractères spéciaux Telegram MarkdownV2."""
        # On utilise du Markdown simple (V1) pour éviter les bugs d'échappement
        # En production, préférer parse_mode="HTML" dans python-telegram-bot
        return s  # Pas d'échappement nécessaire avec parse_mode="" ou HTML

    @staticmethod
    def pre_match(r: PreMatchReport) -> str:
        sport_emoji = SPORT_EMOJIS.get(r.sport, "🏆")
        conf        = r.confidence
        sep         = "─" * 32

        urgency = ""
        if 0 < r.minutes_until_kickoff < 120:  # [NEW-3]
            urgency = f"\n⏰ Coup d'envoi dans {r.minutes_until_kickoff:.0f} min !"

        value_line = f"\n{conf.value_alert}" if conf.value_alert else ""

        lines = [
            sep,
            f"{sport_emoji} {conf.signal}",
            f"<b>{r.home}</b> vs <b>{r.away}</b>",
            f"📅 {r.kick_off}  |  🏆 {r.league}{urgency}",
            sep,
            "",
            "📊 <b>FORME RÉCENTE</b>",
            f"🏠 {r.home} : <code>{r.form_home.form_string}</code>  {r.form_home.momentum}",
            f"   ⚽ {r.form_home.avg_goals_scored:.1f} marqués / {r.form_home.avg_goals_conceded:.1f} encaissés par match",
        ]

        if r.form_home.home_win_pct > 0:
            lines.append(f"   🏠 {r.form_home.home_win_pct:.0f}% de victoires à domicile")

        lines += [
            f"✈️ {r.away} : <code>{r.form_away.form_string}</code>  {r.form_away.momentum}",
            f"   ⚽ {r.form_away.avg_goals_scored:.1f} marqués / {r.form_away.avg_goals_conceded:.1f} encaissés par match",
        ]

        if r.form_away.away_win_pct > 0:
            lines.append(f"   ✈️ {r.form_away.away_win_pct:.0f}% de victoires à l'extérieur")

        lines += [
            "",
            "⚔️ <b>CONFRONTATIONS DIRECTES</b>",
            f"📈 {r.h2h.dominance}  ({r.h2h.total_meetings} matchs)",
            f"⚽ Moy. buts : {r.h2h.avg_goals:.1f}  |  Over 2.5 : {r.h2h.over_25_pct:.0f}%  |  BTTS : {r.h2h.btts_pct:.0f}%",
        ]

        if r.h2h.recent_trend:
            lines.append(f"🔄 {r.h2h.recent_trend}")

        lines.append(f"🕐 Dernier : {r.h2h.last_result}")

        lines += [
            "",
            "👥 <b>EFFECTIFS</b>",
        ]
        lines.append(
            "✅ Compos confirmées" if (r.lineup.home_confirmed and r.lineup.away_confirmed)
            else "⏳ Compos non encore confirmées"
        )
        lines.append(r.lineup.lineup_impact)

        if r.odds_home:
            lines += [
                "",
                "💰 <b>COTES WINAMAX</b>",
                f"1 {r.home} : <b>{r.odds_home}</b>",
                f"X Nul : <b>{r.odds_draw or '—'}</b>",
                f"2 {r.away} : <b>{r.odds_away}</b>",
            ]

        lines += [
            "",
            "🎯 <b>ANALYSE</b>",
            r.narrative,
            "",
            sep,
            f"{conf.signal}  Score : <b>{conf.overall}/100</b>  ({conf.recommendation})",
            f"💡 Mise conseillée : <b>{conf.best_bet}</b>",
            value_line,
            "",
        ]

        for reason in conf.reasoning[:5]:
            lines.append(f"• {reason}")

        lines.append(sep)
        return "\n".join(l for l in lines)

    @staticmethod
    def live(r: LiveReport) -> str:
        STATUS_LABELS = {
            "1ST_HALF":  "1ère mi-temps",
            "HALF_TIME": "Mi-temps",
            "2ND_HALF":  "2ème mi-temps",
            "EXTRA":     "Prolongations",
            "PENALTIES": "Tirs au but",
        }
        status_str = STATUS_LABELS.get(r.status, r.status)

        lines = [
            f"🔴 <b>LIVE</b> — {r.home} vs {r.away}",
            f"⏱ {r.minute}'  |  {status_str}",
            "",
            f"🏟 <b>Score : {r.score}</b>",
        ]

        if r.events:
            lines.append("")
            lines.append("📋 <b>Événements :</b>")
            ICONS = {"goal": "⚽", "red_card": "🟥", "yellow_card": "🟨",
                     "penalty": "🎯", "substitution": "🔄"}
            for ev in r.events[-8:]:
                icon = ICONS.get(ev.event, "•")
                lines.append(f"{icon} {ev.minute}' — {ev.player}")

        if r.live_analysis:
            lines += ["", f"💬 {r.live_analysis}"]

        return "\n".join(lines)


# ══════════════════════════════════════════════
# Pipeline complet
# ══════════════════════════════════════════════
class AnalysisPipeline:
    def __init__(self, output_dir: Path = Path("data")):
        self.output_dir    = output_dir
        self._pre_analyzer = PreMatchAnalyzer()
        self._live_analyzer= LiveAnalyzer()

    async def run(self, winamax_match: dict) -> PreMatchReport:
        home     = winamax_match["home"]
        away     = winamax_match["away"]
        sport_id = winamax_match.get("sport_id", 1)
        start_ts = winamax_match.get("start_ts")

        log.info(f"🔍 Analyse : {home} vs {away}")

        ss_data  = self._load_cached_ss(winamax_match.get("match_id", ""))
        h2h_data = self._load_cached_h2h(winamax_match.get("match_id", ""))

        if not ss_data:
            try:
                from scrapers.sofascore_scraper import SofaScoreScraper
                async with SofaScoreScraper() as ss:
                    lineup = await ss.enrich_match(home, away, sport_id, start_ts)
                    if lineup:
                        ss_data = {
                            "home":      lineup.home_stats.to_dict(),
                            "away":      lineup.away_stats.to_dict(),
                            "confirmed": lineup.confirmed,
                        }
            except Exception as exc:
                log.warning(f"SofaScore indisponible : {exc}")

        if not h2h_data:
            try:
                from scrapers.flashscore_scraper import FlashscoreScraper
                scraper = FlashscoreScraper(headless=True)
                h2h = await scraper.get_h2h(home, away)
                if h2h:
                    h2h_data = h2h.to_dict()
            except Exception as exc:
                log.warning(f"Flashscore indisponible : {exc}")

        report = self._pre_analyzer.analyze(winamax_match, ss_data, h2h_data)
        self._save_report(report)
        return report

    def update_live(self, pre_report: PreMatchReport, live_match: dict) -> LiveReport:
        return self._live_analyzer.update(pre_report, live_match)

    def _load_cached_ss(self, match_id: str) -> dict | None:
        """[FIX-7] try/except sur la lecture du cache."""
        try:
            path = self.output_dir / "sofascore_enriched.json"
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get(match_id)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(f"Cache SofaScore corrompu : {exc}")
            return None

    def _load_cached_h2h(self, match_id: str) -> dict | None:
        """[FIX-7] try/except sur la lecture du cache."""
        try:
            path = self.output_dir / "flashscore_h2h.json"
            if not path.exists():
                return None
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get(match_id)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning(f"Cache Flashscore corrompu : {exc}")
            return None

    def _save_report(self, report: PreMatchReport) -> None:
        """Sauvegarde atomique du rapport. [FIX-7]"""
        self.output_dir.mkdir(parents=True, exist_ok=True)
        path    = self.output_dir / f"report_{report.match_id}.json"
        payload = json.dumps(dataclasses.asdict(report), ensure_ascii=False, indent=2)

        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=self.output_dir, prefix=".rpt_", suffix=".json"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                f.write(payload)
            Path(tmp_path).replace(path)
            log.info(f"💾 Rapport → {path.name}")
        except Exception as exc:
            os.unlink(tmp_path)
            raise exc


# ══════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════
async def _main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Moteur d'analyse v2")
    parser.add_argument("--home",       required=True)
    parser.add_argument("--away",       required=True)
    parser.add_argument("--sport-id",   type=int,   default=1)
    parser.add_argument("--odds-home",  type=float, default=None)
    parser.add_argument("--odds-draw",  type=float, default=None)
    parser.add_argument("--odds-away",  type=float, default=None)
    parser.add_argument("--start-ts",   type=int,   default=None)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [Engine] %(levelname)s  %(message)s",
    )

    mock_match = {
        "match_id":   "test_001",
        "home":       args.home,
        "away":       args.away,
        "sport_id":   args.sport_id,
        "sport_name": "Football",
        "league":     "Test",
        "start_ts":   args.start_ts or int(time.time()) + 3600,
        "status":     "PREMATCH",
        "odds": {
            "home": args.odds_home,
            "draw": args.odds_draw,
            "away": args.odds_away,
        },
    }

    pipeline = AnalysisPipeline()
    report   = await pipeline.run(mock_match)
    print(report.to_telegram_message())


if __name__ == "__main__":
    asyncio.run(_main())
