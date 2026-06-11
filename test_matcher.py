"""Regression set for the frontline matcher (SPEC §4).

Prior manual validation target: 35/35 should-match and 18/18 should-not.
Run: python test_matcher.py
"""
from matcher import is_frontline

SHOULD_MATCH = [
    "Crew Member", "Line Cook", "Warehouse Associate", "Delivery Driver", "CNA",
    "Housekeeper", "Security Guard", "Shift Supervisor", "Assistant Manager",
    "Car Wash Attendant", "Route Sales Representative", "Sales Advisor", "Stocker",
    "Cashier", "Dishwasher", "Forklift Operator", "Order Picker", "Home Health Aide",
    "Certified Nursing Assistant", "Janitor", "Maintenance Technician",
    "Front Desk Agent", "Barista", "Server", "Grill Cook", "Package Handler",
    "Production Associate", "Customer Service Representative", "Caregiver",
    "Bus Driver", "Lot Attendant", "Tire Technician", "Banquet Server", "Team Lead",
    "Key Holder",
]

SHOULD_NOT_MATCH = [
    # skilled / salaried / professional (no frontline stem, or excluded)
    "Registered Nurse", "General Manager", "District Manager", "Software Engineer",
    "Data Analyst", "Store Manager", "Pharmacist", "Marketing Director",
    "Staff Accountant", "Registered Dietitian", "Physical Therapist",
    # frontline stem present BUT dropped by EXCLUDE (exercise the exclude branch)
    "Maintenance Engineer", "Service Director", "Warehouse Operations Manager",
    # false friends — leading \\b must block these
    "Reporter", "Observer", "Career Coach", "Resort Manager",
]


def run():
    fails = []
    for t in SHOULD_MATCH:
        if not is_frontline(t):
            fails.append(("SHOULD MATCH but did not", t))
    for t in SHOULD_NOT_MATCH:
        if is_frontline(t):
            fails.append(("SHOULD NOT MATCH but did", t))

    m_ok = sum(1 for t in SHOULD_MATCH if is_frontline(t))
    n_ok = sum(1 for t in SHOULD_NOT_MATCH if not is_frontline(t))
    print(f"should-match:     {m_ok}/{len(SHOULD_MATCH)}")
    print(f"should-not-match: {n_ok}/{len(SHOULD_NOT_MATCH)}")

    if fails:
        print("\nFAILURES:")
        for reason, t in fails:
            print(f"  [{reason}] {t!r}")
        raise SystemExit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    run()
