import io
import json
from unittest.mock import MagicMock

import frappe
from frappe.tests import IntegrationTestCase

PROVIDER_MODULE = "ki_erp_ai_base.ai_base.doctype.ki_ai_provider.ki_ai_provider"
POST = f"{PROVIDER_MODULE}.requests.post"
GET = f"{PROVIDER_MODULE}.requests.get"
SLEEP = f"{PROVIDER_MODULE}.sleep"
PDF = b"%PDF-1.4 contract test"
SCHEMA = {
	"type": "object",
	"properties": {"total": {"type": "object", "properties": {"value": {"type": "string"}}}},
	"required": ["total"],
}
ANSWER = {"total": {"value": "121.00"}}


def photo(width=1200, height=1600, orientation=None, mode="RGB", fmt="JPEG"):
	from PIL import Image

	image = Image.new(mode, (width, height), "white")
	output = io.BytesIO()
	kwargs = {}
	if orientation:
		exif = Image.Exif()
		exif[0x0112] = orientation
		kwargs["exif"] = exif
	image.save(output, format=fmt, **kwargs)
	return output.getvalue()


def response(status, payload):
	result = MagicMock(status_code=status, text=json.dumps(payload))
	result.json.return_value = payload
	return result


def chat_reply(text, prompt_tokens=30, completion_tokens=10):
	return {
		"choices": [{"message": {"role": "assistant", "content": text}}],
		"usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
	}


#: A valid JSON reply in each protocol's shape.
REPLIES = {
	"OpenAI": {
		"usage": {"input_tokens": 8, "output_tokens": 2},
		"output": [
			{"type": "reasoning", "summary": []},
			{"type": "message", "content": [{"type": "output_text", "text": json.dumps(ANSWER)}]},
		],
	},
	"Anthropic Claude": {
		"stop_reason": "end_turn",
		"usage": {"input_tokens": 8, "output_tokens": 2},
		"content": [{"type": "thinking", "thinking": "..."}, {"type": "text", "text": json.dumps(ANSWER)}],
	},
	"Google Gemini": {
		"usageMetadata": {"promptTokenCount": 8, "candidatesTokenCount": 2},
		"candidates": [{"content": {"parts": [{"text": json.dumps(ANSWER)}]}}],
	},
	"xAI Grok": chat_reply(json.dumps(ANSWER), 8, 2),
	"Azure OpenAI": chat_reply(json.dumps(ANSWER), 8, 2),
	"Mistral AI": chat_reply(json.dumps(ANSWER), 8, 2),
	"Perplexity": chat_reply(json.dumps(ANSWER), 8, 2),
	"NVIDIA NIM": chat_reply(json.dumps(ANSWER), 8, 2),
	"Cohere": {
		"message": {"role": "assistant", "content": [{"type": "text", "text": json.dumps(ANSWER)}]},
		"usage": {"tokens": {"input_tokens": 8, "output_tokens": 2}},
	},
}


class ProviderTestCase(IntegrationTestCase):
	def make_provider(self, provider_type, **values):
		if provider_type == "Azure OpenAI":
			values.setdefault("base_url", "https://acme.openai.azure.com/openai/v1/")
		return frappe.get_doc(
			{
				"doctype": "KI AI Provider",
				"provider_name": f"{provider_type} {frappe.generate_hash(length=8)}",
				"provider_type": provider_type,
				"model_name": "test-model",
				"api_key": "secret-key",
				"send_temperature": 1,
				"temperature": 0.3,
				"max_output_tokens": 1234,
				**values,
			}
		).insert()

	def logs(self, provider, field="status"):
		return frappe.get_all("KI AI Usage Log", filters={"provider": provider.name}, pluck=field)

	def make_user(self, email, role):
		if not frappe.db.exists("User", email):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": email,
					"first_name": "AI",
					"send_welcome_email": 0,
					"roles": [{"role": role}],
				}
			).insert(ignore_permissions=True)
		return email
