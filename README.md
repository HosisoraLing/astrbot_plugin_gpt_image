# GPT Image 2.5 for AstrBot

> 让 AstrBot 的 Agent 直接调用 Codex 订阅完成检索、提示词扩写、生图与改图。

[![AstrBot](https://img.shields.io/badge/AstrBot-%E2%89%A54.28.1-6366f1?style=flat-square)](https://github.com/Soulter/AstrBot)
[![License: MIT](https://img.shields.io/badge/License-MIT-22c55e?style=flat-square)](LICENSE)
[![Backend](https://img.shields.io/badge/backend-Codex%20%2F%20OpenAI-10a37f?style=flat-square)](#后端)

这是一个面向 AstrBot 的 GPT Image 插件。它支持自然语言触发、引用图片改图、Codex 联网查找参考，以及不受 AstrBot LLM Tool 60 秒限制影响的后台生图。

> **AIGC 说明**：本项目使用 AIGC（人工智能生成内容）辅助完成，但已经过人工测试，确认可以正常使用。

## ✨ 功能

- **Agent 原生调用**：`generate_gpt_image`、`edit_gpt_image`。
- **命令与自然语言**：`/画图`、`/改图`，也可直接说“画一张……”或“把这张图改成……”。
- **多图参考**：生图会收集当前消息和引用消息中的图片；改图时第一张是目标图，其余图片作为参考图传给 Codex。
- **Codex 负责最终创作**：AstrBot 主模型只透传用户确认后的请求；Codex 负责搜索、扩写、参考融合和最终生成。
- **多来源检索门槛**：每次任务要求至少 3 个独立联网搜索查询，并由桥端校验搜索事件。
- **作品一致性**：角色、载具、场景、物件、原作属性、精确文字和排除项原样传给 Codex。
- **Session 续接**：改动之前由 Codex 生成的图片时，继续对应 Codex session；映射持久化。
- **后台任务**：工具立即返回 `accepted`，完成后自动发图，不把 Base64 或图片内容写入上下文。
- **双后端**：默认 ChatGPT/Codex 订阅，也可切换 OpenAI Images API。

## 🧭 工作流

```text
用户需求
   │
   ▼
AstrBot Agent：讨论并确认，原样透传最终请求
   │
   ▼
Codex：联网查询 ≥3 个参考 → 内部扩写/校对 → imagegen
   │
   ▼
后台桥接服务 → AstrBot → 当前会话自动收到图片
```

---

## 👤 人类版教程

### 1. 安装插件

将整个目录复制到 AstrBot 的插件目录：

```bash
git clone https://github.com/HosisoraLing/astrbot_plugin_gpt_image.git \
  /AstrBot/data/plugins/astrbot_plugin_gpt_image
```

在 AstrBot WebUI 中启用插件并重启 AstrBot。

### 2. 推荐：配置 Codex 订阅后端

在**宿主机**安装并登录 Codex CLI：

```bash
codex login
codex login status
```

编辑 `bridge/astrbot-codex-image-bridge.service` 中的这三个路径：

```ini
Environment=ASTRBOT_GPT_IMAGE_DATA_DIR=/你的AstrBot目录/data/plugin_data/astrbot_plugin_gpt_image
Environment=CODEX_BIN=/你的Codex路径/codex
Environment=CODEX_HOME=/你的Codex配置目录
```

安装并启动桥接服务：

```bash
sudo cp bridge/astrbot-codex-image-bridge.service \
  /etc/systemd/system/astrbot-codex-image-bridge.service
sudo systemctl daemon-reload
sudo systemctl enable --now astrbot-codex-image-bridge.service
```

Docker 场景必须把宿主机的插件数据目录挂载到容器内：

```text
/宿主机/data/plugin_data/astrbot_plugin_gpt_image
    → /AstrBot/data/plugin_data/astrbot_plugin_gpt_image
```

AstrBot 插件配置保持：

```text
backend = codex_subscription
codex_bridge_socket = /AstrBot/data/plugin_data/astrbot_plugin_gpt_image/codex_bridge.sock
```

不需要把 Codex OAuth 凭据挂载进 AstrBot 容器；凭据只由宿主机桥接服务使用。

### 3. 使用方法

```text
/画图 雨夜里的机械猫
/改图 把背景改成雪夜，人物和构图保持不变
```

自然语言也可以：

```text
画一张《作品名》中的某角色驾驶指定载具穿过雨夜城市。
```

如果是改图，请直接附图或引用图片；附多张图时第一张是目标图，其余是参考图：

```text
把这张图里的白天改成黄昏，人物、服装和构图保持不变。
```

```text
参考第二张图的色彩和第三张图的服装，把第一张目标图改成黄昏场景。
```

改图目标的选择顺序是：引用消息图片 → 当前消息图片 → 本会话上一张插件生成图片。

### 4. 重要行为

- 工具返回“已受理”不代表图片已经完成；完成后会自动发送。
- 不要因为暂时没有图片就重复调用。
- Codex 会先搜索至少 3 个参考，再扩写和生成，因此一次任务可能需要一到数分钟。
- 作品角色、载具、场景等名称不要改写成泛化描述。

### 5. OpenAI API 后端

将 `backend` 改为 `openai_api`，填写 `api_key` 或设置 `OPENAI_API_KEY`。默认地址：

```text
https://api.openai.com/v1
```

兼容中转必须实现 `POST /v1/images/generations`，并返回 `data[0].b64_json`。

---

## 🤖 Agent / 开发者版教程

### Agent 部署流程

以下流程适用于“宿主机运行 Codex CLI + Docker 运行 AstrBot”的部署方式。桥接服务必须运行在能访问 Codex OAuth 凭据的宿主机上，不能把 OAuth 目录直接暴露给 AstrBot 容器。

#### 1. 准备目录与代码

```bash
export ASTRBOT_ROOT=/opt/astrbot
git clone https://github.com/HosisoraLing/astrbot_plugin_gpt_image.git \
  "$ASTRBOT_ROOT/data/plugins/astrbot_plugin_gpt_image"
mkdir -p "$ASTRBOT_ROOT/data/plugin_data/astrbot_plugin_gpt_image"
```

如果 AstrBot 已经安装，只需把插件目录复制到现有的 `data/plugins/` 下。

#### 2. 安装并验证 Codex CLI

在宿主机安装官方 Codex CLI，并使用拥有 ChatGPT/Codex 订阅的账号登录：

```bash
codex login
codex login status
codex --version
```

确认 `codex login status` 显示已登录。`CODEX_HOME` 目录应由运行桥接服务的同一用户可读。

#### 3. 配置桥接服务

复制 unit 文件后，修改其中的三个环境变量。下面示例假设 Codex 安装在 `/root/.local/bin/codex`：

```bash
sudo cp "$ASTRBOT_ROOT/data/plugins/astrbot_plugin_gpt_image/bridge/astrbot-codex-image-bridge.service" \
  /etc/systemd/system/astrbot-codex-image-bridge.service
sudoedit /etc/systemd/system/astrbot-codex-image-bridge.service
```

```ini
Environment=ASTRBOT_GPT_IMAGE_DATA_DIR=/opt/astrbot/data/plugin_data/astrbot_plugin_gpt_image
Environment=CODEX_BIN=/root/.local/bin/codex
Environment=CODEX_HOME=/root/.codex
ExecStart=/usr/bin/python3 /opt/astrbot/data/plugins/astrbot_plugin_gpt_image/bridge/codex_image_bridge.py
```

如果 Codex 和 AstrBot 不在 `/root` 或 `/opt/astrbot`，必须替换为实际路径。然后启动：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now astrbot-codex-image-bridge.service
sudo systemctl status astrbot-codex-image-bridge.service
```

服务启动后应生成以下 socket：

```text
/opt/astrbot/data/plugin_data/astrbot_plugin_gpt_image/codex_bridge.sock
```

#### 4. 配置 Docker 挂载

宿主机的插件数据目录必须和容器内路径对应。Docker Compose 示例：

```yaml
services:
  astrbot:
    volumes:
      - /opt/astrbot/data:/AstrBot/data
```

至少要保证下面这组路径一致：

```text
宿主机：/opt/astrbot/data/plugin_data/astrbot_plugin_gpt_image
容器内：/AstrBot/data/plugin_data/astrbot_plugin_gpt_image
```

然后在插件配置中使用容器内 socket 路径：

```json
{
  "backend": "codex_subscription",
  "codex_bridge_socket": "/AstrBot/data/plugin_data/astrbot_plugin_gpt_image/codex_bridge.sock",
  "enable_command": true,
  "enable_llm_tool": true
}
```

重启 AstrBot：

```bash
docker compose restart astrbot
```

#### 5. 端到端验证

按以下顺序检查：

```bash
# 宿主机：socket 和桥接服务
test -S "$ASTRBOT_ROOT/data/plugin_data/astrbot_plugin_gpt_image/codex_bridge.sock"
systemctl is-active astrbot-codex-image-bridge.service

# 容器：必须能看到同一个 socket
docker exec astrbot test -S /AstrBot/data/plugin_data/astrbot_plugin_gpt_image/codex_bridge.sock

# AstrBot：确认两个工具注册
docker compose logs --since=2m astrbot | grep -E 'generate_gpt_image|edit_gpt_image'
```

日志中应出现 `Added llm tool: generate_gpt_image` 和 `Added llm tool: edit_gpt_image`。完成后在聊天中发送一条简单的“画一张……”进行首次验证；Codex 搜索和生成可能需要几分钟。

#### 6. 升级与回滚

升级前先备份插件数据中的 session 映射和生成图片：

```bash
cp -a "$ASTRBOT_ROOT/data/plugin_data/astrbot_plugin_gpt_image" \
  "$ASTRBOT_ROOT/data/plugin_data/astrbot_plugin_gpt_image.backup"
git -C "$ASTRBOT_ROOT/data/plugins/astrbot_plugin_gpt_image" pull --ff-only
python -m py_compile \
  "$ASTRBOT_ROOT/data/plugins/astrbot_plugin_gpt_image/main.py" \
  "$ASTRBOT_ROOT/data/plugins/astrbot_plugin_gpt_image/bridge/codex_image_bridge.py"
sudo systemctl restart astrbot-codex-image-bridge.service
docker compose restart astrbot
```

如果升级后异常，使用 `git log --oneline` 找到上一版本并执行 `git checkout <commit>`，然后重新执行语法检查和两个服务的重启。不要删除 `codex_sessions.json`，否则旧图片无法继续对应 Codex session。

### 工具契约

#### `generate_gpt_image`

适用于创建新图。`prompt` 应该是**用户讨论后确认的最终请求**，而不是由 AstrBot 主模型自行扩写的完整视觉 prompt。

正确：

```json
{"prompt":"画一张《作品名》中的角色驾驶指定载具穿过雨夜城市。"}
```

不要做这些事：

- 把角色名替换成“一个类似的角色”；
- 把载具名替换成泛化类别；
- 擅自增加剧情、服装、镜头或画风；
- 丢弃用户指定的文字、构图或排除项。

#### `edit_gpt_image`

适用于修改现有图片。`prompt` 只传用户确认的修改要求，并保留“哪些内容不变”的约束。插件会收集引用消息和当前消息中的所有图片：第一张作为目标，其余作为参考，复制到安全输入目录后通过 Codex 的多图输入传递。

`generate_gpt_image` 同样会把用户附带或引用的图片作为参考图传给 Codex；没有图片时才是纯文本生图。

### Codex 侧职责

桥接服务会调用：

```text
codex ... --search --enable image_generation exec ...
```

Codex 收到 `<visual_request>` 后必须：

1. 使用联网搜索查询至少 3 个独立参考；
2. 处理参考之间的冲突和不确定性；
3. 在 Codex 内部扩写视觉 prompt；
4. 保留用户指定的专有名词、原作属性、精确文字和排除项；
5. 调用内置 imagegen；
6. 在任务目录写出唯一的 `result.png`、`result.jpg`、`result.jpeg` 或 `result.webp`。

桥端读取 JSONL 中的 `web_search` 事件；有效查询少于 3 个时任务失败，不保存图片。

### Session 续接

新任务完成后，桥端从 `thread.started` 事件提取 session ID。插件把图片文件名和 session ID 写入：

```text
data/plugin_data/astrbot_plugin_gpt_image/codex_sessions.json
```

当用户修改本会话之前由 Codex 生成的图片时，插件发送该 session ID，桥端执行：

```text
codex ... exec resume <SESSION_ID> ...
```

旧版曾使用 `--ephemeral` 的图片没有可续接 session；从当前版本开始生成的图片才具备完整续接能力。

### 后台与超时

LLM Tool 只创建后台 `asyncio.Task` 并立即返回：

```json
{"status":"accepted","message":"生图任务已提交；完成后会自动发送到当前聊天。不要重复调用。"}
```

不要在工具调用中等待 Codex 完成，否则会再次触发 AstrBot 60 秒工具超时。

### 错误分类

桥端会将错误映射为：

- 额度、`429`、`usage limit`：订阅额度或速率限制；
- `401`、`403`、登录失效：执行 `codex login`；
- 搜索不足：明确显示实际搜索数量；
- 其他错误：提示查看宿主机桥接日志。

### 本地验证

```bash
python -m py_compile main.py bridge/codex_image_bridge.py
systemctl status astrbot-codex-image-bridge.service
journalctl -u astrbot-codex-image-bridge.service -f
```

---

## ⚙️ 配置速览

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `backend` | `codex_subscription` | `codex_subscription` 或 `openai_api` |
| `model` | `gpt-image-2.5-sunburst` | API 后端使用的 GPT Image 2.5 模型 |
| `size` | `1024x1024` | `auto`、横图或竖图 |
| `quality` | `high` | 质量与耗时/额度的权衡 |
| `max_concurrency` | `1` | 同时运行的任务数 |
| `cooldown_seconds` | `10` | 单用户冷却时间 |
| `timeout_seconds` | `180` | API/桥接请求上限 |
| `keep_generated_files` | `20` | 本地保留的最近图片数 |

完整配置定义见 [`_conf_schema.json`](_conf_schema.json)。

## 🔒 安全与隐私

- 不要提交 API Key、OAuth 凭据、socket、生成图片、`codex_sessions.json` 或本地配置。
- 桥接 socket 默认权限为 `0600`，只允许指定本机用户访问。
- 用户提示词会发送给 Codex 及其联网搜索服务；请自行判断敏感信息是否适合发送。

## 📄 许可证

MIT，见 [`LICENSE`](LICENSE)。
