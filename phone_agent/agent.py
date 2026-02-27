"""Main PhoneAgent class for orchestrating phone automation."""

import json
import traceback
from dataclasses import dataclass
from typing import Any, Callable

from phone_agent.actions import ActionHandler
from phone_agent.actions.handler import do, finish, parse_action
from phone_agent.config import get_messages, get_system_prompt
from phone_agent.device_factory import DeviceType, get_device_factory
from phone_agent.model import ModelClient, ModelConfig
from phone_agent.model.client import MessageBuilder


@dataclass
class AgentConfig:
    """Configuration for the PhoneAgent."""

    max_steps: int = 100
    device_id: str | None = None
    lang: str = "cn"
    system_prompt: str | None = None
    verbose: bool = True

    def __post_init__(self):
        if self.system_prompt is None:
            self.system_prompt = get_system_prompt(self.lang)


@dataclass
class StepResult:
    """Result of a single agent step."""

    success: bool
    finished: bool
    action: dict[str, Any] | None
    thinking: str
    message: str | None = None


class PhoneAgent:
    """
    AI-powered agent for automating Android phone interactions.

    The agent uses a vision-language model to understand screen content
    and decide on actions to complete user tasks.

    Args:
        model_config: Configuration for the AI model.
        agent_config: Configuration for the agent behavior.
        confirmation_callback: Optional callback for sensitive action confirmation.
        takeover_callback: Optional callback for takeover requests.

    Example:
        >>> from phone_agent import PhoneAgent
        >>> from phone_agent.model import ModelConfig
        >>>
        >>> model_config = ModelConfig(base_url="http://localhost:8000/v1")
        >>> agent = PhoneAgent(model_config)
        >>> agent.run("Open WeChat and send a message to John")
    """

    def __init__(
        self,
        model_config: ModelConfig | None = None,
        agent_config: AgentConfig | None = None,
        confirmation_callback: Callable[[str], bool] | None = None,
        takeover_callback: Callable[[str], None] | None = None,
    ):
        self.model_config = model_config or ModelConfig()
        self.agent_config = agent_config or AgentConfig()

        self.model_client = ModelClient(self.model_config)
        self.action_handler = ActionHandler(
            device_id=self.agent_config.device_id,
            confirmation_callback=confirmation_callback,
            takeover_callback=takeover_callback,
        )

        self._context: list[dict[str, Any]] = []
        self._step_count = 0
        self._task_prompt: str | None = None
        self._last_action_result: dict[str, Any] | None = None
        self._pinned_task_message_added = False
        self._recent_actions: list[str] = []  # 最近几步操作摘要，用于避免重复循环

    def run(self, task: str) -> str:
        """
        Run the agent to complete a task.

        Args:
            task: Natural language description of the task.

        Returns:
            Final message from the agent.
        """
        self._context = []
        self._step_count = 0
        self._task_prompt = task
        self._last_action_result = None
        self._pinned_task_message_added = False
        self._recent_actions = []
        self._ensure_device_selected()

        # First step with user prompt
        result = self._execute_step(task, is_first=True)

        if result.finished:
            return result.message or "Task completed"

        # Continue until finished or max steps reached
        while self._step_count < self.agent_config.max_steps:
            result = self._execute_step(is_first=False)

            if result.finished:
                return result.message or "Task completed"

        return "Max steps reached"

    def step(self, task: str | None = None) -> StepResult:
        """
        Execute a single step of the agent.

        Useful for manual control or debugging.

        Args:
            task: Task description (only needed for first step).

        Returns:
            StepResult with step details.
        """
        is_first = len(self._context) == 0

        if is_first and not task:
            raise ValueError("Task is required for the first step")

        return self._execute_step(task, is_first)

    def reset(self) -> None:
        """Reset the agent state for a new task."""
        self._context = []
        self._step_count = 0

    def _execute_step(
        self, user_prompt: str | None = None, is_first: bool = False
    ) -> StepResult:
        """Execute a single step of the agent loop."""
        self._step_count += 1

        # Capture current screen state
        device_factory = get_device_factory()
        self._ensure_device_selected(device_factory=device_factory)
        screenshot = device_factory.get_screenshot(self.agent_config.device_id)
        current_app = device_factory.get_current_app(self.agent_config.device_id)

        # Pin a compact task message once, to survive context trimming.
        if not self._pinned_task_message_added and self._task_prompt:
            pinned_text = f"**Task**\n{self._task_prompt}\n"
            self._context.append(MessageBuilder.create_user_message(text=pinned_text))
            self._pinned_task_message_added = True

        # Build messages
        if is_first:
            self._context.append(
                MessageBuilder.create_system_message(self.agent_config.system_prompt)
            )

            screen_info = MessageBuilder.build_screen_info(
                current_app,
                last_action_result=self._last_action_result,
                recent_actions=self._recent_actions,
            )
            text_content = f"{user_prompt}\n\n{screen_info}"

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )
        else:
            screen_info = MessageBuilder.build_screen_info(
                current_app,
                last_action_result=self._last_action_result,
                recent_actions=self._recent_actions,
            )
            text_content = f"** Screen Info **\n\n{screen_info}"

            self._context.append(
                MessageBuilder.create_user_message(
                    text=text_content, image_base64=screenshot.base64_data
                )
            )

        # Get model response
        try:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "=" * 50)
            print(f"💭 {msgs['thinking']}:")
            print("-" * 50)
            self._trim_context()
            response = self.model_client.request(self._context)
        except Exception as e:
            if self.agent_config.verbose:
                traceback.print_exc()
            return StepResult(
                success=False,
                finished=True,
                action=None,
                thinking="",
                message=f"Model error: {e}",
            )

        # Parse action from response
        try:
            action = parse_action(response.action)
        except ValueError:
            if self.agent_config.verbose:
                traceback.print_exc()
            action = finish(message=response.action)

        # If model tries to finish, validate output format and force rewrite if needed
        if action.get("_metadata") == "finish":
            action = self._enforce_finish_format(action)

        if self.agent_config.verbose:
            # Print thinking process
            print("-" * 50)
            print(f"🎯 {msgs['action']}:")
            print(json.dumps(action, ensure_ascii=False, indent=2))
            print("=" * 50 + "\n")

        # Remove image from context to save space
        self._context[-1] = MessageBuilder.remove_images_from_message(self._context[-1])

        # Execute action
        try:
            result = self.action_handler.execute(
                action, screenshot.width, screenshot.height
            )
        except Exception as e:
            if self.agent_config.verbose:
                traceback.print_exc()
            result = self.action_handler.execute(
                finish(message=str(e)), screenshot.width, screenshot.height
            )

        # Store last action execution result for the next step
        self._last_action_result = {
            "success": result.success,
            "should_finish": result.should_finish,
            "message": result.message,
        }

        # Add assistant response to context
        # IMPORTANT: keep context small by storing only the normalized tool call.
        normalized_call = self._action_to_call(action)
        self._context.append(
            MessageBuilder.create_assistant_message(
                f"<think></think><answer>{normalized_call}</answer>"
            )
        )
        # 更新最近操作摘要，便于模型发现重复并跳出循环
        self._recent_actions = (self._recent_actions + [normalized_call])[-5:]

        # Check if finished
        finished = action.get("_metadata") == "finish" or result.should_finish

        if finished and self.agent_config.verbose:
            msgs = get_messages(self.agent_config.lang)
            print("\n" + "🎉 " + "=" * 48)
            print(
                f"✅ {msgs['task_completed']}: {result.message or action.get('message', msgs['done'])}"
            )
            print("=" * 50 + "\n")

        return StepResult(
            success=result.success,
            finished=finished,
            action=action,
            thinking=response.thinking,
            message=result.message or action.get("message"),
        )

    def _action_to_call(self, action: dict[str, Any]) -> str:
        """
        Serialize parsed action dict to a minimal canonical `do(...)` / `finish(...)` call string.
        This prevents large/noisy model output from bloating context.
        """
        meta = action.get("_metadata")
        if meta == "finish":
            msg = action.get("message", "")
            # Escape backslashes and quotes for a Python-like string literal.
            msg = str(msg).replace("\\", "\\\\").replace('"', '\\"')
            return f'finish(message="{msg}")'

        # default to do
        # Use stable key order for better model conditioning
        key_order = [
            "action",
            "app",
            "element",
            "start",
            "end",
            "text",
            "duration",
            "instruction",
            "message",
            "expect_text_contains",
        ]
        kwargs = {k: v for k, v in action.items() if k != "_metadata"}
        parts: list[str] = []
        for k in key_order:
            if k not in kwargs:
                continue
            v = kwargs[k]
            if isinstance(v, str):
                vv = v.replace("\\", "\\\\").replace('"', '\\"')
                parts.append(f'{k}="{vv}"')
            else:
                parts.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
        # Include any remaining keys (rare) at the end
        for k in sorted(set(kwargs.keys()) - set(key_order)):
            v = kwargs[k]
            if isinstance(v, str):
                vv = v.replace("\\", "\\\\").replace('"', '\\"')
                parts.append(f'{k}="{vv}"')
            else:
                parts.append(f"{k}={json.dumps(v, ensure_ascii=False)}")
        return f"do({', '.join(parts)})"

    def _trim_context(self, max_messages: int = 24) -> None:
        """
        Keep conversation context within a small rolling window to avoid exceeding
        model context limits. Images are already removed; we still cap message count.
        """
        if len(self._context) <= max_messages:
            return
        # Keep system + pinned task message (if present), then keep the most recent tail.
        head: list[dict[str, Any]] = []
        # Keep the first system message if already present
        for m in self._context[:4]:
            if m.get("role") == "system" and m not in head:
                head.append(m)
        # Keep first pinned task user message (starts with **Task**)
        for m in self._context[:6]:
            content = m.get("content")
            if m.get("role") == "user" and isinstance(content, list):
                # text-only user message has a list with a single {"type":"text","text":...}
                if content and content[-1].get("type") == "text":
                    txt = content[-1].get("text", "")
                    if isinstance(txt, str) and txt.startswith("**Task**"):
                        head.append(m)
                        break

        tail = self._context[-(max_messages - len(head)) :]
        self._context = head + tail

    def _extract_output_requirements(self) -> str | None:
        if not self._task_prompt:
            return None
        marker = "最终回复："
        if marker not in self._task_prompt:
            return None
        return self._task_prompt.split(marker, 1)[1].strip() or None

    def _ensure_device_selected(self, device_factory=None) -> None:
        """
        Ensure a valid device_id is selected.

        - If user provided a device_id but it's not currently connected, try `adb connect`
          for remote IDs, then fall back to auto-detected first device.
        - If device_id is None, auto-select the first connected device (if any) to make
          behavior deterministic across multi-device setups.
        """
        device_factory = device_factory or get_device_factory()

        # Only ADB/HDC have device_id selection here; iOS agent is separate.
        if device_factory.device_type == DeviceType.IOS:
            return

        devices = []
        try:
            devices = device_factory.list_devices()
        except Exception:
            devices = []

        def _is_connected(dev_id: str) -> bool:
            for d in devices:
                if getattr(d, "device_id", None) == dev_id and getattr(d, "status", None) == "device":
                    return True
            return False

        current = self.agent_config.device_id
        if current:
            if _is_connected(current):
                return

            # Try to connect remote device IDs for ADB
            if device_factory.device_type == DeviceType.ADB and ":" in current:
                try:
                    from phone_agent.adb import ADBConnection

                    ADBConnection().connect(current)
                    devices = device_factory.list_devices()
                    if _is_connected(current):
                        return
                except Exception:
                    pass

            # Provided device_id is invalid/offline: clear and fall back
            self.agent_config.device_id = None
            self.action_handler.device_id = None

        # Auto-select first connected device if available
        if not self.agent_config.device_id and devices:
            first = None
            for d in devices:
                if getattr(d, "status", None) == "device":
                    first = d
                    break
            if first is None:
                first = devices[0]
            self.agent_config.device_id = getattr(first, "device_id", None)
            self.action_handler.device_id = self.agent_config.device_id

    def _validate_finish_message(self, message: str) -> tuple[bool, str]:
        """
        Best-effort validator for finish output. We mainly reject:
        - step-by-step narratives ("我已经…", "步骤…", numbered lists)
        - overly long verbose descriptions
        When user prompt includes "最终回复：" we treat it as a strong requirement.
        """
        msg = (message or "").strip()
        if not msg:
            return False, "finish(message) 为空"

        # finish message must not contain tool/action invocations
        forbidden_call_tokens = ["do(action=", "finish(", "Take_over", "Launch", "Tap", "Swipe", "Back", "Home"]
        for tok in forbidden_call_tokens:
            if tok in msg:
                return False, f'finish(message) 不应包含动作调用/指令片段："{tok}"'

        # Strong signals of "过程复述" which the user explicitly forbids
        banned_phrases = [
            "我已经",
            "让我总结",
            "完成了用户的任务",
            "总结一下",
            "步骤",
            "打开了",
            "点击了",
            "进入了",
            "筛选了",
            "包括：",
            "任务完成",
        ]
        for p in banned_phrases:
            if p in msg:
                return False, f'包含禁止的过程性表述："{p}"'

        # Numbered lists are usually step logs in this project
        if any(tok in msg for tok in ["\n1.", "\n2.", "\n3.", "\n1、", "\n2、", "\n3、"]):
            return False, "包含步骤式编号列表"

        # If prompt explicitly demands structured summary, keep it concise
        req = self._extract_output_requirements()
        if req and len(msg) > 1200:
            return False, "过长，疑似包含无关页面描述"

        return True, "ok"

    def _norm_finish_message(self, action: dict[str, Any]) -> str:
        """Normalize finish action message to str; avoid Ellipsis or non-str."""
        msg = action.get("message")
        return (msg if isinstance(msg, str) else "").strip()

    def _enforce_finish_format(self, action: dict[str, Any]) -> dict[str, Any]:
        """
        If finish output doesn't satisfy constraints, ask model to rewrite.
        This avoids returning non-compliant verbose narratives to the user.
        """
        # 先把 message 规范成字符串，写回到 action，避免 Ellipsis 等非字符串导致后续 json.dumps 崩溃
        message = self._norm_finish_message(action)
        action["message"] = message
        ok, reason = self._validate_finish_message(message)
        if ok:
            return action

        requirements = self._extract_output_requirements()
        req_text = requirements or "请严格遵循用户在任务描述中对最终输出格式/内容的要求。"

        # Up to 2 rewrite attempts without changing device state
        for _ in range(2):
            rewrite_prompt = (
                "你刚才的 finish(message=...) 不符合用户要求，需要重写。\n"
                f"不合格原因：{reason}\n\n"
                "用户对最终输出的硬性要求如下：\n"
                f"{req_text}\n\n"
                "请立刻输出：finish(message=\"...\")。\n"
                "- message 只包含最终汇总，不要复述操作步骤，不要描述界面过程\n"
                "- 不要写“我已经/任务完成/步骤”等过程性文字\n"
                "- 不要输出除 finish(...) 之外的任何内容\n"
            )
            self._context.append(MessageBuilder.create_user_message(text=rewrite_prompt))
            self._trim_context()
            response = self.model_client.request(self._context)
            try:
                new_action = parse_action(response.action)
            except ValueError:
                new_action = finish(message=response.action)

            if new_action.get("_metadata") != "finish":
                action = finish(message=(self._norm_finish_message(new_action) or response.action))
                continue

            new_msg = self._norm_finish_message(new_action)
            new_action["message"] = new_msg
            ok, reason = self._validate_finish_message(new_msg)
            # record the assistant content for transparency in context (keep it minimal)
            self._context.append(
                MessageBuilder.create_assistant_message(
                    f"<think></think><answer>{self._action_to_call(new_action)}</answer>"
                )
            )
            if ok:
                return new_action

            action = new_action

        # Fallback: return the latest rewrite attempt (even if still imperfect)
        return action

    @property
    def context(self) -> list[dict[str, Any]]:
        """Get the current conversation context."""
        return self._context.copy()

    @property
    def step_count(self) -> int:
        """Get the current step count."""
        return self._step_count
