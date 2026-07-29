"""Server-side LLM chat calls for the /a/{slug}/llm endpoint.

Apps never hold a provider API key; they post {system, messages|prompt, model?,
temperature?, maxTokens?, json?, images?, files?} to /a/{slug}/llm and this module
forwards a chat completion to OpenAI or Anthropic with the key held ONLY here. The
provider is inferred from the requested model's membership in the per-provider
allowlists (see config: LLM_OPENAI_MODELS / LLM_ANTHROPIC_MODELS). Mirrors the
Census/Vision/S3 proxies.

Multimodal: apps may attach images (any vision-capable model) and PDF files. The
app posts inline base64 attachments (the browser helper converts File/Blob for
it); we normalize them into typed parts and translate to each provider's own
multimodal shape (OpenAI image_url/file, Anthropic image/document blocks). Images
may also be given as an http(s) URL — the server fetches it and inlines base64
(images only; see the SSRF guard in _fetch_image_url).

Guardrails (the keys are billable, so keep the proxy well-behaved):

  * the model must be on an allowlist; anything else is rejected up front
  * output tokens are clamped to LLM_MAX_OUTPUT_TOKENS (cost guard)
  * total input TEXT size is capped at LLM_MAX_INPUT_CHARS; attachment bytes are
    bounded by the endpoint's LLM_MAX_REQUEST_BYTES and LLM_MAX_ATTACHMENTS
  * only image/* and application/pdf attachments are accepted
  * image URL fetches are SSRF-guarded: public hosts only (private/loopback/
    link-local/reserved IPs rejected), every redirect hop revalidated, and the
    response content-type + size are checked
  * a per-session per-minute cap guards against runaway agent loops
  * the forwarded response body is size-capped and the call has a timeout
  * the provider URL/host is pinned per provider (built here, never caller-supplied)

The response is normalized across providers to {text, model, provider,
finish_reason, usage: {input_tokens, output_tokens}}. An empty completion (e.g.
the model hit the output-token cap during reasoning) is raised as an LLMError
rather than returned as blank text.

Uses httpx directly (already a dependency; no provider SDKs needed).
"""
import asyncio
import base64
import ipaddress
import logging
import re
import socket
import time
from collections import defaultdict, deque
from urllib.parse import urljoin, urlparse

import httpx

from config import settings

logger = logging.getLogger("appmcp.llm")

# Per-session call timestamps for the sliding-window rate limit (per process).
_CALL_LOG: dict[str, deque] = defaultdict(deque)

_ROLES = {"system", "user", "assistant"}

# Attachment media types we accept and translate. Images work on any
# vision-capable model; PDFs are sent as OpenAI files / Anthropic documents.
_IMAGE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_FILE_TYPES = {"application/pdf"}

# Nudge appended to the system prompt when json mode is requested on a provider
# that has no native JSON-response flag (Anthropic).
_JSON_NUDGE = (
    "Respond with a single valid JSON value and nothing else "
    "(no prose, no markdown fences)."
)


class LLMError(Exception):
    """Raised when an LLM request is invalid or the provider call fails."""


def _check_rate(session_key: str) -> None:
    limit = settings.llm_rate_per_min
    if limit <= 0:
        return
    now = time.monotonic()
    log = _CALL_LOG[session_key]
    while log and now - log[0] > 60:
        log.popleft()
    if len(log) >= limit:
        raise LLMError(
            "LLM rate limit exceeded for this session; try again shortly."
        )
    log.append(now)


def _resolve_model(model) -> tuple[str, str]:
    """Return (model, provider), defaulting and allowlist-checking the model."""
    name = (model or settings.llm_default_model or "").strip()
    if not name:
        raise LLMError("No LLM model requested and no default is configured.")
    provider = settings.llm_provider_for(name)
    if provider is None:
        allowed = ", ".join(settings.llm_models) or "(none configured)"
        raise LLMError(f"Model '{name}' is not allowed. Choose one of: {allowed}.")
    return name, provider


def _strip_data_url(data: str) -> tuple[str, str | None]:
    """Return (base64, media_type_or_None) from a data URL or bare base64 string."""
    s = data.strip()
    media = None
    if s.startswith("data:"):
        comma = s.find(",")
        if comma == -1:
            raise LLMError("Malformed data URL in an attachment.")
        media = s[5:comma].split(";")[0].strip().lower() or None
        s = s[comma + 1:].strip()
    return s, media


def _looks_like_url(s) -> bool:
    return isinstance(s, str) and s.strip().lower().startswith(("http://", "https://"))


def _normalize_attachment(att, *, kind: str) -> dict:
    """Normalize one image/file into a typed part.

    Images: {type:"image", media_type, data} for inline base64, or a deferred
    {type:"image", url} that _resolve_image_urls fetches later. Files:
    {type:"file", media_type, data, filename} (inline base64 only; no URLs).

    `att` may be a bare base64/data-URL string, an http(s) URL string (images
    only), or an object with `data`/`url`, optional `media_type`/`mediaType`,
    and (files) `filename`.
    """
    if isinstance(att, str):
        att = {"url": att} if _looks_like_url(att) else {"data": att}
    if not isinstance(att, dict):
        raise LLMError(f"Each {kind} must be a base64/URL string or an object.")

    # A URL may arrive in `url` OR in the `data` field (e.g. an agent-built message
    # part, an older client, or a direct API call). Treat both the same so we
    # always fetch a URL rather than mistaking it for base64.
    url = att.get("url")
    if not _looks_like_url(url) and _looks_like_url(att.get("data")):
        url = att.get("data")
    if _looks_like_url(url):
        if kind != "image":
            raise LLMError("Only images may be given as a URL; send files as base64.")
        if not settings.llm_allow_image_urls:
            raise LLMError("Image URLs are not enabled on this server; send base64.")
        return {"type": "image", "url": url.strip()}

    raw = att.get("data")
    if not isinstance(raw, str) or not raw.strip():
        raise LLMError(f"Each {kind} needs base64 'data' (or, for images, a 'url').")
    b64, url_media = _strip_data_url(raw)
    media = str(att.get("media_type") or att.get("mediaType") or url_media or "").strip().lower()

    if kind == "image":
        media = media or "image/png"
        if media not in _IMAGE_TYPES:
            raise LLMError(
                f"Unsupported image type '{media}'. Allowed: {', '.join(sorted(_IMAGE_TYPES))}."
            )
        return {"type": "image", "media_type": media, "data": b64}

    media = media or "application/pdf"
    if media not in _FILE_TYPES:
        raise LLMError(
            f"Unsupported file type '{media}'. Allowed: {', '.join(sorted(_FILE_TYPES))}."
        )
    return {"type": "file", "media_type": media, "data": b64,
            "filename": str(att.get("filename") or "file.pdf")}


async def _assert_public_host(url: str) -> None:
    """Raise LLMError unless `url`'s host resolves only to public IPs (SSRF guard)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise LLMError("Image URLs must be http(s).")
    host = p.hostname
    if not host:
        raise LLMError("Malformed image URL.")
    port = p.port or (443 if p.scheme == "https" else 80)
    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM
        )
    except OSError as exc:
        raise LLMError(f"Could not resolve image URL host: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
                or ip.is_multicast or ip.is_unspecified):
            raise LLMError("Image URL host is not allowed (resolves to an internal address).")


async def _fetch_image_url(url: str) -> tuple[str, str]:
    """Fetch an image URL to (base64, media_type), revalidating every redirect hop."""
    current = url
    async with httpx.AsyncClient(
        timeout=settings.llm_image_fetch_timeout, follow_redirects=False
    ) as client:
        resp = None
        for _ in range(4):
            await _assert_public_host(current)
            try:
                resp = await client.get(current, headers={"Accept": "image/*"})
            except httpx.HTTPError as exc:
                raise LLMError(f"Could not fetch image URL: {exc}") from exc
            if resp.status_code in (301, 302, 303, 307, 308):
                loc = resp.headers.get("location")
                if not loc:
                    raise LLMError("Image URL redirect missing a location.")
                current = urljoin(current, loc)
                continue
            break
        else:
            raise LLMError("Too many redirects fetching the image URL.")

    if resp.status_code >= 400:
        raise LLMError(f"Image URL returned HTTP {resp.status_code}.")

    ctype = (resp.headers.get("content-type") or "").split(";")[0].strip().lower()
    if ctype == "image/jpg":
        ctype = "image/jpeg"
    if ctype not in _IMAGE_TYPES:
        raise LLMError(
            f"Image URL is not a supported image ({ctype or 'unknown content-type'}). "
            f"Allowed: {', '.join(sorted(_IMAGE_TYPES))}."
        )

    cap = settings.llm_max_image_bytes
    if cap > 0 and len(resp.content) > cap:
        raise LLMError(f"Fetched image too large ({len(resp.content)} bytes; max {cap}).")

    return base64.b64encode(resp.content).decode("ascii"), ctype


async def _resolve_image_urls(turns: list[dict]) -> None:
    """Fetch every deferred image-URL part and replace it with inline base64."""
    for turn in turns:
        for part in turn["parts"]:
            if part.get("type") == "image" and part.get("url") and not part.get("data"):
                b64, media = await _fetch_image_url(part["url"])
                part["data"] = b64
                part["media_type"] = media
                part.pop("url", None)


def _parts_from_content(content) -> list[dict]:
    """Turn a message's `content` (string OR array of parts) into typed parts."""
    if isinstance(content, str):
        if not content:
            raise LLMError("Message 'content' string must be non-empty.")
        return [{"type": "text", "text": content}]
    if not isinstance(content, list) or not content:
        raise LLMError("Message 'content' must be a non-empty string or array of parts.")

    parts: list[dict] = []
    for el in content:
        if isinstance(el, str):
            if el:
                parts.append({"type": "text", "text": el})
            continue
        if not isinstance(el, dict):
            raise LLMError("Each content part must be a string or an object.")
        ptype = str(el.get("type", "")).strip().lower()
        if ptype == "text":
            txt = el.get("text")
            if not isinstance(txt, str) or not txt:
                raise LLMError("A text part needs non-empty 'text'.")
            parts.append({"type": "text", "text": txt})
        elif ptype == "image":
            parts.append(_normalize_attachment(el, kind="image"))
        elif ptype == "file":
            parts.append(_normalize_attachment(el, kind="file"))
        else:
            raise LLMError(f"Unknown content part type '{ptype}'.")
    if not parts:
        raise LLMError("Message 'content' array has no usable parts.")
    return parts


def _build_messages(system, prompt, messages, images, files) -> tuple[str, list[dict]]:
    """Normalize inputs into (system_text, [{role, parts:[...]}, ...] non-system).

    Accepts either a `prompt` string (one user message) or a `messages` array of
    {role, content} where content is a string or an array of typed parts. Any
    system-role messages fold into the returned system text. Top-level `images`
    and `files` (convenience) are appended to the last user turn.
    """
    sys_parts: list[str] = []
    if isinstance(system, str) and system.strip():
        sys_parts.append(system.strip())

    turns: list[dict] = []
    if messages is not None:
        if not isinstance(messages, list) or not messages:
            raise LLMError("'messages' must be a non-empty array of {role, content}.")
        for m in messages:
            if not isinstance(m, dict):
                raise LLMError("Each message must be an object {role, content}.")
            role = str(m.get("role", "")).strip().lower()
            if role not in _ROLES:
                raise LLMError(f"Invalid message role '{role}'. Use user/assistant/system.")
            parts = _parts_from_content(m.get("content"))
            if role == "system":
                # System messages are text-only; fold their text into the prompt.
                if any(p["type"] != "text" for p in parts):
                    raise LLMError("System messages cannot contain attachments.")
                sys_parts.append(" ".join(p["text"] for p in parts))
            else:
                turns.append({"role": role, "parts": parts})
    elif isinstance(prompt, str) and prompt.strip():
        turns.append({"role": "user", "parts": [{"type": "text", "text": prompt}]})
    else:
        raise LLMError("Provide a 'prompt' string or a 'messages' array.")

    # Attach convenience images/files to the last user turn.
    extra: list[dict] = []
    if images is not None:
        if not isinstance(images, list):
            raise LLMError("'images' must be an array.")
        extra += [_normalize_attachment(a, kind="image") for a in images]
    if files is not None:
        if not isinstance(files, list):
            raise LLMError("'files' must be an array.")
        extra += [_normalize_attachment(a, kind="file") for a in files]
    if extra:
        target = next((t for t in reversed(turns) if t["role"] == "user"), None)
        if target is None:
            raise LLMError("Attachments require a user 'prompt' or user message.")
        target["parts"].extend(extra)

    if not turns:
        raise LLMError("At least one user/assistant message is required.")

    n_attach = sum(1 for t in turns for p in t["parts"] if p["type"] != "text")
    if settings.llm_max_attachments > 0 and n_attach > settings.llm_max_attachments:
        raise LLMError(
            f"Too many attachments ({n_attach}); max {settings.llm_max_attachments}."
        )

    system_text = "\n\n".join(sp for sp in sys_parts if sp).strip()

    # Cap TEXT size only; attachment bytes are bounded by the endpoint's request cap.
    text_total = len(system_text) + sum(
        len(p["text"]) for t in turns for p in t["parts"] if p["type"] == "text"
    )
    cap = settings.llm_max_input_chars
    if cap > 0 and text_total > cap:
        raise LLMError(
            f"Prompt too large ({text_total} chars); max {cap}. Summarize or send less data."
        )
    return system_text, turns


def _clamp_tokens(max_tokens, ceiling) -> int:
    """Clamp a requested maxTokens to `ceiling` (the per-model effective cap)."""
    if max_tokens is None:
        return ceiling
    try:
        n = int(max_tokens)
    except (TypeError, ValueError):
        raise LLMError("'maxTokens' must be an integer.")
    if n <= 0:
        raise LLMError("'maxTokens' must be a positive integer.")
    return min(n, ceiling) if ceiling > 0 else n


def _clean_temperature(temperature) -> float | None:
    if temperature is None:
        return None
    try:
        t = float(temperature)
    except (TypeError, ValueError):
        raise LLMError("'temperature' must be a number.")
    if not (0.0 <= t <= 2.0):
        raise LLMError("'temperature' must be between 0 and 2.")
    return t


def _too_big(resp: httpx.Response) -> bool:
    cap = settings.llm_max_response_bytes
    return cap > 0 and len(resp.content) > cap


# A whole response that is just a fenced code block: ```json\n...\n``` (the
# language tag is optional). Anthropic has no native JSON mode and often wraps
# JSON in a fence despite the nudge, so we peel it off in json mode.
_FENCE_RE = re.compile(r"^\s*```[^\n`]*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _unfence_json(text: str) -> str:
    """Strip a single wrapping markdown code fence, if the whole text is one."""
    if not text:
        return text
    m = _FENCE_RE.match(text.strip())
    return m.group(1).strip() if m else text


def _raise_empty(finish_reason, max_tokens) -> None:
    """Turn an empty completion into an actionable error instead of returning ''.

    The usual cause is `max_tokens`: reasoning models spend the output budget on
    internal reasoning and hit the cap before emitting any visible text.
    """
    if finish_reason == "max_tokens":
        raise LLMError(
            f"The model reached the output-token limit ({max_tokens}) before "
            "returning any text. Reasoning models spend output tokens on internal "
            "reasoning first, so raise maxTokens (or the server's "
            "LLM_MAX_OUTPUT_TOKENS) and retry."
        )
    raise LLMError(
        f"The model returned no text (finish_reason={finish_reason!r}). "
        "Try a higher maxTokens or a different model."
    )


def _is_openai_reasoning(model: str) -> bool:
    """Reasoning models (gpt-5*, o1/o3/o4*) use max_completion_tokens and only
    accept the default temperature, unlike the classic chat models (gpt-4o*)."""
    m = model.lower()
    return m.startswith(("gpt-5", "o1", "o3", "o4"))


def _openai_content(parts: list[dict]):
    """Render parts to OpenAI content (a plain string when it's a single text)."""
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    out = []
    for p in parts:
        if p["type"] == "text":
            out.append({"type": "text", "text": p["text"]})
        elif p["type"] == "image":
            out.append({"type": "image_url", "image_url": {
                "url": f"data:{p['media_type']};base64,{p['data']}"}})
        elif p["type"] == "file":
            out.append({"type": "file", "file": {
                "filename": p.get("filename", "file.pdf"),
                "file_data": f"data:{p['media_type']};base64,{p['data']}"}})
    return out


def _anthropic_content(parts: list[dict]):
    """Render parts to Anthropic content (a plain string when it's a single text)."""
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    out = []
    for p in parts:
        if p["type"] == "text":
            out.append({"type": "text", "text": p["text"]})
        elif p["type"] == "image":
            out.append({"type": "image", "source": {
                "type": "base64", "media_type": p["media_type"], "data": p["data"]}})
        elif p["type"] == "file":
            out.append({"type": "document", "source": {
                "type": "base64", "media_type": p["media_type"], "data": p["data"]}})
    return out


async def _call_openai(*, model, system_text, turns, max_tokens, temperature,
                       json_mode) -> dict:
    key = settings.llm_openai_api_key.strip()
    if not key:
        raise LLMError("OpenAI is not configured on this server.")
    msgs = [{"role": "system", "content": system_text}] if system_text else []
    msgs += [{"role": t["role"], "content": _openai_content(t["parts"])} for t in turns]
    body: dict = {"model": model, "messages": msgs}
    if _is_openai_reasoning(model):
        # Reasoning models: token limit uses a different field and temperature is
        # fixed at the default, so we don't forward a custom temperature.
        body["max_completion_tokens"] = max_tokens
    else:
        body["max_tokens"] = max_tokens
        if temperature is not None:
            body["temperature"] = temperature
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    url = settings.llm_openai_base_url.rstrip("/") + "/chat/completions"
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
            resp = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {key}"}
            )
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach OpenAI: {exc}") from exc

    if _too_big(resp):
        raise LLMError("LLM response too large; lower maxTokens.")
    if resp.status_code >= 400:
        raise LLMError(f"OpenAI rejected the request ({resp.status_code}): "
                       f"{resp.text[:300].strip()}")
    try:
        data = resp.json()
    except ValueError:
        raise LLMError(f"OpenAI returned non-JSON: {resp.text[:200].strip()}")

    choices = data.get("choices") or []
    text = ""
    finish_reason = None
    if choices:
        text = (choices[0].get("message") or {}).get("content") or ""
        finish_reason = choices[0].get("finish_reason")
    usage = data.get("usage") or {}
    if not (text or "").strip():
        _raise_empty("max_tokens" if finish_reason == "length" else finish_reason,
                     max_tokens)
    return {
        "text": text,
        "model": data.get("model", model),
        "provider": "openai",
        "finish_reason": finish_reason,
        "usage": {
            "input_tokens": usage.get("prompt_tokens"),
            "output_tokens": usage.get("completion_tokens"),
        },
    }


async def _call_anthropic(*, model, system_text, turns, max_tokens, temperature,
                          json_mode) -> dict:
    key = settings.llm_anthropic_api_key.strip()
    if not key:
        raise LLMError("Anthropic is not configured on this server.")
    sys_text = system_text
    if json_mode:
        sys_text = (sys_text + "\n\n" + _JSON_NUDGE).strip() if sys_text else _JSON_NUDGE
    msgs = [{"role": t["role"], "content": _anthropic_content(t["parts"])} for t in turns]
    body: dict = {"model": model, "messages": msgs, "max_tokens": max_tokens}
    if sys_text:
        body["system"] = sys_text
    if temperature is not None:
        body["temperature"] = temperature

    url = settings.llm_anthropic_base_url.rstrip("/") + "/messages"
    headers = {
        "x-api-key": key,
        "anthropic-version": settings.llm_anthropic_version,
    }
    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout) as client:
            resp = await client.post(url, json=body, headers=headers)
    except httpx.HTTPError as exc:
        raise LLMError(f"Could not reach Anthropic: {exc}") from exc

    if _too_big(resp):
        raise LLMError("LLM response too large; lower maxTokens.")
    if resp.status_code >= 400:
        raise LLMError(f"Anthropic rejected the request ({resp.status_code}): "
                       f"{resp.text[:300].strip()}")
    try:
        data = resp.json()
    except ValueError:
        raise LLMError(f"Anthropic returned non-JSON: {resp.text[:200].strip()}")

    # Anthropic returns content as a list of blocks; concatenate the text blocks.
    # Reasoning ("thinking") blocks are intentionally excluded from the answer.
    text = "".join(
        b.get("text", "") for b in (data.get("content") or [])
        if isinstance(b, dict) and b.get("type") == "text"
    )
    stop_reason = data.get("stop_reason")
    usage = data.get("usage") or {}
    if not text.strip():
        _raise_empty(stop_reason, max_tokens)
    return {
        "text": text,
        "model": data.get("model", model),
        "provider": "anthropic",
        "finish_reason": stop_reason,
        "usage": {
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
        },
    }


async def llm_complete(session_key: str, *, system=None, prompt=None,
                       messages=None, model=None, temperature=None,
                       max_tokens=None, json_mode=False,
                       images=None, files=None) -> dict:
    """Validate and forward one chat completion; return {text, model, usage}.

    `images`/`files` are optional inline attachments (base64 or data URLs, or
    objects with data + media_type[/filename]) appended to the last user turn.

    Raises LLMError on any validation problem or a non-2xx provider response.
    """
    if not settings.llm_configured:
        raise LLMError("The LLM proxy is not configured on this server.")

    model_name, provider = _resolve_model(model)
    system_text, turns = _build_messages(system, prompt, messages, images, files)
    # Clamp to the model's own output limit so providers don't 400 on an oversized
    # max_tokens (the global ceiling can stay high for the models that support it).
    ceiling = settings.llm_output_ceiling_for(model_name)
    out_tokens = _clamp_tokens(max_tokens, ceiling)
    # Reasoning models burn output tokens thinking before they answer; enforce a
    # floor so a small requested maxTokens doesn't leave zero budget for the text.
    if settings.llm_is_reasoning(model_name):
        out_tokens = max(out_tokens, min(settings.llm_reasoning_min_output_tokens, ceiling))
    temp = _clean_temperature(temperature)

    _check_rate(session_key)

    # Fetch any image URLs server-side (SSRF-guarded) into inline base64.
    await _resolve_image_urls(turns)

    kwargs = dict(
        model=model_name, system_text=system_text, turns=turns,
        max_tokens=out_tokens, temperature=temp, json_mode=bool(json_mode),
    )
    if provider == "openai":
        result = await _call_openai(**kwargs)
    else:
        result = await _call_anthropic(**kwargs)

    # In JSON mode, peel a wrapping ```json fence so the app can JSON.parse the
    # text directly (Anthropic often adds one despite the no-fence instruction).
    if json_mode and result.get("text"):
        result["text"] = _unfence_json(result["text"])
    return result
