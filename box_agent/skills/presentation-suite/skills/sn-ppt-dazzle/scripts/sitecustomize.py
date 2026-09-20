import os
import sys

# Make playwright / PIL importable for the single-file deck renderer.
sys.path.insert(
    0,
    "/mnt/afs/zuohaosheng/multimodal_design/dynamic-ppt-bench/backup/cci/zhs-trail3-0801/runtime/venv/lib/python3.10/site-packages",
)

# Point Playwright at the shared chromium cache so the headless-shell binary is found.
os.environ.setdefault(
    "PLAYWRIGHT_BROWSERS_PATH",
    "/mnt/afs/zuohaosheng/multimodal_design/dynamic-ppt-bench/backup/cci/zhs-trail3-0801/stage/repo/.cache/ms-playwright",
)
os.environ.setdefault(
    "DYNAMIC_PPT_CHROMIUM_EXECUTABLE",
    "/mnt/afs/zuohaosheng/multimodal_design/dynamic-ppt-bench/backup/cci/zhs-trail3-0801/stage/repo/.cache/ms-playwright/chromium_headless_shell-1228/chrome-headless-shell-linux64/chrome-headless-shell",
)
