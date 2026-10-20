#include "archive_queue.hpp"
#include "blob_policy.hpp"
#include <curl/curl.h>
#include <nlohmann/json.hpp>
#include <iostream>
#include <regex>
using namespace ticking::evm;using json=nlohmann::json;
namespace {
curl_off_t transfer_limit=8*1024*1024;
struct Curl{CURL*p{curl_easy_init()};~Curl(){curl_easy_cleanup(p);}};
struct Headers{curl_slist*p{};~Headers(){curl_slist_free_all(p);}void add(const std::string&s){p=curl_slist_append(p,s.c_str());need(p,"HTTP headers");}};
struct Response{long status{};Bytes body;std::string error_code;};
Response request(const std::string&url,const std::string&method,View payload,const std::vector<std::string>&headers,size_t cap,bool imds=false){Curl c;need(c.p,"curl init");Headers hs;for(auto&s:headers)hs.add(s);Response response;
 if(!imds){curl_easy_setopt(c.p,CURLOPT_MAX_SEND_SPEED_LARGE,transfer_limit);curl_easy_setopt(c.p,CURLOPT_MAX_RECV_SPEED_LARGE,transfer_limit);}
 curl_easy_setopt(c.p,CURLOPT_HEADERDATA,&response);
 curl_easy_setopt(c.p,CURLOPT_HEADERFUNCTION,+[](char*p,size_t size,size_t n,void*arg)->size_t{
  if(n&&size>SIZE_MAX/n)return 0;size_t bytes=size*n;
  auto&r=*static_cast<Response*>(arg);
  try{
   // Ignore all other response headers; never retain tokens or full error bodies.
   if(bytes>=5&&curl_strnequal(p,"HTTP/",5))r.error_code.clear();
   constexpr std::string_view key="x-ms-error-code:";
   if(bytes>=key.size()&&bytes<=256&&curl_strnequal(p,key.data(),key.size())){
    std::string_view value(p+key.size(),bytes-key.size());
    while(!value.empty()&&(value.front()==' '||value.front()=='\t'))value.remove_prefix(1);
    while(!value.empty()&&(value.back()=='\r'||value.back()=='\n'||value.back()==' '||value.back()=='\t'))value.remove_suffix(1);
    if(value.size()<=128&&std::all_of(value.begin(),value.end(),[](char ch){return (ch>='a'&&ch<='z')||(ch>='A'&&ch<='Z')||(ch>='0'&&ch<='9');}))r.error_code=value;
   }
   return bytes;
  }catch(...){return 0;}
 });
 curl_easy_setopt(c.p,CURLOPT_URL,url.c_str());curl_easy_setopt(c.p,CURLOPT_CUSTOMREQUEST,method.c_str());curl_easy_setopt(c.p,CURLOPT_HTTPHEADER,hs.p);curl_easy_setopt(c.p,CURLOPT_CONNECTTIMEOUT,5L);curl_easy_setopt(c.p,CURLOPT_TIMEOUT,60L);curl_easy_setopt(c.p,CURLOPT_FOLLOWLOCATION,0L);curl_easy_setopt(c.p,CURLOPT_SSL_VERIFYPEER,1L);curl_easy_setopt(c.p,CURLOPT_SSL_VERIFYHOST,2L);curl_easy_setopt(c.p,CURLOPT_PROTOCOLS_STR,imds?"http":"https");if(imds)curl_easy_setopt(c.p,CURLOPT_PROXY,"");if(method=="PUT"){curl_easy_setopt(c.p,CURLOPT_POSTFIELDS,payload.data());curl_easy_setopt(c.p,CURLOPT_POSTFIELDSIZE_LARGE,curl_off_t(payload.size()));}
 struct Writer{Bytes*body;size_t cap;}writer{&response.body,cap};curl_easy_setopt(c.p,CURLOPT_WRITEDATA,&writer);curl_easy_setopt(c.p,CURLOPT_WRITEFUNCTION,+[](char*p,size_t size,size_t n,void*arg)->size_t{auto&w=*static_cast<Writer*>(arg);if(n&&size>SIZE_MAX/n)return 0;size_t bytes=size*n;if(bytes>w.cap-w.body->size())return 0;try{w.body->insert(w.body->end(),p,p+bytes);return bytes;}catch(...){return 0;}});auto result=curl_easy_perform(c.p);need(result==CURLE_OK,"archive HTTP transport failed");curl_easy_getinfo(c.p,CURLINFO_RESPONSE_CODE,&response.status);return response;
}
std::string token(const json&cfg){if(cfg.value("auth",std::string("managed_identity"))=="managed_identity"){auto r=request("http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fstorage.azure.com%2F","GET",{},{"Metadata: true"},65536,true);need(r.status==200,"Blob managed identity unavailable");auto j=json::parse(r.body);return j.at("access_token");}need(cfg.at("auth")=="bearer_file","unsupported Blob authentication");std::filesystem::path p=cfg.at("bearerFile").get<std::string>();struct stat st{};need(lstat(p.c_str(),&st)==0&&S_ISREG(st.st_mode)&&(st.st_mode&077)==0&&st.st_uid==geteuid(),"Blob bearer file permissions");auto b=load_file(p,16384);std::string t(b.begin(),b.end());while(!t.empty()&&(t.back()=='\n'||t.back()=='\r'))t.pop_back();need(!t.empty()&&t.find_first_of("\r\n ")==t.npos,"Blob bearer format");return t;}
}
std::string object_prefix(const json&cfg){auto p=cfg.value("objectPrefix",std::string("sha256"));need(p=="research/pump/sha256","Blob object namespace");return p;}
json archive_file(const json&cfg,View raw){std::string account=cfg.at("account"),container=cfg.at("container");need(std::regex_match(account,std::regex("[a-z0-9]{3,24}"))&&std::regex_match(container,std::regex("[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"))&&container.find("--")==container.npos,"Blob account/container");need(!raw.empty(),"empty archive input");auto digest=hex(sha256(raw));auto name=object_prefix(cfg)+"/"+digest.substr(0,2)+"/"+digest;auto url="https://"+account+".blob.core.windows.net/"+container+"/"+name;auto bearer=token(cfg);need(bearer.find_first_of("\r\n")==bearer.npos,"Blob token header");time_t now=time(nullptr);tm utc{};gmtime_r(&now,&utc);char date[64];need(strftime(date,sizeof(date),"%a, %d %b %Y %H:%M:%S GMT",&utc)>0,"archive date");std::vector<std::string> headers{"Authorization: Bearer "+bearer,"x-ms-version: 2023-11-03",std::string("x-ms-date: ")+date};auto putheaders=headers;putheaders.push_back("x-ms-blob-type: BlockBlob");putheaders.push_back("If-None-Match: *");putheaders.push_back("x-ms-meta-sha256: "+digest);auto proof=archive_and_restore(raw,[&](auto&,View bytes){auto r=request(url,"PUT",bytes,putheaders,65536);return BlobPutResult{r.status,std::move(r.error_code)};},[&](auto&){auto r=request(url,"GET",{},headers,raw.size());return std::make_pair(r.status,std::move(r.body));});json receipt={{"schema",1},{"account",account},{"container",container},{"object",name},{"sha256",digest},{"bytes",raw.size()},{"uploadStatus",proof.put_status},{"fullDownloadVerified",true},{"localDeletionAllowed",false},{"verifiedAtUnix",time(nullptr)}};return receipt;}
json restore_file(const json&cfg,const std::string&digest,uint64_t bytes,const std::filesystem::path&destination){
 need(std::regex_match(digest,std::regex("[0-9a-f]{64}"))&&bytes>0&&bytes<=64*1024*1024,"Blob restore identity");
 std::string account=cfg.at("account"),container=cfg.at("container");
 need(std::regex_match(account,std::regex("[a-z0-9]{3,24}"))&&std::regex_match(container,std::regex("[a-z0-9][a-z0-9-]{1,61}[a-z0-9]"))&&container.find("--")==container.npos,"Blob account/container");
 auto url="https://"+account+".blob.core.windows.net/"+container+"/"+object_prefix(cfg)+"/"+digest.substr(0,2)+"/"+digest;
 auto bearer=token(cfg);need(bearer.find_first_of("\r\n")==bearer.npos,"Blob token header");
 auto r=request(url,"GET",{},{"Authorization: Bearer "+bearer,"x-ms-version: 2023-11-03"},bytes);
 need(r.status==200&&r.body.size()==bytes&&hex(sha256(r.body))==digest,"Blob restored bytes mismatch");
 // seal_file refuses an existing different object and fsyncs the new copy.
 seal_file(destination,r.body);
 return {{"schema",1},{"sha256",digest},{"bytes",bytes},{"fullDownloadVerified",true},{"localDeletionAllowed",false}};
}
int main(int argc,char**argv){try{
 bool restore=argc>1&&std::string(argv[1])=="--restore";
 need((restore&&argc==6)||(!restore&&argc==4),"usage: blob-archive config sealed-file receipt-file OR --sweep config recovery-dir OR --restore config digest destination bytes");
 umask(0077);need(curl_global_init(CURL_GLOBAL_DEFAULT)==CURLE_OK,"curl global init");
 bool sweep=std::string(argv[1])=="--sweep";json cfg;std::ifstream((sweep||restore)?argv[2]:argv[1])>>cfg;
 transfer_limit=cfg.value("maxBytesPerSecond",8*1024*1024LL);
 need(transfer_limit>=1024*1024&&transfer_limit<=32*1024*1024,"Blob transfer rate bound");
 if(restore){std::cout<<restore_file(cfg,argv[3],std::stoull(argv[5]),argv[4]).dump()<<"\n";}
 else if(sweep){std::filesystem::path root=argv[3];ArchiveQueue queue(root);std::filesystem::create_directories(root/"archive-receipts");for(auto&item:queue.pending()){auto raw=load_file(root/"objects"/item.object);need(raw.size()==item.bytes&&hex(sha256(raw))==item.digest,"local archive source mismatch");auto receipt=archive_file(cfg,raw);auto text=receipt.dump(2);auto receipt_file=root/"archive-receipts"/(item.digest+"-"+std::to_string(time(nullptr))+".json");seal_file(receipt_file,View((const uint8_t*)text.data(),text.size()));queue.restored(item,raw);std::cout<<receipt.dump()<<"\n";}}
 else{auto raw=load_file(argv[2]);auto receipt=archive_file(cfg,raw);auto text=receipt.dump(2);seal_file(argv[3],View((const uint8_t*)text.data(),text.size()));std::cout<<receipt.dump()<<"\n";}
 curl_global_cleanup();return 0;
}catch(const std::exception&e){std::cerr<<e.what()<<"\n";return 1;}}
