# Статус проекта

## Текущее состояние

- Проект: `parallel_agentic_os`.
- Актуальный каталог после переименования: `/home/eleazar/kimi_parallel_os`.
- Назначение: переиспользуемый CLI для параллельного запуска независимых Kimi Code CLI сессий в других проектах.
- Этап: первая production-реализация завершена.
- Версия пакета: `0.1.0`.

## Реализовано

- JSON manifest v1 с проверкой задач, путей, лимитов и конфликтов.
- Независимые `kimi -p --output-format stream-json` процессы с сохранением session IDs.
- Foreground и detached supervisor с Unix control socket и handshake.
- SQLite attempt history, CAS transitions, cross-run workspace leases.
- Команды `run`, `list`, `status`, `stop`, `resolve`, `retry`, `export`.
- SIGTERM + проверяемая SIGKILL escalation, stop/spawn handling и descendant reconciliation.
- Immutable manifest snapshots, attempt logs, bounded output и owner-only state.
- Опциональные Git worktrees с immutable base commit и durable setup journal.
- Human/JSON output, structured errors, atomic exports без raw logs/prompts.
- README с quickstart, безопасностью, recovery и ограничениями Linux.

## Проверки

Последние результаты:

- `uv run pytest` — **190 passed, 1 skipped** (opt-in real-Kimi smoke).
- `uv run pytest --cov=parallel_agentic_os --cov-report=term-missing` — **89%**; минимальный порог 80% пройден, целевые 90% не достигнуты.
- `uv run ruff check .` — успешно.
- `uv build` — wheel и source distribution собраны успешно.
- `uv run parallel-agents --help` — CLI запускается.

Coverage subprocess включается через:

```bash
COVERAGE_PROCESS_START=.coveragerc uv run pytest --cov=parallel_agentic_os --cov-report=term-missing
```

Entrypoint-обёртки `__main__.py` и `worker.py` исключены из coverage; остальной production код включён.

## Инфраструктурные заметки

- `uv` установлен локально в `.bootstrap-venv`; project environment находится в `.venv`.
- Pre-commit hook не настроен. Рекомендации описаны в `.claude/hooks/recommended.md`.
- Глобальные настройки Claude/Kimi не изменялись.
- Коммит, push, merge и автоматический retry не выполнялись.
- Каталог не является Git-репозиторием, поэтому `git status`/`git diff --check` неприменимы.

## Реальный smoke test

- Run `pao-20022585551a`: две независимые реальные Kimi-сессии завершились со статусом `completed`.
- Alpha: `session_68931bda-2c78-42e8-a2e4-96def370465c`; файл `alpha/result.md` проверен.
- Beta: `session_2e713c79-a374-4fff-a361-19f39497fc7f`; файл `beta/result.md` проверен.
- Resume Alpha выполнен во время работы Beta и вернул `RESUME_OK` в той же сессии.
- JSON export содержит обе завершённые attempts и session IDs.
- Созданные smoke project и state удалены после проверки.
