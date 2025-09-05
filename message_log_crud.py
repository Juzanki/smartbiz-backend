from crud.message_log_crud import log_message
try:
    from crud.messages_crud import mark_as_sent
except Exception:
    def mark_as_sent(*args, **kwargs):
        return None
__all__ = ["log_message","mark_as_sent"]
