#!/data/data/com.termux/files/usr/bin/bash
# termux_install.sh — Install StaticVideoSorter dependencies on Termux

set -e

echo "================================"
echo "  StaticVideoSorter — Termux Installer"
echo "================================"
echo ""

# Update package repos
echo "[1/3] Updating package repositories..."
pkg update -y && pkg upgrade -y

# Install system packages
echo ""
echo "[2/3] Installing system packages..."
pkg install -y python ffmpeg

# Install Python dependencies
echo ""
echo "[3/3] Installing Python dependencies..."
pip install opencv-python-headless numpy tqdm fastapi uvicorn python-multipart jinja2

echo ""
echo "================================"
echo "  Installation complete!"
echo "================================"
echo ""
echo "Usage:"
echo "  # Detect static videos"
echo "  python detect.py /path/to/videos"
echo ""
echo "  # Extract best frames"
echo "  python extract.py /path/to/videos"
echo ""
echo "  # Launch web GUI"
echo "  python app.py"
echo ""
