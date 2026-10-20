// A native Economic Machine subscriber: observes fixed packets, never signs.
#include "machine/economics/pump_signal.hpp"
#include <array>
#include <chrono>
#include <cstring>
#include <iostream>
#include <poll.h>
#include <stdexcept>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>
#include <vector>
uint64_t le(const unsigned char* p,size_t n){uint64_t v=0;for(size_t i=0;i<n;i++)v|=uint64_t(p[i])<<(8*i);return v;}
uint64_t ms(){return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::system_clock::now().time_since_epoch()).count();}
int main(int argc,char** argv) {
    if(argc!=3)return 2;
    int duration=std::stoi(argv[2]);if(duration<1 || duration>300)return 2;
    sockaddr_un address{};address.sun_family=AF_UNIX;
    if(std::strlen(argv[1])>=sizeof(address.sun_path))return 2;
    std::strcpy(address.sun_path,argv[1]);int fd=::socket(AF_UNIX,SOCK_SEQPACKET|SOCK_CLOEXEC,0);
    if(fd<0 || ::connect(fd,reinterpret_cast<sockaddr*>(&address),sizeof(address)))return 3;
    auto until=std::chrono::steady_clock::now()+std::chrono::seconds(duration);
    uint64_t received=0,decisions=0,revocations=0,version=0,expired=0,errors=0,candidates=0;
    std::vector<uint64_t> latency;latency.reserve(4096);
    while(std::chrono::steady_clock::now()<until && received<4096) {
        pollfd pfd{fd,POLLIN,0};int ready=::poll(&pfd,1,100);
        if(ready<0){++errors;break;}if(!ready)continue;
        if(pfd.revents&(POLLERR|POLLHUP|POLLNVAL)){++errors;break;}
        std::array<unsigned char,233> b{};auto size=::recv(fd,b.data(),b.size(),0);auto now=ms();
        if(size!=232 || std::memcmp(b.data(),"EMPS",4) || le(b.data()+4,4)!=1 || le(b.data()+12,4)!=1){++errors;break;}
        auto kind=le(b.data()+8,4),at=le(b.data()+16,8),end=le(b.data()+24,8);
        if((kind!=1 && kind!=2) || end<=at || end-at>1000){++errors;break;}
        std::array<uint64_t,17> r{};for(size_t i=0;i<17;i++)r[i]=le(b.data()+32+i*8,8);
        if(r[0]>machine::economics::pump::invalidated || r[15] || r[16]){++errors;break;}
        ++received;version=std::max(version,r[13]);
        if(now<at || now>=end){++expired;continue;}
        latency.push_back(now-at);if(kind==2)++revocations;else {++decisions;if(!r[0])++candidates;}
    }
    ::close(fd);std::sort(latency.begin(),latency.end());
    std::cout<<"{\"wire_bytes\":232,\"received\":"<<received<<",\"fresh_decisions\":"<<decisions
        <<",\"revocations\":"<<revocations<<",\"candidate_messages\":"<<candidates<<",\"expired\":"<<expired
        <<",\"errors\":"<<errors<<",\"highest_policy_version\":"<<version
        <<",\"publication_to_subscriber_ms_p50\":"<<(latency.empty()?0:latency[(latency.size()-1)/2])
        <<",\"publication_to_subscriber_ms_max\":"<<(latency.empty()?0:latency.back())
        <<",\"timestamp_resolution_ms\":1,\"orders_submitted\":0}\n";
    return errors || !received ? 4 : 0;
}
