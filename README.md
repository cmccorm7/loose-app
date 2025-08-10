# Hexagon Animation Android App

## How to Build for Android

### Prerequisites (Windows/WSL):

1. **Install WSL (Windows Subsystem for Linux)** if on Windows:
   ```powershell
   wsl --install
   ```

2. **In WSL, install required dependencies**:
   ```bash
   sudo apt update
   sudo apt install -y git zip unzip openjdk-17-jdk python3-pip autoconf libtool pkg-config zlib1g-dev libncurses5-dev libncursesw5-dev libtinfo5 cmake libffi-dev libssl-dev
   ```

3. **Install Android SDK/NDK**:
   ```bash
   # Download and setup Android SDK
   wget https://dl.google.com/android/repository/commandlinetools-linux-9477386_latest.zip
   unzip commandlinetools-linux-9477386_latest.zip
   mkdir -p ~/android-sdk/cmdline-tools
   mv cmdline-tools ~/android-sdk/cmdline-tools/latest
   
   # Add to ~/.bashrc
   echo 'export ANDROID_SDK_ROOT=$HOME/android-sdk' >> ~/.bashrc
   echo 'export PATH=$PATH:$ANDROID_SDK_ROOT/cmdline-tools/latest/bin' >> ~/.bashrc
   source ~/.bashrc
   
   # Install SDK components
   yes | sdkmanager --licenses
   sdkmanager "platform-tools" "platforms;android-33" "build-tools;33.0.2" "ndk;25.2.9519653"
   ```

### Building the APK:

1. **Copy project to WSL**:
   ```bash
   cp -r /mnt/c/Users/analo/OneDrive/Documents/Loose_APP ~/hexagon-app
   cd ~/hexagon-app
   ```

2. **Install Python dependencies**:
   ```bash
   pip3 install buildozer cython
   ```

3. **Initialize and build**:
   ```bash
   buildozer init  # Only needed first time
   buildozer android debug
   ```

4. **Install APK**:
   ```bash
   # Connect Android device with USB debugging enabled
   adb install bin/*.apk
   ```

### Alternative: Using GitHub Actions (Recommended)

Create a `.github/workflows/build-android.yml` file to automatically build APKs:

```yaml
name: Build Android APK

on:
  push:
    branches: [ main ]
  pull_request:
    branches: [ main ]

jobs:
  build:
    runs-on: ubuntu-latest
    
    steps:
    - uses: actions/checkout@v3
    
    - name: Set up Python
      uses: actions/setup-python@v4
      with:
        python-version: '3.11'
    
    - name: Install dependencies
      run: |
        sudo apt update
        sudo apt install -y openjdk-17-jdk
        pip install buildozer cython
    
    - name: Build APK
      run: buildozer android debug
    
    - name: Upload APK
      uses: actions/upload-artifact@v3
      with:
        name: hexagon-app-debug.apk
        path: bin/*.apk
```

### Quick Test (Easier Alternative)

For quick testing, you can use **Kivy Launcher** from Google Play Store:
1. Install Kivy Launcher on your Android device
2. Copy your project folder to: `/sdcard/kivy/`
3. Run via Kivy Launcher

### Features of the Android App:
- ✅ Touch to restart animation
- ✅ Responsive design for different screen sizes
- ✅ Smooth 60 FPS animation
- ✅ Yellow, purple, and black hexagons
- ✅ Gathering → Shaking → Falling animation sequence

### File Structure:
```
Loose_APP/
├── main_kivy.py          # Main Kivy app
├── buildozer.spec        # Android build configuration
├── requirements.txt      # Python dependencies
└── README.md            # This file
```
