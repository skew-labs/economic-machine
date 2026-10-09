"""Deterministic shadow counterfactuals. Receipt VWAP is a proxy, not a fill proof."""
import json,math,sqlite3,statistics
from schema import canonical,digest,validate_profile
from runtime import Engine

def load_window(path,after=0,limit=600):
    db=sqlite3.connect('file:'+str(path)+'?mode=ro',uri=True)
    rows=db.execute('SELECT id,hash,body FROM frames WHERE id>? ORDER BY id LIMIT ?',(after,limit)).fetchall()
    frames=[dict(json.loads(body),id=i,account_hash=h) for i,h,body in rows]
    receipts=[]
    if frames:
        receipts=[dict(zip(('signature','slot','side','lots','turnover'),r)) for r in db.execute('SELECT signature,slot,side,lots,turnover FROM receipts WHERE slot>? AND slot<=? ORDER BY slot,signature',(frames[0]['slot'],frames[-1]['slot']))]
    db.close();return frames,receipts

def manifest(frames,receipts):
    if len(frames)<24:raise ValueError('insufficient observations')
    if any(not f['accounting_verified'] or f['provenance']!='controlled_devnet_self_play' or f['external_holdout'] for f in frames):raise ValueError('unsupported observation provenance')
    if any(b['slot']<=a['slot'] or not 0<=b['unix']-a['unix']<=15 for a,b in zip(frames,frames[1:])):raise ValueError('unordered or gapped observations')
    # Match the deployed program's external-oracle 90-second publish-age
    # rule; market account observation time is not the oracle publish time.
    if any(f['slot']<f['oracle_slot'] or f['unix']-f['publish_time']>90 or f['publish_time']>f['unix']+2 for f in frames):raise ValueError('stale oracle in observations')
    if any(r['side'] not in (0,1) or r['lots']<0 or r['turnover']<0 for r in receipts):raise ValueError('invalid receipt')
    return {'first_id':frames[0]['id'],'last_id':frames[-1]['id'],'first_slot':frames[0]['slot'],'last_slot':frames[-1]['slot'],
        'first_unix':frames[0]['unix'],'last_unix':frames[-1]['unix'],'frame_count':len(frames),'receipt_count':len(receipts),
        'hash':digest({'frames':frames,'receipts':receipts}),'provenance':'controlled_devnet_self_play','external_holdout':False,
        'fill_model':'snapshot queue + aggregated IOC VWAP proxy; counterfactual fills are estimates',
        'network_cost':'not valued in PnL; quote churn receives a separate objective penalty'}

def features(history,position):
    f=history[-1];marks=[x['mark'] for x in history[-8:]]
    volatility=min(100,max(marks)-min(marks))
    return [position,max(-100,min(100,f['imbalance'])),max(-100,min(100,f['momentum'])),volatility,min(4096,f['depth']),min(100,max(0,f['unix']-f['publish_time']))]

def replay(program,profile,frames,receipts,delay=1,queue_multiplier=1):
    validate_profile(profile);manifest(frames,receipts)
    position=0;cash=0;peak=0;drawdown=0;turnover=0;fills=0;updates=0;inventory_sum=0;adverse=0;halted=False;max_position=0
    resting=[];pending=[];last_quote=-100;receipt_index=0;last_funding=frames[0]['funding'];kernels=[];trace=[]
    with Engine(program,profile) as engine:
        for index,f in enumerate(frames):
            cash-=position*(f['funding']-last_funding);last_funding=f['funding']
            # Old resting quotes remain exposed until the replacement arrives.
            while receipt_index<len(receipts) and receipts[receipt_index]['slot']<=f['slot']:
                trade=receipts[receipt_index];receipt_index+=1
                if not trade['lots']:continue
                vwap=trade['turnover']/trade['lots'];volume=trade['lots']
                for q in resting:
                    if q['side']==trade['side'] or q['lots']<=0:continue
                    if (q['side']==0 and vwap>q['price']) or (q['side']==1 and vwap<q['price']):continue
                    consumed=min(volume,q['ahead']);q['ahead']-=consumed;volume-=consumed
                    # Only half of remaining observed demand is attributed to
                    # the counterfactual quote. No unobserved demand is invented.
                    take=min(q['lots'],volume//2)
                    limit=profile['limits']['position'];room=limit-position if q['side']==0 else limit+position
                    if q['reduce']:room=max(0,-position if q['side']==0 else position)
                    take=min(take,max(0,room))
                    if not take:continue
                    signed=take if q['side']==0 else -take;position+=signed;cash-=signed*q['price'];q['lots']-=take;volume-=take
                    turnover+=take*q['price'];fills+=take
                    # Delayed markout is used only in scoring, never in features.
                    future_mark=frames[min(len(frames)-1,index+3)]['mark'];adverse+=max(0,signed*(q['price']-future_mark))
            if pending and pending[0][0]<=index:
                _,resting=pending.pop(0)
            equity=cash+position*f['mark'];peak=max(peak,equity);drawdown=max(drawdown,peak-equity);inventory_sum+=abs(position);max_position=max(max_position,abs(position))
            if equity<=-profile['limits']['loss_ticks']:halted=True;resting=[];pending=[]
            if not halted and f['unix']-last_quote>=6 and not pending:
                book=[o for o in f['orders'] if o['seat']!=profile['actor_seat']]
                bid=max((o['price'] for o in book if o['side']==0),default=0);ask=min((o['price'] for o in book if o['side']==1),default=0)
                d=engine.decide(features(frames[:index+1],position),f['mark'],bid,ask,now=index+2);kernels.append(d['kernel_ns'])
                quotes=[]
                for q in d['quotes']:
                    ahead=sum(o['lots'] for o in book if o['side']==q['side'] and (o['price']>=q['price'] if q['side']==0 else o['price']<=q['price']))
                    quotes.append(dict(q,ahead=math.ceil(ahead*queue_multiplier)))
                pending.append((index+delay,quotes));updates+=1;last_quote=f['unix']
            trace.append([f['slot'],position,equity])
    # Mark all residual inventory down to a stressed immediate exit plus fee.
    exit_price=frames[-1]['mark']-32 if position>0 else frames[-1]['mark']+32
    exit_fee=math.ceil(abs(position)*exit_price*5/10000);cash+=position*exit_price-exit_fee
    pnl=cash;drawdown=max(drawdown,peak-pnl);mean_inventory=inventory_sum/len(frames)
    w=profile['weights'];score=w['pnl']*pnl-w['drawdown']*drawdown-w['inventory']*mean_inventory-w['adverse']*adverse-w['churn']*updates
    return {'pnl_ticks':pnl,'drawdown_ticks':drawdown,'mean_abs_inventory':round(mean_inventory,6),'max_position':max_position,
        'adverse_ticks':adverse,'filled_lots_estimate':fills,'turnover_ticks_estimate':turnover,'quote_updates':updates,'exit_fee_ticks':exit_fee,
        'loss_stop':halted,'score':round(score,6),'trace_hash':digest(trace),'trace':trace,'kernel_ns':kernels}

def compare(parent,candidate,profile,frames,receipts):
    data=manifest(frames,receipts);results=[]
    for delay,queue in ((1,1),(3,2)):
        p=replay(parent,profile,frames,receipts,delay,queue);c=replay(candidate,profile,frames,receipts,delay,queue)
        for report in (p,c):report.pop('trace');report.pop('kernel_ns')
        results.append({'delay_frames':delay,'queue_multiplier':queue,'parent':p,'candidate':c,'score_delta':round(c['score']-p['score'],6)})
    # No promotion for no-fill idling, loss-limit breach or one lucky scenario.
    accepted=all(r['score_delta']>0 and r['candidate']['filled_lots_estimate']>=2 and not r['candidate']['loss_stop'] and r['candidate']['drawdown_ticks']<=profile['limits']['loss_ticks'] for r in results)
    seat=profile['actor_seat'];a,b=frames[0],frames[-1]
    actual={'seat':seat,'equity_change_tokens':b['equities'][seat]-a['equities'][seat],
        'ending_position':b['positions'][seat],'meaning':'observed existing v6 actor, not candidate performance'}
    return {'window':data,'scenarios':results,'research_champion_eligible':accepted,'live_promotion_eligible':False,'observed_actor':actual}
