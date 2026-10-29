"""受限表达式子系统：词法 → 语法 → AST（后续接求值与载入期预检）。

本包对应源仓库 ``web-frontend/modules/core/formula/index.js`` 的
``getCachedParseTree()`` / ``resolveFormula()`` 这条主路径（公式文本 → 解析树 →
在行上下文里求值），本轮把「单元格公式」改写成「审方规则行的 expression」，
并把能力边界收窄到审方场景真正需要的范围。

模块分工：

* ``lexer.py`` —— 文本 → token（非法字符、引号未闭合在此暴露）；
* ``ast.py`` —— AST 节点（Compare / BoolOp / UnaryOp / Literal / FieldRef）；
* ``parser.py`` —— token → AST（递归下降；能力越界在此暴露）；
* ``validator.py`` —— 载入期预检（能力禁用清单 + 字段依赖声明核对）；
* ``evaluator.py`` —— 三态求值（MISSING / EXPLICIT_NO / VALUE）；
* ``cache.py`` —— 解析树 LRU 缓存（上限 512；规则文本变更即时失效）；
* ``normalize.py`` —— 求值前的归一层（单位 / 频次 / 枚举 / 量值 / 时间戳）。

**不使用 eval / exec / compile**：规则行由药事审核岗维护，表达式必须可复算、
可审计、可定位；动态执行会同时毁掉这三点。
"""

from __future__ import annotations

from .ast import BoolOp, Compare, FieldRef, Literal, Node, UnaryOp
from .cache import (
    PARSE_TREE_CACHE_MAX_SIZE,
    CacheStats,
    ParseTreeCache,
    cache_stats,
    default_cache,
    expression_digest,
)
from .errors import (
    CapabilityError,
    ExpressionLanguageError,
    LexError,
    ParseError,
)
from .lexer import COMPARISON_OPERATORS, Token, TokenType, tokenize
from .parser import Parser, parse
from .normalize import (
    CHINA_STANDARD_TIME,
    DAY_QUANTITY_UNITS,
    DRUG_CATEGORY_ALIASES,
    ENUM_FIELDS,
    ENUM_INDEX,
    FREQUENCY_ALIASES,
    FREQUENCY_INDEX,
    Frequency,
    NormalizedValue,
    QUANTITY_FIELDS,
    QUANTITY_SUFFIX_UNITS,
    RX_TYPE_ALIASES,
    SKIN_TEST_ALIASES,
    UNIT_ALIASES,
    UNIT_FIELDS,
    UNIT_INDEX,
    ValueKind,
    normalize_context,
    normalize_days_quantity,
    normalize_enum,
    normalize_field,
    normalize_frequency,
    normalize_quantity,
    normalize_row,
    normalize_timestamp,
    normalize_unit,
    normalize_values,
)
from .evaluator import (
    DEFAULT_EXPLICIT_NO_MARKERS,
    EvalState,
    EvaluationError,
    EvaluationResult,
    FieldResolution,
    FieldState,
    context_from_row,
    evaluate_ast,
    evaluate_expression,
    resolve_field,
)
from .validator import (
    CAPABILITY_BAN_LIST,
    CONFIG_ERROR_KINDS,
    CapabilityBan,
    ExpressionConfigError,
    ExpressionIssue,
    assert_valid_expression,
    capability_ban_table,
    validate_expression,
    validate_expression_syntax,
    validate_field_declaration,
)

__all__ = [
    "CAPABILITY_BAN_LIST",
    "CHINA_STANDARD_TIME",
    "CONFIG_ERROR_KINDS",
    "COMPARISON_OPERATORS",
    "DAY_QUANTITY_UNITS",
    "DEFAULT_EXPLICIT_NO_MARKERS",
    "DRUG_CATEGORY_ALIASES",
    "ENUM_FIELDS",
    "ENUM_INDEX",
    "FREQUENCY_ALIASES",
    "FREQUENCY_INDEX",
    "Frequency",
    "NormalizedValue",
    "PARSE_TREE_CACHE_MAX_SIZE",
    "QUANTITY_FIELDS",
    "QUANTITY_SUFFIX_UNITS",
    "RX_TYPE_ALIASES",
    "SKIN_TEST_ALIASES",
    "UNIT_ALIASES",
    "UNIT_FIELDS",
    "UNIT_INDEX",
    "ValueKind",
    "BoolOp",
    "CacheStats",
    "CapabilityBan",
    "CapabilityError",
    "Compare",
    "EvalState",
    "EvaluationError",
    "EvaluationResult",
    "ExpressionConfigError",
    "ExpressionIssue",
    "ExpressionLanguageError",
    "FieldRef",
    "FieldResolution",
    "FieldState",
    "LexError",
    "Literal",
    "Node",
    "ParseError",
    "ParseTreeCache",
    "Parser",
    "Token",
    "TokenType",
    "UnaryOp",
    "assert_valid_expression",
    "cache_stats",
    "capability_ban_table",
    "context_from_row",
    "default_cache",
    "evaluate_ast",
    "evaluate_expression",
    "expression_digest",
    "normalize_context",
    "normalize_days_quantity",
    "normalize_enum",
    "normalize_field",
    "normalize_frequency",
    "normalize_quantity",
    "normalize_row",
    "normalize_timestamp",
    "normalize_unit",
    "normalize_values",
    "parse",
    "resolve_field",
    "tokenize",
    "validate_expression",
    "validate_expression_syntax",
    "validate_field_declaration",
]
