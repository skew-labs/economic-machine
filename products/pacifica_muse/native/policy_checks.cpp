#include "policy.hpp"
#include <iostream>
using namespace muse_session;
static unsigned checks{};
void check(bool b,const char* m){++checks;if(!b)throw std::runtime_error(m);}
template<class F>void reject(F f,const char*m){bool no=false;try{f();}catch(...){no=true;}check(no,m);}
constexpr std::int64_t at=1792000000000;
J packet(){J p={{"mode","quote"},{"half_spread_millibps",1000},{"inventory_skew_millibps",5000},{"flow_shift_millibps",0},
    {"volatility_multiplier_milli",1000},{"clip_usd_micros",600000000},{"refresh_ticks",2},{"ttl_ms",60000}};
    return {{"schema","muse-policy-install-v1"},{"session_id","test-session"},{"version",1},{"model","meta/muse-spark-1.3-contributor"},
        {"kind","fixture"},{"issued_ms",at},{"published_ms",at+2000},{"expires_ms",at+60000},{"policy",p},
        {"policy_sha256",sha256(p.dump())},{"context_sha256",std::string(64,'a')},
        {"forward_gate",{{"first_ms",at+1},{"last_ms",at+2000},{"samples",8},{"kind","future_input_sanity"},{"profitability_proven",false}}}};
}
int main(){try{
    unsigned saves=0;Session s(Lease::begin(at),[&]{++saves;},[](auto&,auto&,auto&){});
    auto j=packet();check(install_packet(s,j,"test-session",at+2000,false)&&saves==1,"automatic installation persisted");
    check(!install_packet(s,j,"test-session",at+3000,false),"same version never renews TTL");
    check(!s.policy_ready(at+60000)&&!s.policy_ready(at+60001),"expired policy stays expired at exact deadline");
    for(int n=0;n<11;++n){Session x(Lease::begin(at),[]{},[](auto&,auto&,auto&){});auto bad=packet();
        switch(n){
        case 0:bad["session_id"]="other";break;
        case 1:bad["policy"]["clip_usd_micros"]=1200000000;break;
        case 2:bad["model"]="other";break;
        case 3:bad["expires_ms"]=at+70000;break;
        case 4:bad["forward_gate"]["first_ms"]=at-1;break;
        case 5:bad["forward_gate"]["samples"]=7;break;
        case 6:bad["forward_gate"]["profitability_proven"]=true;break;
        case 7:bad["version"]=true;break;
        case 8:bad["context_sha256"]="wrong";break;
        case 9:bad["published_ms"]=at+3000;break;
        case 10:bad["policy"]["ttl_ms"]=60000.0;break;
        }
        reject([&]{install_packet(x,bad,"test-session",at+2000,false);},"malformed/control mismatch rejected");
    }
    Session live(Lease::begin(at),[]{},[](auto&,auto&,auto&){});
    reject([&]{install_packet(live,j,"test-session",at+2000,true);},"fixture cannot activate live policy");
    j["kind"]="muse";check(install_packet(live,j,"test-session",at+2000,true),"validated Muse-origin envelope accepted by live parser");
    for(bool short_ttl:{false,true}){
        Session x(Lease::begin(at),[]{},[](auto&,auto&,auto&){});auto bad=packet();
        if(short_ttl){bad["policy"]["ttl_ms"]=10000;bad["expires_ms"]=at+10000;}
        else bad["policy"]["clip_usd_micros"]=20000000;
        bad["policy_sha256"]=sha256(bad["policy"].dump());
        reject([&]{install_packet(x,bad,"test-session",at+2000,false);},"valid digest cannot change fixed owner sizing/TTL");
    }
    auto root=std::filesystem::temp_directory_path()/("muse-control-"+pacifica::uuid());
    std::filesystem::create_directory(root);auto file=root/"policy.json";
    {std::ofstream out(file);out<<"{\"x\":1,\"x\":2}";}chmod(file.c_str(),0600);
    reject([&]{control_read(file);},"duplicate JSON keys rejected before policy parsing");
    {std::ofstream out(file);out<<packet().dump();}chmod(file.c_str(),0644);
    reject([&]{control_read(file);},"publicly readable policy rejected");
    chmod(file.c_str(),0600);check(control_read(file)==packet(),"private policy file read");
    auto link=root/"alias.json";std::filesystem::create_symlink(file,link);
    reject([&]{control_read(link);},"policy symlink rejected");std::filesystem::remove_all(root);
    // This envelope is a local fixture, not an actual provider response.
    std::cout<<J{{"checks",checks},{"passed",true},{"actual_model_calls",0},{"orders_submitted",0}}.dump()<<'\n';
}catch(const std::exception&e){std::cerr<<e.what()<<'\n';return 1;}}
