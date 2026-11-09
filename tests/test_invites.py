from gradebot.db import Database


def test_invitation_expiry_registration_and_block(tmp_path):
    db = Database(str(tmp_path / 'invites.sqlite3'))
    try:
        db.execute("INSERT INTO settings VALUES('admin','999')")
        token = db.create_invitation(3600)
        assert db.invitation_valid(token)
        assert db.begin_join(123, token)
        assert db.join(123, 'ivanov', 'Иванов Иван', token)
        assert db.authorized(123)
        assert db.all('SELECT name FROM users WHERE telegram_id=123')[0]['name'] == 'Иванов Иван'
        assert db.change_name(123, 'Петров Петр')
        assert db.all('SELECT name FROM users WHERE telegram_id=123')[0]['name'] == 'Петров Петр'
        assert db.block(123)
        assert not db.authorized(123)
        assert not db.begin_join(123, token)
        assert not db.join(123, '', 'Иванов Иван', token)
    finally:
        db.close()


def test_expired_invitation_cannot_register(tmp_path):
    db = Database(str(tmp_path / 'invites.sqlite3'))
    try:
        token = db.create_invitation()
        assert db.begin_join(123, token)
        db.execute('UPDATE invitations SET expires_at=0 WHERE token=?', (token,))
        assert not db.join(123, '', 'Иванов Иван', token)
        assert not db.authorized(123)
    finally:
        db.close()


def test_registration_filter_does_not_use_sqlite_from_worker_thread(tmp_path):
    import inspect

    from gradebot.bot import create_router

    db = Database(str(tmp_path / 'filter.sqlite3'))
    try:
        router = create_router(db, None)
        handler = next(item for item in router.message.handlers if item.callback.__name__ == 'join_name')
        assert inspect.iscoroutinefunction(handler.filters[0].callback)
    finally:
        db.close()
