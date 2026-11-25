# Git safety

- Не выполнять commit, push, merge, rebase, reset --hard или force push без явного запроса.
- Не удалять ветки, worktrees, незакоммиченные изменения или чужие untracked файлы.
- Перед worktree проверять существующий baseline и сообщать, что незакоммиченные файлы туда не попадут.
- Не использовать `--no-verify`.
- Перед завершением проверять `git status` и `git diff --check`, если каталог является Git-репозиторием.
