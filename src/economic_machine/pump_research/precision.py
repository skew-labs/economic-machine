"""Transaction-boundary evidence; never infer people or continuity of funds.

Owners in pre/post token balances are SPL token authorities, not the account's
program owner. Even equal boundary owners do not prove the owner at each CPI.
The frozen instruction decoder remains unchanged.
"""
from .store import address


def enrich_transaction(tx, meta):
    snapshots=[];errors=[];keys=tx['account_keys']
    for phase in ('pre','post'):
        seen=set()
        for b in getattr(meta,phase+'_token_balances',[]):
            index=int(b.account_index)
            owner=getattr(b,'owner','');mint=getattr(b,'mint','')
            amount=getattr(getattr(b,'ui_token_amount',None),'amount','')
            decimals=getattr(getattr(b,'ui_token_amount',None),'decimals',None)
            if index<0 or index>=len(keys) or index in seen or not address(mint) or (owner and not address(owner)) or not str(amount).isdigit():
                errors.append('INVALID_TOKEN_BALANCE_'+phase);continue
            seen.add(index)
            snapshots.append(dict(account=keys[index],phase=phase,owner=owner or None,mint=mint,
                                  amount=str(amount),decimals=decimals))
    # Corrupt/duplicate metadata cannot support an owner shortcut.
    tx['token_owner_snapshots']=[] if errors else snapshots
    tx['precision_errors']=errors
    tx['precision_version']=2
    return tx


def owner_evidence(snapshots,account,mint):
    rows=[s for s in snapshots if s['account']==account and s['mint']==mint]
    phases={s['phase']:s for s in rows}
    if len(phases)!=len(rows):status='conflicting_metadata'
    elif not rows:status='missing_metadata'
    elif len(phases)!=2:status='one_boundary_only'
    elif not all(s['owner'] for s in rows):status='owner_not_recorded'
    elif phases['pre']['owner']!=phases['post']['owner']:status='changed_owner'
    else:status='same_owner_at_both_boundaries'
    return dict(status=status,snapshots=rows,
                boundary_owner=phases['pre']['owner'] if status=='same_owner_at_both_boundaries' else None,
                instruction_time_owner_proven=False,same_person_proven=False)


def order_bound(anchor,slot,stamp):
    """SQL tuple floor. Unknown instruction order excludes the whole slot.

    An unknown slot uses an actual block-time lower bound instead, and does not
    admit timestamp-less processed observations as if they were later.
    """
    if slot is not None:
        if anchor and anchor.get('tx_order') is not None and anchor.get('ix') is not None:
            return (slot,anchor['tx_order'],anchor['ix']),None,'recorded_instruction_order'
        return (slot,2**63-1,2**63-1),None,'later_slot_only'
    return (0,-1,-1),stamp,'block_time_only' if stamp is not None else 'unanchored'
