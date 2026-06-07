import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))


class SyncScriptTests(unittest.TestCase):
    def test_redact_text_removes_standalone_api_keys(self):
        from redact import redact_text

        redacted = redact_text("standalone key sk-ant-abcdefghijklmnopqrstuvwxyz123456")

        self.assertIn("[REDACTED:openai-api-key]", redacted)
        self.assertNotIn("sk-ant-abcdefghijklmnopqrstuvwxyz123456", redacted)

    def test_redact_text_removes_common_database_passwords(self):
        from redact import redact_text

        text = "\n".join(
            [
                'REDIS_PASSWORD="redis-secret"',
                "REDISCLI_AUTH=redis-cli-secret",
                "password = spaced-secret",
                "OPENAI_API_KEY = pk-secret",
                "redis-cli -a redis-flag-secret ping",
                "redis-cli --pass redis-pass-secret ping",
                "redis-server --requirepass redis-server-secret",
                "redis://:redis-url-secret@localhost:6379/0",
                "mongodb://user:mongo-secret@localhost:27017/db",
                "postgresql://user:pg-secret@localhost:5432/db",
                "mysql://user:mysql-secret@localhost:3306/db",
            ]
        )

        redacted = redact_text(text)

        self.assertNotIn("redis-secret", redacted)
        self.assertNotIn("redis-cli-secret", redacted)
        self.assertNotIn("spaced-secret", redacted)
        self.assertNotIn("pk-secret", redacted)
        self.assertNotIn("redis-flag-secret", redacted)
        self.assertNotIn("redis-pass-secret", redacted)
        self.assertNotIn("redis-server-secret", redacted)
        self.assertNotIn("redis-url-secret", redacted)
        self.assertNotIn("mongo-secret", redacted)
        self.assertNotIn("pg-secret", redacted)
        self.assertNotIn("mysql-secret", redacted)
        self.assertIn('REDIS_PASSWORD="[REDACTED:redis-password]"', redacted)
        self.assertIn("REDISCLI_AUTH=[REDACTED:rediscli-auth]", redacted)
        self.assertIn("password = [REDACTED:password]", redacted)
        self.assertIn("OPENAI_API_KEY = [REDACTED:openai-api-key]", redacted)
        self.assertIn("redis-cli -a [REDACTED:redis-cli-password] ping", redacted)
        self.assertIn("redis-cli --pass [REDACTED:redis-cli-password] ping", redacted)
        self.assertIn("redis-server --requirepass [REDACTED:redis-cli-password]", redacted)
        self.assertIn("redis://[REDACTED:url-userinfo]@localhost:6379/0", redacted)
        self.assertIn("mongodb://[REDACTED:url-userinfo]@localhost:27017/db", redacted)
        self.assertIn("postgresql://[REDACTED:url-userinfo]@localhost:5432/db", redacted)
        self.assertIn("mysql://[REDACTED:url-userinfo]@localhost:3306/db", redacted)

    def test_redact_text_removes_url_userinfo_fragments_and_sensitive_query_values(self):
        from redact import redact_text

        text = (
            "https://user:pass@example.com/path?access_token=abc&ok=1#frag "
            "wss://default:pk_xxx@example.com/actors/gateway?rvt-token=secret&sessionid=sid"
        )

        redacted = redact_text(text)

        self.assertNotIn("user:pass", redacted)
        self.assertNotIn("pk_xxx", redacted)
        self.assertNotIn("#frag", redacted)
        self.assertNotIn("access_token=abc", redacted)
        self.assertNotIn("rvt-token=secret", redacted)
        self.assertNotIn("sessionid=sid", redacted)
        self.assertIn("https://[REDACTED:url-userinfo]@example.com/path?access_token=[REDACTED:url-query-access-token]&ok=1", redacted)
        self.assertIn("wss://[REDACTED:url-userinfo]@example.com/actors/gateway?rvt-token=[REDACTED:url-query-rvt-token]&sessionid=[REDACTED:url-query-sessionid]", redacted)

    def test_redact_json_masks_sensitive_keys_and_skips_image_payloads(self):
        from redact import redact_json

        payload = {
            "config": {
                "password": "plain-secret",
                "redis_url": "redis://:redis-secret@localhost:6379/0",
                "nested": [{"wsToken": "ws-secret"}],
            },
            "image": {"type": "base64", "data": "sk-ant-not-redacted-in-image-data"},
            "inline": {"isImage": True, "content": "sk-ant-image-content", "url": "https://x.test/?token=url-secret"},
        }

        redacted = redact_json(payload)

        self.assertEqual(redacted["config"]["password"], "[REDACTED:password]")
        self.assertEqual(redacted["config"]["redis_url"], "[REDACTED:redis-url]")
        self.assertEqual(redacted["config"]["nested"][0]["wsToken"], "[REDACTED:wstoken]")
        self.assertEqual(redacted["image"]["data"], "sk-ant-not-redacted-in-image-data")
        self.assertEqual(redacted["inline"]["content"], "sk-ant-image-content")
        self.assertEqual(redacted["inline"]["url"], "https://x.test/?token=[REDACTED:url-query-token]")

    def test_redact_text_removes_high_value_vendor_tokens(self):
        from redact import redact_text

        text = "\n".join(
            [
                "github_pat_" + "a" * 22 + "_" + "b" * 59,
                "gho_" + "a" * 36,
                "ghu_" + "b" * 36,
                "ghr_" + "c" * 76,
                "glpat-" + "d" * 20,
                "hf_" + "e" * 20,
                "AIza" + "f" * 35,
                "xoxb-" + "1" * 12,
                "https://hooks.slack.com/services/T000/B000/" + "g" * 24,
                "sk_live_" + "h" * 20,
                "pypi-" + "i" * 20,
                "lin_api_" + "j" * 20,
                "ASIA" + "K" * 16,
                "AGE-SECRET-KEY-1" + "L" * 20,
                "eyJ" + "a" * 12 + ".eyJ" + "b" * 12 + "." + "c" * 12,
            ]
        )

        redacted = redact_text(text)

        self.assertIn("[REDACTED:github-token]", redacted)
        self.assertIn("[REDACTED:gitlab-token]", redacted)
        self.assertIn("[REDACTED:huggingface-token]", redacted)
        self.assertIn("[REDACTED:google-api-key]", redacted)
        self.assertIn("[REDACTED:slack-token]", redacted)
        self.assertIn("[REDACTED:slack-webhook]", redacted)
        self.assertIn("[REDACTED:stripe-secret-key]", redacted)
        self.assertIn("[REDACTED:pypi-token]", redacted)
        self.assertIn("[REDACTED:linear-token]", redacted)
        self.assertIn("[REDACTED:aws-access-key]", redacted)
        self.assertIn("[REDACTED:age-secret-key]", redacted)
        self.assertIn("[REDACTED:jwt-token]", redacted)

    def test_redact_text_strips_invisible_tag_chars_before_matching(self):
        from redact import redact_text

        redacted = redact_text("sk-\U000E0000ant-secret-value")

        self.assertEqual(redacted, "[REDACTED:openai-api-key]")

    def test_secret_file_path_helper_matches_likely_secret_files(self):
        from redact import is_secret_file_path

        for path in [".env", ".env.local", "env.production", "foo.secret", "foo.credentials", ".envrc", "nested/.env"]:
            self.assertTrue(is_secret_file_path(path), path)
        for path in [".env.example", ".env.sample", "README.md", "env.example"]:
            self.assertFalse(is_secret_file_path(path), path)

    def test_build_payload_from_claude_jsonl_redacts_secrets_and_skips_non_messages(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps({"type": "queue-operation", "sessionId": "cc-1"}),
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-1",
                                "sessionId": "cc-1",
                                "cwd": "/Users/alice/work/jieli",
                                "gitBranch": "plugin/sync",
                                "timestamp": "2026-06-06T09:00:00.000Z",
                                "message": {
                                    "role": "user",
                                    "content": [{"type": "text", "text": "use ANTHROPIC_API_KEY=sk-ant-secret-value"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "uuid": "a-1",
                                "sessionId": "cc-1",
                                "timestamp": "2026-06-06T09:00:03.000Z",
                                "message": {
                                    "role": "assistant",
                                    "model": "claude-opus-4-1",
                                    "usage": {"totalInputTokens": 25, "maxInputTokens": 100},
                                    "content": [{"type": "text", "text": "done with Authorization: Bearer abc.def.ghi"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "tool-1",
                                "sessionId": "cc-1",
                                "message": {
                                    "role": "user",
                                    "content": [{"type": "tool_result", "content": "Authorization: Bearer tool.secret"}],
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
                    "session_id": "cc-1",
                    "transcript_path": str(transcript),
                    "cwd": "/Users/alice/work/jieli",
                },
                base_url="https://jieli.example.test",
            )

        self.assertEqual(payload["provider"], "claude_code")
        self.assertEqual(payload["repo"], "work/jieli")
        self.assertEqual(payload["branch"], "plugin/sync")
        self.assertEqual(payload["source_url"], "https://jieli.example.test/threads/T-cc-1")
        self.assertEqual(payload["thread"]["id"], "T-cc-1")
        self.assertEqual(payload["thread"]["model"], "claude-opus-4-1")
        self.assertEqual(len(payload["thread"]["messages"]), 3)
        self.assertEqual(payload["thread"]["messages"][1]["usage"], {"totalInputTokens": 25, "maxInputTokens": 100})
        self.assertEqual(payload["thread"]["messages"][2]["role"], "tool")
        self.assertEqual(payload["thread"]["messages"][2]["content"][0]["type"], "tool_result")
        self.assertIn("[REDACTED:authorization-bearer]", payload["thread"]["messages"][2]["content"][0]["content"])
        raw_payload = json.dumps(payload, sort_keys=True)
        self.assertIn("[REDACTED:", raw_payload)
        self.assertNotIn("sk-ant-secret-value", raw_payload)
        self.assertNotIn("abc.def.ghi", raw_payload)
        self.assertNotIn("tool.secret", raw_payload)

    def test_build_payload_uploads_structured_image_blocks(self):
        from sync import build_payload_from_hook

        uploaded_paths: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "1.png"
            image_path.write_bytes(b"fake-png")
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u-image",
                        "sessionId": "cc-image",
                        "message": {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "look at this"},
                                {"type": "image", "source": str(image_path)},
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {
                    "session_id": "cc-image",
                    "transcript_path": str(transcript),
                    "cwd": "/Users/alice/work/jieli",
                },
                base_url="https://jieli.example.test",
                image_uploader=lambda path: uploaded_paths.append(str(path)) or "https://jieli.example.test/attachments/img.png",
            )

        content = payload["thread"]["messages"][0]["content"]
        self.assertEqual(uploaded_paths, [str(image_path)])
        self.assertEqual(
            content,
            [
                {"type": "text", "text": "look at this"},
                {"type": "image", "source": {"url": "https://jieli.example.test/attachments/img.png", "type": "image/png"}},
            ],
        )

    def test_build_payload_uploads_image_placeholder_paths_from_text(self):
        from sync import build_payload_from_hook

        uploaded_paths: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "1.png"
            image_path.write_bytes(b"fake-png")
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u-image-placeholder",
                        "sessionId": "cc-image-placeholder",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "text",
                                    "text": f"ok，看图\n[Image: source: {image_path}]\n这是什么 logo?",
                                }
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {
                    "session_id": "cc-image-placeholder",
                    "transcript_path": str(transcript),
                    "cwd": "/Users/alice/work/jieli",
                },
                base_url="https://jieli.example.test",
                image_uploader=lambda path: uploaded_paths.append(str(path)) or "https://jieli.example.test/attachments/img.png",
            )

        content = payload["thread"]["messages"][0]["content"]
        self.assertEqual(uploaded_paths, [str(image_path)])
        self.assertEqual(
            content,
            [
                {"type": "text", "text": "ok，看图"},
                {"type": "image", "source": {"url": "https://jieli.example.test/attachments/img.png", "type": "image/png"}},
                {"type": "text", "text": "这是什么 logo?"},
            ],
        )

    def test_build_payload_keeps_existing_image_label_when_upload_fails(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "1.png"
            image_path.write_bytes(b"fake-png")
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u-image-fallback",
                        "sessionId": "cc-image-fallback",
                        "message": {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "我测试下图片：\n\n[Image #1]\n你看到了什么"},
                                {"type": "image", "source": str(image_path)},
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {
                    "session_id": "cc-image-fallback",
                    "transcript_path": str(transcript),
                    "cwd": "/Users/alice/work/jieli",
                },
                base_url="https://jieli.example.test",
                image_uploader=lambda path: (_ for _ in ()).throw(OSError("backend is down")),
            )

        content = payload["thread"]["messages"][0]["content"]
        self.assertEqual(len(payload["thread"]["messages"]), 1)
        self.assertEqual(content, "我测试下图片：\n\n[Image #1]\n你看到了什么")
        self.assertNotIn("[Image unavailable]", content)

    def test_build_payload_keeps_existing_image_label_when_upload_succeeds(self):
        from sync import build_payload_from_hook

        uploaded_paths: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "1.png"
            image_path.write_bytes(b"fake-png")
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u-image-success",
                        "sessionId": "cc-image-success",
                        "message": {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "我测试下图片：\n\n[Image #1]\n你看到了什么"},
                                {"type": "image", "source": str(image_path)},
                            ],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {
                    "session_id": "cc-image-success",
                    "transcript_path": str(transcript),
                    "cwd": "/Users/alice/work/jieli",
                },
                base_url="https://jieli.example.test",
                image_uploader=lambda path: uploaded_paths.append(str(path)) or "https://jieli.example.test/attachments/img.png",
            )

        self.assertEqual(uploaded_paths, [str(image_path)])
        self.assertEqual(
            payload["thread"]["messages"][0]["content"],
            [
                {"type": "text", "text": "我测试下图片：\n\n[Image #1]\n你看到了什么"},
                {"type": "image", "source": {"url": "https://jieli.example.test/attachments/img.png", "type": "image/png"}},
            ],
        )

    def test_build_payload_skips_duplicate_image_unavailable_message(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "1.png"
            image_path.write_bytes(b"fake-png")
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-image-text",
                                "sessionId": "cc-image-fallback",
                                "message": {
                                    "role": "user",
                                    "content": [
                                        {"type": "text", "text": "我测试下图片：\n\n[Image #1]\n你看到了什么"},
                                        {"type": "image", "source": str(image_path)},
                                    ],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-image-only",
                                "sessionId": "cc-image-fallback",
                                "message": {
                                    "role": "user",
                                    "content": [{"type": "image", "source": str(image_path)}],
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
                    "session_id": "cc-image-fallback",
                    "transcript_path": str(transcript),
                    "cwd": "/Users/alice/work/jieli",
                },
                base_url="https://jieli.example.test",
                image_uploader=lambda path: (_ for _ in ()).throw(OSError("backend is down")),
            )

        self.assertEqual(len(payload["thread"]["messages"]), 1)
        self.assertEqual(payload["thread"]["messages"][0]["content"], "我测试下图片：\n\n[Image #1]\n你看到了什么")

    def test_upload_attachment_cached_reuses_successful_upload_by_content_hash(self):
        import sync

        calls: list[str] = []

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            image_path = Path(tmpdir) / "1.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
            original_upload = sync.upload_attachment

            try:
                sync.upload_attachment = lambda path, base_url, api_key: calls.append(str(path)) or "https://jieli.example.test/attachments/img.png"
                first = sync.upload_attachment_cached(image_path, "https://jieli.example.test/", "secret", home=home)
                second = sync.upload_attachment_cached(image_path, "https://jieli.example.test/", "secret", home=home)
            finally:
                sync.upload_attachment = original_upload

        self.assertEqual(first, "https://jieli.example.test/attachments/img.png")
        self.assertEqual(second, "https://jieli.example.test/attachments/img.png")
        self.assertEqual(calls, [str(image_path)])

    def test_upload_attachment_cached_does_not_cache_failed_uploads(self):
        import sync

        calls = 0

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir) / "home"
            image_path = Path(tmpdir) / "1.png"
            image_path.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
            original_upload = sync.upload_attachment

            def fail_upload(path: Path, base_url: str, api_key: str) -> str:
                nonlocal calls
                calls += 1
                raise OSError("backend is down")

            try:
                sync.upload_attachment = fail_upload
                with self.assertRaises(OSError):
                    sync.upload_attachment_cached(image_path, "https://jieli.example.test/", "secret", home=home)
                with self.assertRaises(OSError):
                    sync.upload_attachment_cached(image_path, "https://jieli.example.test/", "secret", home=home)
            finally:
                sync.upload_attachment = original_upload

        self.assertEqual(calls, 2)

    def test_build_payload_uses_claude_code_configured_model_alias(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a-1",
                        "sessionId": "cc-model-alias",
                        "timestamp": "2026-06-06T09:00:03.000Z",
                        "message": {
                            "role": "assistant",
                            "model": "gpt-5.4-mini-2026-03-17",
                            "content": [{"type": "text", "text": "done"}],
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"ANTHROPIC_DEFAULT_HAIKU_MODEL": "gpt-5.4-mini"}, clear=True):
                payload = build_payload_from_hook(
                    {
                        "session_id": "cc-model-alias",
                        "transcript_path": str(transcript),
                        "cwd": "/Users/alice/work/jieli",
                    },
                    base_url="https://jieli.example.test",
                )

        self.assertEqual(payload["thread"]["model"], "gpt-5.4-mini")
        self.assertEqual(payload["thread"]["resolved_model"], "gpt-5.4-mini-2026-03-17")

    def test_build_payload_skips_claude_local_command_messages_before_first_prompt(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-caveat",
                                "sessionId": "4e5bb892-5198-4556-bdf0-b9fddc069976",
                                "message": {
                                    "role": "user",
                                    "content": "<local-command-caveat>Caveat: local commands</local-command-caveat>",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-command",
                                "sessionId": "4e5bb892-5198-4556-bdf0-b9fddc069976",
                                "message": {
                                    "role": "user",
                                    "content": "<command-name>/plugin</command-name> <command-message>plugin</command-message>",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-stdout",
                                "sessionId": "4e5bb892-5198-4556-bdf0-b9fddc069976",
                                "message": {
                                    "role": "user",
                                    "content": "<local-command-stdout>(no content)</local-command-stdout>",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-real",
                                "sessionId": "4e5bb892-5198-4556-bdf0-b9fddc069976",
                                "cwd": "/Users/alice/work/jieli",
                                "message": {"role": "user", "content": "你好 我测试一下插件"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {
                    "session_id": "4e5bb892-5198-4556-bdf0-b9fddc069976",
                    "transcript_path": str(transcript),
                    "cwd": "/Users/alice/work/jieli",
                },
                base_url="https://jieli.example.test",
            )

        self.assertEqual(payload["thread"]["id"], "T-4e5bb892-5198-4556-bdf0-b9fddc069976")
        self.assertEqual(payload["thread"]["title"], "你好 我测试一下插件")
        self.assertEqual([message["message_id"] for message in payload["thread"]["messages"]], ["u-real"])

    def test_build_payload_skips_loaded_skill_body_but_keeps_skill_tool_use(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-command",
                                "sessionId": "skill-session",
                                "message": {
                                    "role": "user",
                                    "content": "<command-message>tdd</command-message>\n<command-name>/tdd</command-name>",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "uuid": "a-skill",
                                "sessionId": "skill-session",
                                "message": {
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "tool_use",
                                            "id": "skill-1",
                                            "name": "skill",
                                            "input": {"name": "caveman"},
                                        }
                                    ],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-skill-body",
                                "sessionId": "skill-session",
                                "message": {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": (
                                                "Base directory for this skill: /Users/buru/.claude/skills/caveman\n\n"
                                                "# Caveman\n\n"
                                                "Respond terse like smart caveman. This full skill body should not sync."
                                            ),
                                        }
                                    ],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-real",
                                "sessionId": "skill-session",
                                "cwd": "/Users/alice/work/jieli",
                                "message": {"role": "user", "content": "真正的用户消息"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            payload = build_payload_from_hook(
                {
                    "session_id": "skill-session",
                    "transcript_path": str(transcript),
                    "cwd": "/Users/alice/work/jieli",
                },
                base_url="https://jieli.example.test",
            )

        raw_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("Base directory for this skill", raw_payload)
        self.assertNotIn("Respond terse like smart caveman", raw_payload)
        self.assertNotIn("<command-message>tdd</command-message>", raw_payload)
        self.assertEqual(payload["thread"]["messages"][0]["content"], "/tdd")
        self.assertIn('"name": "skill"', raw_payload)
        self.assertIn("真正的用户消息", raw_payload)

    def test_build_payload_merges_split_assistant_response_after_loaded_skill(self):
        from sync import build_payload_from_hook

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-command",
                                "sessionId": "skill-session",
                                "message": {
                                    "role": "user",
                                    "content": "<command-message>caveman</command-message>\n<command-name>/caveman</command-name>",
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "user",
                                "uuid": "u-skill-body",
                                "sessionId": "skill-session",
                                "message": {
                                    "role": "user",
                                    "content": [
                                        {
                                            "type": "text",
                                            "text": "Base directory for this skill: /Users/buru/.claude/skills/caveman\n\nSkill body.",
                                        }
                                    ],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "uuid": "a-thinking",
                                "sessionId": "skill-session",
                                "message": {
                                    "id": "resp-1",
                                    "role": "assistant",
                                    "content": [{"type": "thinking", "thinking": "Switching style.", "signature": "large-signature"}],
                                },
                            }
                        ),
                        json.dumps(
                            {
                                "type": "assistant",
                                "uuid": "a-text",
                                "sessionId": "skill-session",
                                "message": {
                                    "id": "resp-1",
                                    "role": "assistant",
                                    "content": [{"type": "text", "text": "好。已切 **caveman**。后面都短说。"}],
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
                    "session_id": "skill-session",
                    "transcript_path": str(transcript),
                    "cwd": "/Users/alice/work/jieli",
                },
                base_url="https://jieli.example.test",
            )

        self.assertEqual([message["role"] for message in payload["thread"]["messages"]], ["user", "assistant"])
        self.assertEqual(payload["thread"]["messages"][0]["content"], "/caveman")
        self.assertEqual(
            payload["thread"]["messages"][1]["content"],
            [
                {"type": "thinking", "thinking": "Switching style."},
                {"type": "text", "text": "好。已切 **caveman**。后面都短说。"},
            ],
        )
        raw_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        self.assertNotIn("Base directory for this skill", raw_payload)
        self.assertNotIn("large-signature", raw_payload)

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

    def test_missing_config_response_is_visible_on_user_prompt_submit(self):
        from sync import build_missing_config_hook_response

        response = build_missing_config_hook_response("userpromptsubmit", ["JIELI_API_KEY"])

        self.assertTrue(response["continue"])
        self.assertIn("systemMessage", response)
        self.assertIn("JIELI_API_KEY", response["systemMessage"])
        self.assertIn("https://jieli.app", response["systemMessage"])
        self.assertIn("create an API key", response["systemMessage"])
        self.assertIn("ask the agent to configure it", response["systemMessage"])
        self.assertNotIn("JIELI_BASE_URL", response["systemMessage"])
        self.assertNotIn("self-hosted", response["systemMessage"])

    def test_missing_config_response_is_quiet_on_non_prompt_hooks(self):
        from sync import build_missing_config_hook_response

        self.assertEqual(build_missing_config_hook_response("stop", ["JIELI_API_KEY"]), {})
        self.assertEqual(build_missing_config_hook_response("userpromptsubmit", []), {})

    def test_missing_config_uses_default_base_url_and_accepts_plugin_api_key(self):
        from sync import build_payload_from_hook, missing_config_vars

        missing = missing_config_vars(
            {
                "CLAUDE_PLUGIN_OPTION_API_KEY": "secret",
            }
        )

        self.assertEqual(missing, [])

        with tempfile.TemporaryDirectory() as tmpdir:
            transcript = Path(tmpdir) / "session.jsonl"
            transcript.write_text(
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "u-1",
                        "sessionId": "cc-default-url",
                        "message": {"role": "user", "content": "hello"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                payload = build_payload_from_hook(
                    {
                        "session_id": "cc-default-url",
                        "transcript_path": str(transcript),
                        "cwd": "/Users/alice/work/jieli",
                    }
                )

        self.assertEqual(payload["source_url"], "https://jieli.app/threads/T-cc-default-url")

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
            result = upload_payload({"provider": "claude_code"}, "https://jieli.example.test/", "secret")

        self.assertEqual(captured["url"], "https://jieli.example.test/plugin/threads/upload")
        self.assertEqual(captured["timeout"], 20)
        self.assertEqual(result["success"], True)


class ReadThreadScriptTests(unittest.TestCase):
    def test_fetches_markdown_export_for_thread_id_with_api_key(self):
        from read_thread import fetch_thread_export

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return "thread body".encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["auth"] = request.headers.get("Authorization")
            captured["timeout"] = timeout
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            body = fetch_thread_export("T-abc123", "https://jieli.example.test/", "secret")

        self.assertEqual(body, "thread body")
        self.assertEqual(captured["url"], "https://jieli.example.test/threads/T-abc123.md")
        self.assertEqual(captured["auth"], "Bearer secret")
        self.assertEqual(captured["timeout"], 20)

    def test_fetches_markdown_export_with_truncated_tool_results(self):
        from read_thread import fetch_thread_export

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return "thread body".encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse()

        with patch("urllib.request.urlopen", fake_urlopen):
            fetch_thread_export("T-abc123", "https://jieli.example.test/", "secret", truncate_tool_results=True)

        self.assertEqual(captured["url"], "https://jieli.example.test/threads/T-abc123.md?truncate_tool_results=1")

    def test_rejects_urls_so_skill_must_pass_thread_id_only(self):
        from read_thread import validate_thread_id

        with self.assertRaises(ValueError):
            validate_thread_id("/threads/T-abc123")
        with self.assertRaises(ValueError):
            validate_thread_id("https://jieli.example.test/threads/T-abc123")
        self.assertEqual(validate_thread_id("T-abc123"), "T-abc123")

    def test_limits_output_by_line_range_before_character_truncation(self):
        from read_thread import limit_output

        content = "line 1\nline 2\nline 3\nline 4\n"

        limited = limit_output(content, start_line=2, end_line=3, max_chars=9)

        self.assertTrue(limited.startswith("line 2\nli"))
        self.assertIn("[Content truncated at 9 chars", limited)
        self.assertNotIn("line 1", limited)
        self.assertNotIn("line 4", limited)

    def test_read_thread_main_defaults_to_jieli_app_base_url(self):
        from read_thread import main

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return "thread body".encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            captured["auth"] = request.headers.get("Authorization")
            return FakeResponse()

        stdout = io.StringIO()
        with (
            patch("urllib.request.urlopen", fake_urlopen),
            patch.dict(os.environ, {"JIELI_API_KEY": "secret"}, clear=True),
            patch.object(sys, "argv", ["read_thread.py", "T-abc123"]),
            patch("sys.stdout", stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(stdout.getvalue(), "thread body")
        self.assertEqual(captured["url"], "https://jieli.app/threads/T-abc123.md")
        self.assertEqual(captured["auth"], "Bearer secret")

    def test_read_thread_main_can_request_truncated_tool_results(self):
        from read_thread import main

        captured = {}

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return None

            def read(self):
                return "thread body".encode("utf-8")

        def fake_urlopen(request, timeout):
            captured["url"] = request.full_url
            return FakeResponse()

        stdout = io.StringIO()
        with (
            patch("urllib.request.urlopen", fake_urlopen),
            patch.dict(os.environ, {"JIELI_API_KEY": "secret"}, clear=True),
            patch.object(sys, "argv", ["read_thread.py", "T-abc123", "--truncate-tool-results"]),
            patch("sys.stdout", stdout),
        ):
            exit_code = main()

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured["url"], "https://jieli.app/threads/T-abc123.md?truncate_tool_results=1")


class CommitTrailerTests(unittest.TestCase):
    def test_pre_tool_use_output_adds_trailer_when_session_mapping_exists(self):
        from commit_trailer import build_hook_response

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            mapping_dir = home / ".jieli"
            mapping_dir.mkdir()
            (mapping_dir / "claude-sessions.json").write_text(
                json.dumps(
                    {
                        "cc-1": {
                            "provider_thread_id": "T-cc-1",
                            "base_url": "https://jieli.example.test",
                        }
                    }
                ),
                encoding="utf-8",
            )
            response = build_hook_response(
                {
                    "session_id": "cc-1",
                    "tool_name": "Bash",
                    "tool_input": {"command": 'git commit -m "sync plugin"'},
                },
                home=home,
            )

        updated = response["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn('--trailer "Claude-Code-Thread-ID: https://jieli.example.test/threads/T-cc-1"', updated)

    def test_pre_tool_use_updates_git_commit_inside_top_level_and_chain(self):
        from commit_trailer import build_hook_response

        command = (
            "git status --short && "
            "go test ./cmd/server ./backend/service/thread ./backend/api/route && "
            "git add backend/api/route/export.go backend/service/thread/export.go cmd/server/main.go cmd/server/main_test.go && "
            'git commit -m "fix: support api thread markdown export" -- docs/test.md'
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            mapping_dir = home / ".jieli"
            mapping_dir.mkdir()
            (mapping_dir / "claude-sessions.json").write_text(
                json.dumps(
                    {
                        "cc-chain": {
                            "provider_thread_id": "T-cc-chain",
                            "base_url": "https://jieli.example.test",
                        }
                    }
                ),
                encoding="utf-8",
            )
            response = build_hook_response(
                {
                    "session_id": "cc-chain",
                    "tool_name": "Bash",
                    "tool_input": {"command": command},
                },
                home=home,
            )

        updated = response["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertEqual(
            updated,
            (
                "git status --short && "
                "go test ./cmd/server ./backend/service/thread ./backend/api/route && "
                "git add backend/api/route/export.go backend/service/thread/export.go cmd/server/main.go cmd/server/main_test.go && "
                'git commit -m "fix: support api thread markdown export" '
                '--trailer "Claude-Code-Thread-ID: https://jieli.example.test/threads/T-cc-chain" -- docs/test.md'
            ),
        )

    def test_pre_tool_use_normalizes_legacy_thread_id_without_prefix(self):
        from commit_trailer import build_hook_response

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            mapping_dir = home / ".jieli"
            mapping_dir.mkdir()
            (mapping_dir / "claude-sessions.json").write_text(
                json.dumps(
                    {
                        "cc-legacy": {
                            "provider_thread_id": "cc-legacy",
                            "base_url": "https://jieli.example.test",
                        }
                    }
                ),
                encoding="utf-8",
            )
            response = build_hook_response(
                {
                    "session_id": "cc-legacy",
                    "tool_name": "Bash",
                    "tool_input": {"command": 'git commit -m "sync plugin"'},
                },
                home=home,
            )

        updated = response["hookSpecificOutput"]["updatedInput"]["command"]
        self.assertIn('--trailer "Claude-Code-Thread-ID: https://jieli.example.test/threads/T-cc-legacy"', updated)

    def test_pre_tool_use_leaves_ambiguous_commands_unchanged(self):
        from commit_trailer import build_hook_response

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            mapping_dir = home / ".jieli"
            mapping_dir.mkdir()
            (mapping_dir / "claude-sessions.json").write_text(
                json.dumps(
                    {
                        "cc-pipe": {
                            "provider_thread_id": "T-cc-pipe",
                            "base_url": "https://jieli.example.test",
                        }
                    }
                ),
                encoding="utf-8",
            )
            response = build_hook_response(
                {
                    "session_id": "cc-pipe",
                    "tool_name": "Bash",
                    "tool_input": {"command": 'git commit -m "x" | git status'},
                },
                home=home,
            )

        self.assertEqual(response, {})


class PluginManifestTests(unittest.TestCase):
    def test_standard_hooks_file_is_not_duplicated_in_manifest(self):
        manifest = json.loads((PLUGIN_ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertNotIn("hooks", manifest)

        hooks = json.loads((PLUGIN_ROOT / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        pre_tool_use = hooks["hooks"]["PreToolUse"]
        commands = [
            hook["command"]
            for config in pre_tool_use
            if config.get("matcher") == "Bash"
            for hook in config.get("hooks", [])
        ]
        self.assertTrue(any("commit_trailer.py" in command for command in commands))


class HookInstallTests(unittest.TestCase):
    def test_install_and_uninstall_hooks_preserves_non_jieli_hooks(self):
        from install_hooks import install_hooks, uninstall_hooks

        settings = {
            "hooks": {
                "UserPromptSubmit": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": "echo keep-me"}],
                    }
                ]
            }
        }

        installed = install_hooks(settings, plugin_root="/plugins/claude-code", version="0.1.0")
        commands = [
            hook["command"]
            for config in installed["hooks"]["UserPromptSubmit"]
            for hook in config["hooks"]
        ]
        self.assertIn("echo keep-me", commands)
        self.assertTrue(
            any(
                "sync.py" in command
                and "--trigger userpromptsubmit" in command
                and "--hook-version 0.1.0" in command
                for command in commands
            )
        )

        uninstalled = uninstall_hooks(installed)
        remaining = [
            hook["command"]
            for config in uninstalled["hooks"]["UserPromptSubmit"]
            for hook in config["hooks"]
        ]
        self.assertEqual(remaining, ["echo keep-me"])


if __name__ == "__main__":
    unittest.main()
