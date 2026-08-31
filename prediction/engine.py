from dataclasses import dataclass, field
from typing import Optional
import math


@dataclass
class TeamStats:
    name: str
    conference: str
    wins: int
    losses: int
    points_per_game: float
    points_allowed_per_game: float
    yards_per_play: float
    yards_per_play_allowed: float
    turnover_margin: float
    third_down_pct: float
    red_zone_pct: float
    sacks_per_game: float
    penalty_yards_per_game: float
    strength_of_schedule: float
    elo_rating: float

    @property
    def win_pct(self) -> float:
        total = self.wins + self.losses
        return self.wins / total if total > 0 else 0.5


@dataclass
class MatchupEdge:
    category: str
    team_a_value: float
    team_b_value: float
    advantage: str
    magnitude: str


@dataclass
class PredictionResult:
    team_a: str
    team_b: str
    win_prob_a: float
    win_prob_b: float
    predicted_score_a: float
    predicted_score_b: float
    predicted_winner: str
    confidence: str
    power_rating_a: float
    power_rating_b: float
    matchup_edges: list
    home_team: Optional[str]
    summary: str


class PredictionEngine:
    WEIGHTS = {
        "elo": 0.25,
        "offense": 0.20,
        "defense": 0.20,
        "efficiency": 0.15,
        "schedule": 0.10,
        "record": 0.10,
    }

    HOME_FIELD_POINTS = 3.0
    HOME_FIELD_RATING_BOOST = 3.0

    STAT_RANGES = {
        "ppg": (10.0, 50.0),
        "papg": (10.0, 45.0),
        "ypp": (4.0, 7.5),
        "ypp_def": (4.0, 7.5),
        "to_margin": (-2.0, 2.0),
        "third_down": (0.25, 0.55),
        "red_zone": (0.70, 0.98),
        "sacks": (0.5, 4.0),
        "penalties": (30.0, 90.0),
        "sos": (0.30, 0.70),
        "elo": (1200.0, 1700.0),
        "win_pct": (0.0, 1.0),
    }

    def _normalize(self, value: float, low: float, high: float, invert: bool = False) -> float:
        if high == low:
            return 50.0
        score = (value - low) / (high - low) * 100.0
        score = max(0.0, min(100.0, score))
        return 100.0 - score if invert else score

    def _offense_score(self, team: TeamStats) -> float:
        ppg = self._normalize(team.points_per_game, *self.STAT_RANGES["ppg"])
        ypp = self._normalize(team.yards_per_play, *self.STAT_RANGES["ypp"])
        third = self._normalize(team.third_down_pct, *self.STAT_RANGES["third_down"])
        rz = self._normalize(team.red_zone_pct, *self.STAT_RANGES["red_zone"])
        return ppg * 0.40 + ypp * 0.30 + third * 0.15 + rz * 0.15

    def _defense_score(self, team: TeamStats) -> float:
        papg = self._normalize(team.points_allowed_per_game, *self.STAT_RANGES["papg"], invert=True)
        ypp = self._normalize(team.yards_per_play_allowed, *self.STAT_RANGES["ypp_def"], invert=True)
        sacks = self._normalize(team.sacks_per_game, *self.STAT_RANGES["sacks"])
        return papg * 0.45 + ypp * 0.35 + sacks * 0.20

    def _efficiency_score(self, team: TeamStats) -> float:
        to_margin = self._normalize(team.turnover_margin, *self.STAT_RANGES["to_margin"])
        penalties = self._normalize(team.penalty_yards_per_game, *self.STAT_RANGES["penalties"], invert=True)
        return to_margin * 0.60 + penalties * 0.40

    def calculate_power_rating(self, team: TeamStats) -> float:
        elo = self._normalize(team.elo_rating, *self.STAT_RANGES["elo"])
        offense = self._offense_score(team)
        defense = self._defense_score(team)
        efficiency = self._efficiency_score(team)
        sos = self._normalize(team.strength_of_schedule, *self.STAT_RANGES["sos"])
        record = self._normalize(team.win_pct, *self.STAT_RANGES["win_pct"])

        return (
            self.WEIGHTS["elo"] * elo
            + self.WEIGHTS["offense"] * offense
            + self.WEIGHTS["defense"] * defense
            + self.WEIGHTS["efficiency"] * efficiency
            + self.WEIGHTS["schedule"] * sos
            + self.WEIGHTS["record"] * record
        )

    def _analyze_matchups(self, a: TeamStats, b: TeamStats) -> list:
        comparisons = [
            ("Offense (PPG)", a.points_per_game, b.points_per_game, False),
            ("Defense (PPG Allowed)", a.points_allowed_per_game, b.points_allowed_per_game, True),
            ("Off. Efficiency (YPP)", a.yards_per_play, b.yards_per_play, False),
            ("Def. Efficiency (YPP Allowed)", a.yards_per_play_allowed, b.yards_per_play_allowed, True),
            ("Turnover Margin", a.turnover_margin, b.turnover_margin, False),
            ("Third Down %", a.third_down_pct * 100, b.third_down_pct * 100, False),
            ("Red Zone %", a.red_zone_pct * 100, b.red_zone_pct * 100, False),
            ("Pass Rush (Sacks/G)", a.sacks_per_game, b.sacks_per_game, False),
            ("Discipline (Pen. YPG)", a.penalty_yards_per_game, b.penalty_yards_per_game, True),
            ("Strength of Schedule", a.strength_of_schedule, b.strength_of_schedule, False),
        ]

        edges = []
        for category, val_a, val_b, lower_better in comparisons:
            diff = (val_b - val_a) if lower_better else (val_a - val_b)

            if abs(diff) < 0.01:
                edges.append(MatchupEdge(category, val_a, val_b, "Even", "even"))
                continue

            advantage = a.name if diff > 0 else b.name
            denom = max(abs(val_a), abs(val_b), 0.01)
            pct = abs(diff) / denom * 100

            if pct < 5:
                mag = "slight"
            elif pct < 15:
                mag = "moderate"
            elif pct < 30:
                mag = "significant"
            else:
                mag = "dominant"

            edges.append(MatchupEdge(category, val_a, val_b, advantage, mag))

        return edges

    def _predict_score(self, team: TeamStats, opponent: TeamStats, is_home: bool) -> float:
        base = (team.points_per_game + opponent.points_allowed_per_game) / 2.0
        base += team.turnover_margin * 2.0
        ypp_diff = team.yards_per_play - opponent.yards_per_play_allowed
        base += ypp_diff * 3.0
        if is_home:
            base += self.HOME_FIELD_POINTS / 2.0
        return max(3.0, round(base, 1))

    def predict(
        self,
        team_a: TeamStats,
        team_b: TeamStats,
        home_team: Optional[str] = None,
    ) -> PredictionResult:
        rating_a = self.calculate_power_rating(team_a)
        rating_b = self.calculate_power_rating(team_b)

        eff_a = rating_a + (self.HOME_FIELD_RATING_BOOST if home_team == team_a.name else 0)
        eff_b = rating_b + (self.HOME_FIELD_RATING_BOOST if home_team == team_b.name else 0)

        diff = eff_a - eff_b
        win_prob_a = 1.0 / (1.0 + math.exp(-diff / 10.0))
        win_prob_b = 1.0 - win_prob_a

        score_a = self._predict_score(team_a, team_b, home_team == team_a.name)
        score_b = self._predict_score(team_b, team_a, home_team == team_b.name)

        if win_prob_a >= win_prob_b:
            winner = team_a.name
            conf_val = win_prob_a
        else:
            winner = team_b.name
            conf_val = win_prob_b

        if conf_val > 0.85:
            confidence = "Very High"
        elif conf_val > 0.70:
            confidence = "High"
        elif conf_val > 0.60:
            confidence = "Moderate"
        else:
            confidence = "Low (Toss-up)"

        venue = "Neutral Site"
        if home_team == team_a.name:
            venue = f"at {team_a.name}"
        elif home_team == team_b.name:
            venue = f"at {team_b.name}"

        summary = (
            f"{winner} predicted to win ({venue}) "
            f"with {conf_val * 100:.1f}% probability.  "
            f"Predicted score: {team_a.name} {score_a:.0f} - "
            f"{team_b.name} {score_b:.0f}."
        )

        return PredictionResult(
            team_a=team_a.name,
            team_b=team_b.name,
            win_prob_a=win_prob_a,
            win_prob_b=win_prob_b,
            predicted_score_a=score_a,
            predicted_score_b=score_b,
            predicted_winner=winner,
            confidence=confidence,
            power_rating_a=rating_a,
            power_rating_b=rating_b,
            matchup_edges=self._analyze_matchups(team_a, team_b),
            home_team=home_team,
            summary=summary,
        )
