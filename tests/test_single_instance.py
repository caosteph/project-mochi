"""Only one Mochi may poll the bot token at a time.

Two processes polling one token both answer every message, so she sees each reply twice —
"you also just duplicated the message that you just sent me". That happened when a manual run
overlapped the launchd agent, and nothing prevented it.
"""

import fcntl

from app.main import acquire_single_instance_lock


def test_second_instance_cannot_acquire_the_lock(tmp_path):
    lock = tmp_path / "mochi.lock"
    assert acquire_single_instance_lock(lock) is True

    # A separate open file description = what a second process would have.
    with open(lock, "w") as other:
        try:
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired_twice = True
            fcntl.flock(other, fcntl.LOCK_UN)
        except OSError:
            acquired_twice = False
    assert acquired_twice is False, "a second instance must not be able to take the lock"


def test_lock_records_the_holding_pid(tmp_path):
    import os

    lock = tmp_path / "mochi.lock"
    assert acquire_single_instance_lock(lock) is True
    assert lock.read_text().strip() == str(os.getpid())  # so a human can see who holds it


def test_lock_directory_is_created_if_missing(tmp_path):
    lock = tmp_path / "nested" / "dir" / "mochi.lock"
    assert acquire_single_instance_lock(lock) is True
    assert lock.exists()
