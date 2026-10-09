"""Receipt-backed trading across the deliberately isolated transport windows."""
def recovery_trades(events,transactions):
    disconnect=next((e for e in events if e['kind']=='feed_disconnect_injected' and 'monotonic_ns' in e['body']),None)
    http=next((e for e in events if disconnect and e['kind']=='feed_http_recovered' and e['unix_ns']>disconnect['unix_ns'] and 'source_ns' in e['body']),None)
    ws=next((e for e in events if http and e['kind']=='feed_connected' and e['unix_ns']>http['unix_ns'] and 'monotonic_ns' in e['body']),None)
    def window(start,end):
        rows=[r for r in transactions if start is not None and r['state']=='CONFIRMED'
              and start<=r['body'].get('source_ns',0)<end
              and start<=r['body'].get('submit_ns',0)<=r['body'].get('confirmed_ns',0)<end]
        quotes={str(role):sum(r['role']==role and r['body']['op']==5 for r in rows) for role in (0,1)}
        fills=[r for r in rows if r['body']['op']==6 and r['body'].get('filled_lots',0)>0]
        return {'quotes_by_maker':quotes,'filled_ioc':len(fills),'filled_lots':sum(r['body']['filled_lots'] for r in fills),
                'passed':all(quotes.values()) and bool(fills),
                'signatures':[r['signature'] for r in rows if r['body']['op'] in (5,6)]}
    fallback=window(http['body']['source_ns'] if http else None,ws['body']['monotonic_ns'] if ws else float('inf'))
    reconnected=window(ws['body']['monotonic_ns'] if ws else None,float('inf'))
    pauses=[e for e in events if disconnect and http and disconnect['unix_ns']<e['unix_ns']<http['unix_ns']
            and e['kind']=='compiled_paused' and e['body'].get('reason')=='market stream stale']
    return {'http_fallback':fallback,'after_websocket_reconnect':reconnected,'stale_pause_roles':sorted(set(e['role'] for e in pauses)),
            'injected_socket_retry_rejections':sum(e['kind']=='feed_retry_rejected_injected' for e in events),
            'passed':bool(http and ws and fallback['passed'] and reconnected['passed'])}
