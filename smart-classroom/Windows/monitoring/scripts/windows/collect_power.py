import os
import csv
import logging
from datetime import datetime
import win32pdh

logger = logging.getLogger(__name__)

# Package power from the Windows "Energy Meter" counters, which expose the
# Intel RAPL domains (RAPL_Package0_PKG, ..._PP0 cores, ..._PP1 graphics, ...).
# Readable without admin rights or an extra driver. "Power" is in milliwatts
# and PDH computes it from the energy delta between two collections, so with a
# long-lived query each sample is the average over the whole interval.
_PACKAGE_SUFFIX = "_pkg"


def get_package_power_instances():
    """Return the Energy Meter instances that measure whole-package power."""
    _, instances = win32pdh.EnumObjectItems(None, None, "Energy Meter", win32pdh.PERF_DETAIL_WIZARD)
    return [inst for inst in instances if inst.lower().endswith(_PACKAGE_SUFFIX)]


def start_power_monitoring(interval_seconds, stop_event, output_dir=None):
    output_dir = output_dir or os.getcwd()
    os.makedirs(output_dir, exist_ok=True)

    try:
        instances = get_package_power_instances()
    except Exception as e:
        logger.warning(f"Power monitoring disabled: Energy Meter counters unavailable ({e})")
        return
    if not instances:
        logger.warning("Power monitoring disabled: no RAPL package power counter found")
        return

    power_file = os.path.join(output_dir, "power_metrics.csv")
    mode = 'a' if os.path.exists(power_file) else 'w'

    query = win32pdh.OpenQuery()
    try:
        counters = [win32pdh.AddCounter(query, rf"\Energy Meter({inst})\Power") for inst in instances]
        win32pdh.CollectQueryData(query)
        logger.info(f"Started power monitoring ({', '.join(instances)})")

        with open(power_file, mode, newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if mode == 'w':
                writer.writerow(['timestamp', 'power_usage(w)'])
                f.flush()

            while not stop_event.wait(interval_seconds):
                timestamp = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
                try:
                    win32pdh.CollectQueryData(query)
                    power_mw = sum(win32pdh.GetFormattedCounterValue(c, win32pdh.PDH_FMT_DOUBLE)[1]
                                   for c in counters)
                    writer.writerow([timestamp, round(power_mw / 1000.0, 3)])
                    f.flush()
                except Exception as e:
                    logger.error(f"Power monitoring error: {e}")
    except Exception as e:
        logger.error(f"Power monitoring error: {e}")
    finally:
        win32pdh.CloseQuery(query)
        logger.info("Power monitoring terminated.")
