from unittest.mock import patch

import frappe
import requests

from ki_erp_ai_base import usage
from ki_erp_ai_base.api import AIError, generate, get_providers
from ki_erp_ai_base.tests.utils import PDF, POST, REPLIES, ProviderTestCase, chat_reply, response

COMPANY = "Kalvyai Demo"  # demo data of the development site


class TestApi(ProviderTestCase):
	def test_generate_moves_to_the_next_provider(self):
		first = self.make_provider("xAI Grok", priority=1)
		second = self.make_provider("Mistral AI", priority=2)
		# Busy: the next provider answers
		with patch(POST, side_effect=[requests.Timeout(), response(200, chat_reply("OK"))]) as post:
			answer, call_usage = generate("p", providers=[first, second], text="x")
		self.assertEqual((answer, call_usage["provider"], post.call_count), ("OK", second.name, 2))
		self.assertEqual(self.logs(first), ["Temporary Failure"])
		# A file the first one cannot read: skipped without a call
		claude = self.make_provider("Anthropic Claude")
		with patch(POST, return_value=response(200, REPLIES["Anthropic Claude"])) as post:
			__, call_usage = generate("p", providers=[first, claude], files=[PDF])
		self.assertEqual((call_usage["provider"], post.call_count), (claude.name, 1))
		# A permanent failure is raised: the next provider would most likely fail the same way
		with patch(POST, return_value=response(400, {"error": {"message": "bad request"}})) as post:
			self.assertRaisesRegex(AIError, "bad request", generate, "p", providers=[first, second], text="x")
		self.assertEqual(post.call_count, 1)
		# Every provider busy: the last error
		with patch(POST, side_effect=requests.Timeout()):
			self.assertRaisesRegex(
				AIError, f"^{second.name} did not answer", generate, "p", providers=[first, second], text="x"
			)
		self.assertRaisesRegex(AIError, "No AI provider", generate, "p", providers=[], text="x")

	def test_get_providers_by_priority_and_company(self):
		late = self.make_provider("OpenAI", priority=30)
		early = self.make_provider("OpenAI", priority=5, company=COMPANY)
		shared = self.make_provider("OpenAI", priority=20)
		disabled = self.make_provider("OpenAI", priority=1, enabled=0)
		other = frappe.get_all("Company", filters={"name": ["!=", COMPANY]}, pluck="name", limit=1)
		mine = {late.name, early.name, shared.name, disabled.name}

		def names(company=None):
			return [p.name for p in get_providers(company) if p.name in mine]

		self.assertEqual(names(), [early.name, shared.name, late.name])
		self.assertEqual(names(COMPANY), [early.name, shared.name, late.name])
		if other:
			self.assertEqual(names(other[0]), [shared.name, late.name])

	def test_usage_rows_survive_a_rollback(self):
		provider = self.make_provider("xAI Grok", company=COMPANY)
		with patch(POST, return_value=response(401, {"error": {"message": "bad key"}})):
			self.assertRaises(AIError, provider.generate, "p", text="x")
		rows = list(frappe.local.ki_ai_unsaved_logs)
		row = next(row for row in rows if row["provider"] == provider.name)
		self.assertEqual((row["status"], row["company"]), ("Error", COMPANY))
		# What the rollback callback does outside tests
		frappe.flags.in_test = False
		try:
			with patch("frappe.enqueue") as enqueue:
				usage._requeue()
		finally:
			frappe.flags.in_test = True
		self.assertEqual(enqueue.call_args.kwargs["rows"], rows)
		self.assertIsNone(frappe.local.ki_ai_unsaved_logs)
		# The job writes the row again, with its original name and time
		frappe.db.delete("KI AI Usage Log", {"name": row["name"]})
		usage.restore(rows)
		usage.restore(rows)  # idempotent
		self.assertEqual(
			str(frappe.db.get_value("KI AI Usage Log", row["name"], "creation")), row["creation"]
		)
		self.assertEqual(self.logs(provider), ["Error"])
