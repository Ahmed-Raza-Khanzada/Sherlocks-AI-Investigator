"""Generate the 'Paid OSINT APIs required' PDF procurement note.

Self-contained (fpdf2 only). Run:  .venv/bin/python scripts/make_osint_paid_pdf.py
Output: output/OSINT_Paid_APIs.pdf
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fpdf import FPDF

NAVY = (13, 27, 62)
BLUE = (37, 99, 235)
GREY = (90, 100, 120)
LIGHT = (238, 242, 249)
RED = (200, 40, 60)

# name, env key, why it is needed, what it unlocks, pricing, priority
PAID = [
    ("Bright Data - SERP API",
     "BRIGHTDATA_API_KEY + BRIGHTDATA_SERP_ZONE",
     "Free search engines (DuckDuckGo/Bing) throttle and block automated queries after a "
     "few dozen requests. This gives reliable, un-blocked Google/Bing results for a person's "
     "name + city and dork queries - the only way to consistently surface a Pakistani "
     "citizen's public profiles and mentions at volume.",
     "search_footprint, search_dorks_live",
     "Pay-per-1,000 requests, ~USD 1.5-3 / 1k queries",
     "HIGH"),
    ("Bright Data - Web Unlocker",
     "BRIGHTDATA_API_KEY + BRIGHTDATA_UNLOCKER_ZONE",
     "Facebook / Instagram / X block bots, so a candidate profile URL cannot be read to "
     "confirm it. Web Unlocker fetches the page past those bot-walls so the bio, city and "
     "employer can be checked against what we know - this is the 'confirm the profile is "
     "actually the subject' step, not just 'a handle exists'.",
     "scrape_url",
     "Pay-per-1,000 requests, ~USD 2-5 / 1k pages",
     "HIGH"),
    ("Apify",
     "APIFY_TOKEN (+ chosen actor)",
     "Hosted, maintained scrapers ('actors') that pull structured profile data (name, bio, "
     "followers, location, posts) from Instagram, Facebook, TikTok and X given a username "
     "or profile URL. An alternative/complement to Web Unlocker when you want ready-parsed "
     "social data instead of raw HTML.",
     "search_apify",
     "Pay-as-you-go, ~USD 0.5-5 / 1k results (actor-dependent)",
     "MEDIUM"),
    ("SocialCrawl",
     "SOCIALCRAWL_API_KEY",
     "One hosted API over 65 social platforms (TikTok, Instagram, YouTube, Facebook, X, "
     "LinkedIn, Reddit) that returns ready-parsed profile and post data by username or URL. "
     "Broader platform coverage than a single Apify actor and one unified schema - the fastest "
     "way to pull a subject's social footprint once a candidate handle is known.",
     "search_socialcrawl",
     "Pay-per-call credits; from GBP 15 / 2,500 credits (~GBP 0.006 each), cheaper at volume",
     "MEDIUM"),
    ("Have I Been Pwned (HIBP)",
     "HIBP_API_KEY",
     "Given an email, lists the data breaches it appears in - which reveals other accounts, "
     "linked emails and services the person uses. Useful for pivoting from one email to a "
     "wider online identity.",
     "search_breach",
     "Subscription, ~USD 3.95 / month per key",
     "MEDIUM"),
    ("IPinfo",
     "IPINFO_TOKEN",
     "Geolocates and identifies the network/ASN of an IP address that turns up in a "
     "footprint (e.g. from a leaked post). Enrichment only; lowest priority. (Not yet wired "
     "into the pipeline - buy last.)",
     "search_ip / enrichment",
     "Free up to 50k/mo; paid from ~USD 249/mo",
     "LOW"),
    ("Pipl",
     "PIPL_API_KEY",
     "Identity-resolution API: give an email, phone or username and it returns one merged "
     "person profile with linked accounts and aliases - the strongest pivot engine. Optional: "
     "its data is US/EU-centric and thin for Pakistani subjects, so buy only if a case needs it.",
     "search_pipl (enrichment)",
     "Custom; minimum ~USD 500 / month (via sales)",
     "LOW"),
    ("FullContact",
     "FULLCONTACT_API_KEY",
     "Enrichment API: email / phone / social handle -> unified profile (name, location, job, "
     "bio, social links). Optional; US/EU-centric coverage, weak for PK subjects.",
     "search_fullcontact (enrichment)",
     "Free 100 enrichments; Pro from ~USD 99 / month",
     "LOW"),
    ("Hunter.io",
     "HUNTER_API_KEY",
     "Email finder / verifier: full name + company or domain -> work email and confidence. "
     "Useful for professional subjects; limited for consumer/PK subjects. Optional.",
     "search_hunter (enrichment)",
     "Free 50/mo; Starter ~USD 34/mo (2,000 credits, annual)",
     "LOW"),
    ("Enformion / Endato",
     "ENFORMION_API_KEY + ENFORMION_API_PROFILE",
     "US people & public-records API: name/phone/email/address -> address history, phones, "
     "emails, relatives. Optional and US-only data - not useful for Pakistani subjects.",
     "search_enformion (enrichment)",
     "Free 100/mo; then ~USD 0.25 / match (as low as 0.01 at volume)",
     "LOW"),
]

def _s(t: str) -> str:
    """Core PDF fonts are latin-1 only; drop anything outside it (em-dash, arrows, curly quotes)."""
    return (str(t).replace("\u2014", "-").replace("\u2013", "-").replace("\u2192", "->")
            .replace("\u2019", "'").replace("\u2018", "'").replace("\u201c", '"').replace("\u201d", '"')
            .encode("latin-1", "replace").decode("latin-1"))


class PDF(FPDF):
    def header(self) -> None:
        if self.page_no() == 1:
            return
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*GREY)
        self.cell(0, 8, "Sherlocks - Paid OSINT APIs required", align="L")
        self.cell(0, 8, "CONFIDENTIAL", align="R", new_x="LMARGIN", new_y="NEXT")
        self.ln(2)

    def footer(self) -> None:
        self.set_y(-12)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*GREY)
        self.cell(0, 8, f"Page {self.page_no()}", align="C")


def h(pdf: PDF, text: str, size: int = 13, color=NAVY, top: int = 3) -> None:
    pdf.ln(top)
    pdf.set_font("Helvetica", "B", size)
    pdf.set_text_color(*color)
    pdf.multi_cell(0, 6, _s(text), new_x="LMARGIN", new_y="NEXT")


def body(pdf: PDF, text: str, size: int = 10) -> None:
    pdf.set_font("Helvetica", "", size)
    pdf.set_text_color(30, 34, 44)
    pdf.multi_cell(0, 5, _s(text), new_x="LMARGIN", new_y="NEXT")


def chip(pdf: PDF, label: str, color) -> None:
    pdf.set_font("Helvetica", "B", 8)
    w = pdf.get_string_width(label) + 6
    pdf.set_fill_color(*color)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(w, 5, label, align="C", fill=True, new_x="LMARGIN", new_y="NEXT")


def build() -> Path:
    pdf = PDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=16)
    pdf.set_margins(16, 16, 16)
    pdf.add_page()

    # Title block
    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, 210, 34, style="F")
    pdf.set_xy(16, 9)
    pdf.set_font("Helvetica", "B", 19)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(0, 9, "Paid OSINT APIs Required", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(16)
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(200, 210, 230)
    pdf.cell(0, 6, "Sherlocks - person link graph  |  procurement note")
    pdf.set_xy(16, 38)

    body(pdf, f"Date: {date.today().isoformat()}          Prepared for: procurement / approval")
    pdf.ln(1)
    body(pdf,
         "Sherlocks already finds and confirms online profiles for free using local tools "
         "(sherlock, holehe, Social-Analyzer, GitHub, paste search), a free name search over "
         "DuckDuckGo/Bing, and the in-house Qwen 27B model to confirm which profile is the "
         "subject. Those free tools surface CANDIDATE profiles but throttle quickly and cannot "
         "read a locked social page to confirm it. The paid APIs below remove those two limits: "
         "reliable search at volume, and reading a profile to confirm identity. Every key sits "
         "empty in .env until purchased - a tool with no key is skipped, never faked.")

    h(pdf, "Recommended purchase order")
    body(pdf,
         "1) Bright Data SERP API  +  2) Bright Data Web Unlocker   (buy together - SERP finds "
         "the profiles, Unlocker confirms them; one is little use without the other).\n"
         "3) SocialCrawl  +  4) Apify   (ready-parsed social data across many platforms).\n"
         "5) Have I Been Pwned   (email -> breaches -> linked accounts).\n"
         "6) IPinfo   (IP enrichment; buy last, not yet wired).\n"
         "Optional (LOW): Pipl, FullContact, Hunter.io, Enformion - Western enrichment/records; "
         "data is US/EU-centric and thin for Pakistani subjects, so buy only for a case that needs it.")

    h(pdf, "The paid APIs, and why each is needed")
    for name, key, why, powers, price, prio in PAID:
        pdf.ln(2)
        # header line with priority chip
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(*BLUE)
        pdf.cell(150, 6, _s(name))
        pcol = RED if prio == "HIGH" else (BLUE if prio == "MEDIUM" else GREY)
        chip(pdf, prio, pcol)
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(0, 5, _s(f".env key:  {key}"), new_x="LMARGIN", new_y="NEXT")
        body(pdf, f"Why: {why}", size=10)
        pdf.set_font("Helvetica", "I", 9)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(0, 5, _s(f"Enables tool: {powers}    |    Pricing: {price}"), new_x="LMARGIN", new_y="NEXT")
        pdf.set_draw_color(*LIGHT)
        pdf.line(16, pdf.get_y() + 1, 194, pdf.get_y() + 1)

    # Quotation table
    h(pdf, "Quotation (fill in at purchase)")
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    cols = [(58, "Service"), (40, "Plan"), (30, "Monthly (USD)"), (50, "Notes")]
    for w, t in cols:
        pdf.cell(w, 7, t, border=0, fill=True, align="L")
    pdf.ln(7)
    rows = [
        ("Bright Data SERP API", "Pay-as-you-go", "____", "by expected queries/mo"),
        ("Bright Data Web Unlocker", "Pay-as-you-go", "____", "by expected page fetches/mo"),
        ("Apify", "Pay-as-you-go", "____", "by results/mo + actor rental"),
        ("SocialCrawl", "Credits", "____", "GBP; from 15 / 2,500 credits"),
        ("Have I Been Pwned", "API key", "3.95", "per key"),
        ("IPinfo", "(tier)", "____", "free tier may suffice"),
        ("Pipl (optional)", "API", "____", "US/EU data; min ~500/mo"),
        ("FullContact (optional)", "Pro", "____", "US/EU data; from ~99/mo"),
        ("Hunter.io (optional)", "Starter", "____", "pro emails; from ~34/mo"),
        ("Enformion (optional)", "pay/match", "____", "US-only records"),
        ("TOTAL", "", "____", ""),
    ]
    pdf.set_font("Helvetica", "", 9)
    for i, (a, b, c, d) in enumerate(rows):
        fill = i % 2 == 0
        pdf.set_fill_color(*LIGHT)
        bold = a == "TOTAL"
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        pdf.set_text_color(*(NAVY if bold else (30, 34, 44)))
        for (w, _), val in zip(cols, (a, b, c, d), strict=True):
            pdf.cell(w, 7, _s(val), border=0, fill=fill, align="L")
        pdf.ln(7)

    pdf.ln(3)
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(0, 4,
                   "Costs are vendor list estimates and vary with volume; confirm at purchase. "
                   "Bright Data and Apify are metered (cost = usage). One thorough person search "
                   "is roughly 5-15 SERP queries plus 2-8 profile fetches.")

    out = Path("output/OSINT_Paid_APIs.pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out))
    return out


if __name__ == "__main__":
    path = build()
    print(f"wrote {path.resolve()}  ({path.stat().st_size} bytes)")
