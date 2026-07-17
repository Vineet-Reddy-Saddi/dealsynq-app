# -*- coding: utf-8 -*-
"""Render the Five Town Plaza property profile into a clean, designed PDF."""
import json
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.colors import HexColor
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.platypus import (
    BaseDocTemplate, PageTemplate, Frame, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether, FrameBreak, NextPageTemplate, PageBreak,
)

PROFILE = json.load(open("fivetownplaza/PROFILE.json", encoding="utf-8"))

# ---- Palette -------------------------------------------------------------
NAVY    = HexColor("#12304C")
BLUE    = HexColor("#1F4E78")
STEEL   = HexColor("#3A6EA5")
GOLD    = HexColor("#B98A2E")
INK     = HexColor("#1B2733")
SLATE   = HexColor("#5A6B7B")
MIST    = HexColor("#F1F5F9")
LINE    = HexColor("#D8E0E8")
WHITE   = colors.white

VERIFIED = HexColor("#1E7A46")
STRONG   = HexColor("#1F6FB2")
ESTIMATE = HexColor("#B07A16")
UNRES    = HexColor("#8A97A5")

USABLE_W = letter[0] - 1.5 * inch   # 0.75" margins each side

# ---- Styles --------------------------------------------------------------
ss = getSampleStyleSheet()

def S(name, **kw):
    kw.setdefault("fontName", "Helvetica")
    kw.setdefault("textColor", INK)
    kw.setdefault("fontSize", 9)
    kw.setdefault("leading", 12.5)
    return ParagraphStyle(name, parent=ss["Normal"], **kw)

body      = S("body")
body_sm   = S("body_sm", fontSize=8, leading=10.5, textColor=SLATE)
lead      = S("lead", fontSize=10.5, leading=15, textColor=INK)
h_sec     = S("h_sec", fontName="Helvetica-Bold", fontSize=13, textColor=NAVY, leading=15)
kicker    = S("kicker", fontName="Helvetica-Bold", fontSize=7.5, textColor=GOLD, leading=10, spaceAfter=1)
tbl_head  = S("tbl_head", fontName="Helvetica-Bold", fontSize=7.8, textColor=WHITE, leading=9.5)
tbl_cell  = S("tbl_cell", fontSize=8, leading=10, textColor=INK)
tbl_cellb = S("tbl_cellb", fontName="Helvetica-Bold", fontSize=8, leading=10, textColor=INK)
tbl_num   = S("tbl_num", fontSize=8, leading=10, textColor=INK, alignment=TA_RIGHT)
note_sty  = S("note", fontSize=7.5, leading=10, textColor=SLATE)

# cover styles
cover_name = S("cover_name", fontName="Helvetica-Bold", fontSize=30, textColor=WHITE, leading=32)
cover_sub  = S("cover_sub", fontSize=11, textColor=HexColor("#C7D6E5"), leading=15)
cover_kick = S("cover_kick", fontName="Helvetica-Bold", fontSize=9, textColor=GOLD, leading=12)
kpi_num    = S("kpi_num", fontName="Helvetica-Bold", fontSize=17, textColor=NAVY, alignment=TA_CENTER, leading=19)
kpi_lbl    = S("kpi_lbl", fontName="Helvetica-Bold", fontSize=6.6, textColor=SLATE, alignment=TA_CENTER, leading=8.5)

# ---- Helpers -------------------------------------------------------------
def money(n):
    return "${:,.0f}".format(n)

def P(txt, sty=body):
    return Paragraph(txt, sty)

def esc(s):
    """Escape data-derived text so stray & / < / > don't break the mini-HTML parser."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

def section(title, kicker_txt=None):
    els = []
    if kicker_txt:
        els.append(P(kicker_txt.upper(), kicker))
    els.append(P(title, h_sec))
    els.append(HRFlowable(width="100%", thickness=1.4, color=GOLD,
                          spaceBefore=3, spaceAfter=8, lineCap="round"))
    return els

def datatable(header, rows, col_w, aligns=None, zebra=True, head_bg=NAVY, font=8):
    data = [[Paragraph(h, tbl_head) for h in header]]
    for r in rows:
        data.append(r)
    t = Table(data, colWidths=col_w, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), head_bg),
        ("TOPPADDING", (0, 0), (-1, 0), 5),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ("TOPPADDING", (0, 1), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
        ("LINEBELOW", (0, 0), (-1, 0), 0, head_bg),
    ]
    if zebra:
        for i in range(1, len(data)):
            if i % 2 == 0:
                style.append(("BACKGROUND", (0, i), (-1, i), MIST))
    if aligns:
        for ci, a in enumerate(aligns):
            style.append(("ALIGN", (ci, 0), (ci, -1), a))
    t.setStyle(TableStyle(style))
    return t

def factbox(title, lines, accent=BLUE):
    """A titled callout box."""
    inner = [Paragraph(title, S("fb_t", fontName="Helvetica-Bold", fontSize=8.5,
                                textColor=accent, leading=11, spaceAfter=3))]
    for l in lines:
        inner.append(Paragraph(l, S("fb_l", fontSize=8, leading=11, textColor=INK)))
    t = Table([[inner]], colWidths=[USABLE_W])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), MIST),
        ("LINEBEFORE", (0, 0), (0, -1), 3, accent),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 7),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ]))
    return t

# ---- Page furniture ------------------------------------------------------
def cover_bg(canvas, doc):
    canvas.saveState()
    canvas.setFillColor(NAVY)
    canvas.rect(0, letter[1] - 3.15 * inch, letter[0], 3.15 * inch, fill=1, stroke=0)
    canvas.setFillColor(GOLD)
    canvas.rect(0, letter[1] - 3.20 * inch, letter[0], 0.05 * inch, fill=1, stroke=0)
    footer(canvas, doc)
    canvas.restoreState()

def footer(canvas, doc):
    canvas.saveState()
    canvas.setStrokeColor(LINE)
    canvas.setLineWidth(0.5)
    canvas.line(0.75 * inch, 0.62 * inch, letter[0] - 0.75 * inch, 0.62 * inch)
    canvas.setFont("Helvetica", 7.2)
    canvas.setFillColor(SLATE)
    canvas.drawString(0.75 * inch, 0.46 * inch, "Five Town Plaza  •  Property Intelligence Profile")
    canvas.drawRightString(letter[0] - 0.75 * inch, 0.46 * inch, "Page %d" % doc.page)
    canvas.drawCentredString(letter[0] / 2.0, 0.46 * inch, "DealSynq  •  Prepared for MCAP")
    canvas.restoreState()

def normal_bg(canvas, doc):
    footer(canvas, doc)

# ---- Build content -------------------------------------------------------
story = []

# ===== COVER =====
story.append(Spacer(1, 0.42 * inch))
story.append(P("PROPERTY INTELLIGENCE PROFILE", cover_kick))
story.append(Spacer(1, 4))
story.append(P("Five Town Plaza", cover_name))
story.append(Spacer(1, 3))
story.append(P("380 Cooley Street, Springfield, Massachusetts 01128", cover_sub))
story.append(P("Grocery-anchored retail shopping center  •  9-parcel assemblage", cover_sub))
story.append(Spacer(1, 0.62 * inch))

# KPI tiles
kpis = [
    ("9", "PARCELS"),
    ("29.75", "LAND ACRES"),
    ("336,205", "BUILDING SF"),
    ("99.4%", "OCCUPANCY"),
    ("$31.0M", "2014 SALE"),
]
tile_cells = []
for num, lbl in kpis:
    tile = Table([[P(num, kpi_num)], [P(lbl, kpi_lbl)]], colWidths=[(USABLE_W - 4 * 8) / 5.0])
    tile.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), WHITE),
        ("BOX", (0, 0), (-1, -1), 0.8, LINE),
        ("LINEABOVE", (0, 0), (-1, 0), 2.2, GOLD),
        ("TOPPADDING", (0, 0), (-1, 0), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
        ("TOPPADDING", (0, 1), (-1, 1), 0),
        ("BOTTOMPADDING", (0, 1), (-1, 1), 9),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
    ]))
    tile_cells.append(tile)
kpi_row = Table([tile_cells], colWidths=[(USABLE_W) / 5.0] * 5)
kpi_row.setStyle(TableStyle([
    ("LEFTPADDING", (0, 0), (-1, -1), 2), ("RIGHTPADDING", (0, 0), (-1, -1), 2),
    ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
]))
story.append(kpi_row)
story.append(Spacer(1, 0.3 * inch))

story.append(factbox(
    "AT A GLANCE",
    ["A single street address (380 Cooley St) resolved into a nine-parcel retail assemblage "
     "owned by <b>Five Town Station LLC</b>, a Delaware subsidiary of <b>Phillips Edison &amp; "
     "Company</b> (NASDAQ: PECO) — a publicly traded grocery-anchored shopping-center REIT. "
     "Acquired all-cash for <b>$31,007,400</b> in 2014. 336,205 SF of building across 15+ retail, "
     "restaurant and service tenants at <b>99.4% occupancy</b>, anchored by Big Y, Burlington, "
     "Ollie&#39;s and Best Fitness. Assembled from five independent government and corporate "
     "sources, with a per-field confidence rating on every data point.",],
    accent=BLUE))

story.append(Spacer(1, 0.14 * inch))
story.append(P("Assembled %s  •  Version %s  •  Confidential"
               % (PROFILE["profile_assembled_at"][:10], PROFILE.get("profile_version", 3)),
               S("meta", fontSize=7.5, textColor=SLATE)))

story.append(NextPageTemplate("normal"))
story.append(PageBreak())

# ===== 1. IDENTITY & OVERVIEW =====
story += section("Property Identity", "Section 1")
ident_rows = [
    [P("Property name", tbl_cellb), P("Five Town Plaza (a.k.a. Five Town Station, the legal entity name)", tbl_cell)],
    [P("Anchor address", tbl_cellb), P("380 Cooley Street, Springfield, MA 01128", tbl_cell)],
    [P("Coordinates", tbl_cellb), P("42.094472, -72.501118", tbl_cell)],
    [P("Property type", tbl_cellb), P("Retail — neighborhood / community shopping center", tbl_cell)],
    [P("Use classification", tbl_cellb), P("MA assessor Class 323: Shopping Centers", tbl_cell)],
    [P("Zoning", tbl_cellb), P("SR1C1 (Springfield assessor record card)", tbl_cell)],
    [P("Streets spanned", tbl_cellb), P("Cooley Street + Allen Street", tbl_cell)],
    [P("Resolution method", tbl_cellb), P("Owner-entity aggregation — all parcels owned by the legal entity, grouped from the street-address anchor", tbl_cell)],
]
story.append(datatable(["Field", "Value"], ident_rows, [1.55 * inch, USABLE_W - 1.55 * inch],
                       aligns=["LEFT", "LEFT"]))
story.append(Spacer(1, 14))

# ===== 2. PARCEL ASSEMBLAGE =====
story += section("Parcel Assemblage", "Section 2  •  9 parcels  •  29.75 acres")
pr = []
for p in PROFILE["parcels"]["detail"]:
    pr.append([
        P(p["apn"], tbl_cell),
        P(p["situs_address"].title(), tbl_cell),
        P("{:,.0f}".format(p["land_sqft"]), tbl_num),
        P("{:,.0f}".format(p["building_sqft_govt_source"]) if p["building_sqft_govt_source"] else "—", tbl_num),
        P(money(p["land_assessed_value"]), tbl_num),
        P(p["zone"], tbl_cell),
    ])
tot = PROFILE["parcels"]
numb = S("numb", fontName="Helvetica-Bold", fontSize=8, alignment=TA_RIGHT)
pr.append([
    P("TOTAL", tbl_cellb), P("9 parcels", tbl_cellb),
    Paragraph("{:,.0f}".format(tot["total_land_sqft"]), numb),
    Paragraph("336,205", numb),
    Paragraph(money(tot["total_land_assessed_value"]), numb),
    P("", tbl_cell),
])
tbl = datatable(
    ["APN", "Situs Address", "Land SF", "Bldg SF", "Assessed Value", "Zone"],
    pr, [0.82 * inch, 1.55 * inch, 0.78 * inch, 0.72 * inch, 1.05 * inch, 0.78 * inch],
    aligns=["LEFT", "LEFT", "RIGHT", "RIGHT", "RIGHT", "LEFT"])
tbl.setStyle(TableStyle([("BACKGROUND", (0, len(pr)), (-1, len(pr)), HexColor("#E7EDF3")),
                         ("LINEABOVE", (0, len(pr)), (-1, len(pr)), 1, NAVY)]))
story.append(tbl)
story.append(Spacer(1, 5))
story.append(P("Assessed value is land + improvement value per the Springfield assessor. Building SF is "
               "the government record-card floor area (see Section 3). Two parcels are parking/land only.", note_sty))
story.append(Spacer(1, 14))

# ===== 3. BUILDING DETAIL =====
story += section("Building &amp; Improvements", "Section 3")
b = PROFILE["building"]
story.append(factbox(
    "TOTAL BUILDING AREA — 336,205 SF (confirmed by 3 independent sources)",
    ["<b>Government source:</b> 336,205 SF (Springfield assessor record cards, all buildings)",
     "<b>Cross-check 1:</b> 327,303 SF (Phillips Edison leasing flyer) — within 2.7%",
     "<b>Cross-check 2:</b> 328,372 SF (LoopNet marketing) — within 2.4%",
     "7 separate buildings on the main parcel; year built 1970 (core) – 2004 (newest outparcel). "
     "Building-to-land ratio 25.9%. One passenger elevator (Big Y); 4 of the buildings are wet-sprinklered.",],
    accent=VERIFIED))
story.append(Spacer(1, 9))
br = []
for d in b["per_building_detail"]:
    label = ("Card %s" % d["card"]) if "card" in d else d.get("situs", d.get("apn", ""))
    br.append([
        P(esc(label), tbl_cell),
        P(esc(d["structure_type"].title()), tbl_cell),
        Paragraph(esc(d["grade"]), S("g", fontName="Helvetica-Bold", fontSize=8, alignment=TA_CENTER)),
        P("{:,}".format(d["sqft"]), tbl_num),
        P(esc(", ".join(d["features"]).title()), note_sty),
    ])
story.append(datatable(
    ["Building", "Structure Type", "Grade", "SF", "Notable Features"],
    br, [0.85 * inch, 1.35 * inch, 0.5 * inch, 0.72 * inch, USABLE_W - 3.42 * inch],
    aligns=["LEFT", "LEFT", "CENTER", "RIGHT", "LEFT"]))
story.append(Spacer(1, 4))
story.append(P(b["source_note"], note_sty))
story.append(Spacer(1, 14))

# ===== 4. OWNERSHIP =====
story += section("Ownership &amp; Entity Structure", "Section 4")
own = PROFILE["ownership"]
pc = own["parent_chain"]
story.append(factbox(
    "OWNERSHIP CHAIN — formally confirmed via SEC filing",
    ["<b>Property-owning entity:</b> Five Town Station LLC (Delaware)",
     "<b>Parent:</b> Phillips Edison Grocery Center REIT I, Inc. (Maryland) — now Phillips Edison &amp; "
     "Company, Inc., NASDAQ: PECO",
     "<b>Mailing address:</b> 11501 Northlake Dr, Cincinnati, OH 45249",
     "<b>Source:</b> SEC Exhibit 21.1 (Subsidiaries of the Registrant), FY2014 Form 10-K — a federal "
     "filing, not inferred from the mailing address. The &#8220;[Property] Station LLC&#8221; naming "
     "pattern appears 150+ times in the same exhibit, confirming PECO&#39;s one-entity-per-property structure.",],
    accent=VERIFIED))
story.append(Spacer(1, 9))
mgmt_rows = [
    [P("Property manager", tbl_cellb), P("Phillips Edison &amp; Company (self-managed)", tbl_cell)],
    [P("Leasing broker", tbl_cellb), P("Scott Faloni — (410) 693-3248 — sfaloni@phillipsedison.com", tbl_cell)],
    [P("", tbl_cellb), P("Brogan Burns — (513) 344-0989 — bburns@phillipsedison.com", tbl_cell)],
    [P("Hold period", tbl_cellb), P("11.8 years (since Sept 2014 acquisition)", tbl_cell)],
]
story.append(datatable(["Field", "Detail"], mgmt_rows, [1.55 * inch, USABLE_W - 1.55 * inch],
                       aligns=["LEFT", "LEFT"]))
story.append(Spacer(1, 14))

# ===== 5. TRANSACTION HISTORY =====
story += section("Transaction History", "Section 5")
tr = []
for t in PROFILE["transaction_history"]:
    detail = "Seller: %s" % t.get("seller", "—")
    if t.get("buyer"):
        detail = "Buyer: %s<br/>%s" % (t["buyer"], detail)
    if t.get("structure"):
        detail += "<br/><font size=7 color='#5A6B7B'>%s</font>" % t["structure"]
    tr.append([
        P(t["date"], tbl_cellb),
        Paragraph(money(t["price"]), S("pr", fontName="Helvetica-Bold", fontSize=8.5, textColor=NAVY, alignment=TA_RIGHT)),
        P(t.get("deed_ref", "—"), tbl_cell),
        P(detail, note_sty),
    ])
story.append(datatable(
    ["Date", "Price", "Deed", "Parties"],
    tr, [0.85 * inch, 1.0 * inch, 0.72 * inch, USABLE_W - 2.57 * inch],
    aligns=["LEFT", "RIGHT", "LEFT", "LEFT"]))
story.append(Spacer(1, 5))
story.append(P("The 2014 sale price is <b>verified by three independent sources</b>: Hampden County "
               "Registry of Deeds, the Springfield assessor record card, and the seller&#39;s own SEC "
               "Form 8-K (Urstadt Biddle Properties). Anchor lineage runs Mott&#39;s ShopRite &#8594; A&amp;P "
               "&#8594; Big Y (grocery) and W.T. Grant &#8594; Caldor&#39;s &#8594; Spag&#39;s &#8594; "
               "Burlington (discount).", note_sty))
story.append(Spacer(1, 14))

# ===== 6. FINANCING =====
story += section("Financing &amp; Encumbrances", "Section 6")
fin = PROFILE["financing"]
story.append(factbox(
    "NO RECORDED MORTGAGE",
    ["No mortgage is recorded against the parcels at Hampden County — a verified negative "
     "(the same scraper correctly finds mortgages on other properties).",
     "<b>Why:</b> the seller&#39;s SEC filing states the sale was all-cash, funded from the buyer&#39;s "
     "corporate credit facility rather than property-secured debt — standard for a grocery-anchored REIT.",
     "<b>Related filing:</b> a 2017 UCC financing statement with United Bank (book/page 21802-374).",
     "<b>Distress check:</b> no foreclosure instrument found in the full county records pull. "
     "Bankruptcy not directly checked (requires a paid PACER account).",],
    accent=STRONG))
story.append(Spacer(1, 8))

# Other recorded documents
od = PROFILE["other_recorded_documents"]
odr = []
for d in od:
    odr.append([
        P(d["date_received"], tbl_cell),
        P(esc(d["document_type"]), tbl_cellb),
        P(esc(d["reverse_party"] or "—"), tbl_cell),
        P(d["book_page"], tbl_cell),
    ])
story.append(P("Other recorded instruments (Hampden County Registry of Deeds):", tbl_cellb))
story.append(Spacer(1, 4))
story.append(datatable(
    ["Recorded", "Document Type", "Counterparty", "Book/Page"],
    odr, [0.9 * inch, 1.5 * inch, USABLE_W - 3.5 * inch, 1.1 * inch],
    aligns=["LEFT", "LEFT", "LEFT", "LEFT"]))
story.append(Spacer(1, 14))

# ===== 7. TENANTS =====
story += section("Tenants &amp; Occupancy", "Section 7  •  35 spaces  •  99.4% occupied")
te = PROFILE["tenants"]
bignum = lambda t, c: Paragraph(t, S("bn" + t, fontName="Helvetica-Bold", fontSize=13, textColor=c, alignment=TA_CENTER))
occ_rows = [[
    bignum("35", NAVY), bignum("34", VERIFIED), bignum("1", ESTIMATE), bignum("99.4%", NAVY),
]]
occ_lbls = [["Total spaces", "Occupied", "Available", "Occupancy (by SF)"]]
occ = Table(occ_rows + [[P(x, kpi_lbl) for x in occ_lbls[0]]],
            colWidths=[USABLE_W / 4.0] * 4)
occ.setStyle(TableStyle([
    ("BOX", (0, 0), (-1, -1), 0.8, LINE),
    ("INNERGRID", (0, 0), (-1, -1), 0.5, LINE),
    ("TOPPADDING", (0, 0), (-1, 0), 8), ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
    ("TOPPADDING", (0, 1), (-1, 1), 0), ("BOTTOMPADDING", (0, 1), (-1, 1), 8),
    ("BACKGROUND", (0, 0), (-1, -1), MIST),
]))
story.append(occ)
story.append(Spacer(1, 10))

# Tenant roster table
def tenant_rows(items):
    out = []
    for t in items:
        pub = ""
        if t.get("public_company_flag"):
            pub = t.get("ticker", "Public")
        elif t.get("ticker"):
            pub = t["ticker"]
        status = t["status"]
        stcolor = VERIFIED if status == "occupied" else ESTIMATE
        out.append([
            P(esc(t["space_id"]), tbl_cell),
            P(esc(t["tenant_name"]), tbl_cellb if t["sqft"] >= 30000 else tbl_cell),
            P("{:,}".format(t["sqft"]), tbl_num),
            Paragraph(status.title(), S("st", fontSize=7.6, textColor=stcolor, alignment=TA_CENTER, fontName="Helvetica-Bold")),
            P(esc(pub), note_sty),
        ])
    return out

roster = te["roster"]
story.append(datatable(
    ["Sp.", "Tenant", "SF", "Status", "Public / Ticker"],
    tenant_rows(roster),
    [0.5 * inch, USABLE_W - 3.05 * inch, 0.72 * inch, 0.78 * inch, 1.05 * inch],
    aligns=["LEFT", "LEFT", "RIGHT", "CENTER", "LEFT"]))
story.append(Spacer(1, 5))
story.append(P("Roster from the Phillips Edison leasing flyer (owner source). Ollie&#39;s Bargain Outlet "
               "is independently cross-confirmed by a 2025 recorded lease at the county registry. "
               "8 tenants are publicly traded companies — a credit-quality signal.", note_sty))
story.append(Spacer(1, 14))

# ===== 8. PERMITS =====
story += section("Permit Activity", "Section 8")
pm = PROFILE["permits"]
story.append(P("25+ building permits on record (2010–2025) for the main parcel, from the Springfield "
               "assessor record card. Notable recent activity:", body))
story.append(Spacer(1, 5))
pmr = [[P("• " + x, tbl_cell)] for x in pm["notable"]]
story.append(datatable(["Notable Permits"], pmr, [USABLE_W], aligns=["LEFT"], zebra=True))
story.append(Spacer(1, 14))

# ===== 9. LOCATION / DEMOGRAPHICS / MARKET =====
story += section("Location, Demographics &amp; Market", "Section 9")
dm = PROFILE["demographics"]
demo_rows = [
    [P("Radius", tbl_head), P("Population", tbl_head), P("Households", tbl_head), P("Median HHI", tbl_head)],
]
demo_data = [
    [P("3-mile", tbl_cellb),
     P("{:,}".format(dm["3_mile_radius"]["population"]), tbl_num),
     P("{:,}".format(dm["3_mile_radius"]["households"]), tbl_num),
     P(money(dm["3_mile_radius"]["median_hhi"]), tbl_num)],
    [P("5-mile", tbl_cellb),
     P("{:,}".format(dm["5_mile_radius"]["population"]), tbl_num),
     P("{:,}".format(dm["5_mile_radius"]["households"]), tbl_num),
     P(money(dm["5_mile_radius"]["median_hhi"]), tbl_num)],
]
story.append(datatable(["Radius", "Population", "Households", "Median HHI"], demo_data,
                       [1.3 * inch, (USABLE_W - 1.3 * inch) / 3.0, (USABLE_W - 1.3 * inch) / 3.0, (USABLE_W - 1.3 * inch) / 3.0],
                       aligns=["LEFT", "RIGHT", "RIGHT", "RIGHT"]))
story.append(Spacer(1, 9))
mk = PROFILE["market_context"]
haz = PROFILE["hazard"]
dv = PROFILE["derived_metrics"]
loc = PROFILE["location_context"]
tax = PROFILE["tax"]
tc = mk["traffic_counts"]
mkt_rows = [
    [P("Neighborhood", tbl_cellb), P("%s  •  Census tract %s, block %s  •  city sector %s"
        % (loc["neighborhood"], loc["census_tract"], loc["census_block"], loc["city_sector"]), tbl_cell)],
    [P("Designations", tbl_cellb), P("Historic district: <b>No</b>   •   Overlay district: <b>None</b>   •   CDBG zone: <b>No</b>", tbl_cell)],
    [P("Traffic counts", tbl_cellb), P("Bicentennial Hwy <b>40,000</b> vpd  •  Cooley St <b>10,000</b> vpd  •  Allen St <b>5,000</b> vpd", tbl_cell)],
    [P("Est. annual tax", tbl_cellb), P("<b>%s</b>  (assessed %s &#215; $%.2f / $1,000 FY2026 commercial rate)"
        % (money(tax["estimated_annual_tax"]), money(tax["total_assessed_value"]), tax["fy2026_commercial_tax_rate_per_1000"]), tbl_cell)],
    [P("Price / SF (2014 basis)", tbl_cellb), P("$%.2f" % dv["price_per_square_foot"]["value"], tbl_cell)],
    [P("Springfield avg. cap rate", tbl_cellb), P("%.2f%% (vs. 6.55%% national for large retail centers)" % mk["springfield_avg_cap_rate_pct"], tbl_cell)],
    [P("Flood risk", tbl_cellb), P("FEMA Zone X — minimal hazard, outside the 500-yr floodplain (all 9 parcels)", tbl_cell)],
    [P("Environmental", tbl_cellb), P("No EPA-regulated facility on-site (checked all 42 in ZIP 01118 via EPA ECHO)", tbl_cell)],
    [P("Parcel geometry", tbl_cellb), P("GeoJSON outlines + centroids in hand for all 9 parcels (building footprint outlines still need CV)", tbl_cell)],
]
story.append(datatable(["Metric", "Value"], mkt_rows, [1.85 * inch, USABLE_W - 1.85 * inch],
                       aligns=["LEFT", "LEFT"]))
story.append(Spacer(1, 16))

# ===== 10. CONFIDENCE SUMMARY =====
story += section("Data Confidence Summary", "Section 10  •  every field rated, nothing overclaimed")
cs = PROFILE["confidence_summary"]
tiers = [
    ("VERIFIED", VERIFIED, "Authoritative source, or multiple independent sources agree", cs["verified_tier"]),
    ("STRONG", STRONG, "High-quality single source with reliable match", cs["strong_tier"]),
    ("ESTIMATED", ESTIMATE, "Model-derived or inferred from indirect signals", cs["estimated_tier"]),
    ("UNRESOLVED", UNRES, "Blocked / paywalled / needs additional tooling", cs["unresolved"]),
]
crows = []
for name, col, desc, items in tiers:
    badge = Table([[P(name, S("bdg", fontName="Helvetica-Bold", fontSize=7.5, textColor=WHITE, alignment=TA_CENTER))]],
                  colWidths=[0.95 * inch])
    badge.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, -1), col),
                               ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                               ("ROUNDEDCORNERS", [3, 3, 3, 3])]))
    lst = "<br/>".join("• " + i for i in items)
    crows.append([badge, Paragraph(lst, S("ci", fontSize=8, leading=11.5, textColor=INK))])
ct = Table(crows, colWidths=[1.1 * inch, USABLE_W - 1.1 * inch])
ct.setStyle(TableStyle([
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
    ("LEFTPADDING", (0, 0), (0, -1), 0),
    ("LINEBELOW", (0, 0), (-1, -2), 0.5, LINE),
]))
story.append(ct)
story.append(Spacer(1, 16))

# ===== 11. SOURCES & METHODOLOGY =====
story += section("Sources &amp; Methodology", "Section 11")
story.append(P("Every field in this profile was gathered from a general, reusable tool driven only by "
               "this property&#39;s address and owner name — no code is specific to Five Town Plaza. "
               "The same tools work on any property. Sources used:", body))
story.append(Spacer(1, 5))
src_rows = [[P("• " + s, tbl_cell)] for s in PROFILE["sources_used"]]
story.append(datatable(["Sources"], src_rows, [USABLE_W], aligns=["LEFT"]))
story.append(Spacer(1, 10))
story.append(P("Data-quality controls applied:", tbl_cellb))
story.append(Spacer(1, 4))
for d in PROFILE["data_quality_log"]:
    story.append(P("<b>• %s</b><br/><font size=7.5 color='#5A6B7B'>%s</font>"
                   % (d["issue"], d["resolution"]), S("dq", fontSize=8, leading=11, spaceAfter=5)))

# ---- Document assembly ---------------------------------------------------
frame = Frame(0.75 * inch, 0.72 * inch, USABLE_W, letter[1] - 1.5 * inch, id="main")
doc = BaseDocTemplate("fivetownplaza/Five_Town_Plaza_Profile.pdf", pagesize=letter,
                      leftMargin=0.75 * inch, rightMargin=0.75 * inch,
                      topMargin=0.75 * inch, bottomMargin=0.72 * inch,
                      title="Five Town Plaza — Property Intelligence Profile",
                      author="DealSynq")
doc.addPageTemplates([
    PageTemplate(id="cover", frames=[frame], onPage=cover_bg),
    PageTemplate(id="normal", frames=[frame], onPage=normal_bg),
])
doc.build(story)
print("Wrote fivetownplaza/Five_Town_Plaza_Profile.pdf")
