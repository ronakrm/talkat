# Maintainer: Your Name <your.email@example.com>
pkgname=talkat
pkgver=0.1.0
pkgrel=1
pkgdesc="Voice-to-text dictation system for Wayland Linux compositors"
arch=('any')
url="https://github.com/yourusername/talkat"
license=('MIT')
depends=(
    'python>=3.12'
    'python-numpy'
    'python-requests'
    'python-flask'
    'python-pyaudio'
    'portaudio'
    'ydotool'
)
optdepends=(
    'wl-clipboard: Clipboard support on Wayland'
    'xclip: Clipboard support on X11'
    'libnotify: Desktop notifications'
)
makedepends=(
    'python-build'
    'python-installer'
    'python-wheel'
    'python-setuptools'
)
source=("$pkgname-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz")
sha256sums=('SKIP')

build() {
    cd "$pkgname-$pkgver"
    python -m build --wheel --no-isolation
}

package() {
    cd "$pkgname-$pkgver"
    
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
}