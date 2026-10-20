package main
import (
 "math/big"
 "testing"
 "github.com/ethereum/go-ethereum/common"
 "github.com/ethereum/go-ethereum/core/types"
 "github.com/ethereum/go-ethereum/crypto"
 "github.com/ethereum/go-ethereum/rlp"
 "github.com/ethereum/go-ethereum/trie"
 "github.com/golang/snappy"
)
func fixture(t *testing.T) Input {
 key,e:=crypto.HexToECDSA("0000000000000000000000000000000000000000000000000000000000000001");if e!=nil{t.Fatal(e)}
 to:=common.HexToAddress("0x0000000000000000000000000000000000000002")
 tx,e:=types.SignNewTx(key,types.LatestSignerForChainID(big.NewInt(42161)),&types.LegacyTx{Nonce:0,To:&to,Gas:21000,GasPrice:big.NewInt(1),Value:big.NewInt(5)});if e!=nil{t.Fatal(e)}
 receipt:=&types.Receipt{Status:1,CumulativeGasUsed:21000,Logs:[]*types.Log{}}
 txs:=types.Transactions{tx};rs:=types.Receipts{receipt}
 h:=&types.Header{Number:big.NewInt(0),Difficulty:big.NewInt(0),UncleHash:types.EmptyUncleHash,TxHash:types.DeriveSha(txs,trie.NewStackTrie(nil)),ReceiptHash:types.DeriveSha(rs,trie.NewStackTrie(nil)),GasUsed:21000,GasLimit:30000000}
 hr,_:=rlp.EncodeToBytes(h);br,_:=rlp.EncodeToBytes(&types.Body{Transactions:txs});rr,_:=rlp.EncodeToBytes([]*types.ReceiptForStorage{(*types.ReceiptForStorage)(receipt)})
 return Input{Chain:42161,Blocks:[]Record{{Number:0,Header:snappy.Encode(nil,hr),Body:snappy.Encode(nil,br),Receipts:snappy.Encode(nil,rr),Hash:h.Hash().Bytes()}}}
}
func TestValidRootsAndSender(t *testing.T){in:=fixture(t);out,e:=decode(in);if e!=nil{t.Fatal(e)};tx:=out[0]["transactions"].([]map[string]any)[0];if tx["from"]!="0x7E5F4552091A69125d5DfCb7b8C2659029395Bdf"||tx["value"]!="5"{t.Fatal(tx)}}
func TestRejectHash(t *testing.T){in:=fixture(t);in.Blocks[0].Hash[0]^=1;if _,e:=decode(in);e==nil{t.Fatal("accepted hash")}}
func TestRejectBodyRoot(t *testing.T){in:=fixture(t);b,_:=rlp.EncodeToBytes(&types.Body{});in.Blocks[0].Body=snappy.Encode(nil,b);if _,e:=decode(in);e==nil{t.Fatal("accepted body")}}
func TestRejectReceiptRoot(t *testing.T){in:=fixture(t);r:=&types.Receipt{Status:0,CumulativeGasUsed:21000};b,_:=rlp.EncodeToBytes([]*types.ReceiptForStorage{(*types.ReceiptForStorage)(r)});in.Blocks[0].Receipts=snappy.Encode(nil,b);if _,e:=decode(in);e==nil{t.Fatal("accepted receipt")}}
func TestRejectNumber(t *testing.T){in:=fixture(t);in.Blocks[0].Number=1;if _,e:=decode(in);e==nil{t.Fatal("accepted number")}}
func TestRejectChain(t *testing.T){in:=fixture(t);in.Chain=4663;if _,e:=decode(in);e==nil{t.Fatal("accepted wrong chain")}}
func TestRejectSnappy(t *testing.T){in:=fixture(t);in.Blocks[0].Body=[]byte{255};if _,e:=decode(in);e==nil{t.Fatal("accepted compression")}}
func TestRejectOversizedWindow(t *testing.T){in:=Input{Chain:42161,Blocks:make([]Record,16385)};if _,e:=decode(in);e==nil||e.Error()!="INPUT_SCOPE"{t.Fatal(e)}}
