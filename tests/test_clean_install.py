"""Clean install must not recycle old Telegram request IDs."""
from app.storage import Store


def test_fresh_install_uses_new_request_namespace(tmp_path):
    path = str(tmp_path / "bot.sqlite3")
    store = Store(path)
    store.upsert_user(111)
    rid = store.create_request(111, "create", days=7)
    assert rid >= 10_000_000
    store.close()

    again = Store(path)
    assert again.request(rid) is not None
    again.upsert_user(222)
    nxt = again.create_request(222, "create", days=30)
    assert nxt == rid + 1
    again.close()
