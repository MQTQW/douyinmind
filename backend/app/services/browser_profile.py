"""Serialize access to the project's persisted Douyin browser profile."""
from threading import Lock


persistent_profile_lock = Lock()
