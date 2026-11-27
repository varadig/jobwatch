"""The language models jobwatch writes and reads with: DeepSeek first, Claude as the fallback (or the other way round).

Everything that needs a model asks an `Llm` for a JSON answer that matches a schema. The `Llm` tries its providers in
order: if the first one fails (no balance, a refusal, an answer that does not fit the schema), the next one answers.
The company research needs web search, which only Claude offers, so it goes to a provider that can search."""
import json
import os
import re
import time
import urllib.error
from abc import ABC, abstractmethod
from dataclasses import dataclass

from jobwatch.env import log
from jobwatch.net import http

ANTHROPIC_MODEL = "claude-opus-5-5"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODELS = ("deepseek-chat", "deepseek-reasoner")


class LlmError(Exception):
    """A model could not answer; the message is meant for the user."""


@dataclass
class Completion:
    data: dict       # the answer, already checked against the schema
    model: str       # the model that wrote it
    provider: str    # "deepseek" or "anthropic"
    input_tokens: int = 0
    output_tokens: int = 0


# ---------------------------------------------------------------- checking an answer against a schema
def schema_error(schema, value, path="answer"):
    """Why `value` does not fit the JSON schema (type, required, enum, items), or None if it does. Only the parts of
    JSON Schema that jobwatch's schemas use. Claude's structured outputs guarantee this; other models need checking."""
    kind = schema.get("type")
    if "enum" in schema and value not in schema["enum"]:
        return "%s must be one of %s" % (path, ", ".join(map(str, schema["enum"])))
    checks = {"object": dict, "array": list, "string": str, "boolean": bool}
    if kind in checks and not isinstance(value, checks[kind]):
        return "%s must be %s" % (path, {"object": "an object", "array": "a list", "string": "text", "boolean": "true or false"}[kind])
    if kind == "integer" and (isinstance(value, bool) or not isinstance(value, int)):
        return "%s must be a whole number" % path
    if kind == "object":
        for key in schema.get("required", []):
            if key not in value:
                return "%s is missing “%s”" % (path, key)
        for key, sub in (schema.get("properties") or {}).items():
            if key in value:
                err = schema_error(sub, value[key], "%s.%s" % (path, key))
                if err:
                    return err
    if kind == "array" and "items" in schema:
        for i, item in enumerate(value):
            err = schema_error(schema["items"], item, "%s[%d]" % (path, i))
            if err:
                return err
    return None


def parse_json_answer(text):
    """The JSON object in a model's text (code fences and a lead-in sentence are tolerated)."""
    text = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fenced:
        text = fenced.group(1).strip()
    if not text.startswith("{") and "{" in text:
        text = text[text.index("{"):text.rindex("}") + 1]
    try:
        data = json.loads(text)
    except ValueError:
        raise LlmError("The model's answer is not valid JSON.")
    if not isinstance(data, dict):
        raise LlmError("The model's answer is not a JSON object.")
    return data


# ---------------------------------------------------------------- providers
class Provider(ABC):
    """One model service. A subclass answers JSON requests and says whether it can search the web."""
    name = ""
    label = ""
    searches_web = False

    @abstractmethod
    def json(self, system, user, schema, effort="medium", timeout=120.0, max_tokens=16000):
        """A Completion whose data fits the schema, or raises LlmError."""

    def research(self, system, user, timeout=600.0):
        raise LlmError("%s cannot search the web." % self.label)


class DeepSeekProvider(Provider):
    """DeepSeek's chat API (OpenAI-compatible). Cheap and good at structured text; it has no web search."""
    name = "deepseek"
    label = "DeepSeek"
    MAX_OUTPUT = 8192   # deepseek-chat's limit; asking for more is an error

    def __init__(self, api_key, model="deepseek-chat", client=http):
        self.api_key = api_key
        self.model = model if model in DEEPSEEK_MODELS else "deepseek-chat"
        self.http = client

    def json(self, system, user, schema, effort="medium", timeout=120.0, max_tokens=16000):
        # json mode needs the word "JSON" and a description of the shape; the schema is validated on the way back
        contract = ("\n\nReply with one JSON object only (no prose, no code fences) that fits this JSON schema:\n"
                    + json.dumps(schema, ensure_ascii=False))
        body = {"model": self.model, "response_format": {"type": "json_object"},
                "max_tokens": min(max_tokens, self.MAX_OUTPUT), "temperature": 0.2 if effort == "low" else 0.7,
                "messages": [{"role": "system", "content": system + contract}, {"role": "user", "content": user}]}
        reply = self._post(body, timeout)
        choice = (reply.get("choices") or [{}])[0]
        if choice.get("finish_reason") == "length":
            raise LlmError("DeepSeek's answer was too long, try again.")
        data = parse_json_answer((choice.get("message") or {}).get("content"))
        err = schema_error(schema, data)
        if err:
            raise LlmError("DeepSeek's answer does not fit: %s." % err)
        usage = reply.get("usage") or {}
        return Completion(data, reply.get("model") or self.model, self.name, usage.get("prompt_tokens", 0),
                          usage.get("completion_tokens", 0))

    def _post(self, body, timeout):
        try:
            raw = self.http.get(DEEPSEEK_URL, data=json.dumps(body).encode("utf-8"), timeout=timeout, headers={
                "Authorization": "Bearer " + self.api_key, "Content-Type": "application/json"})
        except urllib.error.HTTPError as ex:
            raise LlmError(self._http_message(ex))
        except (urllib.error.URLError, TimeoutError, OSError):
            raise LlmError("Cannot reach the DeepSeek API (network error).")
        try:
            return json.loads(raw)
        except ValueError:
            raise LlmError("DeepSeek sent something that is not JSON.")

    @staticmethod
    def _http_message(ex):
        try:
            detail = json.loads(ex.read().decode("utf-8", "replace")).get("error", {}).get("message", "")
        except Exception:
            detail = ""
        known = {401: "Invalid DeepSeek API key (Settings → AI).", 402: "The DeepSeek balance is used up (top it up at platform.deepseek.com).",
                 429: "DeepSeek: too many requests, try again in a few minutes."}
        if ex.code in known:
            return known[ex.code]
        return "DeepSeek API error (%s): %s" % (ex.code, detail or ex.reason)


class AnthropicProvider(Provider):
    """Claude: structured outputs are guaranteed by the API, and it can search the web."""
    name = "anthropic"
    label = "Claude"
    searches_web = True

    def __init__(self, api_key, model=ANTHROPIC_MODEL):
        self.api_key = api_key
        self.model = model

    def json(self, system, user, schema, effort="medium", timeout=120.0, max_tokens=16000):
        resp = self._create(timeout, max_tokens=max_tokens, system=system,
                            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
                            messages=[{"role": "user", "content": user}])
        self._check(resp)
        data = parse_json_answer("".join(b.text for b in resp.content if b.type == "text"))
        usage = getattr(resp, "usage", None)
        return Completion(data, resp.model, self.name, getattr(usage, "input_tokens", 0) or 0, getattr(usage, "output_tokens", 0) or 0)

    def research(self, system, user, timeout=600.0):
        """(text, sources): a briefing written with Claude's web search and page reading. A long turn the server
        pauses (pause_turn) is continued a few times; every part of the answer is kept."""
        tools = [{"type": "web_search_20260209", "name": "web_search", "max_uses": 8},
                 {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 5}]
        messages = [{"role": "user", "content": user}]
        blocks = []
        for _ in range(5):
            resp = self._create(timeout, max_tokens=16000, system=system, tools=tools,
                                output_config={"effort": "medium"}, messages=messages)
            blocks.extend(resp.content)
            if resp.stop_reason != "pause_turn":
                break
            messages = [messages[0], {"role": "assistant", "content": blocks}]
        if resp.stop_reason == "refusal":
            raise LlmError("The model refused the request.")
        text = "".join(b.text for b in blocks if b.type == "text").strip()
        if not text:
            raise LlmError("The research came back empty (%s)." % resp.stop_reason)
        return text, research_sources(blocks)

    @staticmethod
    def _check(resp):
        if resp.stop_reason == "refusal":
            raise LlmError("The model refused the request.")
        if resp.stop_reason == "max_tokens":
            raise LlmError("The response was too long, try again.")

    def _create(self, timeout, **kw):
        """One request, with the API's errors turned into LlmError."""
        import anthropic
        client = anthropic.Anthropic(api_key=self.api_key or None, timeout=timeout, max_retries=2)
        try:
            return client.beta.messages.create(model=self.model, betas=["server-side-fallback-2026-07-01"],
                                               fallbacks="default", **kw)
        except anthropic.AuthenticationError:
            raise LlmError("Invalid Anthropic API key (Settings → AI).")
        except anthropic.PermissionDeniedError:
            raise LlmError("The API key has no permission for this model.")
        except anthropic.RateLimitError:
            raise LlmError("Claude API: too many requests, try again in a few minutes.")
        except anthropic.BadRequestError as ex:
            raise LlmError("Claude API bad request: %s" % ex.message)
        except anthropic.APIStatusError as ex:
            raise LlmError("Claude API error (%s): %s" % (ex.status_code, ex.message))
        except anthropic.APIConnectionError:
            raise LlmError("Cannot reach the Claude API (network error).")


def research_sources(blocks):
    """[(title, url)] a web-searched briefing rests on: the citations; without them the pages read, then the search
    results."""
    cited, fetched, found = [], [], []
    for b in blocks:
        kind = getattr(b, "type", "")
        if kind == "text":
            for c in getattr(b, "citations", None) or []:
                cited.append((getattr(c, "title", None), getattr(c, "url", None)))
        elif kind == "web_fetch_tool_result":
            c = getattr(b, "content", None)
            fetched.append((getattr(getattr(c, "content", None), "title", None), getattr(c, "url", None)))
        elif kind == "web_search_tool_result":
            c = getattr(b, "content", None)
            for r in c if isinstance(c, list) else []:   # an error is an object, not a list
                found.append((getattr(r, "title", None), getattr(r, "url", None)))
    out, seen = [], set()
    for title, url in cited or (fetched + found[:12]):
        if url and url.startswith("http") and url not in seen:
            seen.add(url)
            out.append((title or url, url))
    return out


# ---------------------------------------------------------------- the router
def _no_observer(provider, purpose, seconds, error=None, completion=None, fell_back_from=None):
    """What a call to the models reports to (see services.activity.model_call); the server sets the real one."""


observer = _no_observer


class Llm:
    """The providers in the order they are tried. Falsy when no key is set at all."""

    def __init__(self, providers=()):
        self.providers = list(providers)

    def __bool__(self):
        return bool(self.providers)

    @property
    def can_search_web(self):
        return any(p.searches_web for p in self.providers)

    @property
    def names(self):
        return [p.label for p in self.providers]

    def json(self, purpose="", **request):
        """A Completion from the first provider that can answer; when all fail, one error that lists why. `purpose`
        says what it is for, in the activity log."""
        if not self.providers:
            raise LlmError("No AI key is set (Settings → AI).")
        errors, failed = [], None
        for provider in self.providers:
            started = time.monotonic()
            try:
                done = provider.json(**request)
            except LlmError as ex:
                observer(provider, purpose, time.monotonic() - started, error=str(ex), fell_back_from=failed)
                errors.append("%s: %s" % (provider.label, ex))
                failed = provider.label
                if provider is not self.providers[-1]:
                    log.warning("%s failed (%s); trying %s", provider.label, ex, self.providers[self.providers.index(provider) + 1].label)
                continue
            observer(provider, purpose, time.monotonic() - started, completion=done, fell_back_from=failed)
            return done
        raise LlmError(errors[0] if len(errors) == 1 else "; ".join(errors))

    def research(self, system, user, timeout=600.0):
        """(text, sources) from a provider that can search the web."""
        for provider in self.providers:
            if provider.searches_web:
                started = time.monotonic()
                try:
                    text, sources = provider.research(system, user, timeout)
                except LlmError as ex:
                    observer(provider, "company research", time.monotonic() - started, error=str(ex))
                    raise
                observer(provider, "company research", time.monotonic() - started)
                return text, sources
        raise LlmError("The company research needs web search: add an Anthropic API key (Settings → AI).")


def llm_for(cfg):
    """The Llm for the settings: DeepSeek and Claude, whichever have a key, the preferred one first."""
    ai = cfg.get("llm") or {}
    providers = {}
    deepseek_key = cfg.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY", "")
    if deepseek_key:
        providers["deepseek"] = DeepSeekProvider(deepseek_key, ai.get("deepseek_model") or "deepseek-chat")
    anthropic_key = cfg.get("anthropic_api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        providers["anthropic"] = AnthropicProvider(anthropic_key)
    first = ai.get("primary") if ai.get("primary") in ("deepseek", "anthropic") else "deepseek"
    return Llm(sorted(providers.values(), key=lambda p: p.name != first))
