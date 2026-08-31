import pytest
from prediction.engine import PredictionEngine, TeamStats, MatchupEdge, PredictionResult
from prediction.teams_data import get_team, get_all_team_names, get_conferences, get_teams_by_conference


class TestTeamsData:
    def test_all_teams_loaded(self):
        names = get_all_team_names()
        assert len(names) >= 120

    def test_teams_sorted(self):
        names = get_all_team_names()
        assert names == sorted(names)

    def test_get_known_team(self):
        team = get_team("Georgia")
        assert team.name == "Georgia"
        assert team.conference == "SEC"
        assert team.wins > 0
        assert team.elo_rating > 1500

    def test_get_unknown_team_raises(self):
        with pytest.raises(KeyError):
            get_team("Nonexistent University")

    def test_conferences_exist(self):
        confs = get_conferences()
        assert "SEC" in confs
        assert "Big Ten" in confs
        assert "ACC" in confs
        assert "Big 12" in confs

    def test_teams_by_conference(self):
        sec = get_teams_by_conference("SEC")
        assert "Georgia" in sec
        assert "Alabama" in sec
        assert len(sec) >= 14

    def test_all_teams_have_valid_stats(self):
        for name in get_all_team_names():
            team = get_team(name)
            assert team.wins >= 0
            assert team.losses >= 0
            assert team.points_per_game > 0
            assert team.points_allowed_per_game > 0
            assert 3.0 <= team.yards_per_play <= 8.0
            assert 3.0 <= team.yards_per_play_allowed <= 8.0
            assert 0.0 < team.third_down_pct < 1.0
            assert 0.0 < team.red_zone_pct < 1.0
            assert team.sacks_per_game >= 0
            assert team.elo_rating > 1000

    def test_win_pct(self):
        team = get_team("Ohio State")
        assert 0.0 <= team.win_pct <= 1.0
        assert team.win_pct == team.wins / (team.wins + team.losses)


class TestPredictionEngine:
    @pytest.fixture
    def engine(self):
        return PredictionEngine()

    @pytest.fixture
    def strong_team(self):
        return TeamStats(
            name="Strong", conference="Test", wins=12, losses=1,
            points_per_game=40.0, points_allowed_per_game=14.0,
            yards_per_play=7.0, yards_per_play_allowed=4.2,
            turnover_margin=1.0, third_down_pct=0.50,
            red_zone_pct=0.94, sacks_per_game=3.5,
            penalty_yards_per_game=40.0, strength_of_schedule=0.65,
            elo_rating=1680.0,
        )

    @pytest.fixture
    def weak_team(self):
        return TeamStats(
            name="Weak", conference="Test", wins=2, losses=10,
            points_per_game=15.0, points_allowed_per_game=35.0,
            yards_per_play=4.2, yards_per_play_allowed=6.5,
            turnover_margin=-1.0, third_down_pct=0.30,
            red_zone_pct=0.72, sacks_per_game=1.0,
            penalty_yards_per_game=70.0, strength_of_schedule=0.35,
            elo_rating=1220.0,
        )

    def test_strong_beats_weak(self, engine, strong_team, weak_team):
        result = engine.predict(strong_team, weak_team)
        assert result.predicted_winner == "Strong"
        assert result.win_prob_a > 0.90

    def test_probabilities_sum_to_one(self, engine, strong_team, weak_team):
        result = engine.predict(strong_team, weak_team)
        assert abs(result.win_prob_a + result.win_prob_b - 1.0) < 1e-9

    def test_neutral_site(self, engine, strong_team, weak_team):
        result = engine.predict(strong_team, weak_team)
        assert result.home_team is None

    def test_home_field_advantage(self, engine):
        team_a = get_team("Georgia")
        team_b = get_team("Alabama")
        neutral = engine.predict(team_a, team_b)
        home_a = engine.predict(team_a, team_b, home_team="Georgia")
        home_b = engine.predict(team_a, team_b, home_team="Alabama")
        assert home_a.win_prob_a > neutral.win_prob_a
        assert home_b.win_prob_b > neutral.win_prob_b

    def test_symmetric_prediction(self, engine):
        a = get_team("Ohio State")
        b = get_team("Oregon")
        r1 = engine.predict(a, b)
        r2 = engine.predict(b, a)
        assert abs(r1.win_prob_a - r2.win_prob_b) < 1e-9

    def test_predicted_scores_positive(self, engine, strong_team, weak_team):
        result = engine.predict(strong_team, weak_team)
        assert result.predicted_score_a >= 3.0
        assert result.predicted_score_b >= 3.0

    def test_matchup_edges_populated(self, engine, strong_team, weak_team):
        result = engine.predict(strong_team, weak_team)
        assert len(result.matchup_edges) == 10
        for edge in result.matchup_edges:
            assert edge.category
            assert edge.magnitude in ("even", "slight", "moderate", "significant", "dominant")

    def test_confidence_levels(self, engine):
        # Big mismatch
        big = engine.predict(get_team("Ohio State"), get_team("Kent State"))
        assert big.confidence == "Very High"

        # Close matchup
        close = engine.predict(get_team("Ohio State"), get_team("Oregon"))
        assert close.confidence in ("Moderate", "Low (Toss-up)")

    def test_power_rating_range(self, engine):
        for name in get_all_team_names():
            team = get_team(name)
            rating = engine.calculate_power_rating(team)
            assert 0 <= rating <= 100

    def test_summary_contains_winner(self, engine):
        result = engine.predict(get_team("Texas"), get_team("Vanderbilt"))
        assert result.predicted_winner in result.summary

    def test_result_fields(self, engine, strong_team, weak_team):
        result = engine.predict(strong_team, weak_team)
        assert isinstance(result, PredictionResult)
        assert result.team_a == "Strong"
        assert result.team_b == "Weak"
        assert isinstance(result.matchup_edges, list)
