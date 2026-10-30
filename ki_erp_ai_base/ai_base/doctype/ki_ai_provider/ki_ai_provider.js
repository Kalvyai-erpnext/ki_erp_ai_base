// Copyright (c) 2026, kalvyai and contributors
// For license information, please see license.txt

const KI_AI_PROVIDER = "ki_erp_ai_base.ai_base.doctype.ki_ai_provider.ki_ai_provider";

frappe.ui.form.on("KI AI Provider", {
	setup(frm) {
		// Models listed by Fetch Models for the chosen type; any other model can be typed in
		frm.set_query("model_name", () => ({
			query: `${KI_AI_PROVIDER}.model_options`,
			params: { provider_type: frm.doc.provider_type },
		}));
	},

	onload(frm) {
		if (frm.is_new() && !frm.doc.model_name) {
			frm.trigger("provider_type");
		}
	},

	refresh(frm) {
		frm.fields_dict.model_name.df.ignore_validation = 1;
		if (frm.is_new() || !frm.has_perm("write")) {
			return;
		}
		frm.add_custom_button(__("Test Connection"), () => {
			frm.call({ method: "test_connection", doc: frm.doc, freeze: true }).then(({ message }) => {
				if (message.ok) {
					frappe.show_alert({ message: message.message, indicator: "green" });
				} else {
					frappe.msgprint({
						title: message.busy ? __("Provider Busy") : __("Connection Failed"),
						message: message.message,
						indicator: message.busy ? "orange" : "red",
					});
				}
				frm.reload_doc();
			});
		});
		frm.add_custom_button(__("Fetch Models"), () => {
			frm.call({ method: "fetch_models", doc: frm.doc, freeze: true }).then(({ message }) => {
				frappe.show_alert({ message, indicator: "green" });
			});
		});
		frm.add_custom_button(__("View Usage"), () => {
			frappe.set_route("List", "KI AI Usage Log", { provider: frm.doc.name });
		});
	},

	provider_type(frm) {
		frappe
			.xcall(`${KI_AI_PROVIDER}.get_kind_defaults`, { provider_type: frm.doc.provider_type })
			.then((values) => {
				frm.set_value({ ...values, supports_vision: 1 });
				if (frm.doc.provider_type === "Cohere") {
					// Command models cap the answer at 8k and take no reasoning effort
					frm.set_value({ reasoning_effort: "", max_output_tokens: 8000 });
				}
			});
	},
});
