# CLAUDE.md — ceph-bench project rules

## Project overview

Three tools for Ceph OSD performance analysis:

| Tool | Purpose | Dependencies | Status |
|------|---------|-------------|--------|
| `ceph-bench.sh` | Runs `ceph tell osd.N bench` against a live cluster, outputs CSV | Bash, live Ceph cluster | **Stable — do not modify** |
| `ceph-analysis.py` | Analyzes ceph-bench.sh CSV output, finds slow OSDs | pandas, numpy, scipy | **Stable — do not modify** |
| `ceph-cpu-io-sim.py` | CPU/IO benchmark simulator for capacity planning | Python3 stdlib only | **Active development** |
| `test_ceph_cpu_io_sim.py` | Tests for the simulator | unittest | **Active development** |

## Quick reference

```bash
# Run tests
python3 -m pytest test_ceph_cpu_io_sim.py -v

# Quick smoke test
./ceph-cpu-io-sim.py --quick

# Full benchmark
./ceph-cpu-io-sim.py --drive-type hdd --drive-count 12

# Parallel contention test
./ceph-cpu-io-sim.py --quick --parallel 4
```

## Complexity budget

These are hard limits. If a change would exceed them, refactor first.

| File | Max lines | Current |
|------|-----------|---------|
| `ceph-cpu-io-sim.py` | 2,000 | ~3,549 (over budget — needs simplification) |
| `test_ceph_cpu_io_sim.py` | 500 | ~968 (over budget — needs simplification) |
| `README.md` | 120 | ~185 (over budget — needs simplification) |
| `CLAUDE.md` | 200 | this file |

## Development rules

1. **One feature per session.** Do not combine unrelated changes. Finish, commit, push.
2. **Propose before implementing.** State what you'll add AND what you'll remove to stay in budget.
3. **Every addition requires a deletion.** Net line count must not increase unless explicitly approved.
4. **No speculative features.** Only build what's explicitly requested.
5. **No "while I'm here" improvements.** Don't refactor adjacent code, add docstrings, improve error messages, or add type annotations to code you didn't change.
6. **Test before committing.** Run `python3 -m pytest test_ceph_cpu_io_sim.py -v` and `./ceph-cpu-io-sim.py --quick`.
7. **Small commits.** Each commit should be a single logical change that could be reverted independently.

## Anti-patterns — DO NOT

- Add new output formats (CSV, JSON, YAML, HTML). Text output is sufficient.
- Add new CLI arguments without removing one first.
- Add interactive/wizard modes.
- Add comparison features requiring external data files.
- Split into multiple Python modules. Single-script simplicity is a feature.
- Add packaging infrastructure (setup.py, pyproject.toml, __init__.py).
- Add logging frameworks. Use print().
- Add configuration files (YAML, TOML, INI). CLI args are enough.
- Over-engineer library detection. Two tiers max: try Python package → stdlib fallback.
- Add "advice" or "recommendation" sections to output. Report data, let humans decide.
- Add features "for completeness" or "while we're at it."
- Create documentation files beyond README.md and this file.

## What makes a good change

- Improves accuracy of CPU benchmarks or capacity estimates
- Reduces code complexity while maintaining functionality
- Fixes a bug that affects output correctness
- Adds a feature the user explicitly requested, within the complexity budget
- Removes dead code or unused features

## Architecture notes

The simulator has four layers (in execution order):

1. **LibraryManager** — detects available libraries, provides unified benchmark operations
2. **CephBenchmarks** — runs timed CPU operations (CRC32C, SHA256, serialization, RocksDB, CRUSH, EC)
3. **OSDCapacityModel** — translates benchmark results into "how many OSDs can this CPU handle"
4. **ReportGenerator** — formats and prints results

Do not add new layers. Do not add new classes unless replacing an existing one.

## Simplification roadmap

The simulator is currently ~3,549 lines (over its 2,000-line budget). These are the planned cuts, to be done one per session:

1. ~~Create CLAUDE.md~~ (this file)
2. Remove interactive mode, comparison mode, prompt helpers (~170 lines)
3. Simplify LibraryManager — drop ctypes tier (~400 lines saved)
4. Simplify ReportGenerator — text output only, cut CSV/JSON export (~400 lines saved)
5. Trim ClusterConfig + CLI args — remove rarely-used options (~100 lines saved)
6. Simplify OSDCapacityModel — remove CPU scaling advice (~80 lines saved)
7. Rewrite tests — behavioral tests only, cut to ~500 lines (~470 lines saved)
8. Simplify README — cut to ~120 lines (~65 lines saved)

## Project direction

The simulator (`ceph-cpu-io-sim.py`) and the live benchmark tools (`ceph-bench.sh`, `ceph-analysis.py`) are separate concerns and may be split into separate projects. Do not add integration between them.

## Remaining roadmap (genuine improvements, after simplification)

1. **Accuracy validation** — Compare predictions vs real Ceph cluster data
2. **NUMA-aware benchmarking** — Pin workers to NUMA nodes for contention measurement
3. **Better RocksDB modeling** — More realistic compaction and WAL/DB CPU costs
