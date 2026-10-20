// Bounded freezer decoding through the exact Nitro snapshot's geth dependency.
package main

import (
 "bufio"
 "encoding/json"
 "errors"
 "fmt"
 "io"
 "math/big"
 "os"
 "github.com/ethereum/go-ethereum/common"
 "github.com/ethereum/go-ethereum/common/hexutil"
 "github.com/ethereum/go-ethereum/core/types"
 "github.com/ethereum/go-ethereum/crypto"
 "github.com/ethereum/go-ethereum/rlp"
 "github.com/ethereum/go-ethereum/trie"
 "github.com/golang/snappy"
)

type Input struct {
 Chain uint64 `json:"chain"`
 Blocks []Record `json:"blocks"`
}
type Record struct {
 Number uint64 `json:"number"`
 Header []byte `json:"header"`
 Body []byte `json:"body"`
 Receipts []byte `json:"receipts"`
 Hash []byte `json:"hash"`
}
func unpack(raw []byte) ([]byte,error) {
 n,e:=snappy.DecodedLen(raw); if e!=nil{return nil,e}
 if n>16<<20{return nil,errors.New("DECOMPRESSION_BOUND")}
 return snappy.Decode(nil,raw)
}
func decode(in Input) ([]map[string]any,error) {
 if (in.Chain!=42161 && in.Chain!=4663)||len(in.Blocks)==0||len(in.Blocks)>16384{return nil,errors.New("INPUT_SCOPE")}
 out:=make([]map[string]any,0,len(in.Blocks));var parent common.Hash;totalDecoded:=0
 signer:=types.LatestSignerForChainID(new(big.Int).SetUint64(in.Chain))
 for k,rec:=range in.Blocks {
  hraw,e:=unpack(rec.Header);if e!=nil{return nil,e}
  braw,e:=unpack(rec.Body);if e!=nil{return nil,e}
  rraw,e:=unpack(rec.Receipts);if e!=nil{return nil,e}
  totalDecoded+=len(hraw)+len(braw)+len(rraw);if totalDecoded>128<<20{return nil,errors.New("BATCH_DECOMPRESSION_BOUND")}
  var h types.Header;var body types.Body;var stored []*types.ReceiptForStorage
  if e=rlp.DecodeBytes(hraw,&h);e!=nil{return nil,fmt.Errorf("HEADER_RLP: %w",e)}
  if h.Number==nil||!h.Number.IsUint64()||h.Number.Uint64()!=rec.Number{return nil,errors.New("HEADER_NUMBER")}
  if len(rec.Hash)!=32||h.Hash()!=common.BytesToHash(rec.Hash){return nil,errors.New("HEADER_HASH")}
  if k>0&&(rec.Number!=in.Blocks[k-1].Number+1||h.ParentHash!=parent){return nil,errors.New("HEADER_PARENT")};parent=h.Hash()
  if e=rlp.DecodeBytes(braw,&body);e!=nil{return nil,fmt.Errorf("BODY_RLP: %w",e)}
  if e=rlp.DecodeBytes(rraw,&stored);e!=nil{return nil,fmt.Errorf("RECEIPT_RLP: %w",e)}
  if len(stored)!=len(body.Transactions){return nil,errors.New("RECEIPT_COUNT")}
  if types.DeriveSha(types.Transactions(body.Transactions),trie.NewStackTrie(nil))!=h.TxHash{return nil,errors.New("TRANSACTION_ROOT")}
  if types.CalcUncleHash(body.Uncles)!=h.UncleHash{return nil,errors.New("UNCLE_ROOT")}
  receipts:=make(types.Receipts,len(stored));txs:=make([]map[string]any,0,len(stored));logs:=make([]map[string]any,0);logIndex:=0
  for i,tx:=range body.Transactions {
   r:=(*types.Receipt)(stored[i]);receipts[i]=r;r.Type=tx.Type();r.Bloom=types.CreateBloom(r)
   if r.Status>1{return nil,errors.New("RECEIPT_STATUS")}
   sender,e:=types.Sender(signer,tx);if e!=nil{return nil,fmt.Errorf("SENDER: %w",e)}
   var to any;var created any;if tx.To()!=nil{to=tx.To().Hex()}
   if r.ContractAddress!=(common.Address{}){created=r.ContractAddress.Hex()}else if tx.To()==nil&&r.Status==1&&tx.Type()<types.ArbitrumDepositTxType{created=crypto.CreateAddress(sender,tx.Nonce()).Hex()}
   txs=append(txs,map[string]any{"hash":tx.Hash().Hex(),"transactionIndex":i,"from":sender.Hex(),"to":to,"value":tx.Value().String(),"status":r.Status,"contractAddress":created,"input":hexutil.Encode(tx.Data()),"type":tx.Type(),"nonce":tx.Nonce()})
   for _,l:=range r.Logs {
    topics:=make([]string,len(l.Topics));for n,t:=range l.Topics{topics[n]=t.Hex()}
    logs=append(logs,map[string]any{"transactionIndex":i,"transactionHash":tx.Hash().Hex(),"logIndex":logIndex,"address":l.Address.Hex(),"topics":topics,"data":hexutil.Encode(l.Data),"removed":false});logIndex++
   }
  }
  if types.DeriveSha(receipts,trie.NewStackTrie(nil))!=h.ReceiptHash{return nil,errors.New("RECEIPT_ROOT")}
  if types.MergeBloom(receipts)!=h.Bloom{return nil,errors.New("LOG_BLOOM")}
  out=append(out,map[string]any{"header":map[string]any{"number":rec.Number,"hash":h.Hash().Hex(),"parentHash":h.ParentHash.Hex(),"timestamp":h.Time},"transactions":txs,"logs":logs,"traces":[]any{},"trace_boundary":"not_available_from_freezer","snapshot_roots_verified":true,"independent_consensus_verified":false})
 }
 return out,nil
}
func main(){
 scanner:=bufio.NewScanner(io.LimitReader(os.Stdin,64<<20));scanner.Buffer(make([]byte,65536),64<<20)
 writer:=bufio.NewWriter(os.Stdout);defer writer.Flush()
 for scanner.Scan(){
  var input Input;var err error
  if err=json.Unmarshal(scanner.Bytes(),&input);err==nil {
   var blocks []map[string]any;blocks,err=decode(input)
   if err==nil{err=json.NewEncoder(writer).Encode(map[string]any{"blocks":blocks,"root_checks":true,"consensus_finality":false})}
  }
  if err!=nil{fmt.Fprintln(os.Stderr,err);os.Exit(1)}
 }
 if err:=scanner.Err();err!=nil{fmt.Fprintln(os.Stderr,err);os.Exit(1)}
}
