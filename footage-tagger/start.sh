
#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
#  METANAS — Start the app
#  Double-click or run from Terminal:  ./start.sh
# ─────────────────────────────────────────────────────────────────────────────

DIR="$(cd "$(dirname "$0")" && pwd)"
METANAS_HOME="$HOME/.metanas"
VENV="$METANAS_HOME/.venv"
PORT=5151

# Check if virtual env Python binary exists (not just the directory)
if [ ! -f "$VENV/bin/python3" ]; then
  echo "  Virtual environment not found. Running installer first…"
  bash "$DIR/install.sh"
fi

# Check if already running
if lsof -ti tcp:$PORT &>/dev/null; then
  echo "  METANAS is already running at http://localhost:$PORT"
  open "http://localhost:$PORT"
  exit 0
fi

echo ""
echo "  Starting METANAS…"
echo "  Open: http://localhost:$PORT"
echo ""

# Open browser after a short delay (let Flask start first)
(sleep 2 && open "http://localhost:$PORT") &

# Start Flask
cd "$DIR"
exec "$VENV/bin/python3" app.py
