import React, {useEffect, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {PrivyProvider, usePrivy, useWallets, useConnectOrCreateWallet, useLogin, useModalStatus} from '@privy-io/react-auth';
import {arbitrum, arbitrumSepolia} from 'viem/chains';

// Privy owns authentication UI; the existing SKEW SIWE proof owns workspace access.
// No Privy token or app secret is sent to our wallet-auth endpoint.
let controller, pending, started;
const complete = (error, wallet) => {
  const attempt = pending;
  if (!attempt) return;
  pending = null;
  clearTimeout(attempt.timer);
  if (error) attempt.reject(new Error('Wallet login was cancelled or could not finish. Try again.'));
  else wallet.getEthereumProvider().then(provider => attempt.resolve({provider,
    id: 'privy-' + wallet.address.toLowerCase(), rdns: 'io.privy', name: 'Privy wallet'}), attempt.reject);
};
function Bridge() {
  const {ready, logout} = usePrivy();
  const {wallets, ready: walletsReady} = useWallets();
  const [loginCompleted, setLoginCompleted] = useState(0);
  const {isOpen} = useModalStatus();
  useLogin({onComplete: () => {
    if (pending) {pending.embeddedLogin = true; setLoginCompleted(value => value + 1);}
  }, onError: error => complete(error)});
  const {connectOrCreateWallet} = useConnectOrCreateWallet({
    onSuccess: ({wallet}) => complete(null, wallet), onError: error => complete(error),
  });
  useEffect(() => {
    controller = {ready: ready && walletsReady, wallets, connectOrCreateWallet, logout};
  }, [ready, walletsReady, wallets, connectOrCreateWallet, logout]);
  useEffect(() => {
    if (!pending?.embeddedLogin || !walletsReady) return;
    const embedded = wallets.find(wallet => wallet.walletClientType === 'privy');
    if (embedded) complete(null, embedded);
  }, [wallets, walletsReady, loginCompleted]);
  useEffect(() => {
    if (isOpen && pending) pending.opened = true;
    if (isOpen || !pending?.opened) return;
    const attempt = pending;
    const timer = setTimeout(() => {
      if (pending === attempt && !attempt.embeddedLogin) complete(new Error('Cancelled'));
    }, 200);
    return () => clearTimeout(timer);
  }, [isOpen]);
  return null;
}
class WalletErrorBoundary extends React.Component {
  state = {failed: false};
  static getDerivedStateFromError() {return {failed: true};}
  componentDidCatch() {complete(new Error('Wallet unavailable')); controller = null;}
  render() {return this.state.failed ? null : this.props.children;}
}
export async function initialize(config) {
  if (!started) {
    started = true;
    const host = document.createElement('div'); host.id = 'skew-privy-root'; document.body.append(host);
    createRoot(host).render(<WalletErrorBoundary><PrivyProvider appId={config.app_id}
      {...(config.client_id ? {clientId: config.client_id} : {})}
      config={{appearance: {theme: 'light', accentColor: '#175CFF', walletChainType: 'ethereum-only'},
        supportedChains: [arbitrum, arbitrumSepolia], defaultChain: arbitrum,
        embeddedWallets: {ethereum: {createOnLogin: 'users-without-wallets'}}}}>
      <Bridge />
    </PrivyProvider></WalletErrorBoundary>);
  }
  const deadline = Date.now() + 20000;
  while (!controller?.ready) {
    if (Date.now() > deadline) throw new Error('Privy is unavailable. Check the app’s allowed domains or use your browser wallet.');
    await new Promise(resolve => setTimeout(resolve, 100));
  }
}
export function connect() {
  if (!controller?.ready) return Promise.reject(new Error('Wallet is not ready.'));
  if (pending) return Promise.reject(new Error('Finish the open wallet request first.'));
  return new Promise((resolve, reject) => {
    pending = {resolve, reject, timer: setTimeout(() => complete(new Error('Expired')), 180000)};
    try {controller.connectOrCreateWallet();} catch (error) {complete(error);}
  });
}
export async function restore(hint) {
  const wallet = controller?.wallets.find(w => 'privy-' + w.address.toLowerCase() === hint?.id);
  if (!wallet) return null;
  return {id: hint.id, rdns: 'io.privy', name: 'Privy wallet', provider: await wallet.getEthereumProvider()};
}
export async function logout() {if (controller) await controller.logout();}
