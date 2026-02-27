"""Action handler for processing AI model outputs."""

import ast
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any, Callable

from phone_agent.config.timing import TIMING_CONFIG
from phone_agent.device_factory import get_device_factory


@dataclass
class ActionResult:
    """Result of an action execution."""

    success: bool
    should_finish: bool
    message: str | None = None
    requires_confirmation: bool = False


class ActionHandler:
    """
    Handles execution of actions from AI model output.

    Args:
        device_id: Optional ADB device ID for multi-device setups.
        confirmation_callback: Optional callback for sensitive action confirmation.
            Should return True to proceed, False to cancel.
        takeover_callback: Optional callback for takeover requests (login, captcha).
    """

    def __init__(
        self,
        device_id: str | None = None,
        confirmation_callback: Callable[[str], bool] | None = None,
        takeover_callback: Callable[[str], None] | None = None,
    ):
        self.device_id = device_id
        self.confirmation_callback = confirmation_callback or self._default_confirmation
        self.takeover_callback = takeover_callback or self._default_takeover

    def execute(
        self, action: dict[str, Any], screen_width: int, screen_height: int
    ) -> ActionResult:
        """
        Execute an action from the AI model.

        Args:
            action: The action dictionary from the model.
            screen_width: Current screen width in pixels.
            screen_height: Current screen height in pixels.

        Returns:
            ActionResult indicating success and whether to finish.
        """
        action_type = action.get("_metadata")

        if action_type == "finish":
            return ActionResult(
                success=True, should_finish=True, message=action.get("message")
            )

        if action_type != "do":
            return ActionResult(
                success=False,
                should_finish=True,
                message=f"Unknown action type: {action_type}",
            )

        action_name = action.get("action")
        handler_method = self._get_handler(action_name)

        if handler_method is None:
            return ActionResult(
                success=False,
                should_finish=False,
                message=f"Unknown action: {action_name}",
            )

        try:
            return handler_method(action, screen_width, screen_height)
        except Exception as e:
            return ActionResult(
                success=False, should_finish=False, message=f"Action failed: {e}"
            )

    def _get_handler(self, action_name: str) -> Callable | None:
        """Get the handler method for an action."""
        handlers = {
            "Launch": self._handle_launch,
            "Tap": self._handle_tap,
            "TapByText": self._handle_tap_by_text,
            "TapRoomArrowByText": self._handle_tap_room_arrow_by_text,
            "Type": self._handle_type,
            "Type_Name": self._handle_type,
            "Swipe": self._handle_swipe,
            "Back": self._handle_back,
            "Home": self._handle_home,
            "Double Tap": self._handle_double_tap,
            "Long Press": self._handle_long_press,
            "Wait": self._handle_wait,
            "Take_over": self._handle_takeover,
            "Note": self._handle_note,
            "Call_API": self._handle_call_api,
            "Interact": self._handle_interact,
        }
        return handlers.get(action_name)

    @staticmethod
    def _center_from_bounds(bounds: str) -> tuple[int, int] | None:
        """
        Parse Android UIAutomator bounds string "[l,t][r,b]" and return center (x, y).
        """
        try:
            nums = list(map(int, re.findall(r"\d+", bounds)))
            if len(nums) != 4:
                return None
            left, top, right, bottom = nums
            return (left + right) // 2, (top + bottom) // 2
        except Exception:
            return None

    def _convert_relative_to_absolute(
        self, element: list[int], screen_width: int, screen_height: int
    ) -> tuple[int, int]:
        """Convert relative coordinates (0-1000) to absolute pixels."""
        x = int(element[0] / 1000 * screen_width)
        y = int(element[1] / 1000 * screen_height)
        return x, y

    def _handle_launch(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle app launch action."""
        app_name = action.get("app")
        if not app_name:
            return ActionResult(False, False, "No app name specified")

        device_factory = get_device_factory()
        success = device_factory.launch_app(app_name, self.device_id)
        if success:
            return ActionResult(True, False)
        return ActionResult(False, False, f"App not found: {app_name}")

    def _handle_tap(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle tap action (坐标点击).

        说明：这里恢复为执行真实的坐标点击，具体是否使用由提示词约束，
        主要依赖 TapByText / TapRoomArrowByText 来精确点击，Tap 仅作为兜底方案。
        """
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y = self._convert_relative_to_absolute(element, width, height)

        # Check for sensitive operation
        if "message" in action:
            if not self.confirmation_callback(action["message"]):
                return ActionResult(
                    success=False,
                    should_finish=True,
                    message="User cancelled sensitive operation",
                )

        device_factory = get_device_factory()
        device_factory.tap(x, y, self.device_id)

        # Optional guard to avoid misnavigation (best-effort).
        # For ADB devices, we can verify via `uiautomator dump` XML.
        expect_text = action.get("expect_text_contains")
        if isinstance(expect_text, str) and expect_text.strip():
            expect_text = expect_text.strip()
            found = False
            # allow a couple of short retries for page transition
            for _ in range(3):
                xml = device_factory.get_ui_hierarchy_xml(self.device_id)
                if xml and expect_text in xml:
                    found = True
                    break
                time.sleep(0.4)

            if not found:
                # Best-effort rollback: go back to reduce chance of staying on wrong page
                try:
                    device_factory.back(self.device_id)
                except Exception:
                    pass
                return ActionResult(
                    success=False,
                    should_finish=False,
                    message=f'expect_text_contains 未命中："{expect_text}"',
                )
        return ActionResult(True, False)

    def _handle_tap_by_text(self, action: dict, width: int, height: int) -> ActionResult:
        """
        Handle tap by text action.

        Expected format:
            do(action="TapByText", text="房型标题或按钮文字")

        It will:
        1. Dump current UI hierarchy XML.
        2. Find the first node whose `text` contains the given substring.
        3. Tap the center of that node's bounds.
        """
        target_text = (action.get("text") or "").strip()
        if not target_text:
            return ActionResult(False, False, "TapByText 缺少 text 字段")

        device_factory = get_device_factory()
        xml = device_factory.get_ui_hierarchy_xml(self.device_id)
        if not xml:
            return ActionResult(False, False, "无法获取当前页面 UI 层级，TapByText 失败")

        try:
            root = ET.fromstring(xml)
        except Exception as e:
            return ActionResult(False, False, f"解析 UI XML 失败: {e}")

        # 简单策略：找第一个 text 含 target_text 的节点
        for node in root.iter("node"):
            text = node.attrib.get("text") or ""
            if target_text not in text:
                continue
            bounds = node.attrib.get("bounds")
            center = self._center_from_bounds(bounds) if bounds else None
            if not center:
                continue
            x, y = center
            device_factory.tap(x, y, self.device_id)
            return ActionResult(True, False)

        return ActionResult(
            False,
            False,
            f'TapByText 未在 UI 树中找到包含文本 "{target_text}" 的节点',
        )

    def _handle_tap_room_arrow_by_text(self, action: dict, width: int, height: int) -> ActionResult:
        """
        基于房型标题文本点击其右侧的折叠箭头。

        预期格式:
            do(action="TapRoomArrowByText", text="房型标题中的一段中文，例如 商务静谧大床房")

        实现思路:
        1. 在 UI 树中找到 text 含目标子串的房型标题节点。
        2. 以该节点的父节点为“房型卡片容器”，在容器内查找 clickable=true 且位于右侧、纵向与标题行接近的节点作为折叠箭头。
        3. 点击该箭头节点的 bounds 中心。
        """
        target_text = (action.get("text") or "").strip()
        if not target_text:
            return ActionResult(False, False, "TapRoomArrowByText 缺少 text 字段")

        device_factory = get_device_factory()
        xml = device_factory.get_ui_hierarchy_xml(self.device_id)
        if not xml:
            return ActionResult(False, False, "无法获取当前页面 UI 层级，TapRoomArrowByText 失败")

        try:
            root = ET.fromstring(xml)
        except Exception as e:
            return ActionResult(False, False, f"解析 UI XML 失败: {e}")

        # 先找到 title 节点及其父节点
        title_node = None
        title_parent = None
        title_center = None

        for parent in root.iter("node"):
            for child in list(parent):
                if child.tag != "node":
                    continue
                text = child.attrib.get("text") or ""
                if target_text and target_text not in text:
                    continue
                bounds = child.attrib.get("bounds")
                center = self._center_from_bounds(bounds) if bounds else None
                if not center:
                    continue
                title_node = child
                title_parent = parent
                title_center = center
                break
            if title_node is not None:
                break

        if title_node is None or title_parent is None or title_center is None:
            return ActionResult(
                False,
                False,
                f'TapRoomArrowByText 未在 UI 树中找到包含文本 "{target_text}" 的房型标题节点',
            )

        title_x, title_y = title_center

        # 在同一房型卡片容器内寻找“右侧、可点击、纵向接近标题行”的节点作为箭头
        arrow_center: tuple[int, int] | None = None
        best_dy = None

        for node in title_parent.iter("node"):
            if node is title_node:
                continue
            if node.attrib.get("clickable") != "true":
                continue
            bounds = node.attrib.get("bounds")
            center = self._center_from_bounds(bounds) if bounds else None
            if not center:
                continue
            cx, cy = center
            # 只考虑标题右侧的候选（避免点到标题左侧图片等）
            if cx <= title_x:
                continue
            dy = abs(cy - title_y)
            # 过滤掉垂直距离过大的节点（不在同一行）
            if dy > 120:
                continue
            if best_dy is None or dy < best_dy:
                best_dy = dy
                arrow_center = center

        if arrow_center is None:
            return ActionResult(
                False,
                False,
                f'TapRoomArrowByText 未在 UI 树中为房型 "{target_text}" 找到合适的折叠箭头节点',
            )

        ax, ay = arrow_center
        device_factory.tap(ax, ay, self.device_id)
        return ActionResult(True, False)

    def _handle_type(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle text input action."""
        text = action.get("text", "")

        device_factory = get_device_factory()

        # Switch to ADB keyboard
        original_ime = device_factory.detect_and_set_adb_keyboard(self.device_id)
        time.sleep(TIMING_CONFIG.action.keyboard_switch_delay)

        # Clear existing text and type new text
        device_factory.clear_text(self.device_id)
        time.sleep(TIMING_CONFIG.action.text_clear_delay)

        # Handle multiline text by splitting on newlines
        device_factory.type_text(text, self.device_id)
        time.sleep(TIMING_CONFIG.action.text_input_delay)

        # Restore original keyboard
        device_factory.restore_keyboard(original_ime, self.device_id)
        time.sleep(TIMING_CONFIG.action.keyboard_restore_delay)

        return ActionResult(True, False)

    def _handle_swipe(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle swipe action."""
        start = action.get("start")
        end = action.get("end")

        if not start or not end:
            return ActionResult(False, False, "Missing swipe coordinates")

        start_x, start_y = self._convert_relative_to_absolute(start, width, height)
        end_x, end_y = self._convert_relative_to_absolute(end, width, height)

        device_factory = get_device_factory()
        device_factory.swipe(start_x, start_y, end_x, end_y, device_id=self.device_id)
        return ActionResult(True, False)

    def _handle_back(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle back button action."""
        device_factory = get_device_factory()
        device_factory.back(self.device_id)
        return ActionResult(True, False)

    def _handle_home(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle home button action."""
        device_factory = get_device_factory()
        device_factory.home(self.device_id)
        return ActionResult(True, False)

    def _handle_double_tap(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle double tap action."""
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y = self._convert_relative_to_absolute(element, width, height)
        device_factory = get_device_factory()
        device_factory.double_tap(x, y, self.device_id)
        return ActionResult(True, False)

    def _handle_long_press(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle long press action."""
        element = action.get("element")
        if not element:
            return ActionResult(False, False, "No element coordinates")

        x, y = self._convert_relative_to_absolute(element, width, height)
        device_factory = get_device_factory()
        device_factory.long_press(x, y, device_id=self.device_id)
        return ActionResult(True, False)

    def _handle_wait(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle wait action."""
        duration_str = action.get("duration", "1 seconds")
        try:
            duration = float(duration_str.replace("seconds", "").strip())
        except ValueError:
            duration = 1.0

        time.sleep(duration)
        return ActionResult(True, False)

    def _handle_takeover(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle takeover request (login, captcha, etc.)."""
        message = action.get("message", "User intervention required")
        self.takeover_callback(message)
        return ActionResult(True, False)

    def _handle_note(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle note action (placeholder for content recording)."""
        # This action is typically used for recording page content
        # Implementation depends on specific requirements
        return ActionResult(True, False)

    def _handle_call_api(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle API call action (placeholder for summarization)."""
        # This action is typically used for content summarization
        # Implementation depends on specific requirements
        return ActionResult(True, False)

    def _handle_interact(self, action: dict, width: int, height: int) -> ActionResult:
        """Handle interaction request (user choice needed)."""
        # This action signals that user input is needed
        return ActionResult(True, False, message="User interaction required")

    def _send_keyevent(self, keycode: str) -> None:
        """Send a keyevent to the device."""
        from phone_agent.device_factory import DeviceType, get_device_factory
        from phone_agent.hdc.connection import _run_hdc_command

        device_factory = get_device_factory()

        # Handle HDC devices with HarmonyOS-specific keyEvent command
        if device_factory.device_type == DeviceType.HDC:
            hdc_prefix = ["hdc", "-t", self.device_id] if self.device_id else ["hdc"]
            
            # Map common keycodes to HarmonyOS keyEvent codes
            # KEYCODE_ENTER (66) -> 2054 (HarmonyOS Enter key code)
            if keycode == "KEYCODE_ENTER" or keycode == "66":
                _run_hdc_command(
                    hdc_prefix + ["shell", "uitest", "uiInput", "keyEvent", "2054"],
                    capture_output=True,
                    text=True,
                )
            else:
                # For other keys, try to use the numeric code directly
                # If keycode is a string like "KEYCODE_ENTER", convert it
                try:
                    # Try to extract numeric code from string or use as-is
                    if keycode.startswith("KEYCODE_"):
                        # For now, only handle ENTER, other keys may need mapping
                        if "ENTER" in keycode:
                            _run_hdc_command(
                                hdc_prefix + ["shell", "uitest", "uiInput", "keyEvent", "2054"],
                                capture_output=True,
                                text=True,
                            )
                        else:
                            # Fallback to ADB-style command for unsupported keys
                            subprocess.run(
                                hdc_prefix + ["shell", "input", "keyevent", keycode],
                                capture_output=True,
                                text=True,
                            )
                    else:
                        # Assume it's a numeric code
                        _run_hdc_command(
                            hdc_prefix + ["shell", "uitest", "uiInput", "keyEvent", str(keycode)],
                            capture_output=True,
                            text=True,
                        )
                except Exception:
                    # Fallback to ADB-style command
                    subprocess.run(
                        hdc_prefix + ["shell", "input", "keyevent", keycode],
                        capture_output=True,
                        text=True,
                    )
        else:
            # ADB devices use standard input keyevent command
            cmd_prefix = ["adb", "-s", self.device_id] if self.device_id else ["adb"]
            subprocess.run(
                cmd_prefix + ["shell", "input", "keyevent", keycode],
                capture_output=True,
                text=True,
            )

    @staticmethod
    def _default_confirmation(message: str) -> bool:
        """Default confirmation callback using console input."""
        response = input(f"Sensitive operation: {message}\nConfirm? (Y/N): ")
        return response.upper() == "Y"

    @staticmethod
    def _default_takeover(message: str) -> None:
        """Default takeover callback using console input."""
        input(f"{message}\nPress Enter after completing manual operation...")


def parse_action(response: str) -> dict[str, Any]:
    """
    Parse action from model response.

    Args:
        response: Raw response string from the model.

    Returns:
        Parsed action dictionary.

    Raises:
        ValueError: If the response cannot be parsed.
    """
    print(f"Parsing action: {response}")
    try:
        raw = (response or "").strip()

        def _extract_first_call(text: str) -> str | None:
            """
            Extract the first complete `do(...)` or `finish(...)` call from a possibly
            noisy model output (may contain trailing punctuation/extra text).
            """
            if not text:
                return None
            # Prefer earliest occurrence of either marker
            idx_do = text.find("do(")
            idx_finish = text.find("finish(")
            idxs = [i for i in [idx_do, idx_finish] if i >= 0]
            if not idxs:
                return None
            start = min(idxs)

            in_str: str | None = None
            escape = False
            depth = 0
            for i in range(start, len(text)):
                ch = text[i]
                if escape:
                    escape = False
                    continue
                if in_str is not None:
                    if ch == "\\":
                        escape = True
                        continue
                    if ch == in_str:
                        in_str = None
                    continue
                else:
                    if ch in ('"', "'"):
                        in_str = ch
                        continue
                    if ch == "(":
                        depth += 1
                        continue
                    if ch == ")":
                        depth -= 1
                        if depth == 0:
                            return text[start : i + 1].strip()
                        continue
            return None

        extracted = _extract_first_call(raw)
        if not extracted:
            raise ValueError(f"Failed to parse action: {raw}")

        # Escape special characters for valid Python syntax
        extracted = extracted.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")

        try:
            tree = ast.parse(extracted, mode="eval")
            if not isinstance(tree.body, ast.Call):
                raise ValueError("Expected a function call")
            call = tree.body
            fn = call.func.id if isinstance(call.func, ast.Name) else None
            if fn not in {"do", "finish"}:
                raise ValueError(f"Unexpected function: {fn}")

            # Extract keyword arguments safely
            action: dict[str, Any] = {"_metadata": "do" if fn == "do" else "finish"}
            for keyword in call.keywords:
                key = keyword.arg
                value = ast.literal_eval(keyword.value)
                action[key] = value

            # Normalize some common variants
            if action["_metadata"] == "finish":
                if "message" not in action:
                    action["message"] = ""
            return action
        except (SyntaxError, ValueError) as e:
            # Fallback: model sometimes pastes prompt into finish(message="..."); accept known short phase messages
            if "finish(" in raw and "message" in raw:
                for known in ("done_phase1", "no_hot_section", "at_top"):
                    if f'message="{known}"' in raw or f"message='{known}'" in raw:
                        return {"_metadata": "finish", "message": known}
            raise ValueError(f"Failed to parse action: {e} (extracted={extracted})")
    except Exception as e:
        raise ValueError(f"Failed to parse action: {e}")


def do(**kwargs) -> dict[str, Any]:
    """Helper function for creating 'do' actions."""
    kwargs["_metadata"] = "do"
    return kwargs


def finish(**kwargs) -> dict[str, Any]:
    """Helper function for creating 'finish' actions."""
    kwargs["_metadata"] = "finish"
    return kwargs
