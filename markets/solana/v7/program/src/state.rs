use pinocchio::error::ProgramError;
pub type R<T> = Result<T, ProgramError>;
pub fn err(n: u32) -> ProgramError { ProgramError::Custom(n) }
pub fn require(b: bool, n: u32) -> R<()> { if b { Ok(()) } else { Err(err(n)) } }
pub const SEATS: usize = if cfg!(feature="wide256") {256} else {16};
pub const SLOTS: usize = 16;
pub const PAGES: usize = if cfg!(feature="wide256") {64} else {16};
pub const SEAT: usize = 512;
pub const NODE: usize = SEAT + SEATS * 256;
pub const ROOT: usize = NODE + SEATS * SLOTS * 48;
pub const MID: usize = ROOT + 64;
pub const DIR: usize = MID + 16384;
pub const PAGE: usize = DIR + 131072;
pub const SIZE: usize = PAGE + PAGES * 2176;
pub const MAGIC: &[u8; 8] = if cfg!(feature="production") {b"MPERPS03"} else if cfg!(feature="wide256") {b"MPERPS02"} else {b"MPERPS01"};
// Header 352..399 is the resolution ledger; the immutable receiver account
// occupies previously unused bytes 400..431 only in the production layout.
pub const ORACLE_ACCOUNT: usize = 400;
pub const MAX_Q: u64 = 1_000_000;
pub const MAX_PRICE: u32 = 0xff_ffff;
#[inline(always)] pub fn u16at(d: &[u8], o: usize) -> u16 { u16::from_le_bytes(d[o..o+2].try_into().unwrap()) }
#[inline(always)] pub fn u32at(d: &[u8], o: usize) -> u32 { u32::from_le_bytes(d[o..o+4].try_into().unwrap()) }
#[inline(always)] pub fn u64at(d: &[u8], o: usize) -> u64 { u64::from_le_bytes(d[o..o+8].try_into().unwrap()) }
#[inline(always)] pub fn i64at(d: &[u8], o: usize) -> i64 { u64at(d,o) as i64 }
#[inline(always)] pub fn i128at(d: &[u8], o: usize) -> i128 { i128::from_le_bytes(d[o..o+16].try_into().unwrap()) }
#[inline(always)] pub fn w16(d: &mut [u8], o: usize, v: u16) { d[o..o+2].copy_from_slice(&v.to_le_bytes()); }
#[inline(always)] pub fn w32(d: &mut [u8], o: usize, v: u32) { d[o..o+4].copy_from_slice(&v.to_le_bytes()); }
#[inline(always)] pub fn w64(d: &mut [u8], o: usize, v: u64) { d[o..o+8].copy_from_slice(&v.to_le_bytes()); }
#[inline(always)] pub fn wi64(d: &mut [u8], o: usize, v: i64) { w64(d,o,v as u64); }
#[inline(always)] pub fn wi128(d: &mut [u8], o: usize, v: i128) { d[o..o+16].copy_from_slice(&v.to_le_bytes()); }
#[inline(always)] pub fn seat(s: usize) -> usize { SEAT + s * 256 }
#[inline(always)] pub fn node(n: usize) -> usize { NODE + n * 48 }
#[inline(always)] pub fn side(n: usize) -> usize { (n % SLOTS) / 8 }
#[inline(always)] pub fn page(p: usize) -> usize { PAGE + p * 2176 }
pub fn fresh(d: &[u8], slot: u64) -> R<()> {
    require(u64at(d,144)>0 && slot>=u64at(d,152) && slot-u64at(d,152)<=150, 10)
}
pub fn funding_quote(d: &[u8], s: usize) -> i128 {
    let a=seat(s);
    i128at(d,a+128) - i64at(d,a+120) as i128 * (i64at(d,176) as i128-i64at(d,a+144) as i128)
}
pub fn settle_funding(d: &mut [u8], s: usize) {
    let a=seat(s); let c=funding_quote(d,s); let f=i64at(d,176);
    wi128(d,a+128,c); wi64(d,a+144,f);
}
pub fn equity(d: &[u8], s: usize) -> i128 {
    let a=seat(s);
    u64at(d,a+112) as i128 + funding_quote(d,s) + i64at(d,a+120) as i128*u64at(d,144) as i128*u64at(d,168) as i128
}
// Positive unrealized profits do not collateralize new exposure. Every resting slot
// contributes worst execution loss across the configured oracle confidence band.
// Oracle-independent geometry fits exactly in the 56 spare seat bytes. Bounds
// may widen after deletion, but never narrow incorrectly; ambiguous bands scan
// only the affected side. Fee rounding is aggregated exactly per resting node.
pub const RISK_TAG:u64=0x32524d5041434801;
#[derive(Clone,Copy)]
pub struct RiskState {bids:u64,asks:u64,bn:u64,an:u64,fees:u64,minb:u32,maxb:u32,mina:u32,maxa:u32,loss:i128}
impl RiskState {
pub const ZERO:Self=Self{bids:0,asks:0,bn:0,an:0,fees:0,minb:MAX_PRICE,maxb:0,mina:MAX_PRICE,maxa:0,loss:0};
#[inline(always)] fn fee(d:&[u8],p:u64,x:u64)->u64 {(x*p*u64at(d,168)+1999)/2000}
fn geometry(d:&[u8],s:usize)->Self {
    let a=seat(s);
    if u64at(d,a+200)==RISK_TAG {
        return Self{bids:u32at(d,a+208) as u64,asks:u32at(d,a+212) as u64,bn:u64at(d,a+216),an:u64at(d,a+224),fees:u64at(d,a+232),minb:u32at(d,a+240),maxb:u32at(d,a+244),mina:u32at(d,a+248),maxa:u32at(d,a+252),loss:0};
    }
    let mut r=Self::ZERO;
    for k in 0..16 {
        let o=node(s*16+k);let x=u64at(d,o);if x==0 {continue;}
        let p=u32at(d,o+16);let notional=x*p as u64;
        if k<8 {r.bids+=x;r.bn+=notional;r.minb=r.minb.min(p);r.maxb=r.maxb.max(p);}
        else {r.asks+=x;r.an+=notional;r.mina=r.mina.min(p);r.maxa=r.maxa.max(p);}
        r.fees+=Self::fee(d,p as u64,x);
    }
    r
}
pub fn load(d:&[u8],s:usize)->Self {
    let mut r=Self::geometry(d,s);let mark=u64at(d,144);let lo=mark*95/100;let hi=(mark*105+99)/100;
    let buy=if r.bids==0 || lo>=r.maxb as u64 {0} else if lo<=r.minb as u64 {r.bn-r.bids*lo} else {
        let mut sum=0;for k in 0..8 {let o=node(s*16+k);sum+=u64at(d,o)*(u32at(d,o+16) as u64).saturating_sub(lo);}sum
    };
    let sell=if r.asks==0 || hi<=r.mina as u64 {0} else if hi>=r.maxa as u64 {r.asks*hi-r.an} else {
        let mut sum=0;for k in 8..16 {let o=node(s*16+k);sum+=u64at(d,o)*hi.saturating_sub(u32at(d,o+16) as u64);}sum
    };
    r.loss=(buy as i128+sell as i128)*u64at(d,168) as i128+r.fees as i128;r
}
#[inline(always)]
pub fn store(&self,d:&mut [u8],s:usize) {
    let a=seat(s);w64(d,a+200,RISK_TAG);w32(d,a+208,self.bids as u32);w32(d,a+212,self.asks as u32);
    w64(d,a+216,self.bn);w64(d,a+224,self.an);w64(d,a+232,self.fees);
    w32(d,a+240,self.minb);w32(d,a+244,self.maxb);w32(d,a+248,self.mina);w32(d,a+252,self.maxa);
}
pub fn cancel_cached(d:&mut [u8],s:usize,side:usize,p:u64,x:u64) {
    let a=seat(s);if u64at(d,a+200)!=RISK_TAG {return;}
    let q=a+208+side*4;let n=a+216+side*8;
    w32(d,q,u32at(d,q)-x as u32);w64(d,n,u64at(d,n)-x*p);
    w64(d,a+232,u64at(d,a+232)-Self::fee(d,p,x));
    // Leaving old extrema is conservative and keeps deletion O(1).
}
pub fn filled(&mut self,d:&[u8],side:usize,p:u64,before:u64,after:u64) {
    let delta=before-after;let released_fee=Self::fee(d,p,before)-Self::fee(d,p,after);
    if side==0 {self.bids-=delta;self.bn-=delta*p;} else {self.asks-=delta;self.an-=delta*p;}
    self.fees-=released_fee;
    let mark=u64at(d,144);let gap=if side==0 {p.saturating_sub(mark*95/100)} else {((mark*105+99)/100).saturating_sub(p)};
    self.loss-=(delta*gap*u64at(d,168)) as i128+released_fee as i128;
}
pub fn check(&self,d:&[u8],s:usize)->R<()> {
    let a=seat(s);let q=i64at(d,a+120);let mark=u64at(d,144);let tv=u64at(d,168);
    let worst=q.abs().max((q+self.bids as i64).abs()).max((q-self.asks as i64).abs());
    require(worst<=u64at(d,a+160) as i64,11)?;
    let im=((worst as u64*mark*tv+9)/10) as i128;
    require(equity(d,s).min(u64at(d,a+112) as i128)>=im+self.loss,12)
}
}
pub fn risk(d:&mut [u8],s:usize)->R<()> {
    let r=RiskState::load(d,s);r.check(d,s)?;r.store(d,s);Ok(())
}
pub fn trade(d: &mut [u8], buyer: usize, seller: usize, x: u64, p: u32) -> R<()> {
    settle_funding(d,buyer); settle_funding(d,seller);
    trade_settled(d,buyer,seller,x,p)
}
// Matching synchronizes funding once per contiguous maker and once per taker.
// The funding index cannot change inside one instruction; other entry points
// retain trade() and its mandatory settlement.
pub fn trade_settled(d: &mut [u8], buyer: usize, seller: usize, x: u64, p: u32) -> R<()> {
    require(buyer!=seller && x<=MAX_Q,13)?;
    let n=x as i128*p as i128*u64at(d,168) as i128;
    for (s,sign) in [(buyer,1i64),(seller,-1i64)] {
        let a=seat(s); let q=i64at(d,a+120).checked_add(sign*x as i64).ok_or(err(14))?;
        require(q.unsigned_abs()<=MAX_Q,11)?;
        wi64(d,a+120,q); wi128(d,a+128,i128at(d,a+128)-sign as i128*n);
    }
    Ok(())
}
