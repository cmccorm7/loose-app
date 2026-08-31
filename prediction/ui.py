from kivy.uix.screenmanager import Screen
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.spinner import Spinner
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.widget import Widget
from kivy.graphics import Color, Rectangle, RoundedRectangle, Line
from kivy.metrics import dp, sp

from prediction.engine import PredictionEngine
from prediction.teams_data import get_team, get_all_team_names, get_conferences, get_teams_by_conference


class ColoredBox(Widget):
    def __init__(self, color=(0.15, 0.15, 0.2, 1), **kwargs):
        super().__init__(**kwargs)
        self._color = color
        with self.canvas.before:
            Color(*self._color)
            self._rect = Rectangle(pos=self.pos, size=self.size)
        self.bind(pos=self._update, size=self._update)

    def _update(self, *_):
        self._rect.pos = self.pos
        self._rect.size = self.size


class ProbabilityBar(Widget):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.size_hint_y = None
        self.height = dp(32)
        self._prob_a = 0.5
        self._name_a = ""
        self._name_b = ""
        self.bind(pos=self._draw, size=self._draw)

    def set_data(self, name_a, name_b, prob_a):
        self._name_a = name_a
        self._name_b = name_b
        self._prob_a = prob_a
        self._draw()

    def _draw(self, *_):
        self.canvas.clear()
        with self.canvas:
            Color(0.2, 0.6, 0.3, 1)
            w_a = self.width * self._prob_a
            RoundedRectangle(pos=self.pos, size=(w_a, self.height), radius=[dp(4), 0, 0, dp(4)])

            Color(0.7, 0.2, 0.2, 1)
            RoundedRectangle(pos=(self.x + w_a, self.y), size=(self.width - w_a, self.height), radius=[0, dp(4), dp(4), 0])


class FootballPredictorScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.engine = PredictionEngine()

        root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))

        with root.canvas.before:
            Color(0.10, 0.10, 0.14, 1)
            self._bg = Rectangle(size=root.size, pos=root.pos)
        root.bind(size=self._update_bg, pos=self._update_bg)

        # ── Title ────────────────────────────────────────────────────────
        title = Label(
            text="College Football Predictor",
            font_size=sp(22),
            bold=True,
            color=(1, 1, 1, 1),
            size_hint_y=None,
            height=dp(40),
        )
        root.add_widget(title)

        # ── Conference filter ────────────────────────────────────────────
        conf_row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
        conf_row.add_widget(Label(text="Conference:", size_hint_x=0.35, color=(0.8, 0.8, 0.8, 1), font_size=sp(14)))
        conferences = ["All"] + get_conferences()
        self.conf_spinner = Spinner(
            text="All",
            values=conferences,
            size_hint_x=0.65,
            background_color=(0.25, 0.25, 0.32, 1),
            color=(1, 1, 1, 1),
            font_size=sp(14),
        )
        self.conf_spinner.bind(text=self._on_conference_change)
        conf_row.add_widget(self.conf_spinner)
        root.add_widget(conf_row)

        # ── Team selection ───────────────────────────────────────────────
        all_teams = get_all_team_names()

        sel_box = BoxLayout(orientation="vertical", size_hint_y=None, height=dp(130), spacing=dp(6))

        row_a = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
        row_a.add_widget(Label(text="Team A:", size_hint_x=0.25, color=(0.8, 0.8, 0.8, 1), font_size=sp(14)))
        self.team_a_spinner = Spinner(
            text=all_teams[0] if all_teams else "",
            values=all_teams,
            size_hint_x=0.75,
            background_color=(0.20, 0.35, 0.55, 1),
            color=(1, 1, 1, 1),
            font_size=sp(14),
        )
        row_a.add_widget(self.team_a_spinner)
        sel_box.add_widget(row_a)

        sel_box.add_widget(Label(text="VS", font_size=sp(18), bold=True, color=(0.9, 0.75, 0.2, 1), size_hint_y=None, height=dp(30)))

        row_b = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
        row_b.add_widget(Label(text="Team B:", size_hint_x=0.25, color=(0.8, 0.8, 0.8, 1), font_size=sp(14)))
        self.team_b_spinner = Spinner(
            text=all_teams[1] if len(all_teams) > 1 else "",
            values=all_teams,
            size_hint_x=0.75,
            background_color=(0.55, 0.20, 0.20, 1),
            color=(1, 1, 1, 1),
            font_size=sp(14),
        )
        row_b.add_widget(self.team_b_spinner)
        sel_box.add_widget(row_b)

        root.add_widget(sel_box)

        # ── Venue ────────────────────────────────────────────────────────
        venue_row = BoxLayout(size_hint_y=None, height=dp(40), spacing=dp(8))
        venue_row.add_widget(Label(text="Venue:", size_hint_x=0.25, color=(0.8, 0.8, 0.8, 1), font_size=sp(14)))
        self.venue_spinner = Spinner(
            text="Neutral Site",
            values=["Neutral Site", "Team A Home", "Team B Home"],
            size_hint_x=0.75,
            background_color=(0.25, 0.25, 0.32, 1),
            color=(1, 1, 1, 1),
            font_size=sp(14),
        )
        venue_row.add_widget(self.venue_spinner)
        root.add_widget(venue_row)

        # ── Predict button ───────────────────────────────────────────────
        predict_btn = Button(
            text="PREDICT WINNER",
            size_hint_y=None,
            height=dp(48),
            background_color=(0.18, 0.55, 0.34, 1),
            color=(1, 1, 1, 1),
            font_size=sp(16),
            bold=True,
        )
        predict_btn.bind(on_press=self._run_prediction)
        root.add_widget(predict_btn)

        # ── Results area (scrollable) ────────────────────────────────────
        scroll = ScrollView(size_hint_y=1)
        self.results_layout = BoxLayout(
            orientation="vertical",
            size_hint_y=None,
            padding=[dp(8), dp(8)],
            spacing=dp(6),
        )
        self.results_layout.bind(minimum_height=self.results_layout.setter("height"))
        scroll.add_widget(self.results_layout)
        root.add_widget(scroll)

        self._show_placeholder()
        self.add_widget(root)

    def _update_bg(self, widget, *_):
        self._bg.size = widget.size
        self._bg.pos = widget.pos

    def _on_conference_change(self, spinner, text):
        if text == "All":
            teams = get_all_team_names()
        else:
            teams = get_teams_by_conference(text)
        self.team_a_spinner.values = teams
        self.team_b_spinner.values = teams
        if teams:
            if self.team_a_spinner.text not in teams:
                self.team_a_spinner.text = teams[0]
            if self.team_b_spinner.text not in teams:
                self.team_b_spinner.text = teams[-1] if len(teams) > 1 else teams[0]

    def _show_placeholder(self):
        self.results_layout.clear_widgets()
        self.results_layout.add_widget(
            Label(
                text="Select two teams and tap PREDICT",
                color=(0.5, 0.5, 0.5, 1),
                font_size=sp(14),
                size_hint_y=None,
                height=dp(60),
            )
        )

    def _add_section_header(self, text):
        lbl = Label(
            text=text,
            font_size=sp(16),
            bold=True,
            color=(0.9, 0.75, 0.2, 1),
            size_hint_y=None,
            height=dp(30),
            halign="left",
            valign="middle",
        )
        lbl.bind(size=lbl.setter("text_size"))
        self.results_layout.add_widget(lbl)

    def _add_info_line(self, text, color=(0.85, 0.85, 0.85, 1), font_size=14):
        lbl = Label(
            text=text,
            font_size=sp(font_size),
            color=color,
            size_hint_y=None,
            height=dp(24),
            halign="left",
            valign="middle",
            markup=True,
        )
        lbl.bind(size=lbl.setter("text_size"))
        self.results_layout.add_widget(lbl)

    def _add_separator(self):
        sep = Widget(size_hint_y=None, height=dp(1))
        with sep.canvas:
            Color(0.3, 0.3, 0.35, 1)
            sep._line = Rectangle(size=(sep.width, dp(1)), pos=sep.pos)
        sep.bind(
            size=lambda w, s: setattr(w._line, "size", (s[0], dp(1))),
            pos=lambda w, p: setattr(w._line, "pos", p),
        )
        self.results_layout.add_widget(sep)

    def _run_prediction(self, *_):
        name_a = self.team_a_spinner.text
        name_b = self.team_b_spinner.text

        if not name_a or not name_b:
            self._show_error("Please select both teams.")
            return
        if name_a == name_b:
            self._show_error("Please select two different teams.")
            return

        try:
            team_a = get_team(name_a)
            team_b = get_team(name_b)
        except KeyError as e:
            self._show_error(str(e))
            return

        venue = self.venue_spinner.text
        home_team = None
        if venue == "Team A Home":
            home_team = name_a
        elif venue == "Team B Home":
            home_team = name_b

        result = self.engine.predict(team_a, team_b, home_team=home_team)
        self._display_result(result, team_a, team_b)

    def _show_error(self, msg):
        self.results_layout.clear_widgets()
        self.results_layout.add_widget(
            Label(text=msg, color=(1, 0.3, 0.3, 1), font_size=sp(14), size_hint_y=None, height=dp(40))
        )

    def _display_result(self, result, team_a, team_b):
        self.results_layout.clear_widgets()

        # ── Winner & Probability ─────────────────────────────────────
        self._add_section_header("PREDICTION")
        winner_color = (0.3, 0.9, 0.4, 1) if result.predicted_winner == result.team_a else (0.9, 0.4, 0.3, 1)
        self._add_info_line(
            f"Winner: [b]{result.predicted_winner}[/b]  ({result.confidence} confidence)",
            color=winner_color,
            font_size=16,
        )

        bar = ProbabilityBar(size_hint_y=None, height=dp(28))
        bar.set_data(result.team_a, result.team_b, result.win_prob_a)
        self.results_layout.add_widget(bar)

        prob_row = BoxLayout(size_hint_y=None, height=dp(22), spacing=dp(4))
        prob_row.add_widget(Label(
            text=f"{result.team_a}: {result.win_prob_a * 100:.1f}%",
            font_size=sp(12), color=(0.3, 0.8, 0.4, 1), halign="left",
        ))
        prob_row.add_widget(Label(
            text=f"{result.team_b}: {result.win_prob_b * 100:.1f}%",
            font_size=sp(12), color=(0.8, 0.3, 0.3, 1), halign="right",
        ))
        self.results_layout.add_widget(prob_row)

        self._add_separator()

        # ── Predicted Score ──────────────────────────────────────────
        self._add_section_header("PREDICTED SCORE")
        venue_str = "Neutral Site"
        if result.home_team:
            venue_str = f"at {result.home_team}"
        self._add_info_line(
            f"  {result.team_a}  [b]{result.predicted_score_a:.0f}[/b]  -  "
            f"[b]{result.predicted_score_b:.0f}[/b]  {result.team_b}",
            font_size=16,
        )
        self._add_info_line(f"  Venue: {venue_str}", color=(0.6, 0.6, 0.6, 1), font_size=12)

        self._add_separator()

        # ── Power Ratings ────────────────────────────────────────────
        self._add_section_header("POWER RATINGS")
        self._add_info_line(
            f"  {result.team_a}: [b]{result.power_rating_a:.1f}[/b]",
            color=(0.5, 0.7, 1, 1),
        )
        self._add_info_line(
            f"  {result.team_b}: [b]{result.power_rating_b:.1f}[/b]",
            color=(1, 0.5, 0.5, 1),
        )

        self._add_separator()

        # ── Team Profiles ────────────────────────────────────────────
        self._add_section_header("TEAM PROFILES")
        for team, label_color in [(team_a, (0.5, 0.7, 1, 1)), (team_b, (1, 0.5, 0.5, 1))]:
            self._add_info_line(
                f"  [b]{team.name}[/b] ({team.conference})  {team.wins}-{team.losses}",
                color=label_color,
            )

        self._add_separator()

        # ── Matchup Edges ────────────────────────────────────────────
        self._add_section_header("KEY MATCHUP ADVANTAGES")
        for edge in result.matchup_edges:
            if edge.magnitude == "even":
                color = (0.6, 0.6, 0.6, 1)
                adv_text = "Even"
            elif edge.advantage == result.team_a:
                color = (0.4, 0.8, 0.5, 1)
                adv_text = f"{edge.advantage} ({edge.magnitude})"
            else:
                color = (0.9, 0.45, 0.35, 1)
                adv_text = f"{edge.advantage} ({edge.magnitude})"

            self._add_info_line(f"  {edge.category}", color=(0.75, 0.75, 0.75, 1), font_size=13)
            val_a_str = f"{edge.team_a_value:.1f}"
            val_b_str = f"{edge.team_b_value:.1f}"
            self._add_info_line(
                f"    {result.team_a} {val_a_str}  vs  {result.team_b} {val_b_str}  ->  {adv_text}",
                color=color,
                font_size=12,
            )

        self._add_separator()

        # ── Summary ─────────────────────────────────────────────────
        self._add_section_header("SUMMARY")
        summary_lbl = Label(
            text=result.summary,
            font_size=sp(13),
            color=(0.85, 0.85, 0.85, 1),
            size_hint_y=None,
            halign="left",
            valign="top",
            markup=False,
        )
        summary_lbl.bind(size=summary_lbl.setter("text_size"))
        summary_lbl.bind(texture_size=lambda w, ts: setattr(w, "height", ts[1] + dp(16)))
        self.results_layout.add_widget(summary_lbl)

        # ── Algorithm Note ───────────────────────────────────────────
        self.results_layout.add_widget(Widget(size_hint_y=None, height=dp(8)))
        note = Label(
            text=(
                "Algorithm: Composite power rating using Elo (25%), "
                "Offense (20%), Defense (20%), Efficiency (15%), "
                "SOS (10%), Record (10%). Win probability via "
                "logistic function. Home field ~ +3 pts."
            ),
            font_size=sp(10),
            color=(0.45, 0.45, 0.5, 1),
            size_hint_y=None,
            halign="left",
            valign="top",
        )
        note.bind(size=note.setter("text_size"))
        note.bind(texture_size=lambda w, ts: setattr(w, "height", ts[1] + dp(12)))
        self.results_layout.add_widget(note)
