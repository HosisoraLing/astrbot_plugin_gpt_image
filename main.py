from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools, register
import astrbot.api.message_components as Comp
from astrbot.core.star.filter.command import GreedyStr


PLUGIN_NAME = "astrbot_plugin_gpt_image"
SUPPORTED_MODELS = {
    "gpt-image-2.5-sunburst",
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst-2026-09-08",
    "gpt-image-2.5-flare-2026-09-08",
}
SUPPORTED_SIZES = {"auto", "1024x1024", "1536x1024", "1024x1536"}
SUPPORTED_QUALITIES = {"auto", "low", "medium", "high", "xhigh", "max"}
SUPPORTED_FORMATS = {"png", "jpeg", "webp"}
SUPPORTED_BACKGROUNDS = {"auto", "opaque", "transparent"}


class ImageGenerationError(RuntimeError):
    """A user-safe image generation failure."""


@register(
    PLUGIN_NAME,
    "Codex",
    "通过 GPT Image 2.5 生成和修改图片，支持指令与 LLM Tool。",
    "v1.3.0",
)
class GPTImagePlugin(Star):
    def __init__(
        self, context: Context, config: AstrBotConfig | None = None
    ) -> None:
        super().__init__(context)
        self.config = config or {}
        self._semaphore = asyncio.Semaphore(
            self._bounded_int("max_concurrency", 1, 1, 4)
        )
        self._last_request: dict[str, float] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._last_generated: dict[str, Path] = {}
        self._codex_sessions: dict[str, str] = {}
        self._path_codex_sessions: dict[str, str] = {}
        self._data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME)) / "generated"
        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._input_dir = self._data_dir.parent / "inputs"
        self._input_dir.mkdir(parents=True, exist_ok=True)
        self._session_manifest = self._data_dir.parent / "codex_sessions.json"
        self._load_session_manifest()

    def _load_session_manifest(self) -> None:
        try:
            raw = json.loads(self._session_manifest.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                return
            for filename, session_id in raw.items():
                path = (self._data_dir / str(filename)).resolve()
                if (
                    path.parent == self._data_dir.resolve()
                    and path.is_file()
                    and isinstance(session_id, str)
                    and session_id.strip()
                ):
                    self._path_codex_sessions[str(path)] = session_id.strip()
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return

    def _save_session_manifest(self) -> None:
        entries = {
            Path(path).name: session_id
            for path, session_id in self._path_codex_sessions.items()
            if Path(path).is_file() and session_id
        }
        temporary = self._session_manifest.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary.replace(self._session_manifest)

    def _bounded_int(self, key: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            value = default
        return min(max(value, minimum), maximum)

    def _api_key(self) -> str:
        configured = str(self.config.get("api_key", "") or "").strip()
        key = configured or os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise ImageGenerationError(
                "未配置 API Key。请在插件配置中填写 api_key，或设置 OPENAI_API_KEY。"
            )
        return key

    def _endpoint(self) -> str:
        base = str(
            self.config.get("api_base_url", "https://api.openai.com/v1")
            or "https://api.openai.com/v1"
        ).strip().rstrip("/")
        if not base.startswith(("https://", "http://")):
            raise ImageGenerationError("api_base_url 必须是 http:// 或 https:// 地址。")
        if base.endswith("/images/generations"):
            return base
        if base.endswith("/v1"):
            return f"{base}/images/generations"
        return f"{base}/v1/images/generations"

    def _setting(self, key: str, default: str, allowed: set[str]) -> str:
        value = str(self.config.get(key, default) or default).strip().lower()
        return value if value in allowed else default

    def _model(self) -> str:
        return self._setting(
            "model", "gpt-image-2.5-sunburst", SUPPORTED_MODELS
        )

    def _backend(self) -> str:
        backend = str(self.config.get("backend", "codex_subscription") or "").strip()
        if backend not in {"codex_subscription", "openai_api"}:
            return "codex_subscription"
        return backend

    def _validate_prompt(self, prompt: str) -> str:
        prompt = str(prompt or "").strip()
        if not prompt:
            raise ImageGenerationError("提示词不能为空。用法：/画图 <图片描述>")
        max_chars = self._bounded_int("max_prompt_chars", 8000, 1, 32000)
        if len(prompt) > max_chars:
            raise ImageGenerationError(
                f"提示词过长：{len(prompt)} 字符，当前上限为 {max_chars}。"
            )
        return prompt

    def _check_cooldown(self, event: AstrMessageEvent) -> None:
        cooldown = self._bounded_int("cooldown_seconds", 10, 0, 3600)
        if cooldown <= 0:
            return
        sender = str(event.get_sender_id() or "unknown")
        now = time.monotonic()
        remaining = cooldown - (now - self._last_request.get(sender, 0.0))
        if remaining > 0:
            raise ImageGenerationError(f"请求过快，请 {int(remaining) + 1} 秒后再试。")
        self._last_request[sender] = now

    def _access_denial(self, event: AstrMessageEvent) -> str | None:
        """Return a user-facing denial reason, or None when generation is allowed."""
        try:
            is_admin = bool(event.is_admin())
        except Exception:
            is_admin = False

        if bool(self.config.get("admin_only", False)) and not is_admin:
            return "GPT Image 已设置为仅管理员可用。"
        if is_admin:
            return None
        if not bool(self.config.get("whitelist_enabled", False)):
            return None

        raw_ids = self.config.get("whitelist_ids", self.config.get("whitelist", []))
        if isinstance(raw_ids, str):
            allowed = {
                item.strip()
                for item in raw_ids.replace("\n", ",").split(",")
                if item.strip()
            }
        elif isinstance(raw_ids, (list, tuple, set)):
            allowed = {str(item).strip() for item in raw_ids if str(item).strip()}
        else:
            allowed = set()

        candidates = {
            str(event.get_sender_id() or "").strip(),
            str(event.get_group_id() or "").strip(),
            str(self._session_key(event)).strip(),
            str(getattr(event, "unified_msg_origin", "") or "").strip(),
        }
        candidates.discard("")
        if allowed.intersection(candidates):
            return None
        return "当前用户/群组不在 GPT Image 白名单中。"

    @staticmethod
    def _session_key(event: AstrMessageEvent) -> str:
        origin = getattr(event, "unified_msg_origin", None)
        if origin:
            return str(origin)
        return f"{event.get_group_id() or 'private'}:{event.get_sender_id() or 'unknown'}"

    def _payload(self, prompt: str) -> dict[str, Any]:
        output_format = self._setting("output_format", "png", SUPPORTED_FORMATS)
        background = self._setting("background", "auto", SUPPORTED_BACKGROUNDS)
        if background == "transparent" and output_format == "jpeg":
            output_format = "png"

        payload: dict[str, Any] = {
            "model": self._model(),
            "prompt": prompt,
            "n": 1,
            "size": self._setting("size", "1024x1024", SUPPORTED_SIZES),
            "quality": self._setting("quality", "high", SUPPORTED_QUALITIES),
            "output_format": output_format,
            "background": background,
            "moderation": "auto",
        }
        if output_format in {"jpeg", "webp"}:
            payload["output_compression"] = self._bounded_int(
                "output_compression", 90, 0, 100
            )
        return payload

    @staticmethod
    def _safe_api_error(status: int, body: str) -> str:
        message = ""
        try:
            parsed = json.loads(body)
            error = parsed.get("error", {}) if isinstance(parsed, dict) else {}
            if isinstance(error, dict):
                message = str(error.get("message", ""))
            elif error:
                message = str(error)
        except (json.JSONDecodeError, TypeError, ValueError):
            message = body
        message = " ".join(message.split())[:500]
        if status in {401, 403}:
            return f"API 鉴权失败（HTTP {status}），请检查 API Key 和模型权限。"
        if status == 429:
            return "API 请求受限（HTTP 429），请稍后重试或检查额度。"
        if status == 413:
            return "API 拒绝了过大的请求（HTTP 413），请缩短提示词。"
        return f"图像 API 请求失败（HTTP {status}）" + (
            f"：{message}" if message else "。"
        )

    async def _request_image(self, prompt: str) -> tuple[bytes, str]:
        payload = self._payload(prompt)
        timeout_seconds = self._bounded_int("timeout_seconds", 180, 10, 600)
        timeout = aiohttp.ClientTimeout(total=timeout_seconds, connect=30)
        headers = {
            "Authorization": f"Bearer {self._api_key()}",
            "Content-Type": "application/json",
        }

        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._endpoint(), headers=headers, json=payload
                ) as response:
                    body = await response.text()
                    if response.status < 200 or response.status >= 300:
                        raise ImageGenerationError(
                            self._safe_api_error(response.status, body)
                        )
        except ImageGenerationError:
            raise
        except asyncio.TimeoutError as exc:
            raise ImageGenerationError(
                f"图像生成超时（{timeout_seconds} 秒）。"
            ) from exc
        except aiohttp.ClientError as exc:
            raise ImageGenerationError(f"连接图像 API 失败：{type(exc).__name__}") from exc

        try:
            result = json.loads(body)
            item = result["data"][0]
            encoded = item.get("b64_json")
            if not encoded:
                raise KeyError("b64_json")
            image_bytes = base64.b64decode(encoded, validate=True)
        except (json.JSONDecodeError, KeyError, IndexError, TypeError, binascii.Error) as exc:
            raise ImageGenerationError("图像 API 返回格式异常：未找到有效的 Base64 图片。") from exc

        max_bytes = self._bounded_int("max_image_mb", 20, 1, 50) * 1024 * 1024
        if not image_bytes:
            raise ImageGenerationError("图像 API 返回了空图片。")
        if len(image_bytes) > max_bytes:
            raise ImageGenerationError(
                f"生成图片过大（{len(image_bytes) / 1024 / 1024:.1f} MB）。"
            )
        return image_bytes, payload["output_format"]

    async def _request_via_codex_bridge(
        self,
        prompt: str,
        *,
        action: str = "generate",
        input_filenames: list[str] | None = None,
        reference_filenames: list[str] | None = None,
        session_id: str = "",
    ) -> Path:
        socket_path = str(
            self.config.get(
                "codex_bridge_socket",
                "/AstrBot/data/plugin_data/astrbot_plugin_gpt_image/codex_bridge.sock",
            )
            or ""
        ).strip()
        if not socket_path:
            raise ImageGenerationError("未配置 Codex 订阅桥接 Socket。")

        timeout_seconds = self._bounded_int("timeout_seconds", 300, 30, 900)
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(socket_path), timeout=10
            )
        except (OSError, asyncio.TimeoutError) as exc:
            raise ImageGenerationError(
                "无法连接 Codex 订阅生图桥，请检查宿主机桥接服务。"
            ) from exc

        try:
            request = json.dumps(
                {
                    "action": action,
                    "prompt": prompt,
                    "input_filenames": input_filenames or [],
                    "reference_filenames": reference_filenames or [],
                    "session_id": session_id,
                },
                ensure_ascii=False,
            ) + "\n"
            writer.write(request.encode("utf-8"))
            await writer.drain()
            line = await asyncio.wait_for(
                reader.readline(), timeout=timeout_seconds + 30
            )
        except asyncio.TimeoutError as exc:
            raise ImageGenerationError(
                f"Codex 订阅生图超时（{timeout_seconds} 秒）。"
            ) from exc
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, BrokenPipeError):
                pass

        try:
            response = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ImageGenerationError("Codex 订阅桥返回了无效响应。") from exc
        if response.get("status") != "success":
            message = str(response.get("message", "Codex 订阅生图失败。"))[:500]
            raise ImageGenerationError(message)

        filename = str(response.get("filename", ""))
        path = (self._data_dir / filename).resolve()
        data_dir = self._data_dir.resolve()
        if path.parent != data_dir or not path.is_file():
            raise ImageGenerationError("Codex 订阅桥未返回有效图片文件。")
        bridge_session = str(response.get("session_id", "")).strip()
        if bridge_session:
            self._path_codex_sessions[str(path)] = bridge_session
            await asyncio.to_thread(self._save_session_manifest)
        await asyncio.to_thread(self._prune_files)
        return path

    async def _save_image(self, image_bytes: bytes, output_format: str) -> Path:
        extension = "jpg" if output_format == "jpeg" else output_format
        path = self._data_dir / f"{int(time.time())}_{uuid.uuid4().hex[:10]}.{extension}"
        await asyncio.to_thread(path.write_bytes, image_bytes)
        await asyncio.to_thread(self._prune_files)
        return path

    def _prune_files(self) -> None:
        keep = self._bounded_int("keep_generated_files", 20, 1, 500)
        files = sorted(
            (p for p in self._data_dir.iterdir() if p.is_file()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old_file in files[keep:]:
            try:
                old_file.unlink()
            except OSError as exc:
                logger.warning(f"[GPTImage] 清理旧图片失败: {old_file.name}: {exc}")

    @staticmethod
    def _guess_image_suffix(data: bytes) -> str:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if data.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return ".webp"
        raise ImageGenerationError("输入文件不是支持的 PNG、JPEG 或 WebP 图片。")

    async def _reply_image_urls(
        self, event: AstrMessageEvent, reply: Any
    ) -> list[str]:
        bot = getattr(event, "bot", None)
        apis = [bot, getattr(bot, "api", None)]
        if not getattr(reply, "id", None):
            return []
        for api in apis:
            call_action = getattr(api, "call_action", None)
            if not callable(call_action):
                continue
            try:
                response = await call_action("get_msg", message_id=int(reply.id))
                urls: list[str] = []
                for segment in (response or {}).get("message", []):
                    if segment.get("type") == "image":
                        data = segment.get("data", {})
                        value = data.get("url") or data.get("file")
                        if value:
                            urls.append(str(value))
                if urls:
                    return urls
            except Exception as exc:
                logger.debug(f"[GPTImage] 获取引用图片失败: {exc}")
        return []

    @staticmethod
    def _image_component_from_ref(value: str) -> Any:
        if value.startswith(("http://", "https://")):
            return Comp.Image.fromURL(value)
        # OneBot may return a local file URI, absolute path, or a platform file
        # token instead of URL. Image.convert_to_file_path() handles these forms.
        return Comp.Image(file=value)

    async def _message_images(self, event: AstrMessageEvent) -> list[Any]:
        """Collect inline images and images from referenced messages."""
        messages = list(event.get_messages() or [])
        images: list[Any] = []
        seen: set[str] = set()

        def add_image(image: Any) -> None:
            key = ""
            for attr in ("url", "file", "path"):
                value = str(getattr(image, attr, "") or "").strip()
                if value:
                    key = value
                    break
            if not key:
                key = f"component:{id(image)}"
            if key not in seen:
                seen.add(key)
                images.append(image)

        for component in messages:
            if isinstance(component, Comp.Reply):
                for nested in getattr(component, "chain", None) or []:
                    if isinstance(nested, Comp.Image):
                        add_image(nested)
                for url in await self._reply_image_urls(event, component):
                    try:
                        add_image(self._image_component_from_ref(url))
                    except Exception:
                        logger.debug("[GPTImage] 忽略无效引用图片 URL: %s", url)
            elif isinstance(component, Comp.Image):
                add_image(component)
        return images

    async def _find_edit_image(
        self, event: AstrMessageEvent
    ) -> tuple[list[Any], str | None]:
        images = await self._message_images(event)
        if images:
            return images, None

        previous = self._last_generated.get(self._session_key(event))
        if previous and previous.is_file():
            return [Comp.Image.fromFileSystem(previous)], self._path_codex_sessions.get(
                str(previous)
            )
        return [], None

    async def _find_reference_images(self, event: AstrMessageEvent) -> list[Any]:
        return await self._message_images(event)

    async def _materialize_images(
        self, event: AstrMessageEvent,
        components: list[Any],
        *,
        missing_message: str,
        session_id: str | None = None,
    ) -> tuple[list[Path], str | None]:
        if not components:
            raise ImageGenerationError(missing_message)

        max_bytes = self._bounded_int("max_image_mb", 20, 1, 50) * 1024 * 1024
        targets: list[Path] = []
        try:
            for component in components[:8]:
                source_path = Path(await component.convert_to_file_path())
                data = await asyncio.to_thread(source_path.read_bytes)
                if not data or len(data) > max_bytes:
                    raise ImageGenerationError(
                        f"输入图片为空或超过 {max_bytes // 1024 // 1024} MB。"
                    )
                suffix = self._guess_image_suffix(data)
                target = self._input_dir / f"{uuid.uuid4().hex}{suffix}"
                await asyncio.to_thread(target.write_bytes, data)
                targets.append(target)
        except ImageGenerationError:
            for target in targets:
                target.unlink(missing_ok=True)
            raise
        except Exception as exc:
            for target in targets:
                target.unlink(missing_ok=True)
            raise ImageGenerationError("无法读取输入图片，请重新发送或引用图片。") from exc
        return targets, session_id

    def _remember_generated(self, event: AstrMessageEvent, path: Path) -> None:
        self._last_generated[self._session_key(event)] = path

    def _track_background(self, task: asyncio.Task) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    @staticmethod
    async def _send_background_result(
        event: AstrMessageEvent, chain: MessageChain, label: str
    ) -> None:
        try:
            await event.send(chain)
        except Exception:
            logger.exception(f"[GPTImage] 后台{label}结果发送失败")

    async def _run_background_job(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        input_paths: list[Path] | None = None,
        reference_paths: list[Path] | None = None,
        session_id: str | None = None,
    ) -> None:
        action = "edit" if input_paths else "generate"
        try:
            path = await self._generate(
                event,
                prompt,
                enforce_limits=False,
                input_paths=input_paths,
                reference_paths=reference_paths,
                session_id=session_id,
            )
            self._remember_generated(event, path)
            bridge_session = self._path_codex_sessions.get(str(path))
            if bridge_session:
                self._codex_sessions[self._session_key(event)] = bridge_session
            await self._send_background_result(
                event,
                MessageChain([Comp.Image.fromFileSystem(path)]),
                action,
            )
        except asyncio.CancelledError:
            raise
        except ImageGenerationError as exc:
            await self._send_background_result(
                event,
                MessageChain().message(f"❌ GPT Image {action}失败：{exc}"),
                action,
            )
        except Exception as exc:
            logger.exception(f"[GPTImage] 后台{action}任务发生未预期错误")
            await self._send_background_result(
                event,
                MessageChain().message(
                    f"❌ GPT Image {action}失败：内部错误（{type(exc).__name__}）。"
                ),
                action,
            )
        finally:
            for path in (input_paths or []) + (reference_paths or []):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    async def _generate(
        self,
        event: AstrMessageEvent,
        prompt: str,
        *,
        enforce_limits: bool = True,
        input_paths: list[Path] | None = None,
        reference_paths: list[Path] | None = None,
        session_id: str | None = None,
    ) -> Path:
        prompt = self._validate_prompt(prompt)
        if enforce_limits:
            self._check_cooldown(event)
        async with self._semaphore:
            logger.info(
                "[GPTImage] 开始生成: backend=%s model=%s size=%s quality=%s prompt_chars=%d",
                self._backend(),
                self._model(),
                self._setting("size", "1024x1024", SUPPORTED_SIZES),
                self._setting("quality", "high", SUPPORTED_QUALITIES),
                len(prompt),
            )
            if self._backend() == "codex_subscription":
                path = await self._request_via_codex_bridge(
                    prompt,
                    action="edit" if input_paths else "generate",
                    input_filenames=[path.name for path in (input_paths or [])],
                    reference_filenames=[
                        path.name for path in (reference_paths or [])
                    ],
                    session_id=session_id or "",
                )
                logger.info("[GPTImage] Codex 订阅生成完成: file=%s", path.name)
                return path
            if input_paths:
                raise ImageGenerationError("OpenAI API 后端暂未启用改图，请使用订阅后端。")
            if reference_paths:
                raise ImageGenerationError(
                    "OpenAI API 后端暂未启用参考图，请使用 Codex 订阅后端。"
                )
            image_bytes, output_format = await self._request_image(prompt)
            path = await self._save_image(image_bytes, output_format)
            logger.info(
                "[GPTImage] 生成完成: file=%s bytes=%d", path.name, len(image_bytes)
            )
            return path

    @filter.command("画图", alias={"生图", "gpt画图", "gpt_image"})
    async def draw_command(
        self, event: AstrMessageEvent, prompt: GreedyStr = ""
    ):
        """使用 GPT Image 2.5 生成图片。用法：/画图 <描述>"""
        if not bool(self.config.get("enable_command", True)):
            yield event.plain_result("GPT Image 指令已在插件配置中关闭。")
            return
        denial = self._access_denial(event)
        if denial:
            yield event.plain_result(f"❌ {denial}")
            return
        reference_paths: list[Path] | None = None
        try:
            clean_prompt = self._validate_prompt(str(prompt))
            self._check_cooldown(event)
            reference_components = await self._find_reference_images(event)
            reference_paths, _ = await self._materialize_images(
                event,
                reference_components,
                missing_message="",
            ) if reference_components else ([], None)
            task = asyncio.create_task(
                self._run_background_job(
                    event, clean_prompt, reference_paths=reference_paths
                ),
                name="gpt-image-generate",
            )
            self._track_background(task)
            reference_paths = None  # 后台任务接管临时文件生命周期
            yield event.plain_result("🎨 已提交生图任务，完成后会自动发送图片。")
        except ImageGenerationError as exc:
            yield event.plain_result(f"❌ GPT Image 生成失败：{exc}")
        except Exception as exc:
            logger.exception("[GPTImage] 指令生成发生未预期错误")
            yield event.plain_result(
                f"❌ GPT Image 生成失败：内部错误（{type(exc).__name__}）。"
            )
        finally:
            for path in reference_paths or []:
                path.unlink(missing_ok=True)

    @filter.command("改图", alias={"gpt改图", "编辑图片"})
    async def edit_command(
        self, event: AstrMessageEvent, prompt: GreedyStr = ""
    ):
        """修改当前、引用或上一张生成图片。用法：/改图 <修改要求>"""
        if not bool(self.config.get("enable_command", True)):
            yield event.plain_result("GPT Image 指令已在插件配置中关闭。")
            return
        denial = self._access_denial(event)
        if denial:
            yield event.plain_result(f"❌ {denial}")
            return
        input_paths: list[Path] | None = None
        session_id: str | None = None
        try:
            clean_prompt = self._validate_prompt(str(prompt))
            components, session_id = await self._find_edit_image(event)
            input_paths, session_id = await self._materialize_images(
                event,
                components,
                missing_message="没有找到待修改图片。请发送或引用一张图片后再说修改要求。",
                session_id=session_id,
            )
            session_id = session_id or self._codex_sessions.get(self._session_key(event))
            self._check_cooldown(event)
            task = asyncio.create_task(
                self._run_background_job(
                    event,
                    clean_prompt,
                    input_paths=input_paths,
                    session_id=session_id,
                ),
                name="gpt-image-edit",
            )
            self._track_background(task)
            input_paths = None  # 后台任务接管临时文件生命周期
            yield event.plain_result("🖌️ 已提交改图任务，完成后会自动发送图片。")
        except ImageGenerationError as exc:
            yield event.plain_result(f"❌ GPT Image 改图失败：{exc}")
        except Exception as exc:
            logger.exception("[GPTImage] 改图指令发生未预期错误")
            yield event.plain_result(
                f"❌ GPT Image 改图失败：内部错误（{type(exc).__name__}）。"
            )
        finally:
            for path in input_paths or []:
                path.unlink(missing_ok=True)

    @filter.llm_tool(name="generate_gpt_image")
    async def generate_gpt_image(
        self, event: AstrMessageEvent, prompt: str
    ) -> str:
        """Generate and send an image with GPT Image 2.5.

        Use this tool only when the user asks to create, draw, render, or generate an
        image. Pass the user's agreed final request with its proper names, canon
        constraints, wording, and exclusions intact. Do not expand it into a visual
        prompt, substitute characters/vehicles/scenes, invent details, or summarize
        away constraints; Codex performs research, prompt expansion, reference
        reconciliation, and final image generation. The tool accepts the job
        immediately and sends the finished image later. After acceptance, do not call
        it again.

        Args:
            prompt(string): The user's final agreed image request, kept as close to the user's wording as possible. Keep it under 8000 characters.
        """
        if not bool(self.config.get("enable_llm_tool", True)):
            return json.dumps(
                {"status": "error", "message": "GPT Image LLM Tool 已关闭。"},
                ensure_ascii=False,
            )
        denial = self._access_denial(event)
        if denial:
            return json.dumps(
                {"status": "denied", "message": denial}, ensure_ascii=False
            )
        reference_paths: list[Path] | None = None
        try:
            clean_prompt = self._validate_prompt(prompt)
            self._check_cooldown(event)
            session_id = self._codex_sessions.get(self._session_key(event))
            reference_components = await self._find_reference_images(event)
            reference_paths, _ = await self._materialize_images(
                event,
                reference_components,
                missing_message="",
            ) if reference_components else ([], None)
            task = asyncio.create_task(
                self._run_background_job(
                    event,
                    clean_prompt,
                    reference_paths=reference_paths,
                    session_id=session_id,
                ),
                name="gpt-image-generate",
            )
            self._track_background(task)
            reference_paths = None  # 后台任务接管临时文件生命周期
            return json.dumps(
                {
                    "status": "accepted",
                    "message": "生图任务已提交；完成后会自动发送到当前聊天。不要重复调用。",
                    "backend": self._backend(),
                },
                ensure_ascii=False,
            )
        except ImageGenerationError as exc:
            return json.dumps(
                {"status": "error", "message": str(exc)}, ensure_ascii=False
            )
        except Exception as exc:
            logger.exception("[GPTImage] LLM Tool 生成发生未预期错误")
            return json.dumps(
                {
                    "status": "error",
                    "message": f"内部错误（{type(exc).__name__}）。",
                },
                ensure_ascii=False,
            )
        finally:
            for path in reference_paths or []:
                path.unlink(missing_ok=True)

    @filter.llm_tool(name="edit_gpt_image")
    async def edit_gpt_image(
        self, event: AstrMessageEvent, prompt: str
    ) -> str:
        """Edit an image with GPT Image.

        Use this tool when the user asks to change, restyle, retouch, remove from,
        add to, or otherwise edit an existing image. The target is selected from a
        replied image, an image in the current message, or the last image generated
        in this conversation. Pass the user's final editing request and preserve all
        proper names, canon constraints, exact changes, and exclusions. Do not perform
        prompt expansion or replace a named fictional character, vehicle, setting, or
        object; Codex performs research, expansion, reference reconciliation, and
        final editing. The job is accepted immediately and the finished image is sent
        later; do not call the tool again after acceptance.

        Args:
            prompt(string): The user's final agreed editing request, kept close to the user's wording. Keep it under 8000 characters.
        """
        if not bool(self.config.get("enable_llm_tool", True)):
            return json.dumps(
                {"status": "error", "message": "GPT Image LLM Tool 已关闭。"},
                ensure_ascii=False,
            )
        denial = self._access_denial(event)
        if denial:
            return json.dumps(
                {"status": "denied", "message": denial}, ensure_ascii=False
            )
        input_paths: list[Path] | None = None
        session_id: str | None = None
        try:
            clean_prompt = self._validate_prompt(prompt)
            components, session_id = await self._find_edit_image(event)
            input_paths, session_id = await self._materialize_images(
                event,
                components,
                missing_message="没有找到待修改图片。请发送或引用一张图片后再说修改要求。",
                session_id=session_id,
            )
            session_id = session_id or self._codex_sessions.get(self._session_key(event))
            self._check_cooldown(event)
            task = asyncio.create_task(
                self._run_background_job(
                    event,
                    clean_prompt,
                    input_paths=input_paths,
                    session_id=session_id,
                ),
                name="gpt-image-edit",
            )
            self._track_background(task)
            input_paths = None
            return json.dumps(
                {
                    "status": "accepted",
                    "message": "改图任务已提交；完成后会自动发送到当前聊天。不要重复调用。",
                    "backend": self._backend(),
                },
                ensure_ascii=False,
            )
        except ImageGenerationError as exc:
            return json.dumps(
                {"status": "error", "message": str(exc)}, ensure_ascii=False
            )
        except Exception as exc:
            logger.exception("[GPTImage] LLM Tool 改图发生未预期错误")
            return json.dumps(
                {
                    "status": "error",
                    "message": f"内部错误（{type(exc).__name__}）。",
                },
                ensure_ascii=False,
            )
        finally:
            for path in input_paths or []:
                path.unlink(missing_ok=True)

    @filter.command("gpt画图状态")
    async def status_command(self, event: AstrMessageEvent):
        configured = bool(
            str(self.config.get("api_key", "") or "").strip()
            or os.environ.get("OPENAI_API_KEY", "").strip()
        )
        lines = [
            "GPT Image 插件状态",
            f"后端：{'Codex/ChatGPT 订阅' if self._backend() == 'codex_subscription' else 'OpenAI API'}",
            f"API 模型：{self._model()}",
            f"尺寸：{self._setting('size', '1024x1024', SUPPORTED_SIZES)}",
            f"质量：{self._setting('quality', 'high', SUPPORTED_QUALITIES)}",
            f"API Key：{'订阅后端无需配置' if self._backend() == 'codex_subscription' else ('已配置' if configured else '未配置')}",
            f"LLM Tool：{'开启' if self.config.get('enable_llm_tool', True) else '关闭'}",
            "改图：已启用（引用图、当前消息图片或本会话上一张生成图）",
            f"仅管理员：{'开启' if self.config.get('admin_only', False) else '关闭'}",
            f"白名单：{'开启' if self.config.get('whitelist_enabled', False) else '关闭'}",
            f"后台任务：{len(self._background_tasks)}",
        ]
        yield event.plain_result("\n".join(lines))

    async def initialize(self) -> None:
        logger.info(
            "[GPTImage] 插件已加载: backend=%s, model=%s, endpoint=%s",
            self._backend(),
            self._model(),
            self._endpoint(),
        )
