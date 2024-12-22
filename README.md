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
