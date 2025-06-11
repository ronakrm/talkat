#!/usr/bin/env python3

import sys
import argparse
from pathlib import Path
from .main import main as client_main
from .model_server import main as server_main

def main():
    parser = argparse.ArgumentParser(description="Talkat - Voice Command System")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Client mode (listen)
    listen_parser = subparsers.add_parser("listen", help="Start listening for voice commands")
    
    # Long dictation mode
    long_parser = subparsers.add_parser("long", help="Start long dictation mode (continuous recording)")
    
    # Server mode
    server_parser = subparsers.add_parser("server", help="Start the model server")

    args = parser.parse_args()

    if args.command == "listen":
        client_main()
    elif args.command == "long":
        client_main(mode="long")
    elif args.command == "server":
        server_main()
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    main() 