"""
Build script for College Football Predictor desktop app.

Usage:
    python build.py

Produces a standalone executable in dist/CollegeFootballPredictor/
Requirements:
    pip install pyinstaller kivy
"""

import subprocess
import sys
import os
import platform


def main():
    app_name = "CollegeFootballPredictor"
    script = "main.py"
    here = os.path.dirname(os.path.abspath(__file__))

    import kivy
    kivy_path = os.path.dirname(kivy.__file__)
    kivy_data = os.path.join(kivy_path, "data")

    sep = ";" if platform.system() == "Windows" else ":"

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--name", app_name,
        "--windowed",
        "--noconfirm",
        "--clean",
        "--add-data", f"{kivy_data}{sep}kivy/data",
        "--add-data", f"{os.path.join(here, 'prediction')}{sep}prediction",
        "--hidden-import", "kivy.core.window.window_sdl2",
        "--hidden-import", "kivy.core.text.text_sdl2",
        "--hidden-import", "kivy.core.image.img_tex",
        "--hidden-import", "kivy.core.image.img_dds",
        "--hidden-import", "kivy.core.image.img_sdl2",
        "--hidden-import", "kivy.core.clipboard.clipboard_sdl2",
        "--hidden-import", "kivy.core.clipboard._clipboard_ext",
        "--hidden-import", "kivy.core.audio.audio_sdl2",
        "--hidden-import", "kivy.core.video",
        "--hidden-import", "kivy.core.spelling",
        "--hidden-import", "kivy.core.camera",
        "--hidden-import", "kivy.graphics.cgl_backend.cgl_glew",
        "--hidden-import", "kivy.graphics.cgl_backend.cgl_gl",
        "--hidden-import", "kivy.graphics.cgl_backend.cgl_sdl2",
        "--hidden-import", "kivy.uix.screenmanager",
        "--hidden-import", "kivy.uix.scrollview",
        "--hidden-import", "kivy.uix.spinner",
        "--hidden-import", "kivy.uix.gridlayout",
        "--hidden-import", "prediction",
        "--hidden-import", "prediction.engine",
        "--hidden-import", "prediction.teams_data",
        "--hidden-import", "prediction.ui",
        "--collect-data", "kivy",
        script,
    ]

    print(f"Building {app_name} for {platform.system()}...")
    print()

    result = subprocess.run(cmd, cwd=here)

    if result.returncode == 0:
        dist_dir = os.path.join(here, "dist", app_name)
        print()
        print("Build successful!")
        print(f"App location: {dist_dir}")
        if platform.system() == "Windows":
            print(f"Run: {os.path.join(dist_dir, app_name + '.exe')}")
        elif platform.system() == "Darwin":
            print(f"Run: open {os.path.join(here, 'dist', app_name + '.app')}")
        else:
            print(f"Run: {os.path.join(dist_dir, app_name)}")
    else:
        print(f"Build failed with exit code {result.returncode}")
        sys.exit(1)


if __name__ == "__main__":
    main()
