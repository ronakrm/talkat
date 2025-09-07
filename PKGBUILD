# Maintainer: Your Name <your.email@example.com>
pkgname=talkat
pkgver=0.1.0
pkgrel=1
pkgdesc="Voice-to-text dictation system for Wayland Linux compositors"
arch=('any')
url="https://github.com/yourusername/talkat"
license=('MIT')
depends=(
    'python>=3.11'  # Updated to match pyproject.toml minimum requirement
    'python-numpy>=1.24.0'
    'python-requests>=2.20'
    'python-flask>=2.0'
    'python-pyaudio>=0.2.14'
    'python-pytorch>=2.0.0'  # CPU-only version is sufficient
    'python-vosk>=0.3.45'
    'python-librosa>=0.10.1'
    'python-soundfile>=0.12.1'
    'portaudio'
    'ydotool'
)
optdepends=(
    'wl-clipboard: Clipboard support on Wayland'
    'xclip: Clipboard support on X11'
    'libnotify: Desktop notifications'
    'python-pytorch-cuda: GPU acceleration support for PyTorch'
    'python-pytorch-opt: CPU optimizations (AVX2) for PyTorch'
)
makedepends=(
    'python-build'
    'python-installer'
    'python-wheel'
    'python-setuptools'
    'python-pip'  # Needed for installing AUR-only Python packages
)
# Note: Some Python dependencies (faster-whisper, transformers, accelerate) are not
# available in official Arch repos and will be installed via pip during build
source=("$pkgname-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz")
sha256sums=('SKIP')

build() {
    cd "$pkgname-$pkgver"
    
    # Install Python dependencies not available in Arch repos
    # These are needed for the build process
    pip install --user --no-deps \
        "faster-whisper>=1.1.1" \
        "transformers>=4.36.0" \
        "accelerate>=0.24.0"
    
    python -m build --wheel --no-isolation
}

package() {
    cd "$pkgname-$pkgver"
    
    # Install Python dependencies not available in Arch repos
    # These need to be installed system-wide for the package to work
    pip install --root="$pkgdir" --no-deps \
        "faster-whisper>=1.1.1" \
        "transformers>=4.36.0" \
        "accelerate>=0.24.0"
    
    # Install the Python package
    python -m installer --destdir="$pkgdir" dist/*.whl
    
    # Install systemd service files
    install -Dm644 talkat.service "$pkgdir/usr/lib/systemd/user/talkat.service"
    
    # Install desktop file
    install -Dm644 talkat.desktop "$pkgdir/usr/share/applications/talkat.desktop"
    
    # Install license
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"
    
    # Install documentation
    install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
    install -Dm644 AUR_ROADMAP.md "$pkgdir/usr/share/doc/$pkgname/AUR_ROADMAP.md"
    
    # Create necessary directories
    install -dm755 "$pkgdir/usr/share/$pkgname"
    
    # Install post-install message
    cat > "$pkgdir/usr/share/doc/$pkgname/POST_INSTALL.txt" << 'EOF'
TALKAT POST-INSTALL NOTES
=========================

Some Python dependencies for Talkat are not available in the official Arch 
repositories and have been installed via pip:

- faster-whisper: Fast Whisper implementation for speech recognition
- transformers: Hugging Face Transformers library  
- accelerate: Hardware acceleration library

These packages are automatically managed by this AUR package. If you encounter
any issues with these dependencies, you can manually reinstall them with:

  pip install --user faster-whisper transformers accelerate

For GPU acceleration with PyTorch, install the appropriate PyTorch variant:
- python-pytorch-cuda (for NVIDIA GPUs)
- python-pytorch-rocm (for AMD GPUs)
- python-pytorch-opt (for CPU optimizations)

To start using Talkat:
1. Start the service: systemctl --user enable --now talkat
2. Test with: talkat listen
3. See the documentation: /usr/share/doc/talkat/README.md
EOF
}