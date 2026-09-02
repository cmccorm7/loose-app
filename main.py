import os
os.environ.setdefault("KIVY_LOG_LEVEL", "warning")

from kivy.config import Config
Config.set("graphics", "width", "520")
Config.set("graphics", "height", "820")
Config.set("graphics", "resizable", "1")
Config.set("graphics", "minimum_width", "420")
Config.set("graphics", "minimum_height", "600")

from kivy.app import App
from kivy.uix.widget import Widget
from kivy.uix.label import Label
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.screenmanager import ScreenManager, Screen, SlideTransition
from kivy.graphics import Color, Line, Rectangle
from kivy.clock import Clock
from kivy.metrics import dp, sp
import math
import random

from prediction.ui import FootballPredictorScreen


class Hexagon:
    def __init__(self, x, y, size, color):
        self.original_x = x
        self.original_y = y
        self.x = x
        self.y = y
        self.size = size
        self.color = color
        self.angle = 0
        self.shake_offset_x = 0
        self.shake_offset_y = 0
        self.fall_velocity_x = 0
        self.fall_velocity_y = 0
        self.falling = False
        self.shaking = False

    def get_points(self):
        points = []
        for i in range(6):
            angle = math.pi / 3 * i + self.angle
            point_x = self.x + self.size * math.cos(angle) + self.shake_offset_x
            point_y = self.y + self.size * math.sin(angle) + self.shake_offset_y
            points.extend([point_x, point_y])
        return points

    def move_to_center(self, target_x, target_y, speed):
        dx = target_x - self.x
        dy = target_y - self.y
        distance = math.sqrt(dx * dx + dy * dy)
        if distance > speed:
            self.x += (dx / distance) * speed
            self.y += (dy / distance) * speed
            return False
        self.x = target_x
        self.y = target_y
        return True

    def shake(self, intensity):
        self.shake_offset_x = random.uniform(-intensity, intensity)
        self.shake_offset_y = random.uniform(-intensity, intensity)

    def start_falling(self):
        self.falling = True
        self.fall_velocity_x = random.uniform(-5, 5)
        self.fall_velocity_y = random.uniform(-2, 2)

    def update_fall(self):
        if self.falling:
            self.x += self.fall_velocity_x
            self.y += self.fall_velocity_y
            self.fall_velocity_y += 0.3
            self.angle += 0.1


class HexagonWidget(Widget):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.state = "gathering"
        self.timer = 0
        self.colors = [(1, 1, 0), (0.5, 0, 0.5), (0, 0, 0)]
        self.hexagons = []
        self.bind(size=self.on_size_change)
        Clock.schedule_interval(self.update_animation, 1.0 / 60.0)
        self.bind(on_touch_down=self.restart_animation)

    def on_size_change(self, *args):
        if not self.hexagons and self.width > 0 and self.height > 0:
            self.init_hexagons()

    def init_hexagons(self):
        hex_size = min(40, self.width // 15)
        for color in self.colors * 3:
            start_x = random.randint(int(hex_size), int(self.width - hex_size))
            start_y = random.randint(int(hex_size), int(self.height - hex_size))
            self.hexagons.append(Hexagon(start_x, start_y, hex_size, color))

    def restart_animation(self, widget, touch):
        if not self.hexagons:
            return
        self.state = "gathering"
        self.timer = 0
        for hexagon in self.hexagons:
            hexagon.x = random.randint(int(hexagon.size), int(self.width - hexagon.size))
            hexagon.y = random.randint(int(hexagon.size), int(self.height - hexagon.size))
            hexagon.shake_offset_x = 0
            hexagon.shake_offset_y = 0
            hexagon.fall_velocity_x = 0
            hexagon.fall_velocity_y = 0
            hexagon.falling = False
            hexagon.shaking = False
            hexagon.angle = 0

    def update_animation(self, dt):
        if not self.hexagons:
            return
        self.timer += 1

        if self.state == "gathering":
            center_x = self.width / 2
            center_y = self.height / 2
            all_reached = True
            for i, hexagon in enumerate(self.hexagons):
                angle = (2 * math.pi / len(self.hexagons)) * i
                radius = min(self.width, self.height) // 5
                target_x = center_x + radius * math.cos(angle)
                target_y = center_y + radius * math.sin(angle)
                if not hexagon.move_to_center(target_x, target_y, 3):
                    all_reached = False
            if all_reached and self.timer > 60:
                self.state = "shaking"
                self.timer = 0
                for hexagon in self.hexagons:
                    hexagon.shaking = True

        elif self.state == "shaking":
            shake_intensity = min(10, self.timer * 0.2)
            for hexagon in self.hexagons:
                hexagon.shake(shake_intensity)
            if self.timer > 120:
                self.state = "falling"
                self.timer = 0
                for hexagon in self.hexagons:
                    hexagon.start_falling()
                    hexagon.shaking = False

        elif self.state == "falling":
            for hexagon in self.hexagons:
                hexagon.update_fall()
            if self.timer > 300:
                self.restart_animation(None, None)

        self.canvas.clear()
        self.draw_hexagons()

    def draw_hexagons(self):
        with self.canvas:
            for hexagon in self.hexagons:
                Color(*hexagon.color, 0.8)
                points = hexagon.get_points()
                if len(points) >= 12:
                    points.extend([points[0], points[1]])
                Line(points=points, width=2)


class HexagonScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        layout = BoxLayout(orientation="vertical")

        with layout.canvas.before:
            Color(0.10, 0.10, 0.14, 1)
            self._bg = Rectangle(size=layout.size, pos=layout.pos)
        layout.bind(
            size=lambda w, s: setattr(self._bg, "size", s),
            pos=lambda w, p: setattr(self._bg, "pos", p),
        )

        title = Label(
            text="Hexagon Animation",
            size_hint_y=0.08,
            font_size=sp(20),
            color=(1, 1, 1, 1),
        )
        layout.add_widget(title)
        layout.add_widget(HexagonWidget())

        nav_btn = Button(
            text="Go to Football Predictor",
            size_hint_y=None,
            height=dp(44),
            background_color=(0.18, 0.55, 0.34, 1),
            color=(1, 1, 1, 1),
            font_size=sp(14),
        )
        nav_btn.bind(on_press=self._go_predictor)
        layout.add_widget(nav_btn)

        instructions = Label(
            text="Tap screen to restart animation",
            size_hint_y=0.06,
            font_size=sp(13),
            color=(0.6, 0.6, 0.6, 1),
        )
        layout.add_widget(instructions)
        self.add_widget(layout)

    def _go_predictor(self, *_):
        self.manager.transition = SlideTransition(direction="left")
        self.manager.current = "predictor"


class PredictorScreenWrapper(FootballPredictorScreen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        nav_btn = Button(
            text="Hexagon Animation",
            size_hint_y=None,
            height=dp(36),
            background_color=(0.3, 0.3, 0.38, 1),
            color=(1, 1, 1, 1),
            font_size=sp(13),
        )
        nav_btn.bind(on_press=self._go_hexagon)
        root_layout = self.children[0]
        root_layout.add_widget(nav_btn)

    def _go_hexagon(self, *_):
        self.manager.transition = SlideTransition(direction="right")
        self.manager.current = "hexagon"


class LooseApp(App):
    title = "College Football Predictor"

    def build(self):
        self.icon = ""
        sm = ScreenManager()
        sm.add_widget(PredictorScreenWrapper(name="predictor"))
        sm.add_widget(HexagonScreen(name="hexagon"))
        return sm


if __name__ == "__main__":
    LooseApp().run()
