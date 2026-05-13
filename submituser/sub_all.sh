#!/bin/bash

set -euo pipefail

mkdir -p logs submit_files
# seq 0 39
for crate in $(seq 0 29); do
    subfile="submit_files/tr_crate_${crate}.sub"

    cat > "$subfile" <<EOF
universe   = vanilla
executable = tr2.py
arguments  = 131.154.99.225 ${crate} 5000 6666 --packets-per-stream 30000

should_transfer_files   = YES
when_to_transfer_output = ON_EXIT
transfer_input_files    = tr2.py

output = logs/tr_crate_${crate}.\$(ClusterId).\$(ProcId).out
error  = logs/tr_crate_${crate}.\$(ClusterId).\$(ProcId).err
log    = logs/tr_crate_${crate}.\$(ClusterId).log

+OWNER = "condor"

queue
EOF

    echo "Submitting crate ${crate}..."
    condor_submit --spool "$subfile"
    if [ "$crate" -lt 39 ]; then
        echo "Aspetto 30 secondi prima del job successivo..."
        sleep 30
    fi
done
