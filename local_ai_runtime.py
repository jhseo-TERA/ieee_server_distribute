"""Bounded, loopback-only Ollama adapter for the paper repository.

Documents and model answers are untrusted data, never executable instructions.
This adapter never executes tool calls, SQL, shell commands, or model-supplied URLs.
Callers must put retrieved text in explicit data boundaries and independently
validate citations and proposed database changes before presenting an approval.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import queue
import threading
import time
from urllib.parse import urlsplit

import requests
from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError


MODELS = {
    "gpt-oss:20b": {"label": "gpt-oss 20B", "vision": False},
    "qwen3.6:35b-a3b-mtp-q4_K_M": {"label": "Qwen3.6 35B-A3B MTP", "vision": True},
    "qwen3.8:27b-q4_K_M": {"label": "Qwen3.8 27B Q4", "vision": True},
}
ALLOWED_MODELS = tuple(MODELS)
DEFAULT_MODEL = "gpt-oss:20b"
MAX_RESPONSE_CHARS = 256_000


class OllamaError(RuntimeError):
    """A safe, user-visible local model failure."""


class OllamaCancelled(OllamaError):
    pass


class OllamaTimeout(OllamaError):
    pass


def _validate_schema(schema):
    if not isinstance(schema, dict):
        raise ValueError("JSON schema must be an object.")
    pending = [schema]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key in ("$ref", "$dynamicRef"):
                if key in value and not str(value[key]).startswith("#"):
                    raise ValueError("External JSON schema references are not allowed.")
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise ValueError("Invalid JSON schema.") from exc


def parse_json_output(content: str, schema: dict | None = None):
    """Parse and validate a bounded model answer, without fetching schema URLs."""
    if not isinstance(content, str) or len(content) > MAX_RESPONSE_CHARS:
        raise OllamaError("Model JSON response is missing or too large.")
    cleaned = content.strip()
    if cleaned.startswith("```json\n") and cleaned.endswith("```"):
        cleaned = cleaned[8:-3].strip()
    try:
        value = json.loads(cleaned, parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, RecursionError) as exc:
        raise OllamaError("The model did not return valid JSON. No changes were applied.") from exc
    if schema is not None:
        _validate_schema(schema)
        try:
            Draft202012Validator(schema).validate(value)
        except (ValidationError, RecursionError) as exc:
            raise OllamaError("Model JSON does not match the required schema. No changes were applied.") from exc
    return value


def _loopback_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if hostname == "localhost":
            hostname = "127.0.0.1"
        if (parsed.scheme != "http" or not hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or not ipaddress.ip_address(hostname).is_loopback):
            raise ValueError
        port = parsed.port or 11434
        if not 1 <= port <= 65535:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ValueError("Ollama endpoint must be an HTTP loopback address without credentials or a path.") from exc
    authority = f"[{hostname}]" if ":" in hostname else hostname
    return f"http://{authority}:{port}"


def _safe_messages(model, messages):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 32:
        raise ValueError("Supply between 1 and 32 chat messages.")
    result = []
    text_chars = 0
    image_count = 0
    image_bytes = 0
    for message in messages:
        if not isinstance(message, dict) or set(message) - {"role", "content", "images"}:
            raise ValueError("Only role, content, and images are accepted; tool calls are disabled.")
        role, content = message.get("role"), message.get("content")
        if role not in {"system", "user", "assistant"} or not isinstance(content, str):
            raise ValueError("Invalid chat message.")
        text_chars += len(content)
        item = {"role": role, "content": content}
        if message.get("images"):
            if not MODELS[model]["vision"] or role != "user":
                raise ValueError("Image inputs require a Qwen vision model and a user message.")
            images = message["images"]
            if not isinstance(images, list):
                raise ValueError("Images must be a list of base64 image data.")
            checked = []
            for image in images:
                image_count += 1
                if not isinstance(image, str) or len(image) > 8_000_000 or image_count > 4:
                    raise ValueError("At most four bounded image inputs are supported.")
                try:
                    decoded = base64.b64decode(image, validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("Images must contain base64 data, not URLs or file paths.") from exc
                image_bytes += len(decoded)
                if not decoded.startswith((b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff")):
                    raise ValueError("Only PNG and JPEG image data are supported.")
                checked.append(image)
            item["images"] = checked
        result.append(item)
    if text_chars > 120_000 or image_bytes > 16_000_000:
        raise ValueError("The model request exceeds the bounded input size.")
    return result


class OllamaClient:
    """No external requests, automatic downloads, or arbitrary tool invocation."""

    def __init__(self, base_url="http://127.0.0.1:11434", *, deadline_seconds=900):
        self.base_url = _loopback_url(base_url)
        self.deadline_seconds = max(1.0, min(float(deadline_seconds), 1800.0))
        # Cancellation can return while a cold-start HTTP request still awaits
        # headers. Do not start another model until that consumer actually exits.
        self._stream_gate = threading.Lock()

    @staticmethod
    def _session():
        session = requests.Session()
        session.trust_env = False
        return session

    def _get_json(self, session, endpoint):
        with session.get(self.base_url + endpoint, timeout=(2, 4), allow_redirects=False) as response:
            if response.status_code != 200:
                raise OllamaError("Ollama status endpoint is unavailable.")
            if len(response.content) > 1_000_000:
                raise OllamaError("Ollama returned an unexpectedly large status response.")
            value = response.json()
            if not isinstance(value, dict):
                raise OllamaError("Ollama returned invalid status data.")
            return value

    def status(self) -> dict:
        models = [{"name": name, **config, "installed": False, "available": False,
                   "loaded": False} for name, config in MODELS.items()]
        try:
            with self._session() as session:
                tags = self._get_json(session, "/api/tags")
                installed = {item.get("name"): item for item in tags.get("models", [])
                             if isinstance(item, dict)}
                try:
                    ps = self._get_json(session, "/api/ps")
                    loaded = {item.get("name"): item for item in ps.get("models", [])
                              if isinstance(item, dict)}
                except (requests.RequestException, ValueError, OllamaError):
                    loaded = {}
            for model in models:
                name = model["name"]
                model.update(installed=name in installed, available=name in installed,
                             digest=installed.get(name, {}).get('digest'),
                             loaded=name in loaded, size=installed.get(name, {}).get("size"),
                             context_length=loaded.get(name, {}).get("context_length"),
                             size_vram=loaded.get(name, {}).get("size_vram"))
            return {"available": True, "models": models, "default_model": DEFAULT_MODEL}
        except (requests.RequestException, ValueError, TypeError, OllamaError):
            return {"available": False, "models": models, "default_model": DEFAULT_MODEL,
                    "error": "Ollama is not reachable. Start the local Ollama application."}

    def chat(self, model, messages, *, num_ctx=8192, max_tokens=1024,
             thinking="low", schema=None, cancel_event=None, on_progress=None,
             keep_alive=0) -> dict:
        """Stream a bounded request with cancellation; never return private thinking.

        on_progress receives public content chunks and activity counts only. Sources
        inside messages remain untrusted data. The caller owns citation validation.
        Cancellation closes this request only, never unloads other users' models.
        """
        if model not in MODELS:
            raise ValueError("Only the three configured local models are allowed.")
        if type(num_ctx) is not int or num_ctx not in (4096, 8192, 16384):
            raise ValueError("Context must be 4096, 8192, or 16384 tokens.")
        if type(max_tokens) is not int or not 32 <= max_tokens <= 2048:
            raise ValueError("Maximum output must be between 32 and 2048 tokens.")
        if type(keep_alive) is not int or not 0 <= keep_alive <= 300:
            raise ValueError("Model residency must be between 0 and 300 seconds.")
        if thinking not in (False, True, "off", "none", "low", "medium", "high"):
            raise ValueError("Unsupported thinking setting.")
        if schema is not None:
            _validate_schema(schema)
        off = thinking in (False, "off", "none")
        think = ("low" if thinking is True or off else thinking) if model == DEFAULT_MODEL else not off
        payload = {"model": model, "messages": _safe_messages(model, messages), "stream": True,
                   "think": think, "keep_alive": keep_alive,
                   "options": {"num_ctx": num_ctx, "num_predict": max_tokens, "temperature": 0.1}}
        if schema is not None:
            payload["format"] = schema
        cancel_event = cancel_event or threading.Event()
        if cancel_event.is_set():
            raise OllamaCancelled("Local AI request was cancelled.")
        started = time.monotonic()
        while True:
            if cancel_event.is_set():
                raise OllamaCancelled("Local AI request was cancelled while waiting for the previous stream to close.")
            if time.monotonic() - started >= self.deadline_seconds:
                raise OllamaTimeout("The previous local AI request is still closing. Try again shortly.")
            if self._stream_gate.acquire(timeout=0.2):
                break
        events = queue.Queue(maxsize=256)
        stopped = threading.Event()
        response_holder = []

        def emit(kind, value):
            while not stopped.is_set():
                try:
                    events.put((kind, value), timeout=0.2)
                    return
                except queue.Full:
                    pass

        def consume():
            try:
                with self._session() as session:
                    with session.post(self.base_url + "/api/chat", json=payload, stream=True,
                                      timeout=(3, 60), allow_redirects=False) as response:
                        response_holder.append(response)
                        if stopped.is_set():
                            return
                        if response.status_code != 200:
                            raise OllamaError("Ollama rejected the request. Check that the selected model is installed.")
                        received_bytes = 0
                        for line in response.iter_lines(chunk_size=1024):
                            if stopped.is_set():
                                break
                            if not line:
                                continue
                            received_bytes += len(line)
                            if len(line) > MAX_RESPONSE_CHARS or received_bytes > 4_000_000:
                                raise OllamaError("Ollama stream exceeded the response size limit.")
                            value = json.loads(line)
                            if not isinstance(value, dict):
                                raise OllamaError("Ollama returned an invalid stream event.")
                            emit("chunk", value)
                            if value.get("done"):
                                return
                        if not stopped.is_set():
                            raise OllamaError("Ollama stopped before completing the answer.")
            except OllamaError as exc:
                emit("error", exc)
            except requests.Timeout:
                emit("error", OllamaTimeout("Ollama did not respond within the connection timeout."))
            except (requests.RequestException, ValueError, TypeError):
                emit("error", OllamaError("Local Ollama connection failed or returned invalid data."))
            except Exception:
                emit("error", OllamaError("Local Ollama request ended unexpectedly."))
            finally:
                self._stream_gate.release()

        worker = threading.Thread(target=consume, name="paper-ai-ollama-stream", daemon=True)
        try:
            worker.start()
        except BaseException:
            self._stream_gate.release()
            raise
        fragments = []
        response_chars = 0
        chunks = 0
        try:
            while True:
                if cancel_event.is_set():
                    raise OllamaCancelled("Local AI request was cancelled.")
                if time.monotonic() - started >= self.deadline_seconds:
                    raise OllamaTimeout("Local AI request exceeded its time limit.")
                try:
                    kind, item = events.get(timeout=0.2)
                except queue.Empty:
                    continue
                if kind == "error":
                    raise item
                if item.get("error"):
                    raise OllamaError("Ollama could not complete this request.")
                message = item.get("message") or {}
                if not isinstance(message, dict) or message.get("tool_calls"):
                    raise OllamaError("Unexpected tool request was blocked. No actions were executed.")
                content = message.get("content", "")
                if not isinstance(content, str):
                    raise OllamaError("Invalid answer content from Ollama.")
                fragments.append(content)
                response_chars += len(content)
                chunks += 1
                if response_chars > MAX_RESPONSE_CHARS:
                    raise OllamaError("Model answer exceeded the size limit.")
                if on_progress:
                    on_progress({"content": content, "chunks": chunks,
                                 "elapsed_seconds": round(time.monotonic() - started, 1)})
                if item.get("done"):
                    answer = "".join(fragments).strip()
                    if not answer:
                        raise OllamaError("The model returned no answer. Increase the output limit or reduce reasoning.")
                    usage = {key: item.get(key, 0) for key in
                             ("prompt_eval_count", "eval_count", "total_duration", "load_duration",
                              "prompt_eval_duration", "eval_duration")}
                    duration = usage.get("eval_duration")
                    usage["tokens_per_second"] = (round(usage["eval_count"] * 1e9 / duration, 2)
                                                   if isinstance(duration, (int, float)) and duration > 0 else None)
                    result = {"content": answer, "model": model, "usage": usage,
                              "done_reason": item.get("done_reason"),
                              "elapsed_seconds": round(time.monotonic() - started, 2)}
                    if schema is not None:
                        result["json"] = parse_json_output(answer, schema)
                    return result
        finally:
            stopped.set()
            if worker.is_alive() and response_holder:
                # A socket close must not block the caller's cancellation/deadline.
                threading.Thread(target=response_holder[0].close, daemon=True).start()
