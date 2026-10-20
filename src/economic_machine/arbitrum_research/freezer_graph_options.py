"""Future-only derived graph materialization; old page policies stay exact."""
def options(chain, prepared, memory_setting, cache_setting, existing_policy=None):
    if memory_setting not in ('0', '1') or cache_setting not in ('0', '512'):
        raise ValueError('GRAPH_EXECUTION_SETTING')
    if existing_policy is None:
        memory = memory_setting == '1'
        cache = 512 if cache_setting == '512' else None
    else:
        mode = existing_policy.get('graph_materialization')
        cache = existing_policy.get('graph_cache_mib')
        if mode not in (None, 'memory_sealed_v1') or (cache is not None and (type(cache) is not int or cache != 512)):
            raise ValueError('GRAPH_EXECUTION_POLICY')
        memory = mode == 'memory_sealed_v1'
    if (memory or cache is not None) and (chain != 42161 or prepared is not True):
        raise ValueError('GRAPH_EXECUTION_SCOPE')
    policy = {}
    kwargs = {}
    if cache is not None:
        policy['graph_cache_mib'] = cache
        kwargs['cache_mib'] = cache
    if memory:
        policy['graph_materialization'] = 'memory_sealed_v1'
        kwargs['memory_build'] = True
    return policy, kwargs
