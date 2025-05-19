#!/bin/bash
# Setup script for talkat

set -e

echo "Setting up talkat with uv..."

# Install uv if not present
if ! command -v uv &> /dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    source ~/.bashrc
fi

# Install dependencies
echo "Installing Python dependencies..."
uv sync

# Download model if not present
MODEL_DIR="$HOME/.local/share/vosk/model-en"
if [ ! -d "$MODEL_DIR" ]; then
    echo "Downloading Vosk model..."
    mkdir -p ~/.local/share/vosk
    cd ~/.local/share/vosk
    wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
    unzip vosk-model-small-en-us-0.15.zip
    mv vosk-model-small-en-us-0.15 model-en
    rm vosk-model-small-en-us-0.15.zip
    cd -
fi

# Create wrapper script
echo "Creating wrapper script..."
mkdir -p ~/.local/bin
cat > ~/.local/bin/talkat << 'EOF'
#!/bin/bash
cd ~/.local/share/talkat
exec uv run talkat
EOF

chmod +x ~/.local/bin/talkat

echo "Setup complete! You can now run: ~/.local/bin/talkat"
echo "Or set up a keybind pointing to that script."
