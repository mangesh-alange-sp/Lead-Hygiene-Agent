"""Tests for the shared domain predicates, driven by the config allowlists."""

import unittest

from pipeline.config import KNOWN_TLDS, PERSONAL_EMAIL_DOMAINS, PLACEHOLDER_DOMAINS
from pipeline.domains import (
    company_match_key,
    domain_matches_company,
    host_of,
    is_personal_domain,
    is_placeholder_domain,
    is_plausible_domain,
)


class HostOfTests(unittest.TestCase):
    def test_strips_scheme_www_path_and_port(self):
        for value in (
            "https://www.acme.com/careers?utm=x",
            "http://acme.com",
            "www.acme.com",
            "ACME.com",
            "acme.com:443",
        ):
            self.assertEqual(host_of(value), "acme.com", value)


class PersonalDomainTests(unittest.TestCase):
    def test_every_configured_personal_domain_is_detected(self):
        for domain in PERSONAL_EMAIL_DOMAINS:
            self.assertTrue(is_personal_domain(domain), domain)
            self.assertTrue(is_personal_domain(f"https://www.{domain}/x"), domain)

    def test_country_variants_of_free_providers(self):
        for domain in ("yahoo.in", "yahoo.co.uk", "hotmail.co.uk", "outlook.de"):
            self.assertTrue(is_personal_domain(domain), domain)

    def test_corporate_domains_are_not_personal(self):
        for domain in ("acme.com", "amazon.com", "buchert.de", "asml.com"):
            self.assertFalse(is_personal_domain(domain), domain)


class PlausibleDomainTests(unittest.TestCase):
    def test_placeholders_rejected(self):
        for domain in PLACEHOLDER_DOMAINS:
            self.assertTrue(is_placeholder_domain(domain), domain)
            self.assertFalse(is_plausible_domain(domain), domain)

    def test_personal_domains_are_never_plausible_company_domains(self):
        for domain in PERSONAL_EMAIL_DOMAINS:
            self.assertFalse(is_plausible_domain(domain), domain)

    def test_initialisms_and_malformed_hosts_rejected(self):
        for value in ("r.o.c", "a.b.c", "acme", "acme.", ".com", "acme.zzz", "x.io"):
            self.assertFalse(is_plausible_domain(value), value)

    def test_real_domains_accepted(self):
        for value in ("acme.com", "buchert.de", "larsentoubro.com", "asml.com", "www.acme.co.uk"):
            self.assertTrue(is_plausible_domain(value), value)

    def test_every_known_tld_can_form_a_plausible_domain(self):
        for tld in KNOWN_TLDS:
            self.assertTrue(is_plausible_domain(f"acmecorp.{tld}"), tld)


class CompanyMatchTests(unittest.TestCase):
    def test_legal_suffixes_ignored(self):
        self.assertEqual(company_match_key("Büchert GmbH"), company_match_key("Buchert"))
        self.assertEqual(company_match_key("American Honda Motor Co"), "american honda motor")

    def test_domain_matches_company(self):
        self.assertTrue(domain_matches_company("amazon.com", "Amazon.com Inc."))
        self.assertTrue(domain_matches_company("buchert.de", "Büchert GmbH"))
        self.assertTrue(domain_matches_company("larsentoubro.com", "Larsen & Toubro"))

    def test_domain_does_not_match_unrelated_company(self):
        self.assertFalse(domain_matches_company("oracle.com", "Acme Corp"))
        self.assertFalse(domain_matches_company("microsoft.com", "Acme Corp"))
        self.assertFalse(domain_matches_company("test.com", "SailPoint Technologies"))
        self.assertFalse(domain_matches_company("globalcorp.com", "CEA"))

    def test_stem_abbreviation_matches_company(self):
        self.assertTrue(domain_matches_company("socgen.com", "Société Générale"))
        self.assertTrue(domain_matches_company("socgen.com", "Societe Generale"))
        self.assertTrue(domain_matches_company("larsentoubro.com", "L&T Construction"))
        self.assertTrue(domain_matches_company("mail.mil.tw", "R.O.C Military Academy Department of Politics"))
        self.assertFalse(is_plausible_domain("mail.mil.tw"))

    def test_leading_acronym_matches_the_host(self):
        self.assertTrue(domain_matches_company("nciinc.com", "NCI Information Systems"))
        self.assertFalse(domain_matches_company("globalcorp.com", "CEA"))

    def test_example_hosts_are_placeholders(self):
        self.assertTrue(is_placeholder_domain("example.com.tw"))
        self.assertTrue(is_placeholder_domain("mail.example.com"))
        self.assertFalse(is_plausible_domain("example.com.tw"))


if __name__ == "__main__":
    unittest.main()
