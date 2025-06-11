#!/bin/bash
# Setup script for talkat - handles both installation and updates

set -e

# Detect if this is an update or fresh install
if [ -d "/opt/talkat" ]; then
    echo "Updating existing talkat installation..."
    IS_UPDATE=true
else
    echo "Setting up talkat for the first time..."
    IS_UPDATE=false
fi

# Check if running as root for systemd service installation
if [ "$EUID" -ne 0 ]; then 
    echo "Please run as root (sudo) to install systemd service"
    exit 1
fi

# Install uv if not present
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source ~/.bashrc
fi

# Create/update application directory
APP_DIR="/opt/talkat"
if [ "$IS_UPDATE" = true ]; then
    echo "Updating application files at $APP_DIR..."
    # Stop the service before updating
    systemctl stop talkat || true
else
    echo "Creating application directory at $APP_DIR..."
    mkdir -p "$APP_DIR"
fi

# Copy all files (works for both install and update)
cp -r ./* "$APP_DIR/"

# Install dependencies
echo "Installing Python dependencies..."
cd "$APP_DIR"
uv sync

# Create systemd service (only if it doesn't exist or if fresh install)
if [ ! -f /etc/systemd/system/talkat.service ] || [ "$IS_UPDATE" = false ]; then
    echo "Creating systemd service..."
    cat > /etc/systemd/system/talkat.service << EOF
[Unit]
Description=Talkat Model Server
After=network.target

[Service]
Type=simple
User=$SUDO_USER
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/uv run talkat server
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
fi

# Create client wrapper script
echo "Creating client wrapper script..."
cat > /usr/local/bin/talkat << EOF
#!/bin/bash
cd $APP_DIR
exec uv run talkat "\$@"
EOF

chmod +x /usr/local/bin/talkat

# Enable and start the service
echo "Enabling and starting talkat service..."
systemctl daemon-reload
systemctl enable talkat
systemctl start talkat

if [ "$IS_UPDATE" = true ]; then
    echo ""
    echo "Update complete!"
    echo "The talkat service has been restarted with the latest changes."
else
    echo ""
    echo "Setup complete!"
    echo "The model server is now running as a systemd service."
fi

echo ""
echo "Available commands:"
echo "  - talkat listen     # Start short dictation (types to screen)"
echo "  - talkat long       # Start long dictation (saves to file)"
echo "  - talkat calibrate  # Calibrate microphone threshold"
echo "  - talkat server     # Start the model server manually (if needed)"
echo ""
echo "Service management:"
echo "  - systemctl status talkat   # Check service status"
echo "  - systemctl restart talkat  # Restart the service"
echo "  - systemctl stop talkat     # Stop the service"
echo "  - systemctl start talkat    # Start the service"
echo ""
echo "Transcripts are saved to: ~/.local/share/talkat/transcripts/"
