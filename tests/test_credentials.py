from __future__ import annotations

import unittest

from paper_endnote import credentials


class CredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        credentials.use_memory_backend()

    def tearDown(self) -> None:
        credentials._memory = None

    def test_saves_and_reports_username_without_returning_password(self) -> None:
        credentials.save_credentials("mcgill", "user@mcgill.ca", "secret")
        status = credentials.credential_status("mcgill")
        self.assertEqual(status["username"], "user@mcgill.ca")
        self.assertTrue(status["password_saved"])
        stored = credentials.load_credentials("mcgill")
        self.assertEqual(stored["password"], "secret")

    def test_clear_removes_entry(self) -> None:
        credentials.save_credentials("mcgill", "user@mcgill.ca", "secret")
        credentials.clear_credentials("mcgill")
        self.assertIsNone(credentials.load_credentials("mcgill"))
        self.assertFalse(credentials.credential_status("mcgill")["password_saved"])


if __name__ == "__main__":
    unittest.main()
