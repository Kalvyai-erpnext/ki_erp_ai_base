# Copyright (c) 2026, kalvyai and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class KIAIUsageLog(Document):
	"""One AI call: who made it, for what, and how many tokens it used. Written by
	``KIAIProvider.generate`` only; never edited."""

	def before_insert(self):
		self.tokens_total = (self.tokens_in or 0) + (self.tokens_out or 0)
