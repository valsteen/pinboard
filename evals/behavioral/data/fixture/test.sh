#!/bin/sh
# Minimal checks for tally.sh.
set -e
[ "$(printf '1\n2\n3\n' | ./tally.sh)" = 6 ] || { echo "sum failed"; exit 1; }
[ "$(printf '1\n\n2\n' | ./tally.sh)" = 3 ] || { echo "blank failed"; exit 1; }
echo ok
