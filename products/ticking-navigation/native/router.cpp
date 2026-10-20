// Economic Machine product adapter: bounded read-only command selection.
// No network, model, wallet, process creation or per-request heap allocation.
#include <algorithm>
#include <array>
#include <cstdint>
#include <cstring>
#include <string_view>
#include "commands.generated.hpp"
using S=std::string_view;
struct Plan { uint32_t abi, count, ids[4]; };
static S trim(S s) { while(!s.empty() && s.front()==' ')s.remove_prefix(1); while(!s.empty() && s.back()==' ')s.remove_suffix(1); return s; }
static S unwrap(S s) {
  static constexpr S prefixes[]={"can you please ","could you please ","can you ","could you ","please ","show me ","show ","open ","view ","find ","add ","draw ","plot ","display ","use ","switch to ","이 차트에 ","현재 차트에 ","차트에 ","좀 ","the ","a ","an "};
  static constexpr S suffixes[]={" 보여줄래요"," 띄워줄래요"," 보여줄래"," 띄워줄래","보여주세요","띄워주세요","열어주세요","추가해주세요","보여줘","띄워줘","열어줘","넣어줘","추가해줘","그려줘","그려봐","표시해줘","바꿔줘","전환해줘","켜줘"," please"};
  for(int n=0;n<5;++n) { bool changed=false;for(auto p:prefixes)if(s.starts_with(p)){s=trim(s.substr(p.size()));changed=true;break;}if(!changed)break; }
  for(auto p:suffixes)if(s.ends_with(p)){s=trim(s.substr(0,s.size()-p.size()));for(auto x:{S(" 좀")})if(s.ends_with(x)){s=trim(s.substr(0,s.size()-x.size()));break;}break;}
  return s;
}
static int lookup(S s) {
 auto it=std::lower_bound(std::begin(PHRASES),std::end(PHRASES),s,[](const Phrase&a,S b){return a.text<b;});
 return it!=std::end(PHRASES)&&it->text==s?int(it->id):-1;
}
static bool append(Plan&p,uint32_t id,bool chart) {
 if(p.count==4 || (COMMAND_KINDS[id] && !chart))return false;
 // Navigation/conversation must be a single request, never mixed with edits.
 if(p.count && (!COMMAND_KINDS[id] || !COMMAND_KINDS[p.ids[0]]))return false;
 p.ids[p.count++]=id;return true;
}
static bool parse(S raw,Plan&p,bool chart,int depth=0) {
 if(depth>4)return false;
 S q=trim(raw);int id=lookup(q);if(id>=0)return append(p,id,chart);q=unwrap(q);id=lookup(q);
 if(id>=0)return append(p,id,chart);
 for(auto particle:{S("으로"),S("로"),S("을"),S("를")})if(q.ends_with(particle)){int candidate=lookup(trim(q.substr(0,q.size()-particle.size())));if(candidate>=0)return append(p,candidate,chart);}
 for(auto delimiter:{S(";"),S(","),S(" 그리고 "),S(" and ")}) {
  auto pos=q.find(delimiter);if(pos!=S::npos){Plan copy=p;if(parse(q.substr(0,pos),copy,chart,depth+1)&&parse(q.substr(pos+delimiter.size()),copy,chart,depth+1)){p=copy;return true;}return false;}
 }
 // Ordered interval + action; executor waits for candles in that interval.
 if(chart) {
  for(size_t pos=q.find(' ');pos!=S::npos;pos=q.find(' ',pos+1)) {
   int tf=lookup(q.substr(0,pos));if(tf>=0&&COMMAND_KINDS[tf]==2) {Plan copy=p;if(append(copy,tf,true)&&parse(q.substr(pos+1),copy,true,depth+1)&&copy.count>p.count+1){p=copy;return true;}}
  }
  for(auto delim:{S(" on "),S(" at ")}){auto pos=q.rfind(delim);if(pos!=S::npos){int tf=lookup(q.substr(pos+delim.size()));if(tf>=0&&COMMAND_KINDS[tf]==2){Plan copy=p;if(append(copy,tf,true)&&parse(q.substr(0,pos),copy,true,depth+1)){p=copy;return true;}}}}
 }
 return false;
}
extern "C" uint32_t em_ticking_abi(){return 1;}
extern "C" uint32_t em_ticking_capabilities(){return std::size(COMMAND_KINDS);}
extern "C" int em_ticking_resolve(const char*input,size_t size,int chart,Plan*out) noexcept {
 if(!input||!out||size==0||size>1600)return -1;
 std::array<char,1601> buffer{};size_t n=0;
 for(size_t i=0;i<size;++i){unsigned char c=input[i];if(c==0||c==127||(c<32&&c!='\t'&&c!='\r'&&c!='\n'))return -1;
  if(c=='\t'||c=='\r'||c=='\n')c=' ';if(c==' '&&(n==0||buffer[n-1]==' '))continue;if(c>='A'&&c<='Z')c+=32;buffer[n++]=char(c);}
 S q=trim(S(buffer.data(),n));while(!q.empty()&&(q.back()=='.'||q.back()=='!'||q.back()=='?'))q=trim(q.substr(0,q.size()-1));
 Plan p{1,0,{0,0,0,0}};if(!parse(q,p,chart!=0))return 0;
 *out=p;return 1;
}

// A position request is a typed workflow, never a lossy navigation match.
// Consume every token. Unknown constraints go to interpretation, not partial execution.
struct Position { uint32_t abi, asset, interval, side, style; };
static bool one(S w,std::initializer_list<S> words){for(auto x:words)if(w==x)return true;return false;}
static int interval_id(S w) {
 int id=lookup(w);if(id>=0&&COMMAND_KINDS[id]==2)return id;
 size_t n=0;while(n<w.size()&&w[n]>='0'&&w[n]<='9')++n;
 if(!n||n>4)return -1;S suffix=w.substr(n);char unit=0;
 if(one(suffix,{"min","mins","minute","minutes","분","분봉"}))unit='m';
 if(one(suffix,{"hr","hrs","hour","hours","시간","시간봉"}))unit='h';
 if(one(suffix,{"second","seconds","sec","초","초봉"}))unit='s';
 if(one(suffix,{"day","days","일","일봉"}))unit='d';
 if(!unit)return -1;std::array<char,6> text{};std::memcpy(text.data(),w.data(),n);text[n]=unit;
 id=lookup(S(text.data(),n+1));return id>=0&&COMMAND_KINDS[id]==2?id:-1;
}
extern "C" int em_ticking_position(const char*input,size_t size,Position*out) noexcept {
 if(!input||!out||!size||size>1600)return -1;
 std::array<char,1601> buffer{};for(size_t i=0;i<size;i++){unsigned char c=input[i];if(c==0||c==127||(c<32&&c!='\t'&&c!='\n'&&c!='\r'))return -1;if(c>='A'&&c<='Z')c+=32;if(c=='\t'||c=='\n'||c=='\r')c=' ';buffer[i]=c;}
 S q=trim(S(buffer.data(),size));while(!q.empty()&&one(q.substr(q.size()-1),{".","!","?"}))q=trim(q.substr(0,q.size()-1));
 std::array<S,80> words{};size_t count=0;while(!q.empty()){if(count==words.size())return 0;auto pos=q.find(' ');words[count++]=q.substr(0,pos);if(pos==S::npos)break;q=trim(q.substr(pos+1));}
 Position p{1,UINT32_MAX,UINT32_MAX,0,0};bool intent=false;
 for(size_t i=0;i<count;i++){
  S w=words[i];if(one(w,{"position","positions","포지션","포지션을","strategy","strategies","전략","전략을"})){intent=true;continue;}
  if(one(w,{"scalp","scalping","스캘핑","스켈핑","단타"})){if(p.style&&p.style!=1)return 0;p.style=1;continue;}
  if(one(w,{"intraday","daytrading"})){if(p.style&&p.style!=2)return 0;p.style=2;continue;}
  if(one(w,{"swing","스윙"})){if(p.style&&p.style!=3)return 0;p.style=3;continue;}
  if(one(w,{"long","롱"})){if(p.side==2)return 0;p.side=1;continue;}
  if(one(w,{"short","숏"})){if(p.side==1)return 0;p.side=2;continue;}
  int tf=interval_id(w);
  if(tf<0&&i+1<count&&w.find_first_not_of("0123456789")==S::npos&&w.size()<5){std::array<char,32> tmp{};if(w.size()+words[i+1].size()<tmp.size()){std::memcpy(tmp.data(),w.data(),w.size());std::memcpy(tmp.data()+w.size(),words[i+1].data(),words[i+1].size());tf=interval_id(S(tmp.data(),w.size()+words[i+1].size()));if(tf>=0)++i;}}
  if(tf>=0){if(p.interval!=UINT32_MAX&&p.interval!=uint32_t(tf))return 0;p.interval=tf;continue;}
  if(one(w,{"plan","planning","setup","set","up","prepare","build","create","a","an","the","my","on","in","for","at","with","and","i","will","want","to","do","please","can","you","chart","graph","timeframe","current","this","계획","계획해줘","계획해","만들어줘","그려줘","차트","그래프","준비해줘","좀","기준으로","어떻게","해야할지","짜봐","짜줘","짜","짜주세요","세워줘","세워봐","what","should","use","trading"}))continue;
  int asset=-1;size_t used=0;
  // Longest exact catalog phrase first; no substring symbol matches.
  for(size_t n=std::min(size_t(4),count-i);n>0;--n){S name(w.data(),words[i+n-1].data()+words[i+n-1].size()-w.data());int id=lookup(name);if(id>=0&&COMMAND_ASSETS[id]){asset=id;used=n;break;}}
  if(asset<0||p.asset!=UINT32_MAX)return 0;p.asset=asset;i+=used-1;
 }
 if(!intent)return 0;*out=p;return 1;
}
