#!/bin/bash

# Default values
RUNS=1
DEVICE_TYPE=""
OUTPUT_FILE="ceph_bench_results_$(date +%Y%m%d_%H%M%S).csv"
PARALLEL=false
TEMP_DIR="bench_temp_$(date +%s)"
MODE="balanced"
VERBOSE=false

usage() {
    cat << EOF
Ceph OSD Benchmark Script
Usage: $0 [options]

Options:
    -t <type>    Device type to benchmark (hdd|ssd|nvme)
    -r <number>  Number of benchmark runs (default: 1)
    -o <file>    Output file name (default: ceph_bench_results_TIMESTAMP.csv)
    -p           Run benchmarks in parallel
    -m <mode>    Benchmark mode (bandwidth|iops|balanced)
    -v           Verbose output
    -h           Display this help message
EOF
    exit 1
}

cleanup() {
    if [ -d "$TEMP_DIR" ]; then
        rm -rf "$TEMP_DIR"
    fi
}

log() {
    if [ "$VERBOSE" = true ] || [ "$2" = "always" ]; then
        echo "$1"
    fi
}

# Parse command line arguments
while getopts "t:r:o:m:pvh" opt; do
    case $opt in
        t) DEVICE_TYPE=$OPTARG
           if [[ ! $DEVICE_TYPE =~ ^(hdd|ssd|nvme)$ ]]; then
               echo "Error: Device type must be hdd, ssd, or nvme"
               exit 1
           fi
           ;;
        r) RUNS=$OPTARG
           if ! [[ $RUNS =~ ^[0-9]+$ ]] || [ $RUNS -lt 1 ]; then
               echo "Error: Runs must be a positive integer"
               exit 1
           fi
           ;;
        m) MODE=$OPTARG
           if [[ ! $MODE =~ ^(bandwidth|iops|balanced)$ ]]; then
               echo "Error: Mode must be bandwidth, iops, or balanced"
               exit 1
           fi
           ;;
        o) OUTPUT_FILE=$OPTARG ;;
        p) PARALLEL=true ;;
        v) VERBOSE=true ;;
        h) usage ;;
        \?) usage ;;
    esac
done

# Set up directory for parallel execution if needed
if [ "$PARALLEL" = true ]; then
    mkdir -p "$TEMP_DIR"
    if [ $? -ne 0 ]; then
        echo "Error: Failed to create temporary directory"
        exit 1
    fi
    trap cleanup EXIT
fi

# Initialize CSV file with headers
echo "timestamp,osd_id,device_class,run_number,bytes_written,blocksize,elapsed_sec,bytes_per_sec,iops" > "$OUTPUT_FILE"

# Function to run benchmark on a single OSD
benchmark_osd() {
    local osd_id=$1
    local device_class=$2
    local run=$3
    local temp_file="$4"
    local timestamp
    local bench_cmd
    
    # Always drop cache before benchmarking
    log "Dropping cache for OSD.$osd_id..."
    ceph tell osd.$osd_id cache drop 2>/dev/null
    if [ $? -ne 0 ]; then
        log "Warning: Failed to drop cache for OSD.$osd_id" "always"
    fi
    sleep 2
    
    log "Benchmarking OSD.$osd_id (Run $run of $RUNS)..."
    timestamp=$(date +"%Y-%m-%d %H:%M:%S")
    
    # Set benchmark parameters based on mode
    case $MODE in
        bandwidth)
            bench_cmd="ceph tell osd.$osd_id bench"
            ;;
        iops)
            bench_cmd="ceph tell osd.$osd_id bench 12288000 4096 4194304 100"
            ;;
        balanced)
            bench_cmd="ceph tell osd.$osd_id bench 98304000 32768 4194304 100"
            ;;
    esac
    
    # Run benchmark and capture output
    result=$(eval $bench_cmd 2>/dev/null)
    if [ $? -ne 0 ]; then
        log "Error benchmarking OSD.$osd_id" "always"
        return 1
    fi
    
    # Parse JSON output and write to file
    if [ "$PARALLEL" = true ]; then
        echo "$result" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    with open('$temp_file', 'w') as f:
        f.write('$timestamp,$osd_id,$device_class,$run,{},{},{},{},{}\n'.format(
            data['bytes_written'], data['blocksize'], 
            data['elapsed_sec'], data['bytes_per_sec'], data['iops']))
except Exception as e:
    print(f'Error parsing JSON: {str(e)}', file=sys.stderr)
    sys.exit(1)
"
    else
        echo "$result" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    print('$timestamp,$osd_id,$device_class,$run,{},{},{},{},{}'.format(
        data['bytes_written'], data['blocksize'], 
        data['elapsed_sec'], data['bytes_per_sec'], data['iops']))
except Exception as e:
    print(f'Error parsing JSON: {str(e)}', file=sys.stderr)
    sys.exit(1)
" >> "$OUTPUT_FILE"
    fi
}

# Get list of OSDs and their classes
log "Gathering OSD information..."
osd_info=$(ceph osd tree --format=json 2>/dev/null)
if [ $? -ne 0 ]; then
    echo "Error: Failed to get OSD tree information"
    exit 1
fi

# Store OSD information in arrays
readarray -t osd_ids < <(echo "$osd_info" | python3 -c '
import sys, json
data = json.load(sys.stdin)
for node in data["nodes"]:
    if "id" in node and node["id"] >= 0 and node["status"] == "up":
        if "device_class" in node:
            print(node["id"])')

readarray -t device_classes < <(echo "$osd_info" | python3 -c '
import sys, json
data = json.load(sys.stdin)
for node in data["nodes"]:
    if "id" in node and node["id"] >= 0 and node["status"] == "up":
        if "device_class" in node:
            print(node["device_class"])')

# Run benchmarks
log "Starting benchmarks..." "always"
for run in $(seq 1 $RUNS); do
    log "Starting benchmark run $run of $RUNS"
    
    if [ "$PARALLEL" = true ]; then
        run_dir="$TEMP_DIR/run_$run"
        mkdir -p "$run_dir"
        
        for i in "${!osd_ids[@]}"; do
            osd_id="${osd_ids[$i]}"
            device_class="${device_classes[$i]}"
            
            if [ -n "$DEVICE_TYPE" ] && [ "$device_class" != "$DEVICE_TYPE" ]; then
                continue
            fi
            
            temp_file="$run_dir/osd_${osd_id}.csv"
            benchmark_osd "$osd_id" "$device_class" "$run" "$temp_file" &
        done
        
        wait
        
        if [ -d "$run_dir" ]; then
            for temp_file in "$run_dir"/*.csv; do
                if [ -f "$temp_file" ]; then
                    cat "$temp_file" >> "$OUTPUT_FILE"
                fi
            done
            rm -rf "$run_dir"
        fi
    else
        for i in "${!osd_ids[@]}"; do
            osd_id="${osd_ids[$i]}"
            device_class="${device_classes[$i]}"
            
            if [ -n "$DEVICE_TYPE" ] && [ "$device_class" != "$DEVICE_TYPE" ]; then
                continue
            fi
            
            benchmark_osd "$osd_id" "$device_class" "$run"
        done
    fi
done

log "Benchmarking complete. Results saved to $OUTPUT_FILE" "always"
