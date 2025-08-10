from kivy.app import App
from kivy.uix.widget import Widget
from kivy.uix.label import Label
from kivy.uix.boxlayout import BoxLayout
from kivy.graphics import Color, Line, PushMatrix, PopMatrix, Rotate
from kivy.clock import Clock
import math
import random

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
        """Calculate hexagon vertices"""
        points = []
        for i in range(6):
            angle = math.pi / 3 * i + self.angle
            point_x = self.x + self.size * math.cos(angle) + self.shake_offset_x
            point_y = self.y + self.size * math.sin(angle) + self.shake_offset_y
            points.extend([point_x, point_y])
        return points
    
    def move_to_center(self, target_x, target_y, speed):
        """Move hexagon towards center position"""
        dx = target_x - self.x
        dy = target_y - self.y
        distance = math.sqrt(dx*dx + dy*dy)
        
        if distance > speed:
            self.x += (dx / distance) * speed
            self.y += (dy / distance) * speed
            return False
        else:
            self.x = target_x
            self.y = target_y
            return True
    
    def shake(self, intensity):
        """Add shaking effect"""
        self.shake_offset_x = random.uniform(-intensity, intensity)
        self.shake_offset_y = random.uniform(-intensity, intensity)
    
    def start_falling(self):
        """Initialize falling animation"""
        self.falling = True
        self.fall_velocity_x = random.uniform(-5, 5)
        self.fall_velocity_y = random.uniform(-2, 2)
        
    def update_fall(self):
        """Update falling animation"""
        if self.falling:
            self.x += self.fall_velocity_x
            self.y += self.fall_velocity_y
            self.fall_velocity_y += 0.3  # Gravity
            self.angle += 0.1  # Rotation while falling

class HexagonWidget(Widget):
    def __init__(self, **kwargs):
        super(HexagonWidget, self).__init__(**kwargs)
        
        # Animation states
        self.state = "gathering"  # gathering, shaking, falling
        self.timer = 0
        
        # Colors (RGB normalized to 0-1 for Kivy)
        self.colors = [
            (1, 1, 0),      # Yellow
            (0.5, 0, 0.5),  # Purple
            (0, 0, 0)       # Black
        ]
        
        # Create hexagons
        self.hexagons = []
        hex_size = 40
        
        # Wait for widget to be sized before creating hexagons
        self.bind(size=self.on_size_change)
        
        # Start the animation
        Clock.schedule_interval(self.update_animation, 1.0/60.0)  # 60 FPS
        
        # Bind to touch events for restart
        self.bind(on_touch_down=self.restart_animation)
    
    def on_size_change(self, *args):
        """Initialize hexagons when widget is sized"""
        if not self.hexagons and self.width > 0 and self.height > 0:
            self.init_hexagons()
    
    def init_hexagons(self):
        """Initialize hexagons with proper screen dimensions"""
        hex_size = min(40, self.width // 15)  # Adaptive size
        
        # Create 9 hexagons (3 of each color) starting from random positions
        for i, color in enumerate(self.colors * 3):
            start_x = random.randint(int(hex_size), int(self.width - hex_size))
            start_y = random.randint(int(hex_size), int(self.height - hex_size))
            hexagon = Hexagon(start_x, start_y, hex_size, color)
            self.hexagons.append(hexagon)
    
    def restart_animation(self, widget, touch):
        """Restart animation on touch"""
        if not self.hexagons:
            return
            
        self.state = "gathering"
        self.timer = 0
        
        # Reset hexagons
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
        """Update animation logic"""
        if not self.hexagons:
            return
            
        self.timer += 1
        
        if self.state == "gathering":
            # Move hexagons to center in formation
            center_x = self.width / 2
            center_y = self.height / 2
            
            all_reached = True
            for i, hexagon in enumerate(self.hexagons):
                # Arrange in a circular formation
                angle = (2 * math.pi / len(self.hexagons)) * i
                radius = min(self.width, self.height) // 5
                target_x = center_x + radius * math.cos(angle)
                target_y = center_y + radius * math.sin(angle)
                
                reached = hexagon.move_to_center(target_x, target_y, 3)
                if not reached:
                    all_reached = False
            
            # Switch to shaking when all hexagons reach center
            if all_reached and self.timer > 60:  # Wait a bit after gathering
                self.state = "shaking"
                self.timer = 0
                for hexagon in self.hexagons:
                    hexagon.shaking = True
        
        elif self.state == "shaking":
            # Shake hexagons
            shake_intensity = min(10, self.timer * 0.2)  # Gradually increase shake
            
            for hexagon in self.hexagons:
                hexagon.shake(shake_intensity)
            
            # Switch to falling after shaking for a while
            if self.timer > 120:  # Shake for 2 seconds
                self.state = "falling"
                self.timer = 0
                for hexagon in self.hexagons:
                    hexagon.start_falling()
                    hexagon.shaking = False
        
        elif self.state == "falling":
            # Update falling animation
            for hexagon in self.hexagons:
                hexagon.update_fall()
            
            # Reset animation after falling for a while
            if self.timer > 300:  # Fall for 5 seconds
                self.restart_animation(None, None)
        
        # Redraw
        self.canvas.clear()
        self.draw_hexagons()
    
    def draw_hexagons(self):
        """Draw all hexagons"""
        with self.canvas:
            for hexagon in self.hexagons:
                # Set color with some transparency
                Color(*hexagon.color, 0.8)
                
                # Get hexagon points
                points = hexagon.get_points()
                
                # Close the hexagon by adding the first point at the end
                if len(points) >= 12:  # 6 points * 2 coordinates
                    points.extend([points[0], points[1]])
                    
                # Draw hexagon outline
                Line(points=points, width=2)

class MainApp(BoxLayout):
    def __init__(self, **kwargs):
        super(MainApp, self).__init__(**kwargs)
        self.orientation = 'vertical'
        
        # Title label
        title = Label(
            text='Hexagon Intro Animation',
            size_hint_y=0.1,
            font_size='20sp',
            color=(1, 1, 1, 1)
        )
        self.add_widget(title)
        
        # Hexagon animation widget
        self.hex_widget = HexagonWidget()
        self.add_widget(self.hex_widget)
        
        # Instructions
        instructions = Label(
            text='Tap screen to restart animation',
            size_hint_y=0.1,
            font_size='16sp',
            color=(0.8, 0.8, 0.8, 1)
        )
        self.add_widget(instructions)

class HexagonApp(App):
    def build(self):
        return MainApp()

if __name__ == '__main__':
    HexagonApp().run()
