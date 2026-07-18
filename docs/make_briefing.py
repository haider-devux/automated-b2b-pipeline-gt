"""Generate briefing.pdf — a short, non-technical project brief. Run with the venv Python."""
import os
from fpdf import FPDF

INK = (22, 30, 48); PURPLE = (108, 76, 240); GREEN = (34, 150, 90)
GREY = (120, 130, 150); BOX = (245, 246, 251); LINE = (223, 227, 236); WHITE = (255, 255, 255)
W, M = 210, 15


def steps(pdf, y):
    labels = [("1", "DISCOVER", "Find companies"), ("2", "ENRICH", "Research them"),
              ("3", "QUALIFY", "AI judges fit + writes pitch"), ("4", "OUTREACH", "Auto-send (optional human check)")]
    gap = 6
    bw = (W - 2 * M - 3 * gap) / 4
    bh = 26
    for i, (n, t, s) in enumerate(labels):
        x = M + i * (bw + gap)
        pdf.set_fill_color(*BOX); pdf.set_draw_color(*LINE)
        pdf.rect(x, y, bw, bh, "DF")
        pdf.set_fill_color(*PURPLE); pdf.rect(x, y, bw, 2, "F")
        pdf.set_xy(x, y + 5); pdf.set_text_color(*PURPLE); pdf.set_font("Helvetica", "B", 9)
        pdf.cell(bw, 5, f"STEP {n}", align="C")
        pdf.set_xy(x, y + 11); pdf.set_text_color(*INK); pdf.set_font("Helvetica", "B", 11)
        pdf.cell(bw, 5, t, align="C")
        pdf.set_xy(x, y + 17); pdf.set_text_color(*GREY); pdf.set_font("Helvetica", "", 7.5)
        pdf.multi_cell(bw, 3.5, s, align="C")
        if i < 3:
            pdf.set_xy(x + bw, y + bh / 2 - 3); pdf.set_text_color(*PURPLE); pdf.set_font("Helvetica", "B", 11)
            pdf.cell(gap, 6, ">", align="C")


def highlight(pdf, x, y, w, title, sub):
    pdf.set_fill_color(*BOX); pdf.set_draw_color(*LINE); pdf.rect(x, y, w, 20, "DF")
    pdf.set_xy(x + 3, y + 4); pdf.set_text_color(*PURPLE); pdf.set_font("Helvetica", "B", 10)
    pdf.cell(w - 6, 5, title)
    pdf.set_xy(x + 3, y + 10.5); pdf.set_text_color(*GREY); pdf.set_font("Helvetica", "", 8)
    pdf.multi_cell(w - 6, 3.6, sub)


def card(pdf, y, num, title, desc, tools):
    h = 30
    pdf.set_fill_color(255, 255, 255); pdf.set_draw_color(*LINE); pdf.rect(M, y, W - 2 * M, h, "DF")
    pdf.set_fill_color(*PURPLE); pdf.rect(M, y, 2, h, "F")
    # number badge
    pdf.set_fill_color(*PURPLE); pdf.rect(M + 6, y + 5, 18, 9, "F")
    pdf.set_xy(M + 6, y + 6.5); pdf.set_text_color(*WHITE); pdf.set_font("Helvetica", "B", 10)
    pdf.cell(18, 6, num, align="C")
    pdf.set_xy(M + 28, y + 5); pdf.set_text_color(*INK); pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 6, title)
    pdf.set_fill_color(*GREEN); pdf.rect(160, y + 5, 34, 6.5, "F")
    pdf.set_xy(160, y + 6); pdf.set_text_color(*WHITE); pdf.set_font("Helvetica", "B", 8)
    pdf.cell(34, 5, "DONE", align="C")
    pdf.set_xy(M + 28, y + 12); pdf.set_text_color(60, 66, 82); pdf.set_font("Helvetica", "", 9)
    pdf.multi_cell(W - M - 28 - M, 4.2, desc)
    pdf.set_xy(M + 28, y + 23.5); pdf.set_text_color(*GREY); pdf.set_font("Helvetica", "I", 8)
    pdf.multi_cell(W - M - 28 - M, 4, "Tools:  " + tools)


pdf = FPDF("P", "mm", "A4"); pdf.set_auto_page_break(False); pdf.add_page()

# ---- header ----
pdf.set_fill_color(*PURPLE); pdf.rect(0, 0, W, 32, "F")
pdf.set_xy(M, 8); pdf.set_text_color(*WHITE); pdf.set_font("Helvetica", "B", 21)
pdf.cell(0, 9, "Granjur B2B Acquisition Pipeline")
pdf.set_xy(M, 19); pdf.set_font("Helvetica", "", 10.5)
pdf.cell(0, 6, "Automated lead generation & outreach - built in-house, on free tools")

# ---- in one sentence ----
y = 40
pdf.set_xy(M, y); pdf.set_text_color(*PURPLE); pdf.set_font("Helvetica", "B", 10); pdf.cell(0, 5, "IN ONE SENTENCE")
pdf.set_xy(M, y + 6); pdf.set_text_color(*INK); pdf.set_font("Helvetica", "", 11)
pdf.multi_cell(W - 2 * M, 5.6,
    "It finds businesses that may need Granjur's software services, researches each one, uses AI to judge "
    "fit and write a personalised pitch, and prepares compliant outreach - running the whole way from a "
    "single command, with an optional human check before any real email goes out.")

# ---- how it works ----
y = 66
pdf.set_xy(M, y); pdf.set_text_color(*PURPLE); pdf.set_font("Helvetica", "B", 10); pdf.cell(0, 5, "HOW IT WORKS")
steps(pdf, y + 7)

# ---- highlights ----
y = 105
pdf.set_xy(M, y); pdf.set_text_color(*PURPLE); pdf.set_font("Helvetica", "B", 10); pdf.cell(0, 5, "WHY IT'S DIFFERENT")
hw = (W - 2 * M - 3 * 5) / 4
data = [("100% Free", "No paid subscriptions or API fees"),
        ("Local AI", "Runs on our own PC, offline"),
        ("Live Dashboard", "See every lead & the funnel"),
        ("Compliant", "Safe for our email domains")]
for i, (t, s) in enumerate(data):
    highlight(pdf, M + i * (hw + 5), y + 7, hw, t, s)

# ---- footer note page 1 ----
pdf.set_xy(M, 133); pdf.set_text_color(*GREY); pdf.set_font("Helvetica", "I", 9)
pdf.multi_cell(W - 2 * M, 5,
    "The brain: a central database is the single source of truth - every lead flows through it, stage by "
    "stage. The four stages below never call each other; they simply hand the lead on when their job is done.")

# ---- the four components ----
y = 143
pdf.set_xy(M, y); pdf.set_text_color(*INK); pdf.set_font("Helvetica", "B", 13); pdf.cell(0, 6, "The four components")
y += 9
card(pdf, y, "WF-1", "Discovery - find the companies",
     "Pulls businesses from free public sources (online maps data and developer job boards) and lets the "
     "team paste in leads spotted on LinkedIn / Upwork. Automatically keeps only the right size and region.",
     "Python  -  OpenStreetMap  -  RemoteOK & Remotive job feeds")
card(pdf, y + 33, "WF-2", "Enrichment - research each one",
     "Detects the technology a company's website uses, finds a real public contact email, and verifies the "
     "address actually works - all from free, public information.",
     "Python  -  website analysis  -  email / DNS verification")
card(pdf, y + 66, "WF-3", "AI qualification & pitch",
     "Decides if a company is a good fit and which of three offers suits them, then AI writes a tailored "
     "pitch in the right language - English, Arabic, or Chinese.",
     "Local AI (Ollama)  -  deterministic rules engine")
card(pdf, y + 99, "WF-4", "Outreach - send & track",
     "Sends automatically end-to-end, or a person can approve pitches first. Tracks replies, bookings and "
     "unsubscribes - and stays in safe practice mode (nothing emailed) until go-live.",
     "Python  -  webhooks  -  (email provider added at go-live)")

# ---- status strip ----
pdf.set_fill_color(*INK); pdf.rect(0, 288, W, 9, "F")
pdf.set_xy(M, 290); pdf.set_text_color(*WHITE); pdf.set_font("Helvetica", "B", 8.5)
pdf.cell(0, 5, "STATUS:  one command runs all four stages, free, in practice mode.   NEXT:  warm sending domains, then go live.")

out = os.path.join(os.path.dirname(__file__), "briefing.pdf")
pdf.output(out)
print("wrote", out)
