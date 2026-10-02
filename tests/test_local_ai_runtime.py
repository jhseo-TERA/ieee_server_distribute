import base64
import json
import threading
import time
import unittest
from unittest.mock import patch

from local_ai_runtime import (
    ALLOWED_MODELS, OllamaCancelled, OllamaClient, OllamaError, OllamaTimeout,
    parse_json_output,
)


class FakeResponse:
    def __init__(self, events=None, status=200, data=None, wait=False):
        self.events = events or []
        self.status_code = status
        self.data = data or {}
        self.content = json.dumps(self.data).encode()
        self.closed = threading.Event()
        self.wait = wait

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self):
        self.closed.set()

    def json(self):
        return self.data

    def iter_lines(self, **kwargs):
        if self.wait:
            self.closed.wait(5)
        for event in self.events:
            yield event if isinstance(event, bytes) else json.dumps(event).encode()


class FakeSession:
    def __init__(self, response, status_data=None):
        self.response = response
        self.calls = []
        self.status_data = status_data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse(data=self.status_data)


class LocalAiRuntimeTests(unittest.TestCase):
    def test_loopback_only_no_credentials_paths_or_redirect_origins(self):
        for bad in ("https://127.0.0.1:11434", "http://example.com", "http://127.0.0.1/secret",
                    "http://127.0.0.1?next=evil", "http://user:secret@127.0.0.1",
                    "http://localhost.evil:11434", "http://169.254.169.254", "file:///tmp"):
            with self.subTest(url=bad), self.assertRaises(ValueError):
                OllamaClient(bad)
        self.assertEqual(OllamaClient("http://localhost:11434").base_url, "http://127.0.0.1:11434")
        self.assertEqual(OllamaClient("http://[::1]:11434").base_url, "http://[::1]:11434")

    def test_session_disables_environment_proxy_and_credentials(self):
        with OllamaClient()._session() as session:
            self.assertFalse(session.trust_env)

    def run_chat(self, response, **kwargs):
        client = OllamaClient()
        session = FakeSession(response)
        with patch.object(client, "_session", return_value=session):
            result = client.chat(kwargs.pop("model", "gpt-oss:20b"),
                                 kwargs.pop("messages", [{"role": "user", "content": "Compare sources."}]),
                                 **kwargs)
        return result, session

    def test_stream_content_usage_no_thinking_and_off_maps_to_low(self):
        updates = []
        result, session = self.run_chat(FakeResponse([
            {"message": {"thinking": "private reasoning", "content": ""}},
            {"message": {"content": "Answer "}},
            {"message": {"content": "[P1]"}, "done": True, "eval_count": 10,
             "eval_duration": 2_000_000_000, "prompt_eval_count": 20},
        ]), thinking="off", on_progress=updates.append)
        self.assertEqual(result["content"], "Answer [P1]")
        self.assertEqual(result["usage"]["tokens_per_second"], 5.0)
        self.assertNotIn("private reasoning", json.dumps(result) + json.dumps(updates))
        _, request = session.calls[0]
        self.assertFalse(request["allow_redirects"])
        self.assertTrue(request["stream"])
        self.assertEqual(request["json"]["think"], "low")
        self.assertEqual(request["json"]["keep_alive"], 0)
        self.assertNotIn("tools", request["json"])

    def test_qwen_boolean_thinking_and_png_input(self):
        image = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
        _, session = self.run_chat(FakeResponse([{"message": {"content": "table"}, "done": True}]),
                                  model=ALLOWED_MODELS[1], thinking="high",
                                  messages=[{"role": "user", "content": "Read this table", "images": [image]}])
        self.assertIs(session.calls[0][1]["json"]["think"], True)
        self.assertEqual(session.calls[0][1]["json"]["messages"][0]["images"], [image])

    def test_rejects_unknown_models_tools_and_oversized_options_before_network(self):
        client = OllamaClient()
        cases = [{"model": "malicious:latest"}, {"num_ctx": 32768}, {"max_tokens": 2049},
                 {"max_tokens": True}, {"thinking": "max"},
                 {"messages": [{"role": "tool", "content": "run command"}]},
                 {"messages": [{"role": "user", "content": "x", "tool_calls": []}]},
                 {"messages": [{"role": "user", "content": "x", "images": ["http://example.com"]}]}]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                client.chat(case.get("model", ALLOWED_MODELS[0]),
                            case.get("messages", [{"role": "user", "content": "hello"}]),
                            **{k: v for k, v in case.items() if k not in ("model", "messages")})

    def test_schema_output_is_validated_and_remote_ref_refused(self):
        schema = {"type": "object", "properties": {"value": {"type": "number"}}, "required": ["value"]}
        result, session = self.run_chat(FakeResponse([
            {"message": {"content": '{"value": 1.5}'}, "done": True}]), schema=schema)
        self.assertEqual(result["json"], {"value": 1.5})
        self.assertEqual(session.calls[0][1]["json"]["format"], schema)
        with self.assertRaises(OllamaError):
            parse_json_output('{"value": "invented"}', schema)
        with self.assertRaises(OllamaError):
            parse_json_output('{"value": NaN}')
        with self.assertRaises(ValueError):
            parse_json_output("{}", {"$ref": "https://evil.test/schema"})

    def test_cancellation_and_deadline_close_only_active_response(self):
        response = FakeResponse(wait=True)
        client = OllamaClient(deadline_seconds=1)
        with patch.object(client, "_session", return_value=FakeSession(response)):
            started = time.monotonic()
            with self.assertRaises(OllamaTimeout):
                client.chat(ALLOWED_MODELS[0], [{"role": "user", "content": "x"}])
            self.assertLess(time.monotonic() - started, 2)
        self.assertTrue(response.closed.wait(1))
        event = threading.Event()
        event.set()
        with patch.object(client, "_session") as session, self.assertRaises(OllamaCancelled):
            client.chat(ALLOWED_MODELS[0], [{"role": "user", "content": "x"}], cancel_event=event)
        session.assert_not_called()

    def test_inflight_cancel_interrupts_waiting_stream(self):
        event = threading.Event()
        timer = threading.Timer(0.1, event.set)
        timer.start()
        response = FakeResponse(wait=True)
        try:
            with self.assertRaises(OllamaCancelled):
                self.run_chat(response, cancel_event=event)
            self.assertTrue(response.closed.wait(1))
        finally:
            timer.cancel()

    def test_bad_stream_redirect_error_empty_or_tool_call_fails_closed(self):
        responses = [FakeResponse(status=302), FakeResponse([b"not-json"]),
                     FakeResponse([{"error": "sensitive details must not be reflected"}]),
                     FakeResponse([{"message": {"thinking": "reasoning only"}, "done": True}]),
                     FakeResponse([{"message": {"content": "bad", "tool_calls": [{"function": "delete"}]}}]),
                     FakeResponse([{"message": {"content": "partial"}}])]
        for response in responses:
            with self.subTest(response=response), self.assertRaises(OllamaError):
                self.run_chat(response)

    def test_status_filters_unapproved_models_and_marks_available(self):
        client = OllamaClient()
        session = FakeSession(FakeResponse(), {"models": [{"name": ALLOWED_MODELS[0], "size": 100, "digest": "actual-model-digest"},
                                                          {"name": "unrelated:7b", "size": 500}]})
        with patch.object(client, "_session", return_value=session):
            result = client.status()
        self.assertEqual(result['models'][0]['digest'], 'actual-model-digest')
        self.assertTrue(result["available"])
        self.assertEqual([m["name"] for m in result["models"]], list(ALLOWED_MODELS))
        self.assertTrue(result["models"][0]["installed"])
        self.assertFalse(result["models"][1]["installed"])
        self.assertTrue(all(not kwargs["allow_redirects"] for _, kwargs in session.calls))

    def test_cancelled_stream_must_finish_before_next_model_starts(self):
        release_consumer = threading.Event()
        started_consumer = threading.Event()

        class SlowClosingResponse(FakeResponse):
            def iter_lines(self, **kwargs):
                started_consumer.set()
                release_consumer.wait(5)
                yield json.dumps({"message": {"content": "cancelled late answer"}, "done": True}).encode()

            def close(self):
                # Simulate a socket awaiting cold-start headers even after close.
                self.closed.set()

        first_response = SlowClosingResponse()
        second_response = FakeResponse([{"message": {"content": "second answer"}, "done": True}])
        client = OllamaClient()
        cancellation = threading.Event()
        outputs = []

        def first():
            try:
                client.chat(ALLOWED_MODELS[0], [{"role": "user", "content": "first"}], cancel_event=cancellation)
            except OllamaCancelled:
                outputs.append("cancelled")

        def second():
            outputs.append(client.chat(ALLOWED_MODELS[1], [{"role": "user", "content": "second"}])["content"])

        with patch.object(client, "_session", side_effect=[FakeSession(first_response), FakeSession(second_response)]) as factory:
            first_thread = threading.Thread(target=first, daemon=True)
            second_thread = threading.Thread(target=second, daemon=True)
            try:
                first_thread.start()
                self.assertTrue(started_consumer.wait(1))
                cancellation.set()
                first_thread.join(1)
                self.assertEqual(outputs, ["cancelled"])
                self.assertTrue(first_response.closed.wait(1))
                second_thread.start()
                time.sleep(0.25)
                self.assertEqual(factory.call_count, 1)
                release_consumer.set()
                second_thread.join(2)
                self.assertEqual(outputs, ["cancelled", "second answer"])
                self.assertEqual(factory.call_count, 2)
            finally:
                release_consumer.set()
                first_thread.join(1)
                if second_thread.ident:
                    second_thread.join(1)

    def test_waiting_for_stream_gate_respects_cancel_and_deadline_without_network(self):
        client = OllamaClient(deadline_seconds=1)
        client._stream_gate.acquire()
        event = threading.Event()
        timer = threading.Timer(0.1, event.set)
        try:
            with patch.object(client, "_session") as session:
                timer.start()
                with self.assertRaises(OllamaCancelled):
                    client.chat(ALLOWED_MODELS[0], [{"role": "user", "content": "x"}], cancel_event=event)
                with self.assertRaises(OllamaTimeout):
                    client.chat(ALLOWED_MODELS[0], [{"role": "user", "content": "x"}])
                session.assert_not_called()
        finally:
            timer.cancel()
            client._stream_gate.release()


if __name__ == "__main__":
    unittest.main()
