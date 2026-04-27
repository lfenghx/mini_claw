import re
import json
import os
import time
import uuid
import base64
import hashlib
from datetime import datetime, timedelta, timezone as _dt_timezone
from collections.abc import Generator
from typing import Any

from utils.tools import (
    _build_prompt_message_tools,
    _download_file_content,
    _extract_url_and_name,
    _guess_mime_type,
    _infer_ext_from_url,
    _is_allow_reply,
    _is_deny_reply,
    _list_dir,
    _parse_tool_call,
    _safe_filename,
    _safe_get,
    _safe_join,
    _shorten_text,
    _split_message_content,
 )

from utils.mini_skill_debug import _dbg, _model_brief
from utils.mini_skill_exec import _cleanup_old_temp_sessions, _detect_skills_root
from utils.mini_skill_runtime import _AgentRuntime
from utils.mini_skill_schemas import TOOL_SCHEMAS, _tool_call_retry_prompt, _validate_tool_arguments
from utils.mini_skill_storage import (
    _append_history_turn,
    _get_approval_storage_key,
    _get_conversation_approval_storage_key,
    _get_history_storage_key,
    _get_memory_storage_key,
    _get_persona_storage_key,
    _get_user_memory_storage_key_for,
    _get_user_persona_storage_key_for,
    _get_session_dir_storage_key,
    _storage_get_json,
    _storage_get_text,
    _storage_set_json,
    _storage_set_text,
)
from utils.mini_skill_uploads import _build_uploads_context
from utils.mini_skill_prompt import build_system_prompt_content
from utils.mini_skill_hooks import DailyWriteContext, MemoryWriteContext, filter_memory_write, should_write_daily
from utils.mini_skill_usage import LLMUsageAccumulator
from utils.mini_skill_exec_grants import (
    add_allow_entry,
    build_exec_override_from_grants,
    parse_exec_approval_reply,
)
from utils.mini_skill_stream import stream_text_to_user
from utils.mini_skill_assets import persist_llm_assets, redact_user_visible_text
from utils.mini_skill_agent_header import build_agent_tag_header

from dify_plugin import Tool
from dify_plugin.entities.model.message import (
    AssistantPromptMessage,
    PromptMessageTool,
    SystemPromptMessage,
    ToolPromptMessage,
    UserPromptMessage,
)
from dify_plugin.entities.tool import ToolInvokeMessage
from utils.mini_skill_memory import _append_daily_dialogue, _dt_beijing, _reset_role


class SkillAgentTool(Tool):
    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        model = tool_parameters.get("model")
        query = tool_parameters.get("query")
        timeout_seconds = int(tool_parameters.get("timeout_seconds") or 120)
        compaction_max_prompt_tokens = int(tool_parameters.get("compaction_max_prompt_tokens") or 12000)
        loop_detection = bool(tool_parameters.get("loop_detection") if tool_parameters.get("loop_detection") is not None else True)
        exec_approval_enabled = bool(
            tool_parameters.get("exec_approval_enabled")
            if tool_parameters.get("exec_approval_enabled") is not None
            else True
        )
        show_usage_text = bool(
            tool_parameters.get("show_usage_text")
            if tool_parameters.get("show_usage_text") is not None
            else False
        )

        memory_turns = int(tool_parameters.get("memory_turns") or 12)
        system_prompt = tool_parameters.get("system_prompt") or "你是一个xxxx"
        skills_root = _detect_skills_root(tool_parameters.get("skills_root"))
        usage = LLMUsageAccumulator()

        if not query or not isinstance(query, str):
            payload = usage.payload()
            yield self.create_variable_message("llm_usage", payload)
            if show_usage_text:
                yield self.create_text_message(usage.format_text(payload))
            yield self.create_text_message("❌缺少 query 参数\n")
            return
        user_input = str(query)
        started_at = time.time()
        max_tool_turns = 50
        effective_query = str(query)

        def invoke_llm_text_simple(prompt_messages: list[Any]) -> str:
            try:
                try:
                    try:
                        resp = self.session.model.llm.invoke(
                            model_config=model,
                            prompt_messages=prompt_messages,
                            stream=False,
                        )
                    except Exception as e:
                        msg = str(e)
                        if ("incremental_output" in msg) and ("True" in msg):
                            resp = self.session.model.llm.invoke(
                                model_config=model,
                                prompt_messages=prompt_messages,
                            )
                        else:
                            raise
                except TypeError:
                    resp = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=prompt_messages,
                    )
            except Exception as e:
                _dbg(f"onboarding_extract_invoke_failed err={_shorten_text(str(e), 500)}")
                return ""
            usage.record_response(resp)

            if _safe_get(resp, "message") is not None:
                msg = _safe_get(resp, "message") or {}
                content = _safe_get(msg, "content")
                text, _parts = _split_message_content(content)
                return str(text or "").strip()

            if isinstance(resp, str):
                return resp.strip()

            chunks: list[str] = []
            try:
                for chunk in resp:
                    usage.record_chunk(chunk)
                    delta = _safe_get(chunk, "delta") or {}
                    msg = _safe_get(delta, "message") or {}
                    content = _safe_get(msg, "content")
                    t, _parts = _split_message_content(content)
                    if t:
                        chunks.append(str(t))
            except Exception:
                pass
            return "".join(chunks).strip()

        storage = self.session.storage
        history_key = _get_history_storage_key(self.session)
        session_dir_key = _get_session_dir_storage_key(self.session)
        approval_pending_key = _get_conversation_approval_storage_key(self.session, "pending")
        approval_grants_key = _get_approval_storage_key(self.session, "grants")
        approval_state = _storage_get_json(storage, approval_pending_key)
        grants = _storage_get_json(storage, approval_grants_key)
        approval_kind = str(approval_state.get("kind") or "").strip()
        approval_pending = bool(approval_state.get("pending"))
        approval_just_granted = False
        install_granted = bool(grants.get("install") is True)

        plugin_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        temp_root = os.path.join(plugin_root, "temp")
        os.makedirs(temp_root, exist_ok=True)
        persisted_session_dir = _storage_get_text(storage, session_dir_key).strip()
        if persisted_session_dir and os.path.isdir(persisted_session_dir):
            session_dir = persisted_session_dir
        else:
            session_dir = os.path.join(temp_root, f"dify-skill-{uuid.uuid4().hex[:8]}-")
        approval_context = ""
        exec_once_allowed: dict[str, Any] | None = None

        if approval_pending:
            if approval_kind == "install":
                if _is_deny_reply(user_input):
                    _storage_set_json(storage, approval_pending_key, None)
                    yield self.create_text_message("🤝已收到你的拒绝，本次不会执行需要审批的操作。\n")
                    return
                if _is_allow_reply(user_input):
                    next_grants = dict(grants)
                    next_grants["install"] = True
                    _storage_set_json(storage, approval_grants_key, next_grants)
                    _storage_set_json(storage, approval_pending_key, None)
                    approval_just_granted = True
                    install_granted = True
                    original_query = str(approval_state.get("original_query") or "").strip()
                    if original_query:
                        effective_query = original_query + "\n\n[用户已审批]\n允许执行需要审批的操作。"
                    approval_context = "\n\n[审批结果]\n用户已同意执行上一轮需要审批的操作，请继续推进原任务。\n"
                else:
                    prompt = "需要你确认后才能继续执行相关操作。请回复“允许”或“拒绝”。"
                    yield self.create_text_message(prompt)
                    return
            else:
                if not exec_approval_enabled:
                    original_query = str(approval_state.get("original_query") or "").strip()
                    _storage_set_json(storage, approval_pending_key, None)
                    if original_query:
                        effective_query = original_query + "\n\n[用户已审批]\n已关闭执行审批，自动放行上一轮命令。"
                    exec_once_allowed = approval_state if isinstance(approval_state, dict) else None
                    approval_context = "\n\n[审批结果]\n已关闭执行审批：自动放行上一轮命令。\n"
                else:
                    decision = parse_exec_approval_reply(user_input)
                    if decision == "deny":
                        _storage_set_json(storage, approval_pending_key, None)
                        yield self.create_text_message("🤝已收到你的拒绝，本次不会执行需要审批的操作。\n")
                        return
                    if decision in {"once", "always"}:
                        next_grants = dict(grants)
                        exe0 = str(approval_state.get("exe") or "").strip()
                        resolved_path = str(approval_state.get("resolved_exe") or approval_state.get("path") or "").strip()
                        cmd = approval_state.get("command") if isinstance(approval_state.get("command"), list) else []

                        if decision == "always":
                            add_allow_entry(
                                store=next_grants,
                                scope="always",
                                exe=exe0,
                                pattern="*",
                                skill_name=None,
                                command=cmd,
                            )
                            _storage_set_json(storage, approval_grants_key, next_grants)

                        _storage_set_json(storage, approval_pending_key, None)
                        approval_just_granted = True
                        original_query = str(approval_state.get("original_query") or "").strip()
                        if original_query:
                            effective_query = original_query + "\n\n[用户已审批]\n允许执行需要审批的操作。"
                        exec_once_allowed = approval_state if isinstance(approval_state, dict) else None

                        decision_label = {
                            "once": "允许一次",
                            "always": "总是允许",
                        }.get(decision, "允许")
                        approval_context = (
                            "\n\n[审批结果]\n"
                            + f"用户选择：{decision_label}。\n"
                            + "请继续推进原任务并重试上一轮被拦截的命令。\n"
                            + "命令："
                            + json.dumps(cmd or [], ensure_ascii=False)
                            + ("\n可执行文件：" + resolved_path if resolved_path else "")
                            + "\n"
                        )
                    else:
                        prompt = (
                            "需要你确认后才能继续执行相关命令。请回复序号：\n"
                            "1) 允许一次（仅本次）\n"
                            "2) 总是允许（后续不再提示）\n"
                            "3) 拒绝\n"
                        )
                        yield self.create_text_message(prompt)
                        return

        onboarding_key = _get_persona_storage_key(self.session, "onboarding")
        identity_key = _get_persona_storage_key(self.session, "IDENTITY.md")
        user_id = str(getattr(self.runtime, "user_id", None) or "").strip() or "global_user"
        users_index_key = _get_persona_storage_key(self.session, "users_index")
        # user_key = _get_user_persona_storage_key_for(self.session, user_id, "USER.md")
        user_key = 'skill:persona:global_user:USER.md'
        user_onboarding_key = _get_user_persona_storage_key_for(self.session, user_id, "user_onboarding")
        soul_key = _get_persona_storage_key(self.session, "SOUL.md")
        memory_key = _get_user_memory_storage_key_for(self.session, user_id, "MEMORY.md")
        identity_md = _storage_get_text(storage, identity_key).strip()
        onboarding_state = _storage_get_json(storage, onboarding_key)
        onboarding_completed = bool(onboarding_state.get("completed")) and bool(identity_md)
        user_md = _storage_get_text(storage, user_key).strip()

        # reset_words = ["重置身份", "重置设定", "重置角色", "重新初始化", "重新认识", "重做初始化", "换个设定", "改角色", "改设定"]
        reset_words = ["重置角色"]
        if any(w in str(user_input or "") for w in reset_words):
            _reset_role(
                storage=storage,
                session=self.session,
                onboarding_key=onboarding_key,
                identity_key=identity_key,
                user_key=user_key,
                soul_key=soul_key,
                memory_key=memory_key,
                users_index_key=users_index_key,
                keep_daily_days=30,
            )
            yield self.create_text_message("角色重置成功：已清除身份设定与记忆信息，你可通过发送消息再次建立你的专属AI助手。\n")
            return

        if onboarding_completed and not user_md:
            user_onboarding_state = _storage_get_json(storage, user_onboarding_key)
            if not bool(user_onboarding_state.get("pending")):
                _storage_set_json(
                    storage,
                    user_onboarding_key,
                    {"pending": True, "stage": 1, "created_at": int(time.time())},
                )

                def _pick_identity_field(md: str, field: str) -> str:
                    m = re.search(rf"-\s*\*\*{re.escape(field)}:\*\*\s*(.+)", md or "")
                    return str(m.group(1) or "").strip() if m else ""

                agent_name = _pick_identity_field(identity_md, "Name") or "小元"
                agent_creature = _pick_identity_field(identity_md, "Creature") or "AI 助手"
                agent_vibe = _pick_identity_field(identity_md, "Vibe") or "温柔、幽默、靠谱"
                agent_emoji = _pick_identity_field(identity_md, "Emoji") or "🤖"
                yield self.create_text_message(
                    f"你好哇，新朋友！我是 {agent_name}{agent_emoji}，一个{agent_creature}，风格是「{agent_vibe}」。\n"
                    "请问我该怎么称呼你呢？\n"
                    "（请只回复你的称呼，例如：老板 / 张总 / 小王 / Lily）\n"
                )
                return

            extractor_system = SystemPromptMessage(
                content=(
                    "你是信息提取器。请从用户回答中提取用户信息，输出严格 JSON。\n"
                    "输出必须是 JSON 对象本体：不要 Markdown、不要代码块、不要解释、不要多余字符。\n"
                    "必须包含所有字段；缺失时用空字符串。\n"
                    "\n"
                    'JSON schema: {"user":{"name": string, "addressing": string}}\n'
                    "\n"
                    "字段映射规则：\n"
                    "- user.name：用户的姓名/名字（例如“我叫张三/我是张三”）。\n"
                    "- user.addressing：用户希望你怎么称呼TA（例如“叫我老板/以后叫我老板/称呼我老板”）。\n"
                    "- 如果用户只回复一个词（例如“老王/Lily/老板”），优先把它当作 user.addressing。\n"
                    "\n"
                    '示例：输入："叫我老板" 输出：{"user":{"name":"","addressing":"老板"}}\n'
                    '示例：输入："我叫张三" 输出：{"user":{"name":"张三","addressing":""}}\n'
                    '示例：输入："老王" 输出：{"user":{"name":"","addressing":"老王"}}\n'
                )
            )
            extracted = invoke_llm_text_simple([extractor_system, UserPromptMessage(content=user_input)])
            json_text = ""
            if extracted:
                a = extracted.find("{")
                b = extracted.rfind("}")
                if a != -1 and b != -1 and b > a:
                    json_text = extracted[a : b + 1]
            parsed: dict[str, Any] = {}
            if json_text:
                try:
                    obj = json.loads(json_text)
                    parsed = obj if isinstance(obj, dict) else {}
                except Exception:
                    parsed = {}
            user_obj = parsed.get("user") if isinstance(parsed.get("user"), dict) else {}
            user_name = str(user_obj.get("name") or "").strip()
            user_addressing = str(user_obj.get("addressing") or "").strip()
            if not user_addressing:
                s_in = str(user_input or "").strip()
                if s_in and re.fullmatch(r"[^\s，。；;!\n]{1,20}", s_in):
                    user_addressing = s_in
            if not user_addressing and not user_name:
                yield self.create_text_message("我还没听清楚～请只回复你希望我怎么称呼你（例如：老板 / 张总 / 小王 / Lily）。\n")
                return

            user_md_next = (
                "# USER.md - Who Are You?\n\n"
                f"- **Name:** {user_name}\n"
                f"- **Addressing:** {user_addressing}\n"
            )
            _storage_set_text(storage, user_key, user_md_next)
            idx = _storage_get_json(storage, users_index_key)
            users = idx.get("users") if isinstance(idx.get("users"), list) else []
            users2: list[str] = [str(x) for x in users if isinstance(x, str) and str(x).strip()]
            if user_id not in users2:
                users2.append(user_id)
            _storage_set_json(storage, users_index_key, {"users": users2})
            _storage_set_json(storage, user_onboarding_key, None)
            yield self.create_text_message(f"✅好的，我会称呼你为「{user_addressing}」。\n")
            return

        if not onboarding_completed:
            stage = int(onboarding_state.get("stage") or 0)
            if stage <= 0:
                _storage_set_json(
                    storage,
                    onboarding_key,
                    {
                        "stage": 1,
                        "completed": False,
                        "created_at": int(time.time()),
                    },
                )
                yield self.create_text_message(
                    "你好哇😘，我是你的 AI 助手，基于Dify的Mini Skill。\n"
                    "我们先来认识一下吧。\n\n"
                    "请用一段话回答下面问题（按你喜欢的方式写即可）：\n"
                    "1) 你希望我叫什么名字？\n"
                    "2) 我应该怎么称呼你？\n"
                    "3) 你希望我是什么设定/生物？（例如：AI 助手/小动物/程序精灵/赛博管家…）\n"
                    "4) 你希望我的风格是什么？（例如：克制直接/温暖幽默/严谨专业…）\n"
                    "5) 给我设定一个签名 Emoji（可选）\n"
                    "\n"
                    "回答完这一次后，我会把这些信息写入我的身份设定，并从下一轮开始按这个风格交流。"
                )
                return

            extractor_system = SystemPromptMessage(
                content=(
                    "你是信息提取器。请从用户回答中提取身份与偏好，输出严格 JSON。\n"
                    "输出必须是 JSON 对象本体：不要 Markdown、不要代码块、不要解释、不要多余字符。\n"
                    "必须包含所有字段；缺失时用空字符串。\n"
                    "\n"
                    "JSON schema:\n"
                    "{\n"
                    '  "user": {"name": string, "addressing": string},\n'
                    '  "agent": {"name": string, "creature": string, "vibe": string, "emoji": string}\n'
                    "}\n"
                    "\n"
                    "字段映射规则：\n"
                    "- user.name：用户的姓名/名字（例如“我叫张三/我是张三”）。\n"
                    "- user.addressing：用户希望你怎么称呼TA（例如“叫我X/以后叫我X/称呼我X”）。\n"
                    "- agent.name：用户希望你叫什么名字（例如“你叫X/你是X”）。\n"
                    "- agent.creature：设定/生物/身份（例如“AI 助手/赛博管家/程序精灵/小孩/孩子”）。\n"
                    "- agent.vibe：性格/语气/风格/人设，用原话短句归纳（例如“严谨克制”“像孩子一样天真无邪、对一切充满好奇”）。\n"
                    "- agent.emoji：签名/表情/标签/emoji（优先取 1 个字符，如“❓/🤖/😘”；多个时取第一个）。\n"
                    "\n"
                    "示例（仅示例，不要照抄）：\n"
                    '输入："你叫小元，我是你哥哥，你要像孩子一样天真无邪，对一切充满好奇，你的标签是❓"\n'
                    '输出：{"user":{"name":"","addressing":"哥哥"},"agent":{"name":"小元","creature":"小孩","vibe":"像孩子一样天真无邪、对一切充满好奇","emoji":"❓"}}\n'
                    '输入："叫我老板；你叫小元；你是赛博管家；语气严谨克制；emoji=🤖"\n'
                    '输出：{"user":{"name":"","addressing":"老板"},"agent":{"name":"小元","creature":"赛博管家","vibe":"严谨克制","emoji":"🤖"}}\n'
                    "\n"
                    "注意：如果用户用“性格/风格/气质/人设”等描述（例如“性格善良、乖巧、聪明”），应写入 agent.vibe。\n"
                )
            )
            extractor_user = UserPromptMessage(content=user_input)
            extracted = invoke_llm_text_simple([extractor_system, extractor_user])
            _dbg(f"onboarding_extract_raw={_shorten_text(extracted, 1200)}")
            json_text = ""
            if extracted:
                a = extracted.find("{")
                b = extracted.rfind("}")
                if a != -1 and b != -1 and b > a:
                    json_text = extracted[a : b + 1]
            _dbg(f"onboarding_extract_json_candidate={_shorten_text(json_text, 1200)}")
            parsed: dict[str, Any] = {}
            if json_text:
                try:
                    obj = json.loads(json_text)
                    parsed = obj if isinstance(obj, dict) else {}
                except Exception as e:
                    _dbg(f"onboarding_extract_json_load_failed err={_shorten_text(str(e), 500)}")
                    parsed = {}

            if not parsed:
                yield self.create_text_message(
                    "❌身份设定解析失败：未能从你的输入中解析出 JSON。\n"
                    "请按下面格式重新填写（建议一行一个字段）：\n"
                    "1) 你希望我叫什么名字：<助手名>\n"
                    "2) 我应该怎么称呼你：<你的称呼>\n"
                    "3) 你希望我是什么设定/生物：<例如 AI 助手/赛博管家>\n"
                    "4) 你希望我的风格/性格：<例如 善良、乖巧、聪明>\n"
                    "5) 签名 Emoji（可选）：<例如 😘>\n"
                )
                return

            user_obj = parsed.get("user") if isinstance(parsed.get("user"), dict) else {}
            agent_obj = parsed.get("agent") if isinstance(parsed.get("agent"), dict) else {}
            user_name = str(user_obj.get("name") or "").strip()
            user_addressing = str(user_obj.get("addressing") or "").strip()
            agent_name = str(agent_obj.get("name") or "").strip()
            agent_creature = str(agent_obj.get("creature") or "").strip()
            agent_vibe = str(agent_obj.get("vibe") or "").strip()
            agent_emoji = str(agent_obj.get("emoji") or "").strip()

            if not agent_name or not user_addressing:
                yield self.create_text_message(
                    "❌身份设定解析失败：缺少必要字段（助手名/你的称呼）。\n"
                    "请按下面格式重新填写（建议一行一个字段）：\n"
                    "1) 你希望我叫什么名字：<助手名>\n"
                    "2) 我应该怎么称呼你：<你的称呼>\n"
                    "3) 你希望我是什么设定/生物：<例如 AI 助手/赛博管家>\n"
                    "4) 你希望我的风格/性格：<例如 善良、乖巧、聪明>\n"
                    "5) 签名 Emoji（可选）：<例如 😘>\n"
                )
                return

            identity_md_next = (
                "# IDENTITY.md - Who Am I?\n\n"
                f"- **Name:** {agent_name}\n"
                f"- **Creature:** {agent_creature}\n"
                f"- **Vibe:** {agent_vibe}\n"
                f"- **Emoji:** {agent_emoji}\n"
            )
            user_md_next = (
                "# USER.md - Who Are You?\n\n"
                f"- **Name:** {user_name}\n"
                f"- **Addressing:** {user_addressing}\n"
            )
            soul_md_next = (
                "# SOUL.md - Who You Are\n\n"
                "## Core\n"
                "- 说人话，少模板；可以小调皮，但不油腻。\n"
                "- 语气不要刻意迎合，但要给与用户足够的尊重。\n"
                "- 有主见，但不自作主张，遇到不确定的事情时，会向用户询问。\n"
                "- 幽默是你的底色，善良是你的天性，你会主动关心用户。\n"
                "## Vibe\n"
                f"{agent_vibe}\n"
            )

            _storage_set_text(storage, identity_key, identity_md_next)
            _storage_set_text(storage, user_key, user_md_next)
            _storage_set_text(storage, soul_key, soul_md_next)
            idx = _storage_get_json(storage, users_index_key)
            users = idx.get("users") if isinstance(idx.get("users"), list) else []
            users2: list[str] = [str(x) for x in users if isinstance(x, str) and str(x).strip()]
            if user_id not in users2:
                users2.append(user_id)
            _storage_set_json(storage, users_index_key, {"users": users2})
            if not _storage_get_text(storage, memory_key).strip():
                _storage_set_text(storage, memory_key, "# MEMORY.md - Long-term Memory\n\n")
            _storage_set_json(
                storage,
                onboarding_key,
                {"stage": 2, "completed": True, "completed_at": int(time.time())},
            )
            yield self.create_text_message(
                f"✅身份设定已完成：我叫 {agent_name}，风格是「{agent_vibe}」。\n"
                "后续我会基于这个身份来和你交流并持续记忆\n"
                "高阶策略：可通过update_persona工具调整身份，设置灵魂（SOUL.md）"
            )
            return
        os.makedirs(session_dir, exist_ok=True)
        _storage_set_text(storage, session_dir_key, session_dir)
        _cleanup_old_temp_sessions(temp_root, keep=4, protect_dirs={session_dir})

        file_items: list[Any] = []
        files_param = tool_parameters.get("files")
        if isinstance(files_param, list):
            file_items = [x for x in files_param if x]
        elif files_param:
            file_items = [files_param]
        elif tool_parameters.get("file"):
            file_items = [tool_parameters.get("file")]

        uploads_context = ""
        uploaded: list[dict[str, Any]] = []
        if file_items:
            uploads_dir = _safe_join(session_dir, "uploads")
            os.makedirs(uploads_dir, exist_ok=True)
            for item in file_items:
                url, name = _extract_url_and_name(item)
                if not url:
                    yield self.create_text_message("❌未能获取上传文件 URL（files[i].url）。\n")
                    return
                try:
                    content = _download_file_content(str(url), timeout=45)
                except Exception as e:
                    yield self.create_text_message(f"❌文件下载失败：{str(e)}\n")
                    return
                ext = _infer_ext_from_url(str(url))
                filename = _safe_filename(str(name) if name else None, fallback_ext=ext)
                abs_path = os.path.join(uploads_dir, filename)
                try:
                    with open(abs_path, "wb") as f:
                        f.write(content)
                except Exception as e:
                    yield self.create_text_message(f"❌保存上传文件失败：{str(e)}\n")
                    return

                rel_path = f"uploads/{filename}"
                mime = None
                if isinstance(item, dict) and item.get("mime_type"):
                    mime = str(item.get("mime_type") or "").strip() or None
                if not mime:
                    try:
                        mime = _guess_mime_type(filename)
                    except Exception:
                        mime = None
                _, ext = os.path.splitext(filename)
                fmt = (ext or "").lstrip(".").lower().strip()
                if not fmt:
                    fmt = "image"
                base64_data = ""
                mime_norm = str(mime or "").strip().lower()
                if mime_norm.startswith("image/") and len(content) <= 3_000_000:
                    try:
                        base64_data = base64.b64encode(content).decode("ascii")
                    except Exception:
                        base64_data = ""
                uploaded.append(
                    {
                        "relative_path": rel_path,
                        "abs_path": abs_path,
                        "bytes": len(content),
                        "mime_type": mime or "",
                        "filename": filename,
                        "source_url": str(url),
                        "format": fmt,
                        "base64_data": base64_data,
                    }
                )
            if uploaded:
                lines = ["\n\n[本次上传文件(files参数)]", f"uploads_dir: {uploads_dir}"]
                for f in uploaded:
                    rel = str(f.get("relative_path") or "").strip()
                    abs_path = _safe_join(session_dir, rel) if rel else ""
                    lines.append(f"- {rel} | abs={abs_path} | filename={f.get('filename') or ''} | bytes={f.get('bytes') or 0}")
                uploads_context = "\n".join(lines) + "\n"
        else:
            uploads_dir = _safe_join(session_dir, "uploads")
            os.makedirs(uploads_dir, exist_ok=True)

        if not uploads_context:
            uploads_context = _build_uploads_context(session_dir)

        runtime = _AgentRuntime(
            skills_root=skills_root,
            session_dir=session_dir,
            memory_turns=memory_turns,
            skills_snapshot_cache_path=os.path.join(temp_root, "skills_snapshot.json"),
        )

        skills_snapshot = runtime.load_skills_snapshot()
        skills_index = runtime.load_skills_index()
        try:
            skills_count = len(skills_index.get("skills") or []) if isinstance(skills_index, dict) else 0
        except Exception:
            skills_count = 0
        _dbg(
            "start "
            + _model_brief(model)
            + f" session_dir={session_dir} skills_root={skills_root!s} skills_count={skills_count} "
            + f"query_len={len(query)}"
        )
        system_content = build_system_prompt_content(
            system_prompt=str(system_prompt or ""),
            session_dir=str(session_dir),
            skills_root=str(skills_root) if skills_root else None,
            skills_snapshot=skills_snapshot if isinstance(skills_snapshot, dict) else {},
            storage=storage,
            session=self.session,
            user_id=user_id,
            identity_key=identity_key,
            user_key=user_key,
            soul_key=soul_key,
            memory_key=memory_key,
            uploads_context=str(uploads_context or ""),
            approval_context=str(approval_context or ""),
        )

        messages: list[Any] = [SystemPromptMessage(content=system_content)]

        image_entries: list[dict[str, Any]] = []
        for f in uploaded:
            if not isinstance(f, dict):
                continue
            mime = str(f.get("mime_type") or "").strip().lower()
            if mime and not mime.startswith("image/"):
                continue
            image_entries.append(
                {
                    "filename": str(f.get("filename") or "").strip(),
                    "mime_type": mime or "image/*",
                    "format": str(f.get("format") or "").strip().lower() or "image",
                    "source_url": str(f.get("source_url") or "").strip(),
                    "abs_path": str(f.get("abs_path") or "").strip(),
                    "bytes": int(f.get("bytes") or 0),
                    "base64_data": str(f.get("base64_data") or "").strip(),
                }
            )
        image_entries = image_entries[:4]
        if uploaded:
            try:
                img_bytes_total = sum(int(e.get("bytes") or 0) for e in image_entries)
            except Exception:
                img_bytes_total = 0
            _dbg(
                "vision_inputs "
                + json.dumps(
                    {
                        "uploaded_total": len(uploaded),
                        "images_selected": len(image_entries),
                        "images_bytes_total": img_bytes_total,
                        "images": [
                            {
                                "filename": str(e.get("filename") or ""),
                                "bytes": int(e.get("bytes") or 0),
                                "has_url": bool(str(e.get("source_url") or "").strip()),
                                "has_b64": bool(str(e.get("base64_data") or "").strip()),
                            }
                            for e in image_entries
                        ],
                    },
                    ensure_ascii=False,
                )
            )

        def _ensure_base64_data(entry: dict[str, Any]) -> str:
            b64 = str(entry.get("base64_data") or "").strip()
            if b64:
                return b64
            path = str(entry.get("abs_path") or "").strip()
            if not path or not os.path.isfile(path):
                return ""
            try:
                with open(path, "rb") as f:
                    raw = f.read()
                b64 = base64.b64encode(raw).decode("ascii")
            except Exception:
                return ""
            entry["base64_data"] = b64
            return b64

        def build_user_message(*, text: str, mode: str) -> Any:
            if mode in {"default", "all_base64"} and image_entries:
                vision_hint = "你已收到用户上传的图片，请直接查看并分析图片内容（不要声称无法查看图片）。\n"
                parts: list[dict[str, Any]] = [{"type": "text", "data": vision_hint + str(text or "")}]
                picked: list[dict[str, Any]] = []
                for e in image_entries:
                    if mode == "all_base64":
                        b64 = _ensure_base64_data(e)
                        if b64:
                            parts.append(
                                {
                                    "type": "image",
                                    "format": e.get("format") or "image",
                                    "base64_data": b64,
                                    "mime_type": e.get("mime_type") or "image/*",
                                    "filename": e.get("filename") or "",
                                }
                            )
                            picked.append(
                                {
                                    "filename": str(e.get("filename") or ""),
                                    "pick": "base64_force",
                                    "bytes": int(e.get("bytes") or 0),
                                }
                            )
                            continue
                    bytes_len = int(e.get("bytes") or 0)
                    b64 = str(e.get("base64_data") or "").strip()
                    if bytes_len > 0 and bytes_len <= 3_000_000 and b64:
                        parts.append(
                            {
                                "type": "image",
                                "format": e.get("format") or "image",
                                "base64_data": b64,
                                "mime_type": e.get("mime_type") or "image/*",
                                "filename": e.get("filename") or "",
                            }
                        )
                        picked.append(
                            {
                                "filename": str(e.get("filename") or ""),
                                "pick": "base64",
                                "bytes": bytes_len,
                            }
                        )
                        continue
                    url = str(e.get("source_url") or "").strip()
                    if url:
                        parts.append(
                            {
                                "type": "image",
                                "format": e.get("format") or "image",
                                "url": url,
                                "mime_type": e.get("mime_type") or "image/*",
                                "filename": e.get("filename") or "",
                            }
                        )
                        picked.append(
                            {
                                "filename": str(e.get("filename") or ""),
                                "pick": "url",
                                "bytes": bytes_len,
                            }
                        )
                if picked:
                    _dbg(
                        "vision_message_built "
                        + json.dumps(
                            {
                                "mode": mode,
                                "picked_images": picked,
                            },
                            ensure_ascii=False,
                        )
                    )
                return UserPromptMessage(content=parts)
            return UserPromptMessage(content=str(text or ""))

        messages.append(build_user_message(text=effective_query, mode="default"))

        conversation_summary: str = ""

        def estimate_tokens(text: Any) -> int:
            s = str(text or "")
            return max(1, (len(s) // 4) + 1)

        def message_to_text(msg: Any) -> str:
            content = getattr(msg, "content", None)
            if content is None:
                content = _safe_get(msg, "content")
            if isinstance(content, list):
                acc: list[str] = []
                for item in content:
                    if isinstance(item, dict):
                        t = item.get("text") or item.get("content") or item.get("data") or ""
                        if t:
                            acc.append(str(t))
                    elif item:
                        acc.append(str(item))
                return "\n".join(acc).strip()
            if isinstance(content, dict):
                return json.dumps(content, ensure_ascii=False)
            return str(content or "").strip()

        def estimate_prompt_tokens(msgs: list[Any]) -> int:
            total = 0
            for m in msgs:
                total += estimate_tokens(message_to_text(m))
            return total

        def invoke_llm_text(prompt_messages: list[Any]) -> str:
            try:
                try:
                    try:
                        resp = self.session.model.llm.invoke(
                            model_config=model,
                            prompt_messages=prompt_messages,
                            stream=False,
                        )
                    except Exception as e:
                        msg = str(e)
                        if ("incremental_output" in msg) and ("True" in msg):
                            resp = self.session.model.llm.invoke(
                                model_config=model,
                                prompt_messages=prompt_messages,
                            )
                        else:
                            raise
                except TypeError:
                    resp = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=prompt_messages,
                    )
            except Exception:
                return ""
            usage.record_response(resp)

            if _safe_get(resp, "message") is not None:
                msg = _safe_get(resp, "message") or {}
                content = _safe_get(msg, "content")
                text, _parts = _split_message_content(content)
                return str(text or "").strip()

            if isinstance(resp, str):
                return resp.strip()

            chunks: list[str] = []
            try:
                for chunk in resp:
                    usage.record_chunk(chunk)
                    delta = _safe_get(chunk, "delta") or {}
                    msg = _safe_get(delta, "message") or {}
                    content = _safe_get(msg, "content")
                    t, _parts = _split_message_content(content)
                    if t:
                        chunks.append(str(t))
            except Exception:
                pass
            return "".join(chunks).strip()

        def _memory_extract_updates(*, existing_memory_md: str, text: str) -> dict[str, Any]:
            s = str(text or "").strip()
            if not s:
                return {}
            existing = str(existing_memory_md or "").strip()
            if len(existing) > 4000:
                existing = existing[:4000]
            if len(s) > 12000:
                s = s[-12000:]

            extractor_system = SystemPromptMessage(
                content=(
                    "你是记忆整理器。请从对话内容中提取“应写入长期记忆”的信息，并输出严格 JSON。\n"
                    "只提取稳定且可复用的信息：用户偏好（称呼/风格/禁忌）、项目事实（路径/命令/约束）、关键决定。\n"
                    "不要写流水账；不要写情绪安慰；不要编造。\n"
                    "输出 JSON schema:\n"
                    "{\n"
                    '  "user_preferences": {string: string},\n'
                    '  "project_facts": {string: string},\n'
                    '  "decisions": [string]\n'
                    "}\n"
                    "规则：\n"
                    "- key 尽量短（<= 24 字）；value <= 200 字。\n"
                    "- 没有就输出空对象/空数组。\n"
                    "- 只输出 JSON，不要输出任何其它文本。\n"
                )
            )
            extractor_user = UserPromptMessage(
                content=(
                    ("[已有 MEMORY.md（节选）]\n" + existing + "\n\n") if existing else ""
                    + "[待提炼的对话]\n"
                    + s
                )
            )
            out = invoke_llm_text([extractor_system, extractor_user]).strip()
            if not out:
                return {}
            a = out.find("{")
            b = out.rfind("}")
            if a == -1 or b == -1 or b <= a:
                return {}
            j = out[a : b + 1]
            try:
                obj = json.loads(j)
                return obj if isinstance(obj, dict) else {}
            except Exception:
                return {}

        def _memory_merge_managed_block(existing_memory_md: str, updates: dict[str, Any]) -> str:
            existing = str(existing_memory_md or "").strip()
            if not existing:
                existing = "# MEMORY.md - Long-term Memory\n\n"

            def norm_kv(d: Any) -> dict[str, str]:
                if not isinstance(d, dict):
                    return {}
                out: dict[str, str] = {}
                for k, v in d.items():
                    kk = str(k or "").strip()
                    vv = str(v or "").strip()
                    if not kk or not vv:
                        continue
                    if len(kk) > 24:
                        kk = kk[:24]
                    if len(vv) > 200:
                        vv = vv[:200]
                    out[kk] = vv
                return out

            def norm_list(xs: Any) -> list[str]:
                if not isinstance(xs, list):
                    return []
                out: list[str] = []
                for x in xs:
                    s = str(x or "").strip()
                    if not s:
                        continue
                    if len(s) > 240:
                        s = s[:240]
                    if s not in out:
                        out.append(s)
                return out

            user_prefs = norm_kv(updates.get("user_preferences"))
            project_facts = norm_kv(updates.get("project_facts"))
            decisions = norm_list(updates.get("decisions"))

            marker = "## Managed Memory (auto)"
            lines = existing.splitlines()
            start = -1
            for i, line in enumerate(lines):
                if line.strip() == marker:
                    start = i
                    break
            if start != -1:
                end = len(lines)
                for j in range(start + 1, len(lines)):
                    if lines[j].startswith("## ") and lines[j].strip() != marker:
                        end = j
                        break
                preserved = "\n".join(lines[:start]).rstrip() + "\n\n" + "\n".join(lines[end:]).lstrip()
                existing = preserved.strip() + "\n"

            def render_section(title: str, kv: dict[str, str]) -> list[str]:
                if not kv:
                    return [f"### {title}", "- (empty)"]
                out = [f"### {title}"]
                for k in sorted(kv.keys()):
                    out.append(f"- **{k}:** {kv[k]}")
                return out

            def render_list(title: str, items: list[str]) -> list[str]:
                if not items:
                    return [f"### {title}", "- (empty)"]
                out = [f"### {title}"]
                for it in items:
                    out.append(f"- {it}")
                return out

            managed: list[str] = [marker]
            managed.append(f"- updated_at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}")
            managed.append("")
            managed.extend(render_section("User Preferences", user_prefs))
            managed.append("")
            managed.extend(render_section("Project Facts", project_facts))
            managed.append("")
            managed.extend(render_list("Decisions", decisions))
            managed.append("")

            merged = existing.rstrip() + "\n\n" + "\n".join(managed).rstrip() + "\n"
            return merged

        def _memory_flush_for_compaction(*, text: str) -> None:
            existing_md = _storage_get_text(storage, memory_key).strip()
            if not existing_md:
                existing_md = "# MEMORY.md - Long-term Memory\n\n"
            updates = _memory_extract_updates(existing_memory_md=existing_md, text=text)
            if not updates:
                if not _storage_get_text(storage, memory_key).strip():
                    _storage_set_text(storage, memory_key, existing_md)
                return
            merged = _memory_merge_managed_block(existing_md, updates)
            _storage_set_text(storage, memory_key, merged)

        def compact_if_needed() -> None:
            nonlocal conversation_summary
            max_tokens = max(2000, int(compaction_max_prompt_tokens or 0))
            current_tokens = estimate_prompt_tokens(messages)
            if current_tokens <= max_tokens:
                return

            keep_recent_tokens = 2500
            if isinstance(memory_turns, int) and memory_turns > 0:
                keep_recent_tokens = min(6000, max(1200, int(memory_turns) * 250))

            system_msg = messages[0]
            tail: list[Any] = []
            tail_tokens = 0
            for m in reversed(messages[1:]):
                t = estimate_tokens(message_to_text(m))
                if tail and tail_tokens + t > keep_recent_tokens:
                    break
                tail.append(m)
                tail_tokens += t
                if tail_tokens >= keep_recent_tokens:
                    break
            tail.reverse()
            prefix = messages[1 : (len(messages) - len(tail))]
            prefix_text_blocks: list[str] = []
            for m in prefix:
                mt = message_to_text(m)
                if mt:
                    prefix_text_blocks.append(mt)
                if sum(len(x) for x in prefix_text_blocks) > 12000:
                    break
            prefix_text = "\n\n".join(prefix_text_blocks).strip()
            if not prefix_text:
                messages[:] = [system_msg, *tail]
                return

            try:
                filtered = filter_memory_write(MemoryWriteContext(user_id=user_id, text=prefix_text))
                if filtered.strip():
                    _memory_flush_for_compaction(text=filtered)
            except Exception:
                pass

            summarizer_system = SystemPromptMessage(
                content=(
                    "你是一个对话压缩器。请把给定的对话内容总结成可用于后续继续工作的摘要。\n"
                    "要求：\n"
                    "- 只保留能影响后续行动的事实：用户目标、关键约束、已完成进度、重要决定、文件名/路径/参数。\n"
                    "- 不要写寒暄，不要复述无关细节。\n"
                    "- 输出不超过 900 字。\n"
                )
            )
            summarizer_user = UserPromptMessage(
                content=(
                    ("[已有摘要]\n" + conversation_summary + "\n\n") if conversation_summary else ""
                )
                + "[需要压缩的历史]\n"
                + prefix_text
            )
            summary = invoke_llm_text([summarizer_system, summarizer_user]).strip()
            if not summary:
                messages[:] = [system_msg, *tail]
                return

            conversation_summary = summary
            summary_msg = SystemPromptMessage(content="## Conversation Summary\n" + summary.strip())
            messages[:] = [system_msg, summary_msg, *tail]

        final_text: str | None = None
        final_file_meta: dict[str, dict[str, str]] = {}
        empty_responses = 0
        saved_asset_fingerprints: set[str] = set()
        final_text_already_streamed = False
        agent_tag_header = build_agent_tag_header(storage=storage, identity_key=identity_key, identity_md=identity_md)

        def invoke_llm_live(
            *,
            prompt_messages: list[Any],
            tools: list[Any] | None,
            fallback_prompt_messages: list[Any] | None = None,
            alt_prompt_messages: list[Any] | None = None,
        ) -> Generator[ToolInvokeMessage, None, tuple[str, list[Any], Any, int, bool]]:
            nontext_content: list[dict[str, Any]] = []
            tool_calls_all: list[Any] = []
            text_parts: list[str] = []
            chunks_count = 0
            streamed_any = False
            saw_tool_calls = False
            typing_chunk = 6
            emitted_prefix = False
            emitted_len = 0

            def emit_typing(text: str) -> Generator[ToolInvokeMessage, None, None]:
                nonlocal streamed_any
                if not text:
                    return
                tagged = "\n" + agent_tag_header + "\n" + text.strip() + "\n\n"
                step = max(1, int(typing_chunk))
                for i in range(0, len(tagged), step):
                    yield self.create_text_message(tagged[i : i + step])
                    streamed_any = True
            
            def should_emit_user_text(text: str) -> bool:
                if not text:
                    return False
                s = str(text)
                stripped = s.lstrip()
                if stripped.startswith("```") and stripped.count("```") < 2:
                    return False
                return True

            def _invoke_once(pm: list[Any]) -> Generator[ToolInvokeMessage, None, tuple[str, list[Any], Any, int, bool]]:
                nonlocal nontext_content, tool_calls_all, text_parts, chunks_count, streamed_any, saw_tool_calls, emitted_prefix, emitted_len
                nontext_content = []
                tool_calls_all = []
                text_parts = []
                chunks_count = 0
                streamed_any = False
                saw_tool_calls = False
                emitted_prefix = False
                emitted_len = 0

                try:
                    response = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=pm,
                        tools=tools,
                        stream=True,
                    )
                except TypeError:
                    response = self.session.model.llm.invoke(
                        model_config=model,
                        prompt_messages=pm,
                        stream=True,
                    )

                if _safe_get(response, "message") is not None:
                    usage.record_response(response)
                    msg = _safe_get(response, "message") or {}
                    content = _safe_get(msg, "content")
                    text, parts = _split_message_content(content)
                    if parts:
                        nontext_content.extend(parts)
                    tool_calls = _safe_get(msg, "tool_calls") or []
                    if isinstance(tool_calls, list):
                        tool_calls_all.extend(tool_calls)
                        if tool_calls:
                            saw_tool_calls = True
                    if text:
                        text_parts.append(text)
                    combined_text = "".join(text_parts).strip()
                    if combined_text and not saw_tool_calls and should_emit_user_text(combined_text):
                        yield from emit_typing(combined_text)
                    return combined_text, tool_calls_all, nontext_content, chunks_count, streamed_any

                for chunk in response:
                    usage.record_chunk(chunk)
                    chunks_count += 1
                    delta = _safe_get(chunk, "delta") or {}
                    msg = _safe_get(delta, "message") or {}
                    content = _safe_get(msg, "content")
                    t, parts = _split_message_content(content)
                    if parts:
                        nontext_content.extend(parts)
                    tc = _safe_get(msg, "tool_calls") or []
                    if isinstance(tc, list) and tc:
                        tool_calls_all.extend(tc)
                        if not saw_tool_calls:
                            saw_tool_calls = True
                    if t:
                        text_parts.append(t)
                        combined_text_live = "".join(text_parts).strip()
                        if combined_text_live and not saw_tool_calls and should_emit_user_text(combined_text_live):
                            if not emitted_prefix:
                                yield self.create_text_message("\n" + agent_tag_header + "\n")
                                emitted_prefix = True
                            new = combined_text_live[emitted_len:]
                            if new:
                                step = max(1, int(typing_chunk))
                                for i in range(0, len(new), step):
                                    yield self.create_text_message(new[i : i + step])
                                    streamed_any = True
                                emitted_len = len(combined_text_live)
                combined_text = "".join(text_parts).strip()
                if emitted_prefix:
                    yield self.create_text_message("\n\n")
                elif combined_text and not saw_tool_calls and should_emit_user_text(combined_text):
                    yield from emit_typing(combined_text)
                return combined_text, tool_calls_all, nontext_content, chunks_count, streamed_any

            try:
                return (yield from _invoke_once(prompt_messages))
            except Exception as e:
                msg = str(e)
                prefer_alt = (
                    "Download multimodal file timed out" in msg
                    or "InvalidParameter" in msg
                    or "multimodal file" in msg.lower()
                    or "timed out" in msg.lower()
                )
                _dbg(
                    "vision_invoke_failed "
                    + json.dumps(
                        {
                            "prefer_alt": bool(prefer_alt),
                            "has_alt": alt_prompt_messages is not None,
                            "has_fallback": fallback_prompt_messages is not None,
                            "exception": _shorten_text(msg, 400),
                        },
                        ensure_ascii=False,
                    )
                )
                if prefer_alt and alt_prompt_messages is not None:
                    try:
                        _dbg("vision_invoke_retry alt=all_base64")
                        return (yield from _invoke_once(alt_prompt_messages))
                    except Exception:
                        pass
                if fallback_prompt_messages is not None:
                    try:
                        _dbg("vision_invoke_retry fallback=text_only")
                        return (yield from _invoke_once(fallback_prompt_messages))
                    except Exception as e2:
                        return (
                            "",
                            [],
                            {"error": "stream_parse_failed", "exception": str(e2), "primary_exception": str(e)},
                            chunks_count,
                            streamed_any,
                        )
                return "", [], {"error": "stream_parse_failed", "exception": str(e)}, chunks_count, streamed_any

        loop_history_size = 30
        loop_warning_threshold = 10
        loop_critical_threshold = 20
        loop_global_no_progress_threshold = 30
        tool_call_sig_history: list[str] = []
        tool_call_sig_result_history: list[tuple[str, str]] = []
        loop_warned_sigs: set[str] = set()

        def _user_explicitly_requested_skill(text: str, *, skill_id: str, display_name: str) -> bool:
            s = str(text or "")
            if not s:
                return False
            s_lower = s.lower()
            sid = str(skill_id or "").strip().lower()
            if sid and sid in s_lower:
                return True
            dn = str(display_name or "").strip().lower()
            if dn and dn in s_lower:
                return True
            return False

        try:
            for step_idx in range(max_tool_turns):
                if timeout_seconds > 0 and (time.time() - started_at) > float(timeout_seconds):
                    final_text = f"❌超过超时时间 timeout_seconds={timeout_seconds}，已提前停止。"
                    break
                compact_if_needed()
                _dbg(
                    f"step={step_idx+1}/{max_tool_turns} messages={len(messages)} "
                    f"est_tokens={estimate_prompt_tokens(messages)}"
                )
                fallback_messages = None
                alt_messages = None
                if image_entries and len(messages) >= 2:
                    u = messages[1]
                    c = getattr(u, "content", None)
                    if isinstance(c, list) and any(isinstance(it, dict) and it.get("type") == "image" for it in c):
                        alt_messages = list(messages)
                        alt_messages[1] = build_user_message(text=str(effective_query or ""), mode="all_base64")
                        fallback_messages = list(messages)
                        fallback_messages[1] = build_user_message(
                            text=str(effective_query or "")
                            + "\n\n（注意：本次图片未能以视觉输入方式传入，只能通过 uploads/ 文件路径处理）",
                            mode="text",
                        )
                try:
                    res_text, tool_calls, nontext, chunks, streamed_any = yield from invoke_llm_live(
                        prompt_messages=messages,
                        tools=_build_prompt_message_tools(TOOL_SCHEMAS, PromptMessageTool),
                        fallback_prompt_messages=fallback_messages,
                        alt_prompt_messages=alt_messages,
                    )
                except Exception as e:
                    msg = str(e)
                    if "NameResolutionError" in msg or "Failed to resolve" in msg:
                        yield self.create_text_message(
                            "❌ LLM 调用失败：无法解析模型服务域名（DNS/网络问题）。\n"
                            "当前报错信息：\n"
                            + msg
                            + "\n\n请检查：\n"
                            + "1) 运行插件的环境是否能访问公网/是否需要代理\n"
                            + "2) DNS 是否可用（能否解析 dashscope.aliyuncs.com 等域名）\n"
                            + "3) Dify 的模型供应商（通义）网络出站是否被限制\n"
                        )
                    else:
                        yield self.create_text_message(
                            "❌ 大模型调用报错。\n"
                            "报错信息：\n"
                            + msg
                            + "\n\n请检查：\n"
                            + "1) 模型是否欠费/余额不足\n"
                            + "2) API Key/权限是否正确（是否有该模型调用权限）\n"
                            + "3) 网络出站/代理/DNS 是否受限\n"
                            + "4) 模型供应商服务是否异常\n"
                        )
                    return

                _dbg(
                    f"llm_return content_len={len(res_text)} tool_calls={len(tool_calls)} chunks={chunks} "
                    f"nontext={_shorten_text(nontext, 200) if nontext else ''}"
                )
                if nontext:
                    saved_assets = persist_llm_assets(
                        parts=nontext, session_dir=session_dir, saved_asset_fingerprints=saved_asset_fingerprints
                    )
                    if saved_assets:
                        _dbg(f"nontext_assets_saved={len(saved_assets)} paths={_shorten_text(saved_assets, 300)}")
                if tool_calls:
                    empty_responses = 0
                    messages.append(AssistantPromptMessage(content=res_text or "", tool_calls=tool_calls))
                    forced_text: str | None = None
                    for tc in tool_calls:
                        call_id, name, arguments = _parse_tool_call(tc)
                        tool_name = str(name or "")
                        _dbg(f"tool_call name={tool_name} id={call_id!s} args={_shorten_text(arguments, 400)}")

                        args_norm = ""
                        try:
                            args_norm = json.dumps(arguments or {}, ensure_ascii=False, sort_keys=True)
                        except Exception:
                            args_norm = str(arguments or "")
                        tool_sig = f"{tool_name}|{args_norm}"
                        if loop_detection:
                            recent = tool_call_sig_history[-loop_history_size:] if loop_history_size > 0 else tool_call_sig_history
                            repeats = sum(1 for s in recent if s == tool_sig)
                            if repeats >= loop_critical_threshold:
                                forced_text = (
                                    "❌检测到工具调用循环，已提前熔断以避免无意义重试。\n"
                                    f"- tool: {tool_name}\n"
                                    f"- repeats_in_window: {repeats}\n"
                                    "建议：检查参数是否正确、是否缺少前置步骤（如 list/read），或改用更高层的策略（先读说明书/先列目录/先产出中间文件）。"
                                )
                                break
                            if repeats >= loop_warning_threshold and tool_sig not in loop_warned_sigs:
                                loop_warned_sigs.add(tool_sig)
                                result = {
                                    "error": "tool_loop_warning",
                                    "tool": tool_name,
                                    "detail": f"检测到疑似重复调用（最近窗口内重复 {repeats} 次）。请停止重复调用并调整策略。",
                                }
                                messages.append(
                                    ToolPromptMessage(
                                        tool_call_id=str(call_id or ""),
                                        name=tool_name,
                                        content=json.dumps(result, ensure_ascii=False),
                                    )
                                )
                                messages.append(
                                    UserPromptMessage(
                                        content=(
                                            f"你正在重复调用 `{tool_name}`，这通常意味着陷入死循环。"
                                            "请改为：先检查前置条件/路径/目录结构，或直接给出当前可交付产物与下一步建议。"
                                        )
                                    )
                                )
                                continue

                        ok_args, arg_detail = _validate_tool_arguments(tool_name, arguments)
                        if not ok_args:
                            result = {
                                "error": "invalid_tool_arguments",
                                "tool": tool_name,
                                "detail": arg_detail,
                                "got": arguments,
                            }
                            _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                            messages.append(
                                ToolPromptMessage(
                                    tool_call_id=str(call_id or ""),
                                    name=tool_name,
                                    content=json.dumps(result, ensure_ascii=False),
                                )
                            )
                            messages.append(UserPromptMessage(content=_tool_call_retry_prompt(tool_name, arg_detail)))
                            continue

                        if tool_name in {"list_skill_files", "read_skill_file", "run_skill_command"}:
                            skill_name = str(arguments.get("skill_name") or "").strip()
                            entry = runtime.get_skill_entry(skill_name) if skill_name else None
                            if entry:
                                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                                visible = bool(status.get("visible")) if isinstance(status, dict) else False
                                display_name = str(entry.get("name") or "").strip()
                                skill_id = str(entry.get("folder") or entry.get("id") or skill_name).strip()
                                if not visible and not _user_explicitly_requested_skill(
                                    user_input, skill_id=skill_id, display_name=display_name
                                ):
                                    missing = status.get("missing") if isinstance(status, dict) else {}
                                    result = {
                                        "error": "skill_not_callable",
                                        "skill_name": skill_name,
                                        "detail": "该技能当前不可由模型调用（visible=false 或 disable-model-invocation）。仅允许在用户明确点名该技能时继续。",
                                        "missing": missing,
                                    }
                                    _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                                    messages.append(
                                        ToolPromptMessage(
                                            tool_call_id=str(call_id or ""),
                                            name=tool_name,
                                            content=json.dumps(result, ensure_ascii=False),
                                        )
                                    )
                                    messages.append(
                                        UserPromptMessage(
                                            content=(
                                                f"你尝试调用技能《{skill_name}》，但它当前标记为不可由模型调用。\n"
                                                "要求：只允许把该技能作为“说明型展示”出现在技能列表里；不要继续读取/执行它。\n"
                                                "如果用户明确说“就用这个技能”并点名技能名，才允许继续。"
                                            )
                                        )
                                    )
                                    continue

                        if tool_name == "list_skill_files":
                            yield self.create_text_message(
                                f"✅正在查看技能《{str(arguments.get('skill_name') or '')}》文件结构…\n"
                            )
                        elif tool_name == "read_skill_file":
                            yield self.create_text_message(
                                f"✅正在读取技能《{str(arguments.get('skill_name') or '')}》文件：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "run_skill_command":
                            yield self.create_text_message(
                                f"✅正在执行技能《{str(arguments.get('skill_name') or '')}》命令…\n"
                            )
                        elif tool_name == "get_session_context":
                            yield self.create_text_message("✅正在获取会话目录信息…\n")
                        elif tool_name == "get_system_status":
                            yield self.create_text_message("✅正在获取运行环境状态…\n")
                        elif tool_name == "get_current_time":
                            yield self.create_text_message("✅正在获取当前时间…\n")
                        elif tool_name == "get_persona":
                            yield self.create_text_message("✅正在读取当前设定…\n")
                        elif tool_name == "update_persona":
                            yield self.create_text_message("✅正在更新设定…\n")
                        elif tool_name == "write_temp_file":
                            yield self.create_text_message(
                                f"✅正在按说明书写入临时文件：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "read_temp_file":
                            yield self.create_text_message(
                                f"✅正在读取临时文件：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "list_temp_files":
                            yield self.create_text_message("✅正在查看临时目录文件…\n")
                        elif tool_name == "glob_temp_files":
                            yield self.create_text_message("✅正在按模式匹配临时目录文件…\n")
                        elif tool_name == "grep_temp_files":
                            yield self.create_text_message("✅正在检索临时目录内容…\n")
                        elif tool_name == "edit_temp_file":
                            yield self.create_text_message(
                                f"✅正在编辑临时文件：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "delete_temp_path":
                            yield self.create_text_message(
                                f"✅正在删除临时路径：{str(arguments.get('relative_path') or '')}…\n"
                            )
                        elif tool_name == "run_temp_command":
                            yield self.create_text_message("✅正在执行临时命令…\n")
                        elif tool_name == "export_temp_file":
                            yield self.create_text_message(
                                f"✅正在标记交付文件：{str(arguments.get('temp_relative_path') or '')}…\n"
                            )
                        elif tool_name == "web_fetch":
                            yield self.create_text_message("✅正在抓取网页内容…\n")

                        if tool_name == "list_skill_files":
                            result = runtime.list_skill_files(
                                str(arguments.get("skill_name") or ""),
                                int(arguments.get("max_depth") or 2),
                            )
                        elif tool_name == "read_skill_file":
                            result = runtime.read_skill_file(
                                str(arguments.get("skill_name") or ""),
                                str(arguments.get("relative_path") or ""),
                                int(arguments.get("max_chars") or 12000),
                            )
                        elif tool_name == "run_skill_command":
                            requested_command = (
                                arguments.get("command") if isinstance(arguments.get("command"), list) else []
                            )
                            requested_auto_install = bool(arguments.get("auto_install") or False)
                            exe0 = str(requested_command[0] or "") if requested_command else ""
                            install_like_bins = {"pip", "npm", "npx", "bun", "uv", "uvx"}
                            if requested_auto_install or exe0 in install_like_bins:
                                result = {
                                    "error": "install_commands_disabled",
                                    "detail": "对话执行阶段禁止安装/包管理命令与 auto_install。请在“技能管理（TM）”中执行“依赖安装”补全 Python 依赖；JS/系统级依赖请联系管理员在 plugin_daemon 容器安装。",
                                    "command": requested_command,
                                    "auto_install": requested_auto_install,
                                }
                                _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                                messages.append(
                                    ToolPromptMessage(
                                        tool_call_id=str(call_id or ""),
                                        name=tool_name,
                                        content=json.dumps(result, ensure_ascii=False),
                                    )
                                )
                                messages.append(
                                    UserPromptMessage(
                                        content=(
                                            "你刚才尝试在对话中安装依赖/运行包管理命令，这是被禁止的。\n"
                                            "请改为：提示用户打开“技能管理（TM）”并执行“依赖安装”；如果是 JS/系统依赖（如 node/npm/agent-browser），提示联系管理员在 plugin_daemon 容器安装。\n"
                                            "然后继续使用已就绪的技能执行任务。"
                                        )
                                    )
                                )
                                continue

                            skill_for_check = str(arguments.get("skill_name") or "").strip()
                            entry = runtime.get_skill_entry(skill_for_check) if skill_for_check else None
                            if entry:
                                status = entry.get("status") if isinstance(entry.get("status"), dict) else {}
                                eligible = bool(status.get("eligible")) if isinstance(status, dict) else False
                                if not eligible:
                                    missing = status.get("missing") if isinstance(status, dict) else {}
                                    result = {
                                        "error": "skill_not_eligible",
                                        "skill_name": skill_for_check,
                                        "detail": "该技能当前不可执行（eligible=false）。请先补全依赖后再运行。",
                                        "missing": missing,
                                    }
                                    _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                                    messages.append(
                                        ToolPromptMessage(
                                            tool_call_id=str(call_id or ""),
                                            name=tool_name,
                                            content=json.dumps(result, ensure_ascii=False),
                                        )
                                    )
                                    messages.append(
                                        UserPromptMessage(
                                            content=(
                                                f"技能《{skill_for_check}》当前不可执行（缺少依赖）。\n"
                                                "请提示用户到“技能管理（TM）”执行“依赖安装”补全 Python 依赖；JS/系统级依赖请联系管理员在 plugin_daemon 容器安装。\n"
                                                "不要继续尝试 run_skill_command。"
                                            )
                                        )
                                    )
                                    continue
                            exec_override = None
                            if (
                                isinstance(exec_once_allowed, dict)
                                and str(exec_once_allowed.get("kind") or "") in {"exec_once", "exec"}
                                and str(exec_once_allowed.get("tool") or "") == tool_name
                                and isinstance(exec_once_allowed.get("command"), list)
                                and exec_once_allowed.get("command") == requested_command
                            ):
                                exec_override = {
                                    "exe": str(exec_once_allowed.get("exe") or exe0),
                                    "allow_not_in_allowlist": bool(exec_once_allowed.get("allow_not_in_allowlist") is True),
                                    "allow_untrusted_path": bool(exec_once_allowed.get("allow_untrusted_path") is True),
                                    "path_allowlist": exec_once_allowed.get("path_allowlist") if isinstance(exec_once_allowed.get("path_allowlist"), list) else [],
                                }
                            if exec_override is None:
                                exec_override = build_exec_override_from_grants(
                                    grants=grants,
                                    tool_name=tool_name,
                                    skill_name=str(arguments.get("skill_name") or "").strip() or None,
                                    requested_command=requested_command,
                                    exe0=exe0,
                                )
                            if not exec_approval_enabled:
                                exec_override = {"exe": exe0, "allow_not_in_allowlist": True}
                            result = runtime.run_skill_command(
                                skill_name=str(arguments.get("skill_name") or ""),
                                command=requested_command,
                                cwd_relative=(
                                    str(arguments.get("cwd_relative")) if arguments.get("cwd_relative") else None
                                ),
                                auto_install=requested_auto_install,
                                exec_override=exec_override,
                            )
                            if (
                                isinstance(exec_once_allowed, dict)
                                and exec_override is not None
                                and isinstance(result, dict)
                                and result.get("error") is None
                                and result.get("returncode") is not None
                            ):
                                exec_once_allowed = None
                            if isinstance(result, dict):
                                err = str(result.get("error") or "").strip()
                                detail = str(result.get("detail") or "").strip()
                                if err.startswith("command not allowed:") and exec_override is None and exec_approval_enabled:
                                    allow_not_in_allowlist = True
                                    resolved_path = str(result.get("path") or result.get("resolved_exe") or "").strip()
                                    _storage_set_json(
                                        storage,
                                        approval_pending_key,
                                        {
                                            "pending": True,
                                            "kind": "exec",
                                            "tool": tool_name,
                                            "skill_name": str(arguments.get("skill_name") or "").strip(),
                                            "command": requested_command,
                                            "exe": exe0,
                                            "allow_not_in_allowlist": allow_not_in_allowlist,
                                            "path": resolved_path,
                                            "resolved_exe": resolved_path,
                                            "path_allowlist": ["*"],
                                            "original_query": effective_query,
                                            "created_at": int(time.time()),
                                        },
                                    )
                                    forced_text = (
                                        "该步骤需要执行一个未在允许列表的命令。\n"
                                        f"- 命令：{_shorten_text(requested_command, 200)}\n"
                                        + "为安全起见，需要你确认后才能继续。请回复序号：\n"
                                        "1) 允许一次（仅本次）\n"
                                        "2) 总是允许（后续不再提示）\n"
                                        "3) 拒绝\n"
                                    )
                                    break
                            if (
                                isinstance(result, dict)
                                and result.get("returncode") is not None
                                and int(result.get("returncode") or 0) != 0
                            ):
                                stderr = str(result.get("stderr") or "").strip()
                                if stderr:
                                    yield self.create_text_message(
                                        "❌命令执行失败（stderr）：\n"
                                        + _shorten_text(
                                            redact_user_visible_text(
                                                text=stderr, session_dir=session_dir, skills_root=skills_root
                                            ),
                                            1200,
                                        )
                                        + "\n"
                                    )
                            if isinstance(result, dict) and result.get("error") == "no_executable_found":
                                skill = str(result.get("skill") or arguments.get("skill_name") or "")
                                module = str(result.get("module") or "")
                                messages.append(
                                    UserPromptMessage(
                                        content=(
                                            f"技能“{skill}”缺少可执行入口（python -m {module} 在技能目录不存在）。\n"
                                            "不要再尝试调用 run_skill_command 生成最终文件。\n"
                                            "改用 temp 目录策略：用 write_temp_file 生成脚本/程序，在 session_dir 里运行 run_temp_command 产出最终文件；\n"
                                            "若需要安装依赖或运行包管理命令，先向用户发起审批。"
                                        )
                                    )
                                )
                        elif tool_name == "get_session_context":
                            result = runtime.get_session_context()
                        elif tool_name == "get_system_status":
                            result = runtime.get_system_status()
                        elif tool_name == "get_current_time":
                            result = runtime.get_current_time(
                                timezone=(str(arguments.get("timezone") or "").strip() if arguments.get("timezone") else None)
                            )
                        elif tool_name == "get_persona":
                            result = {
                                "identity_md": _storage_get_text(storage, identity_key).strip(),
                                "user_md": _storage_get_text(storage, user_key).strip(),
                                "soul_md": _storage_get_text(storage, soul_key).strip(),
                                "memory_md": _storage_get_text(storage, memory_key).strip(),
                            }
                        elif tool_name == "update_persona":
                            def _md_pick_field(md: str, key: str) -> str:
                                s = str(md or "")
                                if not s:
                                    return ""
                                m = re.search(
                                    rf"^\s*(?:-\s*)?\*\*\s*{re.escape(key)}\s*:\s*\*\*\s*(.+?)\s*$",
                                    s,
                                    flags=re.M | re.I,
                                )
                                return str(m.group(1) or "").strip() if m else ""

                            def _md_set_field(md: str, *, key: str, value: str, header: str) -> str:
                                s = str(md or "").strip()
                                if not s:
                                    s = header.strip() + "\n"
                                lines = s.splitlines()
                                rx = re.compile(rf"^\s*(?:-\s*)?\*\*\s*{re.escape(key)}\s*:\s*\*\*\s*(.*)\s*$", flags=re.I)
                                out: list[str] = []
                                replaced = False
                                for line in lines:
                                    if rx.match(line):
                                        out.append(f"- **{key}:** {value}".rstrip())
                                        replaced = True
                                    else:
                                        out.append(line)
                                if not replaced:
                                    if out and out[-1].strip():
                                        out.append("")
                                    out.append(f"- **{key}:** {value}".rstrip())
                                return "\n".join(out).strip() + "\n"

                            def _soul_set_vibe(md: str, vibe: str) -> str:
                                s = str(md or "").strip()
                                if not s:
                                    return (
                                        "# SOUL.md - Who You Are\n\n"
                                        "## Core\n"
                                        "- 说人话，少模板；可以小调皮，但不油腻。\n"
                                        "- 语气不要刻意迎合，但要给与用户足够的尊重。\n"
                                        "- 有主见，但不自作主张，遇到不确定的事情时，会向用户询问。\n"
                                        "- 幽默是你的底色，善良是你的天性，你会主动关心用户。\n"

                                        "## Vibe\n"
                                        f"{vibe}\n"
                                    )
                                lines = s.splitlines()
                                out: list[str] = []
                                i = 0
                                replaced = False
                                while i < len(lines):
                                    line = lines[i]
                                    if re.match(r"^\s*##\s+Vibe\s*$", line, flags=re.I):
                                        out.append(line)
                                        out.append(vibe)
                                        replaced = True
                                        i += 1
                                        while i < len(lines) and not re.match(r"^\s*##\s+", lines[i]):
                                            i += 1
                                        continue
                                    out.append(line)
                                    i += 1
                                if not replaced:
                                    if out and out[-1].strip():
                                        out.append("")
                                    out.extend(["## Vibe", vibe])
                                return "\n".join(out).strip() + "\n"

                            def _soul_set_core(md: str, core_rules: list[str]) -> str:
                                cleaned: list[str] = []
                                for raw in core_rules or []:
                                    s = str(raw or "").strip()
                                    if not s:
                                        continue
                                    s = re.sub(r"^\s*\d+\s*[\.\)、]\s*", "", s).strip()
                                    if not s:
                                        continue
                                    if s.startswith("-"):
                                        s = s.lstrip("-").strip()
                                    if not s:
                                        continue
                                    if len(s) > 240:
                                        s = s[:240]
                                    if s not in cleaned:
                                        cleaned.append(s)
                                if not cleaned:
                                    return str(md or "").strip() + ("\n" if str(md or "").strip() else "")

                                s = str(md or "").strip()
                                if not s:
                                    s = "# SOUL.md - Who You Are\n\n## Core\n\n"

                                lines = s.splitlines()
                                core_titles = {"Core", "Core Truths"}
                                start = -1
                                for i, line in enumerate(lines):
                                    m = re.match(r"^\s*##\s+(.+?)\s*$", line)
                                    if not m:
                                        continue
                                    title = str(m.group(1) or "").strip()
                                    if title in core_titles:
                                        start = i
                                        break

                                def render_core_block() -> list[str]:
                                    out: list[str] = ["## Core"]
                                    for rule in cleaned:
                                        out.append(f"- {rule}")
                                    return out

                                if start != -1:
                                    end = len(lines)
                                    for j in range(start + 1, len(lines)):
                                        if re.match(r"^\s*##\s+", lines[j]):
                                            end = j
                                            break
                                    next_lines = lines[:start] + render_core_block() + [""] + lines[end:]
                                    return "\n".join(next_lines).strip() + "\n"

                                insert_at = 0
                                if lines and lines[0].lstrip().startswith("#"):
                                    insert_at = 1
                                    while insert_at < len(lines) and lines[insert_at].strip():
                                        insert_at += 1
                                    while insert_at < len(lines) and not lines[insert_at].strip():
                                        insert_at += 1
                                next_lines = lines[:insert_at] + ([""] if insert_at and lines[insert_at - 1].strip() else []) + render_core_block() + [""] + lines[insert_at:]
                                return "\n".join(next_lines).strip() + "\n"

                            mode = str(arguments.get("mode") or "apply").strip().lower()
                            if mode not in {"apply", "preview"}:
                                mode = "apply"

                            agent_obj = arguments.get("agent") if isinstance(arguments.get("agent"), dict) else {}
                            user_obj = arguments.get("user") if isinstance(arguments.get("user"), dict) else {}
                            soul_obj = arguments.get("soul") if isinstance(arguments.get("soul"), dict) else {}
                            agent_name = str(agent_obj.get("name") or "").strip()
                            agent_creature = str(agent_obj.get("creature") or "").strip()
                            agent_vibe = str(agent_obj.get("vibe") or "").strip()
                            agent_emoji = str(agent_obj.get("emoji") or "").strip()
                            user_name = str(user_obj.get("name") or "").strip()
                            user_addressing = str(user_obj.get("addressing") or "").strip()
                            soul_core_rules = soul_obj.get("core_rules") if isinstance(soul_obj.get("core_rules"), list) else None
                            soul_core_text = str(soul_obj.get("core_text") or "").strip()

                            identity_before = _storage_get_text(storage, identity_key).strip()
                            user_before = _storage_get_text(storage, user_key).strip()
                            soul_before = _storage_get_text(storage, soul_key).strip()

                            identity_after = identity_before
                            user_after = user_before
                            soul_after = soul_before
                            changed: list[str] = []

                            if agent_name:
                                identity_after = _md_set_field(
                                    identity_after, key="Name", value=agent_name, header="# IDENTITY.md - Who Am I?\n"
                                )
                                changed.append("agent.name")
                            if agent_creature:
                                identity_after = _md_set_field(
                                    identity_after, key="Creature", value=agent_creature, header="# IDENTITY.md - Who Am I?\n"
                                )
                                changed.append("agent.creature")
                            if agent_vibe:
                                identity_after = _md_set_field(
                                    identity_after, key="Vibe", value=agent_vibe, header="# IDENTITY.md - Who Am I?\n"
                                )
                                soul_after = _soul_set_vibe(soul_after, agent_vibe)
                                changed.append("agent.vibe")
                            if agent_emoji:
                                identity_after = _md_set_field(
                                    identity_after, key="Emoji", value=agent_emoji, header="# IDENTITY.md - Who Am I?\n"
                                )
                                changed.append("agent.emoji")
                            if user_name:
                                user_after = _md_set_field(
                                    user_after, key="Name", value=user_name, header="# USER.md - Who Are You?\n"
                                )
                                changed.append("user.name")
                            if user_addressing:
                                user_after = _md_set_field(
                                    user_after, key="Addressing", value=user_addressing, header="# USER.md - Who Are You?\n"
                                )
                                changed.append("user.addressing")
                            if isinstance(soul_core_rules, list) and soul_core_rules:
                                soul_after = _soul_set_core(soul_after, [str(x) for x in soul_core_rules])
                                changed.append("soul.core")
                            elif soul_core_text:
                                soul_after = _soul_set_core(soul_after, soul_core_text.splitlines())
                                changed.append("soul.core")

                            if mode == "apply" and changed:
                                _storage_set_text(storage, identity_key, identity_after)
                                _storage_set_text(storage, user_key, user_after)
                                if soul_after.strip():
                                    _storage_set_text(storage, soul_key, soul_after)
                                _storage_set_json(
                                    storage,
                                    onboarding_key,
                                    {"stage": 2, "completed": True, "updated_at": int(time.time())},
                                )
                                agent_tag_header = build_agent_tag_header(
                                    storage=storage, identity_key=identity_key, identity_md=identity_md
                                )

                            result = {
                                "ok": True,
                                "mode": mode,
                                "changed": changed,
                                "before": {
                                    "identity_md": identity_before,
                                    "user_md": user_before,
                                    "soul_md": soul_before,
                                },
                                "after": {
                                    "identity_md": identity_after,
                                    "user_md": user_after,
                                    "soul_md": soul_after,
                                },
                            }
                        elif tool_name == "write_temp_file":
                            result = runtime.write_temp_file(
                                str(arguments.get("relative_path") or ""),
                                str(arguments.get("content") or ""),
                            )
                        elif tool_name == "read_temp_file":
                            result = runtime.read_temp_file(
                                str(arguments.get("relative_path") or ""),
                                int(arguments.get("max_chars") or 12000),
                            )
                        elif tool_name == "list_temp_files":
                            result = runtime.list_temp_files(int(arguments.get("max_depth") or 4))
                        elif tool_name == "glob_temp_files":
                            result = runtime.glob_temp_files(
                                str(arguments.get("pattern") or ""),
                                int(arguments.get("max_results") or 200),
                            )
                        elif tool_name == "grep_temp_files":
                            result = runtime.grep_temp_files(
                                str(arguments.get("pattern") or ""),
                                str(arguments.get("glob") or "**/*"),
                                int(arguments.get("max_matches") or 200),
                            )
                        elif tool_name == "edit_temp_file":
                            result = runtime.edit_temp_file(
                                str(arguments.get("relative_path") or ""),
                                str(arguments.get("old_text") or ""),
                                str(arguments.get("new_text") or ""),
                                bool(arguments.get("replace_all") or False),
                            )
                        elif tool_name == "delete_temp_path":
                            result = runtime.delete_temp_path(
                                str(arguments.get("relative_path") or ""),
                                bool(arguments.get("recursive") or False),
                            )
                        elif tool_name == "run_temp_command":
                            requested_command = (
                                arguments.get("command") if isinstance(arguments.get("command"), list) else []
                            )
                            requested_auto_install = bool(arguments.get("auto_install") or False)
                            exe0 = str(requested_command[0] or "") if requested_command else ""
                            install_like_bins = {"pip", "npm", "npx", "bun", "uv", "uvx"}
                            if requested_auto_install or exe0 in install_like_bins:
                                result = {
                                    "error": "install_commands_disabled",
                                    "detail": "对话执行阶段禁止安装/包管理命令与 auto_install。请在“技能管理（TM）”中执行“依赖安装”补全 Python 依赖；JS/系统级依赖请联系管理员在 plugin_daemon 容器安装。",
                                    "command": requested_command,
                                    "auto_install": requested_auto_install,
                                }
                                _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                                messages.append(
                                    ToolPromptMessage(
                                        tool_call_id=str(call_id or ""),
                                        name=tool_name,
                                        content=json.dumps(result, ensure_ascii=False),
                                    )
                                )
                                messages.append(
                                    UserPromptMessage(
                                        content=(
                                            "你刚才尝试在对话中安装依赖/运行包管理命令，这是被禁止的。\n"
                                            "请改为：提示用户打开“技能管理（TM）”并执行“依赖安装”；如果是 JS/系统依赖（如 node/npm/agent-browser），提示联系管理员在 plugin_daemon 容器安装。\n"
                                            "然后继续使用已就绪的技能执行任务。"
                                        )
                                    )
                                )
                                continue
                            exec_override = None
                            if (
                                isinstance(exec_once_allowed, dict)
                                and str(exec_once_allowed.get("kind") or "") in {"exec_once", "exec"}
                                and str(exec_once_allowed.get("tool") or "") == tool_name
                                and isinstance(exec_once_allowed.get("command"), list)
                                and exec_once_allowed.get("command") == requested_command
                            ):
                                exec_override = {
                                    "exe": str(exec_once_allowed.get("exe") or exe0),
                                    "allow_not_in_allowlist": bool(exec_once_allowed.get("allow_not_in_allowlist") is True),
                                    "allow_untrusted_path": bool(exec_once_allowed.get("allow_untrusted_path") is True),
                                    "path_allowlist": exec_once_allowed.get("path_allowlist") if isinstance(exec_once_allowed.get("path_allowlist"), list) else [],
                                }
                            if exec_override is None:
                                exec_override = build_exec_override_from_grants(
                                    grants=grants,
                                    tool_name=tool_name,
                                    skill_name=None,
                                    requested_command=requested_command,
                                    exe0=exe0,
                                )
                            if not exec_approval_enabled:
                                exec_override = {"exe": exe0, "allow_not_in_allowlist": True}
                            result = runtime.run_temp_command(
                                command=requested_command,
                                cwd_relative=(
                                    str(arguments.get("cwd_relative")) if arguments.get("cwd_relative") else None
                                ),
                                auto_install=requested_auto_install,
                                exec_override=exec_override,
                            )
                            if (
                                isinstance(exec_once_allowed, dict)
                                and exec_override is not None
                                and isinstance(result, dict)
                                and result.get("error") is None
                                and result.get("returncode") is not None
                            ):
                                exec_once_allowed = None
                            if isinstance(result, dict):
                                err = str(result.get("error") or "").strip()
                                detail = str(result.get("detail") or "").strip()
                                if err.startswith("command not allowed:") and exec_override is None and exec_approval_enabled:
                                    allow_not_in_allowlist = True
                                    resolved_path = str(result.get("path") or result.get("resolved_exe") or "").strip()
                                    _storage_set_json(
                                        storage,
                                        approval_pending_key,
                                        {
                                            "pending": True,
                                            "kind": "exec",
                                            "tool": tool_name,
                                            "command": requested_command,
                                            "exe": exe0,
                                            "allow_not_in_allowlist": allow_not_in_allowlist,
                                            "path": resolved_path,
                                            "resolved_exe": resolved_path,
                                            "path_allowlist": ["*"],
                                            "original_query": effective_query,
                                            "created_at": int(time.time()),
                                        },
                                    )
                                    forced_text = (
                                        "该步骤需要执行一个未在允许列表的命令。\n"
                                        f"- 命令：{_shorten_text(requested_command, 200)}\n"
                                        + "为安全起见，需要你确认后才能继续。请回复序号：\n"
                                        "1) 允许一次（仅本次）\n"
                                        "2) 总是允许（后续不再提示）\n"
                                        "3) 拒绝\n"
                                    )
                                    break
                            if (
                                isinstance(result, dict)
                                and result.get("returncode") is not None
                                and int(result.get("returncode") or 0) != 0
                            ):
                                stderr = str(result.get("stderr") or "").strip()
                                if stderr:
                                    yield self.create_text_message(
                                        "❌命令执行失败（stderr）：\n"
                                        + _shorten_text(
                                            redact_user_visible_text(
                                                text=stderr, session_dir=session_dir, skills_root=skills_root
                                            ),
                                            1200,
                                        )
                                        + "\n"
                                    )
                        elif tool_name == "export_temp_file":
                            temp_rel = str(arguments.get("temp_relative_path") or "")
                            workspace_rel = str(arguments.get("workspace_relative_path") or "")
                            reserved = {"identity.md", "user.md", "soul.md", "memory.md"}
                            ws_norm = workspace_rel.replace("\\", "/").lstrip("/").strip()
                            ws_base = os.path.basename(ws_norm).lower() if ws_norm else ""
                            if ws_base in reserved or ws_norm.lower().startswith("memory/"):
                                result = {
                                    "error": "reserved_export_name",
                                    "detail": "不允许导出/交付系统人格与记忆文件（IDENTITY/USER/SOUL/MEMORY 或 memory/*.md）。请改用 update_persona 更新设定。",
                                    "requested_name": workspace_rel,
                                }
                                messages.append(
                                    ToolPromptMessage(
                                        tool_call_id=str(call_id or ""),
                                        name=tool_name,
                                        content=json.dumps(result, ensure_ascii=False),
                                    )
                                )
                                messages.append(
                                    UserPromptMessage(
                                        content=(
                                            "你刚才尝试把系统人格/记忆文件当作交付物导出，这是不允许的。\n"
                                            "规则：IDENTITY.md/USER.md/SOUL.md/MEMORY.md/memory/*.md 只能用 update_persona 更新，不能 export_temp_file。\n"
                                            "请改用 update_persona 精确更新相应字段，然后用文本确认即可。"
                                        )
                                    )
                                )
                                continue
                            result = runtime.export_temp_file(
                                temp_relative_path=temp_rel,
                                workspace_relative_path=workspace_rel,
                                overwrite=bool(arguments.get("overwrite") or False),
                            )
                            out_name = os.path.basename(workspace_rel) if workspace_rel else ""
                            if (
                                isinstance(result, dict)
                                and not result.get("error")
                                and temp_rel
                                and out_name
                            ):
                                final_file_meta[temp_rel] = {
                                    **(final_file_meta.get(temp_rel) or {}),
                                    "filename": out_name,
                                    "mime_type": _guess_mime_type(out_name),
                                }
                        elif tool_name == "web_fetch":
                            result = runtime.web_fetch(
                                str(arguments.get("url") or ""),
                                str(arguments.get("extract_mode") or "markdown"),
                                int(arguments.get("max_chars") or 50000),
                            )
                        else:
                            result = {"error": f"unknown tool: {tool_name}"}

                        _dbg(f"tool_result name={tool_name} result={_shorten_text(result, 700)}")
                        if loop_detection:
                            try:
                                rh = hashlib.sha1(
                                    json.dumps(result, ensure_ascii=False, sort_keys=True).encode("utf-8", errors="ignore")
                                ).hexdigest()
                            except Exception:
                                rh = str(len(str(result)))
                            tool_call_sig_history.append(tool_sig)
                            tool_call_sig_result_history.append((tool_sig, rh))
                            if len(tool_call_sig_history) > 200:
                                tool_call_sig_history = tool_call_sig_history[-200:]
                            if len(tool_call_sig_result_history) > 200:
                                tool_call_sig_result_history = tool_call_sig_result_history[-200:]
                            recent_pairs = tool_call_sig_result_history[-loop_global_no_progress_threshold:]
                            if (
                                loop_global_no_progress_threshold > 0
                                and len(recent_pairs) >= loop_global_no_progress_threshold
                                and len(set(recent_pairs)) == 1
                            ):
                                forced_text = (
                                    "❌检测到持续无进展的工具循环（同一调用与结果反复出现），已提前停止。\n"
                                    f"- tool: {tool_name}\n"
                                    "建议：变更参数/切换工具/增加读文件或列目录步骤，或先交付当前阶段产物。"
                                )
                                break
                        messages.append(
                            ToolPromptMessage(
                                tool_call_id=str(call_id or ""),
                                name=tool_name,
                                content=json.dumps(result, ensure_ascii=False),
                            )
                        )
                    if forced_text:
                        final_text = forced_text
                        break
                    if step_idx >= max_tool_turns - 1:
                        try:
                            has_files = any(
                                e.get("type") == "file"
                                for e in _list_dir(session_dir, max_depth=2)
                                if isinstance(e, dict)
                            )
                        except Exception:
                            has_files = False
                        if final_file_meta or has_files:
                            final_text = "已生成文件。"
                            break
                    continue

                if not res_text and not nontext:
                    empty_responses += 1
                    _dbg(f"empty_response_count={empty_responses}")
                    if empty_responses < 3:
                        messages.append(
                            UserPromptMessage(
                                content="你刚才没有输出任何内容。请继续完成任务：如果需要使用工具，请以 function call 发起工具调用；若无需工具，请直接给出最终答复。"
                            )
                        )
                        continue
                    final_text = "模型连续返回空响应，未生成任何结果。"
                    break

                if res_text:
                    final_text = res_text
                    _dbg(f"final_text content_len={len(final_text)}")
                    if streamed_any and final_text:
                        final_text_already_streamed = True
                    break

                if nontext:
                    excerpt = ""
                    try:
                        excerpt = _shorten_text(json.dumps(nontext, ensure_ascii=False), 800)
                    except Exception:
                        excerpt = _shorten_text(nontext, 800)
                    final_text = (
                        "❌ 大模型返回了非文本内容，无法生成可读答复。\n"
                        "返回内容（截断）：\n"
                        + str(excerpt or "")
                        + "\n\n请检查：\n"
                        + "1) 模型是否欠费/余额不足\n"
                        + "2) API Key/权限是否正确（是否有该模型调用权限）\n"
                        + "3) 网络出站/代理/DNS 是否受限\n"
                        + "4) 模型供应商服务是否异常\n"
                    )
                    break

                final_text = "未生成任何文本或文件输出。"
                break
            else:
                try:
                    has_files = any(
                        e.get("type") == "file" for e in _list_dir(session_dir, max_depth=2) if isinstance(e, dict)
                    )
                except Exception:
                    has_files = False
                if final_file_meta or has_files:
                    final_text = "已生成文件。"
                else:
                    final_text = (
                        f"❌超过最大执行轮数 max_tool_turns={max_tool_turns}，仍未得到最终结果。\n"
                        f"可尝试：提高 timeout_seconds（当前 {timeout_seconds} 秒）或检查是否陷入重复工具调用。"
                    )
        finally:
            temp_files_text = ""
            try:
                temp_entries = _list_dir(session_dir, max_depth=10)
                rel_paths = [
                    str(e.get("relative_path"))
                    for e in temp_entries
                    if e.get("type") == "file" and isinstance(e.get("relative_path"), str)
                ]
                if rel_paths:
                    temp_files_text = "\n\n[temp_files]\n" + "\n".join(rel_paths)
                _dbg(f"temp_files_count={len(rel_paths)}")
            except Exception:
                temp_files_text = ""

            files_to_send: list[tuple[str, str, str, str]] = []
            try:
                for rel, meta_override in (final_file_meta or {}).items():
                    if not rel or not isinstance(rel, str):
                        continue
                    rel_norm = rel.replace("\\", "/").lstrip("/")
                    if not rel_norm:
                        continue
                    try:
                        path = _safe_join(session_dir, rel_norm)
                    except Exception:
                        continue
                    if not os.path.isfile(path):
                        continue
                    filename = os.path.basename(rel_norm)
                    out_name = (meta_override.get("filename") if isinstance(meta_override, dict) else None) or filename
                    mime_type = (meta_override.get("mime_type") if isinstance(meta_override, dict) else None) or _guess_mime_type(out_name or filename)
                    files_to_send.append((rel_norm, path, mime_type, out_name))
            except Exception:
                files_to_send = []

            has_any_files = False
            try:
                temp_entries = _list_dir(session_dir, max_depth=10)
                has_any_files = any(e.get("type") == "file" for e in temp_entries if isinstance(e, dict))
            except Exception:
                has_any_files = False

            assistant_text_for_history = ""
            if final_text and final_text.strip():
                if not files_to_send and final_text.strip() == "已生成文件。":
                    final_text = "已生成中间文件，但未调用 export_temp_file 标记交付文件。"
                assistant_text_for_history = final_text.strip()
                _append_history_turn(
                    storage,
                    history_key=history_key,
                    user_text=user_input,
                    assistant_text=assistant_text_for_history,
                )
                if not final_text_already_streamed:
                    yield from stream_text_to_user(create_text_message=self.create_text_message, text=final_text)
            elif files_to_send:
                assistant_text_for_history = "已生成文件。"
                _append_history_turn(
                    storage,
                    history_key=history_key,
                    user_text=user_input,
                    assistant_text=assistant_text_for_history,
                )
                yield from stream_text_to_user(create_text_message=self.create_text_message, text="已生成文件。")
            elif has_any_files:
                assistant_text_for_history = "已生成中间文件，但未调用 export_temp_file 标记交付文件。"
                _append_history_turn(
                    storage,
                    history_key=history_key,
                    user_text=user_input,
                    assistant_text=assistant_text_for_history,
                )
                yield from stream_text_to_user(
                    create_text_message=self.create_text_message,
                    text="已生成中间文件，但未调用 export_temp_file 标记交付文件。",
                )
            else:
                assistant_text_for_history = "未生成任何文本或文件输出。"
                _append_history_turn(
                    storage,
                    history_key=history_key,
                    user_text=user_input,
                    assistant_text=assistant_text_for_history,
                )
                yield from stream_text_to_user(
                    create_text_message=self.create_text_message,
                    text="未生成任何文本或文件输出。",
                )

            payload = usage.payload()
            yield self.create_variable_message("llm_usage", payload)
            if show_usage_text:
                yield self.create_text_message(usage.format_text(payload))

            try:
                if should_write_daily(
                    DailyWriteContext(
                        user_id=user_id,
                        user_text=str(user_input or ""),
                        assistant_text=str(assistant_text_for_history or ""),
                        approval_pending=bool(approval_pending),
                        approval_context=str(approval_context or ""),
                    )
                ):
                    _append_daily_dialogue(
                        storage=storage,
                        session=self.session,
                        user_id=user_id,
                        user_text=user_input,
                        assistant_text=assistant_text_for_history,
                        keep_days=30,
                    )
            except Exception:
                pass

            yielded: set[str] = set()
            yielded_fingerprints: set[str] = set()
            for rel, path, mime_type, out_name in files_to_send:
                if rel in yielded:
                    continue
                yielded.add(rel)
                try:
                    with open(path, "rb") as fp:
                        content = fp.read()
                    try:
                        content_fp = hashlib.sha1(content).hexdigest()
                    except Exception:
                        content_fp = str(len(content))
                    fingerprint_key = f"{out_name}|{mime_type}|{content_fp}"
                    if fingerprint_key in yielded_fingerprints:
                        continue
                    yielded_fingerprints.add(fingerprint_key)
                    yield self.create_blob_message(blob=content, meta={"mime_type": mime_type, "filename": out_name})
                except Exception:
                    continue
            _dbg(f"temp_retained session_dir={session_dir}")
