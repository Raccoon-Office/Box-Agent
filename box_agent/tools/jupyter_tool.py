"""Python code execution sandbox with persistent kernel via Jupyter.

This module provides a sandboxed Python execution environment using a persistent
Jupyter kernel with its own isolated virtual environment. Variables and imports
persist between executions. The sandbox venv is separate from box-agent's own
environment, so user-installed packages don't pollute the tool's dependencies.
"""

import asyncio
import json
import os
import platform
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from ._win_job import assign_pid_to_job
from .base import Tool, ToolResult

# True when running inside a PyInstaller frozen binary
IS_FROZEN = getattr(sys, "frozen", False)

# Default packages installed in the sandbox venv on first startup
SANDBOX_DEFAULT_PACKAGES = [
    "pandas",
    "numpy",
    "matplotlib",
    "seaborn",
    "requests",
    "openpyxl",         # .xlsx support for pandas
    "xlrd",             # .xls support for pandas
    "scikit-learn",     # ML/data analysis
    "python-docx",      # Word (.docx) read/write
    "pypdf",            # PDF read/merge/split
    "pdfplumber",       # PDF table/text extraction
    "reportlab",        # PDF creation
    "python-pptx",      # PowerPoint (.pptx) read/write
    "beautifulsoup4",   # HTML parsing (scraping, doc cleanup)
    "lxml",             # fast XML/HTML parser, backend for bs4/pandas
    "pillow",           # image I/O (matplotlib, pandas plotting deps)
    "pyyaml",           # YAML config/data parsing
    "python-dateutil",  # robust date parsing
    "chardet",          # encoding detection for text/CSV
]

SANDBOX_BASE_DIR = Path.home() / ".box-agent" / "sandbox"

# Keep generated tool-call JSON well below common provider completion caps.
# The kernel is persistent, so long workflows should be split across calls.
MAX_EXECUTE_CODE_CHARS = 8_000

# User-level directory for packages installed at runtime in frozen mode.
# Survives across sessions; kept separate from the frozen binary itself.
RUNTIME_PACKAGES_DIR = Path.home() / ".box-agent" / "runtime-packages"

# Whitelist of pip package names allowed for on-demand installation in
# frozen/runtime mode.  Packages NOT in this set are rejected with a clear
# error.  The set uses **lowercased** names for case-insensitive matching.
ALLOWED_RUNTIME_PACKAGES: set[str] = {
    # Core data science
    "pandas", "numpy", "scipy", "statsmodels",
    # Visualization
    "matplotlib", "seaborn", "plotly", "bokeh", "altair",
    # ML
    "scikit-learn", "scikit-image", "joblib",
    # Excel / tabular I/O
    "openpyxl", "xlrd", "xlsxwriter", "pyarrow", "fastparquet", "tabulate",
    # Document processing
    "python-docx", "pypdf", "pdfplumber", "reportlab", "python-pptx",
    # Web / scraping
    "requests", "beautifulsoup4", "lxml",
    # Images
    "pillow", "opencv-python",
    # Utilities
    "python-dateutil", "python-dotenv", "pyyaml", "chardet", "tqdm", "rich",
    # Database
    "sqlalchemy",
    # Network / graph
    "networkx", "sympy",
}


class SandboxEnvironment:
    """Manages an isolated Python virtual environment for the sandbox.

    Creates a venv at ~/.box-agent/sandbox/venv/ with pip and default
    data-science packages. The kernel runs inside this venv so user
    packages are isolated from box-agent itself.
    """

    def __init__(
        self,
        base_dir: Path | None = None,
        runtime_env: Mapping[str, str] | None = None,
    ):
        self.base_dir = base_dir or SANDBOX_BASE_DIR
        self.runtime_env = dict(runtime_env or {})
        self.venv_dir = self.base_dir / "venv"
        # Windows venv lays out python under Scripts/, Unix under bin/.
        if sys.platform == "win32":
            self.python_path = self.venv_dir / "Scripts" / "python.exe"
        else:
            self.python_path = self.venv_dir / "bin" / "python"
        self._ready = False
        self._bundled_override = False
        self.python_override_path: Path | None = None

        # Host/runtime override: point at an already-prepared interpreter
        # supplied by the embedding app.  BOX_AGENT_SANDBOX_PYTHON is the
        # cross-platform sandbox contract; BOX_AGENT_BUNDLED_PYTHON is retained
        # for older Windows officev3 builds.  Do NOT use BOX_AGENT_PYTHON here:
        # existing hosts may set it to a system Python for shell skills, and
        # that must not silently replace the isolated execute_code runtime.
        # Do NOT set _ready=True here — let ensure_ready() run _verify_packages
        # so we can fall back to RUNTIME_PACKAGES_DIR when the external
        # interpreter is missing one of our defaults.
        override_path = self._resolve_python_override(self.runtime_env)
        if override_path is not None:
            self.python_path = override_path
            self.python_override_path = override_path
            self._bundled_override = True

    @property
    def is_created(self) -> bool:
        """Check if the venv already exists."""
        return self.python_path.exists()

    async def ensure_ready(self, on_progress: Any = None) -> None:
        """Ensure the sandbox venv is created and has default packages.

        In frozen (PyInstaller) mode, venv creation is skipped — packages are
        bundled inside the binary and an in-process kernel is used instead.

        Args:
            on_progress: Optional callback(message: str) for progress updates.
        """
        if self._ready:
            return

        if self._bundled_override:
            # win32: skip the per-package import probes entirely. Spawning many
            # short-lived "python.exe -c import X" processes deadlocks on
            # Windows under injected security DLLs (e.g. flhhlp.dll) — a single
            # hung probe blocks ensure_ready forever and execute_code never
            # returns. The host runtime (provisioned/bundled) already carries
            # the required packages, and the kernel itself is one long-lived
            # subprocess that starts fine. macOS keeps the full verify path.
            if sys.platform == "win32":
                self._ready = True
                return
            # Host python already exists — skip venv/_install_defaults, but
            # verify required packages and fall back to RUNTIME_PACKAGES_DIR for
            # any that the interpreter is missing.
            if on_progress:
                on_progress("Host python: verifying required packages...")
            await self._verify_packages(on_progress)
            self._ready = True
            return

        if IS_FROZEN:
            if on_progress:
                on_progress("Runtime mode: using bundled packages (no venv).")
            self._ready = True
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

        # Verify all required packages are importable
        await self._verify_packages(on_progress)

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

    # import-name -> pip-name for every package we expect in the sandbox.
    # Keys are what `python -c "import X"` would use.
    _REQUIRED_MODULES = {
        "ipykernel": "ipykernel",
        "pandas": "pandas",
        "numpy": "numpy",
        "matplotlib": "matplotlib",
        "seaborn": "seaborn",
        "requests": "requests",
        "openpyxl": "openpyxl",
        "xlrd": "xlrd",
        "sklearn": "scikit-learn",
        "docx": "python-docx",
        "pypdf": "pypdf",
        "pdfplumber": "pdfplumber",
        "reportlab": "reportlab",
        "pptx": "python-pptx",
        "bs4": "beautifulsoup4",
        "lxml": "lxml",
        "PIL": "pillow",
        "yaml": "pyyaml",
        "dateutil": "python-dateutil",
        "chardet": "chardet",
        "pip": "pip",
    }

    def _subprocess_env(self) -> dict[str, str] | None:
        """Build env for subprocess pip/import checks.

        When a host python override is in use we prepend RUNTIME_PACKAGES_DIR
        to PYTHONPATH so fallback-installed packages are importable.
        """
        if not self._bundled_override:
            return None
        env = os.environ.copy()
        env.update({key: value for key, value in self.runtime_env.items() if isinstance(value, str)})
        extra = str(RUNTIME_PACKAGES_DIR)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = extra + (os.pathsep + existing if existing else "")
        return env

    async def _verify_packages(self, on_progress: Any = None) -> None:
        """Verify required packages are importable and auto-install missing ones."""
        env = self._subprocess_env()
        missing = await self._missing_required_packages(env)
        if "pip" in missing:
            await self._ensure_pip_available(on_progress, env)
            missing = [pkg for pkg in missing if pkg != "pip"]

        if not missing:
            return

        if on_progress:
            on_progress(f"Re-installing missing packages: {', '.join(missing)}...")

        # In bundled-override mode the bundled python may live in a read-only
        # location, so install to RUNTIME_PACKAGES_DIR via --target instead.
        pip_args = ["-m", "pip", "install", "--quiet", "--disable-pip-version-check"]
        if self._bundled_override:
            RUNTIME_PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
            pip_args += ["--target", str(RUNTIME_PACKAGES_DIR), "--no-warn-script-location"]
        pip_args += missing

        proc = await asyncio.create_subprocess_exec(
            str(self.python_path), *pip_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            if self._bundled_override:
                raise RuntimeError(
                    "Failed to install missing host python sandbox packages "
                    f"({', '.join(missing)}): {stderr.decode(errors='replace')[:500]}"
                )
            if on_progress:
                on_progress(f"Warning: failed to install some packages: {', '.join(missing)}")
            return

        if self._bundled_override:
            still_missing = await self._missing_required_packages(env)
            if still_missing:
                raise RuntimeError(
                    "Host python sandbox packages are still missing after install: "
                    f"{', '.join(still_missing)}"
                )
        if on_progress:
            on_progress(f"Re-installed: {', '.join(missing)}")

    async def _missing_required_packages(self, env: dict[str, str] | None) -> list[str]:
        missing = []
        for module_name, pip_name in self._REQUIRED_MODULES.items():
            proc = await asyncio.create_subprocess_exec(
                str(self.python_path), "-c", f"import {module_name}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            await proc.communicate()
            if proc.returncode != 0:
                missing.append(pip_name)
        return missing

    async def _ensure_pip_available(
        self,
        on_progress: Any,
        env: dict[str, str] | None,
    ) -> None:
        if on_progress:
            on_progress("Host python: bootstrapping pip...")
        proc = await asyncio.create_subprocess_exec(
            str(self.python_path), "-m", "ensurepip", "--upgrade",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                "Failed to bootstrap pip for host python sandbox: "
                f"{stderr.decode(errors='replace')[:500]}"
            )

        proc = await asyncio.create_subprocess_exec(
            str(self.python_path), "-c", "import pip",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                "pip is still unavailable after ensurepip: "
                f"{stderr.decode(errors='replace')[:500]}"
            )

    def get_kernel_spec(self) -> dict:
        """Get kernel spec that uses the sandbox venv Python."""
        spec: dict = {
            "argv": [
                str(self.python_path),
                "-m", "ipykernel_launcher",
                "-f", "{connection_file}",
            ],
            "display_name": "Box-Agent Sandbox",
            "language": "python",
        }
        # In bundled-override mode the kernel subprocess won't inherit our
        # patched sys.path, so expose RUNTIME_PACKAGES_DIR via PYTHONPATH on
        # the kernel spec instead.
        if self._bundled_override and RUNTIME_PACKAGES_DIR.exists():
            spec["env"] = {"PYTHONPATH": str(RUNTIME_PACKAGES_DIR)}
        return spec

    def get_kernel_spec_dir(self) -> Path:
        """Get path to the kernel spec directory, creating it if needed."""
        spec_dir = self.base_dir / "kernelspec" / "box-agent-sandbox"
        spec_dir.mkdir(parents=True, exist_ok=True)
        spec_file = spec_dir / "kernel.json"
        # Always write fresh spec (python path may change)
        spec_file.write_text(json.dumps(self.get_kernel_spec(), indent=2))
        return spec_dir

    async def install_packages(self, packages: list[str]) -> tuple[bool, str]:
        """Install additional packages into the sandbox environment.

        In frozen mode without a host python, installs to
        ~/.box-agent/runtime-packages/ via bundled pip.  When a host python is
        available, installs through that interpreter and exposes the target via
        PYTHONPATH.  Frozen-mode installs are whitelisted; others are rejected
        with PACKAGE_NOT_ALLOWED.

        In normal mode, installs into the sandbox venv via subprocess pip.

        Args:
            packages: List of pip package names to install.

        Returns:
            (success, message) tuple.
        """
        if self._bundled_override:
            return await self._runtime_python_install(packages, enforce_allowlist=IS_FROZEN)

        if IS_FROZEN:
            return await self._frozen_install(packages)

        proc = await asyncio.create_subprocess_exec(
            str(self.python_path), "-m", "pip", "install",
            *packages,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"pip install timed out (120s) for: {', '.join(packages)}"
        output = stdout.decode() + stderr.decode()
        if proc.returncode == 0:
            return True, output
        return False, output

    async def _runtime_python_install(
        self,
        packages: list[str],
        *,
        enforce_allowlist: bool,
    ) -> tuple[bool, str]:
        """Install packages through the configured host Python runtime."""
        if enforce_allowlist:
            blocked = self._blocked_runtime_packages(packages)
            if blocked:
                return False, self._package_not_allowed_message(blocked)

        RUNTIME_PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            str(self.python_path),
            "-m",
            "pip",
            "install",
            "--target",
            str(RUNTIME_PACKAGES_DIR),
            "--quiet",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            *packages,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._subprocess_env(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return False, f"pip install timed out (120s) for: {', '.join(packages)}"
        output = stdout.decode() + stderr.decode()
        if proc.returncode == 0:
            return True, output or f"Installed {', '.join(packages)} to {RUNTIME_PACKAGES_DIR}"
        return False, output

    async def _frozen_install(self, packages: list[str]) -> tuple[bool, str]:
        """Install packages in frozen mode to the runtime-packages directory.

        Checks packages against ALLOWED_RUNTIME_PACKAGES, then uses pip as a
        library (no subprocess Python needed) to install into
        ~/.box-agent/runtime-packages/.
        """
        blocked = self._blocked_runtime_packages(packages)
        if blocked:
            return False, self._package_not_allowed_message(blocked)

        # Run synchronous pip in a thread to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        try:
            return await asyncio.wait_for(
                loop.run_in_executor(None, self._frozen_pip_install_sync, packages),
                timeout=120,
            )
        except asyncio.TimeoutError:
            return False, f"pip install timed out (120s) for: {', '.join(packages)}"

    @staticmethod
    def _frozen_pip_install_sync(packages: list[str]) -> tuple[bool, str]:
        """Synchronous pip install to RUNTIME_PACKAGES_DIR.

        Uses pip as a library so we don't need a subprocess Python.
        The target directory is added to sys.path so the in-process kernel
        can import newly installed packages immediately.
        """
        target = RUNTIME_PACKAGES_DIR
        target.mkdir(parents=True, exist_ok=True)

        # Ensure the directory is on sys.path for immediate importability
        target_str = str(target)
        if target_str not in sys.path:
            sys.path.insert(0, target_str)

        try:
            from pip._internal.cli.main import main as pip_main
        except ImportError:
            return False, (
                "PACKAGE_NOT_AVAILABLE: pip is not bundled in this runtime. "
                f"Cannot install: {', '.join(packages)}"
            )

        args = [
            "install",
            "--target", target_str,
            "--quiet",
            "--disable-pip-version-check",
            "--no-warn-script-location",
            *packages,
        ]

        try:
            exit_code = pip_main(args)
        except SystemExit as e:
            exit_code = e.code if isinstance(e.code, int) else 1
        except Exception as e:
            return False, f"pip install error: {e}"

        if exit_code == 0:
            return True, f"Installed {', '.join(packages)} to {target}"
        return False, f"pip install failed (exit code {exit_code}) for: {', '.join(packages)}"

    @staticmethod
    def _blocked_runtime_packages(packages: list[str]) -> list[str]:
        allowed_lower = {p.lower() for p in ALLOWED_RUNTIME_PACKAGES}
        return [p for p in packages if p.lower() not in allowed_lower]

    @staticmethod
    def _package_not_allowed_message(blocked: list[str]) -> str:
        return (
            f"PACKAGE_NOT_ALLOWED: {', '.join(blocked)} not in allowed list. "
            f"Allowed packages: {', '.join(sorted(ALLOWED_RUNTIME_PACKAGES))}"
        )

    @staticmethod
    def _resolve_python_override(runtime_env: Mapping[str, str] | None = None) -> Path | None:
        sources: list[Mapping[str, str]] = []
        if runtime_env:
            sources.append(runtime_env)
        sources.append(os.environ)
        for source in sources:
            for env_name in ("BOX_AGENT_SANDBOX_PYTHON", "BOX_AGENT_BUNDLED_PYTHON"):
                override = source.get(env_name)
                if not override:
                    continue
                override_path = Path(override)
                if override_path.is_file():
                    return override_path
        return None


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

        # Patch provisioner factory BEFORE start_kernel().
        # In PyInstaller packaged runtime, entry-point metadata is missing or
        # broken: the entry point for 'local-provisioner' either doesn't exist
        # or .load() returns the *module* instead of the *class*, causing
        # "local-provisioner is not found" or "TypeError: 'module' object is
        # not callable".  We monkey-patch the factory to bypass entry points.
        self._patch_provisioner_factory()

        # Create KernelManager with our custom spec
        km = jupyter_client.KernelManager()
        km._kernel_spec = kernel_spec  # Override the kernel spec
        km.start_kernel()
        self._km = km
        assign_pid_to_job(getattr(km, "kernel_pid", 0) or 0)
        self._kc = km.client()
        self._kc.start_channels()
        self._kc.wait_for_ready(timeout=30)

        # Run setup code in the kernel
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

    _provisioner_patched = False

    @classmethod
    def _patch_provisioner_factory(cls):
        """Monkey-patch KernelProvisionerFactory for PyInstaller compatibility.

        In a PyInstaller frozen environment, importlib.metadata entry points
        are missing or broken.  The factory's create_provisioner_instance()
        calls ``self.provisioners['local-provisioner'].load()`` which either
        raises (entry point not found) or returns the *module* object instead
        of the LocalProvisioner *class* (→ TypeError: 'module' object is not
        callable).

        This patch replaces create_provisioner_instance() with a version that
        directly instantiates LocalProvisioner, skipping entry-point lookup.
        The patch is applied once and is a no-op in non-frozen environments
        where the original factory works correctly.
        """
        if cls._provisioner_patched:
            return

        from jupyter_client.provisioning.factory import KernelProvisionerFactory
        from jupyter_client.provisioning.local_provisioner import LocalProvisioner

        factory = KernelProvisionerFactory.instance()

        # Quick check: does the existing factory work?
        try:
            ep = factory.provisioners.get("local-provisioner")
            if ep is not None:
                loaded = ep.load()
                # Verify it loaded the CLASS, not the module.
                # A module object is not a type — isinstance(mod, type) is False.
                if isinstance(loaded, type):
                    cls._provisioner_patched = True
                    return
        except Exception:
            pass

        # Patch: replace create_provisioner_instance entirely
        _orig_create = factory.create_provisioner_instance

        def _patched_create(kernel_id, kernel_spec, parent):
            provisioner_cfg = factory._get_provisioner_config(kernel_spec)
            provisioner_config = provisioner_cfg.get("config", {})
            return LocalProvisioner(
                kernel_id=kernel_id,
                kernel_spec=kernel_spec,
                parent=parent,
                **provisioner_config,
            )

        factory.create_provisioner_instance = _patched_create
        cls._provisioner_patched = True

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


class InProcessKernelSession:
    """A Jupyter in-process kernel session for frozen (PyInstaller) environments.

    Uses ipykernel.inprocess so the kernel runs inside the box-agent process
    itself, avoiding the need for a subprocess, venv, or kernel spec.  The
    public interface mirrors JupyterKernelSession: start(), execute(), stop(),
    is_alive().
    """

    _diagnostics_logged = False

    def __init__(self, session_id: str, workspace: Path):
        self.session_id = session_id
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._km = None
        self._kc = None

    @classmethod
    def _log_diagnostics(cls):
        """Log one-time diagnostic banner to stderr on first init."""
        if cls._diagnostics_logged:
            return
        cls._diagnostics_logged = True

        try:
            from box_agent import __version__
        except Exception:
            __version__ = "unknown"

        plat = f"{sys.platform}-{platform.machine()}"
        lines = [
            f"[SANDBOX] Runtime: frozen=True, version={__version__}, platform={plat}",
            "[SANDBOX] Mode: in-process kernel (no subprocess, no venv)",
        ]

        # Report bundled package versions
        pkg_versions = []
        for pkg in ("pandas", "numpy", "matplotlib", "seaborn", "openpyxl", "sklearn"):
            try:
                mod = __import__(pkg)
                ver = getattr(mod, "__version__", "?")
                pkg_versions.append(f"{pkg}={ver}")
            except ImportError:
                pkg_versions.append(f"{pkg}=N/A")
        lines.append(f"[SANDBOX] Packages: {', '.join(pkg_versions)}")

        for line in lines:
            print(line, file=sys.stderr)

    async def start(self):
        """Start an in-process kernel."""
        self._log_diagnostics()

        try:
            from ipykernel.inprocess.manager import InProcessKernelManager
        except ImportError as exc:
            raise RuntimeError(
                "KERNEL_START_FAILED: ipykernel.inprocess not available"
            ) from exc

        km = InProcessKernelManager()
        km.start_kernel()
        self._km = km

        kc = km.client()
        kc.start_channels()
        # InProcess wait_for_ready() does not accept timeout
        kc.wait_for_ready()
        self._kc = kc

        # Run setup code — add runtime-packages to path so previously
        # installed packages are importable, then set up workspace + matplotlib
        setup_code = f"""
import sys, os

# Prepend user-installed runtime packages directory
_rt_pkg = r'{RUNTIME_PACKAGES_DIR}'
if _rt_pkg not in sys.path:
    sys.path.insert(0, _rt_pkg)

os.chdir(r'{self.workspace}')

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
                if (
                    msg.get("msg_type") == "status"
                    and msg.get("content", {}).get("execution_state") == "idle"
                ):
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
        """Execute code and return (stdout, images, error).

        Same interface as JupyterKernelSession.execute().
        """
        if self._kc is None:
            return "", [], "Kernel not initialized"

        # Drain pending IOPub messages
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
            msg_id = self._kc.execute(code, silent=False)

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
                        text = re.sub(r"\x1b\[[0-9;]*m", "", text)
                        if name == "stdout":
                            stdout_parts.append(text)
                        elif name == "stderr":
                            stderr_parts.append(text)

                    elif msg_type == "error":
                        error_parts.append(
                            f"{content.get('ename')}: {content.get('evalue')}"
                        )

                    elif msg_type in ("display_data", "execute_result"):
                        data = content.get("data", {})
                        if "image/png" in data:
                            images.append("[PNG Image]")

                except Exception:
                    break

            if error_parts:
                return "", [], "\n".join(error_parts)

        except Exception as e:
            return "", [], f"Execution failed: {str(e)}"

        all_output = "".join(stdout_parts)
        if stderr_parts:
            stderr_text = "".join(stderr_parts).strip()
            if stderr_text:
                if all_output:
                    all_output += "\n" + stderr_text
                else:
                    all_output = stderr_text

        return all_output, images, None

    async def stop(self):
        """Stop the in-process kernel."""
        if self._kc:
            try:
                self._kc.stop_channels()
            except Exception:
                pass
            self._kc = None
        if self._km:
            try:
                self._km.shutdown_kernel()
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

    _sessions: dict[str, "JupyterKernelSession | InProcessKernelSession"] = {}
    _sandbox_env: SandboxEnvironment | None = None
    _sandbox_env_key: str | None = None

    def __init__(
        self,
        workspace_dir: str | None = None,
        runtime_env: Mapping[str, str] | None = None,
        use_output_dir: bool = True,
    ):
        """Initialize sandbox tool.

        Args:
            workspace_dir: Base workspace directory for sandbox sessions.
            runtime_env: Host runtime environment exported by env_context.
            use_output_dir: Chdir kernels into {workspace}/output when True.
        """
        self.workspace_dir = workspace_dir
        self.runtime_env = dict(runtime_env or {})
        self.use_output_dir = use_output_dir
        self._session_id: Optional[str] = None

    def _get_sandbox_env(self) -> SandboxEnvironment:
        """Get or create the shared sandbox environment."""
        key = self._sandbox_env_cache_key()
        if self.__class__._sandbox_env is None or self.__class__._sandbox_env_key != key:
            self.__class__._sandbox_env = SandboxEnvironment(runtime_env=self.runtime_env)
            self.__class__._sandbox_env_key = key
        return self.__class__._sandbox_env

    def _sandbox_env_cache_key(self) -> str:
        for env_name in ("BOX_AGENT_SANDBOX_PYTHON", "BOX_AGENT_BUNDLED_PYTHON"):
            value = self.runtime_env.get(env_name) or os.environ.get(env_name)
            if value:
                return f"{env_name}={value}"
        return ""

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

        status: dict[str, Any] = {
            "current_session_id": self._session_id,
            "sessions": sessions_info,
            "total_sessions": len(self._sessions),
            "frozen": IS_FROZEN,
        }
        if not IS_FROZEN:
            status["venv_path"] = str(env.venv_dir)
            status["venv_exists"] = env.is_created
        return status

    def _create_session(
        self, session_id: str, workspace: Path, env: SandboxEnvironment
    ) -> "JupyterKernelSession | InProcessKernelSession":
        """Create the appropriate kernel session for the current environment."""
        # When the embedding app supplies a standalone Python, route the kernel
        # through that subprocess on every platform. This keeps frozen ACP from
        # relying on PyInstaller-bundled ipykernel/data packages.
        if env._bundled_override:
            return JupyterKernelSession(session_id, workspace, env)
        if IS_FROZEN:
            return InProcessKernelSession(session_id, workspace)
        return JupyterKernelSession(session_id, workspace, env)

    @property
    def name(self) -> str:
        return "execute_code"

    @property
    def description(self) -> str:
        return """Execute Python code in a persistent Jupyter kernel sandbox.

This tool runs Python code in a **real Jupyter kernel** with its own isolated environment:
- Variables, functions, classes, imports all persist between calls
- Pre-installed packages: pandas, numpy, matplotlib, seaborn, scikit-learn, requests,
  openpyxl, xlrd, python-docx, pypdf, pdfplumber, reportlab, python-pptx,
  beautifulsoup4, lxml, pillow, pyyaml, python-dateutil, chardet
- Do NOT %pip install any pre-installed package — it just wastes time and can hit the timeout
- Only %pip install when you hit an actual ModuleNotFoundError for a non-listed package
- Ideal for data analysis: load once, analyze many times

Example workflow:
  1. execute_code(code="import pandas as pd\\ndf = pd.read_csv('data.csv')")
  2. execute_code(code="print(df.describe())")
  3. execute_code(code="from bs4 import BeautifulSoup  # already installed")
  4. execute_code(code="%pip install some_uncommon_pkg")  # only if truly missing

**Full Python state persists in the same session!**

Best practices:
- Break complex analysis into steps
- Keep each code argument under 8000 characters; split large scripts/templates/data
  across multiple calls or files instead of inlining them in one tool call
- Use print() to see intermediate results
- Never use the bash tool's `pip install` for sandbox packages — bash runs against the
  host Python and the sandbox kernel will not see those packages

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
                    "maxLength": MAX_EXECUTE_CODE_CHARS,
                    "description": (
                        "Python code to execute. Keep this under 8000 characters; "
                        "split large scripts, templates, data, or generated file "
                        "content across multiple calls/files instead of inlining "
                        "one huge argument. Variables and functions from previous "
                        "calls in the same session are available. Use %pip install "
                        "<pkg> to install packages."
                    ),
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
        if len(code) > MAX_EXECUTE_CODE_CHARS:
            return ToolResult(
                success=False,
                content="",
                error=(
                    "EXECUTE_CODE_TOO_LARGE: code is "
                    f"{len(code)} characters; limit is {MAX_EXECUTE_CODE_CHARS}. "
                    "Split the work into multiple execute_code calls because "
                    "kernel state persists. Do not inline large file content, "
                    "templates, base64, or data; write/read files in smaller chunks."
                ),
            )
        if self._looks_like_python_pptx_new_deck(code):
            return ToolResult(
                success=False,
                content="",
                error=(
                    "PYTHON_PPTX_NEW_DECK_BLOCKED: execute_code must not create "
                    "new PPT/PPTX decks with python-pptx. Use the pptx skill's "
                    "HTML-first workflow (deck.html -> screenshots -> images_to_pptx) "
                    "or confirmed PptxGenJS native workflow instead. Python is "
                    "allowed for data preparation, extraction, QA, and narrow edits "
                    "to existing decks."
                ),
            )

        # Ensure sandbox environment is ready
        env = self._get_sandbox_env()
        try:
            await env.ensure_ready(on_progress=lambda msg: None)
        except RuntimeError as e:
            return ToolResult(
                success=False,
                content="",
                error=f"SANDBOX_INIT_FAILED: {e}",
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
            session = self._create_session(session_id, workspace, env)
            await session.start()
            self._sessions[session_id] = session
            return ToolResult(
                success=False,
                content="",
                error=f"KERNEL_DIED: Sandbox kernel died (old session={old_session_id}). Auto-restarted with new session={session_id}. Please retry your code.",
            )

        # Get or create kernel session
        if session_id not in self._sessions:
            workspace = self._get_workspace(session_id)
            session = self._create_session(session_id, workspace, env)
            try:
                await session.start()
            except RuntimeError as e:
                return ToolResult(
                    success=False,
                    content="",
                    error=f"KERNEL_START_FAILED: {e}",
                )
            self._sessions[session_id] = session

        session = self._sessions[session_id]

        # Snapshot files in workspace BEFORE execution to detect new ones
        workspace = session.workspace
        pre_files = set(workspace.iterdir()) if workspace.exists() else set()

        # Execute code — run synchronous kernel execute in a thread to avoid
        # blocking the event loop (pip installs inside %pip or auto-install can
        # take minutes).  Wrap with an overall timeout as a safety net.
        _EXEC_TIMEOUT = max(timeout * 3, 180)  # at least 3 min overall
        loop = asyncio.get_event_loop()
        try:
            stdout, images, error = await asyncio.wait_for(
                loop.run_in_executor(None, session.execute, code, timeout),
                timeout=_EXEC_TIMEOUT,
            )

            # Auto-install missing modules and retry once
            if error and "ModuleNotFoundError" in error:
                pkg = self._extract_missing_module(error)
                if pkg:
                    env = self._get_sandbox_env()
                    pip_name = self._MODULE_TO_PIP.get(pkg, pkg)
                    ok, _ = await env.install_packages([pip_name])
                    if ok:
                        stdout, images, error = await asyncio.wait_for(
                            loop.run_in_executor(None, session.execute, code, timeout),
                            timeout=_EXEC_TIMEOUT,
                        )

            if error:
                return ToolResult(
                    success=False,
                    content="",
                    error=self._simplify_error(error),
                )

            # Detect NEW files created during execution
            post_files = set(workspace.iterdir()) if workspace.exists() else set()
            new_files = post_files - pre_files
            artifact_exts = {
                ".png", ".jpg", ".jpeg", ".gif", ".svg",
                ".pdf", ".csv", ".xlsx", ".xls", ".html",
                ".json", ".txt", ".md", ".zip",
                ".docx", ".pptx",
            }
            for f in sorted(new_files):
                if f.is_file() and f.suffix.lower() in artifact_exts:
                    tag = f"[{f.name}]"
                    if tag not in "\n".join(images):
                        images.append(tag)

            content_parts = []
            if stdout.strip():
                content_parts.append(stdout.strip())
            if images:
                content_parts.append("\n".join(images))

            content = "\n".join(content_parts) if content_parts else "(No output)"
            return ToolResult(success=True, content=content)

        except (asyncio.TimeoutError, TimeoutError):
            return ToolResult(
                success=False,
                content="",
                error=f"Code execution timed out ({_EXEC_TIMEOUT}s). "
                      "If installing packages, try running %pip install separately first.",
            )
        except Exception as e:
            return ToolResult(
                success=False,
                content="",
                error=f"Execution failed: {str(e)}",
            )
    _MODULE_TO_PIP = {
        "sklearn": "scikit-learn",
        "cv2": "opencv-python",
        "PIL": "Pillow",
        "bs4": "beautifulsoup4",
        "yaml": "PyYAML",
        "skimage": "scikit-image",
        "dateutil": "python-dateutil",
        "lxml": "lxml",
        "xlrd": "xlrd",
        "sqlalchemy": "SQLAlchemy",
        "dotenv": "python-dotenv",
    }

    @staticmethod
    def _looks_like_python_pptx_new_deck(code: str) -> bool:
        """Detect python-pptx code that creates a brand-new presentation."""
        if not code:
            return False
        if not re.search(r"^\s*(?:from\s+pptx\s+import|import\s+pptx\b)", code, re.MULTILINE):
            return False
        # Reading/editing existing decks uses Presentation(path). A bare
        # Presentation() call is the python-pptx new-deck constructor and has
        # repeatedly bypassed the HTML-first PPT workflow in ACP sessions.
        return bool(re.search(r"\bPresentation\s*\(\s*\)", code))

    def _get_workspace(self, session_id: str) -> Path:
        """Get the directory the kernel chdirs into for a session.

        In default artifact mode this returns ``{workspace_dir}/output/`` so
        generated files land in the canonical artifact location. In project
        workspace mode, callers disable ``use_output_dir`` and the kernel uses
        the workspace/project root directly.
        """
        from box_agent.core import ensure_output_dir
        if self.workspace_dir:
            root = Path(self.workspace_dir).expanduser().resolve()
            if self.use_output_dir:
                return ensure_output_dir(root)
            root.mkdir(parents=True, exist_ok=True)
            return root
        session_root = SANDBOX_BASE_DIR / "sessions" / session_id
        if self.use_output_dir:
            return ensure_output_dir(session_root)
        session_root.mkdir(parents=True, exist_ok=True)
        return session_root

    @staticmethod
    def _extract_missing_module(error: str) -> str | None:
        """Extract module name from ModuleNotFoundError message."""
        # Patterns: "No module named 'sklearn'" or "No module named 'sklearn.ensemble'"
        m = re.search(r"No module named ['\"]([^'\"]+)['\"]", error)
        if m:
            return m.group(1).split(".")[0]
        return None

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
            f"Mode: {'in-process (frozen)' if status.get('frozen') else 'subprocess + venv'}",
        ]
        if not status.get("frozen"):
            lines.append(f"Sandbox venv: {status.get('venv_path', 'N/A')}")
            lines.append(f"Venv exists: {status.get('venv_exists', False)}")
        lines.extend([
            f"Current session: {status['current_session_id'] or 'none'}",
            f"Total sessions: {status['total_sessions']}",
        ])

        if status['sessions']:
            lines.append("\nSessions:")
            for s in status['sessions']:
                alive = "alive" if s['is_alive'] else "dead"
                lines.append(f"  - {s['session_id']}: {alive}")

        return ToolResult(success=True, content="\n".join(lines))
