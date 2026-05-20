import sys
import threading
import contextlib

class AgentCancelled(Exception):
    """Exception raised when agent execution is cancelled by the user."""
    def __init__(self, partial_text: str = "", reasoning_content: str = "") -> None:
        self.partial_text = partial_text
        self.reasoning_content = reasoning_content
        super().__init__("Agent execution cancelled by user")

class CancellationToken:
    """Thread-safe cancellation token based on threading.Event."""
    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    def reset(self) -> None:
        self._event.clear()

    def check(self) -> None:
        if self.cancelled:
            raise AgentCancelled()

class EscKeyMonitor:
    """Background thread that monitors stdin for ESC key presses during execution."""
    def __init__(self, token: CancellationToken) -> None:
        self.token = token
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if not sys.stdin.isatty():
            return
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)

    @contextlib.contextmanager
    def paused(self):
        """Temporarily pause monitoring (e.g. during interactive user prompts)."""
        self._pause_event.set()
        try:
            yield
        finally:
            self._pause_event.clear()

    def _monitor_loop(self) -> None:
        if sys.platform == "win32":
            import msvcrt
            import time
            while not self._stop_event.is_set():
                if self._pause_event.is_set():
                    time.sleep(0.05)
                    continue

                if msvcrt.kbhit():
                    char = msvcrt.getch()
                    # ESC is b'\x1b'
                    if char == b"\x1b":
                        self.token.cancel()
                        break
                    else:
                        # Put it back in the buffer for normal reads
                        try:
                            msvcrt.ungetch(char)
                        except OSError:
                            pass
                else:
                    time.sleep(0.05)
        else:
            import termios
            import tty
            import select
            import os
            
            fd = sys.stdin.fileno()
            try:
                old_settings = termios.tcgetattr(fd)
            except Exception:
                return

            try:
                tty.setcbreak(fd)
                while not self._stop_event.is_set():
                    if self._pause_event.is_set():
                        self._stop_event.wait(0.05)
                        continue

                    r, _, _ = select.select([sys.stdin], [], [], 0.05)
                    if r:
                        char = os.read(fd, 1)
                        if char == b"\x1b":
                            # Wait up to 50ms to see if more chars follow (to distinguish from arrow keys like \x1b[A)
                            r2, _, _ = select.select([sys.stdin], [], [], 0.05)
                            if r2:
                                # Consume the rest of the escape sequence (e.g. arrow keys)
                                os.read(fd, 8)
                            else:
                                self.token.cancel()
                                break
            except Exception:
                pass
            finally:
                try:
                    termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
                except Exception:
                    pass
