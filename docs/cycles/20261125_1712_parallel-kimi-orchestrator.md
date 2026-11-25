---
feature: parallel-kimi-orchestrator
cycle: 20261125_1712_parallel-kimi-orchestrator
phase: REVIEW
created: 2026-11-25 17:12
updated: 2026-11-26 00:20
---

# Parallel Kimi Orchestrator

## Scope Definition

### In Scope
- [ ] Декларативный JSON manifest независимых задач с проектом, prompt и разрешёнными путями.
- [ ] Параллельный запуск отдельных `kimi -p` процессов без скрытых subagents.
- [ ] Сохранение состояния, PID, exit code и `session_id` для открытия пользователем.
- [ ] Команды run/list/status/stop/retry/export и JSON output для интеграции.
- [ ] Проверка пересечений путей; опциональная подготовка Git worktrees.
- [ ] Unit и интеграционные тесты через fake Kimi CLI, без обязательного расхода квоты.
- [ ] Документация для интеграции в другие проекты.

### Out of Scope
- Web UI (пользователь выбрал CLI).
- Автоматический retry, merge, commit, push и автоматическое разрешение конфликтов.
- Хранение токенов и собственная авторизация.
- Redis/Kafka/job queue и распределённое выполнение.
- Гарантированное завершение внешней Kimi-сессии в заданный срок.
- OS-level sandbox. Ограничения путей являются guardrails, а не принудительной файловой изоляцией.

### Files to Change
- `pyproject.toml` (new)
- `src/parallel_agentic_os/` (new package)
- `tests/` (new tests)
- `README.md` (new)
- `examples/` (new example manifests)
- `docs/STATUS.md` (edit)

## Environment

### Scope
- Layer: Backend.
- Plugin: tdd-python.
- Risk: 100 (BLOCK)

### Runtime
- Language: Python 3.14.8 (project requirement Python >=3.12).

### Dependencies (key packages)
- Kimi Code CLI: 2.7.2.
- Git: 2.56.0.
- Runtime: только Python standard library.
- Development: pytest, pytest-cov, Ruff через uv lock.

### Risk Interview (BLOCK only)
- Risk type: Security + External integration + architecture.
- Авторизация: используется существующая авторизация Kimi CLI; токены не хранятся.
- Изоляция: обязательные разрешённые пути + опциональные Git worktrees.
- Ошибки: независимые задачи сохраняются; retry только вручную.
- Rate limits: `max_parallel` и отсутствие автоматического повторного расхода квоты.
- Альтернативы: внутренние subagents отклонены, поскольку пользователю нужны видимые независимые чаты.

## Context & Dependencies

### Reference Documents
- `CLAUDE.md` — правила проекта.
- `docs/STATUS.md` — текущая инфраструктура.
- Kimi Code CLI command reference: `kimi -p`, сохранённые session ID, `--session`, блокировка активной сессии.

## Test List

### TODO
- [ ] TC-01: Given валидный manifest, when загружается, then возвращаются независимые immutable задачи.
- [ ] TC-02: Given дублирующийся/недопустимый `task_id` или пустой prompt, when валидация, then понятная ошибка с именем поля.
- [ ] TC-03: Given абсолютный путь, `..` или symlink за проектом, when валидация, then запуск отклоняется.
- [ ] TC-04: Given одинаковые или вложенные allowed paths у задач общего project, when валидация, then конфликт отклоняется до spawn.
- [ ] TC-05: Given одинаковые относительные paths в разных worktrees, when валидация, then они допускаются; конфликт branch/worktree path отклоняется.
- [ ] TC-06: Given `max_parallel=0`, отрицательное или >16, when валидация, then запуск отклоняется; значение > числа tasks ограничивается их числом.
- [ ] TC-07: Given состояние SQLite, when attempt меняет статус, then сохраняются PID, timestamps, exit code и история попыток.
- [ ] TC-08: Given `session.resume_hint` в stream-json, when парсинг, then извлекается строго валидный session ID; отсутствие ID при exit 0 переводит attempt в failed.
- [ ] TC-09: Given fake Kimi с exit 0 и session ID, when выполнение, then attempt completed и сохранена resume command.
- [ ] TC-10: Given один failed и один успешный fake Kimi, when параллельный run, then успешная задача завершается, failed не отменяет её.
- [ ] TC-11: Given три задачи с задержкой и `max_parallel=2`, when run, then одновременно выполняются не более двух процессов.
- [ ] TC-12: Given активные две задачи, when stop одной, then завершается только её process group, другая продолжается.
- [ ] TC-13: Given failed/stopped attempt, when ручной retry, then создаётся новая attempt и новая Kimi-сессия, старые logs/history сохраняются.
- [ ] TC-14: Given running/completed attempt, when retry, then операция отклоняется с указанием допустимого состояния.
- [ ] TC-15: Given повторяющаяся внешняя ошибка, when run, then новые attempts не создаются автоматически.
- [ ] TC-16: Given валидная worktree-конфигурация, when подготовка, then `git worktree add` вызывается без shell с новой веткой и уникальным path.
- [ ] TC-17: Given существующие branch/worktree path или не-Git project, when подготовка, then запуск отклоняется без удаления/перезаписи.
- [ ] TC-18: Given detached run с fake Kimi, when команда возвращается, then пользователь получает run ID, а supervisor завершает задачи в фоне.
- [ ] TC-19: Given сохранённые runs/attempts, when list/status с human/JSON output, then отображаются статусы, session IDs и команды продолжения.
- [ ] TC-20: Given завершённый run, when export JSON/Markdown, then включаются метаданные, но не prompt logs и содержимое файлов проекта.
- [ ] TC-21: Given отсутствующий исполняемый `kimi`, when run, then задача получает actionable failed status, остальные продолжаются.
- [ ] TC-22: Given ненулевой exit с session ID, when завершение, then статус failed, но session ID сохраняется для ручного resume/retry.
- [ ] TC-23: Given state/logs, when создание, then доступ ограничен владельцем; export не раскрывает логи.
- [ ] TC-24: Given CLI end-to-end с fake `kimi`, when полный сценарий, then run → status → retry → export работает без реальной квоты.

### WIP
(none)

### DISCOVERED
(none)

### DONE
(none)

## Implementation Notes

### Goal
Дать пользователю возможность запускать несколько видимых Kimi-сессий параллельно в любом проекте, проверять завершённые, продолжать их и координировать независимые изменения.

### Background
Текущий ручной workflow требует отдельных `/fork` и терминалов. Внутренние subagents не подходят, поскольку пользователю нужны самостоятельные чаты, которые он может открыть и продолжить. Kimi `-p` создаёт сохранённую сессию и печатает resume hint после завершения; активная сессия заблокирована процессом, поэтому координатор должен показывать resume command прежде всего для завершённых attempts.

### Design Approach

Нормативная архитектура: supervisor владеет процессами через live handles и per-attempt mutexes, принимает stop/retry только через Unix control socket и удерживает run lock. Внешние команды никогда не сигналят PGID из SQLite. Spawn использует точный argv `[kimi, -p, composed_prompt, --output-format, stream-json]`, `cwd=effective_root`, `shell=False`, `start_new_session=True` и уникальный `PAO_ATTEMPT_ID` в environment. Состояние хранится в SQLite с WAL, CAS transitions и cross-run workspace leases. Все изменения после первого review являются частью этого единого дизайна, а не альтернативными вариантами.

`pending → spawning → running → stopping → completed|failed|stopped`, с дополнительными `setup_failed` и `unknown`. Exit 0 без session ID = `failed/session_id_missing`; nonzero с ID = failed с сохранённым resume. Ошибка задачи не отменяет остальные. Retry создаёт новую Kimi-сессию только после явного `--acknowledge-dirty-workspace`, использует тот же task-owned dirty worktree и никогда не делает clean/reset. Постоянные global auth/quota failures могут привести к одной failed attempt на каждую запрошенную task, но никогда к автоматическим новым attempts.

Runtime — только stdlib. `models.py`, `manifest.py`, `state.py`, `process.py`, `worktree.py`, `orchestrator.py`, `worker.py`, `cli.py` отделяют домен, side effects и интерфейс. Обычные тесты используют fake `kimi`; реальный smoke — opt-in.

## Design Corrections After Plan Review

Первый plan-review вернул BLOCK (максимум 96): stale PGID, crash recovery, stop/spawn/retry races, retry worktrees и неполный CLI contract. Объём команд не сокращается; устраняются причины замечаний.

**Supervisor-owned lifecycle.** На каждый run создаётся Unix domain control socket и lock file с `flock`, удерживаемый supervisor всё время жизни. Только supervisor владеет live `Popen` handles и выполняет stop/retry для активного run. Внешняя команда не сигналит PGID из SQLite. Detached start считается успешным только после socket-ready handshake; stdin/stdout/stderr supervisor откреплены в отдельный log. Повторный supervisor для того же run невозможен из-за lock.

**Stop и гонки.** Scheduler атомарно проверяет stop перед claim и сразу после spawn. При stop pending задачи получают stopped и больше не запускаются; активным детям supervisor посылает SIGTERM, после фиксированного grace period — SIGKILL. Неубиваемый/неподтверждённый результат становится `unknown`, а не stopped. Stop идемпотентен. Гарантия ограничивается process group: escaped descendants обозначаются как ограничение, а не как полная containment.

**Crash recovery.** В SQLite сохраняются spawn intent, supervisor generation, heartbeat, PID, process start time и PGID. При следующем вызове reconciliation проверяет nonterminal attempts. Совпадение PID+start time+PGID при потерянном supervisor даёт `unknown/supervisor_lost` без signal/adoption/auto-retry. Отсутствие процесса позволяет прочитать log и перевести attempt в `failed/supervisor_lost`, сохранив найденный session ID. Отсутствие достоверной process identity всегда fail-closed = `unknown`, stop/retry запрещены до ручного разрешения. Состояния run/task/attempt включают `setup_failed`, `stopping`, `unknown`; structured `failure_reason` отделён от статуса.

**Бизнес-атомарность.** Все transitions выполняются через `BEGIN IMMEDIATE` compare-and-swap. Ограничения SQLite: уникальный `(run_id, task_id, attempt_no)` и не более одной active attempt на task. Retry только для текущей terminal failed/stopped attempt; duplicate retry видит уже активную attempt и отклоняется. Retry и stop активного run проходят только через supervisor socket. Неизвестные статусы не считаются terminal.

**Worktree lifecycle.** Task владеет одним worktree на все attempts. Первый запуск создаёт уникальные run/task branch и path; manual retry создаёт новую Kimi-сессию, но намеренно использует тот же dirty worktree без clean/reset. Prompt новой attempt требует сначала проверить `git status/diff` и сохранить изменения предыдущей attempt. Все effective roots и allowed paths валидируются до side effects; регистрируются base commit и каждый созданный resource. При частичном setup failure — `setup_failed`, список созданных resources для ручной проверки, без автоматического удаления и без spawn. Dirty base требует явного `allow_dirty_base=true`; HEAD не содержит uncommitted files. Общие Git refs/object metadata не являются полной изоляцией.

**Kimi contract.** Каждый spawn строго `[kimi, -p, composed_prompt, --output-format, stream-json]` с `cwd=effective_root`, `shell=False`. JSONL parser принимает только `session.resume_hint` со строгим `session_` + UUID, отклоняет malformed/ambiguous IDs. Exit 0 без валидного ID = failed; nonzero с валидным ID = failed с сохранённым resume. Human resume = shell-quoted `cd <effective_root> && kimi --session <id>`, JSON resume = `{cwd, argv}`. Prompt в argv и logs являются чувствительными; secrets в prompts запрещены документацией. Environment не выводится в logs.

**Manifest v1.** Один `project` на manifest, 1–64 tasks, `max_parallel` integer 1–16 (default min(4, tasks)); превышение числа tasks ограничивается фактическим числом задач. Unknown fields, empty tasks/prompt/allowed_paths, whitespace ID, unsupported version отклоняются. `id` соответствует `[a-z][a-z0-9-]{0,63}`. Allowed path — относительный, непустой и не `.`; пустой список запрещён. Worktree fields: enabled, optional base_ref и allow_dirty_base; unique branch/path генерируются оркестратором. Все ошибки возникают до spawn.

**CLI/output/errors.** `run MANIFEST [--detach]`, `list`, `status RUN [--task ID] [--attempt N]`, `stop RUN [--task ID]`, `resolve RUN [--task ID]`, `retry RUN TASK [--detach] --acknowledge-dirty-workspace`, `export RUN [--format json|markdown] [--output PATH|--output -] [--force]`. Runs сортируются created desc, tasks по manifest, attempts по номеру desc. Active attempt без session ID сообщает, что ID появится после завершения; completed можно открыть, failed можно проверить/resume или retry в новой сессии. Exit codes: 0 success/all completed, 2 validation, 3 not-found/state conflict, 4 partial/external failure, 5 supervisor unavailable/unknown. JSON имеет `schema_version=1`, stable `code/message/hint/context`, stdout не смешивается с ошибками. Export содержит allowlist metadata и session IDs, но не prompt/raw logs; stdout по умолчанию, файл пишется атомически 0600 без overwrite без `--force`.

**Trust boundary.** Manifest, prompt и project должны быть trusted. Kimi получает полномочия пользователя; guardrails не мешают чтению secrets, записям вне paths, shared Git/process/external side effects. Для untrusted input требуется внешняя OS sandbox, которая не входит в версию. Каталог state/home проверяется на owner/symlink, DB/WAL/logs/exports 0600, домашний каталог 0700.

### Additional Test List (TODO)
- [ ] TC-25: exact argv/cwd и stream-json parser; malformed, ANSI, duplicate и разные session IDs отклоняются.
- [ ] TC-26: resume completed session в fake CLI, пока другая task остаётся active; real-Kimi smoke — opt-in, не обязательный для обычного suite.
- [ ] TC-27: supervisor crash до/после spawn и status commit; reconciliation даёт failed или unknown по доказанной identity, никогда не сигналит stale PGID.
- [ ] TC-28: stop между claim/spawn, ignored SIGTERM и escalation; pending больше не стартует, escaped descendants документированы.
- [ ] TC-29: два concurrent retry и duplicate supervisor; только один wins CAS/lock, активная workspace не получает параллельную attempt.
- [ ] TC-30: retry worktree-backed task сохраняет dirty changes и branch/path, создаёт новую Kimi attempt/session.
- [ ] TC-31: partial worktree failure не запускает процессы, возвращает setup_failed и точный список resources без автоудаления.
- [ ] TC-32: manifest malformed JSON, unknown fields, empty lists, bad types, whitespace, unsupported version и max_parallel boundaries.
- [ ] TC-33: golden list/status для empty/running/mixed/failed-with-session/unknown; deterministic ordering и корректный resume только terminal.
- [ ] TC-34: structured errors и exit codes для invalid input, not found, conflict, unavailable supervisor и I/O failure.
- [ ] TC-35: export для running/mixed/failed, deterministic allowlist, no prompt/raw logs, stdout/atomic file/no overwrite.
- [ ] TC-36: state symlink/permissions, sensitive argv docs, logs не попадают в error/export.
- [ ] TC-37: fake process harness синхронизируется events, а не sleeps; удаление `kimi` из PATH доказывает отсутствие реальных вызовов.
- [ ] TC-38: partial setup с разными effective roots повторно проверяет path overlap/symlink именно в execution cwd.

## Consolidated Lifecycle and Recovery Contract

**Cross-run workspace leases.** Одна общая SQLite БД всех runs содержит leases `(canonical_root, allowed_path, run_id, task_id, state)`. В `BEGIN IMMEDIATE` перед setup/spawn проверяются все active leases во всех runs; совпадение root и ancestor/descendant paths отклоняется. Lease удерживается в pending/running/stopping/unknown и освобождается только после подтверждённого terminal/resolution. Два разных manifest не могут писать в один workspace даже с разными supervisors.

**Stop/reap сериализация.** Каждая attempt имеет lifecycle mutex для state/identity checks, signal-versus-reap critical sections и final reaping. Блокирующий `wait()` выполняется строго вне mutex одним назначенным reaper, поэтому stop может прервать ожидание. Mutex не удерживается ни во время wait, ни во время grace sleep; перед каждым signal и при финализации identity/state проверяются заново под mutex. После reap старый PGID никогда не используется для signal. Отложенный SIGKILL выполняется под тем же mutex с проверкой, что leader ещё жив и process identity совпадает; подтверждённое завершение отменяет escalation. Attempt получает stopped только при отсутствии живых процессов с её marker и членов исходной PGID, иначе `unknown/escaped_descendants`.

**Marker-based resolve.** Каждый child получает уникальный `PAO_ATTEMPT_ID` и PGID. `resolve RUN [--task ID]` ничего не убивает: сканирует `/proc` только текущего UID на точный marker, не выводя остальное environment. Если найдены живые processes, выводит их PID/PGID/start time и инструкцию оператору проверить и при необходимости завершить вручную; stop/retry остаются запрещены. Если marker и PGID отсутствуют, сохраняет session ID из log и переводит attempt в `failed/supervisor_lost`, после чего допускается manual retry. Pending без spawn intent можно перевести в stopped. `status` для unknown содержит точную команду resolve. Spawn без DB commit находится по marker, поэтому не остаётся без восстановительного пути.

**Setup journal.** Перед каждым Git side effect сохраняются intent `(run/task, operation, base commit, generated branch/path)`, после него — результат. При reconcile журнал сопоставляется с фактическими worktrees/refs/paths; ничего не удаляется автоматически. Не зарегистрированный из-за crash resource обнаруживается по уникальному run/task имени и показывается оператору.

**CLI completion contract.** Foreground run/retry ждут terminal state; detached возвращаются только после socket-ready handshake. Stop возвращает accepted после подтверждения supervisor, а не утверждает немедленную остановку; повторный stop идемпотентен. `status/list` возвращают 0 для читаемого состояния, включая running/mixed, но содержат aggregate и failure details; run foreground = 4 при partial/external failure, supervisor unavailable/unknown = 5. Failed status включает `failure_reason`, child exit code и owner-only log path. Detached handshake timeout/failure не возвращает успешный run ID.

**Resume и retry.** Human resume: shell-quoted `cd <effective_root> && kimi --session <session_id>`. JSON: `{cwd, argv}`; TC-26 запускает из unrelated cwd. Retry требует `--acknowledge-dirty-workspace`, потому что новая сессия не наследует контекст старой и workspace может содержать изменения. Exclusivity гарантируется для supervised/marker processes; untracked processes остаются задокументированным ограничением.

**Export overwrite.** Synopsis `export ... [--force]`; без force существующий файл не изменяется. Сырые logs могут содержать чувствительный output несмотря на отсутствие токенов в prompts; retention/deletion настраивается пользователем, sanitization не обещается.

### Consolidation Tests (TODO)
- [ ] TC-39: два manifests/supervisors с overlapping paths получают один lease; unknown lease блокирует новый run до resolve.
- [ ] TC-40: event-driven stop/reap перед SIGTERM, в grace и перед SIGKILL никогда не сигналит reused PGID.
- [ ] TC-41: unknown с живым orphan → ручное завершение оператором → resolve без marker/PGID → failed/supervisor_lost → acknowledged retry.
- [ ] TC-42: crash после spawn до PID commit находится по PAO_ATTEMPT_ID; pending без intent безопасно завершается.
- [ ] TC-43: leader exited, но descendant с marker жив — stopped/retry запрещены до resolve; escaped descendants не создают ложной exclusivity.
- [ ] TC-44: crash до/после каждой Git mutation и DB commit восстанавливается по setup journal без автоматического удаления.
- [ ] TC-45: dirty base default reject, allow_dirty_base=true accept + base commit; handshake failure не выдаёт false success.
- [ ] TC-46: run/stop/retry JSON accepted/terminal contracts, idempotent stop, failed details и exit codes.
- [ ] TC-47: resume из unrelated cwd сохраняет exact effective_root в human и JSON forms.
- [ ] TC-48: постоянные global quota/auth failures не вызывают auto-retry; поведение one-attempt-per-task явно проверено.

## Progress Log

### 2026-11-25 17:12 - INIT
- Scope и риски подтверждены пользователем.
- Создан Cycle doc.

### 2026-11-25 17:20 - PLAN
- Подтверждены архитектура supervisor/SQLite, guardrails, worktrees и failure semantics.
- Сформированы 24 тестовых сценария, включая abnormal/security/external dependency cases.
- 2026-11-25 [PLAN] plan-review BLOCK (score 96): stale PGID/crash recovery, stop/retry races, worktree retry и CLI contract требовали переработки.
- 2026-11-25 [PLAN] замечания устранены supervisor socket/lock, fail-closed reconciliation, CAS transitions, task-owned worktrees и нормативными manifest/CLI contracts; добавлены TC-25…TC-38.

### 2026-11-26 00:20 - REVIEW
- Production-реализация завершена: manifest, state, process/worktree lifecycle, supervisor, recovery, CLI и export.
- Добавлены regression tests на stop/spawn, SIGKILL escalation, immutable manifest, setup journal, bounded output и CLI contracts.
- `uv run pytest`: 190 passed, 1 opt-in skipped; Ruff и `uv build` успешны.
- Coverage: 89%; минимум 80% пройден, целевые 90% не достигнуты.
- Коммит и push не выполнялись; решение о коммите остаётся за пользователем.

## Next Steps

1. [Done] INIT
2. [Done] PLAN
3. [Done] RED
4. [Done] GREEN
5. [Done] REFACTOR
6. [Done] REVIEW
7. [ ] COMMIT по отдельному решению пользователя
