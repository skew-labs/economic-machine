"""A new preference revision gets a new immutable profile and separate lineage."""
import copy
from schema import digest,integer,validate_profile
def personalize(base,changes):
    validate_profile(base)
    if type(changes) is not dict or not changes or not set(changes)<= {'weights','limits'}:raise ValueError('unsupported owner preference')
    p=copy.deepcopy(base)
    for group,values in changes.items():
        if type(values) is not dict or not values or not set(values)<=set(p[group]):raise ValueError('preference field')
        for key,value in values.items():p[group][key]=integer(value)
    # The profile is not an authority grant. These are research preferences;
    # live position/loss/fee authority still comes from the existing mandate.
    p['profile_hash']=digest({k:v for k,v in p.items() if k!='profile_hash'});validate_profile(p);return p
