from __future__ import annotations

import unittest

from policy import AccessPolicy, CooldownManager, normalize_whitelist


class AccessPolicyTests(unittest.TestCase):
    def test_empty_whitelist_allows_everything(self) -> None:
        self.assertTrue(AccessPolicy([]).is_allowed(group_id="100", sender_id="200"))

    def test_group_takes_precedence_over_sender(self) -> None:
        policy = AccessPolicy(["100", "300"])
        self.assertTrue(policy.is_allowed(group_id="100", sender_id="200"))
        self.assertFalse(policy.is_allowed(group_id="999", sender_id="300"))
        self.assertTrue(policy.is_allowed(group_id="", sender_id="300"))

    def test_normalize_whitelist(self) -> None:
        self.assertEqual(normalize_whitelist([" 1 ", 2, "", None]), {"1", "2"})


class CooldownTests(unittest.IsolatedAsyncioTestCase):
    async def test_claim_and_release(self) -> None:
        now = [100.0]
        cooldown = CooldownManager(30, clock=lambda: now[0])
        self.assertEqual(await cooldown.claim("user"), 0)
        self.assertEqual(await cooldown.claim("user"), 30)
        now[0] = 129.2
        self.assertEqual(await cooldown.claim("user"), 1)
        now[0] = 130.0
        self.assertEqual(await cooldown.claim("user"), 0)
        await cooldown.release("user")
        self.assertEqual(await cooldown.claim("user"), 0)


if __name__ == "__main__":
    unittest.main()
