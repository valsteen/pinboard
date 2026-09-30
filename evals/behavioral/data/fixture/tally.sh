#!/bin/sh
# Sum integers read from standard input, one per line.
sum=0
while IFS= read -r line; do
  [ -z "$line" ] && continue
  sum=$((sum + line))
done
echo "$sum"
