"""Vendor policy tests; pure data, no network or filesystem access."""

import copy
import unittest

import model_presets


class FamilyTests(unittest.TestCase):
    def test_named_vendors_are_recognised(self):
        cases = {
            "deepseek-v4-flash": "DeepSeek",
            "claude-opus-5": "Anthropic",
            "gemini-3.8-flash": "Google",
            "grok-4.7": "xAI",
            "MiniMax-M2": "MiniMax",
            "kimi-k2-thinking": "Moonshot",
            "glm-4.6": "Zhipu",
            "gpt-6-sol": "OpenAI",
        }
        for slug, vendor in cases.items():
            with self.subTest(slug=slug):
                self.assertEqual(model_presets.family_for(slug)["vendor"], vendor)

    def test_matching_is_case_insensitive(self):
        self.assertEqual(model_presets.family_for("DeepSeek-Flash")["vendor"], "DeepSeek")
        self.assertEqual(model_presets.family_for("GLM-4.6")["vendor"], "Zhipu")

    def test_unknown_models_are_not_claimed(self):
        self.assertIsNone(model_presets.family_for("some-private-ft-2026"))
        self.assertIsNone(model_presets.family_for(None))

    def test_all_documented_families_exist(self):
        self.assertEqual(set(model_presets.known_vendors()), {
            "OpenAI", "Anthropic", "Google", "xAI",
            "DeepSeek", "MiniMax", "Moonshot", "Zhipu"})


class PolicyTests(unittest.TestCase):
    def model(self, slug, window=128000, maximum=128000):
        return {"slug": slug, "context_window": window, "max_context_window": maximum,
                "input_modalities": [], "description": "Gateway description."}

    def test_max_policy_uses_the_advertised_window(self):
        result = model_presets.apply_policy(self.model("deepseek-v4-flash", 1000000, 1000000))
        self.assertEqual(result["context_window"], 1000000)
        self.assertEqual(result["max_context_window"], 1000000)

    def test_policy_never_raises_a_smaller_advertised_window(self):
        result = model_presets.apply_policy(self.model("deepseek-v4-flash", 65536, 65536))
        self.assertEqual(result["context_window"], 65536)

    def test_standard_policy_caps_a_large_window(self):
        overrides = {"deepseek-v4-flash": {"context": model_presets.STANDARD,
                                           "standard_cap": 200000}}
        result = model_presets.apply_policy(self.model("deepseek-v4-flash", 1000000, 1000000),
                                            overrides)
        self.assertEqual(result["context_window"], 200000)

    def test_every_family_advertises_the_collaboration_surface(self):
        """Without this field Codex never injects spawn_agent/wait_agent and friends.

        The README promises Sub Agent keeps working with a third-party model; that promise
        is only true when the catalog entry carries `multi_agent_version`.
        """
        for vendor in model_presets.known_vendors():
            with self.subTest(vendor=vendor):
                family = next(f for f in model_presets.FAMILIES if f["vendor"] == vendor)
                self.assertEqual(family["multi_agent"], model_presets.MULTI_AGENT_V2)

    def test_policy_stamps_the_multi_agent_field(self):
        result = model_presets.apply_policy(self.model("claude-opus-5"))
        self.assertEqual(result["multi_agent_version"], "v2")

    def test_a_family_can_opt_out(self):
        family = dict(model_presets.FAMILIES[0], multi_agent=None)
        original = model_presets.FAMILIES
        model_presets.FAMILIES = (family,)
        try:
            source = self.model("gpt-6-sol")
            result = model_presets.apply_policy(source)
            self.assertNotIn("multi_agent_version", result)
            untouched = self.model("gpt-6-sol")
            untouched["multi_agent_version"] = "v1"
            self.assertEqual(model_presets.apply_policy(untouched)["multi_agent_version"], "v1")
        finally:
            model_presets.FAMILIES = original

    def test_an_opting_out_override_wins(self):
        overrides = {"claude-opus-5": {"multi_agent": None}}
        result = model_presets.apply_policy(self.model("claude-opus-5"), overrides)
        self.assertNotIn("multi_agent_version", result)

    def test_unknown_models_are_never_stamped(self):
        result = model_presets.apply_policy(self.model("some-private-ft-2026"))
        self.assertNotIn("multi_agent_version", result)

    def test_describe_reports_the_multi_agent_policy(self):
        table = {row["vendor"]: row for row in model_presets.describe()}
        self.assertEqual(table["DeepSeek"]["multi_agent"], "v2")

    def test_compact_budget_is_lowered_with_the_window(self):
        overrides = {"deepseek-v4-flash": {"context": model_presets.STANDARD,
                                           "standard_cap": 100000}}
        model = self.model("deepseek-v4-flash", 1000000, 1000000)
        model["auto_compact_token_limit"] = 850000
        result = model_presets.apply_policy(model, overrides)
        self.assertEqual(result["auto_compact_token_limit"], 85000)

    def test_gateway_policy_changes_nothing(self):
        overrides = {"deepseek-v4-flash": {"context": model_presets.GATEWAY}}
        model = self.model("deepseek-v4-flash", 12345, 67890)
        self.assertEqual(model_presets.apply_policy(model, overrides)["max_context_window"], 67890)

    def test_unknown_model_passes_through_untouched(self):
        model = self.model("some-private-ft-2026")
        self.assertEqual(model_presets.apply_policy(model), model)

    def test_existing_modalities_are_never_narrowed(self):
        model = self.model("gemini-3.8-flash")
        model["input_modalities"] = ["text", "image", "audio"]
        result = model_presets.apply_policy(model)
        self.assertEqual(result["input_modalities"], ["text", "image", "audio"])

    def test_modalities_are_filled_when_the_gateway_omits_them(self):
        result = model_presets.apply_policy(self.model("claude-opus-5"))
        self.assertEqual(result["input_modalities"], ["text", "image"])

    def test_input_is_never_mutated(self):
        model = self.model("deepseek-v4-flash", 1000000, 1000000)
        snapshot = copy.deepcopy(model)
        model_presets.apply_policy(model)
        self.assertEqual(model, snapshot)


class CatalogTests(unittest.TestCase):
    def catalog(self):
        return {"models": [
            {"slug": "deepseek-v4-flash", "context_window": 1000000,
             "max_context_window": 1000000, "input_modalities": ["text"]},
            {"slug": "gemini-3.8-flash", "context_window": 1000000,
             "max_context_window": 1000000, "input_modalities": ["text"]},
            {"slug": "kimi-k2", "context_window": 262144,
             "max_context_window": 262144, "input_modalities": ["text"]},
            {"slug": "some-private-ft-2026", "context_window": 4096,
             "max_context_window": 4096, "input_modalities": ["text"]},
        ]}

    def test_policy_is_applied_to_every_model(self):
        merged = model_presets.apply_policy_to_catalog(self.catalog())
        self.assertEqual(len(merged["models"]), 4)
        self.assertEqual(merged["models"][0]["context_window"], 1000000)

    def test_catalog_is_never_extended_or_reordered(self):
        original = self.catalog()
        merged = model_presets.apply_policy_to_catalog(original)
        self.assertEqual([m["slug"] for m in merged["models"]],
                         [m["slug"] for m in original["models"]])

    def test_media_models_come_from_policy_not_from_a_hardcoded_list(self):
        self.assertEqual(model_presets.media_models(self.catalog()), ["gemini-3.8-flash"])

    def test_media_models_can_be_overridden_per_slug(self):
        overrides = {"kimi-k2": {"media": model_presets.RELOCATE}}
        self.assertEqual(sorted(model_presets.media_models(self.catalog(), overrides)),
                         ["gemini-3.8-flash", "kimi-k2"])

    def test_malformed_catalog_is_refused(self):
        with self.assertRaises(ValueError):
            model_presets.apply_policy_to_catalog({"models": "not-a-list"})

    def test_describe_covers_every_family(self):
        rows = model_presets.describe()
        self.assertEqual(len(rows), len(model_presets.FAMILIES))
        self.assertTrue(all(row["match"] for row in rows))


if __name__ == "__main__":
    unittest.main(verbosity=2)
