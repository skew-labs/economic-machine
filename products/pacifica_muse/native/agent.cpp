// Reuse the venue parser, never the former Ticking trading policy/entry point.
#define main unused_public_observer_main
#include "quant_shadow.cpp"
#undef main
#include "policy.hpp"
#include "pacifica_transport.hpp"
#include "pacifica_journal.hpp"
#include "pacifica_startup.hpp"
#include "ticking_agent_support.hpp"
#include <sys/prctl.h>
#include <sys/resource.h>

namespace ms=muse_session;
namespace px=pacifica;
static bool old_writer_inactive() {
    auto* pipe=popen("systemctl show pacifica-ticking-agent.service -p LoadState -p ActiveState --value", "r");
    if(!pipe)return false;char buffer[128]{};std::string state;
    while(fgets(buffer,sizeof buffer,pipe))state+=buffer;
    auto code=pclose(pipe);
    return code==0&&state.find("loaded\n")!=std::string::npos&&state.find("inactive\n")!=std::string::npos;
}
static std::string agent_credential(int fd) {
    require(fd>=3,"operator credential descriptor required");
    require(lseek(fd,0,SEEK_SET)==0,"credential descriptor rewind");
    std::array<char,102> bytes{};auto n=::read(fd,bytes.data(),bytes.size());
    require(n>0&&n<101,"credential descriptor bound");std::string result(bytes.data(),n);
    OPENSSL_cleanse(bytes.data(),bytes.size());if(result.ends_with('\n'))result.pop_back();return result;
}
static void flat_baseline(const J& s) {
    auto& a=s.at("account").at("data");
    require(s.at("positions").at("data").empty()&&s.at("orders").at("data").empty()&&
        a.at("positions_count")==0&&a.at("orders_count")==0&&a.at("stop_orders_count")==0,"new session requires independently flat account");
    require(s.at("positions").contains("last_order_id")&&s.at("positions").at("last_order_id")==s.at("orders").at("last_order_id"),"baseline sequence mismatch");
}
int main(int argc,char** argv) {
    signal(SIGINT,signal_stop);signal(SIGTERM,signal_stop);
    rlimit core{0,0};setrlimit(RLIMIT_CORE,&core);prctl(PR_SET_DUMPABLE,0);umask(0077);
    bool live=false,loaded=false;std::filesystem::path root,control;
    Runtime rt;std::vector<std::thread> readers;
    std::unique_ptr<ms::Session> session;std::unique_ptr<px::DurableLedger> disk;
    std::unique_ptr<px::AgentSigner> signer;std::unique_ptr<px::Gateway> gateway;
    std::unique_ptr<CycleLedger> lock;std::unique_ptr<ticking_mm::ModelReceiver> ipc;
    std::unique_ptr<px::Reconciler> reconcile;std::ofstream audit;
    std::string saved,session_id;std::int64_t installed{},policy_rejected{},events{},shadow_quotes{},last_heartbeat{};
    Latencies plan_latency,receipt_latency;std::uint64_t model_packets=0;
    auto save=[&]{if(!loaded||!session||!disk)return;auto state=session->state();auto text=state.dump();
        if(text==saved)return;disk->commit(state);saved=std::move(text);};
    auto record=[&](const J& j){if(audit.is_open()){audit<<j.dump()<<'\n';require(bool(audit),"audit append");}};
    auto finish=[&]{rt.shutdown();for(auto& t:readers)if(t.joinable())t.join();
        if(gateway)gateway->finish();if(reconcile)reconcile->finish();if(audit.is_open())audit.flush();save();};
    auto cancel_known=[&]{if(!live||!loaded||!signer||!session)return;
        auto& c=session->controller;
        for(auto&[cid,o]:c.ledger.orders)if(!o.done()) {
            // Even a missing create ACK may have left an order at the venue.
            // Cancel this durable owned CLOID; never adopt/cancel foreign IDs.
            try{o.cancel_requested=true;o.last_action_ms=wall();save();
                auto payload=signer->request("cancel_order",{{"symbol","SOL"},{"client_order_id",cid}},wall());
                auto result=px::http("/api/v1/orders/cancel",&payload);
                record({{"kind","shutdown_cancel"},{"cid",cid},{"http",result.code},{"success",result.body.value("success",false)},{"at_ms",wall()}});
            }catch(...){record({{"kind","shutdown_cancel_unconfirmed"},{"cid",cid},{"at_ms",wall()}});}
        }
    };
    try {
        require(argc>=5,"usage: muse-maker --observe ROOT CONTROL SECONDS | --live ROOT CONTROL CREDENTIAL_FD IPC_SOCKET");
        std::string mode=argv[1];require(mode=="--observe"||mode=="--live","explicit execution mode required");live=mode=="--live";
        require(argc==(live?6:5),"argument count");
        int seconds=live?0:std::stoi(argv[4]);require(live||(seconds>=5&&seconds<=300),"bounded observation");
        root=std::filesystem::canonical(argv[2]);control=std::filesystem::canonical(argv[3]);
        require(control==root/"control","control directory must belong to session");
        require(!live||old_writer_inactive(),"existing Japan writer is not inactive");
        require(curl_global_init(CURL_GLOBAL_DEFAULT)==CURLE_OK,"TLS initialization");
        std::filesystem::create_directories(root/"state");lock=std::make_unique<CycleLedger>(root/"state");
        audit.open(root/"events.jsonl",std::ios::app);require(bool(audit),"audit open");
        bool prior=std::filesystem::exists(root/"session-start.json");
        J start;
        if(prior){start=ms::control_read(root/"session-start.json");require(start.at("live")==live,"cannot promote an observe journal to live");session_id=start.at("session_id");}
        else session_id=px::uuid();
        auto initial=px::snapshot(false);
        if(!prior&&live){flat_baseline(initial);auto second=px::snapshot(false);flat_baseline(second);
            require(initial.at("positions").at("last_order_id")==second.at("positions").at("last_order_id"),"account changed during handover");initial=second;}
        auto settings=px::get("/api/v1/account/settings?account="+std::string(px::account));
        require(px::configured_sol_leverage(settings)==20,"existing SOL leverage must be 20; executor does not change it");
        auto begun=prior?ms::integer(start.at("start_ms")):wall();
        auto lease=ms::Lease::begin(begun);
        if(prior)require(ms::integer(start.at("deadline_ms"))==lease.deadline,"session deadline changed");
        else {start={{"session_id",session_id},{"start_ms",begun},{"deadline_ms",lease.deadline},{"live",live}};
            quant::atomic_file(root/"session-start.json",start.dump()+"\n",true);}
        session=std::make_unique<ms::Session>(lease,save,[&](const std::string& id,const std::string& op,const J& data){
            require(live&&signer&&gateway&&gateway->connected(),"live transport unavailable");
            require(op=="cancel_order"||(op=="create_order"&&wall()<session->lease().deadline&&session->policy_ready(wall())),"send deadline/policy expired");
            auto payload=signer->request(op,data,wall());
            record({{"kind","unsigned_intent"},{"id",id},{"op",op},{"data",data},{"at_ms",wall()}});
            gateway->send({{"id",id},{"params",{{op,payload}}}});
        });
        if(live){
            if(!prior)px::DurableLedger::initialize(root/"state",session->state());
            disk=std::make_unique<px::DurableLedger>(root/"state");session->restore(disk->load());loaded=true;
            require(session->lease().deadline==lease.deadline,"journal/start deadline mismatch");
            require(wall()<lease.deadline,"original 48-hour session expired");
            ipc=std::make_unique<ticking_mm::ModelReceiver>(argv[5],true);
            signer=std::make_unique<px::AgentSigner>(agent_credential(std::stoi(argv[4])));
        }
        auto& c=session->controller;auto info=px::get("/api/v1/info");bool found=false;
        for(const auto& row:info.at("data"))if(row.at("symbol")=="SOL"){c.market=px::Market::parse(row);found=true;}
        require(found,"SOL market absent");
        initial["revision"]=c.reconciliation_revision();c.reconcile(initial,wall(),true);
        rt.watch_account=px::account;
        if(live)gateway=std::make_unique<px::Gateway>(rt);
        reconcile=std::make_unique<px::Reconciler>(rt);
        const auto deadline=live?mono()+std::max<std::int64_t>(0,lease.deadline-wall())*1000000:mono()+Ns(seconds)*second;
        readers.emplace_back(reader,std::ref(rt),0,deadline);
        readers.emplace_back([&]{std::string previous;
            while(!rt.stop&&!interrupted&&mono()<deadline){
                try{if(std::filesystem::exists(control/"policy.json")){auto j=ms::control_read(control/"policy.json");auto body=j.dump();
                    if(body!=previous){previous=body;rt.push(4,body,mono(),wall());}}}catch(...){/* Old policy still expires. */}
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }});
        if(live)readers.emplace_back([&]{while(!rt.stop&&!interrupted&&mono()<deadline){pollfd p{ipc->fd(),POLLIN,0};if(poll(&p,1,50)<=0)continue;
            ticking_mm::ModelWire w;while(ipc->receive(w))if(!rt.push(1,std::string(reinterpret_cast<const char*>(&w),sizeof w),mono(),wall()))break;}});
        Features venue;Counters counters;std::unordered_set<std::string> seen;std::deque<std::string> ids;
        ticking_mm::em::Fair fair; ticking_mm::em::ModelSnapshot model;
        std::int64_t next_snapshot=0,inflight=0,health=0,last_save=0;
        while(!rt.stop&&!interrupted&&mono()<deadline) {
            Packet p;bool got=rt.wait_pop(p,Clock::now()+std::chrono::milliseconds(100));auto now=wall();
            if(got){++events;
                if(p.venue==1){ticking_mm::ModelWire w;require(p.text.size()==sizeof w,"feature packet ABI");std::memcpy(&w,p.text.data(),sizeof w);
                    if(ipc->accept(w,now,fair,model))++model_packets;}
                else {auto j=J::parse(p.text);
                    if(p.venue==4){try{if(ms::install_packet(*session,j,session_id,now,live)){++installed;record({{"kind","policy_installed"},{"version",session->policy_version()},{"origin",j.at("kind")},{"at_ms",now}});}}
                        catch(...){++policy_rejected;}}
                    else if(p.venue==2){c.response(j,now);record({{"kind","venue_response"},{"data",j},{"at_ms",now}});}
                    else if(p.venue==3){inflight=0;c.reconcile(j,now);next_snapshot=now+(j.value("code",0)==429?60000:(live?1500:5000));}
                    else if(j.value("channel","").starts_with("account_")||j.value("channel","")=="subscribe"){
                        c.private_event(j,now);if(j.value("channel","").starts_with("account_"))record({{"kind","account_event"},{"data",j},{"at_ms",now}});}
                    else ingest(p,j,venue,counters,seen,ids);
                }
            }
            if(venue.top().valid()&&(!live||(gateway&&gateway->connected()))){
                bool available=model.at&&now>=std::int64_t(model.at/1000000)&&now-model.at/1000000<2000;
                auto sigma=available?std::llround(model.sigma_bps*1000):1000;
                auto flow=available?std::llround(model.flow*1000000):0;
                auto before=session->plans;auto begin=mono();session->tick(venue.top(),now,true,sigma,flow,available,!live);auto end=mono();
                if(session->plans>before){plan_latency.add(end-begin);if(got&&p.venue==0)receipt_latency.add(end-p.wire_received);}
                if(!live&&session->last_plan.error==machine::economics::Error::okay)
                    shadow_quotes+=(session->last_plan.bid.quantity>0)+(session->last_plan.ask.quantity>0);
            }
            if(!inflight&&now>=next_snapshot){reconcile->request(c.reconciliation_revision(),live&&(!c.ledger.reconciled||c.ledger.unknown()));inflight=now;next_snapshot=now+(live?1500:5000);}
            if(now-last_save>=1000){c.ledger.prune(now);save();audit.flush();last_save=now;
                require(std::filesystem::space(root).available>2ull*1024*1024*1024,"runtime disk reserve");}
            if(now-health>=1000){last_heartbeat=now;
                quant::atomic_file(root/"status.json",J{{"mode",live?"operator_live":"observe_no_orders"},{"updated_ms",now},
                    {"session_id",session_id},{"deadline_ms",lease.deadline},{"policies_installed",installed},{"policy_version",session->policy_version()},
                    {"policy_rejected",policy_rejected},{"policy_fresh",session->policy_ready(now)},{"events",events},{"native_plans",session->plans},
                    {"decision_reason",session->last_reason},{"policy_ttl_ms",session->policy().ttl_ms},
                    {"per_side_quote_budget_usd",session->policy().clip_usd_micros/1000000},
                    {"hypothetical_quotes",shadow_quotes},{"shadow_exclusivity_assumed",!live},{"exclusive_writer_verified",live},
                    {"real_order_attempts",c.ledger.submissions},{"attributed_fills",c.ledger.fill_receipts},{"net_SOL",c.ledger.net_lots/100.0},
                    {"reconciled",c.ledger.reconciled},{"halt",c.ledger.halt},{"feature_packets",model_packets},
                    {"public_book",{{"bid",venue.top().bid},{"ask",venue.top().ask},{"source_ms",venue.top().source_ms}}},
                    {"plan_cpu",plan_latency.summary()},{"public_receipt_to_plan",receipt_latency.summary()},
                    {"account_snapshot_age_ms",c.ledger.account_ms?J(now-c.ledger.account_ms):J(nullptr)},
                    {"drawdown_stop",nullptr},{"allocated_margin_cap_usdc",60},{"opening_notional_cap_usd",1200}}.dump()+"\n");health=now;
            }
        }
        finish();cancel_known();
        if(live){auto final=px::snapshot(true);final["revision"]=c.reconciliation_revision();c.reconcile(final,wall());save();
            record({{"kind","final_read_only_reconciliation"},{"data",final},{"at_ms",wall()}});}
        auto result=J{{"mode",live?"operator_live":"observe_no_orders"},{"status",rt.failure.empty()?"stopped":"connection_failed"},
            {"reason",rt.failure},{"updated_ms",wall()},{"last_heartbeat_ms",last_heartbeat},{"events",events},{"native_plans",session->plans},
            {"policies_installed",installed},{"policy_rejected",policy_rejected},{"hypothetical_quotes",shadow_quotes},
            {"real_order_attempts",c.ledger.submissions},{"attributed_fills",c.ledger.fill_receipts},{"residual_SOL",c.ledger.net_lots/100.0},
            {"unresolved_owned_orders",c.ledger.any_open_order()},{"reconciled",c.ledger.reconciled},
            {"plan_cpu",plan_latency.summary()},{"public_receipt_to_plan",receipt_latency.summary()},
            {"model_responses_in_this_process",0},{"strategy_profitability_proven",false}};
        quant::atomic_file(root/"summary.json",result.dump()+"\n",true);audit.flush();
        std::cout<<result.dump()<<'\n';curl_global_cleanup();return rt.failure.empty()?0:2;
    }catch(const std::exception& e){
        try{finish();cancel_known();save();if(!root.empty())quant::atomic_file(root/"failure.json",J{{"reason",e.what()},{"mode",live?"operator_live":"observe_no_orders"},{"updated_ms",wall()}}.dump()+"\n",true);}catch(...){}
        std::cerr<<"muse-maker: "<<e.what()<<'\n';curl_global_cleanup();return 2;
    }
}
