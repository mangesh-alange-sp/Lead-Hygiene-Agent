"""
Rule-based tests for derive_website / resolve_website.

Core rule: a free or placeholder email domain never becomes a Website unless the
Company name independently resolves to a real domain.
"""

import unittest

from pipeline.config import PERSONAL_EMAIL_DOMAINS, PLACEHOLDER_DOMAINS
from pipeline.website import derive_website, normalize_website, resolve_website

COMPANIES_WITHOUT_A_DOMAIN = ("", "Acme Corp", "SailPoint Technologies", "Bfg", "Hpworld")


class FreeProviderRules(unittest.TestCase):
    def test_free_provider_email_never_becomes_a_website(self):
        for domain in PERSONAL_EMAIL_DOMAINS:
            for company in COMPANIES_WITHOUT_A_DOMAIN:
                with self.subTest(domain=domain, company=company):
                    self.assertIsNone(derive_website(company, f"pat@{domain}"))

    def test_free_provider_survives_only_via_a_company_domain(self):
        self.assertEqual(
            derive_website("Amazon.com Inc.", "pat@gmail.com"), "https://amazon.com"
        )

    def test_existing_free_provider_website_is_dropped(self):
        for domain in ("gmail.com", "yahoo.in", "hotmail.com", "outlook.com", "icloud.com"):
            with self.subTest(domain=domain):
                self.assertIsNone(resolve_website(domain, "Infosys", "gokul.s@yahoo.in"))
                self.assertIsNone(normalize_website(f"https://www.{domain}"))

    def test_placeholder_email_domains_never_become_websites(self):
        for domain in PLACEHOLDER_DOMAINS:
            with self.subTest(domain=domain):
                self.assertIsNone(derive_website("SailPoint Technologies", f"abcdrf@{domain}"))
        self.assertIsNone(derive_website("TSMC", "kt.lin@example.com.tw"))


class CompanyFirstRules(unittest.TestCase):
    def test_domain_token_in_company_name_wins(self):
        self.assertEqual(derive_website("Amazon.Com Inc.", ""), "https://amazon.com")
        self.assertEqual(derive_website("Booking.com", "pat@gmail.com"), "https://booking.com")

    def test_company_initialism_is_not_a_domain(self):
        self.assertIsNone(derive_website("R.O.C Military Academy Department Of Politics", ""))

    def test_email_domain_used_when_it_belongs_to_the_company(self):
        self.assertEqual(derive_website("Acme", "pat@acme.com"), "https://acme.com")
        self.assertEqual(derive_website("Büchert GmbH", "h@buchert.de"), "https://buchert.de")
        self.assertEqual(derive_website("Société Générale", "stephane.leprince@socgen.com"), "https://socgen.com")

    def test_email_domain_used_when_company_is_unknown(self):
        self.assertEqual(derive_website("", "pat@acme.com"), "https://acme.com")

    def test_unrelated_email_domain_is_not_used(self):
        self.assertIsNone(derive_website("SailPoint Technologies", "abcdrf@oracle.com"))
        self.assertIsNone(derive_website("Acme Corp", "pat@microsoft.com"))


class ExistingValueRules(unittest.TestCase):
    def test_matching_existing_site_is_kept_and_standardized(self):
        for raw in ("https://www.amazon.com/jobs?x=1", "amazon.com", "HTTP://Amazon.COM"):
            with self.subTest(raw=raw):
                self.assertEqual(resolve_website(raw, "Amazon.com Inc.", ""), "https://amazon.com")

    def test_mismatched_existing_site_is_dropped(self):
        self.assertIsNone(resolve_website("oracle.com", "Acme Corp", "mcaroffino@salesforce.com"))
        self.assertIsNone(resolve_website("microsoft.com", "Acme Corp", ""))

    def test_mismatched_existing_site_is_replaced_when_the_email_matches(self):
        self.assertEqual(
            resolve_website("oracle.com", "Acme Corp", "pat@acme-corp.com"),
            "https://acme-corp.com",
        )

    def test_social_and_malformed_hosts_dropped(self):
        for raw in ("linkedin.com/company/acme", "https://facebook.com/acme", "r.o.c", "acme", "n/a"):
            with self.subTest(raw=raw):
                self.assertIsNone(normalize_website(raw))

    def test_output_is_always_https_and_hostonly(self):
        result = resolve_website("http://www.acme.com/path?utm=1", "Acme", "")
        self.assertEqual(result, "https://acme.com")

    def test_idempotent(self):
        for company, email in (("Amazon.com Inc.", ""), ("Acme", "pat@acme.com"), ("Bfg", "willy@wonka.com")):
            once = resolve_website("", company, email)
            with self.subTest(company=company):
                self.assertEqual(resolve_website(once or "", company, email), once)


if __name__ == "__main__":
    unittest.main()
