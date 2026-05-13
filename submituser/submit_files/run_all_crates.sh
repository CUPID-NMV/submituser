#!/bin/bash
set -euo pipefail

for crate in $(seq 0 39); do
    echo "===== START crate ${crate} : $(date) ====="
    python3 tr2.py 131.154.99.225 "${crate}" 5000 6666 --packets-per-stream 3000
    echo "===== END   crate ${crate} : $(date) ====="
    sleep 10
done
