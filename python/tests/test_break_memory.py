"""Break memory and capability rules (A28)."""

import unittest

from agentrealm_agent.break_memory import (
    attempt_failed,
    break_key,
    capabilities_for_code,
    record_attempt,
)
from agentrealm_agent.knowledge_base import KnowledgeBase


class BreakMemoryTest(unittest.TestCase):
    def test_break_key_shape(self):
        self.assertEqual(break_key(12, (10, 20), "cut"), "12,10,20,cut")

    def test_capabilities_from_manual_classes(self):
        self.assertEqual(capabilities_for_code("bronze_sword"), frozenset({"cut", "chop"}))
        self.assertEqual(capabilities_for_code("pocket_knife"), frozenset({"cut", "chop"}))
        self.assertEqual(capabilities_for_code("bronze_mallet"), frozenset({"smash"}))

    def test_failed_pair_is_remembered(self):
        kb = KnowledgeBase.empty("sandbox")
        record_attempt(kb, map_id=1, pos=(3, 4), capability="cut", result="applied_no_effect")
        self.assertTrue(attempt_failed(kb, 1, (3, 4), "cut"))
        self.assertFalse(attempt_failed(kb, 1, (3, 4), "burn"))
