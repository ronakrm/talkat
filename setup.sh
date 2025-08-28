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
    APP_DIR="/opt/talkat"
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

# Copy all files
cp -r ./* "$APP_DIR/"

# Install dependencies
echo "Installing Python dependencies..."
cd "$APP_DIR"
uv sync

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
ExecStart=/usr/bin/uv run talkat server
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
ExecStart=$HOME/.local/bin/uv run talkat server
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
EOF
fi

# Create client wrapper script
echo "Creating client wrapper script..."
cat > "$BIN_DIR/talkat" << EOF
#!/bin/bash
cd $APP_DIR
exec uv run talkat "\$@"
EOF

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
    echo "Note: System-wide installation detected at /opt/talkat"
    echo "To remove it, run:"
    echo "  sudo systemctl stop talkat"
    echo "  sudo systemctl disable talkat"
    echo "  sudo rm -rf /opt/talkat"
    echo "  sudo rm /usr/local/bin/talkat"
    echo "  sudo rm /etc/systemd/system/talkat.service"
fi