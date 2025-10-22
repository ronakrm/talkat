# Maintainer: Ronak Mehta <ronakrm@gmail.com>
pkgname=talkat
pkgver=0.1.0
pkgrel=1
pkgdesc="Voice-to-text dictation system for Wayland Linux compositors"
arch=('any')
url="https://github.com/ronakrm/talkat"
license=('MIT')
depends=(
    'uv'           # Python package manager (handles all Python dependencies)
    'portaudio'    # Audio I/O library
    'ydotool'      # Wayland input automation
)
optdepends=(
    'wl-clipboard: Clipboard support on Wayland'
    'xclip: Clipboard support on X11'
    'libnotify: Desktop notifications'
)
options=('!strip')  # Disable stripping for faster builds (contains large venv)
makedepends=(
    'git'
)
source=("$pkgname-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz")
sha256sums=('f6ef256b98c3690c0c87e7b3f439a2c69c1473d5091fb3f2da039ffe2d461198')

build() {
    cd "$pkgname-$pkgver"

    # Create virtual environment and install dependencies using uv
    # We don't use 'uv sync' because it requires a lockfile (uv.lock)
    # Instead, we use 'uv venv' + 'uv pip install' which reads from pyproject.toml
    uv venv
    uv pip install -e .
}

package() {
    cd "$pkgname-$pkgver"

    # Install application to /usr/lib/talkat
    install -dm755 "$pkgdir/usr/lib/$pkgname"

    # Copy all project files
    cp -r . "$pkgdir/usr/lib/$pkgname/"

    # Create wrapper script in /usr/bin
    install -dm755 "$pkgdir/usr/bin"
    cat > "$pkgdir/usr/bin/talkat" << 'EOF'
#!/bin/bash
cd /usr/lib/talkat
exec uv run talkat "$@"
EOF
    chmod +x "$pkgdir/usr/bin/talkat"

    # Create systemd user service (don't use repo's template version)
    install -dm755 "$pkgdir/usr/lib/systemd/user"
    cat > "$pkgdir/usr/lib/systemd/user/talkat.service" << 'EOF'
[Unit]
Description=Talkat Voice Dictation Server
Documentation=https://github.com/ronakrm/talkat
After=sound.target

[Service]
Type=simple
ExecStart=/usr/bin/talkat server
Restart=on-failure
RestartSec=5
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
EOF

    # Install desktop file
    install -Dm644 talkat.desktop "$pkgdir/usr/share/applications/talkat.desktop"

    # Install license
    install -Dm644 LICENSE "$pkgdir/usr/share/licenses/$pkgname/LICENSE"

    # Install documentation
    install -Dm644 README.md "$pkgdir/usr/share/doc/$pkgname/README.md"
}
