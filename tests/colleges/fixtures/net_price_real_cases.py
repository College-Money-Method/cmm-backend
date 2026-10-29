"""Published sample aid packages used to fit the gift-share ranges.

Source: cmm-frontend plans/reports/researcher-261029-1404-net-price-evidence-widening.md
section 3. Actual net = COA minus all grants/scholarships (incl. merit and state grants).
"""

from __future__ import annotations

# (school, control, coa, sai_used, pct_need_met, merit, actual_net, source_url)
NET_PRICE_REAL_CASES = [
    ("Middlebury College", "private", 88450, 2400, 1.00, 0, 6300, "https://www.middlebury.edu/student-financial-services/estimate-your-cost/financial-aid-offer-examples"),
    ("Middlebury College", "private", 88450, 6130, 1.00, 0, 11530, "https://www.middlebury.edu/student-financial-services/estimate-your-cost/financial-aid-offer-examples"),
    ("Middlebury College", "private", 88450, 10080, 1.00, 0, 16480, "https://www.middlebury.edu/student-financial-services/estimate-your-cost/financial-aid-offer-examples"),
    ("Middlebury College", "private", 88450, 13790, 1.00, 0, 20190, "https://www.middlebury.edu/student-financial-services/estimate-your-cost/financial-aid-offer-examples"),
    ("University of Florida", "public", 23530, 6364, 0.99, 6390, 11300, "https://www.admissions.ufl.edu/pdf/uf-values-you/Sample_Aid_Package_3.pdf"),
    ("University of Florida", "public", 23530, 6364, 0.99, 4800, 12890, "https://www.admissions.ufl.edu/pdf/uf-values-you/Sample_Aid_Package_3.pdf"),
    ("University of Florida", "public", 23530, 6364, 0.99, 0, 17690, "https://www.admissions.ufl.edu/pdf/uf-values-you/Sample_Aid_Package_3.pdf"),
    ("University of Florida", "public", 23530, 0, 0.98, 6390, 2935, "https://www.admissions.ufl.edu/pdf/uf-values-you/Sample_Aid_Package_1.pdf"),
    ("University of Florida", "public", 23530, 0, 0.98, 6325, 3000, "https://www.admissions.ufl.edu/pdf/uf-values-you/Sample_Aid_Package_1.pdf"),
    ("Michigan Technological University", "public", 37431, 0, 0.92, 3000, 17936, "https://www.mtu.edu/finaid/understandingaid/resident/"),
    ("Duke University", "private", 98500, 0, 1.00, 0, 4300, "https://financialaid.duke.edu/how-aid-calculated/sample-aid-offers/"),
    ("Duke University", "private", 97500, 10000, 1.00, 0, 18800, "https://financialaid.duke.edu/how-aid-calculated/sample-aid-offers/"),
    ("Duke University", "private", 98500, 20000, 1.00, 0, 29800, "https://financialaid.duke.edu/how-aid-calculated/sample-aid-offers/"),
    ("Centre College", "private", 73990, 2900, 0.92, 0, 10400, "https://www.centre.edu/cost"),
    ("Centre College", "private", 73990, 13200, 0.92, 0, 20700, "https://www.centre.edu/cost"),
    ("Centre College", "private", 73990, 34300, 0.92, 0, 39800, "https://www.centre.edu/cost"),
    ("Centre College", "private", 73990, 44100, 0.92, 0, 49600, "https://www.centre.edu/cost"),
]

# No-need branch (SAI not published; need inferred 0 from a merit-only letter).
# (school, control, coa, sai_used, pct_need_met, merit, actual_net, source_url)
NO_NEED_REAL_CASE = (
    "Rhodes College", "private", 77110, None, None, 47500, 29610,
    "https://rhodes.edu/sites/default/files/Leroy%20Lynx%20Sample%20Financial%20Aid%20Letter.pdf",
)
