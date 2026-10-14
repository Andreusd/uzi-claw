"""Asynchronous runner for Antigravity CLI (agy) headless process."""

import asyncio
import json
import logging
import signal
from typing import AsyncGenerator, Callable, Awaitable, Optional, Any
from pathlib import Path

from .config import AGY_BIN_PATH

logger = logging.getLogger(__name__)


class AntigravityRunner:
    """Manages spawning and streaming interaction with agy CLI."""

    def __init__(self, bin_path: str = AGY_BIN_PATH):
        self.bin_path = bin_path
        self._current_process: Optional[asyncio.subprocess.Process] = None
        self._is_cancelled = False

    @property
    def is_running(self) -> bool:
        """Check if an agy subprocess is actively running."""
        return self._current_process is not None and self._current_process.returncode is None

    async def interrupt(self) -> bool:
        """Interrupt the active subprocess (equivalent to pressing Esc in agy CLI / sending SIGINT)."""
        if not self.is_running or self._current_process is None:
            return False

        self._is_cancelled = True
        logger.info("Attempting to interrupt running agy process PID %d with SIGINT (Esc)", self._current_process.pid)
        try:
            self._current_process.send_signal(signal.SIGINT)
            for _ in range(6):
                if self._current_process.returncode is not None:
                    break
                await asyncio.sleep(0.5)

            if self._current_process.returncode is None:
                logger.warning("Force terminating agy process PID %d after SIGINT timeout", self._current_process.pid)
                self._current_process.terminate()
            return True
        except ProcessLookupError:
            return True
        except Exception as e:
            logger.error("Error interrupting agy process: %s", e)
            return False

    async def cancel(self) -> bool:
        """Cancel the active subprocess if running."""
        if not self.is_running or self._current_process is None:
            return False

        self._is_cancelled = True
        logger.info("Attempting to terminate running agy process PID %d", self._current_process.pid)
        try:
            self._current_process.terminate()
            # Wait up to 3 seconds for graceful shutdown
            for _ in range(6):
                if self._current_process.returncode is not None:
                    break
                await asyncio.sleep(0.5)

            if self._current_process.returncode is None:
                logger.warning("Force killing agy process PID %d", self._current_process.pid)
                self._current_process.kill()
            return True
        except Exception as e:
            logger.error("Error terminating agy process: %s", e)
            return False

    async def run(
        self,
        prompt: str,
        conversation_id: Optional[str] = None,
        cwd: Optional[str] = None,
        autopilot: bool = True,
        mode: Optional[str] = None,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        event_callback: Optional[Callable[[str, Any], Awaitable[None]]] = None,
    ) -> dict[str, Any]:
        """
        Execute an agy turn and stream JSON events.
        
        Returns a dict containing:
          - conversation_id: str
          - response: str
          - status: "SUCCESS" | "CANCELLED" | "ERROR"
          - error: Optional[str]
        """
        self._is_cancelled = False

        cmd = [self.bin_path, "--output-format=stream-json"]

        if conversation_id:
            cmd.append(f"--conversation={conversation_id}")
        if autopilot:
            cmd.append("--dangerously-skip-permissions")
        if mode:
            cmd.append(f"--mode={mode}")
        if model:
            cmd.append(f"--model={model}")
        if effort:
            cmd.append(f"--effort={effort}")

        cmd.append(f"-p={prompt}")

        work_dir = cwd or str(Path.home())
        logger.info("Executing agy in %s: conversation_id=%s, autopilot=%s", work_dir, conversation_id, autopilot)

        captured_conversation_id = conversation_id
        final_response = ""
        full_text_stream: list[str] = []
        stderr_lines: list[str] = []

        try:
            self._current_process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=work_dir,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Background task to capture stderr
            async def read_stderr():
                if not self._current_process or not self._current_process.stderr:
                    return
                while True:
                    line = await self._current_process.stderr.readline()
                    if not line:
                        break
                    text = line.decode(errors="replace").strip()
                    if text:
                        stderr_lines.append(text)
                        logger.debug("[agy stderr] %s", text)

            stderr_task = asyncio.create_task(read_stderr())

            # Read stdout stream-json line by line
            assert self._current_process.stdout is not None
            while True:
                raw_line = await self._current_process.stdout.readline()
                if not raw_line:
                    break

                line_str = raw_line.decode(errors="replace").strip()
                if not line_str:
                    continue

                try:
                    payload = json.loads(line_str)
                except json.JSONDecodeError:
                    logger.debug("Non-JSON stdout line: %s", line_str)
                    continue

                event_type = payload.get("event")

                if event_type == "init":
                    cid = payload.get("conversation_id")
                    if cid:
                        captured_conversation_id = cid
                    if event_callback:
                        await event_callback("init", {"conversation_id": captured_conversation_id})

                elif event_type == "step_update":
                    step = payload.get("step_update", {})
                    step_type = step.get("step_type")
                    state = step.get("state")

                    if step_type == "agent_response":
                        delta = step.get("text_delta", "")
                        if delta:
                            full_text_stream.append(delta)
                            if event_callback:
                                await event_callback("text_delta", delta)

                    elif step_type == "tool":
                        tool_name = step.get("tool_name") or step.get("tool_info", {}).get("name", "tool")
                        params = step.get("tool_info", {}).get("parameters", {})
                        if state == "ACTIVE":
                            if event_callback:
                                await event_callback("tool_start", {"name": tool_name, "parameters": params})
                        elif state == "DONE":
                            output = step.get("tool_info", {}).get("output", "")
                            if event_callback:
                                await event_callback("tool_done", {"name": tool_name, "output": output})

                    elif step_type == "thought":
                        thought = step.get("text_delta", "")
                        if thought and event_callback:
                            await event_callback("thought", thought)

                elif event_type == "result":
                    res = payload.get("result", {})
                    cid = res.get("conversation_id")
                    if cid:
                        captured_conversation_id = cid
                    resp = res.get("response", "")
                    if resp:
                        final_response = resp
                    if event_callback:
                        await event_callback("result", res)

            await self._current_process.wait()
            await stderr_task

            retcode = self._current_process.returncode
            stderr_joined = "\n".join(stderr_lines).strip()
            is_interrupted = (
                retcode in (-signal.SIGINT, -signal.SIGTERM, 130, 143)
                or "interrupted" in stderr_joined.lower()
            )

            if self._is_cancelled or is_interrupted:
                return {
                    "conversation_id": captured_conversation_id,
                    "response": "".join(full_text_stream) or "⏹️ Execução cancelada ou interrompida.",
                    "status": "CANCELLED",
                    "error": None,
                }

            if retcode != 0 and not final_response:
                error_msg = stderr_joined or f"Process exited with code {retcode}"
                return {
                    "conversation_id": captured_conversation_id,
                    "response": "",
                    "status": "ERROR",
                    "error": error_msg,
                }

            if not final_response and full_text_stream:
                final_response = "".join(full_text_stream)

            return {
                "conversation_id": captured_conversation_id,
                "response": final_response or "".join(full_text_stream),
                "status": "SUCCESS",
                "error": None,
            }

        except asyncio.CancelledError:
            await self.cancel()
            return {
                "conversation_id": captured_conversation_id,
                "response": "".join(full_text_stream) or "Execution cancelled.",
                "status": "CANCELLED",
                "error": None,
            }
        except Exception as ex:
            logger.exception("Error executing agy: %s", ex)
            return {
                "conversation_id": captured_conversation_id,
                "response": "",
                "status": "ERROR",
                "error": str(ex),
            }
        finally:
            self._current_process = None


async def get_usage(bin_path: str = AGY_BIN_PATH) -> tuple[Optional[dict], Optional[str]]:
    """
    Fetch usage and quota limits from Antigravity CLI using `agy --output-format=stream-json -p=/usage`.

    Returns:
        (usage_data_dict, raw_response_str)
    """
    cmd = [bin_path, "--output-format=stream-json", "-p=/usage"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30.0)

        usage_data = None
        raw_response = ""

        for line in stdout.decode(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
                if payload.get("event") == "command_result":
                    cmd_info = payload.get("command", {})
                    if cmd_info.get("name") == "usage":
                        usage_data = cmd_info.get("data")
                elif payload.get("event") == "result":
                    res = payload.get("result", {})
                    if res.get("response"):
                        raw_response = res.get("response").strip()
            except json.JSONDecodeError:
                pass

        if not usage_data and not raw_response and stdout:
            # Fallback if output format wasn't stream-json
            raw_response = stdout.decode(errors="replace").strip()

        return usage_data, raw_response
    except Exception as e:
        logger.error("Failed to fetch usage from agy: %s", e)
        return None, None

