"""Device control utilities for Android automation."""

import os
import subprocess
import time
from pathlib import PurePosixPath
from typing import List, Optional, Tuple

from phone_agent.config.apps import APP_PACKAGES
from phone_agent.config.timing import TIMING_CONFIG


def _run_adb_input(
    adb_prefix: list[str],
    args: list[str],
    timeout_s: float = 6.0,
    op_name: str = "adb input",
) -> bool:
    """Run adb input with timeout guard to avoid blocking forever."""
    try:
        subprocess.run(
            adb_prefix + ["shell", *args],
            capture_output=True,
            timeout=timeout_s,
        )
        return True
    except subprocess.TimeoutExpired:
        print(f"⚠️ {op_name} timeout({timeout_s}s): {' '.join(args)}")
        return False
    except Exception as e:
        print(f"⚠️ {op_name} failed: {e}")
        return False


def get_current_app(device_id: str | None = None) -> str:
    """
    Get the currently focused app name.

    Args:
        device_id: Optional ADB device ID for multi-device setups.

    Returns:
        The app name if recognized, otherwise "System Home".
    """
    adb_prefix = _get_adb_prefix(device_id)

    def _run_shell(cmd: list[str], timeout_s: int = 4) -> tuple[int, str, str]:
        """Run adb shell command and return (returncode, stdout, stderr)."""
        result = subprocess.run(
            adb_prefix + ["shell", *cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )
        return result.returncode, (result.stdout or ""), (result.stderr or "")

    def _extract_focused_line(text: str) -> str | None:
        for line in text.split("\n"):
            if "mCurrentFocus" in line or "mFocusedApp" in line:
                return line
            if "mResumedActivity" in line or "ResumedActivity" in line:
                return line
        return None

    def _match_app_name(line: str) -> str | None:
        for app_name, package in APP_PACKAGES.items():
            if package in line:
                return app_name
        return None

    # 某些系统版本上 `dumpsys window` 输出不稳定；尝试多种命令并做短重试。
    probes: list[list[str]] = [
        ["dumpsys", "window", "windows"],
        ["dumpsys", "window"],
        ["dumpsys", "activity", "activities"],
        ["dumpsys", "activity", "top"],
    ]

    last_debug: str | None = None
    for attempt in range(3):
        for probe in probes:
            try:
                rc, out, err = _run_shell(probe)
            except subprocess.TimeoutExpired:
                last_debug = f"timeout: adb shell {' '.join(probe)}"
                continue

            if out:
                focused = _extract_focused_line(out)
                if focused:
                    matched = _match_app_name(focused)
                    if matched:
                        return matched
                # 有输出但没匹配到已知包名：不算致命，继续探测其它命令
                last_debug = f"no-match: adb shell {' '.join(probe)} -> {focused or 'no focus line'}"
                continue

            # stdout 为空：记录 stderr/rc 以便排查（常见：device offline/unauthorized）
            debug_bits = [f"rc={rc}", f"cmd=adb shell {' '.join(probe)}"]
            if err.strip():
                debug_bits.append(f"stderr={err.strip()}")
            last_debug = "; ".join(debug_bits)

        time.sleep(0.2)

    # 不要抛异常打断任务：返回默认值，同时打印可用的诊断信息。
    if last_debug:
        print(f"⚠️ get_current_app failed, fallback to System Home ({last_debug})")

    # Parse window focus info
    return "System Home"


def tap(
    x: int, y: int, device_id: str | None = None, delay: float | None = None
) -> None:
    """
    Tap at the specified coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        device_id: Optional ADB device ID.
        delay: Delay in seconds after tap. If None, uses configured default.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_tap_delay

    adb_prefix = _get_adb_prefix(device_id)

    _run_adb_input(
        adb_prefix,
        ["input", "tap", str(x), str(y)],
        timeout_s=6.0,
        op_name="tap",
    )
    time.sleep(delay)


def double_tap(
    x: int, y: int, device_id: str | None = None, delay: float | None = None
) -> None:
    """
    Double tap at the specified coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        device_id: Optional ADB device ID.
        delay: Delay in seconds after double tap. If None, uses configured default.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_double_tap_delay

    adb_prefix = _get_adb_prefix(device_id)

    _run_adb_input(
        adb_prefix,
        ["input", "tap", str(x), str(y)],
        timeout_s=6.0,
        op_name="double_tap#1",
    )
    time.sleep(TIMING_CONFIG.device.double_tap_interval)
    _run_adb_input(
        adb_prefix,
        ["input", "tap", str(x), str(y)],
        timeout_s=6.0,
        op_name="double_tap#2",
    )
    time.sleep(delay)


def long_press(
    x: int,
    y: int,
    duration_ms: int = 3000,
    device_id: str | None = None,
    delay: float | None = None,
) -> None:
    """
    Long press at the specified coordinates.

    Args:
        x: X coordinate.
        y: Y coordinate.
        duration_ms: Duration of press in milliseconds.
        device_id: Optional ADB device ID.
        delay: Delay in seconds after long press. If None, uses configured default.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_long_press_delay

    adb_prefix = _get_adb_prefix(device_id)

    _run_adb_input(
        adb_prefix,
        ["input", "swipe", str(x), str(y), str(x), str(y), str(duration_ms)],
        timeout_s=max(6.0, duration_ms / 1000.0 + 3.0),
        op_name="long_press",
    )
    time.sleep(delay)


def swipe(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    duration_ms: int | None = None,
    device_id: str | None = None,
    delay: float | None = None,
) -> None:
    """
    Swipe from start to end coordinates.

    Args:
        start_x: Starting X coordinate.
        start_y: Starting Y coordinate.
        end_x: Ending X coordinate.
        end_y: Ending Y coordinate.
        duration_ms: Duration of swipe in milliseconds (auto-calculated if None).
        device_id: Optional ADB device ID.
        delay: Delay in seconds after swipe. If None, uses configured default.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_swipe_delay

    adb_prefix = _get_adb_prefix(device_id)

    if duration_ms is None:
        # Calculate duration based on distance
        dist_sq = (start_x - end_x) ** 2 + (start_y - end_y) ** 2
        duration_ms = int(dist_sq / 1000)
        duration_ms = max(1000, min(duration_ms, 2000))  # Clamp between 1000-2000ms

    _run_adb_input(
        adb_prefix,
        [
            "input",
            "swipe",
            str(start_x),
            str(start_y),
            str(end_x),
            str(end_y),
            str(duration_ms),
        ],
        timeout_s=max(6.0, duration_ms / 1000.0 + 3.0),
        op_name="swipe",
    )
    time.sleep(delay)


def back(device_id: str | None = None, delay: float | None = None) -> None:
    """
    Press the back button.

    Args:
        device_id: Optional ADB device ID.
        delay: Delay in seconds after pressing back. If None, uses configured default.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_back_delay

    adb_prefix = _get_adb_prefix(device_id)

    _run_adb_input(
        adb_prefix,
        ["input", "keyevent", "4"],
        timeout_s=6.0,
        op_name="back",
    )
    time.sleep(delay)


def home(device_id: str | None = None, delay: float | None = None) -> None:
    """
    Press the home button.

    Args:
        device_id: Optional ADB device ID.
        delay: Delay in seconds after pressing home. If None, uses configured default.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_home_delay

    adb_prefix = _get_adb_prefix(device_id)

    _run_adb_input(
        adb_prefix,
        ["input", "keyevent", "KEYCODE_HOME"],
        timeout_s=6.0,
        op_name="home",
    )
    time.sleep(delay)


def launch_app(
    app_name: str, device_id: str | None = None, delay: float | None = None
) -> bool:
    """
    Launch an app by name.

    Args:
        app_name: The app name (must be in APP_PACKAGES).
        device_id: Optional ADB device ID.
        delay: Delay in seconds after launching. If None, uses configured default.

    Returns:
        True if app was launched, False if app not found.
    """
    if delay is None:
        delay = TIMING_CONFIG.device.default_launch_delay

    if app_name not in APP_PACKAGES:
        return False

    adb_prefix = _get_adb_prefix(device_id)
    package = APP_PACKAGES[app_name]

    subprocess.run(
        adb_prefix
        + [
            "shell",
            "monkey",
            "-p",
            package,
            "-c",
            "android.intent.category.LAUNCHER",
            "1",
        ],
        capture_output=True,
    )
    time.sleep(delay)
    return True


def _get_adb_prefix(device_id: str | None) -> list:
    """Get ADB command prefix with optional device specifier."""
    if device_id:
        return ["adb", "-s", device_id]
    return ["adb"]


def get_ui_hierarchy_xml(
    device_id: str | None = None,
    timeout_s: float = 15.0,
    remote_path: str = "/sdcard/__phone_agent_window_dump.xml",
) -> str | None:
    """
    Dump current UI hierarchy XML via `uiautomator dump` and return the XML as text.

    This is a lightweight way to check whether a certain text is currently visible
    on screen (e.g., for `expect_text_contains` guards), without adding OCR deps.

    Returns:
        XML string if successful, otherwise None.
    """
    adb_prefix = _get_adb_prefix(device_id)
    # Normalize remote path for adb shell
    remote_path = str(PurePosixPath(remote_path))

    def _run_shell(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            adb_prefix + ["shell", *cmd],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
        )

    # 优先用系统常见默认路径，部分 ROM 对默认路径有优化
    default_path = "/sdcard/window_dump.xml"

    def _run_dump(path: str, compressed: bool = False):
        cmd = ["uiautomator", "dump", "--compressed", path] if compressed else ["uiautomator", "dump", path]
        return _run_shell(cmd)

    def _do_dump(path: str, compressed: bool = False, retry_once: bool = True):
        dump = _run_dump(path, compressed)
        if dump.returncode != 0 and retry_once:
            time.sleep(3)
            dump = _run_dump(path, compressed)
        if dump.returncode != 0:
            return None
        cat = _run_shell(["cat", path])
        if cat.returncode != 0:
            return None
        xml = (cat.stdout or "").strip()
        if not xml or "<hierarchy" not in xml:
            return None
        return xml

    try:
        # 1) 先试默认路径（无压缩），失败则等 3 秒重试一次
        xml = _do_dump(default_path, compressed=False)
        if xml:
            return xml
        # 2) 再试自定义路径
        xml = _do_dump(remote_path, compressed=False)
        if xml:
            return xml
        # 3) 带 --compressed 再试默认路径
        xml = _do_dump(default_path, compressed=True)
        return xml
    except Exception:
        return None
