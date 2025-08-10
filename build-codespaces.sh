#!/bin/bash
# GitHub Codespaces build script for Hexagon Animation APK

echo "🚀 Starting Hexagon Animation APK build in Codespaces..."
echo "======================================================="

# Update system packages
echo "📦 Updating system packages..."
sudo apt-get update

# Install Java Development Kit (required for Android builds)
echo "☕ Installing Java JDK..."
sudo apt-get install -y openjdk-17-jdk

# Install required build dependencies
echo "🛠️ Installing build dependencies..."
sudo apt-get install -y \
    git zip unzip \
    autoconf libtool pkg-config \
    zlib1g-dev libncurses5-dev libncursesw5-dev \
    libtinfo5 cmake libffi-dev libssl-dev \
    build-essential

# Set Java environment
export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64
echo "export JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64" >> ~/.bashrc

# Install Python build tools
echo "🐍 Installing Python build tools..."
pip install --upgrade pip
pip install buildozer cython

# Show versions for debugging
echo "📋 Build environment info:"
echo "Java version: $(java -version 2>&1 | head -n 1)"
echo "Python version: $(python3 --version)"
echo "Buildozer version: $(buildozer --version)"

# Clean any previous builds
if [ -d ".buildozer" ]; then
    echo "🧹 Cleaning previous builds..."
    rm -rf .buildozer
fi

if [ -d "bin" ]; then
    echo "🧹 Removing old APK files..."
    rm -rf bin
fi

# Initialize buildozer (creates .buildozer directory structure)
echo "🔧 Initializing buildozer..."
buildozer init || echo "buildozer.spec already exists, continuing..."

# Start the APK build process
echo "🏗️ Building APK (this will take 15-25 minutes)..."
echo "☕ Perfect time for a coffee break!"
echo ""

# Build debug APK
buildozer android debug

# Check if build was successful
if [ -f bin/*.apk ]; then
    echo ""
    echo "🎉 SUCCESS! APK built successfully!"
    echo "📱 Your APK is ready:"
    ls -la bin/*.apk
    echo ""
    echo "📥 To download:"
    echo "1. Go to the 'bin' folder in the file explorer"
    echo "2. Right-click the .apk file → Download"
    echo "3. Transfer to your Android device and install!"
    echo ""
    echo "🎮 Your hexagon animation app is ready to install!"
else
    echo ""
    echo "❌ Build failed. Check the logs above for errors."
    echo "💡 Common fixes:"
    echo "   - Wait a few minutes and try again"
    echo "   - Check internet connection"
    echo "   - Review buildozer.spec configuration"
fi
