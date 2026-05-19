#!/bin/bash
# Setup script for talkat - handles both installation and updates

set -e

# Parse command line arguments
INSTALL_MODE="system"  # Default to system-wide
while [[ $# -gt 0 ]]; do
    case $1 in
        --user)
            INSTALL_MODE="user"
            shift
            ;;
        --system)
            INSTALL_MODE="system"
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Usage: $0 [--user|--system]"
            echo "  --user    Install for current user only (no sudo required)"
            echo "  --system  Install system-wide (requires sudo, default)"
            exit 1
            ;;
    esac
done

# Check if running with proper privileges based on mode
if [ "$INSTALL_MODE" = "system" ]; then
    if [ "$EUID" -ne 0 ]; then
        echo "System-wide installation requires root. Please run with sudo."
        exit 1
    fi
    APP_DIR="/usr/share/talkat"
    BIN_DIR="/usr/local/bin"
    SERVICE_DIR="/etc/systemd/system"
    SERVICE_TYPE="system"
    USER_NAME="$SUDO_USER"
else
    APP_DIR="$HOME/.local/share/talkat"
    BIN_DIR="$HOME/.local/bin"
    SERVICE_DIR="$HOME/.config/systemd/user"
    SERVICE_TYPE="user"
    USER_NAME="$USER"

    # Create user directories if they don't exist
    mkdir -p "$BIN_DIR"
    mkdir -p "$SERVICE_DIR"

    # Ensure ~/.local/bin is in PATH
    if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
        echo "Warning: $BIN_DIR is not in your PATH."
        echo "Add the following to your ~/.bashrc or ~/.zshrc:"
        echo "  export PATH=\"$BIN_DIR:\$PATH\""
    fi
fi

# Detect if this is an update or fresh install
if [ -d "$APP_DIR" ]; then
    echo "Updating existing talkat installation ($INSTALL_MODE mode)..."
    IS_UPDATE=true
else
    echo "Setting up talkat for the first time ($INSTALL_MODE mode)..."
    IS_UPDATE=false
fi

# Install uv if not present
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source ~/.bashrc
fi

# Stop existing service if updating
if [ "$IS_UPDATE" = true ]; then
    echo "Stopping existing service..."
    if [ "$SERVICE_TYPE" = "system" ]; then
        systemctl stop talkat || true
    else
        systemctl --user stop talkat || true
    fi
fi

# Create/update application directory
echo "Installing to $APP_DIR..."
mkdir -p "$APP_DIR"

# Copy source files, excluding build artifacts, venvs, caches, and user data.
# Uses rsync to honor an exclude list; falls back to cp -r ./* (which already
# skips dotfiles via the shell glob) when rsync is unavailable.
if command -v rsync &> /dev/null; then
    rsync -a --delete \
        --exclude='.git/' \
        --exclude='.venv/' \
        --exclude='__pycache__/' \
        --exclude='build/' \
        --exclude='dist/' \
        --exclude='data/' \
        --exclude='.mypy_cache/' \
        --exclude='.pytest_cache/' \
        --exclude='.ruff_cache/' \
        --exclude='*.egg-info/' \
        --exclude='.claude/' \
        ./ "$APP_DIR/"
else
    echo "Warning: rsync not found; falling back to cp. Build artifacts under build/, dist/, and data/ may be copied."
    cp -r ./* "$APP_DIR/"
fi

# Install dependencies
echo "Installing Python dependencies..."
cd "$APP_DIR"

if [ "$SERVICE_TYPE" = "system" ]; then
    # System install: create the venv explicitly, pinned to system Python, then
    # install talkat into it. Mirrors the PKGBUILD layout so /usr/share/talkat/.venv
    # is self-contained and reachable by the non-root service user without
    # depending on a uv-managed Python under /root.
    SYSTEM_PYTHON=$(command -v python3.12 || command -v python3.11 || command -v python3)
    if [ -z "$SYSTEM_PYTHON" ]; then
        echo "Error: no suitable system Python found (need python3.11 or newer)."
        exit 1
    fi
    echo "Using system Python: $SYSTEM_PYTHON"

    # Wipe any stale or half-created .venv before recreating.
    if [ -d "$APP_DIR/.venv" ] && [ ! -x "$APP_DIR/.venv/bin/python" ]; then
        echo "Removing broken/stale .venv at $APP_DIR/.venv..."
        rm -rf "$APP_DIR/.venv"
    fi

    uv venv "$APP_DIR/.venv" --python "$SYSTEM_PYTHON"
    uv pip install --python "$APP_DIR/.venv/bin/python" .

    PYTHON_BIN="$APP_DIR/.venv/bin/python"

    echo "Setting ownership for user $USER_NAME..."
    chown -R "$USER_NAME:$USER_NAME" "$APP_DIR"
else
    # User install: standard uv-managed workflow.
    uv sync
fi

# Create service file
echo "Creating $SERVICE_TYPE service..."
if [ "$SERVICE_TYPE" = "system" ]; then
    cat > "$SERVICE_DIR/talkat.service" << EOF
[Unit]
Description=Talkat Model Server
After=network.target

[Service]
Type=simple
User=$USER_NAME
WorkingDirectory=$APP_DIR
ExecStart=$PYTHON_BIN -m talkat.cli server
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
else
    cat > "$SERVICE_DIR/talkat.service" << EOF
[Unit]
Description=Talkat Model Server
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/uv run talkat server
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
fi

# Create client wrapper script
echo "Creating client wrapper script..."
if [ "$SERVICE_TYPE" = "system" ]; then
    cat > "$BIN_DIR/talkat" << EOF
#!/bin/bash
exec $PYTHON_BIN -m talkat.cli "\$@"
EOF
else
    cat > "$BIN_DIR/talkat" << EOF
#!/bin/bash
cd $APP_DIR
exec uv run talkat "\$@"
EOF
fi

chmod +x "$BIN_DIR/talkat"

# Enable and start the service
echo "Enabling and starting talkat service..."
if [ "$SERVICE_TYPE" = "system" ]; then
    systemctl daemon-reload
    systemctl enable talkat
    systemctl start talkat
else
    systemctl --user daemon-reload
    systemctl --user enable talkat
    systemctl --user start talkat
fi

if [ "$IS_UPDATE" = true ]; then
    echo ""
    echo "Update complete!"
    echo "The talkat service has been restarted with the latest changes."
else
    echo ""
    echo "Setup complete!"
    echo "The model server is now running as a $SERVICE_TYPE service."
fi

echo ""
echo "Available commands:"
echo "  - talkat listen     # Start short dictation (types to screen)"
echo "  - talkat long       # Start long dictation (saves to file)"
echo "  - talkat calibrate  # Calibrate microphone threshold"
echo "  - talkat server     # Start the model server manually (if needed)"
echo ""
echo "Service management:"
if [ "$SERVICE_TYPE" = "system" ]; then
    echo "  - systemctl status talkat   # Check service status"
    echo "  - systemctl restart talkat  # Restart the service"
    echo "  - systemctl stop talkat     # Stop the service"
    echo "  - systemctl start talkat    # Start the service"
else
    echo "  - systemctl --user status talkat   # Check service status"
    echo "  - systemctl --user restart talkat  # Restart the service"
    echo "  - systemctl --user stop talkat     # Stop the service"
    echo "  - systemctl --user start talkat    # Start the service"
fi
echo ""
echo "Transcripts are saved to: ~/.local/share/talkat/transcripts/"

# Clean up old installations if requested
if [ "$INSTALL_MODE" = "user" ] && [ -f "/usr/local/bin/talkat" ]; then
    echo ""
    echo "Note: System-wide installation detected at /usr/share/talkat"
    echo "To remove it, run:"
    echo "  sudo systemctl stop talkat"
    echo "  sudo systemctl disable talkat"
    echo "  sudo rm -rf /usr/share/talkat"
    echo "  sudo rm /usr/local/bin/talkat"
    echo "  sudo rm /etc/systemd/system/talkat.service"
fi
