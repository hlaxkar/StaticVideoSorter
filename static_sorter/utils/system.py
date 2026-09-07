"""
System runtime utilities: signal handling, dependency checks, and terminal state preservation.
"""
import atexit
import shutil
import signal
import sys
import threading
from typing import Dict, List, Tuple

try:
    import termios
except ImportError:
    termios = None


def check_system_dependencies() -> Tuple[bool, Dict[str, bool], List[str]]:
    """
    Check if all required system binaries and python modules are present.
    Returns (all_ok, details_dict, list_of_missing).
    """
    details: Dict[str, bool] = {}
    missing: List[str] = []

    # Binaries
    for binary in ("ffmpeg", "ffprobe"):
        found = shutil.which(binary) is not None
        details[binary] = found
        if not found:
            missing.append(f"{binary} (binary)")

    # Python packages
    try:
        import cv2  # noqa: F401
        details["opencv"] = True
    except ImportError:
        details["opencv"] = False
        missing.append("opencv-python-headless (python module)")

    try:
        import numpy  # noqa: F401
        details["numpy"] = True
    except ImportError:
        details["numpy"] = False
        missing.append("numpy (python module)")

    try:
        import PIL  # noqa: F401
        details["pillow"] = True
    except ImportError:
        details["pillow"] = False
        missing.append("pillow (python module)")

    try:
        import tqdm  # noqa: F401
        details["tqdm"] = True
    except ImportError:
        details["tqdm"] = False  # tqdm is optional / has fallback

    all_ok = len(missing) == 0
    return all_ok, details, missing


class TerminalStateGuard:
    """Safeguards terminal attributes across curses/opencv/tqdm modifications."""

    def __init__(self):
        self._saved_attrs = None

    def save(self):
        if termios is None:
            return
        try:
            self._saved_attrs = termios.tcgetattr(sys.stdin.fileno())
        except (termios.error, ValueError, OSError):
            self._saved_attrs = None

    def restore(self):
        if termios is None or self._saved_attrs is None:
            return
        try:
            termios.tcsetattr(
                sys.stdin.fileno(),
                termios.TCSADRAIN,
                self._saved_attrs,
            )
        except (termios.error, ValueError, OSError):
            pass


class GracefulInterruptHandler:
    """
    Tracks SIGINT (Ctrl+C) / SIGTERM signals.
    1st interrupt sets interrupt_requested -> worker loops stop spawning new tasks and drain.
    2nd interrupt forces immediate exit.
    """

    def __init__(self):
        self.interrupt_event = threading.Event()
        self.hard_stop_event = threading.Event()
        self._sig_count = 0
        self._lock = threading.Lock()
        self._installed = False

    def install(self):
        if not self._installed:
            signal.signal(signal.SIGINT, self._handle_signal)
            signal.signal(signal.SIGTERM, self._handle_signal)
            self._installed = True

    def _handle_signal(self, sig, frame):
        with self._lock:
            self._sig_count += 1
            if self._sig_count == 1:
                self.interrupt_event.set()
                sys.stderr.write(
                    "\n⚠️  Interrupt received — finishing in-flight videos and saving checkpoint...\n"
                    "   Press Ctrl+C again to terminate immediately.\n"
                )
                sys.stderr.flush()
            else:
                self.hard_stop_event.set()
                sys.stderr.write("\n🚨 Immediate termination requested. Exiting...\n")
                sys.stderr.flush()
                sys.exit(130)

    @property
    def is_interrupted(self) -> bool:
        return self.interrupt_event.is_set()
