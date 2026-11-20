"""
Component ID: CMP_OPS_INSTANCE_LAUNCHER

Instance launcher: runs the assistant instances declared in ``instances.yaml`` (each
with its own port, data dir and optional Telegram bot) from the same checkout, venv and
shared config dir. All instances are equal. Entry point: ``python -m assistant.launcher``
(``make up / down / status``). Without instances.yaml, ``make start / stop`` run the
single classic process instead.
"""
