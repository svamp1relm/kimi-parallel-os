**Kimi Parallel OS**
CLI для параллельного запуска независимых, сохранённых Kimi Code CLI сессий в любом проекте. Каждая задача получает собственный процесс, историю, статус и session_id; завершённую сессию можно открыть, пока остальные ещё выполняются.
**Установка**
Требуются Linux с /proc и flock, Python 3.12+, Kimi Code CLI и uv. На других платформах оркестратор завершится с явной ошибкой unsupported_platform.
uv sync --all-extras
 uv run parallel-agents --help

Для разработки используются uv run pytest и uv run ruff check .. После uv sync CLI вызывается через uv run parallel-agents либо через отдельную установку пакета с активированным окружением.
**Быстрый старт**
Создайте agents.json в проекте:
{
   "version": 1,
   "project": "/absolute/path/to/project",
   "max_parallel": 2,
   "tasks": [
     {
       "id": "backend",
       "prompt": "Реализуй backend-часть по спецификации.",
       "allowed_paths": ["src/backend", "tests/backend"]
     },
     {
       "id": "frontend",
       "prompt": "Реализуй frontend-часть по спецификации.",
       "allowed_paths": ["web"]
     }
   ]
 }

Готовый пример находится в examples/agents.json. Запуск из исходников:
uv run parallel-agents run agents.json
 uv run parallel-agents run agents.json --detach --json
 uv run parallel-agents list
 uv run parallel-agents status <run_id>

После завершения задачи статус содержит session_id и готовую команду:
cd /absolute/path/to/project && kimi --session <session_id>

--detach возвращается после supervisor handshake. Активная Kimi-сессия заблокирована её процессом; открывайте завершённые сессии через status или /sessions.
**Stop, recovery и retry**
parallel-agents stop <run_id> --task backend
 parallel-agents status <run_id>
 parallel-agents resolve <run_id> --task backend
 parallel-agents retry <run_id> backend --acknowledge-dirty-workspace

stop адресуется только live supervisor. После его потери состояние становится unknown; resolve сканирует процессы по уникальному PAO_ATTEMPT_ID, но никого не завершает. Если найден живой orphan, сначала проверьте его PID/PGID и завершите вручную, затем повторите resolve.
Retry создаёт новую Kimi-сессию и новую attempt, но сохраняет старые логи и использует тот же dirty workspace/worktree. Перед --acknowledge-dirty-workspace проверьте git status и git diff. Автоматических retry, clean, reset, merge, commit и push нет.
**Worktrees**
Для всех задач можно задать worktree на уровне manifest или индивидуально:
{
   "version": 1,
   "project": "/absolute/path/to/project",
   "worktree": {
     "enabled": true,
     "base_ref": "HEAD",
     "allow_dirty_base": false
   },
   "tasks": [
     {
       "id": "backend",
       "prompt": "Реализуй задачу.",
       "allowed_paths": ["src/backend"]
     }
   ]
 }

Worktree создаётся из зафиксированного commit. Незакоммиченные файлы туда не попадают; dirty base требует явного allow_dirty_base=true. При повторном retry тот же worktree сохраняется. Существующие ресурсы не перезаписываются и не удаляются автоматически.
**Export**
parallel-agents export <run_id> --format json
 parallel-agents export <run_id> --format markdown --output run.md
 parallel-agents export <run_id> --output run.json --force

Export включает только allowlist метаданных и session IDs. Он не содержит prompt, raw logs или содержимое файлов проекта. Файлы создаются атомически с правами 0600; перезапись требует --force.
**Безопасность и ограничения**
- allowed_paths — guardrails в prompt и проверка конфликтов конфигурации, **не OS sandbox**. Kimi сохраняет полномочия пользователя и может читать/изменять другие файлы, процессы, Git metadata и внешние сервисы. Используйте только trusted manifests, prompts и репозитории. Для untrusted input нужна внешняя изоляция ОС.
- Prompt передаётся через **argv** и виден в  **process list** другим процессам пользователя/системы. Не включайте secret, токены, пароли или содержимое секретных файлов в prompts.
- Raw logs и Kimi-managed sessions также могут содержать sensitive output. Они хранятся с owner-only permissions, но sanitization и удаление чувствительного содержимого не гарантируются. Export не очищает сами логи.
- PAO_HOME по умолчанию ~/.parallel_agentic_os; каталог должен быть реальным, не symlink, с правами 0700. БД, WAL, logs и exports получают 0600.
- Process groups и marker reconciliation не гарантируют отсутствие untracked descendants. Остановленные/unknown задачи требуют проверки перед retry.
- Наличие session ID не доказывает отсутствие изменений или ошибок. Для completed требуется exit code 0 и валидный session.resume_hint; nonzero exit остаётся failed даже при сохранённом ID.
**Коды выхода**
- 0 — успех или readable list/status/export;
- 2 — validation/unsafe state home;
- 3 — not found/state conflict/output exists;
- 4 — partial/external/setup/I/O failure;
- 5 — supervisor unavailable или unknown требует recovery.
Обычный test suite использует fake Kimi и не расходует квоту. Реальный smoke включается только явно через PAO_REAL_KIMI_SMOKE=1.
Проверка покрытия subprocess:
COVERAGE_PROCESS_START=.coveragerc uv run pytest --cov=parallel_agentic_os --cov-report=term-missing
