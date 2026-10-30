"""Provider types, errors and file helpers shared by the AI provider DocType and the apps using it."""

import base64
import io

import frappe
from frappe import _

ANTHROPIC_VERSION = "2023-06-01"

#: Provider types, keyed by the value of the Provider Type field. ``protocol`` picks the request
#: adapter; ``pdf`` tells whether the API reads PDF files itself (the others take images only).
PROVIDER_KINDS = {
	"OpenAI": {
		"protocol": "responses",
		"pdf": True,
		"base": "https://api.openai.com/v1",
		"model": "gpt-5-mini",
	},
	"Anthropic Claude": {
		"protocol": "anthropic",
		"pdf": True,
		"base": "https://api.anthropic.com/v1",
		"model": "claude-sonnet-5-5",
	},
	"Google Gemini": {
		"protocol": "gemini",
		"pdf": True,
		"base": "https://generativelanguage.googleapis.com/v1beta",
		"model": "gemini-3.8-flash",
	},
	"xAI Grok": {
		"protocol": "chat",
		"pdf": False,
		"base": "https://api.x.ai/v1",
		"model": "grok-4",
	},
	"Azure OpenAI": {
		"protocol": "chat",
		"pdf": False,
		"base": "",
		"model": "",
	},
	"Mistral AI": {
		"protocol": "chat",
		"pdf": False,
		"base": "https://api.mistral.ai/v1",
		"model": "mistral-medium-latest",
	},
	"Cohere": {
		"protocol": "cohere",
		"pdf": False,
		"base": "https://api.cohere.com/v2",
		"model": "command-a-vision-07-2025",
	},
	"Perplexity": {
		"protocol": "chat",
		"pdf": False,
		"base": "https://api.perplexity.ai",
		"model": "sonar-pro",
	},
	"NVIDIA NIM": {
		"protocol": "chat",
		"pdf": False,
		"base": "https://integrate.api.nvidia.com/v1",
		"model": "nvidia/nemotron-3.5-super-vl-120b-a12b",
	},
}

#: Default seconds allowed per call, set per provider in ``timeout``. Callers running in a
#: request pass a shorter budget through the ``timeout`` argument of ``generate``.
PROVIDER_TIMEOUT = 70

#: HTTP statuses that mean "try again later" (rate limits, overload, outages).
TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}

#: Seconds to wait before each retry of a transient failure. Retries share the call's timeout
#: budget and stop when too little of it is left.
RETRY_DELAYS = (2, 5)
MIN_RETRY_SECONDS = 15

#: Longest image side sent to providers. Phone photos are often 4000px+; handwriting stays
#: legible at this size and requests stay small and fast.
MAX_IMAGE_SIDE = 2400


class AIError(frappe.ValidationError):
	"""Permanent failure: bad key, refused, invalid or truncated output, unreadable file."""


class ProviderBusy(AIError):
	"""Temporary provider failure (timeout, rate limit, outage): retry later."""


class BudgetExceeded(AIError):
	"""The provider's monthly token cap is spent."""


class FileNotSupported(AIError):
	"""The provider cannot read this type of file (PDF or image)."""


def kind_defaults(provider_type):
	"""Field values a new provider of this type starts with."""
	spec = PROVIDER_KINDS.get(provider_type, {})
	return {
		"model_name": spec.get("model", ""),
		# OpenAI's GPT-5 models accept no temperature
		"send_temperature": int(provider_type != "OpenAI"),
	}


def prepare_file(raw):
	"""Return ``(bytes, mimetype)`` ready to send to a provider.

	PDFs pass through unchanged. Images (JPEG, PNG, WebP, TIFF, BMP, GIF...) are rotated per their
	EXIF orientation, downscaled and re-encoded as JPEG, which every provider accepts.
	"""
	if raw.startswith(b"%PDF"):
		return raw, "application/pdf"
	from PIL import Image, ImageOps, UnidentifiedImageError

	try:
		with Image.open(io.BytesIO(raw)) as image:
			image = ImageOps.exif_transpose(image)
			image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
			if image.mode != "RGB":
				background = Image.new("RGB", image.size, "white")
				rgba = image.convert("RGBA")
				background.paste(rgba, mask=rgba.getchannel("A"))
				image = background
			output = io.BytesIO()
			image.save(output, format="JPEG", quality=90)
	except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
		raise AIError(
			_("This file is neither a PDF nor a readable image (JPEG, PNG, WebP, TIFF).")
		) from error
	return output.getvalue(), "image/jpeg"


def strict_schema(schema):
	"""Deep copy of ``schema`` with ``additionalProperties: false`` on every object, as strict
	JSON-schema modes require. Gemini rejects ``additionalProperties`` and gets the plain schema."""
	if isinstance(schema, dict):
		result = {key: strict_schema(value) for key, value in schema.items()}
		if result.get("type") == "object":
			result["additionalProperties"] = False
		return result
	if isinstance(schema, list):
		return [strict_schema(item) for item in schema]
	return schema


def data_uri(data, mimetype):
	return f"data:{mimetype};base64," + base64.b64encode(data).decode()


def error_detail(response):
	"""Human-readable message from a provider error response."""
	try:
		data = response.json()
	except ValueError:
		return response.text[:500]
	if isinstance(data, list) and data:
		data = data[0]
	if isinstance(data, dict):
		error = data.get("error")
		if isinstance(error, dict) and error.get("message"):
			return str(error["message"])[:500]
		for candidate in (error, data.get("detail"), data.get("message"), data.get("title")):
			if isinstance(candidate, str) and candidate:
				return candidate[:500]
	return response.text[:500]
