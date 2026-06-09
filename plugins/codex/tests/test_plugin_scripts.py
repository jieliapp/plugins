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
for module_name in ("sync", "commit_trailer", "read_thread", "redact"):
    sys.modules.pop(module_name, None)
sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))


class CodexSyncScriptTests(unittest.TestCase):
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
        self.assertEqual(payload["labels"], ["codex"])
        self.assertEqual(payload["repo"], "work/jieli")
        self.assertEqual(payload["branch"], "plugin/codex")
        self.assertEqual(payload["source_url"], "https://jieli.example.test/threads/T-codex-1")
        self.assertEqual(payload["thread"]["id"], "T-codex-1")
        self.assertEqual(payload["thread"]["model"], "gpt-5.5")
        self.assertEqual(payload["thread"]["title"], "sync OPENAI_API_KEY=[REDACTED:openai-api-key]")
        self.assertEqual([message["role"] for message in payload["thread"]["messages"]], ["user", "assistant", "assistant", "tool"])
        self.assertEqual(payload["thread"]["messages"][2]["content"][0]["type"], "tool_use")
        self.assertEqual(payload["thread"]["messages"][2]["content"][0]["input"]["token"], "[REDACTED:token]")
        self.assertEqual(payload["thread"]["messages"][3]["content"][0]["type"], "tool_result")
        raw_payload = json.dumps(payload, sort_keys=True)
        self.assertIn("[REDACTED:", raw_payload)
        self.assertNotIn("sk-ant-secret-value", raw_payload)
        self.assertNotIn("abc.def.ghi", raw_payload)
        self.assertNotIn("tool.secret", raw_payload)
        self.assertNotIn("developer instructions", raw_payload)
        self.assertNotIn("private reasoning", raw_payload)
        self.assertNotIn("encrypted", raw_payload)

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

    def test_missing_config_response_is_visible_on_user_prompt_submit(self):
        from sync import build_missing_config_hook_response, missing_config_vars

        with tempfile.TemporaryDirectory() as tmpdir, patch.object(Path, "home", return_value=Path(tmpdir)):
            missing = missing_config_vars({})
            response = build_missing_config_hook_response("userpromptsubmit", missing)

        self.assertTrue(response["continue"])
        self.assertIn("systemMessage", response)
        self.assertIn("settings.json", response["systemMessage"])
        self.assertIn("https://jieli.app", response["systemMessage"])
        self.assertIn("create an API key", response["systemMessage"])
        self.assertIn("chmod it to 600", response["systemMessage"])

    def test_settings_file_satisfies_missing_config_and_base_url(self):
        import jieli_config
        from sync import build_payload_from_hook, missing_config_vars, required_env

        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            settings_path = home / ".jieli" / "settings.json"
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
            settings_path = home / ".jieli" / "settings.json"
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


class CodexCommitTrailerTests(unittest.TestCase):
    def test_updated_commit_command_injects_jieli_thread_trailer(self):
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

            self.assertEqual(updated_commit_command('git add . && git commit -m "ship"', "codex-1", home), "")
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


class CodexReadThreadTests(unittest.TestCase):
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
            settings_path = home / ".jieli" / "settings.json"
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


if __name__ == "__main__":
    unittest.main()
