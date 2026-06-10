#!/bin/bash
set -e

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Check if virtual environment exists
if [ ! -d "$SCRIPT_DIR/venv" ]; then
    echo "Initializing virtual environment..."
    python3 -m venv "$SCRIPT_DIR/venv"
    echo "Initialization complete! (Note: dependencies must be installed manually as there is no requirements.txt)"
fi

# Run the python script with the passed arguments
exec "$SCRIPT_DIR/venv/bin/python" "$SCRIPT_DIR/trainmate_cli.py" "$@"
