// Bounded market waterfall; constant stack regardless of seat capacity.
use crate::{state::*,book};

pub fn absorb(d:&mut [u8],deficit:u64)->R<u64> {
    let insurance=deficit.min(u64at(d,336));
    w64(d,336,u64at(d,336)-insurance);
    w64(d,352,u64at(d,352).checked_add(insurance).ok_or(err(14))?);
    let rest=deficit-insurance;let fees=rest.min(u64at(d,192));
    w64(d,192,u64at(d,192)-fees);Ok(rest-fees)
}

// Insolvency closes every position at one fresh oracle mark. Only positive
// unsettled PnL absorbs the uncovered loss, pro rata with deterministic rounding.
// This is terminal market-wide ADL, not a ranked pairwise liquidation queue.
pub fn resolve(d:&mut [u8],slot:u64)->R<()> {
    require(d[217]==0,61)?;fresh(d,slot)?;
    let (mut deficit,mut profit,mut q,mut c)=(0u64,0u128,0i128,0i128);
    for s in 0..SEATS {
        let a=seat(s);if d[a+168]==0 {continue;}
        let e=equity(d,s);require(e<=u64::MAX as i128,14)?;
        if e<0 {deficit=deficit.checked_add(u64::try_from(-e).map_err(|_|err(14))?).ok_or(err(14))?;}
        let gain=u64::try_from((e-u64at(d,a+112) as i128).max(0)).map_err(|_|err(14))?;
        profit+=gain as u128;q+=i64at(d,a+120) as i128;c+=funding_quote(d,s);
    }
    require(q==0 && c==0,51)?;
    require(deficit as u128>u64at(d,336) as u128+u64at(d,192) as u128,62)?;
    let loss=absorb(d,deficit)?;require(profit>=loss as u128 && profit>0,62)?;
    let mut assigned=0u64;
    for s in 0..SEATS {
        let a=seat(s);if d[a+168]==0 {continue;}
        let gain=(equity(d,s)-u64at(d,a+112) as i128).max(0) as u128;
        assigned+=(gain*loss as u128/profit) as u64;
    }
    let mut remainder=loss-assigned;
    // Floor rounding leaves strictly fewer atoms than positive recipients.
    for s in 0..SEATS {
        let a=seat(s);if d[a+168]==0 {continue;}
        let equity=equity(d,s);let gain=(equity-u64at(d,a+112) as i128).max(0) as u128;
        let mut cut=(gain*loss as u128/profit) as u64;
        if remainder>0 && (cut as u128)<gain {cut+=1;remainder-=1;}
        let e=u64::try_from(equity.max(0)).map_err(|_|err(14))?;
        w64(d,a+112,e.checked_sub(cut).ok_or(err(51))?);
        wi64(d,a+120,0);wi128(d,a+128,0);wi64(d,a+144,i64at(d,176));
        let mut owner=[0u8;32];owner.copy_from_slice(&d[a..a+32]);d[a+32..a+64].copy_from_slice(&owner);
        d[a+169]=0;w64(d,a+176,0);w64(d,a+184,0);w64(d,a+192,0);
        w64(d,a+64,u64at(d,a+64).checked_add(1).ok_or(err(14))?);w64(d,a+72,0);d[a+80..a+112].fill(0);
    }
    require(remainder==0,51)?;
    book::clear(d);
    w64(d,360,u64at(d,360).checked_add(loss).ok_or(err(14))?);
    w64(d,368,u64at(d,368).checked_add(1).ok_or(err(14))?);
    w64(d,376,u64at(d,144));w64(d,384,slot);wi64(d,392,i64at(d,176));
    d[137]=1;d[217]=1;Ok(())
}
