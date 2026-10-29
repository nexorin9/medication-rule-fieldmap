"""规则行仓库：扫描规则目录、载入校验、过滤生效集、按文件变更热重载。

**对应源产品能力**：源仓库 ``core/workflow_actions/registries.py`` 的
``WorkflowActionType.prepare_values()`` / ``raise_if_misconfigured()``——
源产品在校验失败时拒绝跑不起来的已保存配置；本轮把同一套「载入期转换 + 校验」
用在审方规则行上：**非法规则拒绝生效，且不影响其余规则照常生效**。

仓库不做判定，只回答三个问题：现在有哪些规则生效、每条规则长什么样、
哪几条被拒了以及为什么。
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import yaml

from ..expression.cache import ParseTreeCache, default_cache
from ..expression.errors import ExpressionLanguageError
from ..models import RuleRow
from .schema import RuleValidationError, SchemaIssue, default_precheck, validate_rule

#: 规则文件后缀
RULE_SUFFIXES = (".yaml", ".yml")

#: 以 ``_`` 开头的文件视为模板/草稿，不参与载入
SKIP_PREFIX = "_"


@dataclass(frozen=True)
class RuleLoadError:
    """一条规则（或一个文件）的载入失败记录。

    ``issues`` 逐条给出字段名与定位信息，药事审核岗据此改配置。
    """

    path: str
    rule_id: str | None
    issues: tuple[SchemaIssue, ...]

    def __str__(self) -> str:
        target = f" [{self.rule_id}]" if self.rule_id else ""
        detail = "；".join(str(issue) for issue in self.issues)
        return f"{self.path}{target}：{detail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "rule_id": self.rule_id,
            "issues": [
                {
                    "field": issue.field,
                    "message": issue.message,
                    "line": issue.line,
                    "column": issue.column,
                }
                for issue in self.issues
            ],
        }


@dataclass(frozen=True)
class LoadReport:
    """一次载入的汇总。"""

    files_scanned: int
    loaded: int
    rejected: int
    errors: tuple[RuleLoadError, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "files_scanned": self.files_scanned,
            "loaded": self.loaded,
            "rejected": self.rejected,
            "errors": [error.to_dict() for error in self.errors],
        }


class RuleRepository:
    """规则行仓库。

    典型用法::

        repo = RuleRepository(rules_dir)
        repo.load()
        for rule in repo.effective_rules:
            ...
        repo.reload_if_changed()   # 守护进程每批次前调用
    """

    def __init__(
        self,
        rules_dir: str | Path,
        *,
        as_of: date | None = None,
        require_severity: bool = True,
        precheck: Callable[[str], list[SchemaIssue]] = default_precheck,
        cache: ParseTreeCache | None = None,
    ) -> None:
        self.rules_dir = Path(rules_dir)
        self.as_of = as_of or date.today()
        self.require_severity = require_severity
        self.precheck = precheck
        #: 表达式解析树缓存（缺省用进程级默认缓存）：规则文本热重载时，
        #: 改过的那条规则旧解析树立即作废，没改过的直接复用。
        self.cache = cache if cache is not None else default_cache()

        self._rules: dict[str, RuleRow] = {}
        self._errors: list[RuleLoadError] = []
        self._signature: tuple[tuple[str, int, int, str], ...] = ()
        self._loaded = False

    # -- 载入 ---------------------------------------------------------------

    def load(self) -> LoadReport:
        """扫描规则目录并载入全部规则行。

        非法规则只记录错误、不影响其余规则；重复 ``rule_id`` 保留先出现的一条，
        后出现的记为错误。
        """
        self._rules = {}
        self._errors = []

        files = list(self._iter_rule_files())
        for path in files:
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = path.read_text(encoding="utf-8", errors="replace")
                self._errors.append(
                    RuleLoadError(
                        self._rel(path),
                        None,
                        (SchemaIssue(None, "文件不是 UTF-8 编码，无法按规则行解析"),),
                    )
                )
                continue
            self._load_file(path, text)

        self._signature = self._compute_signature(files)
        self._loaded = True
        self._sync_cache()
        return LoadReport(
            files_scanned=len(files),
            loaded=len(self._rules),
            rejected=len(self._errors),
            errors=tuple(self._errors),
        )

    def _load_file(self, path: Path, text: str) -> None:
        rel = self._rel(path)
        try:
            documents = list(yaml.safe_load_all(text))
        except yaml.YAMLError as exc:
            self._errors.append(
                RuleLoadError(rel, None, (SchemaIssue(None, f"YAML 解析失败：{exc}"),))
            )
            return

        for index, document in enumerate(documents):
            if document is None:
                continue
            for raw, position in self._iter_rule_docs(document, index):
                self._load_one(rel, raw, position)

    @staticmethod
    def _iter_rule_docs(document: Any, index: int) -> Iterator[tuple[Any, int]]:
        """一个 YAML 文档可以是一条规则，也可以是 ``rules:`` 列表。"""
        if isinstance(document, dict) and "rules" in document:
            candidates = document.get("rules")
            if not isinstance(candidates, list):
                yield document, index
                return
            for offset, candidate in enumerate(candidates):
                yield candidate, index + offset
            return
        yield document, index

    def _load_one(self, rel: str, raw: Any, position: int) -> None:
        try:
            rule = validate_rule(
                raw,
                require_severity=self.require_severity,
                precheck=self.precheck,
                source_file=rel,
                cache=self.cache,
            )
        except RuleValidationError as exc:
            self._errors.append(RuleLoadError(rel, exc.rule_id, tuple(exc.issues)))
            return

        if rule.rule_id in self._rules:
            self._errors.append(
                RuleLoadError(
                    rel,
                    rule.rule_id,
                    (
                        SchemaIssue(
                            "rule_id",
                            f"规则号重复：{rule.rule_id} 已由 "
                            f"{self._rules[rule.rule_id].source_file} 定义",
                        ),
                    ),
                )
            )
            return
        self._rules[rule.rule_id] = rule

    # -- 热重载 -------------------------------------------------------------

    def reload_if_changed(self) -> bool:
        """规则文件有变更则重新载入，返回是否发生了重载。

        以（相对路径、mtime、大小、内容 sha256）联合判定变更——只比 mtime 在
        同一秒内连续改写时会漏判，内容哈希兜底。
        """
        files = list(self._iter_rule_files())
        signature = self._compute_signature(files)
        if self._loaded and signature == self._signature:
            return False
        self.load()
        return True

    @property
    def signature(self) -> tuple[tuple[str, int, int, str], ...]:
        """当前规则目录的变更签名。"""
        return self._signature

    # -- 查询 ---------------------------------------------------------------

    @property
    def rules(self) -> tuple[RuleRow, ...]:
        """全部载入成功的规则行（按规则号排序，不做生效区间过滤）。"""
        return tuple(self._rules[key] for key in sorted(self._rules))

    @property
    def effective_rules(self) -> tuple[RuleRow, ...]:
        """生效集：按 ``as_of`` 落在生效区间内的规则。"""
        return tuple(rule for rule in self.rules if rule.effective_on(self.as_of))

    @property
    def errors(self) -> tuple[RuleLoadError, ...]:
        """载入失败记录（非法规则在此，不影响其余规则生效）。"""
        return tuple(self._errors)

    def by_rule_id(self, rule_id: str) -> RuleRow | None:
        return self._rules.get(rule_id)

    def by_version(self, rule_id: str, version: str) -> RuleRow | None:
        """按规则号 + 版本取规则（判定幂等键含规则版本，回放时需要精确命中）。"""
        rule = self._rules.get(rule_id)
        if rule is not None and rule.version == version:
            return rule
        return None

    def versions_of(self, rule_id: str) -> tuple[str, ...]:
        return (self._rules[rule_id].version,) if rule_id in self._rules else ()

    # -- 内部 ---------------------------------------------------------------

    def _sync_cache(self) -> None:
        """把载入成功的规则绑定进解析树缓存；下线的规则解绑。

        这是「规则文本热重载失效」的落点：药事审核岗改了某条规则的表达式，
        重新载入时该规则的**旧解析树当场作废**并按新文本重新解析（不等 LRU
        自然淘汰）；没改过的规则继续复用旧树。

        缓存只存解析树，不存求值结果——求值依赖医嘱行上下文，缓存结果会引入
        过期语义。
        """
        for rule_id in self.cache.bound_rule_ids():
            if rule_id not in self._rules:
                self.cache.invalidate_rule(rule_id)
        for rule in self._rules.values():
            try:
                self.cache.get_for_rule(rule.rule_id, rule.expression)
            except ExpressionLanguageError:
                # 表达式本身站不住（载入期已记为错误）：不进缓存
                continue

    def _iter_rule_files(self) -> Iterator[Path]:
        if not self.rules_dir.is_dir():
            return iter(())
        found = [
            path
            for path in self.rules_dir.rglob("*")
            if path.is_file()
            and path.suffix.lower() in RULE_SUFFIXES
            and not path.name.startswith(SKIP_PREFIX)
            and not any(part.startswith(".") for part in path.relative_to(self.rules_dir).parts)
        ]
        found.sort(key=lambda item: self._rel(item))
        return iter(found)

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.rules_dir))
        except ValueError:
            return str(path)

    @staticmethod
    def _compute_signature(files: list[Path]) -> tuple[tuple[str, int, int, str], ...]:
        signature: list[tuple[str, int, int, str]] = []
        for path in files:
            try:
                stat = path.stat()
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                signature.append((str(path), stat.st_mtime_ns, stat.st_size, digest))
            except OSError:  # pragma: no cover - 文件在扫描与读取之间被删
                signature.append((str(path), 0, 0, ""))
        return tuple(signature)
