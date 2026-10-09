"""Explicit opt-in transaction ceilings; never accepts a model-supplied CU limit."""
LIMITS={5:150_000,6:150_000,7:50_000,16:50_000,17:50_000}
V7_LIMITS={5:90_000,6:120_000,7:15_000,16:5_000,17:5_000}
def compute_limit(op,mandate):
    profile=mandate.get('cu_profile')
    if profile is None:return 200_000
    limits={'v6-measured':LIMITS,'v7-measured':V7_LIMITS}.get(profile)
    if limits is None or op not in limits:raise RuntimeError('unknown compute profile or operation')
    return limits[op]
