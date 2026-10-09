// Compile the pinned, proven EconomicRuntime DAG adapter into this separate
// library. The running libevolution.so and archived evaluator remain untouched.
#include "../evolution/native.cpp"
#include "fused_dag.hpp"
#include "native.hpp"
#include <algorithm>
#include <cstring>
#include <limits>
#include <sqlite3.h>
#include <openssl/sha.h>

namespace research {
enum Code { okay=0,invalid=101,temporal=102,overflow=103,conflict=104,storage=105,missing=106,integrity=107 };
constexpr I price_max=0xffffff;
bool equal(const Digest&a,const Digest&b){return std::memcmp(&a,&b,32)==0;}
bool nonzero(const Digest&d){unsigned char v=0;for(auto b:d.bytes)v|=b;return v!=0;}
Digest hash(const void*p,std::size_t n){Digest d{};SHA256(static_cast<const unsigned char*>(p),n,d.bytes);return d;}
bool valid(const Profile&p){
    if(p.abi!=1||!p.revision||p.revision>INT64_MAX||!nonzero(p.user)||!nonzero(p.hash)||p.reserved||p.seat>=12||p.style>2)return false;
    if(p.position<1||p.position>8||p.clip<1||p.clip>2||p.min_spread<2||p.max_spread>32||p.min_spread>p.max_spread||p.max_skew<0||p.max_skew>24||p.loss<1||p.loss>1500)return false;
    for(I w:p.weights)if(w<0||w>1000)return false;
    return true;
}
struct Engine {
    Profile profile;void* dag;U replay_clock{1};bool replayed{};
    ~Engine(){fused_destroy(dag);}
};
int step(Engine&e,const I* f,I mark,I bid,I ask,U now,U observed,Decision&d){
    d={};if(!f||mark<=0||mark>=price_max||bid<0||bid>price_max||ask<0||ask>price_max)return invalid;
    Output out{};const int code=fused_step(e.dag,f,now,observed,&out);if(code||out.error||out.authority)return code?code:invalid;
    std::copy_n(out.values,4,d.raw);const auto&p=e.profile;
    const I skew=std::clamp(out.values[0],-p.max_skew,p.max_skew),spread=std::clamp(out.values[1],p.min_spread,p.max_spread),clip=std::min(out.values[2],p.clip);
    const I position=f[0];d.reduce=out.values[3]==1||std::abs(position)>=p.position;
    if(out.values[3]==2||!clip)return okay;
    I bp=std::max(I{1},mark+skew-spread),ap=std::min(price_max,mark+skew+spread);
    if(ask)bp=std::min(bp,ask-1);if(bid)ap=std::max(ap,bid+1);
    const I buy=std::min(clip,std::max(I{0},d.reduce?-position:p.position-position));
    const I sell=std::min(clip,std::max(I{0},d.reduce?position:p.position+position));
    if(buy&&bp>0&&bp<ap)d.quotes[d.count++]={0,bp,buy,static_cast<I>(d.reduce)};
    if(sell&&bp>0&&bp<ap&&ap<=price_max)d.quotes[d.count++]={1,ap,sell,static_cast<I>(d.reduce)};
    return okay;
}
bool fit(__int128 value){return value>=INT64_MIN&&value<=INT64_MAX;}
I fee(I lots,I price,I bps){return (lots*price*bps+9999)/10000;}
int validate_window(const Frame*f,U n,const Order*o,U no,const Trade*t,U nt,const Fence&b){
    if(!f||n<24||n>8192||(no&&!o)||(nt&&!t)||no>n*4096||nt>1000000||b.purge_frames>10000||b.training>1)return invalid;
    if(b.quote_cost<0||b.quote_cost>1000000||b.exit_cost<0||b.exit_cost>1000000||b.maker_fee_bps<0||b.maker_fee_bps>1000)return invalid;
    if(b.cutoff_id>UINT64_MAX-b.purge_frames||f[0].id<=b.cutoff_id+b.purge_frames||f[0].id<=b.consumed_id||f[0].unix_ns<=b.finished_ns)return temporal;
    for(U i=0;i<n;++i){const auto&x=f[i];
        if(!x.id||!x.slot||!x.unix_ns||x.unix_ns>INT64_MAX||!x.publish_ns||x.publish_ns>INT64_MAX||x.mark<33||x.mark>=price_max||x.funding< -1000000000LL||x.funding>1000000000LL||x.imbalance< -100||x.imbalance>100||x.momentum< -100||x.momentum>100||x.depth<0||x.depth>1000000)return invalid;
        if(x.verified!=1||x.provenance!=1||x.external||x.reserved||x.order_count>4096||x.order_offset>no||x.order_count>no-x.order_offset)return invalid;
        if(x.slot<x.oracle_slot||static_cast<__int128>(x.unix_ns)-x.publish_ns>90000000000LL||static_cast<__int128>(x.publish_ns)-x.unix_ns>2000000000LL)return temporal;
        if(i&&(x.id<=f[i-1].id||x.slot<=f[i-1].slot||x.unix_ns<f[i-1].unix_ns||x.unix_ns-f[i-1].unix_ns>15000000000ULL))return temporal;
    }
    for(U i=0;i<no;++i)if(o[i].side<0||o[i].side>1||o[i].seat<0||o[i].seat>=256||o[i].price<1||o[i].price>price_max||o[i].lots<0||o[i].lots>1000000)return invalid;
    for(U i=0;i<nt;++i)if(t[i].slot<f[0].slot||t[i].slot>f[n-1].slot||(i&&t[i].slot<t[i-1].slot)||t[i].side<0||t[i].side>1||t[i].lots<0||t[i].lots>1000000||t[i].turnover<0||t[i].turnover>16000000000000LL)return invalid;
    return okay;
}
struct Resting { Quote quote;I ahead; };
int replay(Engine&e,const Frame*f,U n,const Order*orders,U no,const Trade*trades,U nt,const Fence&fence,U delay,U queue,Metrics&m,Trace*trace){
    m={};int code=validate_window(f,n,orders,no,trades,nt,fence);if(code)return code;
    if((delay!=1&&delay!=3)||(queue!=1&&queue!=2)||e.replayed)return invalid;
    e.replayed=true;I position=0,cash=0,peak=0,last_funding=f[0].funding;
    Resting resting[2]{},pending[2]{};U resting_count=0,pending_count=0,pending_at=0,trade_index=0;bool has_pending=false,quoted=false;
    U last_quote=0;const auto&p=e.profile;
    for(U index=0;index<n;++index){const auto&x=f[index];
        cash-=position*(x.funding-last_funding);last_funding=x.funding;
        while(trade_index<nt&&trades[trade_index].slot<=x.slot){const auto&t=trades[trade_index++];if(!t.lots)continue;I volume=t.lots;
            for(U j=0;j<resting_count;++j){auto&r=resting[j];auto&q=r.quote;
                if(q.side==t.side||q.lots<=0)continue;
                // Rational comparison avoids floating VWAP boundary drift.
                if((q.side==0&&t.turnover>q.price*t.lots)||(q.side==1&&t.turnover<q.price*t.lots))continue;
                I consumed=std::min(volume,r.ahead);r.ahead-=consumed;volume-=consumed;
                I room=q.reduce?std::max(I{0},q.side==0?-position:position):(q.side==0?p.position-position:p.position+position);
                I take=std::min({q.lots,volume/2,std::max(I{0},room)});if(!take)continue;
                I signed_lots=q.side==0?take:-take;position+=signed_lots;cash-=signed_lots*q.price;q.lots-=take;volume-=take;
                I maker=fee(take,q.price,fence.maker_fee_bps);cash-=maker;m.maker_fees+=maker;m.turnover+=take*q.price;m.fills+=take;
                I future_mark=f[std::min(n-1,index+3)].mark;m.adverse+=std::max(I{0},signed_lots*(q.price-future_mark));
            }
        }
        if(has_pending&&pending_at<=index){std::copy_n(pending,pending_count,resting);resting_count=pending_count;has_pending=false;}
        I equity=cash+position*x.mark;peak=std::max(peak,equity);m.drawdown=std::max(m.drawdown,peak-equity);m.inventory_sum+=std::abs(position);m.max_position=std::max(m.max_position,std::abs(position));
        if(equity<=-p.loss){m.loss_stop=1;resting_count=0;has_pending=false;}
        if(!m.loss_stop&&(!quoted||x.unix_ns-last_quote>=6000000000ULL)&&!has_pending){
            I bid=0,ask=0,lo=x.mark,hi=x.mark;
            for(U j=index>7?index-7:0;j<=index;++j){lo=std::min(lo,f[j].mark);hi=std::max(hi,f[j].mark);}
            for(U j=x.order_offset;j<x.order_offset+x.order_count;++j){const auto&o=orders[j];if(o.seat==static_cast<I>(p.seat))continue;if(o.side==0)bid=std::max(bid,o.price);else ask=ask?std::min(ask,o.price):o.price;}
            I features[6]={position,x.imbalance,x.momentum,std::min(I{100},hi-lo),std::min(I{4096},x.depth),static_cast<I>(x.unix_ns>=x.publish_ns?(x.unix_ns-x.publish_ns)/1000000000ULL:0)};
            Decision d{};U now=++e.replay_clock;code=step(e,features,x.mark,bid,ask,now,now,d);if(code)return code;
            pending_count=d.count;
            for(U j=0;j<d.count;++j){I ahead=0;auto&q=d.quotes[j];
                for(U k=x.order_offset;k<x.order_offset+x.order_count;++k){const auto&o=orders[k];if(o.seat!=static_cast<I>(p.seat)&&o.side==q.side&&(q.side==0?o.price>=q.price:o.price<=q.price))ahead+=o.lots;}
                pending[j]={q,ahead*static_cast<I>(queue)};
            }
            has_pending=true;pending_at=index+delay;++m.updates;last_quote=x.unix_ns;quoted=true;cash-=fence.quote_cost;m.network_cost+=fence.quote_cost;
        }
        if(trace)trace[index]={x.slot,position,equity};
    }
    I exit_price=f[n-1].mark+(position>0?-32:32);m.exit_fee=fee(std::abs(position),exit_price,5);
    I exit_network=position?fence.exit_cost:0;cash+=position*exit_price-m.exit_fee-exit_network;m.network_cost+=exit_network;
    m.pnl=cash;m.drawdown=std::max(m.drawdown,peak-cash);m.frames=static_cast<I>(n);
    const auto&w=p.weights;__int128 numerator=(static_cast<__int128>(w[0])*m.pnl-static_cast<__int128>(w[1])*m.drawdown-static_cast<__int128>(w[3])*m.adverse-static_cast<__int128>(w[4])*m.updates)*n-static_cast<__int128>(w[2])*m.inventory_sum;
    numerator*=1000000;const __int128 score=numerator>=0?(numerator+n/2)/n:-((-numerator+n/2)/n);if(!fit(score))return overflow;m.score_micro=static_cast<I>(score);return okay;
}
struct Registry {sqlite3*db{};~Registry(){if(db)sqlite3_close(db);}};
struct Statement {sqlite3_stmt*s{};~Statement(){if(s)sqlite3_finalize(s);}};
bool prepare(Registry&r,Statement&s,const char*q){return sqlite3_prepare_v2(r.db,q,-1,&s.s,nullptr)==SQLITE_OK;}
void bind(sqlite3_stmt*s,int i,const void*p,int n){sqlite3_bind_blob(s,i,p,n,SQLITE_TRANSIENT);}
int read_profile(Registry&r,const Digest&user,U revision,Profile&out){
    Statement s;if(!prepare(r,s,revision?"SELECT body,checksum FROM profiles WHERE user=? AND revision=?":"SELECT body,checksum FROM profiles WHERE user=? ORDER BY revision DESC LIMIT 1"))return storage;
    bind(s.s,1,&user,32);if(revision)sqlite3_bind_int64(s.s,2,static_cast<I>(revision));if(sqlite3_step(s.s)!=SQLITE_ROW)return missing;
    if(sqlite3_column_bytes(s.s,0)!=sizeof(Profile)||sqlite3_column_bytes(s.s,1)!=32)return integrity;
    std::memcpy(&out,sqlite3_column_blob(s.s,0),sizeof(out));Digest expected{};std::memcpy(&expected,sqlite3_column_blob(s.s,1),32);
    return valid(out)&&equal(out.user,user)&&(!revision||out.revision==revision)&&equal(hash(&out,sizeof(out)),expected)?okay:integrity;
}
int cursor(Registry&r,const Digest&user,U revision,U&out){
    Statement s;if(!prepare(r,s,"SELECT last_id,body,checksum FROM outcomes WHERE user=? AND revision=? ORDER BY last_id DESC LIMIT 1"))return storage;
    bind(s.s,1,&user,32);sqlite3_bind_int64(s.s,2,static_cast<I>(revision));int code=sqlite3_step(s.s);if(code==SQLITE_DONE){out=0;return okay;}if(code!=SQLITE_ROW)return storage;
    if(sqlite3_column_bytes(s.s,1)!=sizeof(Outcome)||sqlite3_column_bytes(s.s,2)!=32)return integrity;
    Outcome value{};Digest expected{};std::memcpy(&value,sqlite3_column_blob(s.s,1),sizeof(value));std::memcpy(&expected,sqlite3_column_blob(s.s,2),32);
    out=static_cast<U>(sqlite3_column_int64(s.s,0));return equal(hash(&value,sizeof(value)),expected)&&equal(value.user,user)&&value.revision==revision&&value.last_id==out?okay:integrity;
}
bool metrics_valid(const Metrics&m){return m.frames>=24&&m.frames<=8192&&m.drawdown>=0&&m.inventory_sum>=0&&m.max_position>=0&&m.max_position<=8&&m.adverse>=0&&m.fills>=0&&m.turnover>=0&&m.updates>=0&&m.updates<=m.frames&&m.exit_fee>=0&&m.maker_fees>=0&&m.network_cost>=0&&(m.loss_stop==0||m.loss_stop==1);}
}
extern "C" void* rp_engine_create(const void*nodes,std::uint64_t count,const std::uint64_t*outputs,const research::Profile*p,std::uint64_t now,std::uint64_t expires){
    if(!p||!research::valid(*p))return nullptr;void*dag=fused_create(static_cast<const Node*>(nodes),count,outputs,now,expires);if(!dag)return nullptr;
    auto*e=new(std::nothrow) research::Engine{*p,dag,now,false};if(!e)fused_destroy(dag);return e;
}
extern "C" int rp_engine_step(void*p,const std::int64_t*f,std::int64_t mark,std::int64_t bid,std::int64_t ask,std::uint64_t now,std::uint64_t observed,research::Decision*out){if(!p||!out)return research::invalid;return research::step(*static_cast<research::Engine*>(p),f,mark,bid,ask,now,observed,*out);}
extern "C" void rp_engine_destroy(void*p){delete static_cast<research::Engine*>(p);}
extern "C" int rp_replay(void*p,const research::Frame*f,std::uint64_t n,const research::Order*o,std::uint64_t no,const research::Trade*t,std::uint64_t nt,const research::Fence*b,std::uint64_t delay,std::uint64_t queue,research::Metrics*m,research::Trace*trace){if(!p||!b||!m)return research::invalid;return research::replay(*static_cast<research::Engine*>(p),f,n,o,no,t,nt,*b,delay,queue,*m,trace);}
extern "C" int rp_compare(void* const* engines,const research::Frame*f,std::uint64_t n,const research::Order*o,std::uint64_t no,const research::Trade*t,std::uint64_t nt,const research::Fence*b,research::Comparison*out){
    using namespace research;if(!engines||!b||!out||b->training)return invalid;*out={};
    for(U i=0;i<4;++i)if(!engines[i])return invalid;
    const auto&profile=static_cast<Engine*>(engines[0])->profile;
    for(U i=1;i<4;++i)if(std::memcmp(&profile,&static_cast<Engine*>(engines[i])->profile,sizeof(Profile)))return conflict;
    bool eligible=true;
    for(U i=0;i<2;++i){
        int code=replay(*static_cast<Engine*>(engines[2*i]),f,n,o,no,t,nt,*b,i?3:1,i?2:1,out->parent[i],nullptr);if(code)return code;
        code=replay(*static_cast<Engine*>(engines[2*i+1]),f,n,o,no,t,nt,*b,i?3:1,i?2:1,out->candidate[i],nullptr);if(code)return code;
        const auto&a=out->parent[i];const auto&c=out->candidate[i];eligible=eligible&&c.score_micro>a.score_micro&&c.fills>=2&&!c.loss_stop&&c.drawdown<=profile.loss;
    }
    out->eligible=eligible;return okay;
}
extern "C" void* rp_registry_open(const char*path){
    if(!path)return nullptr;auto*r=new(std::nothrow) research::Registry{};if(!r)return nullptr;
    if(sqlite3_open_v2(path,&r->db,SQLITE_OPEN_READWRITE|SQLITE_OPEN_CREATE|SQLITE_OPEN_FULLMUTEX,nullptr)!=SQLITE_OK){delete r;return nullptr;}
    sqlite3_busy_timeout(r->db,5000);
    bool version_ok=false;
    {research::Statement version;version_ok=research::prepare(*r,version,"PRAGMA user_version")&&sqlite3_step(version.s)==SQLITE_ROW&&sqlite3_column_int(version.s,0)<=1;}
    if(!version_ok){delete r;return nullptr;}
    const char*sql="PRAGMA journal_mode=WAL;PRAGMA synchronous=FULL;CREATE TABLE IF NOT EXISTS profiles(user BLOB,revision INTEGER,hash BLOB,body BLOB,checksum BLOB,PRIMARY KEY(user,revision));CREATE TABLE IF NOT EXISTS outcomes(user BLOB,revision INTEGER,last_id INTEGER,body BLOB,checksum BLOB,PRIMARY KEY(user,revision,last_id));PRAGMA user_version=1;";
    if(sqlite3_exec(r->db,sql,nullptr,nullptr,nullptr)!=SQLITE_OK){delete r;return nullptr;}return r;
}
extern "C" void rp_registry_close(void*p){delete static_cast<research::Registry*>(p);}
extern "C" int rp_profile_get(void*p,const research::Digest*u,std::uint64_t revision,research::Profile*out){if(!p||!u||!out||revision>INT64_MAX)return research::invalid;return research::read_profile(*static_cast<research::Registry*>(p),*u,revision,*out);}
extern "C" int rp_profile_append(void*p,const research::Profile*value,std::uint64_t expected){
    using namespace research;if(!p||!value||!valid(*value)||expected>=INT64_MAX||value->revision!=expected+1)return invalid;
    auto&r=*static_cast<Registry*>(p);if(sqlite3_exec(r.db,"BEGIN IMMEDIATE",nullptr,nullptr,nullptr)!=SQLITE_OK)return storage;
    auto fail=[&](int code){sqlite3_exec(r.db,"ROLLBACK",nullptr,nullptr,nullptr);return code;};Profile old{};int found=read_profile(r,value->user,0,old);
    if(found!=okay&&found!=missing)return fail(found);
    if((found==missing&&(expected||nonzero(value->parent)))||(found==okay&&(old.revision!=expected||!equal(value->parent,old.hash))))return fail(conflict);
    Statement s;if(!prepare(r,s,"INSERT INTO profiles VALUES(?,?,?,?,?)"))return fail(storage);auto digest=hash(value,sizeof(*value));
    bind(s.s,1,&value->user,32);sqlite3_bind_int64(s.s,2,static_cast<I>(value->revision));bind(s.s,3,&value->hash,32);bind(s.s,4,value,sizeof(*value));bind(s.s,5,&digest,32);
    if(sqlite3_step(s.s)!=SQLITE_DONE)return fail(storage);if(sqlite3_exec(r.db,"COMMIT",nullptr,nullptr,nullptr)!=SQLITE_OK)return fail(storage);return okay;
}
extern "C" int rp_cursor(void*p,const research::Digest*u,std::uint64_t rev,std::uint64_t*out){if(!p||!u||!out||!rev||rev>INT64_MAX)return research::invalid;return research::cursor(*static_cast<research::Registry*>(p),*u,rev,*out);}
extern "C" int rp_outcome_get(void*p,const research::Digest*u,std::uint64_t rev,std::uint64_t last,research::Outcome*out){
    using namespace research;if(!p||!u||!out||rev>INT64_MAX||last>INT64_MAX)return invalid;auto&r=*static_cast<Registry*>(p);Statement s;
    if(!prepare(r,s,"SELECT body,checksum FROM outcomes WHERE user=? AND revision=? AND last_id=?"))return storage;
    bind(s.s,1,u,32);sqlite3_bind_int64(s.s,2,static_cast<I>(rev));sqlite3_bind_int64(s.s,3,static_cast<I>(last));if(sqlite3_step(s.s)!=SQLITE_ROW)return missing;
    if(sqlite3_column_bytes(s.s,0)!=sizeof(Outcome)||sqlite3_column_bytes(s.s,1)!=32)return integrity;
    std::memcpy(out,sqlite3_column_blob(s.s,0),sizeof(*out));Digest expected{};std::memcpy(&expected,sqlite3_column_blob(s.s,1),32);
    return equal(hash(out,sizeof(*out)),expected)&&equal(out->user,*u)&&out->revision==rev&&out->last_id==last?okay:integrity;
}
extern "C" int rp_outcome_append(void*p,const research::Outcome*o){
    using namespace research;if(!p||!o||!nonzero(o->parent)||!nonzero(o->candidate)||!nonzero(o->window)||o->first_id<=o->cutoff_id||o->last_id<o->first_id||o->last_id>INT64_MAX||o->first_ns<=o->finished_ns||o->last_ns<o->first_ns||o->comparison.eligible>1)return temporal;
    auto&r=*static_cast<Registry*>(p);if(sqlite3_exec(r.db,"BEGIN IMMEDIATE",nullptr,nullptr,nullptr)!=SQLITE_OK)return storage;
    auto fail=[&](int code){sqlite3_exec(r.db,"ROLLBACK",nullptr,nullptr,nullptr);return code;};Profile profile{};int code=read_profile(r,o->user,o->revision,profile);if(code)return fail(code);if(!equal(profile.hash,o->profile))return fail(conflict);
    Outcome old{};code=rp_outcome_get(p,&o->user,o->revision,o->last_id,&old);
    if(code==okay){bool same=std::memcmp(&old,o,sizeof(old))==0;return fail(same?okay:conflict);}if(code!=missing)return fail(code);
    U last=0;code=cursor(r,o->user,o->revision,last);if(code)return fail(code);if(o->first_id<=last)return fail(temporal);
    for(U i=0;i<2;++i){auto&a=o->comparison.parent[i];auto&b=o->comparison.candidate[i];if(!metrics_valid(a)||!metrics_valid(b)||a.frames!=b.frames)return fail(invalid);
        if(o->comparison.eligible&&!(b.score_micro>a.score_micro&&b.fills>=2&&!b.loss_stop&&b.drawdown<=profile.loss))return fail(invalid);}
    Statement s;if(!prepare(r,s,"INSERT INTO outcomes VALUES(?,?,?,?,?)"))return fail(storage);auto digest=hash(o,sizeof(*o));
    bind(s.s,1,&o->user,32);sqlite3_bind_int64(s.s,2,static_cast<I>(o->revision));sqlite3_bind_int64(s.s,3,static_cast<I>(o->last_id));bind(s.s,4,o,sizeof(*o));bind(s.s,5,&digest,32);
    if(sqlite3_step(s.s)!=SQLITE_DONE)return fail(storage);if(sqlite3_exec(r.db,"COMMIT",nullptr,nullptr,nullptr)!=SQLITE_OK)return fail(storage);return okay;
}
