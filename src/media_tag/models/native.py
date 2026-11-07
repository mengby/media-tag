"""App-local Windows C++ runtime; never change the system runtime or GPU driver."""

import os
from pathlib import Path
import sys

from .contracts import ModelFailure

_handles = []


def prepare_native_runtime():
    if os.name != 'nt' or _handles:
        return
    import ctypes
    directory = Path(sys.prefix) / 'Scripts'
    try:
        _handles.append(os.add_dll_directory(str(directory)))
        for name in ('msvcp140.dll', 'msvcp140_1.dll', 'msvcp140_2.dll', 'msvcp140_atomic_wait.dll'):
            _handles.append(ctypes.WinDLL(str(directory / name)))
    except OSError:
        _handles.clear()
        raise ModelFailure('native_runtime_unavailable') from None
