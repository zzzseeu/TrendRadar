"""AI 新闻分类模块的共享契约。"""

POLICY = "policy"
RESEARCH = "research"
EXCLUDE = "exclude"

CLASSIFICATION_MODULE_TYPES = frozenset({POLICY, RESEARCH, EXCLUDE})
PERSISTED_MODULE_TYPES = frozenset({POLICY, RESEARCH})
