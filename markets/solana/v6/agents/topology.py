"""Manifest-owned actor roles. Every signer has one seat or keeper authority."""
def makers(manifest):return manifest.get('makers',[0,1])
def flows(manifest):return manifest.get('flows',[2])
def keeper(manifest):return manifest.get('keeper',3)
def is_maker(role,manifest):return role in makers(manifest)
def allowed(role,manifest):
    if role in makers(manifest):return {5,7}
    if role in flows(manifest):return {6,7}
    if role==keeper(manifest):return {16,17}
    raise RuntimeError('unknown actor role')
def validate(manifest):
    n=len(manifest['agents']);roles=makers(manifest)+flows(manifest)
    if sorted(roles)!=list(range(n)) or keeper(manifest)!=n or len(set(manifest['agents']))!=n:
        raise RuntimeError('invalid actor topology')
    if not makers(manifest) or not flows(manifest):raise RuntimeError('missing market side')
