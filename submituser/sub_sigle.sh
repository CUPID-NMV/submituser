#!/bin/bash

set -euo pipefail

mkdir -p logs submit_files

runner="submit_files/run_all_crates.sh"
subfile="submit_files/tr_all_crates.sub"

cat > "$runner" <<'EOF'
#!/bin/bash
set -euo pipefail

for crate in $(seq 0 39); do
    echo "===== START crate ${crate} : $(date) ====="
    python3 tr2.py 131.154.99.225 "${crate}" 5000 6666 --packets-per-stream 3000
    echo "===== END   crate ${crate} : $(date) ====="
    sleep 10
done
EOF

chmod +x "$runner"

cat > "$subfile" <<EOF
universe   = vanilla
executable = submit_files/run_all_crates.sh

should_transfer_files   = YES
when_to_transfer_output = ON_EXIT
transfer_input_files    = tr2.py,submit_files/run_all_crates.sh

output = logs/tr_all_crates.\$(ClusterId).\$(ProcId).out
error  = logs/tr_all_crates.\$(ClusterId).\$(ProcId).err
log    = logs/tr_all_crates.\$(ClusterId).log

+OWNER = "condor"

queue
EOF

echo "Creato: $subfile"
echo "Sottometti con:"
echo "  condor_submit --spool $subfile"
