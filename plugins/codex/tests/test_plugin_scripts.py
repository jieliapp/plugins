import base64
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SCRIPTS = str(PLUGIN_ROOT / "scripts")
SCRIPT_MODULES = ("sync", "commit_trailer", "read_thread", "redact", "jieli_config", "handoff_info")


def use_plugin_scripts() -> None:
    for module_name in SCRIPT_MODULES:
        sys.modules.pop(module_name, None)
    if PLUGIN_SCRIPTS in sys.path:
        sys.path.remove(PLUGIN_SCRIPTS)
    sys.path.insert(0, PLUGIN_SCRIPTS)


use_plugin_scripts()


class PluginScriptTestCase(unittest.TestCase):
    def setUp(self):
        use_plugin_scripts()


class CodexSyncScriptTests(PluginScriptTestCase):
    def test_redact_text_removes_jieli_api_key(self):
        from redact import redact_text

        redacted = redact_text("config jieli_uTN9dHsMCoOgMPBLRnQq_1JkfimaKU2ZfP")

        self.assertIn("[REDACTED:jieli-api-key]", redacted)
        self.assertNotIn("jieli_uTN9dHsMCoOgMPBLRnQq_1JkfimaKU2ZfP", redacted)

    def test_build_payload_from_codex_jsonl_redacts_and_skips_private_items(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "rollout-2026-06-08T00-00-00-codex-1.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "timestamp": "2026-06-08T00:00:00.000Z",
                                "payload": {
                                    "id": "codex-1",
                                    "cwd": "/Users/alice/work/jieli",
                                    "git": {"branch": "plugin/codex"},
                                    "base_instructions": "do not upload this",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "turn_context",
                                "payload": {
                                    "model": "gpt-5.5",
                                    "cwd": "/Users/alice/work/jieli",
                                    "summary": "hidden summary",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "developer",
                                    "content": [{"type": "input_text", "text": "developer instructions"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "timestamp": "2026-06-08T00:00:01.000Z",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "sync OPENAI_API_KEY=sk-ant-secret-value"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "reasoning",
                                    "encrypted_content": "encrypted",
                                    "summary": [{"text": "private reasoning"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "timestamp": "2026-06-08T00:00:02.000Z",
                                "payload": {
                                    "type": "message",
                                    "role": "assistant",
                                    "phase": "final",
                                    "content": [{"type": "output_text", "text": "done Authorization: Bearer abc.def.ghi"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call",
                                    "call_id": "call-1",
                                    "name": "exec_command",
                                    "arguments": json.dumps({"cmd": "git status", "token": "tool-secret"}),
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call_output",
                                    "call_id": "call-1",
                                    "output": "Authorization: Bearer tool.secret",
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {
                    "session_id": "codex-1",
                    "transcript_path": str(transcript),
                    "cwd": "/Users/alice/work/jieli",
                },
                base_url="https://jieli.example.test",
            )

        self.assertEqual(payload["provider"], "codex")
        self.assertEqual(payload["labels"], [])
        self.assertEqual(payload["repo"], "work/jieli")
        self.assertEqual(payload["branch"], "plugin/codex")
        self.assertEqual(payload["source_url"], "https://jieli.example.test/threads/T-codex-1")
        self.assertEqual(payload["thread"]["id"], "T-codex-1")
        self.assertEqual(payload["thread"]["model"], "gpt-5.5")
        self.assertEqual(payload["thread"]["title"], "sync OPENAI_API_KEY=[REDACTED:openai-api-key]")
        self.assertEqual([message["role"] for message in payload["thread"]["messages"]], ["user", "assistant", "assistant", "tool"])
        tool_use = payload["thread"]["messages"][2]["content"][0]
        self.assertEqual(tool_use["type"], "tool_use")
        self.assertEqual(tool_use["name"], "shell_command")
        self.assertEqual(tool_use["input"], {"command": "git status", "cwd": ""})
        tool_result = payload["thread"]["messages"][3]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["run"]["result"]["output"], "Authorization: Bearer [REDACTED:authorization-bearer]")
        raw_payload = json.dumps(payload, sort_keys=True)
        self.assertIn("[REDACTED:", raw_payload)
        self.assertNotIn("sk-ant-secret-value", raw_payload)
        self.assertNotIn("abc.def.ghi", raw_payload)
        self.assertNotIn("tool.secret", raw_payload)
        self.assertNotIn("tool-secret", raw_payload)
        self.assertNotIn("developer instructions", raw_payload)
        self.assertNotIn("private reasoning", raw_payload)
        self.assertNotIn("encrypted", raw_payload)

    def test_build_payload_uses_transcript_session_id_before_hook_session_id(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "rollout-2026-06-08T00-00-00-stable-id.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "timestamp": "2026-06-08T00:00:00.000Z",
                                "payload": {
                                    "id": "stable-transcript-id",
                                    "cwd": "/Users/alice/work/jieli",
                                    "git": {"branch": "plugin/codex"},
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "timestamp": "2026-06-08T00:00:01.000Z",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "hello"}],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {
                    "session_id": "hook-rotated-id",
                    "transcript_path": str(transcript),
                },
                base_url="https://jieli.example.test",
            )

        self.assertEqual(payload["thread"]["id"], "T-stable-transcript-id")
        self.assertEqual(payload["source_url"], "https://jieli.example.test/threads/T-stable-transcript-id")

    def test_build_payload_normalizes_custom_apply_patch_calls_for_jieli(self):
        from sync import build_payload_from_hook

        patch_text = """*** Begin Patch
*** Update File: route_test.go
@@
-old
+new
*** End Patch
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {"id": "codex-custom-tool", "cwd": "/Users/alice/work/jieli"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "fix route test"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "custom_tool_call",
                                    "status": "completed",
                                    "call_id": "call-patch",
                                    "name": "apply_patch",
                                    "input": patch_text,
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "custom_tool_call_output",
                                    "call_id": "call-patch",
                                    "output": "Exit code: 0\nOutput:\nSuccess. Updated route_test.go\n",
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-custom-tool", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
            )

        self.assertEqual([message["role"] for message in payload["thread"]["messages"]], ["user", "assistant", "tool"])
        tool_use = payload["thread"]["messages"][1]["content"][0]
        self.assertEqual(tool_use["type"], "tool_use")
        self.assertEqual(tool_use["id"], "call-patch")
        self.assertEqual(tool_use["name"], "apply_patch")
        self.assertEqual(tool_use["input"], {"patch_text": patch_text})
        tool_result = payload["thread"]["messages"][2]["content"][0]
        self.assertEqual(tool_result["type"], "tool_result")
        self.assertEqual(tool_result["tool_use_id"], "call-patch")
        self.assertIn("Success. Updated route_test.go", tool_result["content"])

    def test_build_payload_replaces_handoff_summary_with_placeholder(self):
        from sync import COMPACTION_PLACEHOLDER, build_payload_from_hook

        long_summary = "**Handoff Summary**\n\n**Current task**\n" + "do not upload this summary\n" * 500
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"id": "codex-compact", "cwd": "/Users/alice/work/jieli"}}),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "原始第一条消息"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "assistant",
                                    "content": [{"type": "output_text", "text": long_summary}],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-compact", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
            )

        messages = payload["thread"]["messages"]
        self.assertEqual(messages[1]["content"], COMPACTION_PLACEHOLDER)
        self.assertEqual(payload["thread"]["title"], "原始第一条消息")
        self.assertNotIn("do not upload this summary", json.dumps(payload, ensure_ascii=False))

    def test_build_payload_hides_codex_git_directives(self):
        from sync import build_payload_from_hook

        final_text = """已提交：`abc1234 fix sync`

::git-stage{cwd="/Users/alice/work/jieli"}
::git-commit{cwd="/Users/alice/work/jieli"}
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "codex-git-directives", "cwd": "/Users/alice/work/jieli"}})
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": final_text}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-git-directives", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
            )

        content = payload["thread"]["messages"][0]["content"]
        self.assertEqual(content, "已提交：`abc1234 fix sync`")
        self.assertNotIn("::git-stage", json.dumps(payload, ensure_ascii=False))
        self.assertNotIn("::git-commit", json.dumps(payload, ensure_ascii=False))

    def test_build_payload_maps_exec_command_to_terminal_tool_shape(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"id": "codex-terminal", "cwd": "/Users/alice/work/jieli"}}),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call",
                                    "call_id": "call-terminal",
                                    "name": "exec_command",
                                    "arguments": json.dumps(
                                        {
                                            "cmd": "git status --short --branch",
                                            "workdir": "/Users/alice/work/jieli",
                                            "yield_time_ms": 10000,
                                            "max_output_tokens": 4000,
                                        }
                                    ),
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call_output",
                                    "call_id": "call-terminal",
                                    "output": "Chunk ID: abc\nProcess exited with code 0\nOutput:\n## main\n",
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-terminal", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
            )

        tool_use = payload["thread"]["messages"][0]["content"][0]
        self.assertEqual(tool_use["name"], "shell_command")
        self.assertEqual(tool_use["input"], {"command": "git status --short --branch", "cwd": "/Users/alice/work/jieli"})
        tool_result = payload["thread"]["messages"][1]["content"][0]
        self.assertEqual(tool_result["content"], "Chunk ID: abc\nProcess exited with code 0\nOutput:\n## main\n")
        self.assertEqual(tool_result["run"]["status"], "completed")
        self.assertEqual(tool_result["run"]["result"]["exitCode"], 0)
        self.assertEqual(tool_result["run"]["result"]["output"], "Chunk ID: abc\nProcess exited with code 0\nOutput:\n## main\n")

    def test_build_payload_marks_nonzero_tool_exit_as_error(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call",
                                    "call_id": "call-fail",
                                    "name": "shell_command",
                                    "arguments": json.dumps({"cmd": "exit 1"}),
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "function_call_output",
                                    "call_id": "call-fail",
                                    "output": "Process exited with code 1\nOutput:\n",
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-fail", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
            )

        tool_result = payload["thread"]["messages"][1]["content"][0]
        self.assertEqual(tool_result["run"]["status"], "error")
        self.assertEqual(tool_result["run"]["result"]["exitCode"], 1)

    def test_build_payload_skips_codex_internal_context_messages(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {"id": "codex-internal", "cwd": "/Users/alice/work/jieli"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "<codex_internal_context>resume</codex_internal_context>"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "real request"}],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-internal", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
            )

        self.assertEqual(len(payload["thread"]["messages"]), 1)
        self.assertEqual(payload["thread"]["messages"][0]["content"], "real request")
        self.assertEqual(payload["thread"]["title"], "real request")

    def test_build_payload_skips_loaded_agents_instructions_block(self):
        from sync import build_payload_from_hook

        agents_block = """# AGENTS.md instructions for /Users/alice/work/jieli <INSTRUCTIONS> #

<INSTRUCTIONS>
# AI AGENT PROTOCOLS v2.0

## 1. Operating Modes
Default State: ANSWER
</INSTRUCTIONS>"""
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "session_meta",
                                "payload": {"id": "codex-agents", "cwd": "/Users/alice/work/jieli"},
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": agents_block}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "actual request"}],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-agents", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
            )

        raw_payload = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(len(payload["thread"]["messages"]), 1)
        self.assertEqual(payload["thread"]["messages"][0]["content"], "actual request")
        self.assertEqual(payload["thread"]["title"], "actual request")
        self.assertNotIn("AI AGENT PROTOCOLS", raw_payload)

    def test_build_payload_skips_loaded_skill_content_block(self):
        from sync import build_payload_from_hook

        skill_block = """<skill>
<name>claude-code-setup:spec-driven-planning</name>
<path>/Users/alice/Library/Mobile Documents/com~apple~CloudDocs/dotfiles/config/claude/skills/spec-driven-planning/SKILL.md</path>
---
name: spec-driven-planning
description: Use this skill whenever the user asks for a spec.
---

# Spec-Driven Planning
Do not upload this loaded skill body.
</skill>"""
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"id": "codex-skill", "cwd": "/Users/alice/work/jieli"}}),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": skill_block}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": "actual request"}],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-skill", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
            )

        raw_payload = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(len(payload["thread"]["messages"]), 1)
        self.assertEqual(payload["thread"]["messages"][0]["content"], "actual request")
        self.assertNotIn("Spec-Driven Planning", raw_payload)
        self.assertNotIn("SKILL.md", raw_payload)

    def test_build_payload_strips_codex_user_file_mentions_prefix(self):
        from sync import build_payload_from_hook

        user_text = """# Files mentioned by the user:

## codex-clipboard-ba43ae37-fd1b-46d2-98dc-a3f3e3c14b3e.png: /var/folders/2s/q99hddf140l937t46th4b91w0000gn/T/codex-clipboard-ba43ae37-fd1b-46d2-98dc-a3f3e3c14b3e.png

## My request for Codex:
threads list, hidden branch name, just show repo"""
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "session_meta", "payload": {"id": "codex-file-prefix", "cwd": "/Users/alice/work/jieli"}}),
                        json.dumps(
                            {
                                "type": "response_item",
                                "payload": {
                                    "type": "message",
                                    "role": "user",
                                    "content": [{"type": "input_text", "text": user_text}],
                                },
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-file-prefix", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
            )

        raw_payload = json.dumps(payload, ensure_ascii=False)
        self.assertEqual(payload["thread"]["messages"][0]["content"], "threads list, hidden branch name, just show repo")
        self.assertEqual(payload["thread"]["title"], "threads list, hidden branch name, just show repo")
        self.assertNotIn("Files mentioned by the user", raw_payload)
        self.assertNotIn("codex-clipboard-ba43ae37", raw_payload)
        self.assertNotIn("/var/folders/", raw_payload)

    def test_build_payload_rewrites_local_markdown_link_targets_as_file_urls(self):
        from sync import build_payload_from_hook

        message = "use [$claude-code-setup:spec-driven-planning](/Users/alice/Library/Mobile Documents/com~apple~CloudDocs/dotfiles/config/claude/skills/spec-driven-planning/SKILL.md)"
        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "codex-link", "cwd": "/Users/alice/work/jieli"}})
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [{"type": "input_text", "text": message}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-link", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
            )

        content = payload["thread"]["messages"][0]["content"]
        self.assertEqual(
            content,
            "use [$claude-code-setup:spec-driven-planning](file:///Users/alice/Library/Mobile%20Documents/com~apple~CloudDocs/dotfiles/config/claude/skills/spec-driven-planning/SKILL.md)",
        )

    def test_build_payload_includes_raw_repo_url_from_git_remote(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            repo.mkdir()
            subprocess.run(["git", "init"], cwd=repo, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(
                ["git", "remote", "add", "origin", "git@home.pika12.com:guoyb/jieli.git"],
                cwd=repo,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "codex-repo-url", "cwd": str(repo)}})
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "sync repo url"}]},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-repo-url", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
            )

        self.assertEqual(payload["repo"], "")
        self.assertEqual(payload["repo_url"], "git@home.pika12.com:guoyb/jieli.git")

    def test_build_payload_uploads_codex_input_image_data_urls(self):
        from sync import build_payload_from_hook

        uploaded: list[tuple[bytes, str]] = []
        image_data = b"\x89PNG\r\n\x1a\n\x00\x00"
        image_url = "data:image/png;base64," + base64.b64encode(image_data).decode("ascii")

        def fake_upload(data: bytes, media_type: str) -> str:
            uploaded.append((data, media_type))
            return "https://jieli.example.test/attachments/img.png"

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "codex-image-data", "cwd": "/Users/alice/work/jieli"}})
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {
                            "type": "message",
                            "role": "user",
                            "content": [
                                {"type": "input_text", "text": "<image name=[Image #1]>"},
                                {"type": "input_image", "image_url": image_url, "detail": "high"},
                                {"type": "input_text", "text": "</image>"},
                                {"type": "input_text", "text": "[Image #1] what is this?"},
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-image-data", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
                image_uploader=lambda path: "unused",
                data_image_uploader=fake_upload,
            )

        self.assertEqual(uploaded, [(image_data, "image/png")])
        self.assertEqual(
            payload["thread"]["messages"][0]["content"],
            [
                {"type": "image", "source": {"url": "https://jieli.example.test/attachments/img.png", "type": "image/png"}},
                {"type": "text", "text": "[Image #1] what is this?"},
            ],
        )

    def test_build_payload_uploads_codex_local_image_events_when_response_item_is_missing(self):
        from sync import build_payload_from_hook

        uploaded_paths: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "1.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "codex-local-image", "cwd": "/Users/alice/work/jieli"}})
                + "\n"
                + json.dumps(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "ok [Image #1] what is this?",
                            "images": [],
                            "local_images": [str(image_path)],
                            "text_elements": [{"placeholder": "[Image #1]"}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {"session_id": "codex-local-image", "transcript_path": str(transcript)},
                base_url="https://jieli.example.test",
                image_uploader=lambda path: uploaded_paths.append(str(path)) or "https://jieli.example.test/attachments/img.png",
            )

        self.assertEqual(uploaded_paths, [str(image_path)])
        self.assertEqual(
            payload["thread"]["messages"][0]["content"],
            [
                {"type": "text", "text": "ok [Image #1] what is this?"},
                {"type": "image", "source": {"url": "https://jieli.example.test/attachments/img.png", "type": "image/png"}},
            ],
        )

    def test_upload_attachment_cached_reuses_successful_upload_by_content_hash(self):
        import sync

        calls: list[tuple[bytes, str]] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            image_path = Path(tmpdir) / "1.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
            original_upload = sync.upload_attachment_data

            try:
                sync.upload_attachment_data = lambda data, media_type, base_url, api_key: calls.append((data, media_type)) or "https://jieli.example.test/attachments/img.png"
                first = sync.upload_attachment_cached(image_path, "https://jieli.example.test/", "secret", home=home)
                second = sync.upload_attachment_cached(image_path, "https://jieli.example.test/", "secret", home=home)
            finally:
                sync.upload_attachment_data = original_upload

        self.assertEqual(first, "https://jieli.example.test/attachments/img.png")
        self.assertEqual(second, "https://jieli.example.test/attachments/img.png")
        self.assertEqual(calls, [(b"\x89PNG\r\n\x1a\n\x00\x00", "image/png")])

    def test_find_session_transcript_uses_codex_home_sessions(self):
        from sync import find_session_transcript

        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex-home"
            session_dir = codex_home / "sessions" / "2026" / "06" / "08"
            session_dir.mkdir(parents=True)
            transcript = session_dir / "rollout-2026-06-08T00-00-00-find-me.jsonl"
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "find-me"}}) + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=True):
                self.assertEqual(find_session_transcript("find-me"), transcript)

    def test_find_session_transcript_falls_back_to_jsonl_content_scan(self):
        from sync import find_session_transcript

        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex-home"
            session_dir = codex_home / "sessions" / "2026" / "06" / "08"
            session_dir.mkdir(parents=True)
            transcript = session_dir / "rollout-2026-06-08T00-00-00-random.jsonl"
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "content-only"}}) + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=True):
                self.assertEqual(find_session_transcript("content-only"), transcript)

    def test_missing_config_response_is_visible_on_stop(self):
        from sync import build_missing_config_hook_response, missing_config_vars

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(Path, "home", return_value=Path(tmpdir)):
            missing = missing_config_vars({})
            response = build_missing_config_hook_response("stop", missing)

        self.assertTrue(response["continue"])
        self.assertIn("systemMessage", response)
        self.assertIn("settings.json", response["systemMessage"])
        self.assertIn("https://jieli.app", response["systemMessage"])
        self.assertIn("create an API key", response["systemMessage"])
        self.assertIn("chmod it to 600", response["systemMessage"])

    def test_missing_config_response_is_quiet_on_user_prompt_submit(self):
        from sync import build_missing_config_hook_response

        self.assertEqual(build_missing_config_hook_response("userpromptsubmit", ["JIELI_API_KEY"]), {})

    def test_settings_file_satisfies_missing_config_and_base_url(self):
        import jieli_config
        from sync import build_payload_from_hook, missing_config_vars, required_env

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            settings_path = home / ".config" / "jieli" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps({"api_key": "jieli-settings-key", "base_url": "https://jieli.example.test"}),
                encoding="utf-8",
            )
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": "codex-settings", "cwd": "/Users/alice/work/jieli"}})
                + "\n"
                + json.dumps(
                    {
                        "type": "response_item",
                        "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hello"}]},
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.object(Path, "home", return_value=home), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(missing_config_vars({}), [])
                self.assertEqual(required_env("JIELI_API_KEY"), "jieli-settings-key")
                self.assertEqual(jieli_config.get_base_url(), "https://jieli.example.test")
                payload = build_payload_from_hook({"session_id": "codex-settings", "transcript_path": str(transcript)})

        self.assertEqual(payload["source_url"], "https://jieli.example.test/threads/T-codex-settings")

    def test_environment_overrides_settings_file(self):
        import jieli_config

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            settings_path = home / ".config" / "jieli" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps({"api_key": "settings-key", "base_url": "https://settings.example.test"}),
                encoding="utf-8",
            )

            env = {"JIELI_API_KEY": "env-key", "JIELI_BASE_URL": "https://env.example.test/"}
            self.assertEqual(jieli_config.get_api_key(env, home=home), "env-key")
            self.assertEqual(jieli_config.get_base_url(env, home=home), "https://env.example.test")

    def test_upload_payload_posts_to_plugin_endpoint(self):
        from sync import upload_payload

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return b'{"success":true}'

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            result = upload_payload({"provider": "codex"}, "https://jieli.example.test/", "secret")

        self.assertEqual(captured["url"], "https://jieli.example.test/plugin/threads/upload")
        self.assertEqual(captured["timeout"], 20)
        self.assertEqual(result["success"], True)

    def test_format_hook_error_includes_redacted_http_body(self):
        from sync import format_hook_error
        import urllib.error

        error = urllib.error.HTTPError(
            "https://jieli.example.test/plugin/threads/upload",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"unsupported provider","api_key":"jieli_uTN9dHsMCoOgMPBLRnQq_1JkfimaKU2ZfP"}'),
        )

        try:
            formatted = format_hook_error(error)
        finally:
            error.close()

        self.assertIn("HTTPError: HTTP Error 400: Bad Request", formatted)
        self.assertIn("unsupported provider", formatted)
        self.assertIn("[REDACTED:jieli-api-key]", formatted)
        self.assertNotIn("jieli_uTN9dHsMCoOgMPBLRnQq_1JkfimaKU2ZfP", formatted)

    def test_sync_lock_is_scoped_per_session(self):
        from sync import SyncLock

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            with SyncLock(home=home, session_id="sess-A") as lock_a:
                self.assertTrue(lock_a.acquired)
                with SyncLock(home=home, session_id="sess-B") as lock_b:
                    self.assertTrue(lock_b.acquired)
                with SyncLock(home=home, session_id="sess-A") as lock_a2:
                    self.assertFalse(lock_a2.acquired)

    def test_wait_for_transcript_flush_waits_for_pending_jsonl_append(self):
        from sync import wait_for_transcript_flush

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text("first\n", encoding="utf-8")

            def append_later():
                time.sleep(0.01)
                with transcript.open("a", encoding="utf-8") as handle:
                    handle.write("second\n")

            writer = threading.Thread(target=append_later)
            writer.start()
            wait_for_transcript_flush(transcript, quiet_seconds=0.03, timeout_seconds=0.5)
            self.assertEqual(transcript.read_text(encoding="utf-8"), "first\nsecond\n")
            writer.join(timeout=1)

    def test_write_session_mapping_records_session_path_and_chmods_file(self):
        from sync import write_session_mapping

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            write_session_mapping(
                "codex-map",
                "https://jieli.example.test/",
                home=home,
                provider_thread_id="T-codex-map",
                session_path="/tmp/session.jsonl",
            )

            path = home / ".jieli" / "codex-sessions.json"
            mapping = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(mapping["codex-map"]["provider_thread_id"], "T-codex-map")
            self.assertEqual(mapping["codex-map"]["base_url"], "https://jieli.example.test")
            self.assertEqual(mapping["codex-map"]["session_path"], "/tmp/session.jsonl")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_main_skips_silently_when_transcript_not_flushed_yet(self):
        from sync import main

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            missing_transcript = home / "sessions" / "missing.jsonl"
            stdin = io.StringIO(
                json.dumps(
                    {
                        "transcript_path": str(missing_transcript),
                        "session_id": "codex-not-flushed",
                        "cwd": "/Users/alice/work/jieli",
                    }
                )
            )
            with (
                patch.dict(os.environ, {"JIELI_API_KEY": "secret"}, clear=True),
                patch.object(sys, "argv", ["sync.py", "--trigger", "sessionstart", "--jieli-hook"]),
                patch("sys.stdin", stdin),
                patch.object(Path, "home", return_value=home),
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertFalse((home / ".jieli" / "hooks.log").exists())


class CodexHandoffInfoTests(PluginScriptTestCase):
    def test_main_prefers_rollout_session_meta_id_over_hook_session_id(self):
        from handoff_info import main

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "rollout.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "session_meta",
                        "payload": {
                            "id": "stable-codex-id",
                            "cwd": tmpdir,
                            "git": {"branch": "feature/handoff"},
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            context = {"session_id": "hook-id", "transcript_path": str(transcript), "cwd": "/wrong"}
            encoded = base64.b64encode(json.dumps(context).encode("utf-8")).decode("ascii")
            stdout = io.StringIO()
            with (
                patch.dict(os.environ, {"JIELI_HANDOFF_CONTEXT_B64": encoded, "JIELI_BASE_URL": "https://jieli.example.test"}, clear=True),
                patch.object(sys, "argv", ["handoff_info.py"]),
                patch("sys.stdout", stdout),
            ):
                exit_code = main()

        data = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(data["confidence"], "high")
        self.assertEqual(data["provider"], "codex")
        self.assertEqual(data["session_id"], "stable-codex-id")
        self.assertEqual(data["thread_id"], "T-stable-codex-id")
        self.assertEqual(data["url"], "https://jieli.example.test/threads/T-stable-codex-id")
        self.assertEqual(data["cwd"], tmpdir)
        self.assertEqual(data["branch"], "feature/handoff")

    def test_main_fails_closed_without_stable_transcript_id(self):
        from handoff_info import main

        context = {"session_id": "hook-only", "cwd": "/repo"}
        encoded = base64.b64encode(json.dumps(context).encode("utf-8")).decode("ascii")
        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {"JIELI_HANDOFF_CONTEXT_B64": encoded}, clear=True),
            patch.object(sys, "argv", ["handoff_info.py"]),
            patch("sys.stdout", stdout),
        ):
            exit_code = main()

        data = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(data["confidence"], "missing")
        self.assertEqual(data["thread_id"], "")
        self.assertIn("stable", data["reason"])

    def test_main_fails_closed_without_hook_context(self):
        from handoff_info import main

        stdout = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(sys, "argv", ["handoff_info.py"]),
            patch("sys.stdout", stdout),
        ):
            exit_code = main()

        data = json.loads(stdout.getvalue())
        self.assertEqual(exit_code, 0)
        self.assertEqual(data["confidence"], "missing")
        self.assertEqual(data["thread_id"], "")
        self.assertIn("reason", data)


class CodexCommitTrailerTests(PluginScriptTestCase):
    def test_pre_tool_use_injects_handoff_context_for_helper_command(self):
        from commit_trailer import build_hook_response

        response = build_hook_response(
            {
                "session_id": "codex-handoff",
                "transcript_path": "/tmp/codex-session.jsonl",
                "cwd": "/repo",
                "tool_name": "Bash",
                "tool_input": {"command": "jieli-handoff-info"},
            }
        )

        updated = response["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn("JIELI_HANDOFF_CONTEXT_B64=", updated)
        self.assertIn(" python3 ", updated)
        self.assertTrue(updated.endswith("/scripts/handoff_info.py"))
        encoded = updated.split("JIELI_HANDOFF_CONTEXT_B64=", 1)[1].split(" ", 1)[0].strip("'")
        context = json.loads(base64.b64decode(encoded).decode("utf-8"))
        self.assertEqual(context["session_id"], "codex-handoff")
        self.assertEqual(context["transcript_path"], "/tmp/codex-session.jsonl")
        self.assertEqual(context["cwd"], "/repo")

    def test_pre_tool_use_injects_handoff_context_for_script_command(self):
        from commit_trailer import build_hook_response

        response = build_hook_response(
            {
                "session_id": "codex-handoff",
                "transcript_path": "/tmp/codex-session.jsonl",
                "cwd": "/repo",
                "tool_name": "Bash",
                "tool_input": {"command": 'python3 "${PLUGIN_ROOT:-$CLAUDE_PLUGIN_ROOT}/scripts/handoff_info.py"'},
            }
        )

        updated = response["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn("JIELI_HANDOFF_CONTEXT_B64=", updated)
        self.assertTrue(updated.endswith(' python3 "${PLUGIN_ROOT:-$CLAUDE_PLUGIN_ROOT}/scripts/handoff_info.py"'))

    def test_updated_commit_command_injects_codex_thread_id_trailer(self):
        from commit_trailer import updated_commit_command

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            mapping_path = home / ".jieli" / "codex-sessions.json"
            mapping_path.parent.mkdir(parents=True)
            mapping_path.write_text(
                json.dumps(
                    {
                        "codex-1": {
                            "provider_thread_id": "T-codex-1",
                            "base_url": "https://jieli.example.test",
                        }
                    }
                ),
                encoding="utf-8",
            )

            updated = updated_commit_command('git commit -m "ship"', "codex-1", home)

        self.assertEqual(
            updated,
            'git commit -m "ship" --trailer "Jieli-Thread: https://jieli.example.test/threads/T-codex-1"',
        )

    def test_updated_commit_command_updates_git_commit_inside_top_level_and_chain(self):
        from commit_trailer import updated_commit_command

        command = (
            "git status --short && "
            "python3 -m unittest plugins/codex/tests/test_plugin_scripts.py && "
            "git add plugins/codex/scripts/commit_trailer.py plugins/codex/tests/test_plugin_scripts.py && "
            'git commit -m "fix: add codex thread trailers" -- plugins/codex/scripts/commit_trailer.py'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            mapping_path = home / ".jieli" / "codex-sessions.json"
            mapping_path.parent.mkdir(parents=True)
            mapping_path.write_text(
                json.dumps({"codex-chain": {"provider_thread_id": "T-codex-chain", "base_url": "https://jieli.example.test"}}),
                encoding="utf-8",
            )

            updated = updated_commit_command(command, "codex-chain", home)

        self.assertEqual(
            updated,
            (
                "git status --short && "
                "python3 -m unittest plugins/codex/tests/test_plugin_scripts.py && "
                "git add plugins/codex/scripts/commit_trailer.py plugins/codex/tests/test_plugin_scripts.py && "
                'git commit -m "fix: add codex thread trailers" '
                '--trailer "Jieli-Thread: https://jieli.example.test/threads/T-codex-chain" -- plugins/codex/scripts/commit_trailer.py'
            ),
        )

    def test_updated_commit_command_injects_before_pathspec_separator(self):
        from commit_trailer import updated_commit_command

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            mapping_path = home / ".jieli" / "codex-sessions.json"
            mapping_path.parent.mkdir(parents=True)
            mapping_path.write_text(
                json.dumps({"codex-1": {"provider_thread_id": "T-codex-1", "base_url": "https://jieli.example.test"}}),
                encoding="utf-8",
            )

            updated = updated_commit_command('git commit -m "ship" -- path.txt', "codex-1", home)

        self.assertEqual(
            updated,
            'git commit -m "ship" --trailer "Jieli-Thread: https://jieli.example.test/threads/T-codex-1" -- path.txt',
        )

    def test_updated_commit_command_does_not_rewrite_complex_shell_or_existing_trailer(self):
        from commit_trailer import updated_commit_command

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            mapping_path = home / ".jieli" / "codex-sessions.json"
            mapping_path.parent.mkdir(parents=True)
            mapping_path.write_text(
                json.dumps({"codex-1": {"provider_thread_id": "T-codex-1", "base_url": "https://jieli.example.test"}}),
                encoding="utf-8",
            )

            self.assertEqual(updated_commit_command('git commit -m "ship" | cat', "codex-1", home), "")
            self.assertEqual(updated_commit_command('git commit -m "ship" --trailer Jieli-Thread:old', "codex-1", home), "")

    def test_build_hook_response_returns_updated_input_shape(self):
        from commit_trailer import build_hook_response

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            mapping_path = home / ".jieli" / "codex-sessions.json"
            mapping_path.parent.mkdir(parents=True)
            mapping_path.write_text(
                json.dumps({"codex-1": {"provider_thread_id": "T-codex-1", "base_url": "https://jieli.example.test"}}),
                encoding="utf-8",
            )

            response = build_hook_response(
                {
                    "tool_name": "Bash",
                    "session_id": "codex-1",
                    "tool_input": {"command": 'git commit -m "ship"'},
                },
                home=home,
            )

        output = response["hookSpecificOutput"]
        self.assertEqual(output["hookEventName"], "PreToolUse")
        self.assertEqual(output["permissionDecision"], "allow")
        self.assertIn("updatedInput", output)
        self.assertIn("Jieli-Thread: https://jieli.example.test/threads/T-codex-1", output["updatedInput"]["command"])


class CodexReadThreadTests(PluginScriptTestCase):
    def test_validate_thread_id_rejects_urls_and_export_extensions(self):
        from read_thread import validate_thread_id

        with self.assertRaises(ValueError):
            validate_thread_id("https://jieli.example.test/threads/T-1")
        with self.assertRaises(ValueError):
            validate_thread_id("T-1.md")
        self.assertEqual(validate_thread_id("T-1"), "T-1")

    def test_main_requires_jieli_api_key_only(self):
        from read_thread import main

        with tempfile.TemporaryDirectory() as tmpdir:
            with (
                patch.object(Path, "home", return_value=Path(tmpdir)),
                patch.dict(os.environ, {"CLAUDE_PLUGIN_OPTION_API_KEY": "legacy"}, clear=True),
                patch.object(sys, "argv", ["read_thread.py", "T-1"]),
                patch("sys.stderr", io.StringIO()) as stderr,
            ):
                exit_code = main()

        self.assertEqual(exit_code, 1)
        self.assertIn("JIELI_API_KEY", stderr.getvalue())

    def test_read_thread_accepts_settings_api_key(self):
        from read_thread import main

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return b"thread markdown"

        def fake_urlopen(request, timeout):
            captured["authorization"] = request.headers["Authorization"]
            captured["url"] = request.full_url
            return FakeResponse()

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            settings_path = home / ".config" / "jieli" / "settings.json"
            settings_path.parent.mkdir(parents=True)
            settings_path.write_text(
                json.dumps({"api_key": "settings-key", "base_url": "https://jieli.example.test"}),
                encoding="utf-8",
            )

            with (
                patch.object(Path, "home", return_value=home),
                patch.dict(os.environ, {}, clear=True),
                patch.object(sys, "argv", ["read_thread.py", "T-1"]),
                patch("urllib.request.urlopen", fake_urlopen),
                patch("sys.stdout", io.StringIO()) as stdout,
            ):
                exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["authorization"], "Bearer settings-key")
        self.assertEqual(captured["url"], "https://jieli.example.test/threads/T-1.md")
        self.assertEqual(stdout.getvalue(), "thread markdown")


class CodexPluginManifestTests(PluginScriptTestCase):
    def test_bin_handoff_info_wrapper_resolves_plugin_root_without_env(self):
        wrapper = PLUGIN_ROOT / "bin" / "jieli-handoff-info"

        result = subprocess.run(
            [str(wrapper), "--help"],
            env={},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage:", result.stdout)

    def test_sync_hooks_do_not_run_on_user_prompt_submit(self):
        hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))

        self.assertNotIn("UserPromptSubmit", hooks["hooks"])


if __name__ == "__main__":
    unittest.main()
