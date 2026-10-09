"""Explicit opt-in ceilings for the measured v6 session operations."""
LIMITS={5:150_000,6:150_000,7:50_000,16:50_000,17:50_000}
def compute_limit(op,mandate):
    profile=mandate.get('cu_profile')
    if profile is None:return 200_000
    if profile!='v6-measured' or op not in LIMITS:raise RuntimeError('unknown compute profile or operation')
    return LIMITS[op]
