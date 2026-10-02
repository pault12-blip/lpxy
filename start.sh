#!/bin/sh
BASE=$(cd "$(dirname "$0")" && pwd)
cd "$BASE" || exit 1

CMD="/usr/bin/python3 $BASE/lpxy.py"
ESCAPED=$(printf '%s' "$CMD" | sed 's/[][\.*^$/]/\\&/g')

PID=$(pgrep -f "^$ESCAPED\$")

if [ "$1" = "status" ]; then
    if [ -n "$PID" ]; then
        echo "running: $CMD (pid $PID)"
    else
        echo "not running: $CMD"
    fi
    echo -n "health: "
    curl -s -m 3 "http://localhost:${PORT:-4000}/health" || echo "unreachable"
    echo
    exit 0
fi

if [ -n "$PID" ]; then
    echo "stopping: $CMD (pid $PID)"
    pkill -f "^$ESCAPED\$"
    while pgrep -f "^$ESCAPED\$" >/dev/null 2>&1; do
        sleep 0.2
    done
fi

if [ "$1" = "kill" ]; then
    exit 0
fi

echo "starting: $CMD"
rm -f "$BASE/log.log"
nohup $CMD >>"$BASE/log.log" 2>&1 &
echo "pid $!"

