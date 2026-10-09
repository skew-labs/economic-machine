#![no_std]
#![allow(unexpected_cfgs)]
mod state;
mod book;
mod oracle;
mod resolution;
use state::*;
use pinocchio::{AccountView, Address, ProgramResult, program_entrypoint, no_allocator, nostd_panic_handler};
use pinocchio::sysvars::{clock::Clock, Sysvar};
use pinocchio::cpi::{Seed, Signer, set_return_data};
use pinocchio_token::{instructions::Transfer, ID as TOKEN};
program_entrypoint!(process, 8);
no_allocator!();
nostd_panic_handler!();

// ABI v1 devnet laboratory: all hot commands have two accounts, market+signer.
// This deliberately pins capacities and trust model; it is not a public-money venue.
fn token(account: &AccountView, mint: &[u8], owner: &[u8]) -> R<u64> {
    require(account.owner()==&TOKEN && account.is_writable(),30)?;
    let d=account.try_borrow()?;
    require(d.len()==165 && &d[..32]==mint && &d[32..64]==owner && d[108]==1,30)?;
    require(u32at(&d,72)==0 && u32at(&d,129)==0,30)?; // no delegate / close authority
    Ok(u64at(&d,64))
}
fn process(id: &Address, accounts: &mut [AccountView], ix: &[u8]) -> ProgramResult {
    require(accounts.len()>=2 && ix.len()>=48 && ix[0]==1 && u16at(ix,2)==0,1)?;
    let count=accounts.len(); let (market, rest)=accounts.split_first_mut().ok_or(err(1))?; let signer=&rest[0];
    require(market.owner()==id && market.is_writable() && signer.is_signer() && market.address()!=signer.address(),2)?;
    let market_key=market.address().clone(); let market_signer=market.is_signer();
    let mut data=market.try_borrow_mut()?; let d=&mut data[..];
    require(d.len()==SIZE && ix.len()==48+u16at(ix,6) as usize,3)?;
    let op=ix[1]; let s=u16at(ix,4) as usize; let payload=&ix[48..];
    let clock=Clock::get()?; let slot=clock.slot;
    if op==0 {
        require(count==6 && payload.len()==9 && market_signer && &d[..8]==[0;8],4)?;
        require(u64at(payload,1)>0 && u64at(payload,1)<=1_000_000,4)?;
        let mint=&rest[1]; let vault=&rest[2]; let authority=&rest[3];
        require(rest[4].address()==&TOKEN && rest[4].executable(),30)?;
        require(mint.owner()==&TOKEN,30)?;
        let m=mint.try_borrow()?;
        require(m.len()==82 && m[44]==6 && m[45]==1,30)?;
        // Devnet-only mint authority may exist. No freeze authority is accepted.
        require(u32at(&m,46)==0,30)?;
        let bump=[payload[0]];
        let pda=Address::create_program_address(&[b"vault",(&market_key).as_ref(),&bump],id).map_err(|_|err(31))?;
        require(authority.address()==&pda && vault.address()!=mint.address(),31)?;
        require(token(vault,mint.address().as_ref(),pda.as_ref())?==0,30)?;
        d[..8].copy_from_slice(MAGIC); d[8..40].copy_from_slice(signer.address().as_ref());
        d[40..72].copy_from_slice(mint.address().as_ref()); d[72..104].copy_from_slice(vault.address().as_ref());
        d[104..136].copy_from_slice(pda.as_ref()); d[136]=payload[0];
        w64(d,168,u64at(payload,1)); w64(d,184,slot); return Ok(());
    }
    require(&d[..8]==MAGIC && s<SEATS,5)?;
    require(slot<=u64at(ix,24),6)?;
    require(u64at(ix,32)==1 && u64at(ix,40)<=u64at(d,160),7)?;
    let a=seat(s);
    if op==15 {
        require(count==3 && payload.is_empty() && d[216]==0 && signer.address().as_ref()==&d[8..40],55)?;
        require(u64at(d,200)==u64at(d,208),55)?;
        for j in 0..SEATS { require(i64at(d,seat(j)+120)==0,55)?; }
        book::clear(d);
        d[216]=1; oracle::update(d,&rest[1],&clock)?; return bump_market(d);
    }
    if op==16 { require(count==3 && payload.is_empty() && d[216]==1,55)?; oracle::update(d,&rest[1],&clock)?; return bump_market(d); }
    if op==17 { require(count==2 && payload.is_empty() && d[216]==1,55)?; oracle::fund(d,&clock)?; return bump_market(d); }
    if d[216]!=0 && matches!(op,4|5|6|10|11|12|14|19) { oracle::fresh_time(d,clock.unix_timestamp)?; }
    if op==18 {
        require(count==5 && payload.len()==8 && d[217]==0,61)?;
        let amount=u64at(payload,0);require(amount>0,45)?;
        let vault=&rest[1];let source=&rest[2];
        require(vault.address().as_ref()==&d[72..104] && source.address()!=vault.address()
            && rest[3].address()==&TOKEN && rest[3].executable(),30)?;
        let before=token(vault,&d[40..72],&d[104..136])?;token(source,&d[40..72],signer.address().as_ref())?;
        Transfer::new(source,vault,signer,amount).invoke()?;
        require(token(vault,&d[40..72],&d[104..136])?==before.checked_add(amount).ok_or(err(14))?,30)?;
        w64(d,336,u64at(d,336).checked_add(amount).ok_or(err(14))?);
        w64(d,344,u64at(d,344).checked_add(amount).ok_or(err(14))?);return bump_market(d);
    }
    if op==19 {require(count==2 && payload.is_empty(),3)?;resolution::resolve(d,slot)?;return bump_market(d);}
    // Oracle fixture is explicitly controlled by the market creator. It cannot
    // mint collateral, edit seats or bypass collateral/margin equations.
    if op==1 {
        require(d[216]==0,55)?;
        require(count==2 && payload.len()==12 && signer.address().as_ref()==&d[8..40],8)?;
        let price=u64at(payload,0); let confidence=u32at(payload,8);
        require(price>0 && price<=MAX_PRICE as u64 && confidence<=100,10)?;
        w64(d,144,price); w64(d,152,slot); return bump_market(d);
    }
    if op==2 {
        require(count==2 && payload.len()==8 && d[a+168]==0 && d[217]==0,9)?;
        let max=u64at(payload,0); require(max>0 && max<=MAX_Q,11)?;
        d[a..a+32].copy_from_slice(signer.address().as_ref());
        d[a+32..a+64].copy_from_slice(signer.address().as_ref());
        w64(d,a+64,1); w64(d,a+152,u64::MAX); w64(d,a+160,max); d[a+168]=1;
        wi64(d,a+144,i64at(d,176)); w16(d,138,u16at(d,138)+1); return bump_market(d);
    }
    // Anyone may execute a bounded, all-seat cash settlement, including during
    // resolution. It succeeds only if fee insurance can fund every negative seat.
    if op==10 { require(count==2 && payload.is_empty(),3)?; fresh(d,slot)?; cash_settle(d)?; return bump_market(d); }
    if op==11 {
        require(d[216]==0,55)?;
        require(count==2 && payload.len()==8 && signer.address().as_ref()==&d[8..40],8)?;
        fresh(d,slot)?; require(slot>=u64at(d,184)+1000,40)?;
        let delta=i64at(payload,0);
        require(delta.unsigned_abs()<=u64at(d,144)*u64at(d,168)/1000,40)?;
        wi64(d,176,i64at(d,176).checked_add(delta).ok_or(err(14))?); w64(d,184,slot); return bump_market(d);
    }
    if op==14 {
        require(count==2 && payload.is_empty() && d[a+168]==1,3)?; fresh(d,slot)?;
        require(equity(d,s)<0,41)?; d[137]=1; return bump_market(d);
    }
    require(d[a+168]==1,9)?;
    let owner=signer.address().as_ref()==&d[a..a+32];
    let session=signer.address().as_ref()==&d[a+32..a+64] && slot<=u64at(d,a+152);
    require(owner||session,8)?;
    if !owner {
        require(matches!(op,5|6|7),8)?;
        if d[a+169]!=0 {require(u64at(d,a+176)&(1u64<<op)!=0,59)?;}
    }
    require(u64at(ix,8)==u64at(d,a+64),42)?;
    let seq=u64at(ix,16); require(seq>0,43)?;
    let hash=solana_sha256_hasher::hashv(&[b"MPERPS-CMD-v1",id.as_ref(),(&market_key).as_ref(),ix]).to_bytes();
    let prior=u64at(d,a+72);
    if seq==prior { require(d[a+80..a+112]==hash,44)?; return Ok(()); }
    require(seq>prior,43)?;
    match op {
        3|4 => {
            require(owner && count==6 && payload.len()==8,8)?;
            let amount=u64at(payload,0); require(amount>0,45)?;
            let vault=&rest[1]; let user_token=&rest[2]; let authority=&rest[3];
            require(vault.address().as_ref()==&d[72..104] && authority.address().as_ref()==&d[104..136]
                && rest[4].address()==&TOKEN && rest[4].executable() && vault.address()!=user_token.address(),30)?;
            let before=token(vault,&d[40..72],&d[104..136])?;
            token(user_token,&d[40..72],&d[a..a+32])?;
            if op==3 {
                require(d[137]==0,46)?;
                Transfer::new(user_token,vault,signer,amount).invoke()?;
                require(token(vault,&d[40..72],&d[104..136])?==before.checked_add(amount).ok_or(err(14))?,30)?;
                w64(d,a+112,u64at(d,a+112).checked_add(amount).ok_or(err(14))?);
                w64(d,200,u64at(d,200).checked_add(amount).ok_or(err(14))?);
            } else {
                fresh(d,slot)?; require(d[137]==0 || d[217]==1,46)?;
                w64(d,a+112,u64at(d,a+112).checked_sub(amount).ok_or(err(12))?); risk(d,s)?;
                let bump=[d[136]]; let seeds=[Seed::from(b"vault"),Seed::from((&market_key).as_ref()),Seed::from(&bump)];
                Transfer::new(vault,user_token,authority,amount).invoke_signed(&[Signer::from(&seeds)])?;
                require(token(vault,&d[40..72],&d[104..136])?==before.checked_sub(amount).ok_or(err(12))?,30)?;
                w64(d,208,u64at(d,208).checked_add(amount).ok_or(err(14))?);
            }
        }
        5 => {
            require(count==2 && payload.len()>=8 && payload.len()==8+payload[0] as usize*32 && payload[0]<=16
                && payload[1]<=3,3)?;
            let mut deadline_bytes=[0u8;8];deadline_bytes[..6].copy_from_slice(&payload[2..8]);
            let policy_until=u64::from_le_bytes(deadline_bytes);
            if payload[1]&1==0 {require(policy_until==0,3)?;}
            else {require(clock.unix_timestamp>=0 && policy_until>clock.unix_timestamp as u64 && policy_until<=clock.unix_timestamp as u64+30,60)?;}
            if !owner && d[216]!=0 {require(payload[1]&1==1,60)?;}
            fresh(d,slot)?; require(d[137]==0,46)?;
            w64(d,a+200,0); // This full-seat frame rebuilds geometry once after all edits.
            book::clean_top(d,0,slot,clock.unix_timestamp);book::clean_top(d,1,slot,clock.unix_timestamp);
            let mut present=0u16;
            // First validate all entries and cancel omitted/repriced/increased
            // slots, allowing a whole two-sided quote frame to move atomically.
            for e in payload[8..].chunks_exact(32) {
                let k=e[0] as usize; require(k<16 && e[1]<=1 && u16at(e,2)==0 && present&(1<<k)==0,47)?;
                present|=1<<k; let p=u32at(e,4); let x=u64at(e,8); let expiry=u64at(e,16);
                require(p>0 && p<=MAX_PRICE && x>0 && x<=MAX_Q && expiry>=slot && expiry<=slot+150,20)?;
                if !owner && d[a+169]!=0 { require(x<=u64at(d,a+184) && expiry<=u64at(d,a+152),59)?; }
                require(p as u64*100>=u64at(d,144)*90 && p as u64*100<=u64at(d,144)*110,48)?;
                if e[1]!=0 {require(book::reduce_cap(d,s,k/8,x)==x,49)?;}
                let n=s*16+k; let o=node(n);
                if u64at(d,o)>0 && !(u32at(d,o+16)==p && x<=u64at(d,o) && expiry<=u64at(d,o+8) && d[o+32]==e[1]
                    && (u64at(d,o+40)==0 || (policy_until!=0 && policy_until<=u64at(d,o+40)))) {book::cancel(d,n);}
            }
            for k in 0..16 {if present&(1<<k)==0 {book::cancel(d,s*16+k);}}
            for e in payload[8..].chunks_exact(32) {book::post(d,s*16+e[0] as usize,u32at(e,4),u64at(e,8),u64at(e,16),u64at(e,24),e[1]!=0,policy_until,payload[1]&2!=0)?;}
            risk(d,s)?;
        }
        6 => {
            require(count==2,3)?; fresh(d,slot)?; require(d[137]==0,46)?;
            require(payload.len()==40 && payload[3]<=1,23)?;
            if payload[3]==1 {let until=u64at(payload,32);require(clock.unix_timestamp>=0 && until>clock.unix_timestamp as u64 && until<=clock.unix_timestamp as u64+30,60)?;}
            if !owner && d[216]!=0 {require(payload[3]==1,60)?;}
            if !owner && d[a+169]!=0 {require(payload.len()==40 && u64at(payload,8)<=u64at(d,a+184),59)?;}
            let (filled,turnover)=book::take(d,s,payload,slot,clock.unix_timestamp)?;
            let mut result=[0u8;16]; w64(&mut result,0,filled); w64(&mut result,8,turnover); set_return_data(&result);
        }
        7 => {
            require(count==2 && payload.len()==8 && payload[2..]==[0;6],3)?;
            let mask=u16at(payload,0); for k in 0..16 {if mask&(1<<k)!=0 {book::cancel(d,s*16+k);}}
        }
        8 => {
            require(owner && count==2 && matches!(payload.len(),40|64) && u64at(payload,32)>=slot && u64at(payload,32)<=slot+216_000,8)?;
            let revoke=payload[..32]==d[a..a+32];
            require(d[216]==0 || revoke || payload.len()==64,59)?;
            d[a+169]=0; w64(d,a+176,0); w64(d,a+184,0); w64(d,a+192,0);
            if payload.len()==64 && !revoke {
                let mask=u64at(payload,40); let clip=u64at(payload,48); let budget=u64at(payload,56);
                require(mask!=0 && mask&!((1<<5)|(1<<6)|(1<<7))==0 && clip>0 && clip<=MAX_Q && budget>0 && budget<=1_000_000_000_000,59)?;
                d[a+169]=1; w64(d,a+176,mask); w64(d,a+184,clip); w64(d,a+192,budget);
            }
            book::cancel_seat(d,s); d[a+32..a+64].copy_from_slice(&payload[..32]);
            w64(d,a+152,u64at(payload,32)); w64(d,a+64,u64at(d,a+64).checked_add(1).ok_or(err(14))?);
            w64(d,a+72,0); d[a+80..a+112].fill(0); return bump_market(d);
        }
        12 => liquidate(d,s,payload,slot)?,
        _ => return Err(err(50)),
    }
    w64(d,a+72,seq); d[a+80..a+112].copy_from_slice(&hash); bump_market(d)
}
fn bump_market(d: &mut [u8]) -> ProgramResult {w64(d,160,u64at(d,160).checked_add(1).ok_or(err(14))?);Ok(())}
fn cash_settle(d: &mut [u8]) -> ProgramResult {
    let mut deficit=0u64; let mut total_q=0i128; let mut total_c=0i128;
    for s in 0..SEATS {
        if d[seat(s)+168]==0 {continue;}
        let e=equity(d,s); if e<0 {deficit=deficit.checked_add(u64::try_from(-e).map_err(|_|err(14))?).ok_or(err(14))?;}
        // Cover only crystallized debt. Forgiving negative equity while leaving
        // its position open gives an insolvent owner a free rebound option.
        require(e>=0 || i64at(d,seat(s)+120)==0,52)?;
        total_q+=i64at(d,seat(s)+120) as i128; total_c+=funding_quote(d,s);
        require(e<=u64::MAX as i128,14)?;
    }
    require(total_q==0 && total_c==0,51)?;
    require(resolution::absorb(d,deficit)?==0,52)?;
    let mut drop=[0u64;4];let mut count=0usize;let mut have_survivors=false;
    for s in 0..SEATS {
        let a=seat(s); if d[a+168]==0 {continue;}
        let e=equity(d,s).max(0) as u64;
        w64(d,a+112,e); wi128(d,a+128,-(i64at(d,a+120) as i128*u64at(d,144) as i128*u64at(d,168) as i128));
        wi64(d,a+144,i64at(d,176));
        if risk(d,s).is_err() {drop[s/64]|=1u64<<(s%64);count+=1;}
        else {have_survivors|=u32at(d,a+208)!=0 || u32at(d,a+212)!=0;}
    }
    book::prune(d,&drop,count,have_survivors);
    // Resolution halt is intentionally sticky: there is no administrative escape
    // that lets insolvent positions or a depleted insurance pool resume trading.
    Ok(())
}
fn liquidate(d: &mut [u8], liquidator: usize, p: &[u8], slot: u64) -> ProgramResult {
    require(p.len()==16 && p[2..8]==[0;6],3)?; fresh(d,slot)?;
    let victim=u16at(p,0) as usize;
    require(victim<SEATS && victim!=liquidator && d[seat(victim)+168]==1,53)?;
    let q=i64at(d,seat(victim)+120); let x=u64at(p,8); let mark=u64at(d,144);
    require(x>0 && x<=q.unsigned_abs() && equity(d,victim)<(q.unsigned_abs() as i128*mark as i128*u64at(d,168) as i128+19)/20,54)?;
    book::cancel_seat(d,victim);
    let (buyer,seller)=if q>0 {(liquidator,victim)} else {(victim,liquidator)};
    trade(d,buyer,seller,x,mark as u32)?;
    // No penalty can create additional negative equity.
    let reward=((x as u128*mark as u128*u64at(d,168) as u128)/1000) as u64;
    let paid=reward.min(u64at(d,seat(victim)+112)).min(equity(d,victim).max(0) as u64);
    w64(d,seat(victim)+112,u64at(d,seat(victim)+112)-paid);
    w64(d,seat(liquidator)+112,u64at(d,seat(liquidator)+112).checked_add(paid).ok_or(err(14))?);
    risk(d,liquidator)?; Ok(())
}
