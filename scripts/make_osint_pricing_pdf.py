"""Generate 'Paid OSINT APIs - pricing plans' PDF: every vendor, every plan, price.

Prices are vendor list prices at time of writing and CHANGE - verify at purchase.
Run:  .venv/bin/python scripts/make_osint_pricing_pdf.py   ->  output/OSINT_Paid_APIs_Pricing.pdf
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fpdf import FPDF

NAVY = (13, 27, 62)
BLUE = (37, 99, 235)
GREY = (90, 100, 120)
LIGHT = (238, 242, 249)
WHITE = (255, 255, 255)

# Each vendor: (title, tool/purpose, env key, priority, columns, rows)
# columns: list of (width_mm, header)
# rows: list of tuples matching columns
VENDORS = [
    {
        "title": "Bright Data - SERP API",
        "purpose": "Un-blocked Google/Bing results for name+city and dork queries (find profiles at volume).",
        "key": "BRIGHTDATA_API_KEY + BRIGHTDATA_SERP_ZONE",
        "priority": "HIGH",
        "cols": [(45, "Plan"), (70, "What you get"), (35, "Monthly"), (40, "Effective / 1k")],
        "rows": [
            ("Pay-as-you-go", "No commitment, billed per request", "$0", "~$1.50 / 1k"),
            ("Growth", "Monthly commitment, volume discount", "$499", "~$1.27 / 1k"),
            ("Business", "Higher volume, priority support", "$999", "~$1.05 / 1k"),
            ("Premium / Enterprise", "Custom volume, SLA, invoicing", "Custom", "lowest"),
        ],
    },
    {
        "title": "Bright Data - Web Unlocker",
        "purpose": "Fetch a locked FB/Insta/X profile page past bot-walls to CONFIRM it is the subject.",
        "key": "BRIGHTDATA_API_KEY + BRIGHTDATA_UNLOCKER_ZONE",
        "priority": "HIGH",
        "cols": [(45, "Plan"), (70, "What you get"), (35, "Monthly"), (40, "Effective / 1k")],
        "rows": [
            ("Pay-as-you-go", "No commitment, billed per page fetch", "$0", "~$1.50 / 1k"),
            ("Growth", "Monthly commitment, volume discount", "$499", "~$1.27 / 1k"),
            ("Business", "Higher volume, priority support", "$999", "~$1.05 / 1k"),
            ("Enterprise", "Custom volume, SLA, invoicing", "Custom", "lowest"),
        ],
    },
    {
        "title": "Apify",
        "purpose": "Hosted scraper 'actors' returning ready-parsed profile data (Instagram/FB/TikTok/X).",
        "key": "APIFY_TOKEN (+ chosen actor)",
        "priority": "MEDIUM",
        "cols": [(45, "Plan"), (70, "What you get"), (35, "Monthly"), (40, "Platform credit")],
        "rows": [
            ("Free", "Trial; limited compute; some actors only", "$0", "$5 credit"),
            ("Starter", "Small volume, pay-as-you-go on top", "$49", "$49 usage"),
            ("Scale", "Team volume", "$499", "$499 usage"),
            ("Business", "High volume, priority", "$999", "$999 usage"),
            ("Enterprise", "Custom, SLA", "Custom", "custom"),
        ],
        "note": "Most social actors also bill per result (~$0.5-5 / 1k) or a monthly actor rental on top of the plan.",
    },
    {
        "title": "SocialCrawl",
        "purpose": "One API over 65 social platforms (TikTok/Insta/YouTube/FB/X/LinkedIn/Reddit) - ready-parsed profile + post data by username or URL.",
        "key": "SOCIALCRAWL_API_KEY",
        "priority": "MEDIUM",
        "cols": [(45, "Plan"), (70, "What you get"), (35, "Price"), (40, "Per credit")],
        "rows": [
            ("Free", "100 credits one-time; all 607 APIs / 65 platforms", "GBP 0", "-"),
            ("Starter", "2,500 credits; universal search, email support", "GBP 15", "GBP 0.0060"),
            ("Growth", "20,000 credits; MCP server, priority support", "GBP 49", "GBP 0.00245"),
            ("Pro", "150,000 credits; MCP server, priority support", "GBP 299", "GBP 0.00199"),
            ("Enterprise", "Custom volume, SLA, dedicated support", "Custom", "lowest"),
        ],
        "note": "Pay per call, not per month; credits never expire; empty/failed calls are refunded. 1 standard call = 1 credit.",
    },
    {
        "title": "Have I Been Pwned (HIBP)",
        "purpose": "Email -> data breaches it appears in -> other accounts and linked emails (pivoting).",
        "key": "HIBP_API_KEY",
        "priority": "MEDIUM",
        "cols": [(45, "Plan"), (85, "Rate limit (email lookups)"), (60, "Monthly (annual billing)")],
        "rows": [
            ("Pwned 1", "10 requests / minute", "$3.95"),
            ("Pwned 2", "50 requests / minute", "$16.50"),
            ("Pwned 3", "100 requests / minute", "$27.50"),
            ("Pwned 4", "500 requests / minute", "$137.50"),
            ("Pwned 5", "1000 requests / minute", "$274.00"),
        ],
    },
    {
        "title": "IPinfo   (lowest priority - buy last, not yet wired)",
        "purpose": "Geolocate / ASN an IP that surfaces in a footprint. Enrichment only.",
        "key": "IPINFO_TOKEN",
        "priority": "LOW",
        "cols": [(45, "Plan"), (70, "Included lookups / data"), (35, "Monthly"), (40, "Notes")],
        "rows": [
            ("Free", "50,000 requests / month", "$0", "may suffice"),
            ("Basic", "~250k requests / month", "$99", "geolocation"),
            ("Standard", "Higher volume + more data", "$249", "+ ASN/privacy"),
            ("Business", "High volume", "$499", "+ ranges"),
            ("Enterprise", "Custom", "Custom", "full dataset"),
        ],
    },
    {
        "title": "Pipl   (identity resolution - optional)",
        "purpose": "Email / phone / username -> one merged person profile with linked accounts. Strongest pivot engine; data coverage is US/EU-centric (thin for PK).",
        "key": "PIPL_API_KEY",
        "priority": "LOW",
        "cols": [(45, "Plan"), (85, "What you get"), (95, "Price")],
        "rows": [
            ("Business / API", "Identity resolution across selectors", "Custom - minimum ~$500 / month (via sales)"),
            ("Enterprise", "Higher volume, SLA", "Custom"),
        ],
    },
    {
        "title": "FullContact   (enrichment - optional)",
        "purpose": "Email/phone/social handle -> unified profile: name, location, job, bio, social links. US/EU-centric coverage.",
        "key": "FULLCONTACT_API_KEY",
        "priority": "LOW",
        "cols": [(45, "Plan"), (85, "What you get"), (35, "Monthly"), (30, "Notes")],
        "rows": [
            ("Free", "100 enrichments", "$0", "trial"),
            ("Pro", "Higher volume, more fields", "$99", "from"),
            ("Enterprise", "Custom volume, SLA", "Custom", "sales"),
        ],
    },
    {
        "title": "Hunter.io   (email finder - optional)",
        "purpose": "Name + company/domain -> work email + verification. Good for professional subjects; consumer/PK coverage limited.",
        "key": "HUNTER_API_KEY",
        "priority": "LOW",
        "cols": [(45, "Plan"), (70, "Credits / month"), (35, "Monthly"), (40, "Annual /mo")],
        "rows": [
            ("Free", "50 credits", "$0", "-"),
            ("Starter", "2,000 credits", "$49", "$34"),
            ("Growth", "10,000 credits", "$149", "$104"),
            ("Scale", "30,000 credits", "$299", "$209"),
            ("Enterprise", "Custom", "Custom", "custom"),
        ],
    },
    {
        "title": "Enformion / Endato (EnformionGO)   (people records - optional)",
        "purpose": "Name/phone/email/address -> people + public-records report (address, phone, email, relatives). US public-records data - not useful for PK subjects.",
        "key": "ENFORMION_API_KEY + ENFORMION_API_PROFILE",
        "priority": "LOW",
        "cols": [(45, "Plan"), (70, "Included / rate"), (35, "Price"), (40, "Per match")],
        "rows": [
            ("Free", "100 searches / 100 matches / mo", "$0", "-"),
            ("Starter", "up to 5,000 searches / mo", "pay-per-match", "$0.25"),
            ("Pro", "unlimited searches (custom)", "custom", "as low as $0.01"),
        ],
    },
]


def _s(t: str) -> str:
    return (str(t).replace("—", "-").replace("–", "-").replace("→", "->")
            .replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
            .encode("latin-1", "replace").decode("latin-1"))


class PDF(FPDF):
    def footer(self) -> None:
        self.set_y(-11)
        self.set_font("Helvetica", "", 8)
        self.set_text_color(*GREY)
        self.cell(0, 8, f"Sherlocks - OSINT paid API pricing plans   |   Page {self.page_no()}", align="C")


def priority_chip(pdf: PDF, prio: str) -> None:
    col = {"HIGH": (200, 40, 60), "MEDIUM": BLUE, "LOW": GREY}[prio]
    pdf.set_font("Helvetica", "B", 8)
    w = pdf.get_string_width(prio) + 6
    pdf.set_fill_color(*col)
    pdf.set_text_color(*WHITE)
    pdf.cell(w, 5, prio, align="C", fill=True, new_x="LMARGIN", new_y="NEXT")


def vendor_block(pdf: PDF, v: dict) -> None:
    cols = v["cols"]
    table_w = sum(w for w, _ in cols)
    # keep a vendor block on one page
    if pdf.get_y() > 165:
        pdf.add_page()
    pdf.ln(3)
    pdf.set_font("Helvetica", "B", 12)
    pdf.set_text_color(*BLUE)
    pdf.cell(table_w - 25, 6, _s(v["title"]))
    priority_chip(pdf, v["priority"])
    pdf.set_font("Helvetica", "", 8.5)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(table_w, 4.5, _s(f"{v['purpose']}   |   .env: {v['key']}"), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(1)
    # header row
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(*WHITE)
    for w, hdr in cols:
        pdf.cell(w, 7, _s(hdr), border=0, fill=True, align="L")
    pdf.ln(7)
    # data rows
    pdf.set_font("Helvetica", "", 9)
    for i, row in enumerate(v["rows"]):
        pdf.set_fill_color(*(LIGHT if i % 2 == 0 else WHITE))
        pdf.set_text_color(30, 34, 44)
        for (w, _), val in zip(cols, row, strict=True):
            pdf.cell(w, 6.5, _s(val), border=0, fill=True, align="L")
        pdf.ln(6.5)
    if v.get("note"):
        pdf.set_font("Helvetica", "I", 8)
        pdf.set_text_color(*GREY)
        pdf.multi_cell(table_w, 4.2, _s("Note: " + v["note"]), new_x="LMARGIN", new_y="NEXT")


def build() -> Path:
    pdf = PDF(orientation="L", unit="mm", format="A4")   # landscape for wide tables
    pdf.set_auto_page_break(auto=True, margin=14)
    pdf.set_margins(14, 12, 14)
    pdf.add_page()

    pdf.set_fill_color(*NAVY)
    pdf.rect(0, 0, 297, 26, style="F")
    pdf.set_xy(14, 6)
    pdf.set_font("Helvetica", "B", 17)
    pdf.set_text_color(*WHITE)
    pdf.cell(0, 8, "Paid OSINT APIs - Pricing Plans", new_x="LMARGIN", new_y="NEXT")
    pdf.set_x(14)
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(200, 210, 230)
    pdf.cell(0, 5, f"Sherlocks - person link graph   |   {date.today().isoformat()}   |   every plan + price per vendor")
    pdf.set_xy(14, 30)

    pdf.set_font("Helvetica", "I", 8.5)
    pdf.set_text_color(*GREY)
    pdf.multi_cell(0, 4.5, _s(
        "List prices at time of writing; vendors change pricing often - confirm on the vendor site at purchase. "
        "Bright Data and Apify are metered (cost = usage). Buy priority: Bright Data SERP + Web Unlocker first "
        "(they work as a pair), then SocialCrawl / Apify for social data, then HIBP, then IPinfo. "
        "The LOW-priority block at the end (Pipl, FullContact, Hunter, Enformion) is optional enrichment - "
        "their data is US/EU-centric and thin for Pakistani subjects, so buy only if a case needs Western records."),
        new_x="LMARGIN", new_y="NEXT")

    for v in VENDORS:
        vendor_block(pdf, v)

    out = Path("output/OSINT_Paid_APIs_Pricing.pdf")
    out.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(out))
    return out


if __name__ == "__main__":
    path = build()
    print(f"wrote {path.resolve()}  ({path.stat().st_size} bytes)")
