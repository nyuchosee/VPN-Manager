from app.storage import Store


def test_review_keeps_last_progress_stage(tmp_path):
    store=Store(str(tmp_path/'bot.sqlite3'))
    try:
        store.upsert_user(111,'x')
        rid=store.create_request(111,'block')
        store.add_operation(rid,'block','x',{'test':True})
        store.operation_stage(rid,'update_sent')
        store.operation_stage(rid,'needs_review')
        row=store.db.execute('SELECT stage,last_progress_stage FROM operations WHERE request_id=?',(rid,)).fetchone()
        assert tuple(row)==('needs_review','update_sent')
        store.operation_stage(rid,'needs_review')
        row=store.db.execute('SELECT last_progress_stage FROM operations WHERE request_id=?',(rid,)).fetchone()
        assert row[0]=='update_sent'
    finally:
        store.close()
