#pragma once
#include "session.hpp"
#include <sys/stat.h>
#include <fcntl.h>
#include <unistd.h>

namespace muse_session {
inline J control_read(const std::filesystem::path& path,std::size_t bound=32768,bool private_file=true) {
    int fd=::open(path.c_str(),O_RDONLY|O_NOFOLLOW|O_CLOEXEC);require(fd>=0,"control file unavailable");
    struct Close{int fd;~Close(){::close(fd);}} close{fd};
    struct stat st{};require(fstat(fd,&st)==0&&S_ISREG(st.st_mode)&&st.st_size>0&&st.st_size<=static_cast<off_t>(bound),"control file shape");
    require(!private_file||(st.st_uid==geteuid()&&!(st.st_mode&0077)),"control file permissions/owner");
    std::string bytes(st.st_size,'\0');std::size_t read=0;
    while(read<bytes.size()){auto n=::read(fd,bytes.data()+read,bytes.size()-read);require(n>0,"control file read");read+=n;}
    std::vector<std::set<std::string>> keys;
    return J::parse(bytes,[&](int,J::parse_event_t event,J& value){
        if(event==J::parse_event_t::object_start)keys.emplace_back();
        else if(event==J::parse_event_t::key)require(keys.back().insert(value.get<std::string>()).second,"duplicate control key");
        else if(event==J::parse_event_t::object_end)keys.pop_back();
        return true;
    });
}
inline std::string sha256(const std::string& s) {
    std::array<unsigned char,32> bytes{};unsigned n=0;
    require(EVP_Digest(s.data(),s.size(),bytes.data(),&n,EVP_sha256(),nullptr)==1&&n==32,"policy digest");
    const char* hex="0123456789abcdef";std::string result;
    for(auto b:bytes){result+=hex[b>>4];result+=hex[b&15];}return result;
}
inline bool hash_string(const J& j) {
    if(!j.is_string())return false;auto s=j.get<std::string>();
    return s.size()==64&&std::all_of(s.begin(),s.end(),[](char c){return (c>='0'&&c<='9')||(c>='a'&&c<='f');});
}
inline std::int64_t integer(const J& j) {
    require(j.is_number_integer()&&!j.is_boolean(),"policy integer type");
    if(j.is_number_unsigned())require(j.get<std::uint64_t>()<=INT64_MAX,"policy integer overflow");
    return j.get<std::int64_t>();
}
inline bool install_packet(Session& session,const J& j,const std::string& session_id,std::int64_t now,bool live) {
    require(j.size()==12&&j.at("schema")=="muse-policy-install-v1"&&j.at("session_id")==session_id,
            "policy session binding");
    require(j.at("model")=="meta/muse-spark-1.3-contributor"&&
        (j.at("kind")=="muse"||(!live&&j.at("kind")=="fixture")),"policy model/origin");
    auto version=integer(j.at("version"));require(version>0,"policy version");
    if(version<=session.policy_version())return false;
    const auto& p=j.at("policy");require(p.is_object()&&p.size()==8,"policy field count");
    mm::Mode mode;
    if(p.at("mode")=="quote")mode=mm::Mode::quote;
    else if(p.at("mode")=="reduce_only")mode=mm::Mode::reduce_only;
    else {require(p.at("mode")=="pause","policy mode");mode=mm::Mode::pause;}
    mm::Policy policy{mode,integer(p.at("half_spread_millibps")),integer(p.at("inventory_skew_millibps")),
        integer(p.at("flow_shift_millibps")),integer(p.at("volatility_multiplier_milli")),
        integer(p.at("clip_usd_micros")),integer(p.at("refresh_ticks")),integer(p.at("ttl_ms"))};
    require(mm::valid(policy)&&hash_string(j.at("policy_sha256"))&&j.at("policy_sha256")==sha256(p.dump())&&hash_string(j.at("context_sha256")),"policy schema/hash");
    require(policy.clip_usd_micros==600*scale&&policy.ttl_ms==60000,"owner 600 USD per-side / 60 second policy required");
    auto issued=integer(j.at("issued_ms")),published=integer(j.at("published_ms")),expires=integer(j.at("expires_ms"));
    require(issued>0&&issued<=published&&published<=now&&now<expires&&
        issued<=INT64_MAX-policy.ttl_ms&&expires==std::min(issued+policy.ttl_ms,session.lease().deadline),"policy deadline/TTL");
    const auto& gate=j.at("forward_gate");
    auto first=integer(gate.at("first_ms")),last=integer(gate.at("last_ms")),samples=integer(gate.at("samples"));
    require(gate.size()==5&&gate.at("kind")=="future_input_sanity"&&gate.at("profitability_proven")==false&&
        samples>=8&&samples<=1000&&first>issued&&last<=published&&last-first>=1000,"policy forward input gate");
    session.install(policy,issued,version,now);return true;
}
}
