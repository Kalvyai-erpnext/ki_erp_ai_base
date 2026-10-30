# Copyright (c) 2026, kalvyai and contributors
# For license information, please see license.txt

import base64
import json
import logging
from time import monotonic, sleep

import frappe
import requests
from frappe import _
from frappe.model.document import Document
from frappe.query_builder.functions import Count, Sum
from frappe.utils import get_first_day, now_datetime, nowdate

from ki_erp_ai_base import usage
from ki_erp_ai_base.ai_base.doctype.ki_ai_model.ki_ai_model import ensure_models
from ki_erp_ai_base.providers import (
	ANTHROPIC_VERSION,
	MIN_RETRY_SECONDS,
	PROVIDER_KINDS,
	RETRY_DELAYS,
	TRANSIENT_STATUS,
	AIError,
	BudgetExceeded,
	FileNotSupported,
	ProviderBusy,
	data_uri,
	error_detail,
	kind_defaults,
	prepare_file,
	strict_schema,
)

logger = logging.getLogger(__name__)


class KIAIProvider(Document):
	"""An AI provider configuration with the customer's own API key.

	Other apps call :meth:`generate`; every call is logged in KI AI Usage Log and counted against
	the monthly token cap.
	"""

	def __setup__(self):
		# A provider created without Send Temperature gets its type's default (off for OpenAI)
		if self.get("send_temperature") is None and self.get("provider_type"):
			self.send_temperature = kind_defaults(self.provider_type)["send_temperature"]

	def before_validate(self):
		if not self.model_name and self.provider_type:
			self.model_name = kind_defaults(self.provider_type)["model_name"]

	def validate(self):
		if not 0 <= (self.temperature or 0) <= 2:
			frappe.throw(_("Temperature must be between 0 and 2."))
		if (self.max_output_tokens or 0) <= 0:
			frappe.throw(_("Max output tokens must be positive."))
		if (self.timeout or 0) <= 0:
			frappe.throw(_("The timeout must be positive."))
		if (self.monthly_token_cap or 0) < 0:
			frappe.throw(_("The monthly token cap cannot be negative."))

	def on_update(self):
		# Keep the provider's model in its type's list, so a model typed in or set by code is
		# still offered there
		ensure_models(self.provider_type, [self.model_name])

	# ------------------------------------------------------------------
	# Usage this month (virtual fields)
	# ------------------------------------------------------------------

	def _month_usage(self):
		"""``(tokens_in, tokens_out, calls)`` of this calendar month, read once per document."""
		if self.is_new():
			return 0, 0, 0
		if getattr(self, "_month_usage_cache", None) is None:
			log = frappe.qb.DocType("KI AI Usage Log")
			row = (
				frappe.qb.from_(log)
				.select(Sum(log.tokens_in), Sum(log.tokens_out), Count("*"))
				.where(log.provider == self.name)
				.where(log.creation >= get_first_day(nowdate()))
			).run()[0]
			self._month_usage_cache = (int(row[0] or 0), int(row[1] or 0), row[2])
		return self._month_usage_cache

	@property
	def month_tokens_in(self):
		return self._month_usage()[0]

	@property
	def month_tokens_out(self):
		return self._month_usage()[1]

	@property
	def month_tokens_total(self):
		return self._month_usage()[0] + self._month_usage()[1]

	@property
	def month_calls(self):
		return self._month_usage()[2]

	@property
	def month_cap_used(self):
		if not self.monthly_token_cap:
			return 0.0
		return min(100.0 * self.month_tokens_total / self.monthly_token_cap, 100.0)

	def budget_reached(self):
		"""Whether the monthly token cap is spent."""
		self._month_usage_cache = None
		return bool(self.monthly_token_cap and self.month_tokens_total >= self.monthly_token_cap)

	# ------------------------------------------------------------------
	# Form buttons
	# ------------------------------------------------------------------

	@frappe.whitelist()
	def test_connection(self):
		"""Send a tiny request with the configured key and model.

		The result is returned, not raised: raising would roll back the health check status.
		"""
		self.check_permission("write")
		try:
			answer, call_usage = self.generate(
				"Answer with the single word OK.", text="ping", feature="health_check"
			)
		except AIError as error:
			self.db_set("last_health_check", f"{now_datetime():%Y-%m-%d %H:%M} failed", update_modified=False)
			return {
				"ok": False,
				"busy": isinstance(error, ProviderBusy),
				"message": _("Connection failed: {0}").format(error),
			}
		self.db_set("last_health_check", f"{now_datetime():%Y-%m-%d %H:%M} OK", update_modified=False)
		return {
			"ok": True,
			"message": _('Connection OK ({0} answered "{1}", {2} tokens).').format(
				self.model_name,
				(answer or "").strip()[:40],
				call_usage["tokens_in"] + call_usage["tokens_out"],
			),
		}

	@frappe.whitelist()
	def fetch_models(self):
		"""List the models the provider's API offers to this key."""
		self.check_permission("write")
		api_key = self.get_password("api_key", raise_exception=False)
		if not api_key:
			frappe.throw(_("Provider {0} has no API key configured.").format(self.name))
		try:
			names = self.fetch_model_names(api_key)
		except AIError as error:
			frappe.throw(str(error), exc=type(error))
		if not names:
			frappe.throw(_("{0} listed no models for this key.").format(self.name))
		ensure_models(self.provider_type, names)
		return _("{0} models found at {1}.").format(len(names), self.name)

	def fetch_model_names(self, api_key):
		"""Model identifiers from the provider's list-models endpoint."""
		protocol = self._spec()["protocol"]
		base = self._base_url()
		if protocol == "anthropic":
			payload = self._request(
				f"{base}/models?limit=1000", {"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}
			)
			return [model["id"] for model in payload.get("data", [])]
		if protocol == "gemini":
			payload = self._request(f"{base}/models?pageSize=1000", {"x-goog-api-key": api_key})
			return [
				model["name"].removeprefix("models/")
				for model in payload.get("models", [])
				if "generateContent" in model.get("supportedGenerationMethods", [])
			]
		if protocol == "cohere":
			# Cohere lists models on v1 only
			payload = self._request(
				f"{base.removesuffix('/v2')}/v1/models?endpoint=chat&page_size=1000",
				{"Authorization": f"Bearer {api_key}"},
			)
			return [model["name"] for model in payload.get("models", [])]
		if self.provider_type == "Azure OpenAI":
			headers = {"api-key": api_key}
		else:
			headers = {"Authorization": f"Bearer {api_key}"}
		payload = self._request(f"{base}/models", headers)
		return [model["id"] for model in payload.get("data", [])]

	# ------------------------------------------------------------------
	# Public API
	# ------------------------------------------------------------------

	def can_read(self, mimetype):
		"""Whether this provider accepts a file of this type."""
		if mimetype == "application/pdf":
			return bool(self.supports_vision and self._spec()["pdf"])
		return bool(self.supports_vision)

	def generate(
		self,
		instructions,
		text=None,
		files=None,
		json_schema=None,
		schema_name="ki_output",
		feature=None,
		reference=None,
		timeout=None,
	):
		"""Run one model call and return ``(result, usage)``.

		:param instructions: system instructions.
		:param text: user content (plain text).
		:param files: raw file bytes (PDF or image), each sent as an attachment.
		:param json_schema: when given, the answer must match it and ``result`` is the parsed JSON;
		        otherwise ``result`` is text.
		:param feature: short tag recorded in the usage log, e.g. 'bill_extraction'.
		:param reference: the document the call is made for, linked in the usage log.
		:param timeout: seconds this call may take, when shorter than the provider's timeout.
		:return: ``(result, {"tokens_in": int, "tokens_out": int, "usage_log": str})``
		:raise ProviderBusy: temporary failure, retry later.
		:raise BudgetExceeded: monthly token cap reached.
		:raise FileNotSupported: the provider cannot read one of the files; nothing is sent.
		:raise AIError: anything else (bad key, refused, invalid output).
		"""
		if self.budget_reached():
			raise BudgetExceeded(
				_("Monthly token cap of {0} reached on provider {1}.").format(
					f"{self.monthly_token_cap:,}", self.name
				)
			)
		api_key = self.get_password("api_key", raise_exception=False)
		if not api_key:
			raise AIError(_("Provider {0} has no API key configured.").format(self.name))
		prepared = [prepare_file(raw) for raw in files or []]
		for _data, mimetype in prepared:
			if not self.can_read(mimetype):
				raise FileNotSupported(
					_("Provider {0} cannot read {1} files.").format(
						self.name, "PDF" if mimetype == "application/pdf" else _("image")
					)
				)
		request = {
			"api_key": api_key,
			"instructions": instructions,
			"text": text or ("Process the attached file." if prepared else ""),
			"files": prepared,
			"schema": json_schema,
			"schema_name": schema_name,
			"timeout": min(self.timeout, timeout or self.timeout),
			# Filled by the adapter as soon as the provider reports it, so a billed answer that is
			# then rejected still counts
			"usage": {"tokens_in": 0, "tokens_out": 0},
		}
		started = monotonic()
		try:
			raw_text = getattr(self, f"_call_{self._spec()['protocol']}")(request)
			if json_schema:
				try:
					result = json.loads(raw_text)
				except ValueError as error:
					raise AIError(_("{0} returned text that is not valid JSON.").format(self.name)) from error
			else:
				result = raw_text
		except AIError as error:
			status = "Temporary Failure" if isinstance(error, ProviderBusy) else "Error"
			self._log_usage(feature, reference, request["usage"], started, status, error)
			raise
		log = self._log_usage(feature, reference, request["usage"], started, "Done")
		return result, dict(request["usage"], usage_log=log.name)

	def _log_usage(self, feature, reference, call_usage, started, status, error=None):
		values = {
			"provider": self.name,
			"provider_type": self.provider_type,
			"model_name": self.model_name,
			"user": frappe.session.user,
			"feature": feature,
			"reference_doctype": reference.doctype if reference else None,
			"reference_name": reference.name if reference else None,
			"tokens_in": call_usage.get("tokens_in", 0),
			"tokens_out": call_usage.get("tokens_out", 0),
			"duration_ms": int((monotonic() - started) * 1000),
			"status": status,
			"error_message": str(error)[:1000] if error else None,
		}
		if self.meta.has_field("company"):  # added when ERPNext is installed
			values["company"] = self.get("company")
		return usage.record(values)

	def _check_truncated(self, truncated):
		"""Refuse an answer cut off by the output-token limit."""
		if truncated:
			raise AIError(
				_(
					"{0} stopped at the limit of {1} output tokens; the answer is incomplete. Raise Max Output Tokens per Call."
				).format(self.name, self.max_output_tokens)
			)

	def _temperature(self):
		return {"temperature": self.temperature} if self.send_temperature else {}

	def _spec(self):
		return PROVIDER_KINDS[self.provider_type]

	def _base_url(self):
		url = (self.base_url or self._spec()["base"]).rstrip("/")
		if not url:
			raise AIError(
				_(
					"Provider {0} needs a Base URL (for Azure OpenAI: https://<resource>.openai.azure.com/openai/v1)."
				).format(self.name)
			)
		return url

	# ------------------------------------------------------------------
	# Transport
	# ------------------------------------------------------------------

	def _request(self, url, headers, body=None, timeout=None):
		"""POST ``body``, or GET when there is none; raise :class:`ProviderBusy` for temporary
		failures and :class:`AIError` for permanent ones.

		Brief congestion (rate limits, overload, dropped connections) is retried within the same
		timeout budget, so the whole call never takes longer than one timeout.
		"""
		budget = timeout or self.timeout
		deadline = monotonic() + budget
		for delay in (*RETRY_DELAYS, None):
			try:
				return self._request_once(url, headers, body, deadline - monotonic(), budget)
			except ProviderBusy as error:
				if (
					delay is None
					or isinstance(error.__cause__, requests.Timeout)
					or deadline - monotonic() - delay < MIN_RETRY_SECONDS
				):
					raise
				logger.info("%s busy, retrying in %ss: %s", self.name, delay, error)
				sleep(delay)

	def _request_once(self, url, headers, body, timeout, budget):
		try:
			if body is None:
				response = requests.get(url, headers=headers, timeout=timeout)
			else:
				response = requests.post(url, headers=headers, json=body, timeout=timeout)
		except requests.Timeout as error:
			raise ProviderBusy(
				_("{0} did not answer within {1} seconds.").format(self.name, round(budget))
			) from error
		except requests.RequestException as error:
			raise ProviderBusy(
				_("Could not reach {0} ({1}).").format(self.name, error.__class__.__name__)
			) from error
		if response.status_code == 200:
			try:
				return response.json()
			except ValueError as error:
				# Typically an HTML page from a proxy or gateway in between
				raise AIError(
					_("{0} answered with something other than JSON: {1}").format(
						self.name, response.text[:500]
					)
				) from error
		message = _("{0} refused the request (HTTP {1}): {2}").format(
			self.name, response.status_code, error_detail(response)
		)
		if response.status_code in TRANSIENT_STATUS:
			raise ProviderBusy(message)
		raise AIError(message)

	def _call_anthropic(self, request):
		"""Anthropic Messages API; structured output via output_config.format."""
		content = [
			{
				"type": "document" if mimetype == "application/pdf" else "image",
				"source": {
					"type": "base64",
					"media_type": mimetype,
					"data": base64.b64encode(data).decode(),
				},
			}
			for data, mimetype in request["files"]
		]
		content.append({"type": "text", "text": request["text"]})
		body = {
			"model": self.model_name,
			"max_tokens": self.max_output_tokens,
			"system": request["instructions"],
			"messages": [{"role": "user", "content": content}],
			**self._temperature(),
		}
		if request["schema"]:
			body["output_config"] = {
				"format": {"type": "json_schema", "schema": strict_schema(request["schema"])}
			}
		if self.reasoning_effort:
			body.setdefault("output_config", {})["effort"] = self.reasoning_effort.lower()
		payload = self._request(
			f"{self._base_url()}/messages",
			{
				"x-api-key": request["api_key"],
				"anthropic-version": ANTHROPIC_VERSION,
				"Content-Type": "application/json",
			},
			body,
			request["timeout"],
		)
		usage = payload.get("usage", {})
		request["usage"] = {
			"tokens_in": usage.get("input_tokens", 0),
			"tokens_out": usage.get("output_tokens", 0),
		}
		self._check_truncated(payload.get("stop_reason") == "max_tokens")
		if payload.get("stop_reason") == "refusal":
			raise AIError(_("Anthropic refused the request (stop reason: refusal)."))
		raw_text = next(
			(block.get("text") for block in payload.get("content", []) if block.get("type") == "text"),
			None,
		)
		if not raw_text:
			raise AIError(_("Anthropic returned no text block."))
		return raw_text

	def _call_gemini(self, request):
		parts = [{"text": request["instructions"]}]
		if request["text"]:
			parts.append({"text": request["text"]})
		parts += [
			{"inline_data": {"mime_type": mimetype, "data": base64.b64encode(data).decode()}}
			for data, mimetype in request["files"]
		]
		config = {"maxOutputTokens": self.max_output_tokens, **self._temperature()}
		if request["schema"]:
			config.update({"responseMimeType": "application/json", "responseSchema": request["schema"]})
		if self.reasoning_effort:
			config["thinkingConfig"] = {"thinkingLevel": self.reasoning_effort.lower()}
		payload = self._request(
			f"{self._base_url()}/models/{self.model_name}:generateContent",
			{"x-goog-api-key": request["api_key"], "Content-Type": "application/json"},
			{"contents": [{"role": "user", "parts": parts}], "generationConfig": config},
			request["timeout"],
		)
		usage = payload.get("usageMetadata", {})
		request["usage"] = {
			"tokens_in": usage.get("promptTokenCount", 0),
			"tokens_out": usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0),
		}
		candidate = (payload.get("candidates") or [{}])[0]
		self._check_truncated(candidate.get("finishReason") == "MAX_TOKENS")
		raw_text = "".join(
			part.get("text", "")
			for part in (candidate.get("content") or {}).get("parts") or []
			if not part.get("thought")
		)
		if not raw_text:
			raise AIError(_("Gemini returned no candidate text: {0}").format(json.dumps(payload)[:500]))
		return raw_text

	def _call_responses(self, request):
		"""OpenAI Responses API."""
		content = [{"type": "input_text", "text": request["text"]}]
		for data, mimetype in request["files"]:
			if mimetype == "application/pdf":
				content.append(
					{
						"type": "input_file",
						"filename": "document.pdf",
						"file_data": data_uri(data, mimetype),
					}
				)
			else:
				content.append({"type": "input_image", "image_url": data_uri(data, mimetype)})
		body = {
			"model": self.model_name,
			"store": False,
			"instructions": request["instructions"],
			"input": [{"role": "user", "content": content}],
			"max_output_tokens": self.max_output_tokens,
			**self._temperature(),
		}
		if request["schema"]:
			body["text"] = {
				"format": {
					"type": "json_schema",
					"name": request["schema_name"],
					"schema": request["schema"],
					"strict": False,
				}
			}
		if self.reasoning_effort:
			body["reasoning"] = {"effort": self.reasoning_effort.lower()}
		payload = self._request(
			f"{self._base_url()}/responses",
			{"Authorization": f"Bearer {request['api_key']}"},
			body,
			request["timeout"],
		)
		usage = payload.get("usage", {})
		request["usage"] = {
			"tokens_in": usage.get("input_tokens", 0),
			"tokens_out": usage.get("output_tokens", 0),
		}
		self._check_truncated((payload.get("incomplete_details") or {}).get("reason") == "max_output_tokens")
		raw_text = "".join(
			part.get("text", "")
			for item in payload.get("output", [])
			for part in item.get("content") or []
			if part.get("type") == "output_text"
		)
		if not raw_text:
			raise AIError(_("OpenAI returned no output text."))
		return raw_text

	def _chat_messages(self, request):
		"""System + user messages in the chat-completions shape."""
		user_content = request["text"]
		if request["files"]:
			user_content = [
				{"type": "text", "text": request["text"]},
				*(
					{"type": "image_url", "image_url": {"url": data_uri(data, mimetype)}}
					for data, mimetype in request["files"]
				),
			]
		return [
			{"role": "system", "content": request["instructions"]},
			{"role": "user", "content": user_content},
		]

	def _call_chat(self, request):
		"""Chat completions: xAI Grok, Azure OpenAI, Mistral, Perplexity, NVIDIA NIM."""
		# Azure GPT-5 and o-series deployments reject max_tokens; every Azure model accepts
		# max_completion_tokens
		limit_key = "max_completion_tokens" if self.provider_type == "Azure OpenAI" else "max_tokens"
		body = {
			"model": self.model_name,
			limit_key: self.max_output_tokens,
			"messages": self._chat_messages(request),
			**self._temperature(),
		}
		if self.reasoning_effort:
			body["reasoning_effort"] = self.reasoning_effort.lower()
		if request["schema"]:
			body["response_format"] = {
				"type": "json_schema",
				"json_schema": {
					"name": request["schema_name"],
					"schema": strict_schema(request["schema"]),
				},
			}
		if self.provider_type == "Azure OpenAI":
			headers = {"api-key": request["api_key"]}
		else:
			headers = {"Authorization": f"Bearer {request['api_key']}"}
		payload = self._request(f"{self._base_url()}/chat/completions", headers, body, request["timeout"])
		usage = payload.get("usage") or {}
		request["usage"] = {
			"tokens_in": usage.get("prompt_tokens", 0),
			"tokens_out": usage.get("completion_tokens", 0),
		}
		try:
			choice = payload["choices"][0]
			raw_text = choice["message"]["content"]
		except (KeyError, IndexError, TypeError) as error:
			raise AIError(
				_("Provider returned no message content: {0}").format(json.dumps(payload)[:500])
			) from error
		self._check_truncated(choice.get("finish_reason") == "length")
		if not raw_text:
			raise AIError(_("{0} returned an empty answer.").format(self.name))
		return raw_text

	def _call_cohere(self, request):
		"""Cohere v2 chat."""
		body = {
			"model": self.model_name,
			"max_tokens": self.max_output_tokens,
			"messages": self._chat_messages(request),
			**self._temperature(),
		}
		if request["schema"]:
			body["response_format"] = {
				"type": "json_object",
				"json_schema": strict_schema(request["schema"]),
			}
		payload = self._request(
			f"{self._base_url()}/chat",
			{"Authorization": f"Bearer {request['api_key']}"},
			body,
			request["timeout"],
		)
		tokens = (payload.get("usage") or {}).get("tokens") or {}
		request["usage"] = {
			"tokens_in": int(tokens.get("input_tokens", 0)),
			"tokens_out": int(tokens.get("output_tokens", 0)),
		}
		self._check_truncated(payload.get("finish_reason") == "MAX_TOKENS")
		raw_text = "".join(
			part.get("text", "")
			for part in (payload.get("message") or {}).get("content") or []
			if part.get("type") == "text"
		)
		if not raw_text:
			raise AIError(_("Cohere returned no text."))
		return raw_text


@frappe.whitelist()
def model_options(provider_type, txt=""):
	"""Models listed for a provider type, for the Model field's dropdown."""
	return frappe.get_all(
		"KI AI Model",
		filters={"provider_type": provider_type, "model_name": ["like", f"%{txt}%"]},
		pluck="model_name",
		order_by="model_name",
		limit=100,
	)


@frappe.whitelist()
def get_kind_defaults(provider_type):
	"""Field values the form sets when the provider type changes."""
	return kind_defaults(provider_type)
