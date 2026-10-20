"""Native throughput policy: preserve pressure and provider backoff constraints."""
def accelerate(policy, measured, state, *, chain_id, enabled=False):
    result=dict(policy)
    if not enabled:return result
    result['throughput_mode']='owner_72h_target'
    result['model_suggested_delay_seconds']=policy['schedule_delay_seconds']
    if (policy['state']=='ADMITTED' and measured['cpu_busy_pct']<85
        and measured['io_pressure_pct']<20 and not measured['source_backoff_seconds']
        and measured['monthly_remaining_krw']>=10000):
        result['schedule_delay_seconds']=max(1,state.get('source_min_delay',1))
        if chain_id==42161:
            result['download_batch_blocks']=min(8192,state.get('source_batch_cap',8192))
    return result
