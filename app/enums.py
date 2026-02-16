from enum import StrEnum


class SaveMode(StrEnum):
    SAVE_OFF = "SAVE_OFF"
    SAVE_BASIC = "SAVE_BASIC"
    SAVE_FULL = "SAVE_FULL"
    SAVE_PRIVATE_ONLY = "SAVE_PRIVATE_ONLY"
    SAVE_GROUPS_ONLY = "SAVE_GROUPS_ONLY"


class SourceType(StrEnum):
    PC_AGENT = "PC_AGENT"
    SERVER_AGENT = "SERVER_AGENT"
    CUSTOM = "CUSTOM"


class MessageEventType(StrEnum):
    CREATE = "create"
    EDIT = "edit"
    DELETE = "delete"

