"""LayerZero V2 endpoint events, scoped by chain and recomputed GUID.

Contracts/eids are pinned to the official metadata snapshot retained in evidence.
Endpoint delivery is message evidence, not OFT amount or recipient wallet proof.
"""
from eth_hash.auto import keccak
from economic_machine.arbitrum_research.graph import _address,_data,_topic_address

REGISTRY={1:(30101,'0x1a44076050125825900e736c501f859c50fe728c'),42161:(30110,'0x1a44076050125825900e736c501f859c50fe728c'),4663:(30416,'0x6f475642a6e85809b1c36fa62763669b1b48dd5b')}
BY_EID={v[0]:k for k,v in REGISTRY.items()}
SENT='0x'+keccak(b'PacketSent(bytes,bytes,address)').hex()
DELIVERED='0x'+keccak(b'PacketDelivered((uint32,bytes32,uint64),address)').hex()

def guid(nonce,src,sender,dst,receiver):
    return '0x'+keccak(nonce.to_bytes(8,'big')+src.to_bytes(4,'big')+sender+dst.to_bytes(4,'big')+receiver).hex()
def array(raw,offset):
    if offset%32 or offset<96 or offset+32>len(raw):raise ValueError('LZ_OFFSET')
    size=int.from_bytes(raw[offset:offset+32],'big');end=offset+32+size;padded=(end+31)//32*32
    if padded>len(raw) or any(raw[end:padded]):raise ValueError('LZ_LENGTH')
    return raw[offset+32:end],padded

def decode(chain,log):
    if chain not in REGISTRY or _address(log['address'])!=REGISTRY[chain][1]:return None
    topics=log.get('topics',[])
    if not topics or topics[0].lower() not in (SENT,DELIVERED):return None
    if len(topics)!=1 or log.get('removed') is True:raise ValueError('LZ_TOPICS_OR_REMOVED')
    raw=_data(log['data']);eid=REGISTRY[chain][0]
    if topics[0].lower()==SENT:
        if len(raw)<160 or len(raw)%32:raise ValueError('LZ_SENT_ABI')
        first=int.from_bytes(raw[:32],'big');second=int.from_bytes(raw[32:64],'big')
        if first!=96:raise ValueError('LZ_FIRST_OFFSET')
        packet,end=array(raw,first);_,end2=array(raw,second)
        _topic_address('0x'+raw[64:96].hex())
        if end!=second or end2!=len(raw) or len(packet)<113 or packet[0]!=1:raise ValueError('LZ_PACKET_ENCODING')
        nonce=int.from_bytes(packet[1:9],'big');src=int.from_bytes(packet[9:13],'big');sender=packet[13:45]
        dst=int.from_bytes(packet[45:49],'big');receiver=packet[49:81];ident='0x'+packet[81:113].hex()
        if src!=eid or guid(nonce,src,sender,dst,receiver)!=ident:raise ValueError('LZ_GUID_OR_SOURCE')
        kind='layerzero_sent'
    else:
        if len(raw)!=128:raise ValueError('LZ_DELIVERED_ABI')
        src=int.from_bytes(raw[:32],'big');sender=raw[32:64];nonce=int.from_bytes(raw[64:96],'big');receiver=raw[96:128];dst=eid
        if src>=2**32 or nonce>=2**64:raise ValueError('LZ_INTEGER')
        ident=guid(nonce,src,sender,dst,receiver);kind='layerzero_delivered'
    if src not in BY_EID or dst not in BY_EID:return None
    return dict(child_chain=BY_EID[src],destination_chain=BY_EID[dst],kind=kind,message_id=ident,
        sender=_topic_address('0x'+sender.hex()),recipient=_topic_address('0x'+receiver.hex()),
        src_eid=src,dst_eid=dst,nonce=str(nonce),actor_kind='application_contract_not_wallet',protocol='LayerZeroV2')
