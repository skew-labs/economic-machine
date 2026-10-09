// Pinned Pyth SOL/USD PriceUpdateV2. Full verification only. No Anchor decoder,
// allocations, CPI, caller-selected feed or administrative price override.
use crate::{state::*,book};
use pinocchio::{AccountView,Address};
use pinocchio::sysvars::clock::Clock;
const RECEIVER: Address=Address::new_from_array([12,183,250,122,93,166,40,251,172,169,154,234,153,247,191,59,220,54,137,104,96,42,191,65,77,78,139,165,103,187,176,191]);
const FEED_ACCOUNT: Address=Address::new_from_array([91,177,21,176,13,210,8,234,172,159,42,107,237,88,182,134,200,125,220,245,46,109,173,241,88,112,34,67,50,22,158,205]);
const FEED: [u8;32]=[239,13,139,111,218,44,235,164,29,161,93,64,149,209,218,57,42,13,47,142,208,198,199,188,15,76,250,200,194,128,181,109];
const DISC:[u8;8]=[34,241,35,99,157,126,244,205];
pub fn fresh_time(d:&[u8],now:i64)->R<()> {
    let publish=i64at(d,224);require(publish>0 && now>=publish && now-publish<=90,56)
}
fn premium(d:&[u8],slot:u64,now:i64,mark:u64)->i64 {
    if let (Some(b),Some(a))=(book::best(d,0),book::best(d,1)) {
        let bo=node(b);let ao=node(a);let bid=u32at(d,bo+16) as u64;let ask=u32at(d,ao+16) as u64;
        if u64at(d,bo+8)>=slot && u64at(d,ao+8)>=slot && bid<ask && ask-bid<=mark/100
            && (u64at(d,bo+40)==0 || now<(u64at(d,bo+40) as i64)) && (u64at(d,ao+40)==0 || now<(u64at(d,ao+40) as i64)) {
            let cap=(mark/1000) as i64;return (((bid+ask)/2) as i64-mark as i64).clamp(-cap,cap);
        }
    } 0
}
pub fn update(d:&mut [u8],account:&AccountView,clock:&Clock)->R<()> {
    require(account.address()==&FEED_ACCOUNT && account.owner()==&RECEIVER,55)?;
    let p=account.try_borrow()?;
    require(p.len()==134 && p[..8]==DISC && p[40]==1 && p[41..73]==FEED,55)?;
    let raw=i64at(&p,73);let confidence=u64at(&p,81);let exponent=i32::from_le_bytes(p[89..93].try_into().unwrap());
    let publish=i64at(&p,93);let posted=u64at(&p,125);
    require(raw>0 && exponent== -8 && confidence as u128*10_000<=raw as u128*100,57)?;
    require(posted<=clock.slot && publish>0 && publish>=i64at(d,224) && publish<=clock.unix_timestamp && clock.unix_timestamp-publish<=90,56)?;
    // SOL/USD cents; one quote atom per tick/lot in the development market.
    let mark=raw as u64/1_000_000;require(mark>0 && mark<=MAX_PRICE as u64,57)?;
    let now=clock.unix_timestamp;let previous=i64at(d,232);
    require(previous<=now,56)?;
    let elapsed=now-previous;
    if previous==0 || elapsed>30 {
        wi128(d,240,0);w64(d,256,0);wi64(d,264,now);w64(d,288,0);
    } else if elapsed>0 {
        let area=i128at(d,240).checked_add(i64at(d,280) as i128*elapsed as i128).ok_or(err(14))?;
        wi128(d,240,area);w64(d,256,u64at(d,256).checked_add(elapsed as u64).ok_or(err(14))?);
        w64(d,288,u64at(d,288).checked_add(1).ok_or(err(14))?);
    }
    wi64(d,232,now);wi64(d,224,publish);wi64(d,280,premium(d,clock.slot,now,mark));
    w64(d,296,confidence);w64(d,144,mark);w64(d,152,clock.slot);Ok(())
}
pub fn fund(d:&mut [u8],clock:&Clock)->R<()> {
    fresh_time(d,clock.unix_timestamp)?;fresh(d,clock.slot)?;
    require(clock.unix_timestamp>=i64at(d,264)+300 && u64at(d,256)>=240 && u64at(d,288)>=8,58)?;
    // Eight-hour premium accrual, sampled at <=30-second intervals. Remainder
    // carries fractional quote atoms so tiny positions do not force rounding up.
    let elapsed=clock.unix_timestamp-i64at(d,232);require((0..=30).contains(&elapsed),58)?;
    let sampled=i128at(d,240).checked_add(i64at(d,280) as i128*elapsed as i128).ok_or(err(14))?;
    let area=sampled.checked_mul(u64at(d,168) as i128).ok_or(err(14))?.checked_add(i128at(d,304)).ok_or(err(14))?;
    let delta=i64::try_from(area/28_800).map_err(|_|err(14))?;
    require(delta.unsigned_abs()<=u64at(d,144)*u64at(d,168)/1000,58)?;
    wi64(d,176,i64at(d,176).checked_add(delta).ok_or(err(14))?);w64(d,184,clock.slot);
    wi128(d,304,area%28_800);wi128(d,240,0);w64(d,256,0);w64(d,288,0);
    wi64(d,264,clock.unix_timestamp);wi64(d,272,clock.unix_timestamp);wi64(d,320,delta);
    wi64(d,232,clock.unix_timestamp);wi64(d,280,premium(d,clock.slot,clock.unix_timestamp,u64at(d,144)));
    w64(d,328,u64at(d,328).checked_add(1).ok_or(err(14))?);Ok(())
}
