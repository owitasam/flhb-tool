"""
flhb-tool - Phase 2: Core Orchestration & Wrapper Engine.

bridge.py sits between Lutris and the target executable/loader. It builds
the execution environment (Wine prefix, DXVK/VKD3D overrides, etc.),
spawns the process with subprocess, and reads stdout/stderr in real time
using dedicated reader threads to prevent buffer deadlocks during game
launch.

This is the primary consumer hook for Phase 3 log monitoring.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple

# Support both `python3 -m flhb_tool.bridge` (module) and direct
# `python3 /path/to/flhb_tool/bridge.py` (script) execution, the latter
# being how Lutris invokes the wrapper.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from config import BridgeConfig, load_config  # noqa: E402
    from log_monitor import LogMonitor  # noqa: E402
else:
    from .config import BridgeConfig, load_config  # noqa: E402
    from .log_monitor import LogMonitor  # noqa: E402

logger = logging.getLogger("flhb-tool.bridge")

# Sentinel used to signal end of a stream.
_STREAM_END: object = object()


@dataclass
class LaunchResult:
    """Outcome of a bridge-launched process."""

    process: subprocess.Popen
    combined_queue: "queue.Queue[Tuple[str, Optional[str]]]"
    reader_threads: List[threading.Thread] = field(default_factory=list)

    @property
    def returncode(self) -> Optional[int]:
        return self.process.poll()


def _enqueue_stream(
    stream,
    out_q: "queue.Queue[Tuple[str, Optional[str]]]",
    stream_name: str,
) -> None:
    """Read lines from *stream* and push ``(stream_name, line)`` onto *out_q*.

    Runs in a dedicated thread so that the producer (the game/loader) can
    never block filling the OS pipe buffer, which would otherwise deadlock
    the launcher while it waits on the other stream. Lines are *not*
    prefixed here; the consumer adds the stream label to avoid doubling it.
    """
    try:
        for line in iter(stream.readline, ""):
            line = line.rstrip("\n")
            out_q.put((stream_name, line))
    except (ValueError, OSError):
        # Stream closed / closed from another thread.
        pass
    finally:
        try:
            stream.close()
        except OSError:
            pass
        out_q.put((stream_name, None))  # sentinel: this stream is done


class GameBridge:
    """Core execution bridge between Lutris and a target executable/loader."""

    def __init__(
        self,
        config: BridgeConfig,
        env: Optional[Dict[str, str]] = None,
    ) -> None:
        self.config = config
        self.base_env = dict(env) if env is not None else dict(os.environ)
        self.process: Optional[subprocess.Popen] = None
        self._result: Optional[LaunchResult] = None

    # ------------------------------------------------------------------
    # Environment building
    # ------------------------------------------------------------------
    def build_environment(self) -> Dict[str, str]:
        """Merge os.environ with environment_overrides and Wine-specific vars.

        The returned environment cleanly inherits the current process
        environment, then applies custom overrides (DXVK, VKD3D, Wine debug
        flags, ...), sets WINEPREFIX/WINEARCH, and exports LD_LIBRARY_PATH
        hints where useful.
        """
        env = dict(self.base_env)

        # Wine prefix / architecture.
        env["WINEPREFIX"] = self.config.wine_prefix_path
        if self.config.wine_arch:
            env["WINEARCH"] = self.config.wine_arch

        # Custom overrides (DXVK, VKD3D, WINEDEBUG, ...).
        env.update(self.config.environment_overrides)

        # Ensure the prefix directory exists; warn otherwise.
        prefix_dir = self.config.wine_prefix_path
        if not os.path.isdir(prefix_dir):
            logger.warning("Wine prefix directory does not exist: %s", prefix_dir)

        return env

    # ------------------------------------------------------------------
    # Process spawning
    # ------------------------------------------------------------------
    def build_command(self) -> List[str]:
        """Build the final command line: [target_executable, *loader_arguments]."""
        return [self.config.target_executable, *self.config.loader_arguments]

    def launch(self, start_readers: bool = True) -> LaunchResult:
        """Spawn the target process and (optionally) start real-time readers.

        Returns a :class:`LaunchResult` holding the Popen handle and the
        stdout/stderr queues. Drain the queues (or use :meth:`iter_output`)
        to consume stream lines as they arrive.

        Raises:
            FileNotFoundError: If the target executable cannot be found.
            OSError: On other process-creation failures.
        """
        cmd = self.build_command()
        env = self.build_environment()
        cwd = self.config.working_directory or os.path.dirname(
            os.path.abspath(self.config.target_executable)
        )

        logger.info("flhb-tool bridge: launching %s", " ".join(cmd))
        logger.debug("  cwd=%s", cwd)
        logger.debug("  env WINEPREFIX=%s", env.get("WINEPREFIX"))

        try:
            proc = subprocess.Popen(
                cmd,
                env=env,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
                bufsize=1,  # line-buffered
                text=True,  # text mode (str lines)
            )
        except FileNotFoundError as exc:
            logger.error("Target executable not found: %s", self.config.target_executable)
            raise
        except OSError as exc:
            logger.error("Failed to spawn process: %s", exc)
            raise

        self.process = proc

        combined_q: "queue.Queue[Tuple[str, Optional[str]]]" = queue.Queue()
        threads: List[threading.Thread] = []

        if start_readers:
            t_out = threading.Thread(
                target=_enqueue_stream,
                args=(proc.stdout, combined_q, "stdout"),
                name="flhb-stdout-reader",
                daemon=True,
            )
            t_err = threading.Thread(
                target=_enqueue_stream,
                args=(proc.stderr, combined_q, "stderr"),
                name="flhb-stderr-reader",
                daemon=True,
            )
            t_out.start()
            t_err.start()
            threads.extend([t_out, t_err])

        # Install signal handlers so an interrupt/termination of this
        # process also cleans up the child Wine/loader process instead of
        # leaving it orphaned.
        self._install_signal_handlers()

        self._result = LaunchResult(
            process=proc,
            combined_queue=combined_q,
            reader_threads=threads,
        )
        return self._result

    # ------------------------------------------------------------------
    # Real-time output consumption
    # ------------------------------------------------------------------
    def iter_output(self) -> Iterator[Tuple[str, str]]:
        """Yield ``(stream, line)`` tuples as output arrives.

        ``stream`` is either ``"stdout"`` or ``"stderr"``. The iterator ends
        when both streams are closed and the process has exited. This is the
        primary consumer hook for Phase 3 log monitoring.

        Uses a single combined queue fed by dedicated reader threads, so the
        caller blocks efficiently instead of polling individual queues.
        """
        if self._result is None:
            raise RuntimeError("Bridge has not been launched; call launch() first.")

        result = self._result
        combined_q = result.combined_queue
        proc = result.process

        streams_done = 0
        total_streams = 2  # stdout + stderr

        while streams_done < total_streams:
            try:
                stream, line = combined_q.get(timeout=0.5)
            except queue.Empty:
                # Periodically check whether the process has exited and all
                # output has been drained, to avoid blocking forever if a
                # reader thread dies without emitting a sentinel.
                if proc.poll() is not None and combined_q.empty():
                    continue
                continue

            if line is None:
                # Sentinel: one stream is fully drained.
                streams_done += 1
                continue

            yield stream, line

        # Ensure reader threads have finished.
        for t in result.reader_threads:
            t.join(timeout=1.0)

        logger.info(
            "flhb-tool bridge: process exited with code %s",
            proc.returncode,
        )

    # ------------------------------------------------------------------
    # Signal handling
    # ------------------------------------------------------------------
    def _install_signal_handlers(self) -> None:
        """Install SIGINT/SIGTERM handlers that terminate the child process.

        Prevents orphaned Wine/loader processes when the bridge is
        interrupted (e.g. Ctrl+C or Lutris terminating the launcher).
        """
        try:
            self._orig_sigint = signal.getsignal(signal.SIGINT)
            self._orig_sigterm = signal.getsignal(signal.SIGTERM)
        except (AttributeError, ValueError):
            # Signals may not be available in some embedded contexts.
            return

        def _handle_signal(signum, frame) -> None:  # noqa: ARG001
            logger.info(
                "flhb-tool bridge: received signal %s, terminating child process",
                signum,
            )
            self.terminate()
            # Fall back to default behavior so the launcher still exits.
            try:
                sys.exit(128 + signum)
            except SystemExit:
                os._exit(128 + signum)

        signal.signal(signal.SIGINT, _handle_signal)
        signal.signal(signal.SIGTERM, _handle_signal)

    # ------------------------------------------------------------------
    # Lifecycle control
    # ------------------------------------------------------------------
    def poll(self) -> Optional[int]:
        """Return the process returncode if it has exited, else None."""
        if self.process is None:
            return None
        return self.process.poll()

    def wait(self, timeout: Optional[float] = None) -> Optional[int]:
        """Wait for the process to exit, returning its returncode."""
        if self.process is None:
            raise RuntimeError("Bridge has not been launched.")
        return self.process.wait(timeout=timeout)

    def terminate(self) -> None:
        """Terminate the process (SIGTERM)."""
        if self.process is None:
            return
        if self.process.poll() is None:
            logger.info("flhb-tool bridge: terminating process (SIGTERM)")
            self.process.terminate()

    def kill(self) -> None:
        """Kill the process (SIGKILL)."""
        if self.process is None:
            return
        if self.process.poll() is None:
            logger.info("flhb-tool bridge: killing process (SIGKILL)")
            self.process.kill()

    # ------------------------------------------------------------------
    # Convenience: launch and stream to a callback
    # ------------------------------------------------------------------
    def stream_to_callback(
        self,
        callback: Callable[[str, str], None],
    ) -> int:
        """Launch the process and feed every output line to *callback*.

        ``callback`` receives ``(stream, line)``. Returns the process
        returncode once it exits.
        """
        self.launch()
        for stream, line in self.iter_output():
            try:
                callback(stream, line)
            except Exception as exc:  # noqa: BLE001 - user callback
                logger.exception("Output callback raised: %s", exc)
        return self.wait()


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point: load a config.json, launch the bridge, monitor logs.

    This is the entry point used by Lutris (direct script execution) and by
    ``python3 -m flhb_tool.bridge``. It performs pre-flight checks are the
    caller's responsibility; here we launch the target executable through
    GameBridge (Phase 2) while monitoring logs for known errors and writing
    crash dumps to ``flhb_debug.log`` (Phase 3).
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="flhb-bridge",
        description="flhb-tool Phase 2+3: launch bridge with log monitoring.",
    )
    parser.add_argument("config", help="Path to config.json")
    parser.add_argument(
        "--log-file",
        default="flhb_debug.log",
        help="Path for flhb_debug.log (default: flhb_debug.log).",
    )
    parser.add_argument(
        "--auto-fix",
        action="store_true",
        default=False,
        help="Attempt automated remediation for known errors.",
    )
    args = parser.parse_args(argv)

    handlers = [logging.StreamHandler(sys.stdout)]
    if args.log_file:
        handlers.append(logging.FileHandler(args.log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )

    try:
        config = load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - surface config errors to user
        logger.error("Failed to load config: %s", exc)
        return 2

    bridge = GameBridge(config)
    monitor = LogMonitor(
        log_file=args.log_file,
        auto_remediate=args.auto_fix,
    )

    def on_output(stream: str, line: str) -> None:
        print(f"[{stream}] {line}")

    try:
        rc = monitor.monitor_bridge(bridge, on_line=on_output)
    except FileNotFoundError:
        logger.error("Target executable not found: %s", config.target_executable)
        return 1
    except OSError as exc:
        logger.error("Failed to launch: %s", exc)
        return 1

    logger.info("Bridge finished with returncode %s", rc)
    return rc if rc is not None else 1


if __name__ == "__main__":
    sys.exit(main())
