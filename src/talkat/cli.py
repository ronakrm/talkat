#!/usr/bin/env python3

import sys
import os
import subprocess
import signal
import argparse
from pathlib import Path
from .main import main as client_main
from .model_server import main as server_main

# PID file location
PID_FILE = Path.home() / ".cache" / "talkat" / "long_dictation.pid"

def get_long_pid():
    """Get the PID of the running long dictation process."""
    if PID_FILE.exists():
        try:
            with open(PID_FILE, 'r') as f:
                pid = int(f.read().strip())
                # Check if process is still running
                os.kill(pid, 0)
                return pid
        except (ValueError, OSError, ProcessLookupError):
            # PID file exists but process is not running
            PID_FILE.unlink(missing_ok=True)
    return None

def start_long_background():
    """Start long dictation in background."""
    if get_long_pid():
        print("Long dictation is already running.")
        return 1
    
    # Ensure cache directory exists
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    
    # Start the process in background
    proc = subprocess.Popen([sys.executable, "-m", "talkat.cli", "long"],
                           stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL,
                           stdin=subprocess.DEVNULL,
                           start_new_session=True)
    
    # Write PID to file
    with open(PID_FILE, 'w') as f:
        f.write(str(proc.pid))
    
    print(f"Long dictation started in background (PID: {proc.pid})")
    try:
        subprocess.run(['notify-send', 'Talkat', 'Long dictation started'], check=False)
    except FileNotFoundError:
        pass
    return 0

def stop_long_background():
    """Stop the background long dictation process."""
    pid = get_long_pid()
    if not pid:
        print("Long dictation is not running.")
        return 1
    
    try:
        # Send SIGINT (Ctrl+C) to trigger graceful shutdown
        os.kill(pid, signal.SIGINT)
        print(f"Stopped long dictation (PID: {pid})")
        PID_FILE.unlink(missing_ok=True)
        try:
            subprocess.run(['notify-send', 'Talkat', 'Long dictation stopped'], check=False)
        except FileNotFoundError:
            pass
        return 0
    except ProcessLookupError:
        print("Long dictation process not found.")
        PID_FILE.unlink(missing_ok=True)
        return 1

def toggle_long_background():
    """Toggle long dictation - start if stopped, stop if running."""
    if get_long_pid():
        return stop_long_background()
    else:
        return start_long_background()

def main():
    parser = argparse.ArgumentParser(description="Talkat - Voice Command System")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Client mode (listen)
    listen_parser = subparsers.add_parser("listen", help="Start listening for voice commands")
    
    # Long dictation mode
    long_parser = subparsers.add_parser("long", help="Start long dictation mode (continuous recording)")
    
    # Start long dictation in background
    start_long_parser = subparsers.add_parser("start-long", help="Start long dictation in background")
    
    # Stop long dictation
    stop_long_parser = subparsers.add_parser("stop-long", help="Stop background long dictation")
    
    # Toggle long dictation
    toggle_long_parser = subparsers.add_parser("toggle-long", help="Toggle long dictation (start if stopped, stop if running)")
    
    # Server mode
    server_parser = subparsers.add_parser("server", help="Start the model server")

    args = parser.parse_args()

    if args.command == "listen":
        client_main()
    elif args.command == "long":
        client_main(mode="long")
    elif args.command == "start-long":
        sys.exit(start_long_background())
    elif args.command == "stop-long":
        sys.exit(stop_long_background())
    elif args.command == "toggle-long":
        sys.exit(toggle_long_background())
    elif args.command == "server":
        server_main()
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main() 