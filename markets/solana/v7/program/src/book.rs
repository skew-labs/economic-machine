use crate::state::*;
// Encoded indices are index+1: newly allocated Solana bytes are already empty.
#[inline(always)] fn bit(d: &mut [u8], o: usize, n: usize, on: bool) {
    let a=o+(n/64)*8; let m=1u64<<(n%64); let v=u64at(d,a);
    w64(d,a,if on { v|m } else { v&!m });
}
#[inline(always)] fn any(d: &[u8], o: usize) -> bool { (u64at(d,o)|u64at(d,o+8)|u64at(d,o+16)|u64at(d,o+24))!=0 }
#[inline(always)] fn edge(d: &[u8], o: usize, high: bool) -> Option<usize> {
    for i in 0..4 { let w=if high {3-i} else {i}; let v=u64at(d,o+w*8);
        if v!=0 { return Some(w*64+if high {63-v.leading_zeros() as usize} else {v.trailing_zeros() as usize}); }
    } None
}
fn bitmap(d: &mut [u8], price: u32, s: usize, p: usize, on: bool) {
    let prefix=price as usize>>8; let high=prefix>>8; let low=prefix&255;
    let leaf=page(p)+8+s*32; bit(d,leaf,price as usize&255,on);
    let mid=MID+s*8192+high*32;
    bit(d,mid,low,any(d,leaf)); bit(d,ROOT+s*32,high,any(d,mid));
}
#[inline(always)] fn head(p: usize, s: usize, tick: usize) -> usize { page(p)+72+s*1024+tick*2 }
#[inline(always)] fn tail(p: usize, s: usize, tick: usize) -> usize { head(p,s,tick)+512 }
pub fn best(d: &[u8], s: usize) -> Option<usize> {
    let high=edge(d,ROOT+s*32,s==0)?;
    let low=edge(d,MID+s*8192+high*32,s==0)?;
    let p=u16at(d,DIR+((high<<8)|low)*2) as usize-1;
    let tick=edge(d,page(p)+8+s*32,s==0)?;
    Some(u16at(d,head(p,s,tick)) as usize-1)
}
pub fn cancel(d: &mut [u8], n: usize) {
    let o=node(n); if u64at(d,o)==0 {return;}
    RiskState::cancel_cached(d,n/16,side(n),u32at(d,o+16) as u64,u64at(d,o));
    unlink(d,n);
}
fn unlink(d:&mut [u8],n:usize) {
    let o=node(n);
    let price=u32at(d,o+16); let prefix=price as usize>>8; let tick=price as usize&255;
    let p=u16at(d,DIR+prefix*2) as usize-1; let s=side(n);
    let prev=u16at(d,o+20); let next=u16at(d,o+22);
    if prev!=0 { w16(d,node(prev as usize-1)+22,next); } else { w16(d,head(p,s,tick),next); }
    if next!=0 { w16(d,node(next as usize-1)+20,prev); } else { w16(d,tail(p,s,tick),prev); }
    w64(d,o,0);
    if u16at(d,head(p,s,tick))==0 { bitmap(d,price,s,p,false); }
    let count=u32at(d,page(p)+4)-1; w32(d,page(p)+4,count);
    if count==0 { w16(d,DIR+prefix*2,0); }
}
pub fn post(d: &mut [u8], n: usize, mut price: u32, qty: u64, expiry: u64, client: u64, reduce: bool, policy_until:u64, slide:bool) -> R<()> {
    require(price>0 && price<=MAX_PRICE && qty>0 && qty<=MAX_Q,20)?;
    let s=side(n);
    if let Some(opposite)=best(d,1-s) {
        let p=u32at(d,node(opposite)+16);
        if if s==0 {price>=p} else {price<=p} {
            require(slide,21)?;
            let adjusted=if s==0 {p-1} else {p+1};
            // Only move away from crossing, within 32 ticks of the signed limit.
            // The limit never becomes more aggressive; oracle admission remains.
            require(price.abs_diff(adjusted)<=32 && adjusted>0 && adjusted<=MAX_PRICE,21)?;
            let mark=u64at(d,144);
            require(adjusted as u64*10>=mark*9 && adjusted as u64*10<=mark*11,48)?;
            price=adjusted;
        }
    }
    let o=node(n);
    // A same-price reduction retains FIFO priority. Extending expiry or changing
    // reduce-only semantics relinquishes it; no mutable generation tombstones.
    if u64at(d,o)>0 && u32at(d,o+16)==price && qty<=u64at(d,o)
        && expiry<=u64at(d,o+8) && d[o+32]==reduce as u8
        && (u64at(d,o+40)==0 || (policy_until!=0 && policy_until<=u64at(d,o+40))) {
        w64(d,seat(n/16)+200,0);
        w64(d,o,qty); w64(d,o+8,expiry); w64(d,o+24,client);w64(d,o+40,policy_until); return Ok(());
    }
    cancel(d,n);
    let prefix=price as usize>>8; let tick=price as usize&255;
    let mut p=u16at(d,DIR+prefix*2) as usize;
    if p==0 {
        p=(0..PAGES).find(|&j| u32at(d,page(j)+4)==0).ok_or(err(22))?+1;
        w16(d,DIR+prefix*2,p as u16); w32(d,page(p-1),prefix as u32);
    }
    p-=1;
    let old_tail=u16at(d,tail(p,s,tick));
    w64(d,seat(n/16)+200,0);
    w64(d,o,qty); w64(d,o+8,expiry); w32(d,o+16,price);
    w16(d,o+20,old_tail); w16(d,o+22,0); w64(d,o+24,client); d[o+32]=reduce as u8;
    w64(d,o+40,policy_until);
    if old_tail!=0 { w16(d,node(old_tail as usize-1)+22,(n+1) as u16); }
    else { w16(d,head(p,s,tick),(n+1) as u16); bitmap(d,price,s,p,true); }
    w16(d,tail(p,s,tick),(n+1) as u16); w32(d,page(p)+4,u32at(d,page(p)+4)+1);
    Ok(())
}
pub fn cancel_seat(d: &mut [u8], s: usize) {
    w64(d,seat(s)+200,0);
    for k in 0..16 {if u64at(d,node(s*16+k))!=0 {unlink(d,s*16+k);}}
}
// Only callers that revoke every resting order may bypass linked-list removal.
// One contiguous zero replaces O(total orders) pointer/bitmap updates.
pub fn clear(d:&mut [u8]) {
    d[NODE..SIZE].fill(0);
    for s in 0..SEATS {w64(d,seat(s)+200,0);}
}
// Bulk risk eviction: preserve the old linked-list traversal order, relink only
// survivors, then rebuild each page's bitmap once. No per-removed-order fee,
// bitmap or directory work. Fixed 32-byte selection mask covers 256 seats.
pub fn prune(d:&mut [u8],drop:&[u64;4],count:usize,have_survivors:bool) {
    if count==0 {return;}
    if !have_survivors {clear(d);return;}
    if count<=4 {
        for s in 0..SEATS {if drop[s/64]&(1u64<<(s%64))!=0 {cancel_seat(d,s);}}
        return;
    }
    for s in 0..SEATS {if drop[s/64]&(1u64<<(s%64))!=0 {
        w64(d,seat(s)+200,0);for k in 0..16 {w64(d,node(s*16+k),0);}
    }}
    for p in 0..PAGES {
        if u32at(d,page(p)+4)==0 {continue;}
        let prefix=u32at(d,page(p)) as usize;let mut kept=0u32;
        for side in 0..2 {
            let mut nonempty=false;
            for word in 0..4 {
                let offset=page(p)+8+side*32+word*8;let mut ticks=u64at(d,offset);let mut live=0u64;
                while ticks!=0 {
                    let low=ticks.trailing_zeros() as usize;let tick=word*64+low;ticks&=ticks-1;
                    let mut cursor=u16at(d,head(p,side,tick));let mut first=0u16;let mut last=0u16;
                    while cursor!=0 {
                        let o=node(cursor as usize-1);let next=u16at(d,o+22);
                        if u64at(d,o)!=0 {
                            w16(d,o+20,last);
                            if last!=0 {w16(d,node(last as usize-1)+22,cursor);} else {first=cursor;}
                            last=cursor;kept+=1;
                        }
                        cursor=next;
                    }
                    if last!=0 {w16(d,node(last as usize-1)+22,0);live|=1u64<<low;}
                    w16(d,head(p,side,tick),first);w16(d,tail(p,side,tick),last);
                }
                w64(d,offset,live);nonempty|=live!=0;
            }
            let high=prefix>>8;let mid=MID+side*8192+high*32;
            bit(d,mid,prefix&255,nonempty);bit(d,ROOT+side*32,high,any(d,mid));
        }
        w32(d,page(p)+4,kept);if kept==0 {w16(d,DIR+prefix*2,0);}
    }
}
pub fn clean_top(d:&mut [u8],side:usize,slot:u64,now:i64) {
    for _ in 0..16 {
        let Some(n)=best(d,side) else {break;};let o=node(n);
        if u64at(d,o+8)>=slot && (u64at(d,o+40)==0 || now<0 || (now as u64)<u64at(d,o+40)) {break;}
        cancel(d,n);
    }
}
pub fn reduce_cap(d: &[u8], s: usize, side: usize, qty: u64) -> u64 {
    let q=i64at(d,seat(s)+120);
    if (side==0 && q<0)||(side==1 && q>0) { qty.min(q.unsigned_abs()) } else {0}
}
// Both expired/self cleanup and executable makers count against one hard visit
// limit. A valid better price is never skipped to reach a more convenient maker.
pub fn take(d: &mut [u8], s: usize, payload: &[u8], slot: u64, now:i64) -> R<(u64,u64)> {
    require(payload.len()==40,23)?;
    let taker_side=payload[0] as usize; let reduce=payload[1]!=0; let visits=payload[2] as usize;
    require(taker_side<=1 && payload[1]<=1 && (1..=16).contains(&visits) && payload[3]<=1,23)?;
    let limit=u32at(payload,4); let max=u64at(payload,8); let min=u64at(payload,16); let budget=u64at(payload,24);
    require(limit>0 && limit<=MAX_PRICE && max>0 && max<=MAX_Q && min<=max,23)?;
    let (mut filled,mut turnover,mut paid_fee)=(0u64,0u64,0u64);
    let mut last_maker=usize::MAX;let mut maker_risk=RiskState::ZERO;let mut taker_settled=false;
    // Oracle-independent order geometry occupies the seat's spare 56 bytes.
    // Deletions update exact quantities/notionals/fees; new quotes rebuild it.
    // No stack array proportional to total market seat capacity.
    for _ in 0..visits {
        if filled==max {break;}
        let Some(n)=best(d,1-taker_side) else {break;}; let o=node(n); let maker=n/16;
        let p=u32at(d,o+16);
        if (taker_side==0 && p>limit)||(taker_side==1 && p<limit) {break;}
        if slot>u64at(d,o+8) || (u64at(d,o+40)>0 && now>=0 && now as u64>=u64at(d,o+40)) || maker==s {
            if last_maker!=usize::MAX {maker_risk.store(d,last_maker);last_maker=usize::MAX;}
            cancel(d,n);continue;
        }
        let mut x=u64at(d,o).min(max-filled);
        if d[o+32]!=0 { x=reduce_cap(d,maker,1-taker_side,x); }
        if x==0 {
            if last_maker!=usize::MAX {maker_risk.store(d,last_maker);last_maker=usize::MAX;}
            cancel(d,n);continue;
        }
        if reduce {x=reduce_cap(d,s,taker_side,x);}
        if x==0 {break;}
        let value=p as u64*u64at(d,168);
        x=x.min((budget-turnover)/value); if x==0 {break;}
        if d[seat(s)+169]!=0 {x=x.min(u64at(d,seat(s)+192)/value); if x==0 {break;}}
        if d[seat(maker)+169]!=0 {x=x.min(u64at(d,seat(maker)+192)/value); if x==0 {
            if last_maker!=usize::MAX {maker_risk.store(d,last_maker);last_maker=usize::MAX;}
            cancel(d,n);continue;
        }}
        // Maker whose margin deteriorated cannot be skipped silently; remove this
        // bounded node and stop. The next command may continue at the new best.
        if last_maker!=maker {
            if last_maker!=usize::MAX {maker_risk.store(d,last_maker);last_maker=usize::MAX;}
            maker_risk=RiskState::load(d,maker);
            if maker_risk.check(d,maker).is_err() {cancel(d,n);break;}
            settle_funding(d,maker);
            last_maker=maker;
        }
        let before=u64at(d,o);let remaining=before-x;
        if remaining==0 {unlink(d,n);} else {w64(d,o,remaining);}
        let (buyer,seller)=if taker_side==0 {(s,maker)} else {(maker,s)};
        if !taker_settled {settle_funding(d,s);taker_settled=true;}
        trade_settled(d,buyer,seller,x,p)?;
        let notional=x.checked_mul(value).ok_or(err(14))?;
        for actor in [s,maker] {let a=seat(actor); if d[a+169]!=0 {w64(d,a+192,u64at(d,a+192).checked_sub(notional).ok_or(err(59))?);}}
        // One ceiling per IOC, independent of order/counterparty fragmentation.
        // Quotient + remainder avoids overflow near a u64 turnover budget.
        let cumulative=turnover+notional;
        let total_fee=cumulative/2000+u64::from(cumulative%2000!=0);
        let fee=total_fee-paid_fee;paid_fee=total_fee;
        let a=seat(s); w64(d,a+112,u64at(d,a+112).checked_sub(fee).ok_or(err(12))?);
        w64(d,192,u64at(d,192).checked_add(fee).ok_or(err(14))?);
        maker_risk.filled(d,1-taker_side,p as u64,before,remaining);
        // Admission before the first fill is sufficient for the maker:
        // q moves inside the pre-admitted [q-asks,q+bids] interval; worst position
        // cannot grow. Removed order-loss reserve covers any equity decrease at
        // this unchanged mark, and makers pay no fee. See MAKER_FILL_INVARIANT.md.
        // Taker fees/exposure still require the final atomic risk check below.
        // Persist once at maker switch/cleanup/end. Never let cancel_cached read
        // pre-fill geometry; every such path flushes before cancellation above.
        turnover+=notional; filled+=x;
    }
    // The taker is the same seat throughout this atomic instruction. Final-state
    // admission is sufficient: any failure rolls all fills and fees back.
    if last_maker!=usize::MAX {maker_risk.store(d,last_maker);}
    require(filled>=min,24)?; risk(d,s)?; Ok((filled,turnover))
}
