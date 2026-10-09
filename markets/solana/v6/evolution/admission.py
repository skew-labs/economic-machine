"""Unsigned handoff validation. This extension never acquires trading authority."""
from schema import digest,validate_profile,validate_program
def assess(program,profile,report,session,now):
    validate_program(program);validate_profile(profile);reasons=[]
    if report.get('program_hash')!=digest(program) or report.get('profile_hash')!=profile['profile_hash']:reasons.append('hash mismatch')
    if not report.get('research_champion_eligible'):reasons.append('candidate did not pass comparisons')
    window=report.get('window',{})
    if not window.get('external_holdout') or window.get('provenance')!='independent_market':reasons.append('no independent holdout')
    if not profile.get('live_promotion'):reasons.append('profile is research only')
    if session.get('position')!=0 or session.get('pending_signatures')!=0 or session.get('open_orders')!=0:reasons.append('handoff not flat and reconciled')
    if not session.get('active') or session.get('expires_unix',0)<=now:reasons.append('owner scope inactive')
    if session.get('profile_hash')!=profile['profile_hash']:reasons.append('different owner profile')
    if now-window.get('last_unix',0)>300 or window.get('last_unix',0)>now:reasons.append('stale evaluation')
    # Report flags alone are not authority. A future live consumer must supply
    # trusted evaluator provenance, an expected champion CAS and scoped signer.
    reasons.append('no live consumer installed for this research extension')
    return {'eligible':False,'reasons':reasons,'program_hash':digest(program),'execution_authority':False}
