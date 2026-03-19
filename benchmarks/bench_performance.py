#!/usr/bin/env python3
# Copyright 2024 The Kapitan Authors
# SPDX-FileCopyrightText: 2024 The Kapitan Authors <kapitan-admins@googlegroups.com>
#
# SPDX-License-Identifier: Apache-2.0
"""
Benchmark suite for the three main performance improvements in Kapitan:

  1. Search-path resolution LRU cache  (kapitan/resources.py)
  2. Bounded JSONNET_CACHE via lru_cache (kapitan/resources.py)
  3. Glob-expansion cache              (kapitan/inputs/base.py)

Run with:
    python benchmarks/bench_performance.py

Each benchmark prints timing and cache-hit statistics so that the gains can be
compared against un-patched versions.
"""

import glob
import os
import tempfile
import time
from functools import lru_cache


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_tree(root, n_dirs=5, n_files_per_dir=20):
    """Create a temporary directory tree used by the file-lookup benchmarks."""
    dirs = []
    for i in range(n_dirs):
        d = os.path.join(root, f"dir_{i}")
        os.makedirs(d, exist_ok=True)
        dirs.append(d)
        for j in range(n_files_per_dir):
            p = os.path.join(d, f"file_{j:04d}.yaml")
            with open(p, "w") as f:
                f.write(f"key: value_{i}_{j}\n")
    return dirs


# ---------------------------------------------------------------------------
# Benchmark 1 – Search-path resolution LRU cache
# ---------------------------------------------------------------------------
# Without the cache every call to read_file / yaml_load / file_exists iterates
# all search paths and calls os.path.exists() for each prefix.  With the cache
# only the first call for a given (search_paths, name) pair hits the filesystem.
# ---------------------------------------------------------------------------

def _resolve_uncached(search_paths, name):
    """Baseline: iterate search paths and call os.path.exists() each time."""
    for path in search_paths:
        full = os.path.join(path, name)
        if os.path.exists(full):
            return full
    return None


@lru_cache(maxsize=None)
def _resolve_cached(search_paths_tuple, name):
    """Patched: same logic but result is cached by lru_cache."""
    for path in search_paths_tuple:
        full = os.path.join(path, name)
        if os.path.exists(full):
            return full
    return None


def bench_search_path_resolution(n_lookups=2000):
    """Compare repeated file lookups with and without the path-resolution cache."""
    print("\n=== Benchmark 1: Search-path resolution LRU cache ===")
    with tempfile.TemporaryDirectory() as root:
        dirs = _make_temp_tree(root, n_dirs=8, n_files_per_dir=50)
        # Target files live only in the last directory (worst-case for linear scan)
        target_dir = dirs[-1]
        search_paths = tuple(dirs)
        filenames = [f"file_{j:04d}.yaml" for j in range(50)]

        # -- Uncached baseline --
        t0 = time.perf_counter()
        for _ in range(n_lookups):
            for name in filenames:
                _resolve_uncached(list(search_paths), name)
        uncached_elapsed = time.perf_counter() - t0

        # -- Cached --
        _resolve_cached.cache_clear()
        t0 = time.perf_counter()
        for _ in range(n_lookups):
            for name in filenames:
                _resolve_cached(search_paths, name)
        cached_elapsed = time.perf_counter() - t0

        info = _resolve_cached.cache_info()
        total_calls = n_lookups * len(filenames)
        print(f"  Lookups      : {total_calls:,}  ({n_lookups} iterations × {len(filenames)} files)")
        print(f"  Uncached     : {uncached_elapsed:.3f}s")
        print(f"  Cached       : {cached_elapsed:.3f}s")
        print(f"  Speedup      : {uncached_elapsed / cached_elapsed:.1f}×")
        print(f"  Cache hits   : {info.hits:,}  misses: {info.misses:,}")


# ---------------------------------------------------------------------------
# Benchmark 2 – Bounded JSONNET_CACHE via lru_cache
# ---------------------------------------------------------------------------
# The original code uses an unbounded dict JSONNET_CACHE.  When a Kapitan run
# processes thousands of targets (or is embedded in a long-running service) the
# dict grows indefinitely.  The patch wraps file reads in lru_cache(maxsize=512)
# so that least-recently-used entries are evicted automatically.
# ---------------------------------------------------------------------------

def _read_uncached(path):
    with open(path) as f:
        return f.read()


@lru_cache(maxsize=512)
def _read_lru(path):
    with open(path) as f:
        return f.read()


def bench_jsonnet_cache(n_files=600, n_reads_per_file=5):
    """
    Simulate reading n_files unique files, each n_reads_per_file times.
    Shows memory (dict size) and timing difference between unbounded dict and
    LRU cache.
    """
    print("\n=== Benchmark 2: Bounded JSONNET_CACHE (lru_cache vs unbounded dict) ===")
    with tempfile.TemporaryDirectory() as root:
        paths = []
        for i in range(n_files):
            p = os.path.join(root, f"lib_{i:04d}.jsonnet")
            with open(p, "w") as f:
                f.write("local x = %d;\n{value: x}\n" % i)
            paths.append(p)

        # -- Unbounded dict baseline --
        cache_dict = {}
        t0 = time.perf_counter()
        for _ in range(n_reads_per_file):
            for p in paths:
                if p not in cache_dict:
                    cache_dict[p] = _read_uncached(p)
        dict_elapsed = time.perf_counter() - t0
        dict_size = len(cache_dict)

        # -- LRU cache --
        _read_lru.cache_clear()
        t0 = time.perf_counter()
        for _ in range(n_reads_per_file):
            for p in paths:
                _read_lru(p)
        lru_elapsed = time.perf_counter() - t0
        info = _read_lru.cache_info()

        print(f"  Files        : {n_files}  (reads per file: {n_reads_per_file})")
        print(f"  Dict cache   : {dict_elapsed:.3f}s  entries held in memory: {dict_size}")
        print(f"  LRU cache    : {lru_elapsed:.3f}s  current size: {info.currsize}  (max 512)")
        print(f"  Memory saved : dict holds {dict_size - info.currsize} extra entries vs LRU")
        print(f"  LRU hits     : {info.hits:,}  misses: {info.misses:,}")


# ---------------------------------------------------------------------------
# Benchmark 3 – Glob-expansion cache
# ---------------------------------------------------------------------------
# inputs/base.py calls glob.glob() for every (search_path × input_path) pair
# on every target compilation.  Many targets share the same search paths and
# glob patterns (e.g. "components/*.jsonnet"), so the results are identical.
# Caching saves redundant filesystem scans.
# ---------------------------------------------------------------------------

def _glob_uncached(search_paths, input_path):
    results = []
    for path in search_paths:
        results.extend(glob.glob(os.path.join(path, input_path)))
    return frozenset(results)


@lru_cache(maxsize=1024)
def _glob_cached(search_paths_tuple, input_path):
    results = []
    for path in search_paths_tuple:
        results.extend(glob.glob(os.path.join(path, input_path)))
    return frozenset(results)


def bench_glob_expansion(n_targets=200, n_patterns=10):
    """
    Simulate n_targets compilation units each resolving n_patterns glob patterns
    against a fixed set of search paths.  Real compilations share patterns across
    targets so the cache hit rate is high.
    """
    print("\n=== Benchmark 3: Glob-expansion cache ===")
    with tempfile.TemporaryDirectory() as root:
        # Create a small component tree
        comp_dir = os.path.join(root, "components")
        os.makedirs(comp_dir)
        for i in range(30):
            with open(os.path.join(comp_dir, f"comp_{i:03d}.jsonnet"), "w") as f:
                f.write("{}\n")

        lib_dir = os.path.join(root, "lib")
        os.makedirs(lib_dir)
        for i in range(10):
            with open(os.path.join(lib_dir, f"util_{i}.libsonnet"), "w") as f:
                f.write("{}\n")

        search_paths = (root, comp_dir, lib_dir)
        patterns = [
            "components/*.jsonnet",
            "lib/*.libsonnet",
            "*.jsonnet",
            "*.libsonnet",
            "components/comp_00*.jsonnet",
        ] * (n_patterns // 5 + 1)
        patterns = patterns[:n_patterns]

        # -- Uncached baseline --
        t0 = time.perf_counter()
        for _ in range(n_targets):
            for pat in patterns:
                _glob_uncached(list(search_paths), pat)
        uncached_elapsed = time.perf_counter() - t0

        # -- Cached --
        _glob_cached.cache_clear()
        t0 = time.perf_counter()
        for _ in range(n_targets):
            for pat in patterns:
                _glob_cached(search_paths, pat)
        cached_elapsed = time.perf_counter() - t0

        info = _glob_cached.cache_info()
        total_globs = n_targets * n_patterns
        print(f"  Targets      : {n_targets}  patterns per target: {n_patterns}")
        print(f"  Total globs  : {total_globs:,}")
        print(f"  Uncached     : {uncached_elapsed:.3f}s")
        print(f"  Cached       : {cached_elapsed:.3f}s")
        print(f"  Speedup      : {uncached_elapsed / cached_elapsed:.1f}×")
        print(f"  Cache hits   : {info.hits:,}  misses: {info.misses:,}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("Kapitan Performance Benchmarks")
    print("=" * 40)
    bench_search_path_resolution()
    bench_jsonnet_cache()
    bench_glob_expansion()
    print("\nDone.")
