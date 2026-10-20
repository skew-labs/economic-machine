"""Crash-durable shared network reservation, independent of ingest rollback."""
import sqlite3
import time

LIMIT = 1024**3

def reserve(store, amount, purpose):
    if type(amount) is not int or not 0 < amount <= 64*1024**2:
        raise ValueError('RESEARCH_IO_RESERVATION_BOUND')
    db = sqlite3.connect(store.root/'io-budget.sqlite3', timeout=5)
    try:
        db.execute('PRAGMA synchronous=FULL')
        db.execute('CREATE TABLE IF NOT EXISTS reservations(id INTEGER PRIMARY KEY,bytes INTEGER,purpose TEXT,at REAL)')
        db.execute('BEGIN IMMEDIATE')
        # Import once. The old counter remains evidence, never reset it.
        db.execute('INSERT OR IGNORE INTO reservations VALUES(0,?,?,?)',
                   (store.state('archive_network_reserved',0),'legacy_archive_reservation',time.time()))
        used=db.execute('SELECT coalesce(sum(bytes),0) FROM reservations').fetchone()[0]
        if used+amount > LIMIT:raise ValueError('ARCHIVE_CUMULATIVE_1GIB_LIMIT')
        db.execute('INSERT INTO reservations(bytes,purpose,at) VALUES(?,?,?)',(amount,purpose,time.time()))
        db.commit()
        return used+amount
    finally:db.close()
