#!/bin/bash
# Setup script for talkat

set -e

echo "Setting up talkat..."

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

# Create application directory
APP_DIR="/opt/talkat"
echo "Creating application directory at $APP_DIR..."
mkdir -p "$APP_DIR"
cp -r ./* "$APP_DIR/"

# Install dependencies
echo "Installing Python dependencies..."
cd "$APP_DIR"
uv sync

# Create systemd service
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

echo "Setup complete!"
echo "The model server is now running as a systemd service."
echo "You can use the following commands:"
echo "  - talkat listen    # Start listening for voice input"
echo "  - talkat server    # Start the model server manually (if needed)"
echo "  - systemctl status talkat    # Check service status"
echo "  - systemctl stop talkat      # Stop the service"
echo "  - systemctl start talkat     # Start the service"
