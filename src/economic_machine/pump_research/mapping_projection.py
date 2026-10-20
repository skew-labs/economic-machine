"""Lossless-to-decoded-field research projection, not an archival substitute.

Native balance changes are observations, not pairwise transfer attribution.
Vote transactions and failed transactions stay present because they can have
fee/balance effects. The original signed CAR witness must remain retrievable.
"""


def native_balance_observation(tx, meta):
    keys=tx.get('account_keys')
    before=list(meta.pre_balances);after=list(meta.post_balances)
    fee=int(meta.fee)
    if (not isinstance(keys,list) or not keys or len(keys)!=len(before) or len(keys)!=len(after)
        or len(set(keys))!=len(keys) or not 0<=fee<2**64
        or any(type(x) is not int or not 0<=x<2**64 for x in before+after)):
        raise ValueError('PROJECTION_BALANCE_SHAPE')
    return dict(fee_lamports=fee,
        native_balances=[dict(account=k,pre_lamports=a,post_lamports=b,delta_lamports=b-a)
                         for k,a,b in zip(keys,before,after)],
        total_delta_lamports=sum(after)-sum(before),
        native_deltas_are_pairwise_transfers=False,
        metadata_is_consensus_verified=False)


def project_block(block, metadata):
    if block.get('decode_errors') or block.get('transaction_nodes')!=len(block.get('transactions',[])):
        raise ValueError('PROJECTION_PARTIAL_BLOCK')
    txs=block['transactions']
    if len(metadata)!=len(txs) or set(metadata)!={tx['signature'] for tx in txs}:
        raise ValueError('PROJECTION_METADATA_MEMBERSHIP')
    projected=[]
    for tx in txs:
        # Retain all existing parser fields, including error/owner/commit data.
        projected.append(dict(tx,**native_balance_observation(tx,metadata[tx['signature']])))
    return dict(block,transactions=projected,projection_schema='economic-machine.mapping-projection.research.v1',
                original_car_required=True,filtered_transactions=0,
                all_protocol_attribution_complete=False)
