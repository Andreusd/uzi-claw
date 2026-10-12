"""Asynchronous runner for direct terminal shell execution."""

import asyncio
import logging
import os
import re
import signal
from pathlib import Path
from typing import Optional, Any

logger = logging.getLogger(__name__)

CWD_MARKER = "___CWD_MARKER___"


class TerminalRunner:
    """Manages executing shell commands directly in bash."""

    def __init__(self):
        self._current_process: Optional[asyncio.subprocess.Process] = None
        self._is_cancelled = False

    @property
    def is_running(self) -> bool:
        """Check if a shell subprocess is actively running."""
        return self._current_process is not None and self._current_process.returncode is None

    async def interrupt(self) -> bool:
        """Interrupt the active subprocess (equivalent to pressing Ctrl+C in a terminal)."""
        proc = self._current_process
        if proc is None or proc.returncode is not None:
            return False

        self._is_cancelled = True
        logger.info("Attempting to interrupt running shell process PID %d (SIGINT)", proc.pid)
        try:
            proc.send_signal(signal.SIGINT)
            for _ in range(6):
                if proc.returncode is not None:
                    break
                await asyncio.sleep(0.5)

            if proc.returncode is None:
                logger.warning("Process did not exit on SIGINT, terminating PID %d", proc.pid)
                proc.terminate()
            return True
        except ProcessLookupError:
            return True
        except Exception as e:
            logger.error("Error interrupting shell process: %s", e)
            return False

    async def cancel(self) -> bool:
        """Cancel the active subprocess if running."""
        proc = self._current_process
        if proc is None or proc.returncode is not None:
            return False

        self._is_cancelled = True
        logger.info("Attempting to terminate running shell process PID %d", proc.pid)
        try:
            proc.terminate()
            for _ in range(6):
                if proc.returncode is not None:
                    break
                await asyncio.sleep(0.5)

            if proc.returncode is None:
                logger.warning("Force killing shell process PID %d", proc.pid)
                proc.kill()
            return True
        except ProcessLookupError:
            return True
        except Exception as e:
            logger.error("Error terminating shell process: %s", e)
            return False

    async def run(
        self,
        command: str,
        cwd: str,
        timeout: float = 120.0,
    ) -> dict[str, Any]:
        """
        Execute a bash command in cwd and return the result dict:
          - output: str
          - exit_code: Optional[int]
          - status: "SUCCESS" | "ERROR" | "TIMEOUT" | "CANCELLED"
          - new_cwd: Optional[str]
          - timeout: float
        """
        self._is_cancelled = False
        work_dir = cwd if Path(cwd).exists() and Path(cwd).is_dir() else str(Path.home())

        # Check if command contains cd
        has_cd = bool(re.search(r"(^|[;&|]\s*)cd(\s|$)", command.strip()))
        if has_cd:
            full_shell_cmd = f"{command} && echo '{CWD_MARKER}' && pwd -P"
        else:
            full_shell_cmd = command

        logger.info("Executing shell command in %s: %s", work_dir, command)

        try:
            self._current_process = await asyncio.create_subprocess_exec(
                "/bin/bash",
                "-c",
                full_shell_cmd,
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=os.environ.copy(),
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    self._current_process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning("Shell command timed out after %s seconds: %s", timeout, command)
                await self.cancel()
                return {
                    "output": "",
                    "exit_code": None,
                    "status": "TIMEOUT",
                    "new_cwd": None,
                    "timeout": timeout,
                }

            if self._is_cancelled:
                return {
                    "output": "",
                    "exit_code": None,
                    "status": "CANCELLED",
                    "new_cwd": None,
                    "timeout": timeout,
                }

            stdout_str = stdout_bytes.decode(errors="replace").strip()
            stderr_str = stderr_bytes.decode(errors="replace").strip()
            exit_code = self._current_process.returncode

            new_cwd = None
            if has_cd and CWD_MARKER in stdout_str:
                before_marker, after_marker = stdout_str.split(CWD_MARKER, 1)
                stdout_str = before_marker.rstrip("\r\n")
                lines = after_marker.strip().splitlines()
                if lines:
                    cand = lines[-1].strip()
                    if Path(cand).is_dir():
                        new_cwd = cand

            # Combine stdout and stderr
            combined = ""
            if stdout_str and stderr_str:
                combined = f"{stdout_str}\n{stderr_str}"
            elif stdout_str:
                combined = stdout_str
            elif stderr_str:
                combined = stderr_str

            status = "SUCCESS" if exit_code == 0 else "ERROR"

            return {
                "output": combined,
                "exit_code": exit_code,
                "status": status,
                "new_cwd": new_cwd,
                "timeout": timeout,
            }

        except asyncio.CancelledError:
            await self.cancel()
            return {
                "output": "",
                "exit_code": None,
                "status": "CANCELLED",
                "new_cwd": None,
                "timeout": timeout,
            }
        except Exception as e:
            logger.exception("Error executing shell command: %s", e)
            return {
                "output": str(e),
                "exit_code": -1,
                "status": "ERROR",
                "new_cwd": None,
                "timeout": timeout,
            }
        finally:
            self._current_process = None
