# ceph-bench
Ceph OSD benchmark and analysis scripts

Description:
  These scripts provide the tools for benchmarking and analyzing 
  the performance of Ceph OSDs. The Bash script for benchmarking
  has been developed and tested with Ceph 18.2.2 on RHEL 9.4 and 
  should be capable of running on a variety of operating systems.
  Python3 dependencies include Numpy, Pandas, & SciPy.

  With these scripts you should be able to easily identify OSDs
  that are underperforming. Note that it's recommended that all
  of the OSDs in each device class be of the same make and model.
  Running the benchmark in parallel is preferred as running the
  tests serially can be very time consuming for mulitple runs.
  > The benchmark script also drops OSD caches before each run
  so that each benchmark isn't affected by the previous run.

   
Bash script options:
```
   -t <type>    Device type to benchmark (hdd|ssd|nvme)
   -r <number>  Number of benchmark runs (default: 1)
   -o <file>    Output file name (default: ceph_bench_results_TIMESTAMP.csv)
   -p           Run benchmarks in parallel
   -m <mode>    Benchmark mode (bandwidth|iops|balanced)
   -v           Verbose output
   -h           Display help message
```
Python script options:
```
   --threshold  <number> Percentage below median to flag as slow (default: 15)
```     
 Dependencies:
   - bash shell
   - Ceph common packages for your distro
   - Python3 & Numpy, Pandas, & SciPy for analysis
   - Standard Linux utilities

An example output from the Bash script:
```
# ./ceph-bench.sh -r 2 -p -m balanced
Starting benchmarks...
Benchmarking complete. Results saved to ceph_bench_results_20241221_105356.csv
```

An example output from the Python analysis script:
```
# ./ceph-analysis.py ceph_bench_results_20241221_105356.csv

=== Ceph OSD Performance Analysis Report ===
Analysis Date: 2024-12-21 10:58:29
Input File: ceph_bench_results_20241221_105356.csv

Total OSDs analyzed: 835
Device Classes: hdd, nvme
Number of runs: 2

=== Basic Statistics by Device Class ===
             bytes_per_sec                                                         iops                                       
                      mean       median         std          min          max      mean    median      std       min       max
device_class                                                                                                                  
hdd             59.46 MB/s   59.06 MB/s   7.94 MB/s   30.34 MB/s   82.38 MB/s   1902.68   1889.92   254.08    970.91   2636.19
nvme           622.28 MB/s  619.75 MB/s  44.32 MB/s  515.00 MB/s  764.10 MB/s  19912.81  19832.16  1418.35  16479.91  24451.06

=== Aggregate Performance by Run ===

HDD Devices:
Run 1: Total Bandwidth: 41.48 GB/s, Total IOPS: 1359070.61
Run 2: Total Bandwidth: 41.56 GB/s, Total IOPS: 1361755.17

NVME Devices:
Run 1: Total Bandwidth: 72.78 GB/s, Total IOPS: 2384714.93
Run 2: Total Bandwidth: 73.07 GB/s, Total IOPS: 2394358.75

=== Potential Performance Issues ===

Potential slow drives (hdd):
OSD.110: Avg IOPS: 1557.78, Avg Throughput: 48.68 MB/s
OSD.114: Avg IOPS: 1593.55, Avg Throughput: 49.80 MB/s
OSD.186: Avg IOPS: 1548.19, Avg Throughput: 48.38 MB/s
OSD.214: Avg IOPS: 1560.95, Avg Throughput: 48.78 MB/s
....
```

### ceph-cpu-io-sim.py - CPU Capacity Simulator

Benchmarks the local CPU's ability to perform Ceph OSD operations (checksumming,
compression, erasure coding, serialization, metadata ops) and estimates how many
OSDs the CPU can sustain at various drive speeds. Useful for capacity planning
before deploying hardware.

No Ceph cluster or optional dependencies are required (runs with Python3 stdlib
only), but accuracy improves when Ceph libraries (pyeclib, lz4, zstd, etc.)
are available. Libraries are auto-detected with a three-tier fallback strategy.

CPU Simulator options:
```
  --interactive, -i     Interactive guided configuration
  --quick               Quick benchmark (2s, fewer sizes; honors other flags)
  --drive-type, -t      Drive type: hdd|ssd|nvme (default: hdd)
  --drive-count, -d     Drives per node (default: 12)
  --drive-iops          Override drive IOPS (0=use profile: HDD=150, SSD=50K, NVMe=500K)
  --osds-per-drive      OSD daemons per drive (default: 1)
  --drives NxTYPE [..]  Mixed media: e.g. --drives 24xhdd 4xnvme:0:2
                         Format: COUNTxTYPE[:IOPS[:OSDS_PER_DRIVE]]
                         (overrides --drive-type/count; use IOPS=0 for defaults)
  --protection, -p      Protection: replicated:N or ec:K+M (default: replicated:3)
  --compress, -c        Enable compression: snappy|zstd|lz4|zlib
  --compress-ratio      Expected compression ratio 0.0-1.0 (default: 0.5)
  --compress-mode       Compression mode: passive|aggressive|force (default: passive)
  --wal-db-separate     WAL/DB on separate fast device
  --object-size         RADOS object size (default: 4m)
  --workload            Workload pattern: sequential|random|mixed (default: mixed)
  --rw-ratio            Read/write ratio: 0.0=all writes, 1.0=all reads (default: 0.7)
  --scrub               Scrub frequency: daily|weekly|disabled (default: daily)
  --scenario, -s        Scenario: best|worst|typical|all (default: typical)
  --recovery-osds       Simulate N OSD failures for recovery CPU analysis
  --output, -o          CSV output base file (creates *_benchmarks.csv and *_capacity.csv)
  --compare             Compare with ceph-bench.sh CSV results
  --json                Output results as JSON (valid JSON, supports --scenario all)
  --parallel            Run benchmarks with N parallel workers to measure CPU
                         contention (simulates N OSD daemons competing for CPU)
  --duration            Seconds per benchmark operation (default: 5.0)
  --sizes               Object sizes for micro-benchmarks (default: 4k 64k 128k 4m)
  --cpu-cores           Total CPU cores (0=auto-detect)
  --cpu-cores-ceph      CPU cores reserved for Ceph (0=auto: total minus 2)
  --verbose, -v         Verbose output
```

Example usage:
```
# Quick benchmark with defaults
./ceph-cpu-io-sim.py --quick

# HDD cluster with 12 drives, 3x replication
./ceph-cpu-io-sim.py --drive-type hdd --drive-count 12 --protection replicated:3

# NVMe cluster with EC and compression
./ceph-cpu-io-sim.py --drive-type nvme --drive-count 4 --protection ec:4+2 --compress zstd

# Mixed media: 24 HDDs for data + 4 NVMe for CephFS metadata (2 OSDs each)
./ceph-cpu-io-sim.py --drives 24xhdd 4xnvme:0:2 --wal-db-separate

# Mixed media with IOPS overrides
./ceph-cpu-io-sim.py --drives 36xhdd:150 4xnvme:10000 --recovery-osds 1

# Single class with multiple OSDs per drive
./ceph-cpu-io-sim.py --drive-type nvme --drive-count 4 --osds-per-drive 2

# Compare simulation with real benchmark results
./ceph-cpu-io-sim.py --drive-type hdd --compare ceph_bench_results.csv

# Run all scenarios for capacity planning
./ceph-cpu-io-sim.py --drive-type ssd --drive-count 8 --scenario all

# Simulate recovery with 1 OSD failure, JSON output
./ceph-cpu-io-sim.py --drive-type hdd --drive-count 36 --recovery-osds 1 --json

# Quick test with NVMe to get CPU scaling advice
./ceph-cpu-io-sim.py --quick --drive-type nvme --drive-count 4

# Benchmark under contention (simulate 32 OSD daemons competing for CPU)
./ceph-cpu-io-sim.py --drives 24xhdd 4xnvme:0:2 --parallel 32 --wal-db-separate
```

Report sections:
- **Benchmark Results**: Raw CPU micro-benchmark performance
- **CPU Cost Per IO**: Weighted cost breakdown by operation
- **OSD Capacity Estimate**: Max OSDs the CPU can sustain
- **Scale-Out Projection**: Cluster performance at 1-64 nodes
- **Recovery Impact Analysis**: CPU impact during OSD failure recovery (with --recovery-osds)
- **CPU Scaling Analysis**: Whether to add more cores or get faster cores
- **Recommendations**: Actionable advice for the configuration

Running tests:
```
python3 -m unittest test_ceph_cpu_io_sim -v
```

Dependencies:
  - Python 3.6+
  - No required external packages for core benchmarks (CRC32C, SHA256, serialization)
  - Compression benchmarks require: `pip install lz4 pyzstd python-snappy`
  - Optional: pyeclib, crcmod, python-rocksdb (for higher accuracy benchmarks)
