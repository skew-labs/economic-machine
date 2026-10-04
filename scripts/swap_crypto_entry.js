// Bundled on the remote builder. ethers 6.15.0, MIT licensed.
import {TypedDataEncoder, keccak256, toUtf8Bytes, Interface, recoverAddress} from 'ethers';
export const hashText = value => keccak256(toUtf8Bytes(value));
export function typedHash(data) {
  const types={...data.types}; delete types.EIP712Domain;
  return TypedDataEncoder.hash(data.domain,types,data.message);
}
export function signer(data,signature){return recoverAddress(typedHash(data),signature);}
const permitInterface=new Interface(['function permit(address owner,address spender,uint256 value,uint256 deadline,uint8 v,bytes32 r,bytes32 s)']);
export function permitCall(data,signature){
  const s=signature.slice(2),p=data.message;let v=parseInt(s.slice(128),16);if(v<27)v+=27;
  return permitInterface.encodeFunctionData('permit',[p.owner,p.spender,p.value,p.deadline,v,'0x'+s.slice(0,64),'0x'+s.slice(64,128)]);
}
export function signatureFromHook(app){
  const decoded=permitInterface.decodeFunctionData('permit',JSON.parse(app).metadata.hooks.pre[0].callData);
  return decoded.r+decoded.s.slice(2)+Number(decoded.v).toString(16).padStart(2,'0');
}
