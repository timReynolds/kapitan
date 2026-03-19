# Kapitan: Top 3 Performance Gains — Benchmark & Implementation Plan

## 1. Incremental Compilation via Production-Ready Persistent Cache

### Problem
Every `kapitan compile` re-compiles **all** targets from scratch, even when only a single input file changed. The `--cache` flag exists (`inputs/cache.py`) but is marked **EXPERIMENTAL** and off by default. For projects with 50–500+ targets, full recompilation dominates wall-clock time.

### Where in the Code
- **`targets.py:141`** — `pool.imap_unordered(worker, target_objs)` compiles every target unconditionally.
- **`inputs/cache.py`** — `InputCache` with Blake2b hashing already exists but is opt-in and limited.
- **`inputs/base.py:48-92`** — `compile_obj()` expands globs and compiles each file without checking staleness.
- **`resources.py:38`** — `JSONNET_CACHE` is in-memory only, lost between runs.

### Expected Impact
**High (2–10× faster for iterative compilations).** Most real-world workflows change 1–3 files between compiles. Skipping unchanged targets turns an O(N) operation into O(1).

### Benchmark Plan
1. **Create a benchmark harness** (`benchmarks/compile_bench.py`) that:
   - Uses a representative inventory (e.g., the existing `tests/test_kubernetes_compiled/` fixtures or a generated inventory with 50/100/200 targets).
   - Measures `compile_targets()` wall-clock time and per-target time using `time.perf_counter()`.
   - Runs three scenarios: (a) cold compile (no cache), (b) warm compile (no changes), (c) warm compile (1 target changed).
2. **Metric**: Total compile time (seconds), number of cache hits vs misses, memory usage via `tracemalloc`.
3. **Baseline**: Run without `--cache`; record per-target times from existing `logger.info` output.
4. **Target**: Warm no-change compile should be ≥5× faster than cold compile.

### Implementation Plan
1. **Make `InputCache` the default** — Remove the EXPERIMENTAL label; enable `--cache` by default (add `--no-cache` to opt out).
2. **Add file-level change detection** — Before compiling a target, hash all its `input_paths` (resolved from globs) + the target's inventory parameters. Compare against the stored hash in `InputCache`. Skip compilation if unchanged.
3. **Persist `JSONNET_CACHE` across runs** — Serialize the jsonnet import cache to disk (alongside `InputCache`) so repeated imports of `kapitan.libjsonnet` and shared libraries are instant.
4. **Add cache invalidation on search_path changes** — Include a hash of the search_paths contents in the cache key so that dependency updates correctly bust the cache.
5. **Bound `JSONNET_CACHE`** — Replace the unbounded `dict` at `resources.py:38` with an `LRUCache(maxsize=4096)` to prevent memory bloat on large projects.

### Key Files to Modify
- `kapitan/inputs/cache.py` — Enhance cache key computation
- `kapitan/inputs/base.py` — Add staleness check in `compile_obj()`
- `kapitan/targets.py` — Skip unchanged targets before dispatching to pool
- `kapitan/resources.py` — Bound and persist `JSONNET_CACHE`
- `kapitan/cli.py` — Change `--cache` default to `True`

---

## 2. Eliminate Inventory Serialization Overhead in Worker Processes

### Problem
When `--inventory-pool-cache` is `True` (the default), the **entire inventory** is serialized via `cached.as_dict()` and passed to every worker process through `multiprocessing.Pool`. For large inventories (hundreds of targets, deep parameter trees), this means:
- **Pickle serialization** of the full inventory dict once per `pool.imap_unordered` call.
- **Pickle deserialization** in *every* worker process (N times for N workers).
- The CLI comment at `cli.py:277-280` explicitly notes: *"setting to False might speed up compilation for large inventories"* — acknowledging this is a known bottleneck.

### Where in the Code
- **`targets.py:135`** — `globals_cached=cached.as_dict() if args.inventory_pool_cache else None` serializes the full cache.
- **`cached.py:104-124`** — `as_dict()` / `from_dict()` are the serialization entry points.
- **`targets.py:282-283`** — Each worker calls `cached.from_dict(globals_cached)` to deserialize.

### Expected Impact
**Medium-High (20–50% faster compile for large inventories).** Serialization overhead scales with inventory size. For inventories with 200+ targets and deep parameter hierarchies, pickle round-trips can consume 30–50% of total compile time.

### Benchmark Plan
1. **Instrument serialization** — Add `time.perf_counter()` measurements around `cached.as_dict()` in `targets.py:135` and `cached.from_dict()` in `targets.py:283`. Log sizes with `sys.getsizeof()` / `len(pickle.dumps(...))`.
2. **Generate scaled inventories** — Create test inventories at 50, 100, 200, 500 targets with realistic parameter depth. Measure compile time vs inventory size.
3. **Metric**: Time spent in serialization/deserialization vs total compile time (percentage). Memory usage per worker.
4. **A/B comparison**: `--inventory-pool-cache=True` vs `False` vs the new shared-memory approach.
5. **Target**: Reduce serialization overhead to <5% of total compile time regardless of inventory size.

### Implementation Plan
1. **Use `multiprocessing.shared_memory`** — Serialize the inventory once to a `SharedMemory` block. Workers attach to the same block by name and deserialize from it (one copy in physical memory, shared across all workers).
   - Create: `shm = shared_memory.SharedMemory(create=True, size=len(data))` in the parent.
   - Workers: `shm = shared_memory.SharedMemory(name=shm_name)` — zero-copy attach.
2. **Alternative: per-target slicing** — Instead of passing the full inventory to every worker, pass only the slice of inventory each target needs. Each `target_obj` already contains its own `parameters.kapitan`; the worker only needs `cached.inv[target_name]` for YAML style lookups (`base.py:275`). Pass just that subset.
3. **Lazy deserialization** — If the full inventory must be available, deserialize lazily on first access rather than eagerly in `compile_target()`.
4. **Use `fork` start method where safe** — On Linux, `fork` shares the parent's memory space copy-on-write, avoiding serialization entirely. Add detection logic: if the inventory is read-only in workers (which it is), prefer `fork` on Linux.

### Key Files to Modify
- `kapitan/targets.py` — Replace `cached.as_dict()` with shared memory or per-target slicing
- `kapitan/cached.py` — Add `SharedMemory`-based serialization methods
- `kapitan/inputs/base.py:275` — Refactor `cached.inv[target_name]` access to work with sliced data

---

## 3. Parallelize OmegaConf Inventory Rendering with Direct Return (Remove Manager.dict)

### Problem
The OmegaConf inventory backend uses `mp.Manager().dict()` for inter-process communication during inventory rendering (`omegaconf/__init__.py:80-87`). `Manager().dict()` creates a **server process** and uses **proxy objects** — every read/write to the shared dict is an IPC call over a socket. For N targets, this means N IPC round-trips just to store results, plus the overhead of starting the Manager server.

Additionally, `register_resolvers()` is called inside every worker (`inventory_worker`, line 101), which re-registers OmegaConf resolvers redundantly in each process.

### Where in the Code
- **`omegaconf/__init__.py:80-87`** — `manager = mp.Manager(); shared_targets = manager.dict()` + `pool.map_async()`.
- **`omegaconf/__init__.py:94-95`** — Results copied back from `shared_targets` into `self.targets`.
- **`omegaconf/__init__.py:97-107`** — `inventory_worker` is a `@staticmethod` that writes to `shared_targets`.
- **`inventory/inventory.py:97-99`** — `render_targets()` called during initialization.

### Expected Impact
**Medium (20–40% faster inventory rendering).** Inventory rendering is the first timed operation in every compile (`targets.py:43-50`). For projects with 100+ targets, the Manager overhead is significant. The `logger.info` at line 48 shows this timing prominently to users.

### Benchmark Plan
1. **Measure inventory rendering time** — The existing `rendering_start` timer at `targets.py:43-50` already captures this. Create a benchmark that runs `get_inventory()` in isolation across scaled inventories.
2. **Profile IPC overhead** — Use `cProfile` or `py-spy` to measure time spent in `Manager.dict().__setitem__()` vs actual `load_target()` work.
3. **Metric**: Inventory rendering wall-clock time, overhead percentage from Manager vs direct return.
4. **Scaled test**: 50, 100, 200, 500 targets with realistic class hierarchies (5–15 classes per target).
5. **Target**: ≥30% reduction in inventory rendering time.

### Implementation Plan
1. **Replace `Manager.dict()` with direct return from workers** — Change `inventory_worker` to return the target object. Use `pool.map()` (which collects return values directly) instead of writing to a shared dict:
   ```python
   def render_targets(self, targets, ignore_class_not_found=False):
       if not self.initialised:
           with mp.Pool(min(len(targets), os.cpu_count())) as pool:
               results = pool.map(self.inventory_worker,
                   [(self, target) for target in targets.values()])
           for target in results:
               self.targets[target.name] = target
   ```
2. **Move `register_resolvers()` to process initializer** — Use the `initializer` parameter of `mp.Pool()` to call `register_resolvers()` once per worker process instead of once per target:
   ```python
   with mp.Pool(parallelism, initializer=register_resolvers) as pool:
   ```
3. **Consider `ProcessPoolExecutor`** — Replace `mp.Pool` with `concurrent.futures.ProcessPoolExecutor` for better error handling and future-based result collection. This also enables easier migration to async patterns.
4. **Batch small inventories** — For inventories with fewer targets than CPU cores, skip multiprocessing entirely (the overhead exceeds the benefit). Add a threshold check:
   ```python
   if len(targets) <= 4:
       for target in targets.values():
           self.load_target(target)
   else:
       # use pool
   ```

### Key Files to Modify
- `kapitan/inventory/backends/omegaconf/__init__.py` — Replace Manager.dict pattern, add pool initializer, add small-inventory fast path

---

## Summary

| # | Optimization | Expected Speedup | Effort | Risk |
|---|-------------|-----------------|--------|------|
| 1 | Incremental compilation cache | 2–10× (iterative) | Medium | Low — cache already exists |
| 2 | Shared memory for inventory | 20–50% (large inv) | Medium | Medium — multiprocessing changes |
| 3 | Remove Manager.dict in OmegaConf | 20–40% (inv rendering) | Low | Low — simpler code |

**Recommended order**: #3 first (lowest effort, immediate win), then #1 (highest impact for users), then #2 (largest inventories benefit most).
