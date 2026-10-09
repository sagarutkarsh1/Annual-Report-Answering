"""Helpers for hosts with very little memory (Render free: 512 MB).  Everything here is opt-in (Settings.low_memory) and guarded:
a trick that does not apply is skipped with a log line, never an error.

* `stub_datasets_for_ragas()` - `import ragas` runs `from datasets import Dataset` at module level, which drags in pyarrow, pandas,
  huggingface_hub, fsspec, dill ... (about 90 MB of resident memory).  ReportLens only uses the `ragas.metrics.collections` API,
  which never touches a Hugging Face dataset, so a stand-in module with an empty `Dataset` class is enough.
* `trim_memory()` - hand freed heap pages back to the operating system (glibc `malloc_trim`); Python frees objects but glibc keeps
  the arenas, so the container's memory graph would otherwise stay at its peak.
"""
from __future__ import annotations

import ctypes
import gc
import importlib.machinery
import logging
import sys
import threading
import types
from pathlib import Path

log = logging.getLogger("reportlens.lowmem")

# One heavy child process at a time (an indexing job OR a scoring run): both together would not fit 512 MB.  Taken by
# `IndexService._index_in_child` and `eval_child.ChildEvaluator`; in-process work never touches it.
HEAVY_JOB_LOCK = threading.Lock()
# Set while an indexing child runs (it needs ~280 MB): the question set then answers one question at a time.
INDEXING_ACTIVE = threading.Event()


class _DatasetStandIn:
    """Placeholder for `datasets.Dataset` (only ever used in type annotations and isinstance checks inside ragas.evaluate)."""


def stub_datasets_for_ragas() -> bool:
    """Install the stand-in unless the real `datasets` is already imported.  True when the stand-in is (now) in place."""
    existing = sys.modules.get("datasets")
    if existing is not None:
        return bool(getattr(existing, "__reportlens_stub__", False))
    module = types.ModuleType("datasets")
    module.__spec__ = importlib.machinery.ModuleSpec("datasets", loader=None)
    module.__version__ = "0+reportlens.stub"
    module.__reportlens_stub__ = True                # type: ignore[attr-defined]
    module.Dataset = _DatasetStandIn                 # type: ignore[attr-defined]
    sys.modules["datasets"] = module
    log.info("memory saver: the Hugging Face 'datasets' package is replaced by a stand-in while RAGAS loads (the scoring API does not use it)")
    return True


def disable_litellm_preload() -> bool:
    """`PageIndexClient(...)` starts importing litellm (~150 MB resident) on a background thread the moment a client exists, whether
    or not this process will ever call a model through it.  Marking the preload as already started makes litellm load at the first
    real model call instead, which for the indexing child is AFTER the layout parse (whose memory has been handed back by then) and
    for the web process may be never (the OpenAI Responses lane does not use it).  False when the SDK has no such seam."""
    try:
        import pageindex.client as client_module
    except ImportError:
        return False
    if not hasattr(client_module, "_litellm_preload_started"):
        log.warning("PageIndex has no litellm preload switch; litellm loads eagerly (about 150 MB more memory)")
        return False
    client_module._litellm_preload_started = True        # type: ignore[attr-defined]
    return True


def memory_summary() -> str:
    """One log line: resident memory (Linux only) and which heavy libraries this process has loaded."""
    try:
        status = {k: v.strip() for k, _, v in (ln.partition(":") for ln in Path("/proc/self/status").read_text().splitlines())}
        rss, hwm = status["VmRSS"], status["VmHWM"]
    except (OSError, KeyError):
        rss = hwm = "n/a"                                   # not Linux: still say what is loaded
    loaded = [m for m in ("litellm", "openai", "agents", "ragas", "pandas", "pyarrow", "datasets") if m in sys.modules]
    return f"rss={rss} peak={hwm} loaded={','.join(loaded) or '-'}"


def trim_memory() -> bool:
    """gc + malloc_trim.  False where there is no glibc (Windows, macOS, musl)."""
    gc.collect()
    try:
        return bool(ctypes.CDLL("libc.so.6").malloc_trim(0))
    except (OSError, AttributeError):
        return False
