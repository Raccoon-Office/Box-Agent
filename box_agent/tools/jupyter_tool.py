"""Python code execution sandbox with persistent kernel via Jupyter.

This module provides a sandboxed Python execution environment using a persistent
Jupyter kernel with its own isolated virtual environment. Variables and imports
persist between executions. The sandbox venv is separate from box-agent's own
environment, so user-installed packages don't pollute the tool's dependencies.
"""

import asyncio
import json
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Optional

from .base import Tool, ToolResult

# Default packages installed in the sandbox venv on first startup
SANDBOX_DEFAULT_PACKAGES = [
    "pandas",
    "numpy",
    "matplotlib",
    "seaborn",
    "requests",
    "openpyxl",  # Excel support for pandas
]

SANDBOX_BASE_DIR = Path.home() / ".box-agent" / "sandbox"


class SandboxEnvironment:
    """Manages an isolated Python virtual environment for the sandbox.

    Creates a venv at ~/.box-agent/sandbox/venv/ with pip and default
    data-science packages. The kernel runs inside this venv so user
    packages are isolated from box-agent itself.
    """

    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or SANDBOX_BASE_DIR
        self.venv_dir = self.base_dir / "venv"
        self.python_path = self.venv_dir / "bin" / "python"
        self._ready = False

    @property
    def is_created(self) -> bool:
        """Check if the venv already exists."""
        return self.python_path.exists()

    async def ensure_ready(self, on_progress: Any = None) -> None:
        """Ensure the sandbox venv is created and has default packages.

        Args:
            on_progress: Optional callback(message: str) for progress updates.
        """
        if self._ready:
            return

        if not self.is_created:
            await self._create_venv(on_progress)
            await self._install_defaults(on_progress)
        else:
            # Venv exists, just verify it works
            if on_progress:
                on_progress("Verifying sandbox environment...")

        # Install ipykernel in sandbox venv (needed for kernel)
        await self._ensure_ipykernel(on_progress)
        self._ready = True

    async def _create_venv(self, on_progress: Any = None) -> None:
        """Create the sandbox virtual environment."""
        if on_progress:
            on_progress(f"Creating sandbox environment at {self.venv_dir}...")

        self.venv_dir.parent.mkdir(parents=True, exist_ok=True)

        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "venv", str(self.venv_dir),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to create sandbox venv: {stderr.decode()}")

        # Ensure pip is available (some systems skip it)
        proc = await asyncio.create_subprocess_exec(
            str(self.python_path), "-m", "ensurepip", "--upgrade",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        if on_progress:
            on_progress("Sandbox environment created.")

    async def _install_defaults(self, on_progress: Any = None) -> None:
        """Install default packages into the sandbox venv."""
        if on_progress:
            on_progress(f"Installing default packages: {', '.join(SANDBOX_DEFAULT_PACKAGES)}...")

        proc = await asyncio.create_subprocess_exec(
            str(self.python_path), "-m", "pip", "install",
            "--quiet", *SANDBOX_DEFAULT_PACKAGES,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            # Non-fatal: some packages may fail on some platforms
            if on_progress:
                on_progress(f"Warning: some packages failed to install: {stderr.decode()[:200]}")
        else:
            if on_progress:
                on_progress("Default packages installed.")

    async def _ensure_ipykernel(self, on_progress: Any = None) -> None:
        """Ensure ipykernel is installed in the sandbox venv."""
        # Check if already installed
        proc = await asyncio.create_subprocess_exec(
            str(self.python_path), "-c", "import ipykernel",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()
        if proc.returncode == 0:
            return

        if on_progress:
            on_progress("Installing ipykernel in sandbox...")

        proc = await asyncio.create_subprocess_exec(
            str(self.python_path), "-m", "pip", "install",
            "--quiet", "ipykernel",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(f"Failed to install ipykernel in sandbox: {stderr.decode()}")

    def get_kernel_spec(self) -> dict:
        """Get kernel spec that uses the sandbox venv Python."""
        return {
            "argv": [
                str(self.python_path),
                "-m", "ipykernel_launcher",
                "-f", "{connection_file}",
            ],
            "display_name": "Box-Agent Sandbox",
            "language": "python",
        }

    def get_kernel_spec_dir(self) -> Path:
        """Get path to the kernel spec directory, creating it if needed."""
        spec_dir = self.base_dir / "kernelspec" / "box-agent-sandbox"
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_file = spec_dir / "kernel.json"
        # Always write fresh spec (python path may change)
        spec_file.write_text(json.dumps(self.get_kernel_spec(), indent=2))
        return spec_dir

    async def install_packages(self, packages: list[str]) -> tuple[bool, str]:
        """Install additional packages into the sandbox venv.

        Args:
            packages: List of package names to install.

        Returns:
            (success, message) tuple.
        """
        proc = await asyncio.create_subprocess_exec(
            str(self.python_path), "-m", "pip", "install",
            *packages,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        output = stdout.decode() + stderr.decode()
        if proc.returncode == 0:
            return True, output
        return False, output


class JupyterKernelSession:
    """A persistent Jupyter kernel session with full state persistence.

    Uses jupyter_client to manage a kernel that runs inside the sandbox
    venv, so all packages are isolated from box-agent.
    """

    def __init__(self, session_id: str, workspace: Path, sandbox_env: SandboxEnvironment):
        self.session_id = session_id
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.sandbox_env = sandbox_env
        self._context = None  # run_kernel context
        self._kc = None

    async def start(self):
        """Start kernel using the sandbox venv's Python."""
        import jupyter_client

        # Get the kernel spec directory pointing to sandbox Python
        kernel_spec_dir = self.sandbox_env.get_kernel_spec_dir()

        # Load the kernel spec from our custom directory
        from jupyter_client.kernelspec import KernelSpec
        kernel_spec = KernelSpec.from_resource_dir(str(kernel_spec_dir))

        # Create KernelManager with our custom spec
        km = jupyter_client.KernelManager()
        km._kernel_spec = kernel_spec  # Override the kernel spec
        km.start_kernel()
        self._km = km
        self._kc = km.client()
        self._kc.start_channels()
        self._kc.wait_for_ready(timeout=30)

        # Run setup
        setup_code = f"""
import os
os.chdir(r'{self.workspace}')

# Set up matplotlib (non-interactive)
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plt.rcParams['figure.figsize'] = (10, 6)
    plt.rcParams['savefig.dpi'] = 100
except ImportError:
    pass
"""
        self._kc.execute(setup_code)
        # Wait for setup to complete
        try:
            while True:
                msg = self._kc.get_iopub_msg(timeout=10)
                if msg.get("msg_type") == "status" and msg.get("content", {}).get("execution_state") == "idle":
                    break
        except Exception:
            pass

    def is_alive(self) -> bool:
        """Check if kernel is alive."""
        if self._km is None:
            return False
        try:
            return self._km.is_alive()
        except Exception:
            return False

    def execute(self, code: str, timeout: int = 60) -> tuple[str, list[str], Optional[str]]:
        """Execute code and return (stdout, images, error)."""
        if self._kc is None:
            return "", [], "Kernel not initialized"

        # Drain any pending IOPub messages from previous operations
        while True:
            try:
                self._kc.get_iopub_msg(timeout=0.1)
            except Exception:
                break

        stdout_parts = []
        stderr_parts = []
        error_parts = []
        images = []

        try:
            # Execute user code
            msg_id = self._kc.execute(code, silent=False)

            # Collect outputs via IOPub
            idle_received = False
            while not idle_received:
                try:
                    msg = self._kc.get_iopub_msg(timeout=timeout)
                    msg_type = msg.get("msg_type")
                    content = msg.get("content", {})

                    if msg_type == "status":
                        if content.get("execution_state") == "idle":
                            idle_received = True

                    elif msg_type == "stream":
                        name = content.get("name", "")
                        text = content.get("text", "")
                        # Strip ANSI escape codes
                        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
                        if name == "stdout":
                            stdout_parts.append(text)
                        elif name == "stderr":
                            # Collect stderr separately — only treat as error
                            # if there's also an explicit error message
                            stderr_parts.append(text)

                    elif msg_type == "error":
                        error_parts.append(f"{content.get('ename')}: {content.get('evalue')}")

                    elif msg_type in ("display_data", "execute_result"):
                        data = content.get("data", {})
                        if "image/png" in data:
                            images.append("[PNG Image]")

                except Exception:
                    break

            # Only report errors from explicit error messages (not stderr)
            # stderr often contains warnings, pip notices, etc.
            if error_parts:
                return "", [], "\n".join(error_parts)

        except Exception as e:
            return "", [], f"Execution failed: {str(e)}"

        # Include stderr as part of stdout (warnings, pip output, etc.)
        all_output = "".join(stdout_parts)
        if stderr_parts:
            stderr_text = "".join(stderr_parts).strip()
            if stderr_text:
                if all_output:
                    all_output += "\n" + stderr_text
                else:
                    all_output = stderr_text

        # Check for matplotlib images
        for ext in ["png", "jpg", "jpeg"]:
            for img in self.workspace.glob(f"*.{ext}"):
                images.append(f"[{img.name}]")

        return all_output, images, None

    async def stop(self):
        """Stop the kernel."""
        if self._kc:
            try:
                self._kc.stop_channels()
            except Exception:
                pass
            self._kc = None
        if self._km:
            try:
                self._km.shutdown_kernel(now=True)
            except Exception:
                pass
            self._km = None


class JupyterSandboxTool(Tool):
    """Execute Python code in a persistent Jupyter kernel sandbox.

    This tool provides:
    - Isolated venv with pip (separate from box-agent)
    - Default data-science packages (pandas, numpy, matplotlib, etc.)
    - Full state persistence (variables, functions, imports persist)
    - Session-based kernel isolation
    - %pip install support for additional packages
    """

    _sessions: dict[str, JupyterKernelSession] = {}
    _sandbox_env: SandboxEnvironment | None = None

    def __init__(self, workspace_dir: str | None = None):
        """Initialize sandbox tool.

        Args:
            workspace_dir: Base workspace directory for sandbox sessions.
        """
        self.workspace_dir = workspace_dir
        self._session_id: Optional[str] = None

    @classmethod
    def _get_sandbox_env(cls) -> SandboxEnvironment:
        """Get or create the shared sandbox environment."""
        if cls._sandbox_env is None:
            cls._sandbox_env = SandboxEnvironment()
        return cls._sandbox_env

    def get_status(self) -> dict[str, Any]:
        """Get current sandbox status."""
        env = self._get_sandbox_env()
        sessions_info = []
        for sid, session in self._sessions.items():
            sessions_info.append({
                "session_id": sid,
                "is_alive": session.is_alive(),
                "workspace": str(session.workspace),
            })

        return {
            "current_session_id": self._session_id,
            "sessions": sessions_info,
            "total_sessions": len(self._sessions),
            "venv_path": str(env.venv_dir),
            "venv_exists": env.is_created,
        }

    @property
    def name(self) -> str:
        return "execute_code"

    @property
    def description(self) -> str:
        return """Execute Python code in a persistent Jupyter kernel sandbox.

This tool runs Python code in a **real Jupyter kernel** with its own isolated environment:
- Variables, functions, classes, imports all persist between calls
- Pre-installed packages: pandas, numpy, matplotlib, seaborn, requests, openpyxl
- Install more packages with: %pip install <package>
- Ideal for data analysis: load once, analyze many times

Example workflow:
  1. execute_code(code="import pandas as pd\\ndf = pd.read_csv('data.csv')")
  2. execute_code(code="print(df.describe())")
  3. execute_code(code="%pip install scikit-learn")  # Install on demand
  4. execute_code(code="from sklearn.cluster import KMeans")

**Full Python state persists in the same session!**

Best practices:
- Break complex analysis into steps
- Use print() to see intermediate results
- Use %pip install for any missing packages

Output formats:
- Text output (print statements, repr)
- Images (matplotlib plots)
- Errors (simplified tracebacks)
"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python code to execute. Variables and functions from previous calls in the same session are available. Use %pip install <pkg> to install packages.",
                },
                "session_id": {
                    "type": "string",
                    "description": "Session ID for persistent kernel. Same session_id shares all state. Auto-generated if not provided.",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Execution timeout in seconds (default: 60, max: 300)",
                    "default": 60,
                },
            },
            "required": ["code"],
        }

    async def execute(
        self,
        code: str,
        session_id: Optional[str] = None,
        timeout: int = 60,
    ) -> ToolResult:
        """Execute Python code in sandbox with persistent kernel.

        Args:
            code: Python code to execute
            session_id: Session ID for persistent kernel
            timeout: Execution timeout in seconds

        Returns:
            ToolResult with execution output, images, or errors
        """
        timeout = min(max(1, timeout), 300)

        if not self._is_valid_code(code):
            return ToolResult(
                success=False,
                content="",
                error="Code appears to be empty or contains only comments.",
            )

        # Ensure sandbox environment is ready
        env = self._get_sandbox_env()
        try:
            await env.ensure_ready(on_progress=lambda msg: None)
        except RuntimeError as e:
            return ToolResult(
                success=False,
                content="",
                error=f"Failed to initialize sandbox environment: {e}",
            )

        # Create or get session
        if session_id is None:
            session_id = self._session_id or str(uuid.uuid4())[:8]
        self._session_id = session_id

        # Check kernel health if session already exists
        existing_session = self._sessions.get(self._session_id)
        if existing_session and not existing_session.is_alive():
            old_session_id = self._session_id
            del self._sessions[self._session_id]
            existing_session = None
            session_id = str(uuid.uuid4())[:8]
            self._session_id = session_id
            workspace = self._get_workspace(session_id)
            session = JupyterKernelSession(session_id, workspace, env)
            await session.start()
            self._sessions[session_id] = session
            return ToolResult(
                success=False,
                content="",
                error=f"Sandbox kernel died (old session={old_session_id}). Auto-restarted with new session={session_id}. Please retry your code.",
            )

        # Get or create kernel session
        if session_id not in self._sessions:
            workspace = self._get_workspace(session_id)
            session = JupyterKernelSession(session_id, workspace, env)
            await session.start()
            self._sessions[session_id] = session

        session = self._sessions[session_id]

        # Execute code
        try:
            stdout, images, error = session.execute(code, timeout)

            if error:
                return ToolResult(
                    success=False,
                    content="",
                    error=self._simplify_error(error),
                )

            content_parts = []
            if stdout.strip():
                content_parts.append(stdout.strip())
            if images:
                content_parts.append("\n".join(images))

            content = "\n".join(content_parts) if content_parts else "(No output)"
            return ToolResult(success=True, content=content)

        except Exception as e:
            return ToolResult(
                success=False,
                content="",
                error=f"Execution failed: {str(e)}",
            )

    def _get_workspace(self, session_id: str) -> Path:
        """Get workspace directory for a session."""
        if self.workspace_dir:
            return Path(self.workspace_dir) / "sandbox" / session_id
        return SANDBOX_BASE_DIR / "sessions" / session_id

    def _is_valid_code(self, code: str) -> bool:
        """Check if code is valid."""
        stripped = code.strip()
        if not stripped:
            return False
        lines = stripped.split("\n")
        meaningful_lines = [
            l.strip() for l in lines if l.strip() and not l.strip().startswith("#")
        ]
        return len(meaningful_lines) > 0

    def _simplify_error(self, error: str) -> str:
        """Simplify Python error traceback."""
        error = re.sub(r"\x1b\[[0-9;]*m", "", error)
        lines = error.split("\n")
        relevant_lines = []
        skip_patterns = [
            "/Library/Developer/CommandLineTools",
            "/System/Library/Frameworks",
            "site-packages/jupyter",
            "site-packages/ipykernel",
        ]

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if any(pattern in stripped for pattern in skip_patterns):
                continue
            relevant_lines.append(stripped)

        result = "\n".join(relevant_lines[:20])
        if len(result) > 1000:
            result = result[:1000] + "\n...(truncated)"
        return result or error

    @classmethod
    async def shutdown_all(cls):
        """Shutdown all kernel sessions."""
        for session in list(cls._sessions.values()):
            await session.stop()
        cls._sessions.clear()


class SandboxStatusTool(Tool):
    """Get status of Jupyter sandbox sessions."""

    _sandbox_tool: Optional[JupyterSandboxTool] = None

    @classmethod
    def set_sandbox_tool(cls, tool: JupyterSandboxTool):
        """Set the sandbox tool to query status from."""
        cls._sandbox_tool = tool

    @property
    def name(self) -> str:
        return "sandbox_status"

    @property
    def description(self) -> str:
        return """Get the status of Jupyter sandbox sessions.

Shows current session ID, all active sessions, whether each kernel is alive,
and sandbox venv status.
"""

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {},
        }

    async def execute(self) -> ToolResult:
        """Get sandbox status."""
        if self._sandbox_tool is None:
            return ToolResult(success=False, content="", error="Sandbox not initialized")

        status = self._sandbox_tool.get_status()

        lines = [
            f"Sandbox venv: {status['venv_path']}",
            f"Venv exists: {status['venv_exists']}",
            f"Current session: {status['current_session_id'] or 'none'}",
            f"Total sessions: {status['total_sessions']}",
        ]

        if status['sessions']:
            lines.append("\nSessions:")
            for s in status['sessions']:
                alive = "alive" if s['is_alive'] else "dead"
                lines.append(f"  - {s['session_id']}: {alive}")

        return ToolResult(success=True, content="\n".join(lines))
