"""Equivalent status counts without materializing the normalized graph view."""
import time

def summary(s):
    counts={}
    for table in ('candidates','observations','transfers','launches','activity','evidence','jobs','plans','watchlist','launch_hits'):
        physical=table
        if table=='transfers' and s.db.execute("SELECT type FROM sqlite_master WHERE name='transfers'").fetchone()[0]=='view':
            physical='transfer_cells'
        counts[table]=s.db.execute('SELECT count(*) FROM '+physical).fetchone()[0]
    return dict(at=time.time(),counts=counts,
                ath_quality={r[0]:r[1] for r in s.db.execute('SELECT ath_status,count(*) FROM candidates GROUP BY ath_status')},
                verified_all_time_top1000=False,complete_wallet_history=False,trading_enabled=False,
                coverage=s.coverage(),ticking=s.state('ticking'),archive=s.state('archive'))
