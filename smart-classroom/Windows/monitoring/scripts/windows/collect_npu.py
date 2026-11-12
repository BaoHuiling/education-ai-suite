import os
import re
import csv
import ctypes
from ctypes import wintypes
from collections import defaultdict
from datetime import datetime
import logging
import win32pdh

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# NPU utilization comes from the same "GPU Engine" PDH counters Task Manager
# reads: the NPU is an MCDM (compute-only) adapter and its engines show up
# there under its own LUID. Level Zero Sysman engine activity was used before,
# but on the Intel NPU driver it does not track inference load at all (it read
# ~10% idle and ~10% under full load while these counters read 0% / 85%).

_KMTQAITYPE_ADAPTERTYPE = 15
_ADAPTERTYPE_COMPUTE_ONLY = 1 << 11
_ENUMADAPTERS_FILTER_INCLUDE_COMPUTE_ONLY = 1 << 0
_MAX_ADAPTERS = 64

_INSTANCE_RE = re.compile(r"pid_\d+_(luid_0x[0-9a-f]+_0x[0-9a-f]+)_(phys_\d+_eng_\d+)", re.IGNORECASE)


class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", wintypes.DWORD), ("HighPart", wintypes.LONG)]


class _ADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", ctypes.c_uint), ("AdapterLuid", _LUID),
                ("NumOfSources", ctypes.c_ulong), ("bPrecisePresentRegionsPreferred", wintypes.BOOL)]


class _ENUMADAPTERS3(ctypes.Structure):
    _fields_ = [("Filter", ctypes.c_uint64), ("NumAdapters", ctypes.c_uint),
                ("pAdapters", ctypes.POINTER(_ADAPTERINFO))]


class _QUERYADAPTERINFO(ctypes.Structure):
    _fields_ = [("hAdapter", ctypes.c_uint), ("Type", ctypes.c_int),
                ("pPrivateDriverData", ctypes.c_void_p), ("PrivateDriverDataSize", ctypes.c_uint)]


class _CLOSEADAPTER(ctypes.Structure):
    _fields_ = [("hAdapter", ctypes.c_uint)]


def get_npu_luids():
    """Return the PDH LUID tags ("luid_0x..._0x...") of all compute-only adapters (NPUs).

    D3DKMTEnumAdapters2 skips compute-only adapters, so this needs
    D3DKMTEnumAdapters3 with the IncludeComputeOnly filter (Windows 11).
    """
    gdi32 = ctypes.WinDLL("gdi32")
    adapters = (_ADAPTERINFO * _MAX_ADAPTERS)()
    enum = _ENUMADAPTERS3(_ENUMADAPTERS_FILTER_INCLUDE_COMPUTE_ONLY, _MAX_ADAPTERS, adapters)
    status = gdi32.D3DKMTEnumAdapters3(ctypes.byref(enum))
    if status != 0:
        raise OSError(f"D3DKMTEnumAdapters3 failed: NTSTATUS {status & 0xFFFFFFFF:#x}")

    luids = set()
    for adapter in adapters[:enum.NumAdapters]:
        adapter_type = ctypes.c_uint(0)
        query = _QUERYADAPTERINFO(adapter.hAdapter, _KMTQAITYPE_ADAPTERTYPE,
                                  ctypes.cast(ctypes.byref(adapter_type), ctypes.c_void_p),
                                  ctypes.sizeof(adapter_type))
        if gdi32.D3DKMTQueryAdapterInfo(ctypes.byref(query)) == 0 and adapter_type.value & _ADAPTERTYPE_COMPUTE_ONLY:
            luid = adapter.AdapterLuid
            luids.add(f"luid_0x{luid.HighPart & 0xFFFFFFFF:08x}_0x{luid.LowPart:08x}")
        gdi32.D3DKMTCloseAdapter(ctypes.byref(_CLOSEADAPTER(adapter.hAdapter)))
    return luids


def get_npu_utilization(counter, npu_luids):
    """Task Manager semantics: sum each engine over processes, report the busiest engine."""
    try:
        values = win32pdh.GetFormattedCounterArray(counter, win32pdh.PDH_FMT_DOUBLE)
    except win32pdh.error:
        # No GPU Engine instances yet (nothing has opened a device).
        return 0.0

    engine_totals = defaultdict(float)
    for instance, value in values.items():
        m = _INSTANCE_RE.search(instance)
        if m and m.group(1).lower() in npu_luids:
            engine_totals[m.group(2)] += value
    return min(100.0, max(engine_totals.values(), default=0.0))


def start_npu_monitoring(interval_seconds, stop_event, output_dir=None):
    if output_dir is None:
        output_dir = os.getcwd()
    if not os.path.exists(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    try:
        npu_luids = get_npu_luids()
    except Exception as e:
        logger.error(f"NPU detection failed: {e}")
        return
    if not npu_luids:
        logger.warning("No NPU adapter found; NPU monitoring disabled.")
        return

    npu_file = os.path.join(output_dir, "npu_metrics.csv")
    mode = 'a' if os.path.exists(npu_file) else 'w'

    # One long-lived wildcard query: each sample is the average over the whole
    # interval, and engines of processes that open the NPU later are picked up
    # on the next collection.
    query = win32pdh.OpenQuery()
    try:
        counter = win32pdh.AddCounter(query, r"\GPU Engine(*)\Utilization Percentage")
        win32pdh.CollectQueryData(query)
        logger.info(f"Started NPU monitoring ({', '.join(sorted(npu_luids))})")

        with open(npu_file, mode, newline='', encoding='utf-8') as file:
            writer = csv.writer(file)
            if mode == 'w':
                writer.writerow(["timestamp", "total_npu_utilization"])
                file.flush()

            while not stop_event.wait(interval_seconds):
                timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                try:
                    win32pdh.CollectQueryData(query)
                    util = get_npu_utilization(counter, npu_luids)
                except Exception as e:
                    logger.error(f"NPU monitoring error: {e}")
                    util = 0.0
                writer.writerow([timestamp, util])
                file.flush()
    except Exception as e:
        logger.error(f"Error in NPU monitoring: {e}")
    finally:
        win32pdh.CloseQuery(query)
        logger.info("NPU monitoring terminated.")
