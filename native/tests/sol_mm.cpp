#include "machine/economics/sol_mm.hpp"
#include <iostream>
#include <stdexcept>
#include <limits>
using namespace machine::economics::sol_mm;
static unsigned checks=0;
static void check(bool ok,const char* name){++checks;if(!ok)throw std::runtime_error(name);}
int main(){
    const Ns now=100*second;
    FairFilter f;
    check(f.observe({0,1,50,now,100,2,1000,true,true}),"executed observation admitted");
    check(!f.observe({0,1,50,now,100,2,1000,true,true}),"duplicate sequence rejected");
    check(!f.observe({1,1,50,now,100,2,1000,false,true}),"non-swap rejected");
    check(!f.observe({1,1,50,now,std::nan(""),2,1000,true,true}),"NaN price rejected");
    check(f.snapshot(now).pools==1,"pool count not message count");
    check(f.vault(0,50,now,1100),"independent vault update admitted");
    check(f.vault(0,50,now+1,1000)&&f.snapshot(now+1).liquidity_change<0,"non-swap liquidity proxy change observed");
    check(!f.vault(0,50,now,1200),"older vault write cannot replace fresh balance");
    check(f.snapshot(now+3*second).pools==0,"stale pool expires");
    f.observe({1,1,50,now,100.01,-1,1000,true,true});
    for(unsigned n=2;n<102;n++)f.observe({0,n,50,now+n,100,1,1000,true,true});
    check(f.snapshot(now+1000).pools==2,"chatty pool stays one vote");
    f.invalidate_fork(50);check(f.snapshot(now+1000).pools==0,"dead fork invalidates feature state");
    check(!f.observe({0,2,51,now+1000,100,0,1000,true,true}),"replayed sequence remains rejected after fork");
    Regression r;Features x{1,.1,.2,.3,.4,.5};
    for(int i=0;i<1000;++i)check(r.learn(x,.3),"RLS finite update");
    check(std::abs(r.predict(x)-.3)<.02,"RLS converges on constant synthetic relation");
    check(!r.learn(x,std::nan("")),"NaN outcome cannot poison weights");
    ForwardStudy forward;forward.observe(now,100,x);forward.observe(now+500'000'000,100.01,x);
    check(forward.scored==1,"future-only label matured");forward.gap();check(forward.missing==1,"gap censors pending labels");
    ConditionalMarkout markout;
    check(markout.fill(7,now,1,100,x),"own fill admitted");
    check(!markout.fill(7,now,1,100,x),"duplicate fill not trained twice");
    markout.mark(now+999'000'000,99);check(markout.forecast(x).labels==0,"no premature markout");
    markout.mark(now+second,99);check(markout.forecast(x).labels==1,"actual future markout");
    markout.fill(8,now+second,1,99,x);markout.mark(now+3*second,100);check(markout.missing==1,"late price is not exact-horizon label");
    Survival hazard;hazard.observe(10,false);check(!hazard.supported(),"cancel is exposure not fill");
    check(!hazard.observe(-1,true),"negative exposure rejected");
    auto q=queue_after(10,2,3,4);check(q.ahead_low==5&&q.ahead_high==12&&!q.exact_rank,"L2 interval not invented FIFO rank");
    Market book{now,99'990'000,100'010'000,10000,10000,10};
    Account a{1,now,60,60,60,20,0,0,0,true};
    ModelSnapshot m{1,now,100,1,0,0,0,1,.2,false,true};
    std::array<Resting,2> orders{};
    auto base=plan(now,book,a,m,orders);
    check(!base.market_orders_allowed,"no market operation in plan type");
    check(base.sides[0].action==Action::create&&base.sides[1].action==Action::create,"normal two-sided maker quotes");
    check(base.sides[0].price<book.ask&&base.sides[1].price>book.bid,"both sides passive");
    for(int i=0;i<2;++i)orders[i]={std::uint64_t(i+1),base.sides[i].price,base.sides[i].quantity,now,{},false,false};
    a.opening_reserved=orders[0].remaining+orders[1].remaining;
    auto keep=plan(now+1000,book,a,m,orders);
    check(keep.sides[0].action==Action::keep&&keep.sides[1].action==Action::keep,"unchanged quotes retain queue");
    m.fair+=.00001;auto tiny=plan(now+2000,book,a,m,orders);
    check(tiny.sides[0].action==Action::keep&&tiny.sides[1].action==Action::keep,"tiny fair movement does not churn");
    orders[0].pending=true;check(plan(now,book,a,m,orders).sides[0].action==Action::wait,"unknown/cancel pending cannot replace");
    orders[0].pending=false;a.net=500000;a.opening_reserved-=500000;orders[0].remaining-=500000;
    auto after_fill=plan(now,book,a,m,orders);
    check(after_fill.sides[1].action==Action::cancel&&after_fill.sides[1].reason==Reason::inventory,"opening ask converts to reduce-only after bid fill");
    orders={};a.net=5'550'000;a.available_margin=0;a.opening_reserved=0;
    auto exit=plan(now,book,a,m,orders);
    check(exit.sides[1].action==Action::create&&exit.sides[1].reduce_only,"long gets maker-only reduction with zero free margin");
    check(exit.sides[1].quantity<=a.net&&exit.sides[1].price>book.bid,"exit cannot reverse or cross");
    check(exit.sides[0].action!=Action::create,"no capacity for more inventory");
    a.net=550'000;auto partial=plan(now,book,a,m,orders);
    check(partial.sides[1].quantity<=550'000,"partial fill leaves bounded opposite order");
    a.closing_reserved=550'000;check(plan(now,book,a,m,orders).sides[1].action!=Action::create,"pending exit reserves full remaining position");
    a.closing_reserved=0;a.net=-550'000;auto short_exit=plan(now,book,a,m,orders);
    check(short_exit.sides[0].reduce_only&&short_exit.sides[0].quantity<=550'000,"short reduction is symmetric");
    check(!applicable(base,now,2),"account revision invalidates old plan");
    check(!applicable(base,now+second,1),"plan expiry checked without model search");
    a.net=0;a.available_margin=60;a.leverage=10;check(plan(now,book,a,m,{}).sides[0].action==Action::wait,"20x configuration mismatch visible");
    a.leverage=20;m.fair=std::nan("");check(plan(now,book,a,m,{}).sides[0].action==Action::wait,"NaN model not executable");
    std::cout<<"sol_mm_checks="<<checks<<" passed; synthetic correctness only; no orders\n";
}
