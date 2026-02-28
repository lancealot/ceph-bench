#!/usr/bin/env python3

# ceph-cpu-io-sim.py - Ceph CPU IO Simulator
# Copyright (C) 2024
# License: GPL v3 (see LICENSE)

"""
Ceph CPU IO Simulator - Benchmarks CPU capacity for Ceph OSD workloads.

Measures how many OSDs a given CPU can support by running real Ceph-like
CPU operations (checksumming, compression, erasure coding, serialization,
metadata ops) and modeling the per-IO CPU cost at various drive speeds.

Works with zero optional dependencies (stdlib only) but produces more
accurate results when Ceph libraries are available.
"""

import argparse
import binascii
import csv
import ctypes
import ctypes.util
import hashlib
import json
import math
import multiprocessing
import os
import platform
import struct
import sys
import time
import zlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

VERSION = "1.0.0"

OBJECT_SIZES = {
    '4k': 4096,
    '8k': 8192,
    '16k': 16384,
    '32k': 32768,
    '64k': 65536,
    '128k': 131072,
    '256k': 262144,
    '512k': 524288,
    '1m': 1048576,
    '4m': 4194304,
    '16m': 16777216,
}

DRIVE_PROFILES = {
    'hdd': {
        'random_iops_min': 100,
        'random_iops_max': 200,
        'random_iops_typical': 150,
        'seq_throughput_mb': 175,
        'latency_ms': 5.0,
    },
    'ssd': {
        'random_iops_min': 10000,
        'random_iops_max': 100000,
        'random_iops_typical': 50000,
        'seq_throughput_mb': 550,
        'latency_ms': 0.1,
    },
    'nvme': {
        'random_iops_min': 100000,
        'random_iops_max': 1000000,
        'random_iops_typical': 500000,
        'seq_throughput_mb': 3500,
        'latency_ms': 0.02,
    },
}

BLUESTORE_OVERHEAD = {
    'wal_db_same_device': 1.15,
    'wal_db_separate': 1.05,
    'rocksdb_compaction': 1.10,
}

SCRUB_FREQUENCY_OVERHEAD = {
    'daily': 0.05,
    'weekly': 0.01,
    'disabled': 0.0,
}


# ---------------------------------------------------------------------------
# Library Detection and Wrapping
# ---------------------------------------------------------------------------

class LibraryManager:
    """Detects available Ceph-related libraries and provides unified wrappers."""

    def __init__(self):
        self.available = {}
        self._liblz4 = None
        self._libzstd = None
        self._libsnappy = None
        self._crc32c_fn = None
        self._ec_driver = None
        self._kv_store = {}
        self._detect_libraries()

    def _detect_libraries(self):
        self._detect_crc32c()
        self._detect_lz4()
        self._detect_zstd()
        self._detect_snappy()
        self._detect_erasure_coding()
        self._detect_rocksdb()
        self.available['zlib'] = 'stdlib'
        self.available['sha256'] = 'hashlib'

    # -- CRC32C --
    def _detect_crc32c(self):
        try:
            import crcmod
            self._crc32c_fn = crcmod.predefined.mkCrcFun('crc-32c')
            self.available['crc32c'] = 'crcmod'
            return
        except (ImportError, Exception):
            pass
        try:
            import crc32c as _crc32c
            self._crc32c_fn = _crc32c.crc32c
            self.available['crc32c'] = 'crc32c'
            return
        except (ImportError, Exception):
            pass
        self._crc32c_fn = zlib.crc32
        self.available['crc32c'] = 'zlib_crc32'

    # -- LZ4 --
    def _detect_lz4(self):
        try:
            import lz4.block
            self.available['lz4'] = 'python_lz4'
            return
        except (ImportError, Exception):
            pass
        try:
            path = ctypes.util.find_library('lz4')
            if path is None:
                for candidate in ['liblz4.so.1', 'liblz4.so', 'liblz4.dylib']:
                    try:
                        lib = ctypes.CDLL(candidate)
                        path = candidate
                        break
                    except OSError:
                        continue
            if path:
                lib = ctypes.CDLL(path)
                lib.LZ4_compress_default.argtypes = [
                    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
                lib.LZ4_compress_default.restype = ctypes.c_int
                lib.LZ4_compressBound.argtypes = [ctypes.c_int]
                lib.LZ4_compressBound.restype = ctypes.c_int
                lib.LZ4_decompress_safe.argtypes = [
                    ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_int]
                lib.LZ4_decompress_safe.restype = ctypes.c_int
                self._liblz4 = lib
                self.available['lz4'] = 'ctypes'
                return
        except (OSError, Exception):
            pass
        self.available['lz4'] = None

    # -- ZSTD --
    def _detect_zstd(self):
        try:
            import pyzstd
            self.available['zstd'] = 'pyzstd'
            return
        except (ImportError, Exception):
            pass
        try:
            import zstandard
            self.available['zstd'] = 'zstandard'
            return
        except (ImportError, Exception):
            pass
        try:
            path = ctypes.util.find_library('zstd')
            if path is None:
                for candidate in ['libzstd.so.1', 'libzstd.so', 'libzstd.dylib']:
                    try:
                        lib = ctypes.CDLL(candidate)
                        path = candidate
                        break
                    except OSError:
                        continue
            if path:
                lib = ctypes.CDLL(path)
                lib.ZSTD_compress.argtypes = [
                    ctypes.c_void_p, ctypes.c_size_t,
                    ctypes.c_void_p, ctypes.c_size_t, ctypes.c_int]
                lib.ZSTD_compress.restype = ctypes.c_size_t
                lib.ZSTD_compressBound.argtypes = [ctypes.c_size_t]
                lib.ZSTD_compressBound.restype = ctypes.c_size_t
                lib.ZSTD_decompress.argtypes = [
                    ctypes.c_void_p, ctypes.c_size_t,
                    ctypes.c_void_p, ctypes.c_size_t]
                lib.ZSTD_decompress.restype = ctypes.c_size_t
                lib.ZSTD_isError.argtypes = [ctypes.c_size_t]
                lib.ZSTD_isError.restype = ctypes.c_uint
                self._libzstd = lib
                self.available['zstd'] = 'ctypes'
                return
        except (OSError, Exception):
            pass
        self.available['zstd'] = None

    # -- Snappy --
    def _detect_snappy(self):
        try:
            import snappy
            self.available['snappy'] = 'python_snappy'
            return
        except (ImportError, Exception):
            pass
        try:
            path = ctypes.util.find_library('snappy')
            if path is None:
                for candidate in ['libsnappy.so.1', 'libsnappy.so', 'libsnappy.dylib']:
                    try:
                        lib = ctypes.CDLL(candidate)
                        path = candidate
                        break
                    except OSError:
                        continue
            if path:
                lib = ctypes.CDLL(path)
                self._libsnappy = lib
                self.available['snappy'] = 'ctypes'
                return
        except (OSError, Exception):
            pass
        self.available['snappy'] = None

    # -- Erasure Coding --
    def _detect_erasure_coding(self):
        try:
            from pyeclib.ec_iface import ECDriver
            self.available['erasure_coding'] = 'pyeclib'
            return
        except (ImportError, Exception):
            pass
        try:
            import reedsolo
            self.available['erasure_coding'] = 'reedsolo'
            return
        except (ImportError, Exception):
            pass
        self.available['erasure_coding'] = 'xor_simulation'

    # -- RocksDB --
    def _detect_rocksdb(self):
        try:
            import rocksdb
            self.available['rocksdb'] = 'python_rocksdb'
            return
        except (ImportError, Exception):
            pass
        try:
            import plyvel
            self.available['rocksdb'] = 'plyvel'
            return
        except (ImportError, Exception):
            pass
        self.available['rocksdb'] = 'dict_simulation'

    def summary(self) -> str:
        lines = ["=== Library Detection ==="]
        crc_label = {
            'crcmod': 'crcmod (hardware-accelerated CRC32C)',
            'crc32c': 'crc32c package (Castagnoli)',
            'zlib_crc32': 'zlib.crc32 (IEEE polynomial, ~same CPU cost)',
        }
        lines.append(f"CRC32C:        {crc_label.get(self.available.get('crc32c', ''), 'unknown')}")

        comp_parts = []
        for algo in ['lz4', 'zstd', 'snappy', 'zlib']:
            status = self.available.get(algo)
            if status is None:
                comp_parts.append(f"{algo}: NOT AVAILABLE")
            elif status == 'stdlib':
                comp_parts.append(f"{algo} (stdlib)")
            else:
                comp_parts.append(f"{algo} ({status})")
        lines.append(f"Compression:   {', '.join(comp_parts)}")

        ec_label = {
            'pyeclib': 'pyeclib (ISA-L/Jerasure)',
            'reedsolo': 'reedsolo (pure Python Reed-Solomon)',
            'xor_simulation': 'XOR simulation (install pyeclib for real RS)',
        }
        lines.append(f"Erasure Code:  {ec_label.get(self.available.get('erasure_coding', ''), 'unknown')}")

        kv_label = {
            'python_rocksdb': 'python-rocksdb (native)',
            'plyvel': 'plyvel (LevelDB)',
            'dict_simulation': 'dict simulation',
        }
        lines.append(f"RocksDB:       {kv_label.get(self.available.get('rocksdb', ''), 'unknown')}")
        lines.append(f"SHA256:        hashlib (OpenSSL-backed)")
        return '\n'.join(lines)

    def crc32c(self, data: bytes) -> int:
        return self._crc32c_fn(data)

    def compress(self, algorithm: str, data: bytes, level: int = -1) -> bytes:
        if algorithm == 'zlib':
            return zlib.compress(data, level if level >= 0 else 5)

        if algorithm == 'lz4':
            impl = self.available.get('lz4')
            if impl == 'python_lz4':
                import lz4.block
                return lz4.block.compress(data)
            elif impl == 'ctypes' and self._liblz4:
                src = data
                bound = self._liblz4.LZ4_compressBound(len(src))
                dst = ctypes.create_string_buffer(bound)
                result = self._liblz4.LZ4_compress_default(
                    src, dst, len(src), bound)
                if result <= 0:
                    raise RuntimeError("LZ4 compression failed")
                return dst.raw[:result]
            else:
                raise RuntimeError("lz4 not available")

        if algorithm == 'zstd':
            impl = self.available.get('zstd')
            if impl == 'pyzstd':
                import pyzstd
                return pyzstd.compress(data, level if level >= 0 else 1)
            elif impl == 'zstandard':
                import zstandard
                cctx = zstandard.ZstdCompressor(level=level if level >= 0 else 1)
                return cctx.compress(data)
            elif impl == 'ctypes' and self._libzstd:
                src = data
                bound = self._libzstd.ZSTD_compressBound(len(src))
                dst = ctypes.create_string_buffer(bound)
                result = self._libzstd.ZSTD_compress(
                    dst, bound, src, len(src), level if level >= 0 else 1)
                if self._libzstd.ZSTD_isError(result):
                    raise RuntimeError("ZSTD compression failed")
                return bytes(dst.raw[:result])
            else:
                raise RuntimeError("zstd not available")

        if algorithm == 'snappy':
            impl = self.available.get('snappy')
            if impl == 'python_snappy':
                import snappy
                return snappy.compress(data)
            elif impl == 'ctypes' and self._libsnappy:
                raise RuntimeError("snappy ctypes compress not fully implemented")
            else:
                raise RuntimeError("snappy not available")

        raise ValueError(f"Unknown compression algorithm: {algorithm}")

    def decompress(self, algorithm: str, data: bytes,
                   original_size: int = 0) -> bytes:
        if algorithm == 'zlib':
            return zlib.decompress(data)

        if algorithm == 'lz4':
            impl = self.available.get('lz4')
            if impl == 'python_lz4':
                import lz4.block
                return lz4.block.decompress(data, uncompressed_size=original_size)
            elif impl == 'ctypes' and self._liblz4:
                dst = ctypes.create_string_buffer(original_size)
                result = self._liblz4.LZ4_decompress_safe(
                    data, dst, len(data), original_size)
                if result < 0:
                    raise RuntimeError("LZ4 decompression failed")
                return dst.raw[:result]
            else:
                raise RuntimeError("lz4 not available")

        if algorithm == 'zstd':
            impl = self.available.get('zstd')
            if impl == 'pyzstd':
                import pyzstd
                return pyzstd.decompress(data)
            elif impl == 'zstandard':
                import zstandard
                dctx = zstandard.ZstdDecompressor()
                return dctx.decompress(data, max_output_size=original_size or
                                       len(data) * 20)
            elif impl == 'ctypes' and self._libzstd:
                out_size = original_size or len(data) * 10
                dst = ctypes.create_string_buffer(out_size)
                result = self._libzstd.ZSTD_decompress(
                    dst, out_size, data, len(data))
                if self._libzstd.ZSTD_isError(result):
                    raise RuntimeError("ZSTD decompression failed")
                return bytes(dst.raw[:result])
            else:
                raise RuntimeError("zstd not available")

        if algorithm == 'snappy':
            impl = self.available.get('snappy')
            if impl == 'python_snappy':
                import snappy
                return snappy.decompress(data)
            else:
                raise RuntimeError("snappy not available")

        raise ValueError(f"Unknown decompression algorithm: {algorithm}")

    def ec_encode(self, data: bytes, k: int, m: int) -> List[bytes]:
        impl = self.available.get('erasure_coding')
        if impl == 'pyeclib':
            from pyeclib.ec_iface import ECDriver
            if self._ec_driver is None:
                self._ec_driver = ECDriver(k=k, m=m,
                                           ec_type='liberasurecode_rs_vand')
            return self._ec_driver.encode(data)
        elif impl == 'reedsolo':
            return self._ec_encode_reedsolo(data, k, m)
        else:
            return self._ec_encode_xor(data, k, m)

    def ec_decode(self, chunks: List[bytes], k: int, m: int,
                  missing: List[int]) -> bytes:
        impl = self.available.get('erasure_coding')
        if impl == 'pyeclib':
            from pyeclib.ec_iface import ECDriver
            if self._ec_driver is None:
                self._ec_driver = ECDriver(k=k, m=m,
                                           ec_type='liberasurecode_rs_vand')
            available = [c for i, c in enumerate(chunks) if i not in missing]
            return self._ec_driver.decode(available)
        elif impl == 'reedsolo':
            return self._ec_decode_reedsolo(chunks, k, m, missing)
        else:
            return self._ec_decode_xor(chunks, k, m, missing)

    @staticmethod
    def _xor_blocks(a: bytes, b: bytes) -> bytes:
        """XOR two byte strings efficiently using array module."""
        import array
        # Process 8 bytes at a time using unsigned long long
        padded_len = (len(a) + 7) & ~7
        a_padded = a.ljust(padded_len, b'\x00')
        b_padded = b.ljust(padded_len, b'\x00')
        a_arr = array.array('Q')
        a_arr.frombytes(a_padded)
        b_arr = array.array('Q')
        b_arr.frombytes(b_padded)
        result = array.array('Q', (x ^ y for x, y in zip(a_arr, b_arr)))
        return result.tobytes()[:len(a)]

    def _ec_encode_xor(self, data: bytes, k: int, m: int) -> List[bytes]:
        chunk_size = len(data) // k
        remainder = len(data) % k
        if remainder != 0:
            data += b'\x00' * (k - remainder)
            chunk_size = len(data) // k
        chunks = [data[i * chunk_size:(i + 1) * chunk_size] for i in range(k)]
        parity_chunks = []
        for p in range(m):
            parity = b'\x00' * chunk_size
            for chunk in chunks:
                parity = self._xor_blocks(parity, chunk)
            # For additional parity chunks, rotate data to simulate
            # Galois field multiplication cost
            if p > 0:
                for i, chunk in enumerate(chunks):
                    shift = (p * i * 8) % (chunk_size * 8)
                    shift_bytes = shift // 8
                    if shift_bytes > 0 and shift_bytes < chunk_size:
                        rotated = chunk[shift_bytes:] + chunk[:shift_bytes]
                        parity = self._xor_blocks(parity, rotated)
            parity_chunks.append(parity)
        return chunks + parity_chunks

    def _ec_decode_xor(self, chunks: List[bytes], k: int, m: int,
                       missing: List[int]) -> bytes:
        chunk_size = len(chunks[0]) if chunks else 0
        if len(missing) == 0:
            return b''.join(chunks[:k])
        if len(missing) == 1 and missing[0] < k:
            data_indices = [i for i in range(k) if i != missing[0]]
            parity = chunks[k] if k < len(chunks) else b'\x00' * chunk_size
            recovered = parity
            for idx in data_indices:
                if idx < len(chunks):
                    recovered = self._xor_blocks(recovered, chunks[idx])
            result_chunks = []
            for i in range(k):
                if i == missing[0]:
                    result_chunks.append(recovered)
                else:
                    result_chunks.append(chunks[i])
            return b''.join(result_chunks)
        return b''.join(chunks[:k])

    def _ec_encode_reedsolo(self, data: bytes, k: int, m: int) -> List[bytes]:
        import reedsolo
        rs = reedsolo.RSCodec(m)
        chunk_size = len(data) // k
        if len(data) % k != 0:
            data += b'\x00' * (k - len(data) % k)
            chunk_size = len(data) // k
        chunks = [data[i * chunk_size:(i + 1) * chunk_size] for i in range(k)]
        parity_chunks = []
        for p in range(m):
            parity = bytearray(chunk_size)
            for i, chunk in enumerate(chunks):
                encoded = rs.encode(chunk)
                parity_part = encoded[len(chunk):]
                if p < len(parity_part):
                    for j in range(chunk_size):
                        parity[j] ^= parity_part[j % len(parity_part)]
            parity_chunks.append(bytes(parity))
        return chunks + parity_chunks

    def _ec_decode_reedsolo(self, chunks: List[bytes], k: int, m: int,
                            missing: List[int]) -> bytes:
        return self._ec_decode_xor(chunks, k, m, missing)


# ---------------------------------------------------------------------------
# Data Classes
# ---------------------------------------------------------------------------

@dataclass
class BenchmarkResult:
    """Result of a single benchmark test."""
    operation: str
    object_size: int
    ops_per_sec: float
    cpu_time_per_op_us: float
    throughput_mbps: float
    cpu_utilization: float
    iterations: int
    elapsed_sec: float
    library_used: str
    notes: str = ''


@dataclass
class ClusterConfig:
    """Represents the user's theoretical cluster configuration."""
    cpu_cores: int = 0
    cpu_cores_for_ceph: float = 0.0
    cpu_model: str = ''

    drive_type: str = 'hdd'
    drive_count: int = 12
    drive_iops: int = 0
    drive_throughput_mb: int = 0

    protection_type: str = 'replicated'
    replica_count: int = 3
    ec_k: int = 4
    ec_m: int = 2

    compression_enabled: bool = False
    compression_algorithm: str = 'zstd'
    compression_ratio: float = 0.5
    compression_mode: str = 'passive'

    wal_db_separate: bool = False

    object_size: str = '4m'
    workload_pattern: str = 'mixed'
    read_write_ratio: float = 0.7

    scrub_frequency: str = 'daily'

    scenario: str = 'typical'

    benchmark_duration: float = 5.0
    object_sizes_to_test: List[str] = field(
        default_factory=lambda: ['4k', '64k', '128k', '4m'])

    def get_object_size_bytes(self) -> int:
        return OBJECT_SIZES.get(self.object_size, 4194304)

    def get_drive_iops(self) -> int:
        if self.drive_iops > 0:
            return self.drive_iops
        profile = DRIVE_PROFILES[self.drive_type]
        if self.scenario == 'best':
            return profile['random_iops_min']
        elif self.scenario == 'worst':
            return profile['random_iops_max']
        else:
            return profile['random_iops_typical']

    def get_drive_throughput_mb(self) -> int:
        if self.drive_throughput_mb > 0:
            return self.drive_throughput_mb
        return DRIVE_PROFILES[self.drive_type]['seq_throughput_mb']


# ---------------------------------------------------------------------------
# CPU Micro-Benchmarks
# ---------------------------------------------------------------------------

class CephBenchmarks:
    """Runs CPU micro-benchmarks simulating Ceph OSD operations."""

    def __init__(self, libs: LibraryManager, config: ClusterConfig,
                 verbose: bool = False):
        self.libs = libs
        self.config = config
        self.verbose = verbose
        self.results: List[BenchmarkResult] = []

    def run_all(self) -> List[BenchmarkResult]:
        self.results = []
        for size_name in self.config.object_sizes_to_test:
            size_bytes = OBJECT_SIZES[size_name]
            data = os.urandom(size_bytes)

            self._print_progress(f"  CRC32C @ {size_name}...")
            self._bench_crc32c(data, size_name)

            self._print_progress(f"  SHA256 @ {size_name}...")
            self._bench_sha256(data, size_name)

            if self.config.compression_enabled:
                for algo in self._get_available_compression_algos():
                    self._print_progress(f"  Compress {algo} @ {size_name}...")
                    self._bench_compress(data, size_name, algo)
                    self._print_progress(f"  Decompress {algo} @ {size_name}...")
                    self._bench_decompress(data, size_name, algo)

            if self.config.protection_type == 'erasure':
                self._print_progress(
                    f"  EC encode {self.config.ec_k}+{self.config.ec_m} "
                    f"@ {size_name}...")
                self._bench_ec_encode(data, size_name)
                self._print_progress(
                    f"  EC decode {self.config.ec_k}+{self.config.ec_m} "
                    f"@ {size_name}...")
                self._bench_ec_decode(data, size_name)

            self._print_progress(f"  Serialization @ {size_name}...")
            self._bench_serialization(data, size_name)

            self._print_progress(f"  RocksDB sim @ {size_name}...")
            self._bench_rocksdb_sim(size_name)

        self._print_progress("  CRUSH calculation...")
        self._bench_crush_calculation()

        return self.results

    def _print_progress(self, msg: str):
        if self.verbose:
            print(msg, flush=True)

    def _get_available_compression_algos(self) -> List[str]:
        algo = self.config.compression_algorithm
        if self.libs.available.get(algo) is not None:
            return [algo]
        available = []
        for a in ['lz4', 'zstd', 'snappy', 'zlib']:
            if self.libs.available.get(a) is not None:
                available.append(a)
        if available:
            print(f"Warning: {algo} not available, using: {available[0]}")
            return [available[0]]
        return ['zlib']

    def _calibrate_iterations(self, func, target_seconds=None) -> int:
        target = target_seconds or self.config.benchmark_duration
        warmup = 3
        for _ in range(warmup):
            func()
        start = time.perf_counter()
        for _ in range(10):
            func()
        elapsed = time.perf_counter() - start
        per_op = elapsed / 10
        if per_op <= 0:
            return 1000000
        return max(100, int(target / per_op))

    def _run_timed(self, name: str, func, object_size: int,
                   library_used: str, notes: str = '') -> BenchmarkResult:
        iterations = self._calibrate_iterations(func)

        start_cpu = time.process_time()
        start_wall = time.perf_counter()
        for _ in range(iterations):
            func()
        end_wall = time.perf_counter()
        end_cpu = time.process_time()

        elapsed = end_wall - start_wall
        cpu_time = end_cpu - start_cpu

        if elapsed <= 0:
            elapsed = 1e-9
        if cpu_time <= 0:
            cpu_time = 1e-9

        ops_per_sec = iterations / elapsed
        cpu_time_per_op = (cpu_time / iterations) * 1e6
        throughput = (object_size * iterations / elapsed / (1024 * 1024)
                      if object_size > 0 else 0.0)
        cpu_util = cpu_time / elapsed

        result = BenchmarkResult(
            operation=name,
            object_size=object_size,
            ops_per_sec=ops_per_sec,
            cpu_time_per_op_us=cpu_time_per_op,
            throughput_mbps=throughput,
            cpu_utilization=cpu_util,
            iterations=iterations,
            elapsed_sec=elapsed,
            library_used=library_used,
            notes=notes,
        )
        self.results.append(result)
        return result

    def _bench_crc32c(self, data: bytes, size_name: str):
        def op():
            self.libs.crc32c(data)
        self._run_timed(
            f'crc32c_{size_name}', op, len(data),
            self.libs.available.get('crc32c', 'zlib'))

    def _bench_sha256(self, data: bytes, size_name: str):
        def op():
            hashlib.sha256(data).digest()
        self._run_timed(
            f'sha256_{size_name}', op, len(data), 'hashlib',
            notes='deep scrub verification')

    def _bench_compress(self, data: bytes, size_name: str, algo: str):
        comp_data = self._generate_compressible_data(
            len(data), self.config.compression_ratio)
        level = {'zstd': 1, 'zlib': 5, 'lz4': -1, 'snappy': -1}.get(algo, -1)

        def op():
            self.libs.compress(algo, comp_data, level)

        self._run_timed(
            f'compress_{algo}_{size_name}', op, len(comp_data),
            self.libs.available.get(algo, 'unknown'),
            notes=f'ratio={self.config.compression_ratio}')

    def _bench_decompress(self, data: bytes, size_name: str, algo: str):
        comp_data = self._generate_compressible_data(
            len(data), self.config.compression_ratio)
        level = {'zstd': 1, 'zlib': 5, 'lz4': -1, 'snappy': -1}.get(algo, -1)
        try:
            compressed = self.libs.compress(algo, comp_data, level)
        except Exception:
            return
        original_size = len(comp_data)

        def op():
            self.libs.decompress(algo, compressed, original_size)

        self._run_timed(
            f'decompress_{algo}_{size_name}', op, original_size,
            self.libs.available.get(algo, 'unknown'))

    def _bench_ec_encode(self, data: bytes, size_name: str):
        k, m = self.config.ec_k, self.config.ec_m

        def op():
            self.libs.ec_encode(data, k, m)

        self._run_timed(
            f'ec_encode_{k}_{m}_{size_name}', op, len(data),
            self.libs.available.get('erasure_coding', 'xor'),
            notes=f'k={k} m={m}')

    def _bench_ec_decode(self, data: bytes, size_name: str):
        k, m = self.config.ec_k, self.config.ec_m
        chunks = self.libs.ec_encode(data, k, m)
        missing = [k - 1]

        def op():
            self.libs.ec_decode(chunks, k, m, missing)

        self._run_timed(
            f'ec_decode_{k}_{m}_{size_name}', op, len(data),
            self.libs.available.get('erasure_coding', 'xor'),
            notes=f'k={k} m={m}, 1 missing chunk')

    def _bench_serialization(self, data: bytes, size_name: str):
        crc_fn = self.libs.crc32c

        def op():
            header = struct.pack('<IIQQQII',
                                 0x0001, len(data), 0x12345678,
                                 0, len(data), 42, 0)
            crc_fn(header)
            crc_fn(data)

        self._run_timed(
            f'serialization_{size_name}', op, len(data),
            self.libs.available.get('crc32c', 'zlib'),
            notes='replication message encoding')

    def _bench_rocksdb_sim(self, size_name: str):
        KV_OPS_PER_IO = 4
        obj_size = OBJECT_SIZES[size_name]
        store = {}
        counter = [0]

        def op():
            for i in range(KV_OPS_PER_IO):
                k = struct.pack('>QQ', counter[0], i)
                v = struct.pack('<QQII',
                                counter[0] * obj_size, obj_size,
                                0, zlib.crc32(k))
                store[k] = v
            counter[0] += 1

        self._run_timed(
            f'rocksdb_sim_{size_name}', op, 0,
            self.libs.available.get('rocksdb', 'dict_simulation'),
            notes=f'{KV_OPS_PER_IO} KV ops per IO')

    def _bench_crush_calculation(self):
        num_osds = max(self.config.drive_count * 8, 64)
        if self.config.protection_type == 'replicated':
            placements = self.config.replica_count
        else:
            placements = self.config.ec_k + self.config.ec_m

        seed_data = os.urandom(4096)
        seed_offset = [0]

        def op():
            off = seed_offset[0] % (len(seed_data) - 4)
            seed_offset[0] += 4
            pg_id = struct.unpack_from('<I', seed_data, off)[0]
            selected = []
            for rep in range(placements):
                best_osd = -1
                best_hash = -1
                for osd in range(num_osds):
                    h = zlib.crc32(struct.pack('<III', pg_id, osd, rep))
                    if h > best_hash:
                        best_hash = h
                        best_osd = osd
                selected.append(best_osd)

        self._run_timed(
            'crush_calculation', op, 0, 'simulation',
            notes=f'{placements} placements across {num_osds} OSDs')

    @staticmethod
    def _generate_compressible_data(size: int, ratio: float) -> bytes:
        random_fraction = min(ratio * 1.5, 1.0)
        random_bytes = int(size * random_fraction)
        pattern_bytes = size - random_bytes
        return os.urandom(random_bytes) + (b'\x00' * pattern_bytes)


# ---------------------------------------------------------------------------
# OSD Capacity Model
# ---------------------------------------------------------------------------

class OSDCapacityModel:
    """Calculates how many OSDs a CPU can support based on benchmark results."""

    def __init__(self, config: ClusterConfig, results: List[BenchmarkResult]):
        self.config = config
        self.results = results
        self._cpu_costs: Dict[str, float] = {}

    def calculate(self) -> Dict[str, Any]:
        self._compute_per_io_cpu_cost()

        total_cpu_us_per_io = self._total_cpu_cost_per_io()
        available_cpu_us = self.config.cpu_cores_for_ceph * 1_000_000
        drive_iops = self.config.get_drive_iops()

        if total_cpu_us_per_io <= 0:
            total_cpu_us_per_io = 1.0

        cpu_us_per_osd_per_sec = total_cpu_us_per_io * drive_iops

        if cpu_us_per_osd_per_sec > 0:
            max_osds = available_cpu_us / cpu_us_per_osd_per_sec
        else:
            max_osds = float('inf')

        overhead = self._compute_overhead_multiplier()
        max_osds_adjusted = max_osds / overhead

        return {
            'max_osds_raw': max_osds,
            'max_osds_adjusted': math.floor(max_osds_adjusted),
            'cpu_us_per_io': total_cpu_us_per_io,
            'cpu_us_per_osd_per_sec': cpu_us_per_osd_per_sec,
            'overhead_multiplier': overhead,
            'drive_iops': drive_iops,
            'available_cpu_us': available_cpu_us,
            'per_operation_costs': dict(self._cpu_costs),
            'headroom_percentage': self._compute_headroom(max_osds_adjusted),
        }

    def _compute_per_io_cpu_cost(self):
        obj_size = self.config.get_object_size_bytes()
        size_name = self.config.object_size
        for r in self.results:
            key = r.operation
            if size_name in key or r.object_size == obj_size:
                base = key
                for sn in OBJECT_SIZES:
                    base = base.replace(f'_{sn}', '')
                if base not in self._cpu_costs:
                    self._cpu_costs[base] = r.cpu_time_per_op_us
            elif r.object_size == 0:
                self._cpu_costs[r.operation] = r.cpu_time_per_op_us

    def _get_cost(self, prefix: str) -> float:
        for key, val in self._cpu_costs.items():
            if key.startswith(prefix):
                return val
        return 0.0

    def _total_cpu_cost_per_io(self) -> float:
        crc_cost = self._get_cost('crc32c')

        comp_cost = 0.0
        if self.config.compression_enabled:
            comp_prob = {'passive': 0.3, 'aggressive': 0.7, 'force': 1.0}
            p = comp_prob.get(self.config.compression_mode, 0.3)
            rw = self.config.read_write_ratio
            algo = self.config.compression_algorithm
            compress_c = self._get_cost(f'compress_{algo}')
            decompress_c = self._get_cost(f'decompress_{algo}')
            comp_cost = ((1 - rw) * p * compress_c +
                         rw * p * decompress_c)

        params = self._get_scenario_params()

        if self.config.protection_type == 'erasure':
            rw = self.config.read_write_ratio
            ec_encode_c = self._get_cost('ec_encode')
            ec_decode_c = self._get_cost('ec_decode')
            ec_cost = ((1 - rw) * ec_encode_c +
                       ec_decode_c * params['ec_recovery_fraction'])
        else:
            serial_c = self._get_cost('serialization')
            ec_cost = (self.config.replica_count - 1) * serial_c

        rocksdb_cost = self._get_cost('rocksdb_sim')
        rocksdb_cost *= params['kv_ops_per_io'] / 4.0

        crush_cost = self._get_cost('crush_calculation')

        scrub_overhead = SCRUB_FREQUENCY_OVERHEAD.get(
            self.config.scrub_frequency, 0.01)
        sha256_cost = self._get_cost('sha256') * scrub_overhead

        total = (crc_cost + comp_cost + ec_cost + rocksdb_cost +
                 crush_cost + sha256_cost)

        self._cpu_costs['_total'] = total
        self._cpu_costs['_crc32c_weighted'] = crc_cost
        self._cpu_costs['_compression_weighted'] = comp_cost
        self._cpu_costs['_protection_weighted'] = ec_cost
        self._cpu_costs['_rocksdb_weighted'] = rocksdb_cost
        self._cpu_costs['_crush_weighted'] = crush_cost
        self._cpu_costs['_scrub_weighted'] = sha256_cost

        return total

    def _get_scenario_params(self) -> dict:
        if self.config.scenario == 'best':
            return {
                'kv_ops_per_io': 3,
                'context_switch_overhead': 1.05,
                'safety_margin': 1.00,
                'ec_recovery_fraction': 0.001,
            }
        elif self.config.scenario == 'worst':
            return {
                'kv_ops_per_io': 6,
                'context_switch_overhead': 1.20,
                'safety_margin': 1.30,
                'ec_recovery_fraction': 0.05,
            }
        else:
            return {
                'kv_ops_per_io': 4,
                'context_switch_overhead': 1.10,
                'safety_margin': 1.15,
                'ec_recovery_fraction': 0.01,
            }

    def _compute_overhead_multiplier(self) -> float:
        params = self._get_scenario_params()
        overhead = 1.0

        if self.config.wal_db_separate:
            overhead *= BLUESTORE_OVERHEAD['wal_db_separate']
        else:
            overhead *= BLUESTORE_OVERHEAD['wal_db_same_device']

        overhead *= BLUESTORE_OVERHEAD['rocksdb_compaction']
        overhead *= params['context_switch_overhead']
        overhead *= params['safety_margin']
        return overhead

    def _compute_headroom(self, max_osds: float) -> float:
        if max_osds <= 0:
            return 0.0
        used_fraction = self.config.drive_count / max_osds
        return max(0.0, (1.0 - used_fraction) * 100)


# ---------------------------------------------------------------------------
# Scale-Out Projection
# ---------------------------------------------------------------------------

class ScaleOutProjection:
    """Projects performance across multiple nodes."""

    NODE_COUNTS = [1, 2, 4, 8, 16, 32, 64]

    def __init__(self, config: ClusterConfig, capacity: Dict[str, Any]):
        self.config = config
        self.capacity = capacity

    def project(self) -> List[Dict[str, Any]]:
        rows = []
        for n in self.NODE_COUNTS:
            rows.append(self._project_for_nodes(n))
        return rows

    def _project_for_nodes(self, node_count: int) -> Dict[str, Any]:
        osds_per_node = min(self.config.drive_count,
                            max(self.capacity['max_osds_adjusted'], 0))
        total_osds = osds_per_node * node_count
        drive_iops = self.config.get_drive_iops()

        total_iops = total_osds * drive_iops

        if self.config.protection_type == 'replicated':
            write_amplification = self.config.replica_count
        else:
            write_amplification = ((self.config.ec_k + self.config.ec_m) /
                                   self.config.ec_k)

        rw = self.config.read_write_ratio
        denominator = rw * 1.0 + (1 - rw) * write_amplification
        if denominator <= 0:
            denominator = 1.0
        effective_iops = total_iops / denominator

        network_efficiency = 1.0 - (0.02 * math.log2(max(node_count, 1)))
        network_efficiency = max(network_efficiency, 0.80)
        if node_count <= 1:
            network_efficiency = 1.0

        effective_iops *= network_efficiency

        obj_size = self.config.get_object_size_bytes()
        throughput_mbps = effective_iops * obj_size / (1024 * 1024)

        return {
            'nodes': node_count,
            'osds_per_node': osds_per_node,
            'total_osds': total_osds,
            'raw_iops': total_iops,
            'effective_iops': int(effective_iops),
            'throughput_mbps': throughput_mbps,
            'network_efficiency': network_efficiency,
            'cpu_limited': (self.config.drive_count >
                            self.capacity['max_osds_adjusted']),
        }


# ---------------------------------------------------------------------------
# Report Generator
# ---------------------------------------------------------------------------

def bytes_to_human_readable(bytes_value: float) -> str:
    """Convert bytes/sec to human readable format."""
    units = ['B/s', 'KB/s', 'MB/s', 'GB/s', 'TB/s']
    index = 0
    while bytes_value >= 1024 and index < len(units) - 1:
        bytes_value /= 1024
        index += 1
    return f"{bytes_value:.2f} {units[index]}"


class ReportGenerator:
    """Formats and outputs results in text and CSV."""

    def __init__(self, config: ClusterConfig, libs: LibraryManager,
                 bench_results: List[BenchmarkResult],
                 capacity: Dict[str, Any],
                 scale_out: List[Dict[str, Any]]):
        self.config = config
        self.libs = libs
        self.bench_results = bench_results
        self.capacity = capacity
        self.scale_out = scale_out

    def print_report(self):
        self._print_header()
        self._print_system_info()
        print()
        print(self.libs.summary())
        print()
        self._print_config_summary()
        self._print_benchmark_results()
        self._print_cpu_cost_breakdown()
        self._print_capacity_estimate()
        self._print_scale_out_table()
        self._print_recommendations()

    def _print_header(self):
        print()
        print("=" * 60)
        print("  Ceph CPU IO Simulator Report")
        print("=" * 60)
        print(f"Date:    {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"Version: {VERSION}")

    def _print_system_info(self):
        print()
        print("=== System Information ===")
        print(f"CPU Model:    {self.config.cpu_model or 'Unknown'}")
        print(f"CPU Cores:    {self.config.cpu_cores} "
              f"({self.config.cpu_cores_for_ceph:.0f} allocated to Ceph)")
        print(f"Architecture: {platform.machine()}")
        print(f"Python:       {platform.python_version()}")
        print(f"OS:           {platform.system()} {platform.release()}")

    def _print_config_summary(self):
        print("=== Cluster Configuration ===")
        print(f"Drive Type:   {self.config.drive_type.upper()}")
        print(f"Drive Count:  {self.config.drive_count} per node")
        print(f"Drive IOPS:   {self.config.get_drive_iops():,} "
              f"({self.config.scenario} profile)")

        if self.config.protection_type == 'replicated':
            print(f"Protection:   Replicated x{self.config.replica_count}")
        else:
            print(f"Protection:   EC {self.config.ec_k}+{self.config.ec_m}")

        if self.config.compression_enabled:
            print(f"Compression:  {self.config.compression_algorithm} "
                  f"({self.config.compression_mode}, "
                  f"est. ratio {self.config.compression_ratio:.2f})")
        else:
            print("Compression:  Disabled")

        print(f"WAL/DB:       {'Separate device' if self.config.wal_db_separate else 'Same device'}")
        print(f"Object Size:  {self.config.object_size}")
        rw = self.config.read_write_ratio
        print(f"Workload:     {self.config.workload_pattern.capitalize()} "
              f"({rw*100:.0f}% read / {(1-rw)*100:.0f}% write)")
        print(f"Scrub:        {self.config.scrub_frequency.capitalize()}")
        print(f"Scenario:     {self.config.scenario.capitalize()}")

    def _print_benchmark_results(self):
        print()
        print("=== Benchmark Results ===")
        header = (f"{'Operation':<35} {'Size':>6} {'Ops/sec':>12} "
                  f"{'CPU us/op':>12} {'Throughput':>14} {'Library':<18}")
        print(header)
        print("-" * len(header))

        for r in self.bench_results:
            size_str = _format_size(r.object_size) if r.object_size > 0 else '--'
            tp_str = (f"{r.throughput_mbps:,.1f} MB/s"
                      if r.throughput_mbps > 0 else 'N/A')
            print(f"{r.operation:<35} {size_str:>6} {r.ops_per_sec:>12,.0f} "
                  f"{r.cpu_time_per_op_us:>12,.2f} {tp_str:>14} "
                  f"{r.library_used:<18}")

    def _print_cpu_cost_breakdown(self):
        print()
        print(f"=== CPU Cost Per IO "
              f"(at {self.config.object_size} object size, "
              f"{self.config.scenario} scenario) ===")
        costs = self.capacity.get('per_operation_costs', {})

        rows = [
            ('CRC32C', costs.get('_crc32c_weighted', 0)),
            ('Compression', costs.get('_compression_weighted', 0)),
            ('Data Protection', costs.get('_protection_weighted', 0)),
            ('RocksDB metadata', costs.get('_rocksdb_weighted', 0)),
            ('CRUSH lookup', costs.get('_crush_weighted', 0)),
            ('Scrub (amortized)', costs.get('_scrub_weighted', 0)),
        ]

        header = f"{'Component':<25} {'Weighted CPU us/IO':>20}"
        print(header)
        print("-" * len(header))
        total = 0.0
        for name, val in rows:
            print(f"{name:<25} {val:>20,.2f}")
            total += val
        print("-" * len(header))
        print(f"{'TOTAL':<25} {total:>20,.2f}")

    def _print_capacity_estimate(self):
        cap = self.capacity
        print()
        print("=== OSD Capacity Estimate ===")
        print(f"Available CPU:         "
              f"{cap['available_cpu_us']:,.0f} us/sec "
              f"({self.config.cpu_cores_for_ceph:.0f} cores)")
        print(f"Drive IOPS:            "
              f"{cap['drive_iops']:,} "
              f"({self.config.drive_type.upper()} {self.config.scenario})")
        print(f"CPU per IO:            {cap['cpu_us_per_io']:,.2f} us")
        print(f"CPU per OSD per sec:   {cap['cpu_us_per_osd_per_sec']:,.0f} us")
        print(f"Overhead multiplier:   {cap['overhead_multiplier']:.2f}x")
        print(f"Max OSDs (raw):        {cap['max_osds_raw']:.2f}")
        print(f"Max OSDs (adjusted):   {cap['max_osds_adjusted']}")
        print(f"Drives configured:     {self.config.drive_count}")

        headroom = cap['headroom_percentage']
        if headroom > 0:
            print(f"CPU headroom:          {headroom:.1f}%")
        else:
            over = ((self.config.drive_count /
                     max(cap['max_osds_adjusted'], 0.01) - 1) * 100)
            print(f"CPU headroom:          NEGATIVE "
                  f"({over:.0f}% overprovisioned)")

        if cap['max_osds_adjusted'] < self.config.drive_count:
            print()
            print("!! WARNING: CPU cannot sustain all configured drives "
                  "at expected IOPS.")
            print("   Drives will be throttled by CPU capacity.")

    def _print_scale_out_table(self):
        print()
        print("=== Scale-Out Projection ===")
        header = (f"{'Nodes':>5} {'OSDs/Node':>10} {'Total OSDs':>11} "
                  f"{'Raw IOPS':>14} {'Eff. IOPS':>14} "
                  f"{'Throughput':>14} {'CPU Ltd':>8}")
        print(header)
        print("-" * len(header))

        for row in self.scale_out:
            tp = _format_throughput(row['throughput_mbps'])
            cpu_ltd = 'YES' if row['cpu_limited'] else 'no'
            print(f"{row['nodes']:>5} {row['osds_per_node']:>10} "
                  f"{row['total_osds']:>11} {row['raw_iops']:>14,} "
                  f"{row['effective_iops']:>14,} {tp:>14} {cpu_ltd:>8}")

    def _print_recommendations(self):
        cap = self.capacity
        print()
        print("=== Recommendations ===")

        max_osds = cap['max_osds_adjusted']
        drives = self.config.drive_count

        if max_osds >= drives * 2:
            print("CPU has ample headroom for this configuration.")
            print("Consider adding more drives or enabling compression "
                  "for better utilization.")
        elif max_osds >= drives:
            headroom = cap['headroom_percentage']
            print(f"CPU can support all {drives} drives with "
                  f"{headroom:.0f}% headroom.")
            if headroom < 20:
                print("Headroom is limited. Monitor CPU usage under load.")
        elif max_osds > 0:
            print(f"CPU can only support {max_osds} of {drives} drives.")
            print("Consider:")
            if self.config.compression_enabled:
                print("  - Disabling or reducing compression")
            if self.config.drive_type == 'nvme':
                print("  - Using fewer NVMe drives per node")
                print("  - Adding more CPU cores")
            if not self.config.wal_db_separate:
                print("  - Moving WAL/DB to a separate fast device")
            print("  - Using larger RADOS object sizes to reduce per-IO "
                  "overhead")
        else:
            print(f"CPU BOTTLENECK: Cannot sustain even 1 OSD at "
                  f"{cap['drive_iops']:,} IOPS.")
            print("This typically occurs with NVMe drives at full speed.")
            print("Real-world Ceph deployments throttle NVMe to match "
                  "CPU capacity.")
            print("Consider:")
            print("  - Reducing drive IOPS expectation "
                  "(use --drive-iops to set realistic target)")
            print("  - Adding significantly more CPU cores")
            print("  - Using larger RADOS object sizes")

    def export_csv(self, filepath: str):
        bench_path = filepath.replace('.csv', '_benchmarks.csv')
        cap_path = filepath.replace('.csv', '_capacity.csv')

        ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        with open(bench_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'operation', 'object_size', 'ops_per_sec',
                'cpu_time_per_op_us', 'throughput_mbps', 'cpu_utilization',
                'library_used', 'scenario', 'notes'])
            for r in self.bench_results:
                writer.writerow([
                    ts, r.operation, r.object_size, f'{r.ops_per_sec:.2f}',
                    f'{r.cpu_time_per_op_us:.2f}', f'{r.throughput_mbps:.2f}',
                    f'{r.cpu_utilization:.4f}', r.library_used,
                    self.config.scenario, r.notes])
        print(f"\nBenchmark results saved to: {bench_path}")

        with open(cap_path, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                'timestamp', 'scenario', 'drive_type', 'drive_count',
                'drive_iops', 'protection', 'compression', 'object_size',
                'cpu_us_per_io', 'max_osds_raw', 'max_osds_adjusted',
                'overhead_multiplier', 'cpu_headroom_pct'])
            prot = (f'replicated:{self.config.replica_count}'
                    if self.config.protection_type == 'replicated'
                    else f'ec:{self.config.ec_k}+{self.config.ec_m}')
            comp = (self.config.compression_algorithm
                    if self.config.compression_enabled else 'none')
            cap = self.capacity
            writer.writerow([
                ts, self.config.scenario, self.config.drive_type,
                self.config.drive_count, cap['drive_iops'], prot, comp,
                self.config.object_size, f"{cap['cpu_us_per_io']:.2f}",
                f"{cap['max_osds_raw']:.2f}", cap['max_osds_adjusted'],
                f"{cap['overhead_multiplier']:.2f}",
                f"{cap['headroom_percentage']:.1f}"])
        print(f"Capacity estimates saved to: {cap_path}")

    def to_json(self) -> str:
        data = {
            'version': VERSION,
            'timestamp': datetime.now().isoformat(),
            'system': {
                'cpu_model': self.config.cpu_model,
                'cpu_cores': self.config.cpu_cores,
                'cpu_cores_for_ceph': self.config.cpu_cores_for_ceph,
                'architecture': platform.machine(),
                'python_version': platform.python_version(),
            },
            'config': {
                'drive_type': self.config.drive_type,
                'drive_count': self.config.drive_count,
                'drive_iops': self.config.get_drive_iops(),
                'protection_type': self.config.protection_type,
                'replica_count': self.config.replica_count,
                'ec_k': self.config.ec_k,
                'ec_m': self.config.ec_m,
                'compression_enabled': self.config.compression_enabled,
                'compression_algorithm': self.config.compression_algorithm,
                'object_size': self.config.object_size,
                'scenario': self.config.scenario,
            },
            'benchmarks': [
                {
                    'operation': r.operation,
                    'object_size': r.object_size,
                    'ops_per_sec': round(r.ops_per_sec, 2),
                    'cpu_time_per_op_us': round(r.cpu_time_per_op_us, 2),
                    'throughput_mbps': round(r.throughput_mbps, 2),
                    'library_used': r.library_used,
                }
                for r in self.bench_results
            ],
            'capacity': {
                'max_osds_raw': round(self.capacity['max_osds_raw'], 2),
                'max_osds_adjusted': self.capacity['max_osds_adjusted'],
                'cpu_us_per_io': round(self.capacity['cpu_us_per_io'], 2),
                'overhead_multiplier': round(
                    self.capacity['overhead_multiplier'], 2),
                'headroom_percentage': round(
                    self.capacity['headroom_percentage'], 1),
            },
            'scale_out': self.scale_out,
        }
        return json.dumps(data, indent=2)


# ---------------------------------------------------------------------------
# Comparison with Real Benchmarks
# ---------------------------------------------------------------------------

def compare_with_real(csv_path: str, config: ClusterConfig,
                      capacity: Dict[str, Any]):
    """Compare simulation with real ceph-bench.sh results."""
    print()
    print("=" * 60)
    print("  Comparison with Real Benchmark Results")
    print("=" * 60)
    print(f"Source: {csv_path}")

    try:
        rows = []
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

        if not rows:
            print("No data found in CSV file.")
            return

        device_classes = {}
        for row in rows:
            dc = row.get('device_class', 'unknown')
            if dc not in device_classes:
                device_classes[dc] = {'iops': [], 'throughput': [],
                                      'osds': set()}
            try:
                device_classes[dc]['iops'].append(float(row.get('iops', 0)))
                device_classes[dc]['throughput'].append(
                    float(row.get('bytes_per_sec', 0)))
                device_classes[dc]['osds'].add(row.get('osd_id', ''))
            except (ValueError, TypeError):
                continue

        print()
        print(f"{'Device Class':<15} {'Avg IOPS':>12} "
              f"{'Avg Throughput':>16} {'OSD Count':>10}")
        print("-" * 55)

        for dc, data in sorted(device_classes.items()):
            if not data['iops']:
                continue
            avg_iops = sum(data['iops']) / len(data['iops'])
            avg_tp = sum(data['throughput']) / len(data['throughput'])
            osd_count = len(data['osds'])
            print(f"{dc:<15} {avg_iops:>12,.0f} "
                  f"{bytes_to_human_readable(avg_tp):>16} {osd_count:>10}")

        print()
        print("Simulated CPU capacity at real IOPS levels:")
        print(f"{'Device Class':<15} {'Real IOPS/OSD':>14} "
              f"{'CPU us/IO (sim)':>16} {'Max OSDs (sim)':>15}")
        print("-" * 62)

        cpu_us_per_io = capacity['cpu_us_per_io']
        available_cpu_us = capacity['available_cpu_us']

        for dc, data in sorted(device_classes.items()):
            if not data['iops']:
                continue
            avg_iops = sum(data['iops']) / len(data['iops'])
            osd_count = len(data['osds'])
            real_cpu_per_osd = cpu_us_per_io * avg_iops
            if real_cpu_per_osd > 0:
                real_max_osds = available_cpu_us / real_cpu_per_osd
            else:
                real_max_osds = float('inf')

            print(f"{dc:<15} {avg_iops:>14,.0f} "
                  f"{cpu_us_per_io:>16,.2f} {real_max_osds:>15,.1f}")

            utilization = (osd_count / real_max_osds * 100
                           if real_max_osds > 0 else float('inf'))
            if utilization > 100:
                print(f"  >> CPU BOTTLENECK: ~{utilization:.0f}% utilization "
                      "suggests throttling")
            elif utilization > 80:
                print(f"  >> WARNING: CPU near saturation at "
                      f"~{utilization:.0f}%")
            else:
                print(f"  >> OK: CPU has ~{100-utilization:.0f}% headroom")

    except FileNotFoundError:
        print(f"Error: File not found: {csv_path}")
    except Exception as e:
        print(f"Error reading CSV: {e}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1048576:
        return f"{size_bytes / 1048576:.0f}M"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f}K"
    return f"{size_bytes}B"


def _format_throughput(mbps: float) -> str:
    if mbps >= 1024:
        return f"{mbps / 1024:,.1f} GB/s"
    return f"{mbps:,.1f} MB/s"


def _detect_cpu_model() -> str:
    try:
        with open('/proc/cpuinfo') as f:
            for line in f:
                if line.startswith('model name'):
                    return line.split(':', 1)[1].strip()
    except (OSError, IOError):
        pass
    try:
        return platform.processor() or 'Unknown'
    except Exception:
        return 'Unknown'


def _prompt_input(prompt: str, default: str = '') -> str:
    try:
        val = input(prompt).strip()
        return val if val else default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def _prompt_int(prompt: str, default: int) -> int:
    val = _prompt_input(prompt, str(default))
    try:
        return int(val)
    except ValueError:
        return default


def _prompt_float(prompt: str, default: float) -> float:
    val = _prompt_input(prompt, str(default))
    try:
        return float(val)
    except ValueError:
        return default


def _prompt_choice(prompt: str, choices: List[str], default: str) -> str:
    choices_str = '/'.join(choices)
    val = _prompt_input(f"{prompt} [{choices_str}] ({default}): ", default)
    if val in choices:
        return val
    return default


# ---------------------------------------------------------------------------
# Interactive Mode
# ---------------------------------------------------------------------------

def interactive_config() -> ClusterConfig:
    """Guide user through cluster configuration interactively."""
    config = ClusterConfig()

    print()
    print("=== Ceph CPU IO Simulator - Interactive Configuration ===")
    print()

    # CPU
    cores = os.cpu_count() or 4
    print(f"Detected {cores} CPU cores.")
    config.cpu_cores = _prompt_int(f"CPU cores available [{cores}]: ", cores)
    default_ceph = max(1, config.cpu_cores - 2)
    config.cpu_cores_for_ceph = _prompt_float(
        f"CPU cores for Ceph [{default_ceph}]: ", float(default_ceph))

    # Drives
    print()
    config.drive_type = _prompt_choice("Drive type", ['hdd', 'ssd', 'nvme'],
                                       'hdd')
    config.drive_count = _prompt_int("Drives per node [12]: ", 12)

    custom_iops = _prompt_input(
        "Custom drive IOPS (0=use profile default) [0]: ", "0")
    try:
        config.drive_iops = int(custom_iops)
    except ValueError:
        config.drive_iops = 0

    # Data protection
    print()
    prot = _prompt_choice("Data protection", ['replicated', 'erasure'],
                          'replicated')
    config.protection_type = prot
    if prot == 'replicated':
        config.replica_count = _prompt_int("Replica count [3]: ", 3)
    else:
        config.ec_k = _prompt_int("EC data chunks (k) [4]: ", 4)
        config.ec_m = _prompt_int("EC parity chunks (m) [2]: ", 2)

    # Compression
    print()
    comp = _prompt_choice("Enable compression?", ['yes', 'no'], 'no')
    config.compression_enabled = (comp == 'yes')
    if config.compression_enabled:
        config.compression_algorithm = _prompt_choice(
            "Compression algorithm", ['snappy', 'zstd', 'lz4', 'zlib'],
            'zstd')
        config.compression_mode = _prompt_choice(
            "Compression mode", ['passive', 'aggressive', 'force'], 'passive')
        config.compression_ratio = _prompt_float(
            "Expected compression ratio (0.0-1.0) [0.5]: ", 0.5)

    # WAL/DB
    print()
    config.wal_db_separate = (_prompt_choice(
        "WAL/DB on separate device?", ['yes', 'no'], 'no') == 'yes')

    # Object size
    config.object_size = _prompt_choice(
        "RADOS object size", list(OBJECT_SIZES.keys()), '4m')

    # Workload
    print()
    config.workload_pattern = _prompt_choice(
        "Workload pattern", ['sequential', 'random', 'mixed'], 'mixed')
    config.read_write_ratio = _prompt_float(
        "Read/write ratio (0.0=all writes, 1.0=all reads) [0.7]: ", 0.7)

    # Scrub
    config.scrub_frequency = _prompt_choice(
        "Scrub frequency", ['daily', 'weekly', 'disabled'], 'daily')

    # Scenario
    print()
    config.scenario = _prompt_choice(
        "Scenario", ['best', 'worst', 'typical', 'all'], 'typical')

    # Duration
    config.benchmark_duration = _prompt_float(
        "Benchmark duration per test (seconds) [5.0]: ", 5.0)

    return config


# ---------------------------------------------------------------------------
# CLI Argument Parsing
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description='Ceph CPU IO Simulator - Benchmark CPU capacity '
                    'for OSD workloads',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --drive-type hdd --drive-count 12 --protection replicated:3
  %(prog)s --drive-type nvme --drive-count 4 --protection ec:4+2 --compress zstd
  %(prog)s --interactive
  %(prog)s --quick
  %(prog)s --compare real_bench_results.csv
        """)

    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--interactive', '-i', action='store_true',
                      help='Interactive guided configuration')
    mode.add_argument('--quick', action='store_true',
                      help='Quick benchmark with sensible defaults')

    cpu = parser.add_argument_group('CPU Configuration')
    cpu.add_argument('--cpu-cores', type=int, default=0,
                     help='Total CPU cores (0=auto-detect)')
    cpu.add_argument('--cpu-cores-ceph', type=float, default=0,
                     help='CPU cores reserved for Ceph '
                          '(0=auto: total minus 2)')

    drive = parser.add_argument_group('Drive Configuration')
    drive.add_argument('--drive-type', '-t',
                       choices=['hdd', 'ssd', 'nvme'], default='hdd',
                       help='Drive type (default: hdd)')
    drive.add_argument('--drive-count', '-d', type=int, default=12,
                       help='Drives per node (default: 12)')
    drive.add_argument('--drive-iops', type=int, default=0,
                       help='Override drive IOPS (0=use profile default)')

    prot = parser.add_argument_group('Data Protection')
    prot.add_argument('--protection', '-p', default='replicated:3',
                      help='Protection: replicated:N or ec:K+M '
                           '(default: replicated:3)')

    comp = parser.add_argument_group('Compression')
    comp.add_argument('--compress', '-c', default=None,
                      choices=['snappy', 'zstd', 'lz4', 'zlib'],
                      help='Enable compression with specified algorithm')
    comp.add_argument('--compress-ratio', type=float, default=0.5,
                      help='Expected compression ratio (default: 0.5)')
    comp.add_argument('--compress-mode', default='passive',
                      choices=['passive', 'aggressive', 'force'],
                      help='Compression mode (default: passive)')

    bs = parser.add_argument_group('BlueStore')
    bs.add_argument('--wal-db-separate', action='store_true',
                    help='WAL/DB on separate fast device')
    bs.add_argument('--object-size', default='4m',
                    choices=list(OBJECT_SIZES.keys()),
                    help='RADOS object size (default: 4m)')

    wl = parser.add_argument_group('Workload')
    wl.add_argument('--workload', default='mixed',
                    choices=['sequential', 'random', 'mixed'],
                    help='Workload pattern (default: mixed)')
    wl.add_argument('--rw-ratio', type=float, default=0.7,
                    help='Read/write ratio 0.0-1.0 (default: 0.7)')
    wl.add_argument('--scrub', default='daily',
                    choices=['daily', 'weekly', 'disabled'],
                    help='Scrub frequency (default: daily)')

    parser.add_argument('--scenario', '-s', default='typical',
                        choices=['best', 'worst', 'typical', 'all'],
                        help='Scenario (default: typical)')

    out = parser.add_argument_group('Output')
    out.add_argument('--output', '-o', default=None,
                     help='CSV output file')
    out.add_argument('--compare', default=None,
                     help='Compare with ceph-bench.sh CSV results')
    out.add_argument('--json', action='store_true',
                     help='Output results as JSON')

    bench = parser.add_argument_group('Benchmark Control')
    bench.add_argument('--duration', type=float, default=5.0,
                       help='Seconds per benchmark (default: 5.0)')
    bench.add_argument('--sizes', nargs='+',
                       default=['4k', '64k', '128k', '4m'],
                       choices=list(OBJECT_SIZES.keys()),
                       help='Object sizes to test '
                            '(default: 4k 64k 128k 4m)')

    parser.add_argument('--verbose', '-v', action='store_true',
                        help='Verbose output')
    parser.add_argument('--version', action='version',
                        version=f'%(prog)s {VERSION}')

    return parser.parse_args()


def parse_protection(spec: str) -> Tuple[str, int, int, int]:
    """Parse protection spec like 'replicated:3' or 'ec:4+2'."""
    spec = spec.strip().lower()
    if spec.startswith('replicated'):
        parts = spec.split(':')
        count = int(parts[1]) if len(parts) > 1 else 3
        return 'replicated', count, 0, 0
    elif spec.startswith('ec'):
        parts = spec.split(':')
        if len(parts) > 1:
            km = parts[1].split('+')
            k = int(km[0])
            m = int(km[1]) if len(km) > 1 else 2
            return 'erasure', 0, k, m
    return 'replicated', 3, 0, 0


def build_config_from_args(args) -> ClusterConfig:
    config = ClusterConfig()
    config.cpu_cores = args.cpu_cores
    config.cpu_cores_for_ceph = args.cpu_cores_ceph
    config.drive_type = args.drive_type
    config.drive_count = args.drive_count
    config.drive_iops = args.drive_iops

    ptype, rep_count, ec_k, ec_m = parse_protection(args.protection)
    config.protection_type = ptype
    config.replica_count = rep_count
    config.ec_k = ec_k
    config.ec_m = ec_m

    if args.compress:
        config.compression_enabled = True
        config.compression_algorithm = args.compress
        config.compression_ratio = args.compress_ratio
        config.compression_mode = args.compress_mode

    config.wal_db_separate = args.wal_db_separate
    config.object_size = args.object_size
    config.workload_pattern = args.workload
    config.read_write_ratio = args.rw_ratio
    config.scrub_frequency = args.scrub
    config.scenario = args.scenario
    config.benchmark_duration = args.duration
    config.object_sizes_to_test = args.sizes

    return config


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    args = parse_args()

    libs = LibraryManager()

    if args.interactive:
        config = interactive_config()
    elif args.quick:
        config = ClusterConfig()
        config.benchmark_duration = 2.0
        config.object_sizes_to_test = ['4k', '4m']
    else:
        config = build_config_from_args(args)

    if config.cpu_cores == 0:
        config.cpu_cores = os.cpu_count() or 4
    if config.cpu_cores_for_ceph <= 0:
        config.cpu_cores_for_ceph = float(max(1, config.cpu_cores - 2))

    config.cpu_model = _detect_cpu_model()

    scenarios = (['best', 'worst', 'typical'] if config.scenario == 'all'
                 else [config.scenario])

    all_results = []
    all_capacities = []

    for scenario in scenarios:
        config.scenario = scenario

        print(f"\n{'=' * 60}")
        print(f"  Running benchmarks (scenario: {scenario})...")
        print(f"{'=' * 60}")

        benchmarks = CephBenchmarks(libs, config, verbose=args.verbose)
        results = benchmarks.run_all()

        model = OSDCapacityModel(config, results)
        capacity = model.calculate()

        projection = ScaleOutProjection(config, capacity)
        scale_out = projection.project()

        report = ReportGenerator(config, libs, results, capacity, scale_out)

        if args.json:
            print(report.to_json())
        else:
            report.print_report()

        output_file = args.output or (
            f"ceph_cpu_sim_{datetime.now():%Y%m%d_%H%M%S}_{scenario}.csv")
        report.export_csv(output_file)

        all_results.extend(results)
        all_capacities.append(capacity)

    if args.compare and all_capacities:
        compare_with_real(args.compare, config, all_capacities[-1])


if __name__ == '__main__':
    main()
