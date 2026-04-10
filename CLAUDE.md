# CLAUDE.md — ceph-bench project rules

## Project overview

Two tools for Ceph OSD performance analysis:

| Tool | Purpose | Dependencies | Status |
|------|---------|-------------|--------|
| `ceph-bench.sh` | Runs `ceph tell osd.N bench` against a live cluster, outputs CSV | Bash, live Ceph cluster | **Stable** |
| `ceph-analysis.py` | Analyzes ceph-bench.sh CSV output, finds slow OSDs | pandas, numpy, scipy | **Stable** |

The CPU/OSD capacity simulator has moved to its own project: [ceph_osd_sim](https://github.com/lancealot/ceph_osd_sim)

## Quick reference

```bash
# Run benchmarks (requires live Ceph cluster)
./ceph-bench.sh -t hdd -r 2 -p -m balanced

# Analyze results
./ceph-analysis.py ceph_bench_results_TIMESTAMP.csv
```

## Development rules

1. **One feature per session.** Do not combine unrelated changes. Finish, commit, push.
2. **Propose before implementing.** State what you'll add AND what you'll remove to stay in budget.
3. **No speculative features.** Only build what's explicitly requested.
4. **No "while I'm here" improvements.** Don't refactor adjacent code, add docstrings, improve error messages, or add type annotations to code you didn't change.
5. **Small commits.** Each commit should be a single logical change that could be reverted independently.

## Anti-patterns — DO NOT

- Add new output formats (CSV, JSON, YAML, HTML). Text output is sufficient.
- Add packaging infrastructure (setup.py, pyproject.toml, __init__.py).
- Add logging frameworks. Use print().
- Add configuration files (YAML, TOML, INI). CLI args are enough.
- Add features "for completeness" or "while we're at it."
- Create documentation files beyond README.md and this file.
