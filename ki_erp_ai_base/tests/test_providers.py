import base64
import io
from unittest.mock import patch

import frappe
import requests
from frappe.client import get as client_get

from ki_erp_ai_base.ai_base.doctype.ki_ai_provider.ki_ai_provider import model_options
from ki_erp_ai_base.providers import (
	ANTHROPIC_VERSION,
	PROVIDER_KINDS,
	AIError,
	BudgetExceeded,
	FileNotSupported,
	ProviderBusy,
	prepare_file,
)
from ki_erp_ai_base.tests.utils import (
	ANSWER,
	GET,
	PDF,
	POST,
	REPLIES,
	SCHEMA,
	SLEEP,
	ProviderTestCase,
	chat_reply,
	photo,
	response,
)

URLS = {
	"OpenAI": "https://api.openai.com/v1/responses",
	"Anthropic Claude": "https://api.anthropic.com/v1/messages",
	"Google Gemini": "https://generativelanguage.googleapis.com/v1beta/models/test-model:generateContent",
	"xAI Grok": "https://api.x.ai/v1/chat/completions",
	"Azure OpenAI": "https://acme.openai.azure.com/openai/v1/chat/completions",
	"Mistral AI": "https://api.mistral.ai/v1/chat/completions",
	"Perplexity": "https://api.perplexity.ai/chat/completions",
	"NVIDIA NIM": "https://integrate.api.nvidia.com/v1/chat/completions",
	"Cohere": "https://api.cohere.com/v2/chat",
}


class TestProviderContracts(ProviderTestCase):
	"""Request and response shapes for each provider API, without network."""

	def call(self, provider, reply, **kwargs):
		kwargs.setdefault("json_schema", SCHEMA)
		with patch(POST, return_value=reply) as post:
			result = provider.generate("Be precise.", **kwargs)
		return result, post.call_args

	def raised(self, provider, reply=None, side_effect=None):
		"""Call and return the raised error."""
		with patch(POST, return_value=reply, side_effect=side_effect), patch(SLEEP):
			with self.assertRaises(AIError) as raised:
				provider.generate("Be precise.", text="x", json_schema=SCHEMA)
		return raised.exception

	def test_provider_types_match_the_doctypes(self):
		for doctype in ("KI AI Provider", "KI AI Model"):
			options = frappe.get_meta(doctype).get_field("provider_type").options.split("\n")
			self.assertEqual(options, list(PROVIDER_KINDS), doctype)
		self.assertEqual(set(REPLIES), set(PROVIDER_KINDS))

	def test_every_provider_url_auth_limits_and_usage(self):
		reference = frappe.get_doc("Role", "AI User")
		for kind, reply in REPLIES.items():
			provider = self.make_provider(kind)
			(data, usage), call = self.call(
				provider, response(200, reply), text="Invoice text", feature="test", reference=reference
			)
			self.assertEqual(call.args[0], URLS[kind], kind)
			headers, body = call.kwargs["headers"], call.kwargs["json"]
			if kind == "Anthropic Claude":
				self.assertEqual(headers["x-api-key"], "secret-key")
				self.assertEqual(headers["anthropic-version"], ANTHROPIC_VERSION)
			elif kind == "Google Gemini":
				self.assertEqual(headers["x-goog-api-key"], "secret-key")
			elif kind == "Azure OpenAI":
				self.assertEqual(headers["api-key"], "secret-key")
			else:
				self.assertEqual(headers["Authorization"], "Bearer secret-key", kind)
			# Temperature and the output limit from the provider settings
			if kind == "Google Gemini":
				config = body["generationConfig"]
				self.assertEqual((config["temperature"], config["maxOutputTokens"]), (0.3, 1234))
			else:
				self.assertEqual(body["temperature"], 0.3, kind)
				limit_key = {"OpenAI": "max_output_tokens", "Azure OpenAI": "max_completion_tokens"}.get(
					kind, "max_tokens"
				)
				self.assertEqual(body[limit_key], 1234, kind)
			self.assertNotIn("tools", body, kind)
			self.assertEqual(data, ANSWER, kind)
			self.assertEqual((usage["tokens_in"], usage["tokens_out"]), (8, 2), kind)
			log = frappe.get_doc("KI AI Usage Log", usage["usage_log"])
			self.assertEqual((log.provider, log.tokens_total, log.status), (provider.name, 10, "Done"), kind)
			self.assertEqual((log.feature, log.user), ("test", "Administrator"), kind)
			self.assertEqual((log.reference_doctype, log.reference_name), ("Role", "AI User"), kind)

	def test_structured_output_per_protocol(self):
		bodies = {}
		for kind in ("OpenAI", "Anthropic Claude", "Google Gemini", "xAI Grok", "Cohere"):
			__, call = self.call(self.make_provider(kind), response(200, REPLIES[kind]), text="x")
			bodies[kind] = call.kwargs["json"]
		self.assertEqual(bodies["OpenAI"]["text"]["format"]["type"], "json_schema")
		self.assertEqual(bodies["OpenAI"]["instructions"], "Be precise.")
		self.assertFalse(bodies["OpenAI"]["store"])
		schema = bodies["Anthropic Claude"]["output_config"]["format"]["schema"]
		self.assertFalse(schema["additionalProperties"])
		self.assertEqual(bodies["Anthropic Claude"]["system"], "Be precise.")
		self.assertNotIn(
			"additionalProperties", bodies["Google Gemini"]["generationConfig"]["responseSchema"]
		)
		self.assertEqual(bodies["xAI Grok"]["response_format"]["type"], "json_schema")
		self.assertEqual(bodies["xAI Grok"]["messages"][0], {"role": "system", "content": "Be precise."})
		self.assertEqual(bodies["Cohere"]["response_format"]["type"], "json_object")

	def test_reasoning_effort_per_protocol(self):
		bodies = {}
		for kind in ("OpenAI", "Anthropic Claude", "Google Gemini", "xAI Grok", "Azure OpenAI"):
			provider = self.make_provider(kind, reasoning_effort="High")
			__, call = self.call(provider, response(200, REPLIES[kind]), text="x")
			bodies[kind] = call.kwargs["json"]
		self.assertEqual(bodies["OpenAI"]["reasoning"], {"effort": "high"})
		self.assertEqual(bodies["Anthropic Claude"]["output_config"]["effort"], "high")
		self.assertIn("format", bodies["Anthropic Claude"]["output_config"])
		self.assertEqual(
			bodies["Google Gemini"]["generationConfig"]["thinkingConfig"], {"thinkingLevel": "high"}
		)
		self.assertEqual(bodies["xAI Grok"]["reasoning_effort"], "high")
		self.assertEqual(bodies["Azure OpenAI"]["reasoning_effort"], "high")
		# Empty sends nothing, so the provider default applies
		__, call = self.call(self.make_provider("OpenAI"), response(200, REPLIES["OpenAI"]), text="x")
		self.assertNotIn("reasoning", call.kwargs["json"])

	def test_plain_text_answer_and_temperature_off(self):
		provider = self.make_provider("Mistral AI", send_temperature=0)
		(answer, __), call = self.call(
			provider, response(200, chat_reply("OK")), text="ping", json_schema=None
		)
		self.assertEqual(answer, "OK")
		self.assertNotIn("temperature", call.kwargs["json"])
		self.assertNotIn("response_format", call.kwargs["json"])

	def test_files_use_each_api_format(self):
		image = photo()
		bodies = {}
		for kind in ("OpenAI", "Anthropic Claude", "Google Gemini", "xAI Grok", "Cohere"):
			__, call = self.call(self.make_provider(kind), response(200, REPLIES[kind]), files=[image])
			bodies[kind] = call.kwargs["json"]
		self.assertEqual(
			bodies["Google Gemini"]["contents"][0]["parts"][-1]["inline_data"]["mime_type"], "image/jpeg"
		)
		block = bodies["Anthropic Claude"]["messages"][0]["content"][0]
		self.assertEqual((block["type"], block["source"]["media_type"]), ("image", "image/jpeg"))
		self.assertEqual(bodies["OpenAI"]["input"][0]["content"][-1]["type"], "input_image")
		for kind in ("xAI Grok", "Cohere"):
			part = bodies[kind]["messages"][1]["content"][-1]
			self.assertTrue(part["image_url"]["url"].startswith("data:image/jpeg;base64,"), kind)
		# PDFs: native for OpenAI / Claude / Gemini, refused before any call elsewhere
		__, call = self.call(
			self.make_provider("Anthropic Claude"), response(200, REPLIES["Anthropic Claude"]), files=[PDF]
		)
		document = call.kwargs["json"]["messages"][0]["content"][0]
		self.assertEqual(base64.b64decode(document["source"]["data"]), PDF)
		__, call = self.call(self.make_provider("OpenAI"), response(200, REPLIES["OpenAI"]), files=[PDF])
		self.assertEqual(call.kwargs["json"]["input"][0]["content"][-1]["type"], "input_file")
		grok = self.make_provider("xAI Grok")
		self.assertFalse(grok.can_read("application/pdf"))
		with patch(POST) as post, self.assertRaisesRegex(FileNotSupported, "cannot read PDF"):
			grok.generate("p", files=[PDF])
		post.assert_not_called()
		blind = self.make_provider("OpenAI", supports_vision=0)
		with patch(POST) as post, self.assertRaisesRegex(FileNotSupported, "cannot read image"):
			blind.generate("p", files=[image])
		post.assert_not_called()

	def test_errors_are_logged_and_classified(self):
		provider = self.make_provider("Anthropic Claude")
		refused = {"stop_reason": "refusal", "content": [], "usage": {"input_tokens": 40, "output_tokens": 3}}
		self.assertIn("refusal", str(self.raised(provider, response(200, refused))))
		overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
		error = self.raised(provider, response(529, overloaded))
		self.assertIsInstance(error, ProviderBusy)
		self.assertRegex(str(error), "HTTP 529.*Overloaded")
		bad_key = {"type": "error", "error": {"message": "invalid x-api-key"}}
		error = self.raised(provider, response(401, bad_key))
		self.assertNotIsInstance(error, ProviderBusy)
		self.assertIn("invalid x-api-key", str(error))
		for failure in (requests.Timeout(), requests.ConnectionError()):
			self.assertIsInstance(self.raised(provider, side_effect=failure), ProviderBusy)
		grok = self.make_provider("xAI Grok")
		self.assertIn("not valid JSON", str(self.raised(grok, response(200, chat_reply("x")))))
		gateway_page = response(200, None)
		gateway_page.json.side_effect = ValueError
		gateway_page.text = "<html>Gateway login</html>"
		self.assertIn("Gateway login", str(self.raised(grok, gateway_page)))
		self.assertEqual(
			sorted(self.logs(provider)),
			["Error", "Error", "Temporary Failure", "Temporary Failure", "Temporary Failure"],
		)
		# A billed answer that is refused still counts its tokens
		self.assertEqual(sum(self.logs(provider, "tokens_total")), 43)

	def test_transient_failures_are_retried(self):
		provider = self.make_provider("Anthropic Claude")
		busy = response(503, {"error": {"message": "high demand"}})
		with (
			patch(
				POST,
				side_effect=[busy, requests.ConnectionError(), response(200, REPLIES["Anthropic Claude"])],
			) as post,
			patch(SLEEP) as sleep,
		):
			result, _usage = provider.generate("p", text="x", json_schema=SCHEMA)
		self.assertEqual(result, ANSWER)
		self.assertEqual(post.call_count, 3)
		self.assertEqual(sleep.call_count, 2)
		# Every attempt shares one budget instead of getting a fresh timeout
		self.assertTrue(all(call.kwargs["timeout"] <= 70 for call in post.call_args_list))
		# Gives up after the last retry, and never retries a timeout or a budget too short to try
		# again
		with patch(POST, return_value=busy) as post, patch(SLEEP):
			self.assertRaises(ProviderBusy, provider.generate, "p", text="x")
		self.assertEqual(post.call_count, 3)
		with patch(POST, side_effect=requests.Timeout()) as post, patch(SLEEP):
			self.assertRaises(ProviderBusy, provider.generate, "p", text="x")
		self.assertEqual(post.call_count, 1)
		with patch(POST, return_value=busy) as post, patch(SLEEP):
			self.assertRaises(ProviderBusy, provider.generate, "p", text="x", timeout=10)
		self.assertEqual(post.call_count, 1)

	def test_timeout_is_set_per_provider(self):
		provider = self.make_provider("Anthropic Claude", timeout=30)
		reply = response(200, REPLIES["Anthropic Claude"])
		with patch(POST, return_value=reply) as post:
			provider.generate("p", text="x")
		self.assertLessEqual(post.call_args.kwargs["timeout"], 30)
		self.assertGreater(post.call_args.kwargs["timeout"], 25)
		with patch(POST, return_value=reply) as post:
			provider.generate("p", text="x", timeout=10)
		self.assertLessEqual(post.call_args.kwargs["timeout"], 10)
		self.assertRegex(str(self.raised(provider, side_effect=requests.Timeout())), "within 30 seconds")
		provider.timeout = 0
		self.assertRaises(frappe.ValidationError, provider.save)

	def test_fetch_models_per_protocol(self):
		listings = {
			"OpenAI": (
				"https://api.openai.com/v1/models",
				"Authorization",
				{"data": [{"id": "gpt-a"}, {"id": "gpt-b"}]},
				["gpt-a", "gpt-b"],
			),
			"Anthropic Claude": (
				"https://api.anthropic.com/v1/models?limit=1000",
				"x-api-key",
				{"data": [{"id": "claude-a"}]},
				["claude-a"],
			),
			"Google Gemini": (
				"https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000",
				"x-goog-api-key",
				{
					"models": [
						{"name": "models/gemini-a", "supportedGenerationMethods": ["generateContent"]},
						{"name": "models/embed-a", "supportedGenerationMethods": ["embedContent"]},
					]
				},
				["gemini-a"],
			),
			"Cohere": (
				"https://api.cohere.com/v1/models?endpoint=chat&page_size=1000",
				"Authorization",
				{"models": [{"name": "command-a"}]},
				["command-a"],
			),
			"Mistral AI": (
				"https://api.mistral.ai/v1/models",
				"Authorization",
				{"data": [{"id": "mistral-a"}]},
				["mistral-a"],
			),
			"Azure OpenAI": (
				"https://acme.openai.azure.com/openai/v1/models",
				"api-key",
				{"data": [{"id": "gpt-azure"}]},
				["gpt-azure"],
			),
		}
		for kind, (url, auth, payload, names) in listings.items():
			provider = self.make_provider(kind)
			with patch(GET, return_value=response(200, payload)) as get:
				provider.fetch_models()
			self.assertEqual(get.call_args.args[0], url, kind)
			self.assertIn(auth, get.call_args.kwargs["headers"], kind)
			options = model_options(kind)
			self.assertTrue(set(names) <= set(options), kind)
			self.assertNotIn("embed-a", options)

	def test_model_dropdown_follows_model_name(self):
		provider = self.make_provider("Mistral AI", model_name="mistral-x")
		self.assertIn("mistral-x", model_options("Mistral AI"))
		self.assertNotIn("mistral-x", model_options("Cohere"))
		self.assertEqual(model_options("Mistral AI", "stral-x"), ["mistral-x"])
		provider.model_name = "mistral-z"
		provider.save()
		self.assertIn("mistral-z", model_options("Mistral AI"))
		duplicate = {"doctype": "KI AI Model", "provider_type": "Mistral AI", "model_name": "mistral-z"}
		self.assertRaises(frappe.DuplicateEntryError, frappe.get_doc(duplicate).insert)
		with (
			patch(GET, return_value=response(200, {"data": []})),
			self.assertRaisesRegex(frappe.ValidationError, "listed no models"),
		):
			provider.fetch_models()
		user = self.make_user("ki-ai-user@example.test", "AI User")
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(user)
		with patch(GET) as get, self.assertRaises(frappe.PermissionError):
			frappe.get_doc("KI AI Provider", provider.name).fetch_models()
		get.assert_not_called()

	def test_truncated_or_empty_answers_are_refused(self):
		truncated = {
			"OpenAI": {
				"status": "incomplete",
				"incomplete_details": {"reason": "max_output_tokens"},
				"usage": {"input_tokens": 8, "output_tokens": 1234},
				"output": [{"type": "message", "content": [{"type": "output_text", "text": "Par"}]}],
			},
			"Anthropic Claude": {
				"stop_reason": "max_tokens",
				"usage": {"input_tokens": 8, "output_tokens": 1234},
				"content": [{"type": "text", "text": "Par"}],
			},
			"Google Gemini": {
				"usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 1234},
				"candidates": [{"finishReason": "MAX_TOKENS", "content": {"parts": [{"text": "Par"}]}}],
			},
			"xAI Grok": dict(
				chat_reply("Par", 8, 1234),
				choices=[{"finish_reason": "length", "message": {"content": "Par"}}],
			),
			"Cohere": {
				"finish_reason": "MAX_TOKENS",
				"message": {"content": [{"type": "text", "text": "Par"}]},
				"usage": {"tokens": {"input_tokens": 8, "output_tokens": 1234}},
			},
		}
		for kind, reply in truncated.items():
			provider = self.make_provider(kind)
			with patch(POST, return_value=response(200, reply)):
				with self.assertRaisesRegex(AIError, "limit of 1234 output tokens"):
					provider.generate("p", text="x")
			self.assertEqual(
				(self.logs(provider), self.logs(provider, "tokens_out")), (["Error"], [1234]), kind
			)
		blocked = {"promptFeedback": {"blockReason": "SAFETY"}, "usageMetadata": {"promptTokenCount": 8}}
		error = self.raised(self.make_provider("Google Gemini"), response(200, blocked))
		self.assertIn("no candidate text", str(error))

	def test_monthly_cap_and_limits(self):
		provider = self.make_provider("xAI Grok", monthly_token_cap=15)
		self.call(provider, response(200, REPLIES["xAI Grok"]), text="x")
		provider = frappe.get_doc("KI AI Provider", provider.name)
		self.assertEqual((provider.month_tokens_in, provider.month_tokens_out), (8, 2))
		self.assertFalse(provider.budget_reached())
		self.call(provider, response(200, REPLIES["xAI Grok"]), text="x")
		provider = frappe.get_doc("KI AI Provider", provider.name)
		self.assertEqual((provider.month_tokens_total, provider.month_calls), (20, 2))
		self.assertEqual(provider.month_cap_used, 100.0)
		self.assertEqual(provider.as_dict()["month_tokens_total"], 20)  # shown on the form
		with patch(POST) as post, self.assertRaises(BudgetExceeded):
			provider.generate("p", text="x")
		post.assert_not_called()
		provider.temperature = 3.0
		self.assertRaises(frappe.ValidationError, provider.save)

	def test_temperature_off_by_default_for_openai_only(self):
		def make(provider_type):
			return frappe.get_doc(
				{
					"doctype": "KI AI Provider",
					"provider_name": f"default {provider_type} {frappe.generate_hash(length=6)}",
					"provider_type": provider_type,
				}
			).insert()

		openai, claude = make("OpenAI"), make("Anthropic Claude")
		self.assertEqual((openai.send_temperature, claude.send_temperature), (0, 1))
		# The type's default model when none is given
		self.assertEqual((openai.model_name, claude.model_name), ("gpt-5-mini", "claude-sonnet-5-5"))

	def test_missing_key_or_azure_url(self):
		with self.assertRaisesRegex(AIError, "no API key"):
			self.make_provider("OpenAI", api_key=None).generate("p", text="x")
		azure = self.make_provider("Azure OpenAI", base_url=None)
		with patch(POST) as post, self.assertRaisesRegex(AIError, "needs a Base URL"):
			azure.generate("p", text="x")
		post.assert_not_called()

	def test_api_key_hidden_from_users(self):
		provider = self.make_provider("OpenAI")
		user = self.make_user("ki-ai-user@example.test", "AI User")
		self.addCleanup(frappe.set_user, "Administrator")
		frappe.set_user(user)
		doc = client_get("KI AI Provider", provider.name)
		self.assertEqual(doc["provider_name"], provider.provider_name)
		self.assertIsNone(doc.get("api_key"))
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc("KI AI Provider", provider.name).test_connection()

	def test_test_connection_returns_the_result(self):
		provider = self.make_provider("xAI Grok")
		with patch(POST, return_value=response(200, chat_reply("OK", 3, 1))):
			result = provider.test_connection()
		self.assertTrue(result["ok"])
		self.assertIn('answered "OK", 4 tokens', result["message"])
		self.assertTrue(
			frappe.db.get_value("KI AI Provider", provider.name, "last_health_check").endswith("OK")
		)
		with patch(POST, return_value=response(401, {"error": {"message": "bad key"}})):
			result = provider.test_connection()
		self.assertEqual((result["ok"], result["busy"]), (False, False))
		self.assertIn("bad key", result["message"])
		self.assertTrue(
			frappe.db.get_value("KI AI Provider", provider.name, "last_health_check").endswith("failed")
		)
		self.assertEqual(sorted(self.logs(provider, "feature")), ["health_check", "health_check"])

	def test_prepare_file_images(self):
		from PIL import Image

		self.assertEqual(prepare_file(PDF), (PDF, "application/pdf"))
		data, mimetype = prepare_file(photo(4000, 3000, orientation=6))
		self.assertEqual(mimetype, "image/jpeg")
		with Image.open(io.BytesIO(data)) as image:
			self.assertEqual(image.size, (1800, 2400))  # upright and downscaled
		data, mimetype = prepare_file(photo(800, 600, mode="RGBA", fmt="PNG"))
		self.assertEqual(mimetype, "image/jpeg")
		for fmt in ("WEBP", "TIFF"):
			self.assertEqual(prepare_file(photo(600, 800, fmt=fmt))[1], "image/jpeg", fmt)
		with self.assertRaisesRegex(AIError, "neither a PDF nor a readable image"):
			prepare_file(b"GIF-not-really")
