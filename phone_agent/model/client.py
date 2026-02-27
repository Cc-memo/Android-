"""Model client for AI inference using OpenAI-compatible API."""

import json
import random
import time
from dataclasses import dataclass, field
from typing import Any

from openai import APITimeoutError, APIConnectionError, InternalServerError, OpenAI, RateLimitError

from phone_agent.config.i18n import get_message


@dataclass
class ModelConfig:
    """Configuration for the AI model."""

    base_url: str = "http://localhost:8000/v1"
    api_key: str = "EMPTY"
    model_name: str = "autoglm-phone-9b"
    max_tokens: int = 3000
    temperature: float = 0.0
    top_p: float = 0.85
    frequency_penalty: float = 0.2
    extra_body: dict[str, Any] = field(default_factory=dict)
    lang: str = "cn"  # Language for UI messages: 'cn' or 'en'
    request_max_retries: int = 4
    request_retry_backoff_base_seconds: float = 1.0
    request_retry_backoff_max_seconds: float = 12.0


@dataclass
class ModelResponse:
    """Response from the AI model."""

    thinking: str
    action: str
    raw_content: str
    # Performance metrics
    time_to_first_token: float | None = None  # Time to first token (seconds)
    time_to_thinking_end: float | None = None  # Time to thinking end (seconds)
    total_time: float | None = None  # Total inference time (seconds)


class ModelClient:
    """
    Client for interacting with OpenAI-compatible vision-language models.

    Args:
        config: Model configuration.
    """

    def __init__(self, config: ModelConfig | None = None):
        self.config = config or ModelConfig()
        self.client = OpenAI(base_url=self.config.base_url, api_key=self.config.api_key)

    def request(self, messages: list[dict[str, Any]]) -> ModelResponse:
        """
        Send a request to the model.

        Args:
            messages: List of message dictionaries in OpenAI format.

        Returns:
            ModelResponse containing thinking and action.

        Raises:
            ValueError: If the response cannot be parsed.
        """
        def _is_retriable_error(err: Exception) -> bool:
            if isinstance(err, (APIConnectionError, APITimeoutError, RateLimitError)):
                return True
            if isinstance(err, InternalServerError):
                # 常见的临时网关/服务端错误
                return getattr(err, "status_code", None) in {502, 503, 504}
            return False

        def _get_backoff_seconds(attempt_idx: int) -> float:
            # 指数退避 + 少量抖动，避免同时重试打爆服务
            base = max(0.0, self.config.request_retry_backoff_base_seconds)
            cap = max(base, self.config.request_retry_backoff_max_seconds)
            exp = min(cap, base * (2**attempt_idx))
            jitter = random.uniform(0.0, min(0.25, exp))
            return min(cap, exp + jitter)

        last_err: Exception | None = None
        for attempt in range(max(1, int(self.config.request_max_retries) + 1)):
            # Start timing per attempt
            start_time = time.time()
            time_to_first_token = None
            time_to_thinking_end = None

            raw_content = ""
            buffer = ""  # Buffer to hold content that might be part of a marker
            action_markers = ["finish(message=", "do(action="]
            in_action_phase = False  # Track if we've entered the action phase
            first_token_received = False

            try:
                stream = self.client.chat.completions.create(
                    messages=messages,
                    model=self.config.model_name,
                    max_tokens=self.config.max_tokens,
                    temperature=self.config.temperature,
                    top_p=self.config.top_p,
                    frequency_penalty=self.config.frequency_penalty,
                    extra_body=self.config.extra_body,
                    stream=True,
                )

                for chunk in stream:
                    if len(chunk.choices) == 0:
                        continue
                    if chunk.choices[0].delta.content is not None:
                        content = chunk.choices[0].delta.content
                        raw_content += content

                        # Record time to first token
                        if not first_token_received:
                            time_to_first_token = time.time() - start_time
                            first_token_received = True

                        if in_action_phase:
                            # Already in action phase, just accumulate content without printing
                            continue

                        buffer += content

                        # Check if any marker is fully present in buffer
                        marker_found = False
                        for marker in action_markers:
                            if marker in buffer:
                                # Marker found, print everything before it
                                thinking_part = buffer.split(marker, 1)[0]
                                print(thinking_part, end="", flush=True)
                                print()  # Print newline after thinking is complete
                                in_action_phase = True
                                marker_found = True

                                # Record time to thinking end
                                if time_to_thinking_end is None:
                                    time_to_thinking_end = time.time() - start_time

                                break

                        if marker_found:
                            continue  # Continue to collect remaining content

                        # Check if buffer ends with a prefix of any marker
                        # If so, don't print yet (wait for more content)
                        is_potential_marker = False
                        for marker in action_markers:
                            for i in range(1, len(marker)):
                                if buffer.endswith(marker[:i]):
                                    is_potential_marker = True
                                    break
                            if is_potential_marker:
                                break

                        if not is_potential_marker:
                            # Safe to print the buffer
                            print(buffer, end="", flush=True)
                            buffer = ""

                # Calculate total time
                total_time = time.time() - start_time
                break
            except Exception as e:
                last_err = e
                if attempt >= int(self.config.request_max_retries) or not _is_retriable_error(e):
                    raise

                wait_s = _get_backoff_seconds(attempt)
                status = getattr(e, "status_code", None)
                status_str = f"HTTP {status}" if status else e.__class__.__name__
                print(
                    f"\n（模型服务暂时不可用：{status_str}，{wait_s:.1f}s 后重试 {attempt + 1}/{self.config.request_max_retries + 1}）"
                )
                time.sleep(wait_s)

        else:
            # 理论上不会走到这里；兜底抛出最后一次错误
            if last_err is not None:
                raise last_err
            raise RuntimeError("Model request failed without exception.")

        # Parse thinking and action from response
        thinking, action = self._parse_response(raw_content)

        # Print performance metrics
        lang = self.config.lang
        print()
        print("=" * 50)
        print(f"⏱️  {get_message('performance_metrics', lang)}:")
        print("-" * 50)
        if time_to_first_token is not None:
            print(
                f"{get_message('time_to_first_token', lang)}: {time_to_first_token:.3f}s"
            )
        if time_to_thinking_end is not None:
            print(
                f"{get_message('time_to_thinking_end', lang)}:        {time_to_thinking_end:.3f}s"
            )
        print(
            f"{get_message('total_inference_time', lang)}:          {total_time:.3f}s"
        )
        print("=" * 50)

        return ModelResponse(
            thinking=thinking,
            action=action,
            raw_content=raw_content,
            time_to_first_token=time_to_first_token,
            time_to_thinking_end=time_to_thinking_end,
            total_time=total_time,
        )

    def _parse_response(self, content: str) -> tuple[str, str]:
        """
        Parse the model response into thinking and action parts.

        Parsing rules:
        1. If content contains 'finish(message=', everything before is thinking,
           everything from 'finish(message=' onwards is action.
        2. If rule 1 doesn't apply but content contains 'do(action=',
           everything before is thinking, everything from 'do(action=' onwards is action.
        3. Fallback: If content contains '<answer>', use legacy parsing with XML tags.
        4. Otherwise, return empty thinking and full content as action.

        Args:
            content: Raw response content.

        Returns:
            Tuple of (thinking, action).
        """
        # Rule 1: Check for finish(message=
        if "finish(message=" in content:
            parts = content.split("finish(message=", 1)
            thinking = parts[0].strip()
            action = "finish(message=" + parts[1]
            return thinking, action

        # Rule 2: Check for do(action=
        if "do(action=" in content:
            parts = content.split("do(action=", 1)
            thinking = parts[0].strip()
            action = "do(action=" + parts[1]
            return thinking, action

        # Rule 3: Fallback to legacy XML tag parsing
        if "<answer>" in content:
            parts = content.split("<answer>", 1)
            thinking = parts[0].replace("<think>", "").replace("</think>", "").strip()
            action = parts[1].replace("</answer>", "").strip()
            return thinking, action

        # Rule 4: No markers found, return content as action
        return "", content


class MessageBuilder:
    """Helper class for building conversation messages."""

    @staticmethod
    def create_system_message(content: str) -> dict[str, Any]:
        """Create a system message."""
        return {"role": "system", "content": content}

    @staticmethod
    def create_user_message(
        text: str, image_base64: str | None = None
    ) -> dict[str, Any]:
        """
        Create a user message with optional image.

        Args:
            text: Text content.
            image_base64: Optional base64-encoded image.

        Returns:
            Message dictionary.
        """
        content = []

        if image_base64:
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/png;base64,{image_base64}"},
                }
            )

        content.append({"type": "text", "text": text})

        return {"role": "user", "content": content}

    @staticmethod
    def create_assistant_message(content: str) -> dict[str, Any]:
        """Create an assistant message."""
        return {"role": "assistant", "content": content}

    @staticmethod
    def remove_images_from_message(message: dict[str, Any]) -> dict[str, Any]:
        """
        Remove image content from a message to save context space.

        Args:
            message: Message dictionary.

        Returns:
            Message with images removed.
        """
        if isinstance(message.get("content"), list):
            message["content"] = [
                item for item in message["content"] if item.get("type") == "text"
            ]
        return message

    @staticmethod
    def build_screen_info(current_app: str, **extra_info) -> str:
        """
        Build screen info string for the model.

        Args:
            current_app: Current app name.
            **extra_info: Additional info to include (e.g. last_action_result, recent_actions).

        Returns:
            JSON string with screen info.
        """
        info = {"current_app": current_app}
        recent_actions = extra_info.pop("recent_actions", None)
        info.update(extra_info)
        text = json.dumps(info, ensure_ascii=False)
        if recent_actions:
            text += "\n\n最近几步操作（避免重复同一操作）:\n" + "\n".join(
                f"  - {a}" for a in recent_actions
            )
            text += "\n若最近几次操作与本次打算相同或界面无明显变化，请换其他目标或结束任务并 finish。"
        return text
