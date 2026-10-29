# 合理用药审方字段对照系统 · 守护进程镜像
#
# 构建（仓库根目录执行）：
#   docker build -f deploy/Dockerfile -t medication-rule-fieldmap:0.1.0 .
#
# 镜像里**不含任何凭据**：HIS_BASE_URL / HIS_API_TOKEN / WECOM_WEBHOOK_URL
# 一律在运行时由 compose 的 env_file 从仓外注入。

FROM python:3.11-slim

# watchdog 走 inotify，locale 决定中文表头与中文归因文本的编码
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8

WORKDIR /opt/medication-rule-fieldmap

# 先装依赖再拷代码：改代码不重装依赖，层缓存还能复用
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY rules/ ./rules/
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir --no-deps . \
    && mkdir -p /var/lib/medication-rule-fieldmap/inbox \
                /var/lib/medication-rule-fieldmap/out \
                /var/lib/medication-rule-fieldmap/state

# 非 root 运行；inbox 是 HIS 落地目录的**只读**挂载
RUN useradd --system --home /opt/medication-rule-fieldmap --shell /usr/sbin/nologin mrfm \
    && chown -R mrfm:mrfm /var/lib/medication-rule-fieldmap /opt/medication-rule-fieldmap
USER mrfm

WORKDIR /var/lib/medication-rule-fieldmap

# 容器内自检：规则行能载入、表达式求值器不依赖动态执行
HEALTHCHECK --interval=60s --timeout=10s --start-period=10s --retries=3 \
  CMD python -m med_rule_fieldmap.cli rules validate --rules /opt/medication-rule-fieldmap/rules || exit 1

ENTRYPOINT ["python", "-m", "med_rule_fieldmap.daemon"]
CMD ["--inbox", "/var/lib/medication-rule-fieldmap/inbox", \
     "--rules", "/opt/medication-rule-fieldmap/rules", \
     "--out", "/var/lib/medication-rule-fieldmap/out", \
     "--state-db", "/var/lib/medication-rule-fieldmap/state/mrfm.sqlite3", \
     "--heartbeat-sec", "30"]
