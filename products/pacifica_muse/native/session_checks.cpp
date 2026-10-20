#include "session.hpp"
#include "pacifica_journal.hpp"
#include <iostream>

using namespace muse_session;
static unsigned checks{};
static void check(bool b,const char* what){++checks;if(!b)throw std::runtime_error(what);}
template<class F>void rejects(F f,const char* what){bool failed=false;try{f();}catch(...){failed=true;}check(failed,what);}
constexpr std::int64_t at=1792000000000;
mm::Policy policy(){return {mm::Mode::quote,1000,5000,0,1000,20*scale,2,60000};}
J account(){return {{"account_equity","65.249428"},{"available_to_spend","65.249428"},{"cross_mmr","0"},{"maker_fee","-0.00005"}};}
struct Fixture {
    std::vector<J> out;unsigned saves{};bool uncertain{},disk_failure{};
    Session s;
    quant::Top top{149.99,150.01,100,100,1000000000,at,1};
    Fixture():s(Lease::begin(at),[this]{++saves;if(disk_failure)throw std::runtime_error("mock disk failure");},[this](const auto& id,const auto& op,const auto& data){
        check(saves>0,"reservation persisted first");
        check(op=="cancel_order"||(op=="create_order"&&data.at("tif")=="ALO"&&data.at("symbol")=="SOL"),"maker SOL only");
        out.push_back({{"id",id},{"op",op},{"data",data}});
        if(uncertain)throw std::runtime_error("mock transport uncertain");
    }){
        auto& c=s.controller;c.ledger.account_info(account(),at);c.ledger.reconciled=true;
        c.subscriptions={"account_info","account_order_updates","account_trades","account_positions"};
        s.install(policy(),at,1,at);
    }
    void tick(std::int64_t now=at){s.tick(top,now,true,1000,0,false);}
    void ack(const J& msg,int oid,std::int64_t now=at){s.controller.response({{"id",msg.at("id")},{"type",msg.at("op")},{"code",200},
        {"data",{{"I",msg.at("data").at("client_order_id")},{"i",oid},{"s","SOL"}}}},now);}
};
J fill(const pacifica::Order& o,int h,const char* amount,std::int64_t now=at){
    return {{"u",pacifica::account},{"s","SOL"},{"h",h},{"i",o.oid},{"I",o.cid},{"a",amount},{"p",pacifica::decimal(o.price)},
        {"f","-0.000750"},{"n","0.000750"},{"t",now},{"te","fulfill_maker"},{"ts","open_long"},{"tc","normal"}};
}
void terminal(Fixture& f,const pacifica::Order& o,const char* filled,int sequence){
    f.s.controller.ledger.order_event({{"u",pacifica::account},{"s","SOL"},{"i",o.oid},{"I",o.cid},{"d",o.side==1?"bid":"ask"},
        {"a",pacifica::decimal(o.qty)},{"f",filled},{"li",sequence},{"os","cancelled"}});
}
int main(){try {
    Fixture f;f.tick();auto& c=f.s.controller;
    check(f.out.size()==1&&c.ledger.fill_receipts==0,"submission not fill");
    auto buy=f.out.back();f.tick();check(f.out.size()==1,"unknown create not duplicated");
    f.ack(buy,71);f.tick();check(f.out.size()==2,"opposite side after first ACK");
    auto sell=f.out.back();f.ack(sell,72);f.tick();check(f.out.size()==2,"unchanged quote retains priority");
    check(c.ledger.opening_pending()<=1200,"both sides share opening cap");
    Fixture full;auto full_policy=policy();full_policy.clip_usd_micros=600*scale;
    full.s.install(full_policy,at+1,2,at+1);full.tick(at+1);full.ack(full.out.back(),701,at+1);full.tick(at+1);
    check(full.out.size()==2,"owner 600 per side reaches both mock transports");
    check(full.s.controller.ledger.opening_pending()>1190&&full.s.controller.ledger.opening_pending()<=1200,"ACK recomputation preserves full two-sided budget");
    check(full.s.policy_ready(at+60000)&&!full.s.policy_ready(at+60001),"one minute expires exactly at original issuance plus TTL");
    full.ack(full.out.back(),702,at+1);
    full.s.controller.ledger.equity=30;full.s.controller.ledger.free_margin=30;
    full.tick(at+2);
    check(full.out.size()==4&&full.out[2]["op"]=="cancel_order"&&full.out[3]["op"]=="cancel_order",
          "resting opening orders cancelled when aggregate capital ceiling contracts");
    auto& order=c.ledger.orders.at(buy["data"]["client_order_id"].get<std::string>());
    check(c.ledger.trade(fill(order,100,"0.10"),at),"actual partial fill consumed");
    check(!c.ledger.trade(fill(order,100,"0.10"),at)&&c.ledger.net_lots==10,"duplicate partial fill ignored");
    f.tick();check(f.out.back()["op"]=="cancel_order","inventory change cancels opening opposite quote");
    auto cancel=f.out.back();f.ack(cancel,72);
    auto& ask=c.ledger.orders.at(sell["data"]["client_order_id"].get<std::string>());
    check(!ask.done()&&ask.reserved()>0,"cancel ACK retains reservation");
    auto count=f.out.size();f.tick();check(f.out.size()==count,"no replacement during cancel uncertainty");
    terminal(f,ask,"0",10);c.ledger.order_event({{"u",pacifica::account},{"s","SOL"},{"i",71},{"I",order.cid},{"d","bid"},
        {"a",pacifica::decimal(order.qty)},{"f","0.10"},{"li",11},{"os","partially_filled"}});
    f.tick();check(f.out.back()["data"].value("reduce_only",false),"opposite quote now reduce-only");
    check(pacifica::units(f.out.back()["data"]["amount"])<=10,"exit limited by actual inventory");

    Fixture late;late.tick();late.ack(late.out.back(),81);
    late.tick(at+duration_ms);check(late.out.back()["op"]=="cancel_order"&&late.s.lease().stopped,"48 hour deadline cancels owned quote");
    auto saved=late.s.state();Session restored(Lease::begin(at+duration_ms),[]{},[](auto&,auto&,auto&){});restored.restore(saved);
    check(restored.lease().deadline==at+duration_ms&&restored.lease().stopped&&!restored.policy_ready(at+duration_ms),"restart cannot extend lease or renew policy");
    auto invalid=saved;invalid["lease"]["deadline"]=at+duration_ms+1;
    rejects([&]{restored.restore(invalid);},"modified deadline rejected");
    Lease rollback=Lease::begin(at);check(!rollback.observe(at-1)&&rollback.stopped,"clock rollback latches closed");
    rejects([&]{f.s.install(policy(),at,1,at);},"duplicate version not TTL renewal");
    rejects([&]{f.s.install(policy(),at,2,at);},"same issue time not TTL renewal");
    Fixture observe;observe.s.tick(observe.top,at,true,1000,0,false,true);
    check(observe.out.empty()&&observe.s.controller.ledger.orders.empty()&&observe.s.plans==1,"observation computes without reserving or sending");
    observe.tick();observe.ack(observe.out.back(),99);auto observed_sends=observe.out.size();
    observe.s.tick(observe.top,at+2001,true,1000,0,false,true);
    check(observe.out.size()==observed_sends&&!observe.s.controller.ledger.orders.begin()->second.cancel_requested,"observation never cancels existing orders");
    Session restart_policy(Lease::begin(at),[]{},[](auto&,auto&,auto&){});restart_policy.restore(observe.s.state());
    rejects([&]{restart_policy.install(policy(),at,2,at+1);},"restart cannot accept same policy issue time under a new version");
    restart_policy.install(policy(),at+1,2,at+1);
    check(restart_policy.policy_ready(at+1),"restart accepts a newly issued policy");
    Fixture stale;stale.tick(at+2001);check(stale.out.empty(),"stale account/market creates nothing");
    Fixture owner;owner.s.tick(owner.top,at,false,1000,0,false);check(owner.out.empty(),"exclusive writer required");
    Fixture fee;fee.s.controller.ledger.fee_bps=0;fee.tick();check(fee.out.empty()&&!fee.s.expected_rebate_verified,"changed rebate prevents new opening");
    Fixture paused;paused.s.controller.ledger.net_lots=10;paused.s.controller.ledger.fee_bps=0;
    auto pause=policy();pause.mode=mm::Mode::pause;paused.s.install(pause,at+1,2,at+1);paused.tick(at+1);
    check(paused.out.empty(),"pause cannot be overridden by fee/risk reduction");
    Fixture wide;wide.s.controller.policy.native_continuity=true;rejects([&]{wide.tick();},"legacy account-sized mode rejected");
    Fixture loss;loss.s.controller.ledger.equity=15;loss.s.controller.ledger.free_margin=15;
    loss.tick();check(loss.out.size()==1,"no owner-disallowed P&L stop");
    check(loss.s.controller.planner.capital_cap(loss.s.controller.ledger,loss.s.controller.policy)==300,"capital contracts with equity");
    Fixture unknown;unknown.uncertain=true;rejects([&]{unknown.tick();},"uncertain send captured");unknown.uncertain=false;unknown.tick();
    check(unknown.out.size()==1&&unknown.s.controller.ledger.unknown(),"uncertain send cannot become new nonce");
    Fixture disk_fault;disk_fault.disk_failure=true;rejects([&]{disk_fault.tick();},"failed persistence propagates");
    check(disk_fault.out.empty(),"disk failure prevents transport call");
    Fixture race;race.tick();auto first=race.out.back();race.ack(first,91);
    auto& racing=race.s.controller.ledger.orders.at(first["data"]["client_order_id"].get<std::string>());
    race.tick(at+duration_ms);race.ack(race.out.back(),91,at+duration_ms);
    terminal(race,racing,"0.10",40);
    check(!racing.done()&&racing.reserved()>0,"terminal cancel with missing fill retains reservation");
    check(race.s.controller.ledger.trade(fill(racing,300,"0.10",at+duration_ms),at+duration_ms+1),"late fill after deadline still accounted");
    check(racing.done()&&race.s.controller.ledger.net_lots==10,"late fill resolves reserve but leaves real inventory");
    auto before=race.out.size();race.tick(at+duration_ms+2);
    check(race.out.size()==before,"no new orders after deadline, including passive exits");

    // Actual two-slot journal, public RFC8032 fixture key, injected transport.
    auto root=std::filesystem::temp_directory_path()/("muse-session-"+pacifica::uuid());std::filesystem::create_directory(root);
    Session* active=nullptr;std::unique_ptr<pacifica::DurableLedger> disk;unsigned signatures=0;
    Session durable(Lease::begin(at),[&]{if(disk)disk->commit(active->state());},[&](const std::string&,const std::string& op,const J& body){
        auto state=disk->load();auto cid=body["client_order_id"].get<std::string>();bool reserved=false;
        for(const auto& o:state["ledger"]["orders"])if(o["cid"]==cid)reserved=true;
        check(reserved,"disk reservation exists before signer");
        const char* hex="9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a";
        std::array<unsigned char,64> bytes{};for(int i=0;i<64;i++)bytes[i]=std::stoul(std::string(hex+2*i,2),nullptr,16);
        pacifica::AgentSigner fixture(pacifica::base58(bytes.data(),bytes.size()));auto request=fixture.request(op,body,at);
        auto pub=pacifica::unbase58(fixture.public_key),sig=pacifica::unbase58(request["signature"]);
        EVP_PKEY* key=EVP_PKEY_new_raw_public_key(EVP_PKEY_ED25519,nullptr,pub.data(),pub.size());EVP_MD_CTX* ctx=EVP_MD_CTX_new();
        auto message=pacifica::canonical(op,body,at);
        bool valid=EVP_DigestVerifyInit(ctx,nullptr,nullptr,nullptr,key)==1&&EVP_DigestVerify(ctx,sig.data(),sig.size(),reinterpret_cast<const unsigned char*>(message.data()),message.size())==1;
        EVP_MD_CTX_free(ctx);EVP_PKEY_free(key);check(valid,"canonical signed packet verifies");++signatures;
    });active=&durable;
    pacifica::DurableLedger::initialize(root,durable.state());disk=std::make_unique<pacifica::DurableLedger>(root);
    durable.controller.ledger.account_info(account(),at);durable.controller.ledger.reconciled=true;
    durable.controller.subscriptions=f.s.controller.subscriptions;durable.install(policy(),at,1,at);
    durable.tick(f.top,at,true,1000,0,false);check(signatures==1,"native plan reaches fixture signer and mock transport");
    auto restore_state=disk->load();Session after(Lease::begin(at+100),[]{},[](auto&,auto&,auto&){});after.restore(restore_state);
    check(after.controller.ledger.unknown()&&!after.controller.ledger.reconciled,"crash restores unresolved reservation and requires reconciliation");
    disk.reset();std::filesystem::remove_all(root);

    // Virtual time only: not 48 hours of wall-clock operation or exchange fills.
    Lease span=Lease::begin(at);std::uint64_t frames=0;
    for(std::int64_t elapsed=0;elapsed<=duration_ms;elapsed+=1000){
        bool open=span.observe(at+elapsed);check(open==(elapsed<duration_ms),"exact 48 hour admission boundary");++frames;
    }
    std::cout<<J{{"passed",true},{"checks",checks},{"simulated_hours",48},{"virtual_clock_frames",frames},
        {"synthetic_signatures",signatures},{"orders_submitted",0},{"network_calls",0},{"real_48h_run",false}}.dump()<<'\n';
}catch(const std::exception& e){std::cerr<<"muse session check "<<checks<<": "<<e.what()<<'\n';return 1;}}
