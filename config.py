"""Tunable lists for the frontline matcher (SPEC §4).

ROLES come from roles.txt (loaded at runtime by matcher.py) — do NOT hardcode
them here. EXCLUDE / LEAD_OK live in source because they are short, opinionated,
and the spec ships them as a starter list to tune as real data arrives.
"""

# Frontline threshold: we only need to know if a company has >= this many open
# frontline roles (a strong Nova signal). Counting beyond it is unnecessary, so
# fetchers/agents may early-stop once reached. Overridable via pipeline --threshold.
FRONTLINE_THRESHOLD = 20

# EXCLUDE — skilled / salaried / professional titles we must drop.
# Matched with the same `\b` + prefix rule as ROLES (see matcher.is_frontline).
# NOTE: do NOT add bare `manager` or bare `supervisor` here — that would kill
# the hourly leads in LEAD_OK.
EXCLUDE = [
    # Salaried management
    "general manager", "store manager", "district manager", "regional manager",
    "area manager", "operations manager", "branch manager", "division manager",
    "plant manager", "market manager",
    # Clinical / credentialed
    "registered nurse", "nurse practitioner", "charge nurse", "physician",
    "surgeon", "doctor", "pharmacist", "dentist", "physical therapist",
    "occupational therapist", "respiratory therapist", "clinician", "dietitian",
    "nutritionist", "social worker",
    # Technical / professional
    "engineer", "architect", "scientist", "developer", "programmer", "analyst",
    "accountant", "attorney", "lawyer", "controller",
    # Leadership
    "director", "vice president", "president", "chief", "head of", "principal",
    "superintendent", "professor", "consultant",
]

# EXCLUDE_EXACT — short acronyms matched as WHOLE words (`\bterm\b`). With only a
# leading boundary these collide with frontline words: `coo` -> COOk, `rn` ->
# words starting rn, etc. The SPEC ships them in EXCLUDE but flags it as a list
# to tune; whole-word is that tune.
EXCLUDE_EXACT = [
    "rn", "lpn", "lvn", "np", "crna",
    "vp", "ceo", "cfo", "coo", "cto", "chro",
]

# LEAD_OK — hourly leads that are allowed even when an EXCLUDE term is present.
# Matched as a plain substring (per SPEC §4), not a regex.
LEAD_OK = [
    "assistant manager", "assistant store manager", "assistant general manager",
    "assistant restaurant manager", "shift manager", "shift lead", "shift leader",
    "shift supervisor", "team lead", "team leader", "crew lead", "crew leader",
    "floor supervisor", "lead associate", "key holder", "opening crew",
    "closing crew",
]
